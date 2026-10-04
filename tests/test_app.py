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

    def test_employer_description_is_stored_and_shown(self):
        triage = lambda batch, **kw: {1: (8, "fit", "Makes elevators")}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(self.store.get(1).employer, "Makes elevators")
        self.assertIn("Makes elevators", self.bot.sent[-1][0])

    def test_triage_usage_is_recorded(self):
        def triage(batch, on_usage=None, **kw):
            on_usage({"model": "haiku", "cost_usd": 0.01, "input_tokens": 5, "output_tokens": 1})
            return {}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(self.store.usage_summary(days=1)["triage"]["calls"], 1)

    def test_second_poll_sends_nothing(self):
        triage = lambda batch, **kw: {x.id: (9, "") for x in batch}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.bot.sent.clear()
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(result["new"], 0)
        self.assertEqual(self.bot.sent, [])

    def test_triage_failure_keeps_postings_pending_for_next_run(self):
        def broken(batch, **kw):
            raise RuntimeError("claude down")
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=broken)
        self.assertEqual(result["notified"], 0)
        self.assertEqual(self.store.get(1).status, "pending")
        triage = lambda batch, **kw: {x.id: (9, "now") for x in batch}
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([]), triage_fn=triage)
        self.assertEqual(result["notified"], 1)
        self.assertEqual(self.store.get(1).status, "notified")

    def test_overflow_beyond_max_per_run_waits_for_next_run(self):
        self.cfg["triage"]["max_per_run"] = 2
        triage = lambda batch, **kw: {x.id: (9, "") for x in batch}
        first = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(3)]), triage_fn=triage)
        self.assertEqual(first["notified"], 2)
        second = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([]), triage_fn=triage)
        self.assertEqual(second["notified"], 1)

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

    def test_report_file_name_transliterates_umlauts(self):
        q = p(2, title="Entwickler Größe")
        q.company = "Dr. Güldener Ärzte"
        path = app.write_report(self.cfg, q, "x")
        self.assertTrue(path.name.endswith("-dr-gueldener-aerzte-entwickler-groesse.md"), path.name)

    def test_listen_resumes_unfinished_reports(self):
        self.store.set_status(1, "interesting")
        resumed = []
        app.resume_reports(self.cfg, self.store, self.bot, background=lambda fn: resumed.append(fn))
        self.assertEqual(len(resumed), 1)

    def test_report_failure_is_reported_to_chat(self):
        def broken(posting, **kw):
            raise RuntimeError("timeout")
        self.press("r:1", report_fn=broken)
        self.assertIn("failed", self.bot.sent[-1][0].lower())

    def test_expired_button_answer_still_applies_action(self):
        def expired(cid, text=""):
            raise OSError("HTTP Error 400: query is too old")
        self.bot.answer = expired
        self.press("s:1")
        self.assertEqual(self.store.get(1).status, "skipped")

    def test_one_failing_update_does_not_drop_the_rest(self):
        handled = []

        def handle(upd, *args, **kw):
            if upd["update_id"] == 1:
                raise RuntimeError("boom")
            handled.append(upd["update_id"])

        batches = [[{"update_id": 1}, {"update_id": 2}]]
        self.bot.updates = lambda wait=0: batches.pop(0) if batches else []
        self.bot.set_commands = lambda c: None
        app.listen(self.cfg, self.store, self.bot, stop=lambda: not batches, handle=handle)
        self.assertEqual(handled, [2])

    def test_unknown_posting_is_answered(self):
        self.press("s:999")
        self.assertIn("unknown", self.bot.answers[-1].lower())


class LinkTest(Base):
    REPORT = '---\ncompany: "Open Co"\nrole: "Backend Engineer"\n---\n## 1. TL;DR\n- nice'

    def send_link(self, text):
        upd = {"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}
        app.handle_update(upd, self.cfg, self.store, self.bot,
                          report_fn=lambda posting, **kw: self.REPORT, background=lambda fn: fn())

    def test_pasted_link_produces_report_named_from_frontmatter(self):
        self.send_link("look at https://jobs.example.com/open-co/backend-engineer-123 please")
        files = list((self.dir / "reports").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{2}-\d{2}-\d{2}-open-co-backend-engineer\.md$")
        stored = self.store.get(1)
        self.assertEqual((stored.source, stored.company, stored.status), ("manual", "Open Co", "reported"))
        self.assertIn("Researching", self.bot.sent[0][0])

    def test_same_link_twice_reuses_posting(self):
        self.send_link("https://jobs.example.com/x")
        self.send_link("https://jobs.example.com/x")
        self.assertIsNone(self.store.get(2))
        self.assertEqual(len(self.bot.docs), 2)

    def test_usage_command_replies_with_summary(self):
        self.store.record_usage("triage", {"model": "haiku", "cost_usd": 0.25, "input_tokens": 5, "output_tokens": 1})
        self.send_link("/usage")
        self.assertIn("0.25", self.bot.sent[-1][0])
        self.assertIn("triage", self.bot.sent[-1][0])

    def test_help_and_start_list_commands_and_buttons(self):
        for cmd in ("/help", "/start"):
            self.send_link(cmd)
            text = self.bot.sent[-1][0]
            for word in ("/usage", "/help", "Deep report", "Skip", "Applied", "link"):
                self.assertIn(word, text)

    def test_listen_registers_command_menu(self):
        registered = []
        self.bot.set_commands = registered.extend
        app.listen(self.cfg, self.store, self.bot, stop=lambda: True)
        self.assertIn("usage", [name for name, _ in registered])
        self.assertIn("help", [name for name, _ in registered])

    def test_text_without_link_gets_help(self):
        self.send_link("hello")
        self.assertIn("link", self.bot.sent[-1][0].lower())
