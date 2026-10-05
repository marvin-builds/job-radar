"""Orchestration: poll sources, triage, notify; react to Telegram buttons."""
from __future__ import annotations

import contextlib
import html
import json
import logging
import re
import queue
import threading
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

from . import changes, claude, config, feedback, interview
from .filter import matches
from .http import get_json, get_text
from .model import Posting
from .sources import REGISTRY, feeds
from .store import PollLock, PollLocked, Store
from .telegram import Bot, posting_card

log = logging.getLogger("job-radar")
BATCH = 10
TYPING_EVERY_S = 4  # Telegram hides "typing..." after about 5 s
THINKING = "⏳ Moment, ich denke nach …"
COMMANDS = [("help", "Was dieser Bot kann"), ("setup", "Jobsuche einrichten oder ändern"),
            ("profile", "Dein Profil anzeigen"),
            ("abbrechen", "Laufende Einrichtung abbrechen"), ("quellen", "Jobbörsen aus deinem Fachgebiet"),
            ("usage", "KI-Nutzung heute, 7 und 30 Tage"), ("feedback", "Lob, Kritik oder Ideen schicken")]
HELP = """<b>Job-Radar</b>
Neue passende Stellen kommen stündlich (Mo–Fr 7–21 Uhr, am Wochenende alle 2 Stunden), höchstens 5 auf einmal.

<b>Knöpfe an jeder Stelle</b>
🔎 Recherche – Stelle und Arbeitgeber genau prüfen, der Bericht kommt nach ein paar Minuten
👎 Überspringen – ausblenden
✅ Beworben – als beworben merken

<b>Nachrichten</b>
Link zu einer Stelle schicken – Recherche zu einer Stelle, die du selbst gefunden hast
/setup – Jobsuche einrichten oder ändern (ein kurzes Gespräch)
/abbrechen – eine laufende Einrichtung abbrechen
/profile – dein Profil anzeigen
/quellen – Jobbörsen aus deinem Fachgebiet suchen und verwalten
/usage – KI-Nutzung heute, 7 und 30 Tage
/feedback – Lob, Kritik oder Ideen zum Bot schicken
/help – diese Hilfe"""


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


NEEDS_INVITE = ("🔒 Dieser Bot ist privat. Du brauchst einen Einladungslink von der Person, die ihn betreibt. "
                "Jeder Link funktioniert nur einmal.")
INVITE_USED = "Dieser Einladungslink ist nicht mehr gültig. Deine Jobsuche hier läuft weiter wie bisher."
CHAT_MOVED = ("🔁 Dieser Chat gehört jetzt zur Jobsuche aus dem neuen Einladungslink. "
              "Die bisherige Jobsuche schickt dir keine Nachrichten mehr.")
NO_PROFILE ="👋 Deine Jobsuche ist noch nicht eingerichtet. Schick /setup, dann stelle ich dir ein paar Fragen."


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
        bot.send(f"⚠️ Recherche zu {p.title} fehlgeschlagen: {e}")
        return
    store.set_status(p.id, "reported")
    bot.send_document(path, caption=f"{p.company} - {p.title}\n\n{_tldr(md)}")


LIMITS_MAX_AGE_S = 15 * 60


_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _resets(ts) -> str:
    if not ts:
        return "?"
    when = datetime.fromtimestamp(ts)
    return f"{_WEEKDAYS[when.weekday()]} {when:%d.%m. %H:%M}"


