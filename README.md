# Muse Telegram Bridge — Multimodal Messaging & Multi-Agent Group Chat

把一个个人 AI 助理接进 Telegram：基于 Bot API 长轮询收消息，落盘队列，再由 worker（你自己的 agent 或服务）处理并回消息。核心是三段式：**长轮询 → 队列 → 发送**，中间的 worker 由部署者自己实现——因为 worker 跑在什么运行时上、用什么 agent 框架，每个人都不一样，这部分本项目故意不替你定。

English version: [README.en.md](README.en.md)

## 核心卖点

**1. Multi-Agent Group Chat（多 Agent 群聊）**

多个 AI Agent 在同一个 Telegram 群里获取上下文，通过 @提及、广播词、只读消息机制控制回复，减少互相干扰。

**2. Multimodal Messaging（多模态消息）**

支持文本、图片、视频、语音、音频和文件的接收与发送。至于 Muse 能否理解相应内容，则取决于 Worker 和模型能力。

> 准确说明：目前公开的是通信桥接骨架，并非开箱即用的完整多 Agent 系统。真正已经跑通的，是我们在自己的 Telegram 群里把这套协作方式实际用起来了。

## 这是什么

- 一个 Telegram Bot 的收发骨架，不是一个开箱即用的机器人。
- `listener.py` 只做一件事：用 `getUpdates` 长轮询收消息，下载媒体，写进 `queue.jsonl`，别的什么都不做。
- `send_message.py` 只做一件事：把文本、图片、文件、音频、视频、语音条发出去。
- `append_history.py` 只做一件事：把对话记进 `history.jsonl`。
- 谁来读队列、生成回复、决定回什么？那是 worker 的事。本项目只规定队列的格式（见下文），worker 由你自己接。

## 与 Muse 集成

这个桥是为 Muse（Meta 的个人 AI 助理）写的：Muse 当 worker，桥只负责把 Telegram 变成 Muse 的一个聊天界面。

- **分工**：Muse 负责想、查、写、做决定；桥负责收消息、发消息。Muse 自己的工具、记忆和文件都不受桥的限制——桥只是多了一个聊天入口。
- **怎么接上**：部署者需要一个触发器，在 `queue.jsonl` 出现新消息时唤醒一个 Muse 会话（比如文件监听、定时检查、或者你自己的 hook）。触发方式由部署者自己选，本项目不规定。
- **Muse 怎么做**：Muse 认领队列（`mv` 成 processing 文件）、按 `update_id` 顺序读记录、按“Worker 如何处理 queue.jsonl”那一节的规矩处理：连发合并成一轮、生成时亮 typing、发之前自动停、发完记 history。
- **主动推送**：任何程序（包括 Muse 自己）都可以直接调 `send_message.py` 发消息，不用等用户先发。

## 架构

```
Telegram ──getUpdates 长轮询──> listener.py
                                      │ 新消息落盘
                                      ▼
                              queue.jsonl ──> worker（你自己实现）
                                      │ 读队列、生成回复
                                      ▼
                              send_message.py ──> Telegram

主动推送：任意程序 ──> send_message.py ──> Telegram
```

队列里每条记录（JSON，每行一条）长这样：

```json
{
  "update_id": 123456789,
  "chat_id": 123456789,
  "user_id": 123456789,
  "from_name": "Someone (@someone)",
  "text": "你好",
  "date": 1795000000,
  "type": "text",
  "media": null,
  "group": false,
  "log_only": false
}
```

- `group` 为 `true` 时是群消息，`chat_id` 是群号。
- `log_only` 为 `true` 时是只记不回的群消息（见下文“群模式”），worker 只能记进 history，绝不能在群里回。
- `media` 不为 `null` 时是媒体消息，`local_path` 是下载到本地的文件，`download_ok` 表示下载是否成功；下载失败不要假装看到了。

## 群模式（watch-only）

私聊：只有 owner（`TELEGRAM_OWNER_USER_ID` / `TELEGRAM_OWNER_CHAT_ID`）的消息会进队列。

群聊：所有成员的消息都会进队列（除了机器人自己的消息），这样 worker 能看到完整的上下文。但只有同时满足下面两条的群消息才是**可回复**的（`log_only: false`）：

