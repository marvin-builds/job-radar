"""The whole bot with real threads: listener loop, chat workers, background jobs, SQLite files,
invites, interview slot and Claude lock. Only Telegram, Claude and the job portals are fakes.

Unit tests run background jobs synchronously, so they never see two threads on one store;
this test does (#24: the shared SQLite connection broke /quellen and /suchen, #23)."""
import json
import logging
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from job_radar import app, claude, config, slot
from job_radar.model import Posting
from job_radar.sources import arbeitnow, ba

SEARCH = {"sources": {"ba": {"keywords": ["Gärtner"], "areas": [{"where": "Kassel", "radius_km": 30}]}},
          "filter": {"include": ["gärtner"], "locations": ["Kassel"]}}
DONE = {"done": True, "summary": "- **Gärtnerin** in Kassel", "profile_md": "# Profil\nGärtnerin in Kassel",
        "search": SEARCH}


def fake_claude(prompt, *, model, tools, on_usage=None, **kw):
    """Answers like Claude would, by prompt; slow enough that jobs overlap."""
    time.sleep(0.05)
    if on_usage:
        on_usage({"model": model, "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 10, "duration_ms": 50})
    if prompt.startswith("You screen job postings"):
        ids = [line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("id: ")]
        return json.dumps([{"id": int(i), "score": 8, "why": "passt", "employer": "Gärtnerei"} for i in ids])
    if prompt.startswith("You set up a personal job radar"):
        if "Person: Gärtnerin" in prompt:
            return json.dumps(DONE)
        return json.dumps({"done": False, "say": "Was machst du beruflich?"})
    if prompt.startswith("You research one job posting"):
        assert "Gärtnerin in Kassel" not in prompt, "the research call must not see the profile"
        return "Notizen: Gärtnerei Grün, 20 Leute ([Quelle](https://example.org))"
    if prompt.startswith("You write a report"):
        return '---\ncompany: "Gärtnerei Grün"\nrole: "Gärtner"\n---\n# Bericht\n## TL;DR\n- **passt** gut'
    if prompt.startswith("You help one job seeker find job boards"):
        return "[]"
    return "ok"


def details(posting, http_get):
    posting.text += "\nArbeitszeit: Vollzeit"


def postings(cfg, http_get):
    return [Posting(source="ba", external_id=str(i), url=f"https://jobs.example/{i}", title="Gärtner (m/w/d)",
                    company="Gärtnerei Grün", location="Kassel", text="gärtner") for i in range(3)]


class Bot:
    """Telegram for one chat; sends land in a shared, thread-safe list."""

    def __init__(self, world, chat):
        self.world, self.chat_id = world, chat

    def send(self, text, buttons=None):
        self.world.sent.append((self.chat_id, text, buttons))

    def send_document(self, path, caption="", buttons=None):
        self.world.sent.append((self.chat_id, f"DOC {Path(path).name} {caption}", None))

    def note(self, text):
        return 1

    def delete(self, message_id):
        pass

    def typing(self):
        pass

    def answer(self, callback_id, text=""):
        pass

    def username(self):
        return "radar_bot"


class Receiver:
    """The shared bot's long polling, fed by the test; `None` stops the listener."""

    def __init__(self):
        self.inbox, self.next_id = queue.Queue(), 1

    def set_commands(self, commands, chat_id=None):
        pass

    def updates(self, wait=0):
        try:
            upd = self.inbox.get(timeout=0.2)
        except queue.Empty:
            return []
        if upd is None:
            raise app.Stopping()
        return [upd]


class Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(f"{record.levelname} {record.getMessage()}")


class TwoPeopleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for patcher in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "c"),
                                                     "XDG_DATA_HOME": str(root / "d")}),
                        mock.patch.object(claude, "run", fake_claude),
                        mock.patch.object(ba, "fetch", postings),
                        mock.patch.object(ba, "enrich", details),
                        mock.patch.object(arbeitnow, "fetch", lambda cfg, http_get: []),
                        mock.patch.object(claude, "LOCK_PATH", root / "d" / "claude.lock"),
                        mock.patch.object(slot, "PATH", root / "d" / "interview.slot")):
            patcher.start()
            self.addCleanup(patcher.stop)
        config.config_dir().mkdir(parents=True)
        (config.config_dir() / "env").write_text("TELEGRAM_BOT_TOKEN=t\n")
        self.codes = {name: config.create_invite(name) for name in ("anna", "ben")}
        self.sent, self.receiver = [], Receiver()
        self.errors = Errors()
        logging.getLogger("job-radar").addHandler(self.errors)
        self.addCleanup(logging.getLogger("job-radar").removeHandler, self.errors)
        self.registry = app.Registry("t", make_bot=lambda token, chat: Bot(self, chat))
        self.addCleanup(self.registry.close)
        self.listener = threading.Thread(target=app.listen, args=(self.registry, self.receiver), daemon=True)
        self.listener.start()

    def tearDown(self):
        self.receiver.inbox.put(None)
        self.listener.join(10)

    def say(self, chat, text):
        self.receiver.inbox.put({"update_id": 1, "message": {"chat": {"id": chat}, "text": text,
                                                             "date": int(time.time())}})

    def tap(self, chat, data):
        self.receiver.inbox.put({"update_id": 1, "callback_query": {
            "id": "c", "data": data, "message": {"chat": {"id": chat}, "date": int(time.time())}}})

    def texts(self, chat):
        return [text for c, text, _ in list(self.sent) if c == str(chat)]

    def wait_for(self, chat, part, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(part in text for text in self.texts(chat)):
                return
            time.sleep(0.02)
        self.fail(f"chat {chat} never got {part!r}; got {self.texts(chat)}; log: {self.errors.records}")

    def test_two_people_at_once(self):
        anna, ben = 101, 202
        self.say(anna, f"/start {self.codes['anna']}")
        self.wait_for(anna, "Was machst du beruflich?")
        self.say(ben, f"/start {self.codes['ben']}")
        self.wait_for(ben, "Gerade richtet jemand anderes")  # one interview at a time

        self.say(anna, "Gärtnerin in Kassel, Teilzeit")
        self.wait_for(anna, "So würde ich für dich suchen")
        self.tap(anna, "i:ok")  # saves, then first poll and board search in the background
        # Meanwhile ben pokes at everything, on another worker thread.
        for text in ("/quellen", "/zeiten", "/suchen", "/usage", "/help"):
            self.say(ben, text)
        self.tap(ben, "q:find")
        self.tap(ben, "z:workdays")
        self.say(anna, "/suchen")  # while her first poll may still run

        self.wait_for(anna, "Das waren die besten Treffer")
        cards = [buttons for c, _, buttons in list(self.sent) if c == str(anna) and buttons
                 and buttons[0][0][1].startswith("r:")]
        self.assertTrue(cards)
        self.tap(anna, cards[0][0][0][1])  # 🔎 report, in the background
        posting = cards[-1][0][1][1].split(":")[1]
        self.tap(anna, f"v:{posting}")  # ⭐ bookmark while the report runs
        self.say(anna, "/merkliste")
        self.wait_for(anna, "⭐ Vorgemerkt")
        self.tap(anna, f"m:a:{posting}")  # applied, from the list
        self.say(ben, "/setup")  # anna's interview is over, so ben may start now
        self.wait_for(anna, "DOC ")
        self.wait_for(ben, "Was machst du beruflich?")
        self.wait_for(ben, "Ab jetzt suche ich Mo–Fr")
        self.wait_for(anna, "Jobbörse")  # the board search after her setup finished

        self.receiver.inbox.put(None)
        self.listener.join(10)
        self.assertFalse(self.listener.is_alive())
        self.assertEqual(self.errors.records, [])
        anna_dir = config.config_dir("anna")
        self.assertIn("Gärtnerin", (anna_dir / "profile.md").read_text())
        self.assertEqual(json.loads((config.config_dir("ben") / "search.json").read_text())["schedule"],
                         {"weekdays": [9, 13, 18], "weekend": []})
        store = self.registry.get(str(anna))[1]
        self.assertEqual(set(store.usage_summary(1)), {"interview", "triage", "report", "sources"})
        self.assertEqual(store.get_state("busy:/quellen search"), "")
        self.assertEqual([str(p.id) for p, _ in store.marked("applied")], [posting])
        self.assertEqual(len(store.due_reminders(days_ahead=15)), 1)


if __name__ == "__main__":
    unittest.main()
