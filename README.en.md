# Muse Telegram Bridge — Multimodal Messaging & Multi-Agent Group Chat

Connect a personal AI assistant to Telegram: long-poll the Bot API for incoming messages, append them to an on-disk queue, and let a worker (your own agent or service) process them and reply. The core is three stages — **long-poll → queue → send** — and the worker is intentionally left to the deployer, because which runtime and agent framework it runs on differs for everyone. This project does not decide that part for you.

中文版：[README.md](README.md)

## Highlights

**1. Multi-Agent Group Chat**

Multiple AI agents share context in the same Telegram group, with replies controlled by @mentions, broadcast words, and a read-only message mechanism, so they don't interfere with each other.

**2. Multimodal Messaging**

Send and receive text, images, video, voice, audio, and files. Whether Muse understands a given piece of content depends on the Worker and the model's capabilities.

> Note: what is published here is the communication bridge skeleton, not a ready-to-use complete multi-agent system. What is already proven in practice is that this collaboration pattern is running in our own Telegram group.

## What this is

- A send/receive skeleton for a Telegram bot, not a ready-made bot.
- `listener.py` does exactly one thing: long-poll `getUpdates`, download media, and append to `queue.jsonl`. Nothing else.
- `send_message.py` does exactly one thing: send text, photos, files, audio, video, and voice notes.
- `append_history.py` does exactly one thing: append a conversation line to `history.jsonl`.
- Who reads the queue, generates replies, and decides what to say? That is the worker's job. This project only defines the queue format (below); you connect the worker yourself.

## Using it with Muse

This bridge was written for Muse (Meta's personal AI assistant): Muse is the worker, and the bridge only turns Telegram into one more chat surface for it.

- **Division of labor**: Muse thinks, looks things up, writes, and decides; the bridge receives and sends messages. Muse's own tools, memory, and files are not limited by the bridge — the bridge only adds a chat entry point.
- **How to connect it**: the deployer needs a trigger that wakes a Muse session when new messages appear in `queue.jsonl` (for example a file watcher, a periodic check, or your own hook). The trigger mechanism is the deployer's choice; this project does not prescribe it.
- **What Muse does**: Muse claims the queue (rename it with `mv` to a processing file), reads the records in `update_id` order, and processes them following the rules in "How a worker processes queue.jsonl": merge consecutive messages into one round, show typing while generating, stop it automatically before sending, and append to history after sending.
- **Proactive push**: any program (including Muse itself) can call `send_message.py` directly to send a message, without waiting for the user to message first.

## Architecture

```
Telegram ──getUpdates long-poll──> listener.py
                                      │ new messages to disk
                                      ▼
                              queue.jsonl ──> worker (you implement it)
                                      │ read queue, generate replies
                                      ▼
                              send_message.py ──> Telegram

Proactive push: any program ──> send_message.py ──> Telegram
```

Each queue record (JSON, one per line) looks like this:

```json
{
  "update_id": 123456789,
  "chat_id": 123456789,
  "user_id": 123456789,
  "from_name": "Someone (@someone)",
  "text": "hello",
  "date": 1795000000,
  "type": "text",
  "media": null,
  "group": false,
  "log_only": false
}
```

- When `group` is `true`, it is a group message and `chat_id` is the group id.
- When `log_only` is `true`, it is a log-only group message (see "Group mode" below): the worker may only append it to history and must never reply in the group.
- When `media` is not `null`, it is a media message; `local_path` is the downloaded local file and `download_ok` says whether the download succeeded. Never pretend you saw a failed download.

## Group mode (watch-only)

Private chat: only the owner's messages (`TELEGRAM_OWNER_USER_ID` / `TELEGRAM_OWNER_CHAT_ID`) enter the queue.

Group chat: every member's message enters the queue (except the bot's own), so the worker sees the full context. But a group message is **replyable** (`log_only: false`) only when both of these hold:

