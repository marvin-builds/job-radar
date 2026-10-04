"""Orchestration: poll sources, triage, notify; react to Telegram buttons."""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path

from . import claude, config, interview
from .filter import matches
from .http import get_json
from .model import Posting
from .sources import REGISTRY
from .telegram import posting_card

log = logging.getLogger("job-radar")
BATCH = 10
COMMANDS = [("help", "What this bot can do"), ("setup", "Set up or change your job search"),
            ("profile", "Show your profile"), ("usage", "AI usage today, 7 and 30 days")]
HELP = """<b>job-radar</b>
New matching jobs arrive hourly (Mon–Fri 07–21, weekends every 2 h), at most 5 at a time.

<b>Buttons on each job</b>
🔎 Deep report – research the job and employer, report arrives in a few minutes
👎 Skip – hide it
✅ Applied – add it to the application tracker

<b>Messages</b>
Paste a job link – deep report for a posting you found yourself
/setup – set up or change your job search (a short interview)
/profile – show your profile
/usage – AI usage for today, 7 and 30 days
/help – this message"""


def has_profile(cfg: dict) -> bool:
    return any(Path(f).exists() for f in cfg.get("profile_files", []))


def profile_text(cfg: dict) -> str:
    parts = [Path(f).read_text(encoding="utf-8") for f in cfg.get("profile_files", []) if Path(f).exists()]
    return "\n\n".join(parts) or "(no profile configured: rate only on what the posting itself says)"


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


NO_PROFILE = "👋 Your job search is not set up yet. Send /setup and I'll ask you a few questions."


def poll(cfg: dict, store, bot, fetch=None, triage_fn=claude.triage, dry_run: bool = False) -> dict:
    if not has_profile(cfg):
        if not dry_run and not store.get_state("setup_nudged"):
            bot.send(NO_PROFILE)
            store.set_state("setup_nudged", "1")
        return {"skipped": "no profile"}
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
                scores = triage_fn(batch, profile=profile, model=tcfg["model"],
                                   on_usage=lambda u: store.record_usage("triage", u))
            except Exception as e:
                log.warning("triage failed, retrying next run: %s", e)
                continue
            for p in batch:
                if p.id in scores:
                    p.score, p.why, *rest = scores[p.id]
                    p.employer = rest[0] if rest else ""
                    store.set_score(p.id, p.score, p.why)
                    store.set_employer(p.id, p.employer)
        threshold = tcfg.get("threshold", 6)
        candidates = []
        for p in to_score:
            if p.score is None:
                continue
            if p.score >= threshold:
                candidates.append(p)
            else:
                store.set_status(p.id, "low")

    # Good matches that did not fit into an earlier run's card limit go first.
    candidates += store.by_status("queued")
    candidates.sort(key=lambda p: -(p.score if p.score is not None else 0))
    limit = cfg.get("notify", {}).get("max_per_run", 5)
    candidates, overflow = candidates[:limit], candidates[limit:]
    for p in overflow:
        store.set_status(p.id, "queued")

    for p in candidates:
        text, buttons = posting_card(p)
        if dry_run:
            print(text, "\n")
            continue
        bot.send(text, buttons)
        store.mark_notified(p.id)
        store.set_status(p.id, "notified")
    return {"fetched": len(fetched), "new": len(new), "matched": len(matched), "notified": len(candidates)}


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _slug(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text.lower().translate(_UMLAUTS)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:limit].strip("-")


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
                       timeout=cfg["report"].get("timeout_s", 900),
                       on_usage=lambda u: store.record_usage("report", u))
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


LIMITS_MAX_AGE_S = 15 * 60


