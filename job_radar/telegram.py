"""Minimal Telegram Bot API client (long polling, no webhook or public server)."""
from __future__ import annotations

import html
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .http import USER_AGENT, post_json, urlopen
from .model import Posting

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096


class TelegramError(Exception):
    """A refused API call, with Telegram's reason (e.g. "query is too old"); never the token."""


def _refused(method: str, e: urllib.error.HTTPError) -> TelegramError:
    try:
        reason = json.loads(e.read().decode("utf-8", "replace")).get("description", "")
    except Exception:
        reason = ""
    return TelegramError(f"{method}: HTTP {e.code}" + (f": {reason}" if reason else f" {e.reason}"))


def _chunks(text: str, limit: int = LIMIT) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            parts.append(line[:limit]); line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current); current = ""
        current += line
    return parts + [current] if current else parts


class Bot:
    def __init__(self, token: str, chat_id: str, post=post_json):
        self.token, self.chat_id, self.post = token, str(chat_id), post
        self.offset = 0

    def call(self, method: str, payload: dict, timeout: int = 60):
        try:
            return self.post(API.format(token=self.token, method=method), payload, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise _refused(method, e) from None

    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        parts = _chunks(text)
        for i, part in enumerate(parts):
            payload = {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row] for row in buttons]}
            self.call("sendMessage", payload)

    def note(self, text: str) -> int:
        """Send a short plain message and return its id, to delete it later."""
        resp = self.call("sendMessage", {"chat_id": self.chat_id, "text": text})
        return resp["result"]["message_id"]

    def delete(self, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id})

    def typing(self) -> None:
        """Show "typing..." for a few seconds while Claude thinks."""
        self.call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    def answer(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def updates(self, wait: int = 0) -> list[dict]:
        resp = self.call("getUpdates", {"offset": self.offset, "timeout": wait,
                                        "allowed_updates": ["callback_query", "message"]}, timeout=wait + 30)
        mine = []
        for upd in resp.get("result", []):
            self.offset = max(self.offset, upd["update_id"] + 1)
            msg = upd.get("message") or upd.get("callback_query", {}).get("message") or {}
            if not self.chat_id or str(msg.get("chat", {}).get("id")) == self.chat_id:
                mine.append(upd)
        return mine

    def send_document(self, path: Path, caption: str = "") -> None:
        boundary = uuid.uuid4().hex
        path = Path(path)
        body = b""
        for name, value in (("chat_id", self.chat_id), ("caption", caption[:1024])):
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
                 f"Content-Type: text/markdown\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(API.format(token=self.token, method="sendDocument"), data=body, method="POST",
                                     headers={"User-Agent": USER_AGENT,
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(req, timeout=60) as resp:
                json.load(resp)
        except urllib.error.HTTPError as e:
            raise _refused("sendDocument", e) from None


def posting_card(p: Posting) -> tuple[str, list[list[tuple[str, str]]]]:
    e = html.escape
    where = e(p.location or "?") + (" · remote" if p.remote else "")
    lines = [f'<b><a href="{e(p.url, quote=True)}">{e(p.title)}</a></b>',
             f"{e(p.company or '?')} · {where}" + (f" · {e(p.contract_type)}" if p.contract_type else "")]
    if p.employer:
        lines.append(f"🏢 {e(p.employer)}")
    if p.salary:
        lines.append(f"💶 {e(p.salary)}")
    if p.score is not None:
        lines.append(f"⭐ {p.score}/10 · {e(p.why)}")
    lines.append(f"<i>{e(p.source)}</i>")
    buttons = [[("🔎 Recherche", f"r:{p.id}"), ("👎 Überspringen", f"s:{p.id}"), ("✅ Beworben", f"a:{p.id}")]]
    return "\n".join(lines), buttons