1. The sender is the owner;
2. The message addresses the bot: the text contains the bot's @username, any token from `TELEGRAM_MENTION_TOKENS`, or a broadcast mention (`全部成员` / `全体成员` / `所有人` / `@大家` / `@all` / `@everyone`), or it replies to a message sent by the bot.

Non-replyable group messages (`log_only: true`) are history-only, never replied to. English broadcast tokens only match with the `@` (`@all`, `@everyone`), so ordinary English words do not trigger by accident; a bare "大家" without `@` does not trigger either.

## Three borrowed improvements

These three ideas are borrowed from [wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge) (a project that connects Muse to WeChat). That project worked through, in detail, the pitfalls of running a real assistant inside a chat app; three of them apply to Telegram just as well. Credit goes there.

### 1. Consecutive-message merge

When the user sends several messages in a row, the worker reads all of them first and replies only once (the latest message is the main question, the earlier ones are context). Before sending, it checks once more for new arrivals and merges those into the same round instead of sending a separate reply.

Why: replying per message feels mechanical, and it easily answers a step behind, out of context.

Where it lives: a worker-side rule, not a change to the listener or the send script. When the worker processes its own processing file, it groups consecutive replyable records from the same chat into one round, and re-checks the new queue before sending.

### 2. Typing keeper

When the worker starts on a replyable message, it immediately shows "typing", refreshes it every 4 seconds while generating, and stops it before sending.

Why: Telegram typing lasts only about 5 seconds; without refreshing it goes dark. Flashing it only right before the send leaves the user staring at silence during a long reply, wondering whether the bridge died. Keeping it on while generating shows the bot is alive.

How it works: `send_message.py` provides `--typing` (show once) and `--typing-keep <seconds>` (background keeper, self-limiting). **The key part is the automatic stop on send**: when the keeper starts, it writes its PID to `typing-<chat_id>.pid`; before sending a real message, `send_message.py` reads that file, verifies the process really is a keeper (by reading `/proc/<pid>/cmdline`, so a reused PID is never killed), kills it, removes the file, and then sends. Typing therefore always stops the moment the message goes out, no matter whether the worker remembered to kill the keeper. Relying on the worker to kill the PID is unreliable — in practice typing kept flashing for two minutes after a reply had already been sent.

### 3. Send validation

Before sending, the body is validated: an empty body, or a body starting with `--`, is refused — logged only, never sent.

Why: the WeChat bridge once had an incident where a worker building the send command dropped the body placeholder, so the dedup flag `--key` slid into the body position, and the bridge sent it verbatim because the send path never validated the body. The send-time guard is the fix that came out of that incident.

How it works: `validate_body()` in `send_message.py`, applied to both `--text` and `--caption`; a refusal exits with code 2, logs to stderr, sends nothing, and does not crash.

## Configuration

