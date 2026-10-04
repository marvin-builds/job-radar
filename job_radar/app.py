"""Orchestration: poll sources, triage, notify; react to Telegram buttons."""
from __future__ import annotations

import contextlib
import html
import json
import logging
import re
import queue
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path

from . import claude, config, interview
from .filter import matches
from .http import get_json
from .model import Posting
from .sources import REGISTRY
from .store import Store
from .telegram import Bot, posting_card

log = logging.getLogger("job-radar")
BATCH = 10
TYPING_EVERY_S = 4  # Telegram hides "typing..." after about 5 s
COMMANDS = [("help", "Was dieser Bot kann"), ("setup", "Jobsuche einrichten oder ändern"),
            ("profile", "Dein Profil anzeigen"), ("usage", "KI-Nutzung heute, 7 und 30 Tage")]
HELP = """<b>Job-Radar</b>
Neue passende Stellen kommen stündlich (Mo–Fr 7–21 Uhr, am Wochenende alle 2 Stunden), höchstens 5 auf einmal.

<b>Knöpfe an jeder Stelle</b>
🔎 Recherche – Stelle und Arbeitgeber genau prüfen, der Bericht kommt nach ein paar Minuten
👎 Überspringen – ausblenden
✅ Beworben – als beworben merken

<b>Nachrichten</b>
Link zu einer Stelle schicken – Recherche zu einer Stelle, die du selbst gefunden hast
/setup – Jobsuche einrichten oder ändern (ein kurzes Gespräch)
/profile – dein Profil anzeigen
/usage – KI-Nutzung heute, 7 und 30 Tage
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


NO_PROFILE = "👋 Deine Jobsuche ist noch nicht eingerichtet. Schick /setup, dann stelle ich dir ein paar Fragen."


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
def _typing(bot):
    """Show "typing..." until the block ends; a Claude call can take a minute."""
    done = threading.Event()

    def keep_typing():
        while not done.is_set():
            try:
                bot.typing()
            except Exception as e:  # "typing..." is cosmetic
                log.info("could not show typing: %s", e)
                return
            done.wait(TYPING_EVERY_S)

    t = threading.Thread(target=keep_typing, daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join()


def _thread(fn) -> None:
    threading.Thread(target=fn, daemon=False).start()


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
        with _typing(bot):
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


def _interview_confirm(cfg: dict, store, bot, count_fn) -> None:
    reply = json.loads(store.get_state("interview_proposal") or "{}")
    if store.get_state("interview") != "proposed" or not reply:
        bot.send("Da gibt es nichts zu bestätigen. Schick /setup, um anzufangen.")
        return
    cdir = Path(cfg["config_dir"])
    cdir.mkdir(parents=True, exist_ok=True)
    config.write_search(cdir / "search.json", reply["search"])
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
        return
    days = cfg["sources"].get("ba", {}).get("published_since_days", 7)
    if n:
        bot.send(f"{n} Stellen aus den letzten {days} Tagen passen zu deiner Suche, die KI bewertet sie als Nächstes. "
                 "Neue passende Stellen kommen ab dem nächsten stündlichen Durchlauf hier an.")
    else:
        bot.send(f"Aus den letzten {days} Tagen passt keine Stelle zu dieser Suche. Schick /setup, um sie zu erweitern.")


def handle_update(upd: dict, cfg: dict, store, bot, report_fn=claude.deep_report, background=_thread,
                  interview_fn=None, count_fn=count_matches) -> None:
    interview_fn = interview_fn or _interview_turn(cfg, store)
    cb = upd.get("callback_query")
    if not cb:
        text = upd.get("message", {}).get("text", "")
        command = text.strip().split()[0] if text.strip().startswith("/") else ""
        if command == "/setup" or (command == "/start" and not has_profile(cfg)):
            store.interview_clear()
            store.set_state("interview", "")
            store.set_state("interview_proposal", "")
            _interview_step(cfg, store, bot, interview_fn, None)
            return
        if command == "/profile":
            bot.send(html.escape(profile_text(cfg)) if has_profile(cfg)
                     else "Noch kein Profil. Schick /setup, um eins anzulegen.")
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
            _interview_confirm(cfg, store, bot, count_fn)
        elif raw_id == "edit":
            store.set_state("interview", "active")
            store.interview_add("bot", "Was soll ich ändern?")
            bot.send("✏️ Was soll ich ändern?")
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


def dispatch(upd: dict, registry: Registry, handle=handle_update, run=lambda chat, fn: fn()) -> None:
    """Route an update to its person's instance; a stranger needs a valid invite link.

    `run(chat, fn)` executes the handling; `listen` passes ChatWorkers.submit."""
    chat = _chat_of(upd)
    inst = registry.get(chat)
    if inst is None:
        match = re.fullmatch(r"/start\s+(\S+)", upd.get("message", {}).get("text", "").strip())
        name = config.redeem_invite(match.group(1)) if match else None
        if name is None:
            log.info("ignoring update from unknown chat %s", chat)
            return
        config.set_env_value(config.config_dir(name) / "env", "TELEGRAM_CHAT_ID", chat)
        registry.reload()
        inst = registry.get(chat)
        log.info("chat %s joined as instance %s", chat, name)
        upd = {**upd, "message": {**upd["message"], "text": "/start"}}
    cfg, store, bot = inst
    run(chat, lambda: handle(upd, cfg, store, bot))


def listen(registry: Registry, receiver, stop=lambda: False, handle=dispatch) -> None:
    """One long-polling loop for the shared bot; `receiver` is a Bot without a chat."""
    log.info("listening for Telegram updates")
    try:
        receiver.set_commands(COMMANDS)  # the "/" menu in the chat
    except Exception as e:
        log.warning("could not register commands: %s", e)
    for cfg, store, bot in registry.all():
        resume_reports(cfg, store, bot)
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
