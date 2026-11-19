#!/usr/bin/env python3
"""Telegram bridge listener: long-poll Telegram and queue messages.

Design:
- Long-poll Telegram getUpdates with timeout 50s. Run exactly ONE
  instance of this listener per bot token. A second poller calling
  getUpdates for the same bot produces 409 conflicts.
- Whitelist (watch-only group mode): private chat: the sender's
  user_id and the chat_id must both equal the configured owner
  (TELEGRAM_OWNER_USER_ID / TELEGRAM_OWNER_CHAT_ID). Group/supergroup
  chat: EVERY message is queued for context EXCEPT the bot's own
  messages (from TELEGRAM_BOT_ID), regardless of speaker or whether
  the bot is named, so a later worker can follow who said what.
  But a group record is REPLYABLE only when the speaker is the owner
  AND the message names the bot (text/caption contains the bot's
  @username, one of TELEGRAM_MENTION_TOKENS, or a broadcast mention
  such as "全部成员" / "全体成员" / "所有人" / "@大家" / "@all" /
  "@everyone", or it replies to a message from the bot). English
  broadcast tokens match case-insensitively and only with the @, so
  ordinary words do not trigger; bare "大家" without @ does NOT
  trigger. Non-replyable group records carry "log_only": true and
  the worker must ONLY append them to history (with speaker name),
  never send a group reply for them. Replyable group records carry
  "log_only": false. The group chat_id is recorded in the queue
  record (field "group": true); the bot only receives messages from
  groups it was added to.
- Accepted text/caption plus media info are appended, one JSON per
  line, to queue.jsonl. Media (photo, document, video, audio, voice)
  are downloaded first to media/ as <update_id>_<original filename>,
  with download_ok true/false recorded, never silently dropped.
- listener_state.json next_offset is updated atomically (temp file
  + rename) to update_id + 1 after each processed update.
- Configuration comes from environment variables (see README.md).
  The bot token is used as-is in the bot URL path segment; do NOT
  URL-encode it. Never hard-code, log, or print the token.
- Proxy: this script sets no proxy itself. If your environment needs
  one, configure it via the standard HTTP(S)_PROXY environment
  variables; urllib honours them.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))

API = "https://api.telegram.org"


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"[listener] missing required environment variable {name}")
    return value


def _require_env_int(name):
    value = _require_env(name)
    try:
        return int(value)
    except ValueError:
        raise SystemExit(
            f"[listener] environment variable {name} must be an integer, got {value!r}")


BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN").strip()
WHITELIST_USER = _require_env_int("TELEGRAM_OWNER_USER_ID")
WHITELIST_CHAT = _require_env_int("TELEGRAM_OWNER_CHAT_ID")
BOT_ID = _require_env_int("TELEGRAM_BOT_ID")

BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()
EXTRA_MENTIONS = [t.strip() for t in
                  os.environ.get("TELEGRAM_MENTION_TOKENS", "").split(",")
                  if t.strip()]
BROADCAST_TOKENS = ("全部成员", "全体成员", "所有人", "@大家",
                    "@all", "@everyone")
MENTION_TOKENS = tuple(
    ([f"@{BOT_USERNAME}"] if BOT_USERNAME else [])
    + EXTRA_MENTIONS
    + list(BROADCAST_TOKENS))


def _is_named(text, msg):
    t = text or ""
    if any(tok in t or tok.lower() in t.lower() for tok in MENTION_TOKENS):
        return True
    reply = msg.get("reply_to_message") or {}
    return (reply.get("from") or {}).get("id") == BOT_ID


QUEUE = os.path.join(BASE, "queue.jsonl")
STATE = os.path.join(BASE, "listener_state.json")
MEDIA_DIR = os.path.join(BASE, "media")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _bot_url(method=""):
    return f"{API}/bot{BOT_TOKEN}/{method}" if method else f"{API}/bot{BOT_TOKEN}"


def _post(method, params=None):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(_bot_url(method), data=data, method="POST")
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=70) as resp:
        return json.loads(resp.read().decode())


def _file_url(file_path):
    """Telegram file download URL is /file/bot<token>/<path>, NOT
    /bot<token>/file/<path>. The latter 404s for every media download."""
    return f"{API}/file/bot{BOT_TOKEN}/{file_path}"


def _download_file(file_id, update_id, orig_name):
    """Return (local_path, ok). Never raises for a download failure."""
    try:
        info = _post("getFile", {"file_id": file_id})
        if not info.get("ok"):
            return None, False
        file_path = info["result"]["file_path"]
        safe_name = os.path.basename(file_path).replace("/", "_")
        local_name = f"{update_id}_{orig_name or safe_name}"
        local_path = os.path.join(MEDIA_DIR, local_name)
        file_url = _file_url(file_path)
        req = urllib.request.Request(file_url, method="GET")
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=120) as resp:
            body = resp.read()
        with open(local_path, "wb") as f:
            f.write(body)
        return local_path, True
    except Exception:
        return None, False


def load_offset():
    try:
        with open(STATE) as f:
            return int(json.load(f).get("next_offset", 0))
    except Exception:
        return 0


def save_offset(offset):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"next_offset": offset}, f)
    os.replace(tmp, STATE)


def append_queue(record):
    with open(QUEUE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def backoff_for(exc, body):
    text = str(body) + str(exc)
    if "401" in text or "403" in text or "404" in text:
        return 60
    if "409" in text:
        return 15
    if "429" in text:
        return 30
    return 10


def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    offset = load_offset()
    delay = 5
    while True:
        try:
            data = _post("getUpdates", {
                "offset": str(offset) if offset else "",
                "timeout": "50",
                "limit": "20",
            })
            delay = 5
        except Exception as e:
            print(f"[listener] getUpdates error: {e}, retry in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if not data.get("ok"):
            wait = backoff_for("", data)
            print(f"[listener] getUpdates not ok: {data}, retry in {wait}s", flush=True)
            time.sleep(wait)
            continue
        for update in data.get("result", []):
            update_id = update.get("update_id")
            msg = update.get("message") or update.get("edited_message") or {}
            if not msg:
                if update_id is not None:
                    offset = update_id + 1
                    save_offset(offset)
                continue
            frm = msg.get("from", {})
            chat = msg.get("chat", {})
            user_id = frm.get("id")
            chat_id = chat.get("id")
            chat_type = chat.get("type", "")
            # Whitelist first, offset always advances.
            text = msg.get("text") or msg.get("caption") or ""
            is_group = chat_type in ("group", "supergroup")
            if chat_id == WHITELIST_CHAT:
                is_group = False
                if user_id != WHITELIST_USER:
                    offset = update_id + 1
                    save_offset(offset)
                    continue
                log_only = False
            elif is_group:
                # Watch-only group mode: log every group message
                # except the bot's own, reply only when the owner
                # names the bot (incl. broadcast to all).
                if user_id == BOT_ID:
                    offset = update_id + 1
                    save_offset(offset)
                    continue
                log_only = not (user_id == WHITELIST_USER
                                and _is_named(text, msg))
            else:
                offset = update_id + 1
                save_offset(offset)
                continue
            media = None
            # Pick the largest photo size, else document/video/audio/voice.
            if msg.get("photo"):
                photo = msg["photo"][-1]
                local, ok = _download_file(photo["file_id"], update_id, "photo.jpg")
                media = {"type": "photo", "file_id": photo["file_id"],
                         "local_path": local, "download_ok": ok}
            elif msg.get("document"):
                doc = msg["document"]
                local, ok = _download_file(doc["file_id"], update_id,
                                           doc.get("file_name"))
                media = {"type": "document", "file_id": doc["file_id"],
                         "file_name": doc.get("file_name"),
                         "mime_type": doc.get("mime_type"),
                         "local_path": local, "download_ok": ok}
            elif msg.get("video"):
                vid = msg["video"]
                local, ok = _download_file(vid["file_id"], update_id, "video.mp4")
                media = {"type": "video", "file_id": vid["file_id"],
                         "local_path": local, "download_ok": ok}
            elif msg.get("audio"):
                aud = msg["audio"]
                local, ok = _download_file(aud["file_id"], update_id,
                                           aud.get("file_name"))
                media = {"type": "audio", "file_id": aud["file_id"],
                         "local_path": local, "download_ok": ok}
            elif msg.get("voice"):
                voice = msg["voice"]
                local, ok = _download_file(voice["file_id"], update_id, "voice.ogg")
                media = {"type": "voice", "file_id": voice["file_id"],
                         "local_path": local, "download_ok": ok}
            from_name = (frm.get("first_name") or "")
            if frm.get("username"):
                from_name = f"{from_name} (@{frm.get('username')})".strip()
            record = {
                "update_id": update_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "from_name": from_name,
                "text": text,
                "date": msg.get("date"),
                "type": media["type"] if media else "text",
                "media": media,
                "group": is_group,
                "log_only": log_only,
            }
            append_queue(record)
            offset = update_id + 1
            save_offset(offset)
            print(f"[listener] queued update {update_id} type={record['type']}", flush=True)


if __name__ == "__main__":
    main()
