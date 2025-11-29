# ⏰ 智能提醒插件 (Smart Reminder Plugin)

> 为 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 设计的强大定时提醒系统，支持自然语言解析、数据库持久化存储与拟人化提醒推送。

![Version](https://img.shields.io/badge/version-1.1.0-blue)
![License](https://img.shields.io/badge/license-GPL--3.0-green)
![Author](https://img.shields.io/badge/author-SkillfulPainter-orange)

## 📖 简介

**Smart Reminder Plugin** 是一个运行在 MaiBot 框架下的实用工具插件。它允许用户通过自然的对话方式设置定时提醒。

不同于简单的倒计时工具，本插件深度集成了 LLM 的能力，能够智能解析模糊的时间表达（如"明天这个时候"），并在触发提醒时结合 Bot 的人设（Persona）生成符合性格的提醒话术，而不是死板的机器通知。

## ✨ 主要功能

* **🗣️ 自然语言交互**：无需死记硬背指令格式，支持 "十分钟后提醒我"、"明天早上九点叫我开会" 等自然表达。
* **🤖 LLM 智能时间解析**：内置时间解析器，针对复杂的时间描述（如 "下周三下午"）调用 LLM (`utils_small` 模型) 进行精准转换。
* **💾 数据持久化**：所有提醒事项写入数据库。即使 Bot 程序重启或崩溃，未过期的提醒任务在重启后依然会被调度执行，不会丢失。
* **🎭 拟人化提醒**：提醒触发时，会根据 Bot 当前的人设（Nickname / Personality）利用 LLM 实时重写提醒内容，让通知更具温度。
* **⚡ 高效后台调度**：采用基于时间窗口（Sliding Window）的后台调度器，定期扫描数据库，高效管理近期任务，降低内存占用。
* **👥 群聊@提醒**：在群聊环境中触发提醒时，自动@设置提醒的用户，确保提醒不会被忽略。

## 🚀 如何使用

确保插件已启用，在私聊或群聊中对 Bot 发送包含关键词的消息。

**触发关键词：** `提醒`、`叫我`、`记得`、`别忘了`

### 示例

* **相对时间：**
    > "10分钟后**提醒**我关火"
* **绝对时间：**
    > "明天早上8点**叫我**起床"
* **模糊时间（依赖当前语境）：**
    > "**别忘了**晚上10点提醒我抢票"

*注意：如果未指定具体时间，Bot 会提示你补充时间信息。*

### 群聊提醒示例

当提醒在群聊中触发时，Bot会自动@设置提醒的用户：

> "@张三 该起床啦！你昨天让我今天早上8点叫你的~"

## ⚙️ 配置参数

插件配置文件位于 `config.toml`，支持热重载（视主程序实现而定）。

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| **[components]** | | |
| `action_set_reminder_enable` | `true` | 是否开启提醒功能的主开关。 |
| **[reminder_settings]** | | |
| `scheduler_scan_interval` | `3600` | **数据库扫描间隔（秒）**。<br>后台调度器每隔多久扫描一次数据库，查找新任务。默认为1小时。 |
| `scheduler_lookahead_window` | `86400` | **预加载时间窗口（秒）**。<br>每次扫描时，将未来多久内的任务加载到内存队列中。默认为24小时。 |
| `send_confirmation` | `true` | **发送确认消息**。<br>设置提醒成功后，是否立即发送一条"好的，已设置..."的确认回复。 |
| `enable_group_mention` | `true` | **群聊@提醒功能**。<br>在群聊中触发提醒时是否自动@设置提醒的用户。 |
| **[napcat]** | | |
| `url` | `"http://127.0.0.1:3000"` | **NapCat HTTP API地址**。<br>不带尾部斜杠的完整URL。 |
| `token` | `""` | **NapCat HTTP API TOKEN**。<br>需要填入从NapCat获取的访问令牌。 |

## 🛠️ 实现原理

本插件基于 Python 异步编程 (`asyncio`) 实现，主要由以下模块构成：

1.  **RemindAction (动作触发)**：
    * 通过关键词捕获用户意图。
    * 使用 `dateutil` 进行基础时间解析，若失败则 Fallback 到 **LLM (Planner Model)** 进行自然语言时间理解。
    * 将解析后的标准时间戳与事件内容存入 `ActionRecords` 数据库表。
    * 记录提醒设置者的用户ID和消息来源（私聊/群聊）。

2.  **ReminderScheduler (全局调度器)**：
    * 这是一个后台驻留的 `AsyncTask`。
    * 采用 **"生产者-消费者"** 模式的变体：每隔 `scan_interval` 秒扫描数据库，查找 `[当前时间, 当前时间 + window_seconds]` 范围内的未完成任务。
    * 将符合条件的任务实例化为 `DatabaseReminderTask` 并注入内存任务队列。
    * 自动处理过期未执行的任务（标记为完成以防死循环）。

3.  **DatabaseReminderTask (任务执行单元)**：
    * 负责具体的倒计时等待 (`asyncio.sleep`)。
    * **Double Check 机制**：倒计时结束后，再次查询数据库确认任务未被取消。
    * **人设注入**：调用 `generator_api.rewrite_reply`，提示 LLM "现在是提醒时间，请以符合你人设的方式提醒用户"，生成最终的推送消息。
    * **群聊@处理**：如果消息来源是群聊且启用了@功能，则在消息前添加`@用户`标记，确保提醒设置者能够收到通知。

## 📦 安装说明

### 前置准备工作

在安装本插件之前，请确保已完成以下准备工作：

1. **NapCat HTTP服务端设置**：
   - 在NapCat中开启HTTP API服务
   - 设置一个访问令牌(Token)
   - 记录HTTP API的地址和端口（默认为`http://127.0.0.1:3000`）

2. **获取NapCat配置信息**：
   - 打开NapCat配置文件或管理界面
   - 找到HTTP API设置部分
   - 复制生成的访问令牌(Token)

### 安装步骤

1.  将本插件文件夹放入 MaiBot 的 `plugins` 目录下。
2.  确保 MaiBot 核心版本 >= 0.8.0。
3.  打开插件配置文件 `config.toml`，在 `[napcat]` 部分填入您的NapCat HTTP API信息：
    