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

from . import changes, claude, config, feedback, interview, schedule, slot
from .filter import matches
from .http import get_json, get_text
from .model import Posting
from .sources import REGISTRY, feeds, pages
from .store import PollLock, PollLocked, Store
from .telegram import Bot, posting_card
from .text import days_since, markdown_to_html, posted_ago

log = logging.getLogger("job-radar")
BATCH = 10
TYPING_EVERY_S = 4  # Telegram hides "typing..." after about 5 s
THINKING = "⏳ Moment, ich denke nach …"
COMMANDS = [("help", "Was dieser Bot kann"), ("setup", "Jobsuche einrichten oder ändern"),
            ("profile", "Dein Profil anzeigen"),
            ("recherche", "Eine Stelle prüfen, die du selbst gefunden hast"),
            ("merkliste", "Vorgemerkte Stellen und Bewerbungen"),
            ("abbrechen", "Laufende Einrichtung abbrechen"), ("suchen", "Jetzt nach neuen Stellen suchen"),
            ("zeiten", "Wann ich nach neuen Stellen suche"),
            ("zeitraum", "Wie alt Stellen höchstens sein dürfen"),
            ("quellen", "Jobbörsen aus deinem Fachgebiet"),
            ("usage", "KI-Nutzung heute, 7 und 30 Tage"), ("feedback", "Lob, Kritik oder Ideen schicken")]
ADMIN_COMMANDS = [("einladen", "Einladungslink für eine neue Person"), ("einladungen", "Offene Einladungen")]
HELP = """<b>Job-Radar</b>
Neue passende Stellen kommen 3× am Tag (9, 13 und 18 Uhr), höchstens 5 auf einmal. Mit /zeiten stellst du das um.

<b>Knöpfe an jeder Stelle</b>
🔎 Recherche – Stelle und Arbeitgeber genau prüfen, der Bericht kommt nach ein paar Minuten
⭐ Vormerken – für später merken; alle findest du unter /merkliste

<b>Selbst eine Stelle gefunden?</b>
Schick mir einfach den Link (oder /recherche). Ich prüfe Stelle und Arbeitgeber, und mit ⭐ Vormerken unter dem Bericht kommt sie auf deine /merkliste.

<b>Befehle</b>
/recherche – eine Stelle prüfen, die du selbst gefunden hast
/setup – Jobsuche einrichten oder ändern (ein kurzes Gespräch)
/abbrechen – eine laufende Einrichtung abbrechen
/profile – dein Profil anzeigen
/merkliste – vorgemerkte Stellen, dort auch „beworben“; ich frage nach 14 Tagen nach
/suchen – jetzt gleich nach neuen Stellen suchen
/zeiten – wann ich nach neuen Stellen suche
/zeitraum – wie alt Stellen höchstens sein dürfen
/quellen – deine Jobbörsen anzeigen, entfernen oder neue suchen
/usage – KI-Nutzung heute, 7 und 30 Tage
/feedback – Lob, Kritik oder Ideen zum Bot schicken
/help – diese Hilfe"""


MAX_TEXT = 2000  # characters of one chat message that may reach Claude
LIMITS = {"report": (15, 86400), "quellen": (3, 86400), "interview": (100, 86400)}  # calls, seconds
LIMIT_TEXT = {"report": "Du hast heute schon {n} Recherchen bekommen. Morgen geht es weiter.",
              "quellen": "Nach Jobbörsen habe ich heute schon {n}× gesucht. Morgen geht es wieder.",
              "interview": "Wir haben heute schon sehr viel geschrieben. Morgen geht es weiter."}


def _allowed(store, kind: str) -> bool:
    """Count one call of `kind`; False (nothing counted) once the person reached the day's limit."""
    limit, window = LIMITS[kind]
    now = time.time()
    try:
        stamps = [t for t in json.loads(store.get_state(f"rate:{kind}") or "[]") if now - t < window]
    except (ValueError, TypeError):
        stamps = []
    if len(stamps) >= limit:
        log.info("limit reached: %s", kind)
        return False
    store.set_state(f"rate:{kind}", json.dumps(stamps + [now]))
    return True


def _limit_text(kind: str) -> str:
    return LIMIT_TEXT[kind].format(n=LIMITS[kind][0])


def has_profile(cfg: dict) -> bool:
    return any(Path(f).exists() for f in cfg.get("profile_files", []))


def profile_text(cfg: dict) -> str:
    parts = [Path(f).read_text(encoding="utf-8") for f in cfg.get("profile_files", []) if Path(f).exists()]
    return "\n\n".join(parts) or "(no profile configured: rate only on what the posting itself says)"


def fetch_all(cfg: dict, registry=REGISTRY, http_get=get_json, on_error=None, on_usage=None,
              extract_fn=claude.extract_postings) -> list[Posting]:
    """New postings of all sources. Boards without a feed need a model call per changed page,
    so they are read only when the call can be recorded (`on_usage`), i.e. in a poll."""
    postings = []
    for name, scfg in cfg["sources"].items():
        if not scfg.get("enabled", True) or name not in registry:
            continue
        extra = {}
        if name == "pages":
            model = cfg["triage"]["model"]
            extra = {"extract": (lambda page: extract_fn(page, model=model, on_usage=on_usage)) if on_usage else None,
                     "cache_path": Path(cfg["db_path"]).with_name("pages-cache.json")}
        try:
            got = registry[name].fetch(scfg, http_get, **extra)
            log.info("source %s: %d postings", name, len(got))
            postings += got
        except Exception as e:  # one broken portal must not stop the run
            log.warning("source %s failed: %s", name, e)
            if on_error:
                on_error(f"{name}: {e}")
    return postings


