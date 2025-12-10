import datetime
import json
import time
from typing import ClassVar, Optional
import asyncio
import requests
from dateutil.parser import parse as parse_datetime
from maim_message import UserInfo, GroupInfo

from src.manager.async_task_manager import AsyncTask, async_task_manager
from src.common.database.database_model import ActionRecords # 确保引入数据库模型
from src.chat.message_receive.chat_stream import ChatStream
from src.common.logger import get_logger
from src.plugin_system import (
    ActionActivationType,
    BaseAction,
    BasePlugin,
    ComponentInfo,
    ConfigField,
    register_plugin,
)

from src.plugin_system.apis import database_api, generator_api, llm_api, send_api, person_api

logger = get_logger("reminder_plugin")
_plugin_instance = None


class DatabaseReminderTask(AsyncTask):
    """负责执行单个提醒任务：等待 -> 生成回复 -> 发送 -> 更新数据库"""

    def __init__(
            self,
            delay: float,
            action_id: str,
            chat_stream: ChatStream,
            user_id: str,
            user_name: str,
            event_details: str,
            creator_name: str,
            group_id: Optional[str] = None
    ):
        # 使用 action_id 作为任务名的后缀，防止重复
        super().__init__(task_name=f"db_reminder_{action_id}")
        self.delay = delay
        self.action_id = action_id
        self.chat_stream = chat_stream
        self.user_id = user_id
        self.user_name = user_name
        self.event_details = event_details
        self.creator_name = creator_name
        self.group_id = group_id

    async def run(self):
        try:
            # 1. 倒计时
            if self.delay > 0:
                logger.info(f"[ReminderTask] 任务 {self.event_details} 进入等待，时长: {self.delay:.2f}s")
                await asyncio.sleep(self.delay)

            # 2. 二次检查数据库 (Double Check)
            # 防止在等待期间，用户通过其他方式删除了该提醒，或者程序重启导致重复执行
            # 这里简单查询一下状态
            records = await database_api.db_query(
                model_class=ActionRecords,
                query_type="get",
                filters={"action_id": self.action_id, "action_done": False}
            )
            if not records:
                logger.info(f"[ReminderTask] 任务 {self.event_details} 已失效或被完成，跳过执行。")
                return

            logger.info(f"[ReminderTask] 触发提醒: {self.event_details}")
            if self.group_id and _plugin_instance:
                try:
                    napcat_url = _plugin_instance.get_config("napcat.url")
                    ACCESS_TOKEN = _plugin_instance.get_config("napcat.token")
                    if napcat_url and ACCESS_TOKEN:
                        # 构造 NapCat / OneBot 11 的请求 Payload
                        url = f"{napcat_url}/send_group_msg"
                        payload = {
                            "group_id": self.group_id,
                            "message": [
                                {
                                    "type": "at",
                                    "data": {
                                        "qq": self.user_id
                                    }
                                }
                            ]
                        }
                        headers = {
                            'Authorization': f"Bearer {ACCESS_TOKEN}",
                            'Content-Type': 'application/json'
                        }

                        logger.debug(f"[ReminderTask] 正在通过 HTTP 发送群聊 At: {url}, payload: {payload}")

                        response = requests.post(url, json=payload, headers=headers, timeout=5)
                        response.raise_for_status()
                        logger.info(f"[ReminderTask] At 消息发送成功: {response.text}")
                    # 稍微停顿一下，确保At消息先到达
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"[ReminderTask] 发送At消息失败: {e}", exc_info=True)
            person_id = person_api.get_person_id("qq", self.user_id)
            print(person_id)
            person_nickname = await person_api.get_person_value(person_id, "person_name")
            extra_info = f"**现在是提醒时间，请你以一种符合你人设的的方式**巧妙**地提醒 {person_nickname}。\n提醒内容: {self.event_details}**"

            # 调用 rewrite_reply 进行润色
            # 这会利用 Expressor 根据人设和历史记录重写这句话
            success, reply_model = await generator_api.rewrite_reply(
                chat_stream=self.chat_stream,
                raw_reply=extra_info,
                reason="原始提醒消息太死板了，需要用符合人设的语气进行即时提醒",  # 告诉LLM为什么要重写
                enable_splitter=True,  # 允许将长消息切分成多条发送
            )

            # 4. 发送消息
            if success and reply_model and reply_model.content:
                # rewrite_reply 返回的是处理好的 content，直接发送即可
                await send_api.text_to_stream(
                    text=reply_model.content,
                    stream_id=self.chat_stream.stream_id,
                )
            else:
                # 兜底：如果重写失败，直接发送原始文本
                await send_api.text_to_stream(
                    text=extra_info,
                    stream_id=self.chat_stream.stream_id,
                )

            # 5. 更新数据库状态为已完成
            await database_api.db_query(
                model_class=ActionRecords,
                query_type="update",
                filters={"action_id": self.action_id},
                data={"action_done": True}
            )
            logger.info(f"[ReminderTask] 任务 {self.event_details} 完成并更新数据库。")

        except Exception as e:
            logger.error(f"[ReminderTask] 执行失败: {e}", exc_info=True)