1. 发信人是 owner；
2. 消息点名了机器人：正文里有机器人的 @用户名、`TELEGRAM_MENTION_TOKENS` 里任何一个词、或者广播词（`全部成员` / `全体成员` / `所有人` / `@大家` / `@all` / `@everyone`），或者回复了机器人发过的消息。

不可回复的群消息（`log_only: true`）只记 history，不回。英文广播词只认带 `@` 的（`@all`、`@everyone`），不带 `@` 的普通英文词不会误触发；裸写的“大家”不触发。

## 三个借来的改进

这三个点子是从 [wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge)（把 Muse 接进微信的项目）抄来的。那个项目把“一个真实助理跑在聊天软件上会踩的坑”打磨得很细，其中三项对 Telegram 同样适用，在此致谢。

### 1. 连发合并

用户一连发几条消息时，worker 先全读完、只回一次（最新那条当主问题，前面的当上下文）。发之前再看一眼有没有新消息进来，有就并进这一轮，不单独再回一条。

为什么：逐条回显得机械，还容易慢一拍、答非所问。

实现位置：worker 层面的规矩，不改 listener 和发送脚本。worker 处理自己的 processing 文件时，把同一 chat 的连续可回复记录合成一轮；发之前复查新队列，同 chat 的新消息并进来。

### 2. 正在输入（typing 保活）

worker 开始处理一条可回复的消息时，立即亮“正在输入”，生成期间每 4 秒刷新一次，发之前停掉。

为什么：Telegram 的 typing 只能撑约 5 秒，不刷新就灭了。只在发之前闪一下，用户等长回复时看不到动静，会以为桥断了。生成期间一直亮着，用户就知道机器人还活着。

实现：`send_message.py` 提供 `--typing`（亮一次）和 `--typing-keep <秒数>`（后台保活，上限自限）。**关键是发前自动停**：保活启动时把 PID 写进 `typing-<chat_id>.pid`，发真实消息之前，`send_message.py` 先读这个文件、核对进程确实是保活（读 `/proc/<pid>/cmdline` 确认，防 PID 复用误杀），杀掉它、删掉文件，再发消息。这样不管 worker 忘没忘，typing 一定在消息发出的那一刻停。只靠 worker 记得杀 PID 是靠不住的——实测就出过消息发出两分钟后 typing 还在闪的情况。

### 3. 发送校验

发之前验正文：空的、或以 `--` 开头的，一律不发、只记日志。

为什么：微信桥出过一次事故：worker 拼发送命令漏了正文占位符，防重复的 `--key` 滑进正文位置，桥对正文没有任何校验，就原样发出去了。发前校验就是事后补的那道守卫。

实现：`send_message.py` 里的 `validate_body()`，对 `--text` 和 `--caption` 都验；拒绝时退出码 2，只记日志，不发、不崩。

## 配置

所有配置走环境变量，不要把 token 写进任何文件。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 是 | 从 @BotFather 拿到的 bot token。 |
| `TELEGRAM_OWNER_USER_ID` | 是 | 你的 Telegram user ID（owner）。只有这个人能私聊机器人。 |
| `TELEGRAM_OWNER_CHAT_ID` | 是 | 你和机器人的私聊 chat ID（通常和 user ID 相同）。 |
| `TELEGRAM_BOT_ID` | 是 | 机器人的数字 ID（`getMe` 可查）。用来跳过机器人自己的群消息。 |
| `TELEGRAM_BOT_USERNAME` | 否 | 机器人的 @用户名（不带 @）。会作为群里的点名 token。 |
| `TELEGRAM_MENTION_TOKENS` | 否 | 逗号分隔的额外点名 token（比如机器人的名字或昵称）。广播词是内置的，不用填。 |

也可以参考 `config.example.json`：把它复制成 `config.json` 填好自己的值，再从里面导出环境变量。**填好的 `config.json` 不要提交**（已在 `.gitignore` 里）。token 永远不要写进会被提交的文件。

## 运行 listener

