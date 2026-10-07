"""Minimal Telegram Bot API client (long polling, no webhook or public server)."""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from pathlib import Path

from .http import USER_AGENT, post_json, urlopen
from .model import Posting
from .text import posted_ago

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096


class TelegramError(Exception):
    """A refused API call, with Telegram's reason (e.g. "query is too old"); never the token."""


def _error_body(e: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(e.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _refused(method: str, e: urllib.error.HTTPError, body: dict | None = None) -> TelegramError:
    reason = (body if body is not None else _error_body(e)).get("description", "")
    return TelegramError(f"{method}: HTTP {e.code}" + (f": {reason}" if reason else f" {e.reason}"))


def _cut_line(line: str, limit: int) -> list[str]:
    """A single line too long for one message: cut as plain text, so no tag or entity is split."""
    plain, pieces = html.unescape(re.sub(r"<[^>]+>", "", line)), []
    while plain:
        n = limit
        while len(html.escape(plain[:n], quote=False)) > limit:
            n -= 100
        pieces.append(html.escape(plain[:n], quote=False))
        plain = plain[n:]
    return pieces


def _blocks(text: str, limit: int) -> list[str]:
    """Paragraphs, or lines of a paragraph too long for one message; tags never span lines."""
    blocks = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            blocks.append(para)
            continue
        current = ""
        for line in para.split("\n"):
            for piece in ([line] if len(line) <= limit else _cut_line(line, limit)):
                if current and len(current) + 1 + len(piece) > limit:
                    blocks.append(current)
                    current = ""
                current = f"{current}\n{piece}" if current else piece
        blocks.append(current)
    return blocks


def _chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split a message under Telegram's limit at paragraph breaks, else at line ends."""
    parts, current = [], ""
    for block in _blocks(text, limit):
        if current and len(current) + 2 + len(block) > limit:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    return parts + [current] if current else parts


def _keyboard(buttons: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]}


MAX_RETRY_AFTER_S = 30  # a longer wait than that would hold up the chat's worker
MAX_ATTEMPTS = 4


class Bot:
    def __init__(self, token: str, chat_id: str, post=post_json, sleep=time.sleep):
        self.token, self.chat_id, self.post, self.sleep = token, str(chat_id), post, sleep
        self.offset = 0

    def call(self, method: str, payload: dict, timeout: int = 60):
        url = API.format(token=self.token, method=method)
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self.post(url, payload, timeout=timeout)
            except urllib.error.HTTPError as e:
                body = _error_body(e)
                params = body.get("parameters")
                wait = params.get("retry_after") if e.code == 429 and isinstance(params, dict) else None
                # e.g. many /merkliste entries at once
                if not isinstance(wait, int) or wait > MAX_RETRY_AFTER_S or attempt == MAX_ATTEMPTS - 1:
                    raise _refused(method, e, body) from None
            self.sleep(wait)

    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        parts = _chunks(text)
        for i, part in enumerate(parts):
            payload = {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = _keyboard(buttons)
            self.call("sendMessage", payload)

    def edit(self, message_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        """Replace the text of a sent message; without buttons its buttons go away."""
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = _keyboard(buttons)
        self.call("editMessageText", payload)

    def note(self, text: str) -> int:
        """Send a short plain message and return its id, to delete it later."""
        resp = self.call("sendMessage", {"chat_id": self.chat_id, "text": text})
        return resp["result"]["message_id"]

    def delete(self, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id})

    def typing(self) -> None:
        """Show "typing..." for a few seconds while Claude thinks."""
        self.call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})

    def set_commands(self, commands: list[tuple[str, str]], chat_id: str | None = None) -> None:
        """The "/" menu, for everyone or (chat_id) for one chat only."""
        payload = {"commands": [{"command": c, "description": d} for c, d in commands]}
        if chat_id:
            payload["scope"] = {"type": "chat", "chat_id": chat_id}
        self.call("setMyCommands", payload)

    def username(self) -> str:
        if not getattr(self, "_username", None):
            self._username = self.call("getMe", {})["result"]["username"]
        return self._username

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

    def send_document(self, path: Path, caption: str = "", buttons: list[list[tuple[str, str]]] | None = None) -> None:
        boundary = uuid.uuid4().hex
        path = Path(path)
        body = b""
        caption = _chunks(caption, 1024)[0] if caption else ""  # HTML, cut outside tags
        fields = [("chat_id", self.chat_id), ("caption", caption), ("parse_mode", "HTML")]
        if buttons:
            fields.append(("reply_markup", json.dumps(_keyboard(buttons), ensure_ascii=False)))
        for name, value in fields:
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


KIND_LABELS = {"internship": "Praktikum", "apprenticeship": "Ausbildung"}


def posting_card(p: Posting, today: date | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    e = html.escape
    where = e(p.location or "?") + (" · remote" if p.remote else "")
    title = (f'<a href="{e(p.url, quote=True)}">{e(p.title)}</a>' if re.match(r"https?://", p.url or "")
             else e(p.title))  # sources are untrusted: no javascript: or other links
    lines = [f"<b>{title}</b>",
             f"{e(p.company or '?')} · {where}" + (f" · {e(KIND_LABELS.get(p.contract_type, p.contract_type))}" if p.contract_type else "")]
    if p.employer:
        lines.append(f"🏢 {e(p.employer)}")
    if re.match(r"https?://", p.website or ""):
        host = urllib.parse.urlsplit(p.website).hostname or p.website
        lines.append(f'🌐 <a href="{e(p.website, quote=True)}">{e(host.removeprefix("www."))}</a>')
    if (p.company or "?") != "?":  # the ratings sites block bots, so the person looks there (#32)
        name = urllib.parse.quote_plus(p.company)
        lines.append(f'💬 Bewertungen: <a href="https://www.kununu.com/de/search?q={e(name, quote=True)}">kununu</a>'
                     f' · <a href="https://www.glassdoor.de/Search/results.htm?keyword={e(name, quote=True)}">Glassdoor</a>')
    if p.salary:
        lines.append(f"💶 {e(p.salary)}")
    online = posted_ago(p.posted_at, today or date.today())
    if online:
        lines.append(f"📅 online {online}")
    if p.score is not None:
        lines.append(f"⭐ {p.score}/10 · {e(p.why)}")
    lines.append(f"<i>{e(p.source)}</i>")
    buttons = [[("🔎 Recherche", f"r:{p.id}"), ("⭐ Vormerken", f"v:{p.id}")]]
    return "\n".join(lines), buttons