class ReminderScheduler(AsyncTask):
    """后台调度器：定期扫描数据库，将未来24小时内的任务加载到内存"""

    def __init__(self, scan_interval: int, window_seconds: int):
        super().__init__(task_name="Global_Reminder_Scheduler")
        self.scan_interval = scan_interval  # 使用配置参数
        self.window_seconds = window_seconds  # 使用配置参数
        self.active_tasks = set()

    async def run(self):
        logger.info("[Scheduler] 提醒事项调度器已启动 (24小时滑动窗口模式)")
        while True:
            try:
                now = time.time()
                future_limit = now + self.window_seconds

                # 1. 查询所有未完成的提醒
                pending_reminders = await database_api.db_query(
                    model_class=ActionRecords,
                    query_type="get",
                    filters={"action_name": "reminder_item", "action_done": False}
                )

                if pending_reminders:
                    count = 0
                    for record in pending_reminders:
                        try:
                            action_id = record["action_id"]

                            # 如果任务已经在运行（或者在上次扫描中已加入），则跳过
                            # 注意：这里需要配合 async_task_manager 的状态管理，
                            # 简单起见，我们假设只要在这里加入过就不再加，除非重启
                            if action_id in self.active_tasks:
                                continue

                            data = json.loads(record["action_data"])
                            remind_time = data.get("remind_time", 0)

                            if remind_time < (now - 5):
                                logger.warning(
                                    f"[Scheduler] 发现过期任务 {action_id} (原定时间已过)，正在静默标记为完成...")

                                # 必须更新数据库，否则下次扫描还会扫到它
                                await database_api.db_query(
                                    model_class=ActionRecords,
                                    query_type="update",
                                    filters={"action_id": action_id},
                                    data={"action_done": True}
                                )
                                continue  # 跳过本次循环，不添加任务
                            # 核心逻辑：只加载 [过去未执行] 到 [未来24小时] 之间的任务
                            # 如果重启时有过去的任务没执行，这里也会立即触发（delay < 0）
                            if remind_time <= future_limit:
                                delay = remind_time - now

                                # 尝试从数据中获取群组信息并注入 stream
                                group_id = data.get("group_id")
                                group_platform = data.get("platform")

                                user_info = UserInfo(
                                    user_id=data["user_id"],
                                    user_nickname=data["user_name"],
                                    platform=group_platform or record["chat_info_platform"]
                                )

                                # 构建 ChatStream (从记录中恢复最基础的流信息)
                                # 注意：这里是一个简化构建，实际可能需要更完整的上下文恢复，
                                # 但用于发送消息通常足够了
                                temp_stream = ChatStream(
                                    stream_id=record["chat_id"],
                                    platform=record["chat_info_platform"],
                                    user_info=user_info
                                )

                                if group_id:
                                    temp_stream.group_info = GroupInfo(
                                        group_id=group_id,
                                        platform=group_platform or record["chat_info_platform"],
                                        group_name="Group"  # 数据库未存群名，暂时给默认值
                                    )
                                # 填充 group_info 如果是群聊 (简单判断逻辑，视具体数据结构而定)
                                # 如果你的 record 数据里存了 group_id 可以这里恢复，否则默认为私聊或通用处理

                                task = DatabaseReminderTask(
                                    delay=delay,
                                    action_id=action_id,
                                    chat_stream=temp_stream,
                                    user_id=data["user_id"],
                                    user_name=data["user_name"],
                                    event_details=data["event_details"],
                                    creator_name=data["user_name"],  # 简化，默认创建者是用户自己
                                    group_id=group_id
                                )

                                await async_task_manager.add_task(task)
                                self.active_tasks.add(action_id)
                                count += 1
                        except Exception as e:
                            logger.error(f"[Scheduler] 解析单条记录失败: {e}")

                    if count > 0:
                        logger.info(f"[Scheduler] 本轮扫描新加载了 {count} 个近期任务。")

            except Exception as e:
                logger.error(f"[Scheduler] 扫描循环出错: {e}", exc_info=True)

            # 等待下一次扫描
            await asyncio.sleep(self.scan_interval)
