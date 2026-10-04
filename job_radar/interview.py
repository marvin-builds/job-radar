"""Telegram onboarding: Claude interviews the person and derives profile and search settings.

Each turn is one stateless `claude -p` call without tools; the transcript lives in the store.
"""
from __future__ import annotations

import json
import re
import subprocess

from . import claude
from .config import validate_search

ROLES = {"bot": "Interviewer", "person": "Person"}


class InterviewError(RuntimeError):
    pass


def _transcript(log: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{ROLES.get(role, role)}: {text}" for role, text in log) or "(no messages yet: start)"


def _parse(reply: str) -> dict:
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        raise InterviewError(f"no JSON object in reply: {reply[:200]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise InterviewError(f"bad JSON: {e}") from e
    if not data.get("done"):
        if not isinstance(data.get("say"), str) or not data["say"].strip():
            raise InterviewError('a question reply needs a non-empty "say"')
        return {"say": data["say"], "done": False}
    for key in ("summary", "profile_md"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise InterviewError(f'a finished reply needs a non-empty "{key}"')
    try:
        validate_search(data.get("search"))
    except ValueError as e:
        raise InterviewError(f"invalid search settings: {e}") from e
    return {"done": True, "summary": data["summary"], "profile_md": data["profile_md"], "search": data["search"]}


def turn(log: list[tuple[str, str]], *, previous: str, model: str, runner=subprocess.run, on_usage=None) -> dict:
    """The interviewer's next message, or the finished profile and settings."""
    prompt = claude._fill("interview.md", previous=previous or "(none: first setup)", transcript=_transcript(log))
    error = None
    for _ in range(2):
        text = prompt if error is None else (
            f"{prompt}\n\n## Your previous reply was rejected\n\n{error}\nReply again, fixing this.")
        try:
            return _parse(claude.run(text, model=model, tools=[], runner=runner, on_usage=on_usage))
        except InterviewError as e:
            error = str(e)
    raise InterviewError(error)
