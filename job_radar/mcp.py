"""An instance's bookmarks as an MCP server on stdin/stdout (#43).

Meant to be started by an MCP client on another machine over SSH, so the bot's machine opens no
port: `ssh <host> python3 -m job_radar --instance <name> mcp`. JSON-RPC 2.0, one message per
line; stdout carries nothing else, logs go to stderr. The only write is `confirm_imported`."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TextIO

from .app import _report_file, _tldr
from .model import Posting

log = logging.getLogger("job-radar")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
MAX_TEXT = 20_000  # characters of posting text in get_bookmark

TOOLS = [
    {"name": "list_bookmarks",
     "description": "Postings the person bookmarked (⭐ Vormerken) or marked as applied for in the Telegram "
                    "bot (field kind: saved or applied), oldest first. "
                    "By default only those not yet confirmed with confirm_imported. Without the posting "
                    "text; get_bookmark has it.",
     "inputSchema": {"type": "object", "properties": {
         "new_only": {"type": "boolean", "default": True,
                      "description": "only bookmarks not confirmed as imported yet"}}}},
    {"name": "get_bookmark",
     "description": "One bookmarked or applied-for posting with its stored text (may be only a summary, depending on "
                    "the source) and the summary of its deep report, if one was written.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "confirm_imported",
     "description": "Mark bookmarks as imported, so list_bookmarks no longer lists them as new. Call it "
                    "only after the import was saved.",
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}},
]


class ToolError(Exception):
    """A tool call the client got wrong: answered with isError, not as a protocol error."""


def _item(p: Posting, mark: dict) -> dict:
    return {"id": p.id, "kind": mark["kind"], "title": p.title, "company": p.company, "location": p.location,
            "remote": p.remote, "contract_type": p.contract_type, "salary": p.salary, "source": p.source,
            "url": p.url, "website": p.website, "employer": p.employer, "tags": p.tags,
            "posted_at": p.posted_at, "found_at": mark["first_seen"], "bookmarked_at": mark["at"],
            "score": p.score, "why": p.why, "exported_at": mark["exported_at"]}


def _bookmarks(store, new_only: bool) -> list[tuple[Posting, dict]]:
    if new_only:
        return store.unexported()
    marks = store.marked("saved", limit=None) + store.marked("applied", limit=None)
    return sorted(marks, key=lambda m: m[1]["seq"])


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ids(value) -> list[int]:
    if not isinstance(value, list) or not all(_is_int(i) for i in value):
        raise ToolError("ids must be a list of integers")
    return value


def call_tool(cfg: dict, store, name: str, args: dict):
    if name == "list_bookmarks":
        new_only = args.get("new_only", True)
        if not isinstance(new_only, bool):
            raise ToolError("new_only must be true or false")
        return [_item(p, m) for p, m in _bookmarks(store, new_only)]
    if name == "get_bookmark":
        pid = args.get("id")
        found = store.bookmark(pid) if _is_int(pid) else None
        if not found:
            raise ToolError(f"no bookmark with id {pid!r}")
        p, mark = found
        report = _report_file(cfg, store, p)
        summary = _tldr(Path(report).read_text(encoding="utf-8")) if report else ""
        return {**_item(p, mark), "text": p.text[:MAX_TEXT], "text_cut": len(p.text) > MAX_TEXT,
                "report_summary": summary}
    if name == "confirm_imported":
        return {"confirmed": store.set_exported(_ids(args.get("ids")))}
    raise ToolError(f"unknown tool {name!r}")


def _handle(cfg: dict, store, msg: dict) -> dict | None:
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "job-radar", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            result = call_tool(cfg, store, params.get("name"), params.get("arguments") or {})
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
    raise LookupError(method)


def serve(cfg: dict, store, inp: TextIO, out: TextIO) -> int:
    """Answer requests until the client closes stdin."""
    def send(reply: dict) -> None:
        out.write(json.dumps({"jsonrpc": "2.0", **reply}, ensure_ascii=False) + "\n")
        out.flush()

    for line in inp:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(msg, dict):  # e.g. a batch, which MCP no longer has
            send({"id": None, "error": {"code": -32600, "message": "invalid request"}})
            continue
        if "id" not in msg or "method" not in msg:  # a notification, or the client's answer to a request
            continue
        try:
            send({"id": msg["id"], "result": _handle(cfg, store, msg)})
        except LookupError:
            send({"id": msg["id"], "error": {"code": -32601, "message": f"method not found: {msg.get('method')}"}})
        except Exception as e:  # one bad request must not end the session
            log.exception("mcp request failed")
            send({"id": msg["id"], "error": {"code": -32603, "message": str(e)}})
    return 0