def enrich_postings(postings: list[Posting], registry=REGISTRY, http_get=get_json) -> None:
    """Lets a source add text a posting lacks (the BA search gives only title and occupation)."""
    for p in postings:
        enrich = getattr(registry.get(p.source), "enrich", None)
        if not enrich:
            continue
        try:
            enrich(p, http_get)
        except Exception as e:  # without details the posting is still rated, just on less
            log.warning("details for a %s posting failed: %s", p.source, e)


NEEDS_INVITE = ("🔒 Dieser Bot ist privat. Du brauchst einen Einladungslink von der Person, die ihn betreibt. "
                "Jeder Link funktioniert nur einmal.")
INVITE_USED = "Dieser Einladungslink ist nicht mehr gültig. Deine Jobsuche hier läuft weiter wie bisher."
CHAT_MOVED = ("🔁 Dieser Chat gehört jetzt zur Jobsuche aus dem neuen Einladungslink. "
              "Die bisherige Jobsuche schickt dir keine Nachrichten mehr.")
SLOT_TAKEN = "Gerade richtet jemand anderes sein Job-Radar ein. Versuch es bitte in ein paar Minuten noch einmal."
SLOT_LOST = ("Deine Einrichtung lag über 30 Minuten still, und inzwischen richtet jemand anderes sein Job-Radar ein. "
             "Schick /setup, um neu anzufangen, sobald du Zeit hast.")
ASK_LINK = ("Schick mir jetzt den Link zur Stellenanzeige, zum Beispiel aus dem Browser oder einer anderen Job-App. "
            "Ich prüfe Stelle und Arbeitgeber, das dauert ein paar Minuten.")
NO_PROFILE ="👋 Deine Jobsuche ist noch nicht eingerichtet. Schick /setup, dann stelle ich dir ein paar Fragen."