def usage_text(store, probe=None) -> str:
    limits = store.latest_limits()
    if probe and (limits is None or limits["age_s"] > LIMITS_MAX_AGE_S):
        try:
            probe()
            limits = store.latest_limits()
        except Exception as e:
            log.warning("limit probe failed: %s", e)
    lines = ["<b>Plan-Limits</b> (ganzes Claude-Konto, alle Nutzer zusammen)"]
    if limits:
        for label, key in (("5-Stunden-Fenster", "five_hour"), ("Woche", "seven_day")):
            if limits.get(key) is not None:
                lines.append(f"{label}: {round(limits[key] * 100)}% · zurückgesetzt {_resets(limits.get(key + '_resets'))}")
        lines.append(f"<i>Stand vor {limits['age_s'] // 60} Min.</i>")
    else:
        lines.append("unbekannt (noch kein Claude-Aufruf)")
    lines += ["", "<b>Aufrufe für diese Jobsuche</b> (zum Listenpreis gerechnet, im Abo nicht berechnet)"]
    for label, days in (("Heute", 1), ("7 Tage", 7), ("30 Tage", 30)):
        summary = store.usage_summary(days)
        if not summary:
            lines.append(f"{label}: keine")
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


@contextlib.contextmanager
def _waiting(bot):
    """Show a wait note and "typing..." until the block ends; a Claude call can take a minute.

    Telegram apps do not always show "typing...", so the note is what the person can rely on."""
    try:
        note = bot.note(THINKING)
    except Exception as e:  # cosmetic, like "typing..."
        log.info("could not send the wait note: %s", e)
        note = None
    done = threading.Event()

    def keep_typing():
        while not done.is_set():
            try:
                bot.typing()
            except Exception as e:  # a hiccup must not end the indicator for this turn
                log.info("could not show typing: %s", e)
            done.wait(TYPING_EVERY_S)

    t = threading.Thread(target=keep_typing, daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join()
        if note is not None:
            try:
                bot.delete(note)
            except Exception as e:
                log.info("could not delete the wait note: %s", e)


def _thread(fn) -> None:
    threading.Thread(target=fn, daemon=False).start()


BUSY_TIMEOUT_S = 45 * 60  # a crashed job's mark must not block deploys forever
ACTIVE_CHAT_S = 60  # someone who just tapped is probably tapping again


@contextlib.contextmanager
def busy_with(store, what: str):
    """Mark a job a listener restart would cut off, so `busy` (deploy.sh) waits for it."""
    store.set_state("busy:" + what, str(time.time()))
    try:
        yield
    finally:
        store.set_state("busy:" + what, "")


def busy_reasons(store) -> tuple[list[str], list[str]]:
    """Why a listener restart now would hurt this instance: (blocking, only worth listing)."""
    now, blocking = time.time(), []
    log = store.interview_log()
    if store.get_state("interview") == "active" and log and log[-1][0] == "person":
        blocking.append("interview turn running")
    for key, since in sorted(store.states("busy:").items()):
        if now - float(since) < BUSY_TIMEOUT_S:
            blocking.append(f"{key.removeprefix('busy:')} running")
    last = store.get_state("last_update")
    if last and now - float(last) < ACTIVE_CHAT_S:
        blocking.append("using the bot right now")
    reports = len(store.by_status("interesting"))
    listed = [f"{reports} report(s) running, resumed after a restart"] if reports else []
    return blocking, listed


def _answer(bot, callback_id: str, text: str) -> None:
    """Acknowledge a button tap; Telegram rejects answers to old taps, which is harmless."""
    try:
        bot.answer(callback_id, text)
    except Exception as e:
        log.info("could not answer button tap: %s", e)


def count_matches(cfg: dict, fetch=fetch_all) -> int:
    """Postings the new settings would have let through, as a sanity check after setup."""
    fresh = config.load(Path(cfg["config_dir"]) / "config.json")
    errors = []
    postings = fetch(fresh, on_error=errors.append)
    if errors:  # a portal outage must not read as "nothing matches"
        raise RuntimeError("; ".join(errors))
    return sum(matches(p, fresh["filter"]) for p in postings)


def _interview_turn(cfg: dict, store):
    return lambda log, previous: interview.turn(log, previous=previous, model=cfg["interview"]["model"],
                                                on_usage=lambda u: store.record_usage("interview", u))


def _interview_step(cfg: dict, store, bot, interview_fn, message: str | None) -> None:
    """Send the next question or the proposal; record the exchange only if Claude answered."""
    transcript = store.interview_log() + ([("person", message)] if message else [])
    previous = profile_text(cfg) if has_profile(cfg) else ""
    try:
        with _waiting(bot):
            reply = interview_fn(transcript, previous)
    except Exception as e:
        log.warning("interview turn failed: %s", e)
        bot.send("⚠️ Da ist bei mir etwas schiefgegangen. Schick deine letzte Nachricht bitte nochmal.")
        return
    if message:
        store.interview_add("person", message)
    if not reply["done"]:
        store.interview_add("bot", reply["say"])
        store.set_state("interview", "active")
        bot.send(html.escape(reply["say"]))
        return
    store.interview_add("bot", "Vorschlag:\n" + reply["summary"])
    store.set_state("interview_proposal", json.dumps(reply))
    store.set_state("interview", "proposed")
    bot.send(f"<b>So würde ich für dich suchen:</b>\n\n{html.escape(reply['summary'])}",
             [[("✅ Passt so", "i:ok"), ("✏️ Ändern", "i:edit")]])


def _interview_cancel(cfg: dict, store, bot) -> None:
    if not store.get_state("interview"):
        bot.send("Es läuft gerade keine Einrichtung. Mit /setup fängst du eine an.")
        return
    store.interview_clear()
    store.set_state("interview", "")
    store.set_state("interview_proposal", "")
    bot.send("Okay, die Einrichtung ist abgebrochen. Nichts davon wurde gespeichert. "
             + ("Dein bisheriges Profil bleibt, wie es war." if has_profile(cfg)
                else "Mit /setup kannst du jederzeit neu anfangen."))


def _interview_confirm(cfg: dict, store, bot, count_fn, follow_up) -> None:
    """Save the proposal; `follow_up(poll_first)` then runs the first poll and the board search."""
    reply = json.loads(store.get_state("interview_proposal") or "{}")
    if store.get_state("interview") != "proposed" or not reply:
        bot.send("Da gibt es nichts zu bestätigen. Schick /setup, um anzufangen.")
        return
    cdir = Path(cfg["config_dir"])
    cdir.mkdir(parents=True, exist_ok=True)
    search = reply["search"]
    if _feeds(cfg):  # boards the person added stay; the interview does not know them
        search.setdefault("sources", {})["feeds"] = {"feeds": _feeds(cfg)}
    config.write_search(cdir / "search.json", search)
    (cdir / "profile.md").write_text(reply["profile_md"].strip() + "\n", encoding="utf-8")
    store.interview_clear()
    store.set_state("interview", "")
    store.set_state("interview_proposal", "")
    store.set_state("setup_nudged", "")
    bot.send("✅ Gespeichert. Ich schaue, wie viele aktuelle Stellen passen …")
    try:
        n = count_fn(cfg)
    except Exception as e:
        log.warning("match count failed: %s", e)
        bot.send("Neue passende Stellen kommen ab dem nächsten stündlichen Durchlauf hier an.")
        follow_up(False)
        return
    days = cfg["sources"].get("ba", {}).get("published_since_days", 7)
    if n:
        bot.send(f"{n} Stellen aus den letzten {days} Tagen passen zu deiner Suche. Die KI bewertet sie jetzt, "
                 "das dauert ein paar Minuten. Die besten schicke ich dir dann hier.")
    else:
        bot.send(f"Aus den letzten {days} Tagen passt keine Stelle zu dieser Suche. Schick /setup, um sie zu erweitern.")
    follow_up(bool(n))


def locked_poll(cfg: dict, store, bot) -> dict:
    """One poll; raises PollLocked while another poll of this instance runs (timer or manual)."""
    with PollLock(Path(cfg["db_path"]).with_name("poll.lock")):
        return poll(cfg, store, bot)


def reload(cfg: dict) -> dict:
    """The config as saved now; the listener loads it once, before any /setup changes it."""
    if cfg.get("instance"):
        return config.load_instance(cfg["instance"])
    return config.load(Path(cfg["config_dir"]) / "config.json")


def _first_poll(cfg: dict, store, bot, poll_fn) -> None:
    """Rate the matches right after setup instead of waiting for the next scheduled poll."""
    try:
        result = poll_fn(reload(cfg), store, bot)
    except PollLocked:
        return  # a poll is running anyway and sends the cards
    except Exception as e:
        log.warning("first poll failed: %s", e)
        bot.send("Das Bewerten hat gerade nicht geklappt. Ich versuche es beim nächsten stündlichen Durchlauf wieder.")
        return
    if result.get("notified"):
        bot.send("Das waren die besten Treffer. Neue Stellen prüfe ich ab jetzt regelmäßig.")
    else:
        bot.send("Fertig bewertet: Gerade passt keine Stelle gut genug. Neue Stellen prüfe ich ab jetzt regelmäßig.")


def _search_path(cfg: dict) -> Path:
    return Path(cfg["config_dir"]) / "search.json"


def _search(cfg: dict) -> dict:
    path = _search_path(cfg)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _feeds(cfg: dict) -> list[dict]:
    """The person's own job board feeds, as saved now (the listener's cfg may be older)."""
    return _search(cfg).get("sources", {}).get("feeds", {}).get("feeds", [])


def _save_feeds(cfg: dict, items: list[dict]) -> None:
    search = _search(cfg)
    search.setdefault("sources", {})["feeds"] = {"feeds": items}
    config.write_search(_search_path(cfg), search)


def check_feed(url: str, get=get_text) -> int:
    """Entries in the feed; raises if it does not load or is no RSS or Atom feed."""
    return len(feeds.parse(get(url), "check"))


def suggest_sources(cfg: dict, store, bot, find_fn=claude.find_sources, check_fn=check_feed) -> None:
    """Let Claude look for job boards of the person's field and offer the ones whose feed works."""
    with busy_with(store, "/quellen search"):
        _suggest_sources(cfg, store, bot, find_fn, check_fn)


def _suggest_sources(cfg: dict, store, bot, find_fn, check_fn) -> None:
    known = [f["url"] for f in _feeds(cfg)]
    bot.send("🔎 Ich suche jetzt nach Jobbörsen aus deinem Fachgebiet, die die großen Portale nicht abdecken. "
             "Das dauert ein paar Minuten.")
    try:
        found = find_fn(profile=profile_text(cfg), known=known, model=cfg["report"]["model"],
                        on_usage=lambda u: store.record_usage("sources", u))
    except Exception as e:
        log.warning("source search failed: %s", e)
        bot.send("⚠️ Die Suche nach Jobbörsen hat nicht geklappt. Versuch es später mit /quellen nochmal.")
        return
    offers = []
    for item in found:
        if item["url"] in known or any(o["url"] == item["url"] for o in offers):
            continue
        try:
            if check_fn(item["url"]):
                offers.append(item)
        except Exception as e:  # Claude can be wrong about a feed; only offer what loads
            log.info("feed %s does not load: %s", item["url"], e)
    store.set_state("sources_offer", json.dumps(offers))
    if not offers:
        bot.send("Ich habe gerade keine weitere passende Jobbörse gefunden, die ich automatisch lesen kann.")
        return
    bot.send(f"Diese {len(offers)} Jobbörsen passen zu dir. Soll ich dort auch nach Stellen schauen?")
    for i, item in enumerate(offers):
        bot.send(f"<b>{html.escape(item['name'])}</b>\n{html.escape(item['why'])}\n{html.escape(item['url'])}",
                 [[("➕ Aufnehmen", f"q:add:{i}"), ("Nein", f"q:no:{i}")]])


def _list_sources(cfg: dict, store, bot) -> None:
    current = _feeds(cfg)
    store.set_state("sources_shown", json.dumps(current))
    if not current:
        bot.send("Bisher suche ich nur in den großen Jobportalen.")
        return
    bot.send("<b>Deine zusätzlichen Jobbörsen</b>\nTippe auf eine, um sie zu entfernen.",
             [[(f"🗑 {f['name']}", f"q:del:{i}")] for i, f in enumerate(current)])


def _source_button(cfg: dict, store, bot, cb: dict, arg: str) -> None:
    what, _, raw = arg.partition(":")
    key = {"add": "sources_offer", "del": "sources_shown"}.get(what)
    items = json.loads(store.get_state(key) or "[]") if key else []
    item = items[int(raw)] if raw.isdigit() and int(raw) < len(items) else None
    if item is None:
        _answer(bot, cb["id"], "Okay" if what == "no" else "Veraltet, schick /quellen nochmal")
        return
    current = _feeds(cfg)
    if what == "add":
        if all(f["url"] != item["url"] for f in current):
            _save_feeds(cfg, current + [{"name": item["name"], "url": item["url"]}])
        _answer(bot, cb["id"], "Aufgenommen")
        bot.send(f"✅ {html.escape(item['name'])} ist jetzt in deiner Suche. Ab dem nächsten Durchlauf schaue ich dort auch.")
    else:
        if any(f["url"] == item["url"] for f in current):
            _save_feeds(cfg, [f for f in current if f["url"] != item["url"]])
        _answer(bot, cb["id"], "Entfernt")


def _send_feedback(cfg: dict, bot, feedback_fn, text: str) -> None:
    try:
        feedback_fn(cfg, text)
    except feedback.NotConfigured as e:
        log.warning("feedback not sent (%s): %s", e, text)
        bot.send("Feedback ist hier noch nicht eingerichtet. Sag bitte direkt Bescheid.")
        return
    except Exception as e:
        log.warning("feedback not sent (%s): %s", e, text)
        bot.send("⚠️ Das hat nicht geklappt. Versuch es später mit /feedback nochmal.")
        return
    bot.send("Danke für dein Feedback! 🙏 Es ist angekommen.")


def _after_setup(cfg: dict, store, bot, poll_fn, sources_fn) -> None:
    if poll_fn:
        with busy_with(store, "first search after setup"):
            _first_poll(cfg, store, bot, poll_fn)
    sources_fn()


def handle_update(upd: dict, cfg: dict, store, bot, report_fn=claude.deep_report, background=_thread,
                  interview_fn=None, count_fn=count_matches, poll_fn=None, find_sources_fn=claude.find_sources,
                  check_feed_fn=check_feed, feedback_fn=feedback.send) -> None:
    poll_fn = poll_fn or locked_poll
    interview_fn = interview_fn or _interview_turn(cfg, store)
    sources = lambda: suggest_sources(cfg, store, bot, find_sources_fn, check_feed_fn)
    cb = upd.get("callback_query")
    if not cb:
        text = upd.get("message", {}).get("text", "")
        command = text.strip().split()[0] if text.strip().startswith("/") else ""
        if command and store.get_state("feedback"):  # another command drops a waiting /feedback
            store.set_state("feedback", "")
        if command == "/feedback":
            words = text.strip().split(None, 1)
            if len(words) > 1:
                _send_feedback(cfg, bot, feedback_fn, words[1].strip())
            else:
                store.set_state("feedback", "waiting")
                bot.send("Schreib mir jetzt dein Feedback in einer Nachricht.")
            return
        if not command and store.get_state("feedback") == "waiting":
            store.set_state("feedback", "")
            _send_feedback(cfg, bot, feedback_fn, text.strip())
            return
        if command == "/setup" or (command == "/start" and not has_profile(cfg)):
            store.interview_clear()
            store.set_state("interview", "")
            store.set_state("interview_proposal", "")
            _interview_step(cfg, store, bot, interview_fn, None)
            return
        if command == "/abbrechen":
            _interview_cancel(cfg, store, bot)
            return
        if command == "/profile":
            bot.send(html.escape(profile_text(cfg)) if has_profile(cfg)
                     else "Noch kein Profil. Schick /setup, um eins anzulegen.")
            return
        if command == "/quellen":
            _list_sources(cfg, store, bot)
            background(sources)
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
            bot.send("🔎 Ich recherchiere zu dieser Stelle, der Bericht kommt in ein paar Minuten.")
            background(lambda: run_report(cfg, store, bot, p, report_fn))
        else:
            bot.send("In deiner Nachricht habe ich keinen Link zu einer Stelle gefunden.\n\n" + HELP)
        return
    action, _, raw_id = cb.get("data", "").partition(":")
    if action == "i":
        _answer(bot, cb["id"], "")
        if raw_id == "ok":
            _interview_confirm(cfg, store, bot, count_fn, lambda poll_first: background(
                lambda: _after_setup(cfg, store, bot, poll_fn if poll_first else None, sources)))
        elif raw_id == "edit":
            store.set_state("interview", "active")
            store.interview_add("bot", "Was soll ich ändern?")
            bot.send("✏️ Was soll ich ändern?")
        return
    if action == "q":
        _source_button(cfg, store, bot, cb, raw_id)
        return
    p = store.get(int(raw_id)) if raw_id.isdigit() else None
    if p is None:
        _answer(bot, cb["id"], "Unbekannte Stelle")
        return
    if action == "s":
        store.set_status(p.id, "skipped")
        _answer(bot, cb["id"], "Übersprungen")
    elif action == "a":
        store.set_status(p.id, "applied")
        append_tracker(cfg, p)
        _answer(bot, cb["id"], "Als beworben gemerkt")
    elif action == "r":
        store.set_status(p.id, "interesting")
        _answer(bot, cb["id"], "Ich recherchiere, das dauert ein paar Minuten")
        background(lambda: run_report(cfg, store, bot, p, report_fn))
    else:
        _answer(bot, cb["id"], "Unbekannte Aktion")


def resume_reports(cfg: dict, store, bot, background=_thread) -> None:
    """Restart reports that a service restart interrupted."""
    for p in store.by_status("interesting"):
        log.info("resuming report for %s", p.id)
        background(lambda p=p: run_report(cfg, store, bot, p))


class Registry:
    """The instances whose chat is known, each with its config, store and a bot bound to its chat."""

    def __init__(self, token: str, make_bot=Bot, open_store=Store):
        self.token, self.make_bot, self.open_store = token, make_bot, open_store
        self.stores, self.by_chat = {}, {}
        self.reload()

    def reload(self) -> None:
        by_chat = {}
        for name in config.instances():
            try:
                chat = config.instance_env(name).get("TELEGRAM_CHAT_ID")
                if not chat:
                    continue  # invite not redeemed yet
                cfg = config.load_instance(name)
                if name not in self.stores:
                    self.stores[name] = self.open_store(Path(cfg["db_path"]))
            except Exception:  # one broken config must not cut off everyone else
                log.exception("skipping instance %s", name)
                continue
            by_chat[chat] = (cfg, self.stores[name], self.make_bot(self.token, chat))
        self.by_chat = by_chat

    def get(self, chat: str):
        return self.by_chat.get(chat)

    def all(self) -> list:
        return sorted(self.by_chat.values(), key=lambda inst: inst[0]["instance"])

    def close(self) -> None:
        for store in self.stores.values():
            store.close()


def _chat_of(upd: dict) -> str:
    msg = upd.get("message") or upd.get("callback_query", {}).get("message") or {}
    return str(msg.get("chat", {}).get("id", ""))


class ChatWorkers:
    """One worker thread per chat: a chat's updates run in order, a slow one never blocks other chats."""

    def __init__(self):
        self.queues = {}

    def submit(self, chat: str, fn) -> None:
        if chat not in self.queues:
            self.queues[chat] = queue.Queue()
            threading.Thread(target=self._work, args=(self.queues[chat],), daemon=True).start()
        self.queues[chat].put(fn)
        waiting = self.queues[chat].qsize()
        if waiting > 1:  # the next log lines of this chat explain the wait
            log.info("%d updates waiting for one chat", waiting)

    @staticmethod
    def _work(jobs: queue.Queue) -> None:
        while True:
            fn = jobs.get()
            try:
                fn()
            except Exception:
                log.exception("update failed")
            finally:
                jobs.task_done()

    def join(self) -> None:
        for jobs in list(self.queues.values()):
            jobs.join()


def _kind(upd: dict) -> str:
    """What an update is, for the log: never message text or a chat id."""
    cb = upd.get("callback_query")
    if cb:
        return "button " + re.sub(r"[^\w:]", "?", str(cb.get("data", "")))[:20]
    text = upd.get("message", {}).get("text", "").strip()
    return re.sub(r"[^\w/]", "?", text.split()[0])[:20] if text.startswith("/") else "message"


def _age(upd: dict) -> str:
    """Seconds since the message was sent; a button carries no date, so its card's age is a hint."""
    if "callback_query" in upd:
        sent, what = upd["callback_query"].get("message", {}).get("date"), "card sent"
    else:
        sent, what = upd.get("message", {}).get("date"), "sent"
    return f", {what} {int(time.time()) - sent} s ago" if isinstance(sent, int) else ""


def dispatch(upd: dict, registry: Registry, handle=handle_update, run=lambda chat, fn: fn()) -> None:
    """Route an update to its person's instance; a stranger needs a valid invite link.

    `run(chat, fn)` executes the handling; `listen` passes ChatWorkers.submit."""
    chat = _chat_of(upd)
    inst = registry.get(chat)
    match = re.fullmatch(r"/start\s+(\S+)", upd.get("message", {}).get("text", "").strip())
    name = config.redeem_invite(match.group(1)) if match else None
    if inst is None and name is None:
        log.info("ignoring %s from an unknown chat", _kind(upd).split()[0])
        if chat and "message" in upd:  # silence would look like a hanging bot
            stranger = registry.make_bot(registry.token, chat)
            run(chat, lambda: stranger.send(NEEDS_INVITE))
        return
    if match and name is None:  # a bound chat opened an old link: don't start anything
        bot = inst[2]
        run(chat, lambda: bot.send(INVITE_USED))
        return
    moved = inst is not None and name is not None and inst[0]["instance"] != name
    if moved:
        config.set_env_value(config.config_dir(inst[0]["instance"]) / "env", "TELEGRAM_CHAT_ID", "")
    if name is not None and (inst is None or moved):
        config.set_env_value(config.config_dir(name) / "env", "TELEGRAM_CHAT_ID", chat)
        registry.reload()
        inst = registry.get(chat)
        changes.mark_seen(inst[1], changes.load())
        log.info("a chat %s instance %s", "moved to" if moved else "joined as", name)
        if moved:
            bot = inst[2]
            run(chat, lambda: bot.send(CHAT_MOVED))
    if match:
        upd = {**upd, "message": {**upd["message"], "text": "/start"}}
    cfg, store, bot = inst
    name, kind, received = cfg.get("instance", "?"), _kind(upd), time.monotonic()
    log.info("%s: %s%s", name, kind, _age(upd))

    def timed():
        started = time.monotonic()
        store.set_state("last_update", str(time.time()))
        try:
            handle(upd, cfg, store, bot)
        finally:
            store.set_state("last_update", str(time.time()))
            log.info("%s: %s handled in %.1f s, waited %.1f s", name, kind,
                     time.monotonic() - started, started - received)

    run(chat, timed)


def listen(registry: Registry, receiver, stop=lambda: False, handle=dispatch) -> None:
    """One long-polling loop for the shared bot; `receiver` is a Bot without a chat."""
    log.info("listening for Telegram updates")
    try:
        receiver.set_commands(COMMANDS)  # the "/" menu in the chat
    except Exception as e:
        log.warning("could not register commands: %s", e)
    news = changes.load()
    for cfg, store, bot in registry.all():
        resume_reports(cfg, store, bot)
        changes.announce(store, bot, news)
    workers = ChatWorkers()
    while not stop():
        try:
            batch = receiver.updates(wait=50)
        except Exception as e:  # network hiccups: keep the service alive
            log.warning("could not fetch updates: %s", e)
            threading.Event().wait(10)
            continue
        for upd in batch:
            try:
                handle(upd, registry, run=workers.submit)
            except Exception:
                log.exception("update %s failed", upd.get("update_id"))
