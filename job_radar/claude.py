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


def _usage(model: str, out: dict, limits: dict) -> dict:
    u = out.get("usage") or {}
    usage = {"model": model, "cost_usd": out.get("total_cost_usd", 0),
             "input_tokens": sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                        "cache_read_input_tokens")),
             "output_tokens": u.get("output_tokens", 0), "duration_ms": out.get("duration_ms", 0)}
    # Plan limits of the whole account (not only this service), as Claude Code reports them.
    for window in ("five_hour", "seven_day"):
        if window in limits:
            usage[window] = limits[window].get("utilization")
            usage[f"{window}_resets"] = limits[window].get("resetsAt")
    return usage


def _parse_stream(stdout: str) -> tuple[dict, dict]:
    result, limits = {}, {}
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
        elif event.get("type") == "rate_limit_event":
            limits = (event.get("rate_limit_info") or {}).get("unifiedWindows") or limits
    return result, limits


def run(prompt: str, *, model: str, tools: list[str], timeout: int = 300, runner=subprocess.run,
        on_usage=None) -> str:
    args = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--restricted", "--strict-mcp-config", "--no-session-persistence",
            "--tools", ",".join(tools)]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    proc = runner(args, input=prompt, capture_output=True, text=True, timeout=timeout)
    out, limits = _parse_stream(proc.stdout)
    if proc.returncode != 0 or out.get("is_error") or "result" not in out:
        raise ClaudeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[:500]}")
    if on_usage:
        on_usage(_usage(model, out, limits))
    return out["result"]


def probe_limits(*, runner=subprocess.run, on_usage=None) -> None:
    """Tiny Haiku call, only to read the current plan-limit utilization."""
    run("Reply with: ok", model="haiku", tools=[], timeout=120, runner=runner, on_usage=on_usage)


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


def triage(postings: list[Posting], *, profile: str, model: str, runner=subprocess.run,
           on_usage=None) -> dict[int, tuple[int, str, str]]:
    block = "\n\n---\n\n".join(describe(p, TRIAGE_TEXT_LIMIT) for p in postings)
    reply = run(_fill("triage.md", profile=profile, postings=block), model=model, tools=[], runner=runner,
                on_usage=on_usage)
    known = {p.id for p in postings}
    scores = {}
    for item in _json_array(reply):
        try:
            pid, score = int(item["id"]), int(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in known:
            scores[pid] = (score, str(item.get("why", "")), str(item.get("employer", "")))
    return scores


def deep_report(p: Posting, *, profile: str, model: str, timeout: int = 900, runner=subprocess.run,
                on_usage=None) -> str:
    prompt = _fill("deep-report.md", profile=profile, posting=describe(p), today=date.today().isoformat())
    return run(prompt, model=model, tools=["WebSearch", "WebFetch"], timeout=timeout, runner=runner,
               on_usage=on_usage).strip()
