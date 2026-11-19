#!/usr/bin/env python3
"""Send a Telegram message/photo/document/audio/video/voice as a bot.

Usage:
  send_message.py --chat-id <CHAT_ID> --text "hello"
  send_message.py --chat-id <CHAT_ID> --photo /path/to/img.jpg --caption "hi"
  send_message.py --chat-id <CHAT_ID> --document /path/to/file.pdf --caption "hi"
  send_message.py --chat-id <CHAT_ID> --audio /path/to/song.mp3 --caption "hi"
  send_message.py --chat-id <CHAT_ID> --video /path/to/clip.mp4 --caption "hi"
  send_message.py --chat-id <CHAT_ID> --voice /path/to/note.ogg --caption "hi"
  send_message.py --chat-id <CHAT_ID> --typing
  send_message.py --chat-id <CHAT_ID> --typing-keep 300

Voice notes must be OGG Opus, otherwise Telegram rejects them.
Audio/video/voice all use sendAudio / sendVideo / sendVoice with
the same multipart upload as photo/document.

Text uses urlencoded POST. Photo/document use multipart/form-data,
because Bot API file uploads need multipart, not urlencoded fields.
The bot token comes from the TELEGRAM_BOT_TOKEN environment
variable and is used as-is in the bot URL path segment; do NOT
URL-encode it. Never hard-code, log, or print the token.
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

API = "https://api.telegram.org"


def _bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "[send_message] missing required environment variable TELEGRAM_BOT_TOKEN")
    return token.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _bot_url(method):
    return f"{API}/bot{_bot_token()}/{method}"


def _post_form(method, fields, files=None):
    if not files:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(_bot_url(method), data=data, method="POST")
    else:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += f"{v}\r\n".encode()
        for field, path in files.items():
            filename = os.path.basename(path)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            body += f"--{boundary}\r\n".encode()
            body += (f'Content-Disposition: form-data; name="{field}"; '
                     f'filename="{filename}"\r\n').encode()
            body += f"Content-Type: {ctype}\r\n\r\n".encode()
            with open(path, "rb") as f:
                body += f.read()
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(_bot_url(method), data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def validate_body(text, what="text"):
    """Send-time guard, borrowed from the wechat-ilink-muse-bridge
    --key incident: a worker once sent a CLI flag as a message
    because the send path never validated the body. Reject
    empty/whitespace-only bodies and bodies starting with '--' (a
    flag that slipped into the text position). Return an error
    string, or None when the body is fine. Never raise."""
    if text is None:
        return None
    if not str(text).strip():
        return f"refused to send empty {what}: body is blank"
    if str(text).lstrip().startswith("--"):
        return f"refused to send {what} starting with '--': likely a flag, not a message"
    return None


def send_typing(chat_id):
    """Send one sendChatAction typing. Non-fatal, returns ok bool."""
    try:
        result = _post_form("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        return bool(result.get("ok"))
    except Exception:
        return False


BASE = os.path.dirname(os.path.abspath(__file__))


def _typing_pid_file(chat_id):
    return os.path.join(BASE, f"typing-{chat_id}.pid")


def stop_typing_keeper(chat_id):
    """Stop a running --typing-keep for this chat, if any. Called
    before a real send so the typing indicator stops the moment the
    message goes out, even if the worker forgot to kill the keeper.
    Verifies the PID is really a typing-keep process before killing,
    so a reused PID is never touched. The /proc check is
    Linux-specific; on other systems the PID file is still removed
    and the keeper self-exits when its time limit expires."""
    pid_file = _typing_pid_file(chat_id)
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception:
        return
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode(errors="ignore")
        if "send_message.py" not in cmdline or "typing-keep" not in cmdline:
            return
        os.kill(pid, 15)  # SIGTERM
    except Exception:
        pass
    try:
        os.remove(pid_file)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--text")
    ap.add_argument("--photo")
    ap.add_argument("--document")
    ap.add_argument("--audio")
    ap.add_argument("--video")
    ap.add_argument("--voice")
    ap.add_argument("--caption", default="")
    ap.add_argument("--typing", action="store_true",
                    help="Send only a typing indicator and exit (no message).")
    ap.add_argument("--typing-keep", type=int, default=0, metavar="SECONDS",
                    help="Keep sending a typing indicator every 4s for up to "
                    "SECONDS, then exit. Self-limiting so a forgotten keeper "
                    "cannot run forever.")
    args = ap.parse_args()
    # Typing-only modes: show the user the bot is working while the
    # worker generates a reply. Telegram typing lasts ~5s, so the
    # keeper re-sends every 4s. Both exit 0 even if Telegram is
    # briefly unreachable; the real send below still runs.
    if args.typing_keep > 0:
        pid_file = _typing_pid_file(args.chat_id)
        try:
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        try:
            deadline = time.time() + args.typing_keep
            while time.time() < deadline:
                send_typing(args.chat_id)
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(4, remaining))
        finally:
            try:
                os.remove(pid_file)
            except Exception:
                pass
        return 0
    if args.typing:
        send_typing(args.chat_id)
        return 0
    # A real send stops any typing keeper for this chat first, so the
    # indicator ends the moment the message goes out. This is the
    # backstop for a worker that forgot to kill the keeper.
    stop_typing_keeper(args.chat_id)
    # Send-time guard (wechat-ilink --key lesson): validate the body
    # before anything goes out, including the typing flash below.
    # Refuse, log to stderr, exit 2.
    err = validate_body(args.text, "text") if args.text is not None else None
    if err is None and args.caption:
        err = validate_body(args.caption, "caption")
    if err:
        print(f"[send_message] {err}", file=sys.stderr)
        return 2
    # Show "typing..." before the message, like other bridges do.
    # Non-fatal: if this call fails, the real send below still runs.
    try:
        _post_form("sendChatAction", {"chat_id": args.chat_id, "action": "typing"})
    except Exception:
        pass
    if args.photo:
        fields = {"chat_id": args.chat_id}
        if args.caption:
            fields["caption"] = args.caption
        result = _post_form("sendPhoto", fields, {"photo": args.photo})
    elif args.document:
        fields = {"chat_id": args.chat_id}
        if args.caption:
            fields["caption"] = args.caption
        result = _post_form("sendDocument", fields, {"document": args.document})
    elif args.audio:
        fields = {"chat_id": args.chat_id}
        if args.caption:
            fields["caption"] = args.caption
        result = _post_form("sendAudio", fields, {"audio": args.audio})
    elif args.video:
        fields = {"chat_id": args.chat_id}
        if args.caption:
            fields["caption"] = args.caption
        result = _post_form("sendVideo", fields, {"video": args.video})
    elif args.voice:
        fields = {"chat_id": args.chat_id}
        if args.caption:
            fields["caption"] = args.caption
        result = _post_form("sendVoice", fields, {"voice": args.voice})
    else:
        if not args.text:
            print("nothing to send", file=sys.stderr)
            return 2
        result = _post_form("sendMessage", {"chat_id": args.chat_id, "text": args.text})
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