# =============================== Action ===============================


class RemindAction(BaseAction):
    """一个能从对话中智能识别并设置定时提醒的动作。"""

    # === 基本信息 ===
    action_name = "set_reminder"
    action_description = "根据用户的对话内容，智能地设置一个未来的提醒事项（仅限提醒自己）。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["提醒", "叫我", "记得", "别忘了"]
    parallel_action = False

    # === LLM 判断与参数提取 ===
    #llm_judge_prompt = ""
    action_parameters = {  # 修正：添加了这部分
        "remind_time": "提醒时间，格式为YYYY-MM-DD HH:MM:SS",
        "event_details": "用一句话概括提醒内容.",
    }
    action_require = [
        "当用户请求在未来的某个时间点提醒自己某件事时使用",
        "适用于包含明确时间信息和事件描述的对话，例如：'10分钟后提醒我收快递'、'明天早上九点提醒我开会'",
        "如果用户的请求中**没有**包含具体的时间，例如用户说：'提醒我收快递'(没有日期和时间)、'提醒我明天开会'(没有时间)。此时**你应当使用reply action**，提醒他要告诉你具体时间，**且在告知你时间的消息中要包含‘提醒’这个关键词**，这样你的待办事项模块才会识别并工作。在**获取到了确切时间后**再使用本action设置提醒事项",
    ]

    async def execute(self) -> tuple[bool, str]:
        """执行设置提醒的动作"""
        remind_time_str = self.action_data.get("remind_time")
        event_details = self.action_data.get("event_details")

        if not all([remind_time_str, event_details]):
            missing_params = [
                p
                for p, v in {
                    "remind_time": remind_time_str,
                    "event_details": event_details,
                }.items()
                if not v
            ]
            error_msg = f"缺少必要的提醒参数: {', '.join(missing_params)}"
            logger.warning(f"[ReminderPlugin] LLM未能提取完整参数: {error_msg}")
            return False, error_msg

        # 1. 解析时间
        try:
            assert isinstance(remind_time_str, str)
            # 优先尝试直接解析
            try:
                target_time = parse_datetime(remind_time_str, fuzzy=True)
            except Exception as e:
                # 如果直接解析失败，调用 LLM 进行转换
                logger.info(f"[ReminderPlugin] 直接解析时间 '{remind_time_str}' 失败，尝试使用 LLM 进行转换...")

                # 获取所有可用的模型配置
                available_models = llm_api.get_available_models()
                if "utils_small" not in available_models:
                    raise ValueError("未找到 'utils_small' 模型配置，无法解析时间") from e

                # 明确使用 'planner' 模型
                model_to_use = available_models["utils_small"]

                # 在执行时动态获取当前时间
                current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prompt = (
                    f"请将以下自然语言时间短语转换为一个未来的、标准的 'YYYY-MM-DD HH:MM:SS' 格式。"
                    f"请只输出转换后的时间字符串，不要包含任何其他说明或文字。\n"
                    f"作为参考，当前时间是: {current_time_str}\n"
                    f"需要转换的时间短语是: '{remind_time_str}'\n"
                    f"规则:\n"
                    f"- 如果用户没有明确指出是上午还是下午，请根据当前时间判断。例如，如果当前是上午，用户说'8点'，则应理解为今天的8点；如果当前是下午，用户说'8点'，则应理解为今天的20点。\n"
                    f"- 如果转换后的时间早于当前时间，则应理解为第二天的时间。\n"
                    f"示例:\n"
                    f"- 当前时间: 2025-09-16 10:00:00, 用户说: '8点' -> '2025-09-17 08:00:00'\n"
                    f"- 当前时间: 2025-09-16 14:00:00, 用户说: '8点' -> '2025-09-16 20:00:00'\n"
                    f"- 当前时间: 2025-09-16 23:00:00, 用户说: '晚上10点' -> '2025-09-17 22:00:00'"
                )

                success, response, _, _ = await llm_api.generate_with_model(
                    prompt, model_config=model_to_use, request_type="plugin.reminder.time_parser"
                )

                if not success or not response:
                    raise ValueError(f"LLM未能返回有效的时间字符串: {response}") from e

                converted_time_str = response.strip()
                logger.info(f"[ReminderPlugin] LLM 转换结果: '{converted_time_str}'")
                target_time = parse_datetime(converted_time_str, fuzzy=False)

        except Exception as e:
            logger.error(f"[ReminderPlugin] 无法解析或转换时间字符串 '{remind_time_str}': {e}", exc_info=True)
            await self.send_text(f"不是哥们，到底几点啊，没听清 '{remind_time_str}'，提醒设置失败。")
            return False, f"无法解析时间 '{remind_time_str}'"

        now = datetime.datetime.now()
        if target_time <= now:
            await self.send_text("提醒时间必须是一个未来的时间点哦，提醒设置失败。")
            return False, "提醒时间必须在未来"

        # 2. 存储提醒事项到数据库
        try:
            from src.common.database.database_model import ActionRecords

            # 生成唯一ID
            action_id = str(int(time.time() * 1000000))
            # [新增] 提取群组信息
            group_id = None
            group_platform = None
            if self.chat_stream.group_info:
                group_id = self.chat_stream.group_info.group_id
                group_platform = self.chat_stream.group_info.platform

            # [修改] 将群组信息写入 action_data 字典
            data_dict = {
                "user_id": str(self.user_id),
                "user_name": str(self.user_nickname),
                "event_details": str(event_details),
                "remind_time": target_time.timestamp(),
            }
            if group_id:
                data_dict["group_id"] = group_id
                data_dict["group_platform"] = group_platform

            record_data = {
                "action_id": action_id,
                "time": time.time(),
                "action_name": "reminder_item",
                "action_data": json.dumps(data_dict),
                "action_done": False,  # 初始状态为未完成
                "action_build_into_prompt": False,
                "action_prompt_display": f"提醒事项: {event_details} (提醒时间: {target_time.strftime('%Y-%m-%d %H:%M:%S')})",
                "chat_id": self.chat_stream.stream_id,
                "chat_info_stream_id": self.chat_stream.stream_id,
                "chat_info_platform": self.chat_stream.platform,
            }

            saved_record = await database_api.db_save(
                model_class=ActionRecords,
                data=record_data,
                key_field="action_id",
                key_value=action_id
            )

            if not saved_record:
                logger.error("[ReminderPlugin] 存储提醒事项失败")
                await self.send_text("抱歉，设置提醒时发生了一点内部错误（存储失败）。")
                return False, "存储提醒事项失败"

        except Exception as e:
            logger.error(f"[ReminderPlugin] 存储提醒事项时出错: {e}", exc_info=True)
            await self.send_text("抱歉，设置提醒时发生了一点内部错误。")
            return False, "存储提醒事项时发生内部错误"
        """
        try:
            success, llm_response = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                request_type="replyer",
                from_plugin=True,
            )
            if not success or not llm_response or not llm_response.reply_set:
                logger.info("回复生成失败")
                fallback_message = f"好的，我记下了。\n将在 {target_time.strftime('%Y-%m-%d %H:%M:%S')} 提醒你：\n{event_details}"
                await self.send_text(fallback_message)
            else:
                
            await database_api.store_action_info(
                chat_stream=self.chat_stream,
                action_build_into_prompt=True,
                action_done=True,
                thinking_id=thinking_id,
                action_data={"reply_text": reply_text},
                action_name="reply",
            )
            for reply_content in llm_response.reply_set.reply_data:
                if reply_content.content_type != ReplyContentType.TEXT:
                    continue
                data: str = reply_content.content  # type: ignore
                if not first_replied:
                    await send_api.text_to_stream(
                        text=data,
                        stream_id=self.chat_stream.stream_id,
                        reply_message=message_data,
                        set_reply=need_reply,
                        typing=False,
                        selected_expressions=selected_expressions,
                    )
                    first_replied = True
                else:
                    await send_api.text_to_stream(
                        text=data,
                        stream_id=self.chat_stream.stream_id,
                        reply_message=message_data,
                        set_reply=False,
                        typing=True,
                        selected_expressions=selected_expressions,
                    )
        except Exception as e:
            logger.error(f"{self.log_prefix} 回复生成失败：{e}")
            fallback_message = f"好的，我记下了。\n将在 {target_time.strftime('%Y-%m-%d %H:%M:%S')} 提醒你：\n{event_details}"
            await self.send_text(fallback_message)        
        else:
        """
        time_diff = target_time.timestamp() - time.time()
        if time_diff <= 24 * 3600:
            logger.info(f"[ReminderPlugin] 任务在24小时内，立即加入内存队列: {action_id}")
            reminder_task = DatabaseReminderTask(
                delay=time_diff,
                action_id=action_id,
                chat_stream=self.chat_stream,
                user_id=str(self.user_id),
                user_name=str(self.user_nickname),
                event_details=str(event_details),
                creator_name=str(self.user_nickname),
                group_id=group_id
            )
            await async_task_manager.add_task(reminder_task)
        # Fallback message
        need_confirm = True
        if _plugin_instance:
            need_confirm = _plugin_instance.get_config("reminder_settings.send_confirmation", True)

        if need_confirm:
            fallback_message = f"好的，我记下了。将在 {target_time.strftime('%Y-%m-%d %H:%M:%S')} 提醒你：{event_details}"
            success, reply_set = await generator_api.rewrite_reply(
                chat_stream=self.chat_stream,
                raw_reply=fallback_message,
                reason="原始消息太死板了，你不喜欢，用符合你性格的方式自然地说出来",
                enable_splitter=True,
            )
            await self.send_text(reply_set.content)
        else:
            logger.info(f"[ReminderPlugin] 提醒设置成功({action_id})，根据配置跳过发送确认消息。")
        return True, "提醒设置成功"


