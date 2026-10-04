"""Orchestration: poll sources, triage, notify; react to Telegram buttons."""
from __future__ import annotations

import logging
import re
import threading
from datetime import date
from pathlib import Path

from . import claude
from .filter import matches
from .http import get_json
from .model import Posting
from .sources import REGISTRY
from .telegram import posting_card

log = logging.getLogger("job-radar")
BATCH = 10


def profile_text(cfg: dict) -> str:
    parts = [Path(f).read_text(encoding="utf-8") for f in cfg.get("profile_files", []) if Path(f).exists()]
    return "\n\n".join(parts) or "(no profile configured: rate on general senior backend fit)"


def fetch_all(cfg: dict, registry=REGISTRY, http_get=get_json, on_error=None) -> list[Posting]:
    postings = []
    for name, scfg in cfg["sources"].items():
        if not scfg.get("enabled", True) or name not in registry:
            continue
        try:
            got = registry[name].fetch(scfg, http_get)
            log.info("source %s: %d postings", name, len(got))
            postings += got
        except Exception as e:  # one broken portal must not stop the run
            log.warning("source %s failed: %s", name, e)
            if on_error:
                on_error(f"{name}: {e}")
    return postings


def poll(cfg: dict, store, bot, fetch=None, triage_fn=claude.triage, dry_run: bool = False) -> dict:
    fetched = (fetch or fetch_all)(cfg)
    new = store.add_new(fetched)
    matched = []
    for p in new:
        if matches(p, cfg["filter"]):
            matched.append(p)
        else:
            store.set_status(p.id, "filtered")

    # Matches wait as "pending" until scored, so a failed or capped triage run
    # retries them next time instead of sending them unscored.
    for p in matched:
        store.set_status(p.id, "pending")
    queue = store.by_status("pending")

    tcfg = cfg["triage"]
    if not tcfg.get("enabled", True):
        candidates = queue
    else:
        profile = profile_text(cfg)
        to_score = queue[: tcfg.get("max_per_run", 40)]
        for i in range(0, len(to_score), BATCH):
            batch = to_score[i:i + BATCH]
            try:
                scores = triage_fn(batch, profile=profile, model=tcfg["model"])
            except Exception as e:
                log.warning("triage failed, retrying next run: %s", e)
                continue
            for p in batch:
                if p.id in scores:
                    p.score, p.why = scores[p.id]
                    store.set_score(p.id, p.score, p.why)
        threshold = tcfg.get("threshold", 6)
        candidates = []
        for p in to_score:
            if p.score is None:
                continue
            if p.score >= threshold:
                candidates.append(p)
            else:
                store.set_status(p.id, "low")
        candidates.sort(key=lambda p: -p.score)

    for p in candidates:
        text, buttons = posting_card(p)
        if dry_run:
            print(text, "\n")
            continue
        bot.send(text, buttons)
        store.mark_notified(p.id)
        store.set_status(p.id, "notified")
    return {"fetched": len(fetched), "new": len(new), "matched": len(matched), "notified": len(candidates)}


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def write_report(cfg: dict, p: Posting, markdown: str) -> Path:
    out_dir = Path(cfg["report"]["reports_dir"] or Path.cwd())
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date.today():%y-%m-%d}-{_slug(p.company)}-{_slug(p.title)}.md"
    path.write_text(markdown + "\n", encoding="utf-8")
    return path


def _tldr(markdown: str) -> str:
    match = re.search(r"#+\s*(?:1\.\s*)?TL;DR\s*\n(.*?)(?:\n#|\Z)", markdown, re.S | re.I)
    return (match.group(1).strip() if match else "")[:1000]


def _frontmatter(markdown: str, key: str) -> str:
    match = re.search(rf'^{key}:\s*"?(.*?)"?\s*$', markdown.split("\n---", 1)[0], re.M)
    return match.group(1).strip() if match else ""


def posting_for_url(store, url: str) -> Posting:
    """A posting the user found themselves; the report fills in company and role."""
    existing = store.find("manual", url)
    if existing:
        return existing
    slug = url.rstrip("/").rsplit("/", 1)[-1] or url
    return store.add_new([Posting(source="manual", external_id=url, url=url, title=slug)])[0]


def run_report(cfg: dict, store, bot, p: Posting, report_fn=claude.deep_report) -> None:
    try:
        md = report_fn(p, profile=profile_text(cfg), model=cfg["report"]["model"],
                       timeout=cfg["report"].get("timeout_s", 900))
        if p.source == "manual":
            p.company = _frontmatter(md, "company") or p.company
            p.title = _frontmatter(md, "role") or p.title
            store.set_meta(p.id, p.company, p.title)
        path = write_report(cfg, p, md)
    except Exception as e:
        log.exception("report for %s failed", p.id)
        bot.send(f"⚠️ Report for {p.title} failed: {e}")
        return
    store.set_status(p.id, "reported")
    bot.send_document(path, caption=f"{p.company} - {p.title}\n\n{_tldr(md)}")


def append_tracker(cfg: dict, p: Posting) -> None:
    tracker = cfg.get("tracker_path")
    if not tracker or not Path(tracker).exists():
        return
    row = f"| {date.today().isoformat()} | {p.company} | {p.title} | {p.source} | Applied | | {p.url} |\n"
    with open(tracker, "a", encoding="utf-8") as f:
        f.write(row)


def _thread(fn) -> None:
    threading.Thread(target=fn, daemon=False).start()


def handle_update(upd: dict, cfg: dict, store, bot, report_fn=claude.deep_report, background=_thread) -> None:
    cb = upd.get("callback_query")
    if not cb:
        text = upd.get("message", {}).get("text", "")
        link = re.search(r"https?://\S+", text)
        if link:
            p = posting_for_url(store, link.group(0))
            store.set_status(p.id, "interesting")
            bot.send("🔎 Researching that posting, the report arrives in a few minutes.")
            background(lambda: run_report(cfg, store, bot, p, report_fn))
        else:
            bot.send("job-radar is listening. Digests arrive after each scheduled poll; "
                     "paste a job link here to get a deep report.")
        return
    action, _, raw_id = cb.get("data", "").partition(":")
    p = store.get(int(raw_id)) if raw_id.isdigit() else None
    if p is None:
        bot.answer(cb["id"], "Unknown posting")
        return
    if action == "s":
        store.set_status(p.id, "skipped")
        bot.answer(cb["id"], "Skipped")
    elif action == "a":
        store.set_status(p.id, "applied")
        append_tracker(cfg, p)
        bot.answer(cb["id"], "Marked as applied")
    elif action == "r":
        store.set_status(p.id, "interesting")
        bot.answer(cb["id"], "Researching, this takes a few minutes")
        background(lambda: run_report(cfg, store, bot, p, report_fn))
    else:
        bot.answer(cb["id"], "Unknown action")


def listen(cfg: dict, store, bot, stop=lambda: False) -> None:
    log.info("listening for Telegram updates")
    while not stop():
        try:
            for upd in bot.updates(wait=50):
                handle_update(upd, cfg, store, bot)
        except Exception as e:  # network hiccups: keep the service alive
            log.warning("update loop error: %s", e)
            threading.Event().wait(10)