def poll(cfg: dict, store, bot, fetch=None, triage_fn=claude.triage, dry_run: bool = False,
         enrich=enrich_postings, employers=None) -> dict:
    if not has_profile(cfg):
        if not dry_run and not store.get_state("setup_nudged"):
            bot.send(NO_PROFILE)
            store.set_state("setup_nudged", "1")
        return {"skipped": "no profile"}
    fetched = fetch(cfg) if fetch else fetch_all(cfg, on_usage=lambda u: store.record_usage("pages", u))
    if not dry_run:  # a web search: only in a poll that sends what it finds
        fetched += (employers or due_employers)(cfg, store)
    new = store.add_new(fetched)
    matched = []
    for p in new:
        if p.source == "initiativ" or matches(p, cfg["filter"]):  # found by search terms and region already
            matched.append(p)
        else:
            store.set_status(p.id, "filtered")

    # Matches wait as "pending" until scored, so a failed or capped triage run
    # retries them next time instead of sending them unscored.
    for p in matched:
        store.set_status(p.id, "pending")
    queue = store.by_status("pending")

    tcfg = cfg["triage"]
    likes, dislikes = store.liked(10), store.disliked(10)  # what the person marked before (#26)
    stats = {"triage_calls": 0}  # for measuring the AI usage per poll (#17)
    if not tcfg.get("enabled", True):
        candidates = queue
    else:
        profile = profile_text(cfg)
        to_score = queue[: tcfg.get("max_per_run", 60)]
        before = [x.text for x in to_score]
        enrich(to_score)
        for x, old in zip(to_score, before):
            if x.text != old:
                store.set_text(x.id, x.text)
        for i in range(0, len(to_score), BATCH):
            batch = to_score[i:i + BATCH]
            stats["triage_calls"] += 1
            try:
                scores = triage_fn(batch, profile=profile, model=tcfg["model"], likes=likes, dislikes=dislikes,
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
    if not dry_run:
        ask_about_applications(store, bot)
    scored = sum(p.score is not None for p in queue) if tcfg.get("enabled", True) else 0
    return {"fetched": len(fetched), "new": len(new), "matched": len(matched), "notified": len(candidates),
            **stats, "scored": scored}


FOLLOW_UP_DAYS = 14
OUTCOMES = {"gespraech": "📅 Gespräch", "absage": "❌ Absage"}


def ask_about_applications(store, bot) -> None:
    """Two weeks after "beworben" ask how it went; no answer means asking again two weeks later."""
    for p, mark in store.due_reminders():
        bot.send(f"Hast du von <b>{html.escape(p.title)}</b> ({html.escape(p.company or '?')}) etwas gehört? "
                 f"Beworben am {_date(mark['at'])}.",
                 [[(OUTCOMES["gespraech"], f"o:gespraech:{p.id}"), (OUTCOMES["absage"], f"o:absage:{p.id}"),
                   ("⏳ Noch nichts", f"o:wait:{p.id}")]])
        store.remind_later(p.id, FOLLOW_UP_DAYS)


def _date(sql_ts: str) -> str:
    return datetime.strptime(sql_ts[:10], "%Y-%m-%d").strftime("%d.%m.") if sql_ts else "?"


def _link(p: Posting) -> str:
    title = html.escape(p.title)
    return f'<a href="{html.escape(p.url, quote=True)}">{title}</a>' if re.match(r"https?://", p.url or "") else title


def _online(p: Posting, mark: dict) -> str:
    """How long a posting is online, or else since when the radar knows it (#33)."""
    today = date.today()
    since = posted_ago(p.posted_at, today)
    if since:
        return f"📅 online {since}"
    found = mark.get("first_seen") or ""  # UTC from SQLite
    days = days_since(found.replace(" ", "T") + "+00:00" if found else "", today)
    if days is None:
        return ""
    return "📅 heute gefunden" if days == 0 else "📅 gestern gefunden" if days == 1 else f"📅 vor {days} Tagen gefunden"


def _entry(p: Posting, mark: dict) -> str:
    where = f" · {html.escape(p.location)}" if p.location else ""
    lines = [f"<b>{_link(p)}</b>", html.escape(p.company or "?") + where]
    online = _online(p, mark)
    if online:
        lines.append(online)
    return "\n".join(lines)


def merkliste(store, bot) -> None:
    """Applications in one message, then each bookmark in its own with ✅ beworben and 🗑 entfernen (#33)."""
    saved, applied = store.marked("saved", 20), store.marked("applied", 20)
    if not saved and not applied:
        bot.send("Noch nichts vorgemerkt. Tippe bei einer Stelle auf ⭐ Vormerken, dann findest du sie hier wieder.")
        return
    if applied:
        lines = ["<b>✅ Beworben</b>"]
        for p, mark in applied:
            state = OUTCOMES.get(mark["outcome"], "⏳ wartet auf Antwort")
            lines.append(f"• {_link(p)} – {html.escape(p.company or '?')}, am {_date(mark['at'])} · {state}")
        bot.send("\n".join(lines))
    if saved:
        total = store.count_marked("saved")
        shown = f"die neuesten {len(saved)} von {total}" if total > len(saved) else f"{total}, die neueste zuerst"
        bot.send(f"<b>⭐ Vorgemerkt</b> ({shown}):")
    for p, mark in saved:
        bot.send(_entry(p, mark), [[("✅ beworben", f"e:a:{p.id}"), ("🗑 entfernen", f"e:x:{p.id}")]])


def _mark_button(cfg: dict, store, bot, cb: dict, action: str, raw: str) -> None:
    """v: bookmark from a card; e:a / e:x apply or remove one /merkliste entry and update its message;
    m:a / m:x the same on the old list in one message; o: outcome of an application;
    s / a: the old cards' skip and applied buttons."""
    what, _, pid = raw.rpartition(":") if action in ("m", "e", "o") else ("", "", raw)
    p = store.get(int(pid)) if pid.isdigit() else None
    if p is None:
        _answer(bot, cb["id"], "Unbekannte Stelle")
        return
    if action == "e" and store.mark_of(p.id) != "saved":  # an older /merkliste message; marked since
        _answer(bot, cb["id"], "Schon als beworben gemerkt – siehe /merkliste" if store.mark_of(p.id) == "applied"
                else "Schon entfernt")
    elif action == "v" and store.mark_of(p.id) == "applied":  # a late tap on an old card or report
        _answer(bot, cb["id"], "Schon als beworben gemerkt – siehe /merkliste")
    elif action == "v":
        store.mark(p.id, "saved")
        _answer(bot, cb["id"], "Vorgemerkt – alle findest du unter /merkliste")
    elif (action, what) in (("m", "a"), ("e", "a"), ("a", "")):
        store.mark(p.id, "applied", remind_in_days=FOLLOW_UP_DAYS)
        append_tracker(cfg, p)
        _answer(bot, cb["id"], f"Als beworben gemerkt. In {FOLLOW_UP_DAYS} Tagen frage ich nach.")
        if action == "e":
            _edit_entry(bot, cb, f"{_entry(p, {'first_seen': store.first_seen(p.id)})}\n✅ beworben – in {FOLLOW_UP_DAYS} Tagen frage ich nach")
    elif (action, what) in (("m", "x"), ("e", "x"), ("s", "")):
        store.mark(p.id, "removed")
        _answer(bot, cb["id"], "Entfernt")
        if action == "e":
            _edit_entry(bot, cb, f"🗑 <s>{html.escape(p.title)}</s> – entfernt")
    elif action == "o" and what == "wait":
        store.remind_later(p.id, FOLLOW_UP_DAYS)
        _answer(bot, cb["id"], f"Okay, ich frage in {FOLLOW_UP_DAYS} Tagen nochmal.")
    elif action == "o" and what in OUTCOMES:
        store.set_outcome(p.id, what)
        _answer(bot, cb["id"], "Viel Erfolg! 🍀" if what == "gespraech" else "Schade. Weiter geht's!")
    else:
        _answer(bot, cb["id"], "Unbekannte Aktion")


def _edit_entry(bot, cb: dict, text: str) -> None:
    """Show the new state in the tapped /merkliste entry and drop its buttons; the mark is saved anyway."""
    message_id = cb.get("message", {}).get("message_id")
    if not message_id:
        return
    try:
        bot.edit(message_id, text)
    except Exception as e:
        log.warning("could not update the list entry: %s", e)


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
    bot.send_document(path, caption=f"<b>{html.escape(f'{p.company} - {p.title}', quote=False)}</b>\n\n"
                                    + markdown_to_html(_tldr(md)), buttons=[[("⭐ Vormerken", f"v:{p.id}")]])


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
    # Daemon: a stopping listener does not wait for a report; reports resume after the restart.
    threading.Thread(target=fn, daemon=True).start()


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
    if not _allowed(store, "interview"):
        bot.send(_limit_text("interview"))
        return
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
    bot.send(f"<b>So würde ich für dich suchen:</b>\n\n{markdown_to_html(reply['summary'])}",
             [[("✅ Passt so", "i:ok"), ("✏️ Ändern", "i:edit")]])


def _owner(cfg: dict) -> str:
    return cfg.get("instance") or cfg["config_dir"]


def _keep_slot(cfg: dict, store, bot) -> bool:
    """Refresh this person's interview slot; if someone else took it meanwhile, end the interview."""
    if slot.take(_owner(cfg)):
        return True
    store.interview_clear()
    store.set_state("interview", "")
    store.set_state("interview_proposal", "")
    bot.send(SLOT_LOST)
    return False


def _interview_cancel(cfg: dict, store, bot) -> None:
    if not store.get_state("interview"):
        bot.send("Es läuft gerade keine Einrichtung. Mit /setup fängst du eine an.")
        return
    store.interview_clear()
    store.set_state("interview", "")
    store.set_state("interview_proposal", "")
    slot.release(_owner(cfg))
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
    for kind in ("feeds", "pages"):  # boards the person added stay; the interview does not know them
        if _boards(cfg, kind):
            search.setdefault("sources", {})[kind] = {kind: _boards(cfg, kind)}
    if "schedule" in _search(cfg):  # so do the poll times from /zeiten
        search["schedule"] = _search(cfg)["schedule"]
    if _saved_age(cfg):  # and the posting age from /zeitraum
        search.setdefault("sources", {}).setdefault("ba", {})["published_since_days"] = _saved_age(cfg)
    config.write_search(cdir / "search.json", search)
    (cdir / "profile.md").write_text(reply["profile_md"].strip() + "\n", encoding="utf-8")
    store.interview_clear()
    store.set_state("interview", "")
    store.set_state("interview_proposal", "")
    store.set_state("setup_nudged", "")
    slot.release(_owner(cfg))
    bot.send("✅ Gespeichert. Ich schaue, wie viele aktuelle Stellen passen …")
    try:
        n = count_fn(cfg)
    except Exception as e:
        log.warning("match count failed: %s", e)
        bot.send("Neue passende Stellen kommen ab der nächsten Suche hier an.")
        follow_up(False)
        return
    days = _days_back(cfg)
    if n:
        bot.send(f"{n} Stellen aus den letzten {days} Tagen passen zu deiner Suche. Vorher schaue ich noch nach "
                 "Jobbörsen aus deinem Fachgebiet, dann bewertet die KI die Stellen. Die besten schicke ich dir hier.")
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
        bot.send("Das Bewerten hat gerade nicht geklappt. Ich versuche es bei der nächsten Suche wieder.")
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


def _boards(cfg: dict, kind: str = "feeds") -> list[dict]:
    """The person's own job boards ("feeds", or "pages" without a feed), as saved now
    (the listener's cfg may be older)."""
    return _search(cfg).get("sources", {}).get(kind, {}).get(kind, [])


def _feeds(cfg: dict) -> list[dict]:
    return _boards(cfg, "feeds")


def _save_boards(cfg: dict, kind: str, items: list[dict]) -> None:
    search = _search(cfg)
    search.setdefault("sources", {})[kind] = {kind: items}
    config.write_search(_search_path(cfg), search)


def check_page(url: str, store, model: str, get=get_text, extract_fn=claude.extract_postings) -> int:
    """Postings a model can read off a board's list page; raises if the page does not load."""
    return len(pages.read(get(url), url, "check",
                          lambda page: extract_fn(page, model=model, on_usage=lambda u: store.record_usage("pages", u))))


def check_feed(url: str, get=get_text) -> int:
    """Entries in the feed; raises if it does not load or is no RSS or Atom feed."""
    return len(feeds.parse(get(url), "check"))


def search_field(cfg: dict) -> str:
    """What the board search may know: the person's search terms and region, never the profile
    (it fetches web pages, which could ask it to send what it knows somewhere)."""
    search = _search(cfg)
    ba, words = search.get("sources", {}).get("ba", {}), search.get("filter", {})
    terms = list(dict.fromkeys(ba.get("keywords", []) + words.get("include", [])))[:30]
    places = [f"{a['where']} ({a.get('radius_km', '?')} km)" for a in ba.get("areas", []) if a.get("where")]
    places += [w for w in words.get("locations", []) if not any(p.startswith(w + " (") for p in places)]
    return f"Search terms: {', '.join(terms) or 'unknown'}\nRegion: {', '.join(places[:15]) or 'all of Germany'}"


def suggest_sources(cfg: dict, store, bot, find_fn=claude.find_sources, check_fn=check_feed,
                    check_page_fn=None) -> int:
    """Let Claude look for job boards of the person's field and offer the ones that can be read:
    a feed that loads, or a list page a model can read postings off. Returns how many it offered."""
    check_page_fn = check_page_fn or (lambda url: check_page(url, store, cfg["triage"]["model"]))
    if not _allowed(store, "quellen"):
        bot.send(_limit_text("quellen"))
        return 0
    with busy_with(store, "/quellen search"):
        return _suggest_sources(cfg, store, bot, find_fn, check_fn, check_page_fn)


SEARCH_WITH_BOARDS = ("Nimm die passenden Jobbörsen auf und tippe dann auf 🔎 Jetzt suchen, "
                      "dann schaue ich gleich auch dort. Sonst suche ich zur nächsten gewohnten Zeit.")


def _suggest_sources(cfg: dict, store, bot, find_fn, check_fn, check_page_fn) -> int:
    known = [f["url"] for f in _boards(cfg, "feeds") + _boards(cfg, "pages")]
    bot.send("🔎 Ich suche jetzt nach Jobbörsen aus deinem Fachgebiet, die die großen Portale nicht abdecken. "
             "Das dauert ein paar Minuten.")
    try:
        found = find_fn(field=search_field(cfg), known=known, model=cfg["report"]["model"],
                        on_usage=lambda u: store.record_usage("sources", u))
    except Exception as e:
        log.warning("source search failed: %s", e)
        bot.send("⚠️ Die Suche nach Jobbörsen hat nicht geklappt. Versuch es später mit /quellen nochmal.")
        return 0
    offers = []
    for item in found:
        if item["url"] in known or any(o["url"] == item["url"] for o in offers):
            continue
        try:
            if (check_page_fn if item.get("kind") == "page" else check_fn)(item["url"]):
                offers.append(item)
        except Exception as e:  # Claude can be wrong about a board; only offer what can be read
            log.info("board %s cannot be read: %s", item["url"], e)
    store.set_state("sources_offer", json.dumps(offers))
    if not offers:
        bot.send("Ich habe gerade keine weitere passende Jobbörse gefunden, die ich automatisch lesen kann.")
        return 0
    bot.send(f"Diese {len(offers)} Jobbörsen passen zu dir. Soll ich dort auch nach Stellen schauen?")
    for i, item in enumerate(offers):
        no_feed = "\n<i>ohne Feed: die KI liest die Seite bei jeder Suche</i>" if item.get("kind") == "page" else ""
        bot.send(f"<b>{html.escape(item['name'])}</b>\n{html.escape(item['why'])}\n{html.escape(item['url'])}{no_feed}",
                 [[("➕ Aufnehmen", f"q:add:{i}"), ("Nein", f"q:no:{i}")]])
    bot.send(SEARCH_WITH_BOARDS, [[("🔎 Jetzt suchen", "q:go")]])
    return len(offers)


PORTALS = {"ba": "Bundesagentur für Arbeit", "arbeitnow": "Arbeitnow"}  # searched for everyone


def _list_sources(cfg: dict, store, bot) -> None:
    current = [{**f, "kind": "feed"} for f in _boards(cfg, "feeds")] + [{**f, "kind": "page"} for f in _boards(cfg, "pages")]
    store.set_state("sources_shown", json.dumps(current))
    search = [("🔎 Neue Jobbörsen suchen", "q:find")]
    portals = [label for key, label in PORTALS.items() if cfg["sources"].get(key, {}).get("enabled", True)]
    always = f"Ich suche immer in: {', '.join(portals)}." if portals else ""
    if cfg["sources"].get("initiativ", {}).get("enabled", True):
        always = (always + " Einmal pro Woche suche ich außerdem nach Arbeitgebern aus deinem Fachgebiet, "
                  "die Initiativbewerbungen annehmen.").strip()
    if not current:
        bot.send(f"{always}\n\nZusätzliche Jobbörsen aus deinem Fachgebiet hast du noch keine. Soll ich danach "
                 "suchen? Das dauert ein paar Minuten.", [search])
        return
    names = "\n".join(f"• {html.escape(f['name'])}" + (" (ohne Feed)" if f["kind"] == "page" else "")
                      for f in current)
    bot.send(f"{always}\n\n<b>Dazu deine {len(current)} zusätzlichen Jobbörsen:</b>\n{names}\n\n"
             "Tippe auf eine, um sie zu entfernen, oder lass mich nach weiteren suchen.",
             [[(f"🗑 {f['name']}", f"q:del:{i}")] for i, f in enumerate(current)] + [search])


def _source_button(cfg: dict, store, bot, cb: dict, arg: str) -> None:
    what, _, raw = arg.partition(":")
    key = {"add": "sources_offer", "del": "sources_shown"}.get(what)
    items = json.loads(store.get_state(key) or "[]") if key else []
    item = items[int(raw)] if raw.isdigit() and int(raw) < len(items) else None
    if item is None:
        _answer(bot, cb["id"], "Okay" if what == "no" else "Veraltet, schick /quellen nochmal")
        return
    kind = "pages" if item.get("kind") == "page" else "feeds"
    current = _boards(cfg, kind)
    if what == "add":
        if all(f["url"] != item["url"] for f in current):
            _save_boards(cfg, kind, current + [{"name": item["name"], "url": item["url"]}])
        _answer(bot, cb["id"], "Aufgenommen")
        bot.send(f"✅ {html.escape(item['name'])} ist jetzt in deiner Suche. Ab dem nächsten Durchlauf schaue ich dort auch.")
    else:
        if any(f["url"] == item["url"] for f in current):
            _save_boards(cfg, kind, [f for f in current if f["url"] != item["url"]])
        _answer(bot, cb["id"], "Entfernt")


def check_employer(url: str, get=get_text) -> bool:
    """Whether the careers page loads and speaks of unsolicited applications; raises if it does not load."""
    return "initiativ" in get(url).lower()


def due_employers(cfg: dict, store, find_fn=claude.find_employers, check_fn=check_employer) -> list[Posting]:
    """Employers of the person's field and region that welcome unsolicited applications (#31), as
    postings for the poll. The web search costs about as much as a report, so it runs at most every
    `every_days`; an employer once found is never offered again."""
    ecfg = cfg["sources"].get("initiativ", {})
    if not ecfg.get("enabled", True):
        return []
    every = ecfg.get("every_days", 7) * 86400
    last = store.get_state("initiativ_at")
    if last and time.time() - float(last) < every:
        return []
    known = store.companies("initiativ")
    try:
        found = find_fn(field=search_field(cfg), known=known, model=cfg["report"]["model"],
                        on_usage=lambda u: store.record_usage("employers", u))
    except Exception as e:
        log.warning("employer search failed, trying again tomorrow: %s", e)
        store.set_state("initiativ_at", str(time.time() - every + 86400))
        return []
    store.set_state("initiativ_at", str(time.time()))
    seen, postings = {name.casefold() for name in known}, []
    for item in found:
        if item["name"].casefold() in seen:
            continue
        try:
            if not check_fn(item["url"]):
                log.info("employer page %s does not mention unsolicited applications", item["url"])
                continue
        except Exception as e:  # Claude can be wrong about a page; only offer what loads
            log.info("employer page %s cannot be read: %s", item["url"], e)
            continue
        seen.add(item["name"].casefold())
        postings.append(Posting(
            source="initiativ", external_id=item["url"], url=item["url"], title="Initiativbewerbung",
            company=item["name"], location=item["location"], employer=item["about"],
            text=f"No job posting: {item['name']} welcomes unsolicited applications (Initiativbewerbung) "
                 f"on this careers page. {item['why']}"))
    return postings


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


SEARCH_NOW_COOLDOWN_S = 10 * 60  # /suchen must not burn the shared AI usage


def _search_now(cfg: dict, store, bot, poll_fn, background) -> None:
    if not has_profile(cfg):
        bot.send(NO_PROFILE)
        return
    last = store.get_state("search_now_at")
    wait = SEARCH_NOW_COOLDOWN_S - (time.time() - float(last)) if last else 0
    if wait > 0:
        bot.send(f"Du hast gerade erst gesucht. Mit /suchen geht es wieder in {int(wait // 60) + 1} Minuten.")
        return
    store.set_state("search_now_at", str(time.time()))
    bot.send("🔎 Ich suche jetzt nach neuen Stellen. Das kann ein paar Minuten dauern.")
    background(lambda: _run_search_now(cfg, store, bot, poll_fn))


def _run_search_now(cfg: dict, store, bot, poll_fn) -> None:
    try:
        with busy_with(store, "/suchen"):
            result = poll_fn(reload(cfg), store, bot)
    except PollLocked:
        store.set_state("search_now_at", "")  # nothing ran, so no cooldown
        bot.send("Die Suche läuft gerade schon. Neue Treffer kommen gleich hier an.")
        return
    except Exception as e:
        log.warning("search now failed: %s", e)
        store.set_state("search_now_at", "")  # a failed search may be retried at once
        bot.send("⚠️ Die Suche hat gerade nicht geklappt. Versuch es gleich nochmal mit /suchen.")
        return
    n = result.get("notified", 0)
    bot.send(f"Fertig: {n} neue passende Stelle{'n' if n != 1 else ''} oben." if n
             else "Fertig: nichts Neues, gerade passt keine neue Stelle.")


def _invite(bot) -> None:
    label = config.next_label("family")
    code = config.create_invite(label)
    bot.send(f"Einladung für <b>{label}</b>, der Link funktioniert einmal:\n"
             f"https://t.me/{bot.username()}?start={code}\n\n"
             "Sobald die Person ihn öffnet, richtet sie ihre Suche ein, und die regelmäßige Suche startet von selbst.")


def _list_invites(bot) -> None:
    names = config.open_invite_names()
    bot.send("Offene Einladungen: " + ", ".join(names) if names else "Keine offenen Einladungen.")


def _times(cfg: dict, store, bot, arg: str) -> None:
    if arg:
        try:
            _save_times(cfg, bot, schedule.parse(arg))
        except ValueError as e:
            bot.send(f"Das habe ich nicht verstanden. {e}")
        return
    current = _search(cfg).get("schedule") or schedule.DEFAULT
    labels = [(label, f"z:{key}") for key, (label, _) in schedule.PRESETS.items()]
    bot.send(f"Ich suche gerade {schedule.describe(current)} nach neuen Stellen. Wann soll ich suchen?\n\n"
             f"Eigene Uhrzeiten: z. B. /zeiten 8 12 17 (volle Stunden zwischen {schedule.FIRST_HOUR} und "
             f"{schedule.LAST_HOUR} Uhr, jeden Tag).", [labels[:2], labels[2:]])


def _save_times(cfg: dict, bot, plan: dict) -> None:
    search = _search(cfg)
    search["schedule"] = plan
    config.write_search(_search_path(cfg), search)
    bot.send(f"✅ Ab jetzt suche ich {schedule.describe(plan)} nach neuen Stellen.")


AGES = {7: "1 Woche", 28: "4 Wochen", 60: "2 Monate", 100: "3 Monate"}  # /zeitraum choices, in days


def _saved_age(cfg: dict) -> int | None:
    return _search(cfg).get("sources", {}).get("ba", {}).get("published_since_days")


def _days_back(cfg: dict) -> int:
    """How many days back the BA search goes, as saved now (the listener's cfg may be older)."""
    return _saved_age(cfg) or cfg["sources"].get("ba", {}).get("published_since_days", 28)


def _posting_age(cfg: dict, bot) -> None:
    bot.send(f"Bei der Bundesagentur für Arbeit suche ich Stellen der letzten {_days_back(cfg)} Tage. "
             "Wie alt dürfen Stellen höchstens sein?\n\n"
             "<i>Ältere Stellen sind oft schon vergeben, aber nicht immer.</i>",
             [[(label, f"t:{days}") for days, label in AGES.items()]])


def _save_age(cfg: dict, bot, days: int) -> None:
    search = _search(cfg)
    search.setdefault("sources", {}).setdefault("ba", {})["published_since_days"] = days
    config.write_search(_search_path(cfg), search)
    bot.send(f"✅ Ab jetzt suche ich Stellen der letzten {days} Tage. Ältere Stellen, die dazukommen, "
             "bekommst du bei der nächsten Suche – oder gleich mit /suchen.")


def _after_setup(cfg: dict, store, bot, poll_fn, sources_fn) -> None:
    """Boards first, so the first search can include the ones the person takes (#29): with
    boards on offer it waits for "Jetzt suchen", else it searches right away."""
    if sources_fn() or not poll_fn:
        return
    with busy_with(store, "first search after setup"):
        _first_poll(cfg, store, bot, poll_fn)


def handle_update(upd: dict, cfg: dict, store, bot, report_fn=claude.deep_report, background=_thread,
                  interview_fn=None, count_fn=count_matches, poll_fn=None, find_sources_fn=claude.find_sources,
                  check_feed_fn=check_feed, feedback_fn=feedback.send, check_page_fn=None) -> None:
    poll_fn = poll_fn or locked_poll
    interview_fn = interview_fn or _interview_turn(cfg, store)
    sources = lambda: suggest_sources(cfg, store, bot, find_sources_fn, check_feed_fn, check_page_fn)
    cb = upd.get("callback_query")
    if not cb:
        text = upd.get("message", {}).get("text", "")
        command = text.strip().split()[0] if text.strip().startswith("/") else ""
        if command and store.get_state("feedback"):  # another command drops a waiting /feedback
            store.set_state("feedback", "")
        if command and store.get_state("recherche"):  # ... or a waiting /recherche
            store.set_state("recherche", "")
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
            if not slot.take(_owner(cfg)):
                bot.send(SLOT_TAKEN)
                return
            store.interview_clear()
            store.set_state("interview", "")
            store.set_state("interview_proposal", "")
            _interview_step(cfg, store, bot, interview_fn, None)
            return
        if command == "/abbrechen":
            _interview_cancel(cfg, store, bot)
            return
        if command == "/profile":
            bot.send(markdown_to_html(profile_text(cfg)) if has_profile(cfg)
                     else "Noch kein Profil. Schick /setup, um eins anzulegen.")
            return
        if command in ("/einladen", "/einladungen") and cfg.get("instance") and cfg["instance"] == config.admin():
            (_invite if command == "/einladen" else _list_invites)(bot)
            return
        if command == "/merkliste":
            merkliste(store, bot)
            return
        if command == "/recherche" and not re.search(r"https?://", text):
            store.set_state("recherche", "waiting")
            bot.send(ASK_LINK)
            return
        if command == "/zeiten":
            _times(cfg, store, bot, text.strip()[len("/zeiten"):].strip())
            return
        if command == "/zeitraum":
            _posting_age(cfg, bot)
            return
        if command == "/suchen":
            _search_now(cfg, store, bot, poll_fn, background)
            return
        if command == "/quellen":  # only lists; the search costs AI, so it waits for the button
            _list_sources(cfg, store, bot)
            return
        asked_for_link = not command and store.get_state("recherche") == "waiting"
        if not asked_for_link and not command and store.get_state("interview") in ("active", "proposed"):
            if len(text) > MAX_TEXT:
                bot.send(f"Deine Nachricht ist sehr lang. Schreib sie bitte kürzer, höchstens {MAX_TEXT} Zeichen.")
                return
            if _keep_slot(cfg, store, bot):
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
        waiting = store.get_state("recherche") == "waiting"
        store.set_state("recherche", "")
        if link:
            if not _allowed(store, "report"):
                bot.send(_limit_text("report"))
                return
            p = posting_for_url(store, link.group(0)[:500])
            store.set_status(p.id, "interesting")
            bot.send("🔎 Ich recherchiere zu dieser Stelle, der Bericht kommt in ein paar Minuten. "
                     "Mit ⭐ Vormerken darunter kommt sie auf deine /merkliste.")
            background(lambda: run_report(cfg, store, bot, p, report_fn))
        elif waiting:
            bot.send("Darin habe ich keinen Link gefunden. Kopiere die Adresse der Stellenanzeige aus dem Browser "
                     "(sie beginnt mit https://) und schick sie mir einfach hier.")
        else:
            bot.send("In deiner Nachricht habe ich keinen Link zu einer Stelle gefunden.\n\n" + HELP)
        return
    action, _, raw_id = cb.get("data", "").partition(":")
    if action == "i":
        _answer(bot, cb["id"], "")
        if raw_id == "ok":
            _interview_confirm(cfg, store, bot, count_fn, lambda poll_first: background(
                lambda: _after_setup(cfg, store, bot, poll_fn if poll_first else None, sources)))
        elif raw_id == "edit" and _keep_slot(cfg, store, bot):
            store.set_state("interview", "active")
            store.interview_add("bot", "Was soll ich ändern?")
            bot.send("✏️ Was soll ich ändern?")
        return
    if action == "q" and raw_id == "go":
        _answer(bot, cb["id"], "")
        _search_now(cfg, store, bot, poll_fn, background)
        return
    if action == "q" and raw_id == "find":
        _answer(bot, cb["id"], "Ich suche")
        background(sources)
        return
    if action == "q":
        _source_button(cfg, store, bot, cb, raw_id)
        return
    if action == "t":
        days = int(raw_id) if raw_id.isdigit() and int(raw_id) in AGES else None
        _answer(bot, cb["id"], "Gespeichert" if days else "Veraltet, schick /zeitraum nochmal")
        if days:
            _save_age(cfg, bot, days)
        return
    if action == "z":
        preset = schedule.PRESETS.get(raw_id)
        _answer(bot, cb["id"], "Gespeichert" if preset else "Veraltet, schick /zeiten nochmal")
        if preset:
            _save_times(cfg, bot, preset[1])
        return
    if action in ("v", "m", "e", "o", "s", "a"):
        _mark_button(cfg, store, bot, cb, action, raw_id)
        return
    p = store.get(int(raw_id)) if raw_id.isdigit() else None
    if p is None:
        _answer(bot, cb["id"], "Unbekannte Stelle")
        return
    if action == "r":
        if not _allowed(store, "report"):
            _answer(bot, cb["id"], "Limit für heute erreicht")
            bot.send(_limit_text("report"))
            return
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

    def __init__(self, token: str, make_bot=Bot, open_store=Store, on_join=lambda name: None):
        """`on_join(name)` runs when an invite binds a chat, e.g. to start the instance's poll timer."""
        self.token, self.make_bot, self.open_store, self.on_join = token, make_bot, open_store, on_join
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

    def join(self, timeout: float | None = None) -> bool:
        """Wait until every queued job is done; False if the timeout ran out first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for jobs in list(self.queues.values()):
            with jobs.all_tasks_done:
                while jobs.unfinished_tasks:
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    jobs.all_tasks_done.wait(left)
        return True


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
        registry.on_join(name)
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


class Stopping(BaseException):
    """Raised in the listener's main thread on SIGTERM; BaseException so no `except Exception` swallows it."""


DRAIN_S = 60  # below systemd's default stop timeout of 90 s


def listen(registry: Registry, receiver, stop=lambda: False, handle=dispatch) -> None:
    """One long-polling loop for the shared bot; `receiver` is a Bot without a chat.

    On `Stopping` it takes no more updates and lets the chats' queued jobs finish."""
    log.info("listening for Telegram updates")
    workers = ChatWorkers()
    try:
        try:
            receiver.set_commands(COMMANDS)  # the "/" menu in the chat
            for cfg, _, bot in registry.all():
                if cfg["instance"] == config.admin():
                    receiver.set_commands(COMMANDS + ADMIN_COMMANDS, chat_id=bot.chat_id)
        except Exception as e:
            log.warning("could not register commands: %s", e)
        news = changes.load()
        for cfg, store, bot in registry.all():
            resume_reports(cfg, store, bot)
            # In the chat's queue: a slow send must not hold up everyone's updates.
            workers.submit(bot.chat_id, lambda store=store, bot=bot: changes.announce(store, bot, news))
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
    except Stopping:
        log.info("stopping: no new updates, finishing the queued ones")
    if not workers.join(timeout=DRAIN_S):
        log.warning("stopping with unfinished updates after %d s", DRAIN_S)