def _resets(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d.%m. %H:%M") if ts else "?"


def usage_text(store, probe=None) -> str:
    limits = store.latest_limits()
    if probe and (limits is None or limits["age_s"] > LIMITS_MAX_AGE_S):
        try:
            probe()
            limits = store.latest_limits()
        except Exception as e:
            log.warning("limit probe failed: %s", e)
    lines = ["<b>Plan limits</b> (whole Claude account, including your own use)"]
    if limits:
        for label, key in (("5-hour window", "five_hour"), ("Weekly", "seven_day")):
            if limits.get(key) is not None:
                lines.append(f"{label}: {round(limits[key] * 100)}% · resets {_resets(limits.get(key + '_resets'))}")
        lines.append(f"<i>as of {limits['age_s'] // 60} min ago</i>")
    else:
        lines.append("unknown (no Claude call recorded yet)")
    lines += ["", "<b>job-radar's own calls</b> (list-price equivalent, not billed on a subscription)"]
    for label, days in (("Today", 1), ("7 days", 7), ("30 days", 30)):
        summary = store.usage_summary(days)
        if not summary:
            lines.append(f"{label}: none")
            continue
        total = sum(s["cost_usd"] or 0 for s in summary.values())
        parts = ", ".join(f"{kind} {s['calls']}× ${s['cost_usd'] or 0:.2f} "
                          f"({(s['input_tokens'] or 0) // 1000}k in / {(s['output_tokens'] or 0) // 1000}k out)"
                          for kind, s in sorted(summary.items()))
        lines.append(f"{label}: ${total:.2f} · {parts}")
    return "\n".join(lines)


def append_tracker(cfg: dict, p: Posting) -> None:
    tracker = cfg.get("tracker_path")
    if not tracker or not Path(tracker).exists():
        return
    row = f"| {date.today().isoformat()} | {p.company} | {p.title} | {p.source} | Applied | | {p.url} |\n"
    with open(tracker, "a", encoding="utf-8") as f:
        f.write(row)


def _thread(fn) -> None:
    threading.Thread(target=fn, daemon=False).start()


def _answer(bot, callback_id: str, text: str) -> None:
    """Acknowledge a button tap; Telegram rejects answers to old taps, which is harmless."""
    try:
        bot.answer(callback_id, text)
    except Exception as e:
        log.info("could not answer button tap: %s", e)


def count_matches(cfg: dict) -> int:
    """Postings the new settings would have let through, as a sanity check after setup."""
    fresh = config.load(Path(cfg["config_dir"]) / "config.json")
    return sum(matches(p, fresh["filter"]) for p in fetch_all(fresh))


def _interview_turn(cfg: dict, store):
    return lambda log, previous: interview.turn(log, previous=previous, model=cfg["interview"]["model"],
                                                on_usage=lambda u: store.record_usage("interview", u))


def _interview_step(cfg: dict, store, bot, interview_fn, message: str | None) -> None:
    """Send the next question or the proposal; record the exchange only if Claude answered."""
    transcript = store.interview_log() + ([("person", message)] if message else [])
    previous = profile_text(cfg) if has_profile(cfg) else ""
    try:
        bot.typing()
    except Exception as e:  # "typing..." is cosmetic
        log.info("could not show typing: %s", e)
    try:
        reply = interview_fn(transcript, previous)
    except Exception as e:
        log.warning("interview turn failed: %s", e)
        bot.send("⚠️ Sorry, that went wrong on my side. Please send your last message again.")
        return
    if message:
        store.interview_add("person", message)
    if not reply["done"]:
        store.interview_add("bot", reply["say"])
        store.set_state("interview", "active")
        bot.send(html.escape(reply["say"]))
        return
    store.interview_add("bot", "Proposal:\n" + reply["summary"])
    store.set_state("interview_proposal", json.dumps(reply))
    store.set_state("interview", "proposed")
    bot.send(f"<b>Here's your job search:</b>\n\n{html.escape(reply['summary'])}",
             [[("✅ Looks good", "i:ok"), ("✏️ Change", "i:edit")]])


def _interview_confirm(cfg: dict, store, bot, count_fn) -> None:
    reply = json.loads(store.get_state("interview_proposal") or "{}")
    if store.get_state("interview") != "proposed" or not reply:
        bot.send("Nothing to confirm. Send /setup to start.")
        return
    cdir = Path(cfg["config_dir"])
    cdir.mkdir(parents=True, exist_ok=True)
    config.write_search(cdir / "search.json", reply["search"])
    (cdir / "profile.md").write_text(reply["profile_md"].strip() + "\n", encoding="utf-8")
    store.interview_clear()
    store.set_state("interview", "")
    store.set_state("interview_proposal", "")
    store.set_state("setup_nudged", "")
    bot.send("✅ Saved. Checking how many current postings match…")
    try:
        n = count_fn(cfg)
    except Exception as e:
        log.warning("match count failed: %s", e)
        bot.send("New matching jobs will arrive here from the next hourly run.")
        return
    days = cfg["sources"].get("ba", {}).get("published_since_days", 7)
    if n:
        bot.send(f"{n} postings from the last {days} days pass your search; the AI rates them next. "
                 "New matching jobs arrive here from the next hourly run.")
    else:
        bot.send(f"No postings from the last {days} days pass this search. Send /setup to widen it.")


def handle_update(upd: dict, cfg: dict, store, bot, report_fn=claude.deep_report, background=_thread,
                  interview_fn=None, count_fn=count_matches) -> None:
    interview_fn = interview_fn or _interview_turn(cfg, store)
    cb = upd.get("callback_query")
    if not cb:
        text = upd.get("message", {}).get("text", "")
        command = text.strip().split()[0] if text.strip().startswith("/") else ""
        if command == "/setup" or (command == "/start" and not has_profile(cfg)):
            store.interview_clear()
            _interview_step(cfg, store, bot, interview_fn, None)
            return
        if command == "/profile":
            bot.send(html.escape(profile_text(cfg)) if has_profile(cfg)
                     else "No profile yet. Send /setup to create one.")
            return
        if not command and store.get_state("interview") in ("active", "proposed"):
            _interview_step(cfg, store, bot, interview_fn, text.strip())
            return
        if text.strip().startswith("/usage"):
            bot.send(usage_text(store, probe=lambda: claude.probe_limits(
                on_usage=lambda u: store.record_usage("probe", u))))
            return
        if text.strip().startswith(("/help", "/start")):
            bot.send(HELP)
            return
        link = re.search(r"https?://\S+", text)
        if link:
            p = posting_for_url(store, link.group(0))
            store.set_status(p.id, "interesting")
            bot.send("🔎 Researching that posting, the report arrives in a few minutes.")
            background(lambda: run_report(cfg, store, bot, p, report_fn))
        else:
            bot.send("I didn't find a job link in that message.\n\n" + HELP)
        return
    action, _, raw_id = cb.get("data", "").partition(":")
    if action == "i":
        _answer(bot, cb["id"], "")
        if raw_id == "ok":
            _interview_confirm(cfg, store, bot, count_fn)
        elif raw_id == "edit":
            store.set_state("interview", "active")
            store.interview_add("bot", "What should change?")
            bot.send("✏️ What should change?")
        return
    p = store.get(int(raw_id)) if raw_id.isdigit() else None
    if p is None:
        _answer(bot, cb["id"], "Unknown posting")
        return
    if action == "s":
        store.set_status(p.id, "skipped")
        _answer(bot, cb["id"], "Skipped")
    elif action == "a":
        store.set_status(p.id, "applied")
        append_tracker(cfg, p)
        _answer(bot, cb["id"], "Marked as applied")
    elif action == "r":
        store.set_status(p.id, "interesting")
        _answer(bot, cb["id"], "Researching, this takes a few minutes")
        background(lambda: run_report(cfg, store, bot, p, report_fn))
    else:
        _answer(bot, cb["id"], "Unknown action")


def resume_reports(cfg: dict, store, bot, background=_thread) -> None:
    """Restart reports that a service restart interrupted."""
    for p in store.by_status("interesting"):
        log.info("resuming report for %s", p.id)
        background(lambda p=p: run_report(cfg, store, bot, p))


def listen(cfg: dict, store, bot, stop=lambda: False, handle=handle_update) -> None:
    log.info("listening for Telegram updates")
    try:
        bot.set_commands(COMMANDS)  # the "/" menu in the chat
    except Exception as e:
        log.warning("could not register commands: %s", e)
    resume_reports(cfg, store, bot)
    while not stop():
        try:
            batch = bot.updates(wait=50)
        except Exception as e:  # network hiccups: keep the service alive
            log.warning("could not fetch updates: %s", e)
            threading.Event().wait(10)
            continue
        for upd in batch:
            try:
                handle(upd, cfg, store, bot)
            except Exception:
                log.exception("update %s failed", upd.get("update_id"))