Everything is configured through environment variables. Never put a token in any file.

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather. |
| `TELEGRAM_OWNER_USER_ID` | Yes | Your Telegram user ID (the owner). Only this user can chat with the bot privately. |
| `TELEGRAM_OWNER_CHAT_ID` | Yes | Your private chat ID with the bot (usually the same as the user ID). |
| `TELEGRAM_BOT_ID` | Yes | The bot's numeric Telegram ID (from `getMe`). Used to skip the bot's own group messages. |
| `TELEGRAM_BOT_USERNAME` | No | The bot's @username (without the @). Used as a group mention token. |
| `TELEGRAM_MENTION_TOKENS` | No | Comma-separated extra mention tokens (e.g. the bot's name or nickname). Broadcast tokens are built in and do not need to be listed. |

See `config.example.json` as a reference: copy it to `config.json`, fill in your own values, and export them as environment variables. **Never commit a filled-in `config.json`** (it is already in `.gitignore`). Never put a token in any file that will be committed.

## Running the listener

```bash
export TELEGRAM_BOT_TOKEN="<your token>"
export TELEGRAM_OWNER_USER_ID="<your user id>"
export TELEGRAM_OWNER_CHAT_ID="<your chat id>"
export TELEGRAM_BOT_ID="<your bot id>"
export TELEGRAM_BOT_USERNAME="<your bot username>"
# Optional: export TELEGRAM_MENTION_TOKENS="Name,Nickname"

python3 listener.py
```

Notes:

- Run exactly **one** listener per bot token. Two processes calling `getUpdates` for the same bot produce 409 conflicts.
- The listener is a long-running process; run it under systemd, supervisor, or your own process manager so it restarts automatically if it dies.
- `listener_state.json` holds `next_offset`, the resume point: after a restart the listener continues from there, so no message is lost or pulled twice. Do not edit or delete it by hand.
- The queue and history live next to the scripts as `queue.jsonl` and `history.jsonl`; media goes to `media/`.

## How a worker processes queue.jsonl

The worker is the deployer's own part; this project does not ship one. The recommended flow is:

1. **Claim**: atomically rename `queue.jsonl` (`mv`) to `processing-<last update_id>.jsonl`. If the rename fails (another worker already claimed it), exit immediately, do nothing, delete nothing.
2. **Process line by line**: read your own processing file in `update_id` order. Re-check the whitelist; for `log_only: true` records, append to history only and never reply; for media with `download_ok` true, open the local file first and reply based on both the text and the file; if the download failed, say so and ask the user to resend — do not pretend you saw it.
3. **Merge consecutive messages**: group consecutive replyable records from the same chat into one round and reply once; before sending, re-check the new queue and merge same-chat arrivals into this round.
4. **Typing**: when starting on a replyable message, start `send_message.py --chat-id <id> --typing-keep 300` in the background; the send stops it automatically.
5. **Send**: reply with `send_message.py --chat-id <that record's chat_id>`, using `--text` for text, `--photo` for images, `--document` for files, `--audio` for audio, `--video` for video, and `--voice` for voice notes (OGG Opus format). After a successful send, append the user and assistant lines with `append_history.py`.
6. **Finish**: once everything in your claimed file is processed and sent, check whether a new `queue.jsonl` appeared and repeat (up to 5 rounds is a good limit); then delete **your own** processing files. Never delete another worker's file, and never use a wildcard delete.
7. **On failure**: if a send fails, leave the processing file unchanged, report the failure and the unsent content, and do not claim success.

## Sending messages

```bash
# Text
python3 send_message.py --chat-id <CHAT_ID> --text "hello"

# Photo / file / audio / video / voice (voice must be OGG Opus)
python3 send_message.py --chat-id <CHAT_ID> --photo /path/to/img.jpg --caption "look at this"
python3 send_message.py --chat-id <CHAT_ID> --document /path/to/file.pdf
python3 send_message.py --chat-id <CHAT_ID> --voice /path/to/note.ogg

# Show typing once, send nothing
python3 send_message.py --chat-id <CHAT_ID> --typing

# Keep typing for 300 seconds (run in the background while generating a reply)
python3 send_message.py --chat-id <CHAT_ID> --typing-keep 300
```

## Files

| File | Purpose |
| --- | --- |
| `listener.py` | Long-poll for messages, download media, write the queue. |
| `send_message.py` | Send messages / media / typing. Includes the send guard and the typing keeper. |
| `append_history.py` | Append one conversation line to `history.jsonl`. |
| `config.example.json` | Configuration example; fill in your own values. |
| `.gitignore` | Ignores runtime state and secrets. |
| `LICENSE` | MIT License. |

Runtime files generated next to the scripts (all in `.gitignore`, never committed): `queue.jsonl`, `history.jsonl`, `listener_state.json`, `processing-*.jsonl`, `typing-*.pid`, `media/`.

## Acknowledgements

- [wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge): the consecutive-message merge, the typing keeper, and the send validation are borrowed from here, including the lesson of the `--key` incident.
- Telegram Bot API.

## License

MIT License, see [LICENSE](LICENSE).