# ===== 插件注册 =====
@register_plugin
class ReminderPlugin(BasePlugin):
    """一个专门用于设置提醒事项的插件（仅限提醒自己）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 启动后台调度器
        # 使用 create_task 避免阻塞插件加载
        global _plugin_instance
        _plugin_instance = self
        asyncio.create_task(self._start_scheduler())

    async def _start_scheduler(self):
        await asyncio.sleep(5)  # 等待数据库就绪

        # [修改点] 从配置中读取参数
        scan_interval = self.get_config("reminder_settings.scheduler_scan_interval", 3600)
        window_seconds = self.get_config("reminder_settings.scheduler_lookahead_window", 86400)

        # 将配置传给调度器
        scheduler = ReminderScheduler(scan_interval=scan_interval, window_seconds=window_seconds)
        await async_task_manager.add_task(scheduler)

    # 插件基本信息
    plugin_name: str = "reminder_plugin"  # 内部标识符
    enable_plugin: bool = True
    dependencies: list[str] = []  # 插件依赖列表
    python_dependencies: list[str] = []  # Python包依赖列表，现在使用内置API
    config_file_name: str = "config.toml"  # 配置文件名

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本信息",
        "components": "功能开关",
        "reminder_settings": "调度器高级设置"  # 新增描述
    }

    # 配置Schema定义
    config_schema: ClassVar[dict] = {
        "plugin": {
            "name": ConfigField(type=str, default="reminder_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.1.0", description="插件版本"),  # 版本号升级
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.1", description="配置版本"),
        },
        "napcat": {
            "url": ConfigField(type=str, default="http://127.0.0.1:3000",
                               description="NapCat HTTP API地址 (不带尾部斜杠)"),
            "token": ConfigField(type=str, default="123456789",
                               description="NapCat HTTP API TOKEN"),
        },
        "components": {
            "action_set_reminder_enable": ConfigField(type=bool, default=True, description="是否启用定时提醒功能"),
        },
        "reminder_settings": {
            "scheduler_scan_interval": ConfigField(
                type=int,
                default=3600,
                description="后台扫描数据库的时间间隔(秒)，默认12小时"
            ),
            "scheduler_lookahead_window": ConfigField(
                type=int,
                default=86400,
                description="预加载内存的任务时间窗口(秒)，默认24小时。过大会占用更多内存，过小可能导致频繁数据库读取。"
            ),
            "send_confirmation": ConfigField(
                type=bool,
                default=True,
                description="设置提醒成功后，是否发送确认消息(如:好的，已设置...)"
            ),
        }
    }

    def get_plugin_components(self) -> list[tuple[ComponentInfo, type]]:
        enable_components = []
        if self.get_config("components.action_set_reminder_enable"):
            enable_components.append((RemindAction.get_action_info(), RemindAction))
        return enable_components
