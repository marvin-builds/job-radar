import tempfile
import unittest
from pathlib import Path

from job_radar import app
from job_radar.config import DEFAULTS, _merge
from job_radar.model import Posting
from job_radar.store import Store


class FakeBot:
    def __init__(self):
        self.sent, self.answers, self.docs = [], [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))

    def answer(self, cid, text=""):
        self.answers.append(text)

    def send_document(self, path, caption=""):
        self.docs.append((Path(path), caption))


def p(i, title="Senior Kotlin Engineer", **kw):
    return Posting(source="t", external_id=str(i), url=f"https://x/{i}", title=title, company="ACME",
                   location="Hamburg", **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "db.sqlite")
        self.bot = FakeBot()
        self.cfg = _merge(DEFAULTS, {"filter": {"locations": []}, "triage": {"threshold": 6},
                                     "report": {"reports_dir": str(self.dir / "reports")}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class PollTest(Base):
    def fetch(self, postings):
        return lambda cfg: postings

    def test_filters_triages_and_notifies_above_threshold(self):
        postings = [p(1), p(2), p(3, title="Junior Kotlin Dev")]
        triage = lambda batch, **kw: {1: (8, "great"), 2: (3, "meh")}
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch(postings), triage_fn=triage)
        self.assertEqual(result, {"fetched": 3, "new": 3, "matched": 2, "notified": 1})
        self.assertEqual(len([s for s in self.bot.sent if s[1]]), 1)
        self.assertIn("great", self.bot.sent[-1][0])
        self.assertEqual(self.store.get(2).status, "low")
        self.assertEqual(self.store.get(3).status, "filtered")
        self.assertEqual(self.store.get(1).status, "notified")

    def test_second_poll_sends_nothing(self):
        triage = lambda batch, **kw: {x.id: (9, "") for x in batch}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.bot.sent.clear()
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(result["new"], 0)
        self.assertEqual(self.bot.sent, [])

    def test_triage_failure_still_notifies_unscored(self):
        def broken(batch, **kw):
            raise RuntimeError("claude down")
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=broken)
        self.assertEqual(result["notified"], 1)

    def test_triage_runs_in_batches_and_respects_max_per_run(self):
        self.cfg["triage"]["max_per_run"] = 15
        batches = []
        triage = lambda batch, **kw: batches.append(len(batch)) or {}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(30)]), triage_fn=triage)
        self.assertEqual(batches, [10, 5])


class FetchAllTest(unittest.TestCase):
    def test_broken_source_does_not_stop_others(self):
        class Good:
            @staticmethod
            def fetch(cfg, http_get): return [p(1)]

        class Bad:
            @staticmethod
            def fetch(cfg, http_get): raise OSError("down")

        cfg = {"sources": {"good": {"enabled": True}, "bad": {"enabled": True}, "off": {"enabled": False}}}
        errors = []
        got = app.fetch_all(cfg, registry={"good": Good, "bad": Bad, "off": Bad}, on_error=errors.append)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(errors), 1)


class ButtonTest(Base):
    def setUp(self):
        super().setUp()
        [self.posting] = self.store.add_new([p(1)])

    def press(self, data, report_fn=None):
        upd = {"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}}
        return app.handle_update(upd, self.cfg, self.store, self.bot,
                                 report_fn=report_fn or (lambda posting, **kw: "---\ntitle: x\n---\n# R\n## TL;DR\n- good"),
                                 background=lambda fn: fn())

    def test_skip_sets_status(self):
        self.press("s:1")
        self.assertEqual(self.store.get(1).status, "skipped")

    def test_applied_appends_tracker_row(self):
        tracker = self.dir / "tracker.md"
        tracker.write_text("| Date | Company |\n|---|---|\n")
        self.cfg["tracker_path"] = str(tracker)
        self.press("a:1")
        self.assertEqual(self.store.get(1).status, "applied")
        self.assertIn("| ACME | Senior Kotlin Engineer |", tracker.read_text())

    def test_report_writes_file_and_sends_it(self):
        self.press("r:1")
        files = list((self.dir / "reports").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{2}-\d{2}-\d{2}-acme-senior-kotlin-engineer\.md$")
        self.assertEqual(self.bot.docs[0][0], files[0])
        self.assertEqual(self.store.get(1).status, "reported")

    def test_report_failure_is_reported_to_chat(self):
        def broken(posting, **kw):
            raise RuntimeError("timeout")
        self.press("r:1", report_fn=broken)
        self.assertIn("failed", self.bot.sent[-1][0].lower())

    def test_unknown_posting_is_answered(self):
        self.press("s:999")
        self.assertIn("unknown", self.bot.answers[-1].lower())
