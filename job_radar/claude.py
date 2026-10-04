"""Headless Claude Code calls (`claude -p`).

Triage runs with no tools; the deep report gets only WebSearch and WebFetch.
`--restricted` keeps command execution out and ignores the user's settings files,
so job-board text cannot steer Claude into touching this machine.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from pathlib import Path

from .model import Posting

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
TRIAGE_TEXT_LIMIT = 1500


class ClaudeError(RuntimeError):
    pass


def run(prompt: str, *, model: str, tools: list[str], timeout: int = 300, runner=subprocess.run) -> str:
    args = ["claude", "-p", "--output-format", "json", "--model", model,
            "--restricted", "--strict-mcp-config", "--no-session-persistence",
            "--tools", ",".join(tools)]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    proc = runner(args, input=prompt, capture_output=True, text=True, timeout=timeout)
    try:
        out = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        out = {}
    if proc.returncode != 0 or out.get("is_error") or "result" not in out:
        raise ClaudeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[:500]}")
    return out["result"]


def _fill(template: str, **values: str) -> str:
    text = (PROMPTS / template).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def describe(p: Posting, text_limit: int | None = None) -> str:
    text = p.text if text_limit is None else p.text[:text_limit]
    lines = [f"id: {p.id}", f"title: {p.title}", f"company: {p.company}", f"location: {p.location}",
             f"remote: {p.remote}", f"contract: {p.contract_type or 'unknown'}", f"url: {p.url}"]
    if p.salary:
        lines.append(f"salary: {p.salary}")
    if p.tags:
        lines.append(f"tags: {', '.join(p.tags)}")
    return "\n".join(lines) + f"\ntext:\n{text}"


def _json_array(reply: str) -> list:
    match = re.search(r"\[.*\]", reply, re.S)
    if not match:
        raise ClaudeError(f"no JSON array in triage reply: {reply[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ClaudeError(f"bad triage JSON: {e}") from e


def triage(postings: list[Posting], *, profile: str, model: str, runner=subprocess.run) -> dict[int, tuple[int, str]]:
    block = "\n\n---\n\n".join(describe(p, TRIAGE_TEXT_LIMIT) for p in postings)
    reply = run(_fill("triage.md", profile=profile, postings=block), model=model, tools=[], runner=runner)
    known = {p.id for p in postings}
    scores = {}
    for item in _json_array(reply):
        try:
            pid, score = int(item["id"]), int(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in known:
            scores[pid] = (score, str(item.get("why", "")))
    return scores


def deep_report(p: Posting, *, profile: str, model: str, timeout: int = 900, runner=subprocess.run) -> str:
    prompt = _fill("deep-report.md", profile=profile, posting=describe(p), today=date.today().isoformat())
    return run(prompt, model=model, tools=["WebSearch", "WebFetch"], timeout=timeout, runner=runner).strip()