```bash
export TELEGRAM_BOT_TOKEN="<你的 token>"
export TELEGRAM_OWNER_USER_ID="<你的 user id>"
export TELEGRAM_OWNER_CHAT_ID="<你的 chat id>"
export TELEGRAM_BOT_ID="<你的 bot id>"
export TELEGRAM_BOT_USERNAME="<你的 bot 用户名>"
# 可选：export TELEGRAM_MENTION_TOKENS="名字,昵称"

python3 listener.py
```

注意：

- 每个 bot token **只能跑一个** listener。两个进程同时调 `getUpdates` 会产生 409 冲突。
- listener 是常驻进程，建议用 systemd、supervisor 或你自己的进程管理跑起来，挂了自动拉起。
- `listener_state.json` 里的 `next_offset` 是断点续传用的：listener 重启后从这里继续拉，不会丢消息，也不会重复拉。不要手动改它、删它。
- 队列和历史都存在脚本旁边的 `queue.jsonl`、`history.jsonl`，媒体存在 `media/`。

## Worker 如何处理 queue.jsonl

Worker 是部署者自己的部分，这里只规定推荐的做法（本项目不提供 worker）：

1. **认领**：把 `queue.jsonl` 原子性地改名（`mv`）成 `processing-<最后一条 update_id>.jsonl`。如果改名失败（文件已经被别的 worker 认领），直接退出，什么都不做、不删。
2. **逐行处理**：按 `update_id` 顺序读自己认领的文件。先复核白名单；`log_only: true` 的只记 history，不回；有媒体且 `download_ok` 为真时，先打开本地文件再回；下载失败就直说下载失败、请用户重发，不要假装看到了。
3. **连发合并**：同一 chat 的连续可回复记录合成一轮，只回一次；发之前复查新队列，同 chat 的新消息并进来。
4. **正在输入**：开始处理可回复的消息时，先在后台起 `send_message.py --chat-id <id> --typing-keep 300`，发消息时它会被自动停掉。
5. **发送**：用 `send_message.py --chat-id <该记录的 chat_id>` 回消息，文本用 `--text`，图片用 `--photo`，文件用 `--document`，音频用 `--audio`，视频用 `--video`，语音条用 `--voice`（OGG Opus 格式）。发完再用 `append_history.py` 记 history。
6. **收尾**：自己认领的文件全部处理完、发送成功后，再检查有没有新的 `queue.jsonl` 出现，有就重复以上步骤（建议最多 5 轮）；全部完成后删掉**自己**的 processing 文件。永远不要删别的 worker 的文件，不要用通配符删。
7. **失败处理**：某条发送失败时，保持 processing 文件不变，上报失败和没发出去的内容，不要谎称成功。

## 发送消息

```bash
# 文本
python3 send_message.py --chat-id <CHAT_ID> --text "你好"

# 图片 / 文件 / 音频 / 视频 / 语音（语音必须是 OGG Opus）
python3 send_message.py --chat-id <CHAT_ID> --photo /path/to/img.jpg --caption "看这个"
python3 send_message.py --chat-id <CHAT_ID> --document /path/to/file.pdf
python3 send_message.py --chat-id <CHAT_ID> --voice /path/to/note.ogg

# 只亮一下 typing，不发消息
python3 send_message.py --chat-id <CHAT_ID> --typing

# typing 保活 300 秒（后台跑，生成回复时用）
python3 send_message.py --chat-id <CHAT_ID> --typing-keep 300
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `listener.py` | 长轮询收消息、下载媒体、写队列。 |
| `send_message.py` | 发消息 / 发媒体 / typing。含发送校验和 typing 保活。 |
| `append_history.py` | 往 `history.jsonl` 追加一行对话记录。 |
| `config.example.json` | 配置示例，照着填自己的值。 |
| `.gitignore` | 忽略运行时状态和机密文件。 |
| `LICENSE` | MIT License。 |

运行时会生成的文件（都在 `.gitignore` 里，不会提交）：`queue.jsonl`、`history.jsonl`、`listener_state.json`、`processing-*.jsonl`、`typing-*.pid`、`media/`。

## 致谢

- [wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge)：连发合并、typing 保活、发送校验这三个点子是从这里抄来的，包括 `--key` 事故的教训。
- Telegram Bot API。

## License

MIT License，见 [LICENSE](LICENSE)。
