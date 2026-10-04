import tempfile
import unittest
from pathlib import Path

import json

from job_radar import app
from job_radar.interview import InterviewError
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

    def typing(self):
        self.typed = getattr(self, "typed", 0) + 1


def p(i, title="Senior Kotlin Engineer", **kw):
    return Posting(source="t", external_id=str(i), url=f"https://x/{i}", title=title, company="ACME",
                   location="Hamburg", **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "db.sqlite")
        self.bot = FakeBot()
        self.cfg = _merge(DEFAULTS, {"filter": {"locations": [], "include": ["kotlin"], "exclude_title": ["junior"]}, "triage": {"threshold": 6},
                                     "report": {"reports_dir": str(self.dir / "reports")},
                                     "profile_files": [str(self.dir / "profile.md")], "config_dir": str(self.dir)})
        (self.dir / "profile.md").write_text("Kotlin backend developer")

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

    def test_cards_per_run_are_capped_and_rest_follows_best_first(self):
        self.cfg["notify"] = {"max_per_run": 2}
        scores = {1: 6, 2: 9, 3: 7, 4: 8}
        triage = lambda batch, **kw: {x.id: (scores[x.id], "") for x in batch}
        first = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(1, 5)]), triage_fn=triage)
        self.assertEqual(first["notified"], 2)
        self.assertEqual([self.store.get(i).status for i in (2, 4)], ["notified", "notified"])
        self.assertEqual(self.store.get(3).status, "queued")
        self.bot.sent.clear()
        second = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([]), triage_fn=triage)
        self.assertEqual(second["notified"], 2)
        self.assertEqual([self.store.get(i).status for i in (3, 1)], ["notified", "notified"])
        self.assertIn("Senior Kotlin Engineer", self.bot.sent[0][0])

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


class CountMatchesTest(Base):
    def test_failed_source_raises_instead_of_counting_zero(self):
        def fetch(cfg, on_error):
            on_error("ba: HTTP 503")
            return []
        with self.assertRaises(RuntimeError):
            app.count_matches(self.cfg, fetch=fetch)

    def test_counts_matching_postings(self):
        (self.dir / "search.json").write_text(json.dumps({"filter": {"exclude_title": ["junior"]}}))
        self.assertEqual(app.count_matches(self.cfg, fetch=lambda cfg, on_error: [p(1), p(2, "Junior Kotlin")]), 1)


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
        self.assertIn("fehlgeschlagen", self.bot.sent[-1][0].lower())

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
        registry = type("R", (), {"all": lambda self: []})()
        app.listen(registry, self.bot, stop=lambda: not batches, handle=handle)
        self.assertEqual(handled, [2])

    def test_unknown_posting_is_answered(self):
        self.press("s:999")
        self.assertIn("unbekannt", self.bot.answers[-1].lower())


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
        self.assertIn("Ich recherchiere", self.bot.sent[0][0])

    def test_same_link_twice_reuses_posting(self):
        self.send_link("https://jobs.example.com/x")
        self.send_link("https://jobs.example.com/x")
        self.assertIsNone(self.store.get(2))
        self.assertEqual(len(self.bot.docs), 2)

    def test_usage_command_replies_with_summary(self):
        self.store.record_usage("triage", {"model": "haiku", "cost_usd": 0.25, "input_tokens": 5, "output_tokens": 1,
                                           "five_hour": 0.05, "five_hour_resets": 1791146400,
                                           "seven_day": 0.031, "seven_day_resets": 1791702000})
        self.send_link("/usage")
        text = self.bot.sent[-1][0]
        self.assertIn("0.25", text)
        self.assertIn("triage", text)
        self.assertIn("5-Stunden-Fenster: 5%", text)
        self.assertIn("Woche: 3%", text)
        self.assertIn(app._resets(1791702000).split()[0], app._WEEKDAYS)

    def test_usage_probes_when_limits_are_stale(self):
        probes = []
        text = app.usage_text(self.store, probe=lambda: probes.append(1) or self.store.record_usage(
            "probe", {"model": "haiku", "five_hour": 0.4, "seven_day": 0.5}))
        self.assertEqual(probes, [1])
        self.assertIn("Woche: 50%", text)

    def test_usage_without_limits_or_probe_says_unknown(self):
        self.assertIn("unbekannt", app.usage_text(self.store).lower())

    def test_help_and_start_list_commands_and_buttons(self):
        for cmd in ("/help", "/start"):
            self.send_link(cmd)
            text = self.bot.sent[-1][0]
            for word in ("/usage", "/help", "/setup", "/profile", "Recherche", "Überspringen", "Beworben", "Link"):
                self.assertIn(word, text)

    def test_listen_registers_command_menu(self):
        registered = []
        self.bot.set_commands = registered.extend
        app.listen(type("R", (), {"all": lambda self: []})(), self.bot, stop=lambda: True)
        self.assertIn("usage", [name for name, _ in registered])
        self.assertIn("help", [name for name, _ in registered])

    def test_text_without_link_gets_help(self):
        self.send_link("hello")
        self.assertIn("link", self.bot.sent[-1][0].lower())


class ProfileTextTest(Base):
    def test_joins_existing_profile_files(self):
        (self.dir / "a.md").write_text("Nurse, part-time")
        self.cfg["profile_files"] = [str(self.dir / "a.md"), str(self.dir / "missing.md")]
        self.assertEqual(app.profile_text(self.cfg), "Nurse, part-time")

    def test_fallback_assumes_no_field(self):
        self.cfg["profile_files"] = [str(self.dir / "missing.md")]
        text = app.profile_text(self.cfg).lower()
        self.assertIn("no profile", text)
        self.assertNotIn("backend", text)


SEARCH = {"filter": {"include": ["pflegefachkraft"], "locations": ["Kassel"]}}


def ask(text):
    return {"done": False, "say": text}


DONE = {"done": True, "summary": "Teilzeit Pflege bei Kassel", "profile_md": "# Profil\nPflege", "search": SEARCH}


class InterviewFlowTest(Base):
    def setUp(self):
        super().setUp()
        (self.dir / "profile.md").unlink()
        self.replies, self.calls = [], []

    def fake_turn(self, log, previous):
        self.calls.append((list(log), previous))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def update(self, upd):
        app.handle_update(upd, self.cfg, self.store, self.bot, interview_fn=self.fake_turn,
                          count_fn=lambda cfg: 12, background=lambda fn: fn())

    def send(self, text, *replies):
        self.replies += replies
        self.update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}})

    def press(self, data):
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}})

    def propose(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Pflege, Teilzeit, Kassel", DONE)

    def test_start_without_profile_begins_interview(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.assertIn("Was machst du beruflich?", self.bot.sent[-1][0])
        self.assertEqual(self.store.get_state("interview"), "active")
        self.assertEqual(self.store.interview_log(), [("bot", "Was machst du beruflich?")])
        self.assertGreater(self.bot.typed, 0)

    def test_start_with_profile_shows_help(self):
        (self.dir / "profile.md").write_text("x")
        self.send("/start")
        self.assertIn("/setup", self.bot.sent[-1][0])
        self.assertEqual(self.calls, [])

    def test_answers_go_to_the_interview_even_with_a_link(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Mein Profil: https://example.com/me", ask("Danke! Wo wohnst du?"))
        self.assertEqual(self.calls[-1][0][-1], ("person", "Mein Profil: https://example.com/me"))
        self.assertIsNone(self.store.get(1))
        self.assertIn("Wo wohnst du?", self.bot.sent[-1][0])

    def test_finished_interview_proposes_summary_with_buttons(self):
        self.propose()
        text, buttons = self.bot.sent[-1]
        self.assertIn("Teilzeit Pflege bei Kassel", text)
        self.assertEqual([data for _, data in buttons[0]], ["i:ok", "i:edit"])
        self.assertEqual(self.store.get_state("interview"), "proposed")

    def test_confirm_writes_profile_and_search_and_reports_count(self):
        self.propose()
        self.press("i:ok")
        self.assertEqual((self.dir / "profile.md").read_text(), "# Profil\nPflege\n")
        self.assertEqual(json.loads((self.dir / "search.json").read_text()), SEARCH)
        self.assertEqual(self.store.get_state("interview"), "")
        self.assertEqual(self.store.interview_log(), [])
        self.assertIn("12", self.bot.sent[-1][0])

    def test_change_reopens_interview_with_the_proposal_in_the_transcript(self):
        self.propose()
        self.press("i:edit")
        self.assertEqual(self.store.get_state("interview"), "active")
        self.send("Lieber 50 km", DONE)
        log = self.calls[-1][0]
        self.assertTrue(any("Teilzeit Pflege bei Kassel" in text for _, text in log[:-1]))
        self.assertEqual(log[-1], ("person", "Lieber 50 km"))

    def test_setup_with_existing_profile_passes_it_as_previous(self):
        (self.dir / "profile.md").write_text("# Altes Profil")
        self.send("/setup", ask("Was hat sich geändert?"))
        self.assertEqual(self.calls[0][1], "# Altes Profil")
        self.assertEqual(self.store.get_state("interview"), "active")

    def test_setup_drops_an_old_proposal_even_if_the_first_turn_fails(self):
        self.propose()
        self.send("/setup", InterviewError("bad"))
        self.press("i:ok")
        self.assertFalse((self.dir / "search.json").exists())
        self.assertIn("nichts zu bestätigen", self.bot.sent[-1][0])

    def test_profile_command_shows_profile_or_hint(self):
        self.send("/profile")
        self.assertIn("/setup", self.bot.sent[-1][0])
        (self.dir / "profile.md").write_text("# Profil <Pflege>")
        self.send("/profile")
        self.assertIn("# Profil &lt;Pflege&gt;", self.bot.sent[-1][0])

    def test_failed_turn_keeps_the_transcript_and_asks_to_resend(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Pflege", InterviewError("bad"))
        self.assertEqual(self.store.interview_log(), [("bot", "Was machst du beruflich?")])
        self.assertIn("nochmal", self.bot.sent[-1][0].lower())

    def test_poll_without_profile_nudges_once_and_fetches_nothing(self):
        def fetch(cfg):
            raise AssertionError("must not fetch")
        for _ in range(2):
            result = app.poll(self.cfg, self.store, self.bot, fetch=fetch)
        self.assertEqual(result, {"skipped": "no profile"})
        self.assertEqual(len([t for t, _ in self.bot.sent if "/setup" in t]), 1)


class RouterTest(unittest.TestCase):
    def setUp(self):
        import os
        from unittest import mock
        from job_radar import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d")})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.config_dir().mkdir(parents=True)
        (config.config_dir() / "env").write_text("TELEGRAM_BOT_TOKEN=t\n")
        config.create_instance("anna")
        config.set_env_value(config.config_dir("anna") / "env", "TELEGRAM_CHAT_ID", "42")
        self.bots = {}

        def make_bot(token, chat_id):
            bot = FakeBot()
            bot.chat_id = chat_id
            self.bots[chat_id] = bot
            return bot

        self.registry = app.Registry("t", make_bot=make_bot)
        self.addCleanup(self.registry.close)
        self.handled = []

    def handle(self, upd, cfg, store, bot):
        self.handled.append((cfg["instance"], upd.get("message", {}).get("text"), bot.chat_id))

    def msg(self, chat, text):
        app.dispatch({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}, self.registry, handle=self.handle)

    def test_known_chat_goes_to_its_instance(self):
        self.msg(42, "hallo")
        self.assertEqual(self.handled, [("anna", "hallo", "42")])

    def test_buttons_are_routed_by_their_message_chat(self):
        upd = {"update_id": 1, "callback_query": {"id": "c", "data": "s:1", "message": {"chat": {"id": 42}}}}
        app.dispatch(upd, self.registry, handle=self.handle)
        self.assertEqual(self.handled[0][0], "anna")

    def test_unknown_chat_is_ignored(self):
        self.msg(7, "hallo")
        self.msg(7, "/start wrongcode")
        self.assertEqual(self.handled, [])
        self.assertNotIn("7", self.bots)

    def test_invite_code_binds_chat_and_starts(self):
        code = self.config.create_invite("ben")
        self.msg(7, f"/start {code}")
        self.assertEqual(self.handled, [("ben", "/start", "7")])
        self.assertEqual(self.config.instance_env("ben")["TELEGRAM_CHAT_ID"], "7")
        self.msg(8, f"/start {code}")
        self.assertEqual(len(self.handled), 1)

    def test_registry_lists_bound_instances(self):
        self.config.create_instance("unbound")
        self.assertEqual([cfg["instance"] for cfg, _, _ in self.registry.all()], ["anna"])

    def test_broken_instance_config_does_not_drop_the_others(self):
        self.config.create_instance("bad")
        self.config.set_env_value(self.config.config_dir("bad") / "env", "TELEGRAM_CHAT_ID", "9")
        (self.config.config_dir("bad") / "config.json").write_text("{")
        self.config.create_instance("zed")
        self.config.set_env_value(self.config.config_dir("zed") / "env", "TELEGRAM_CHAT_ID", "50")
        self.registry.reload()
        self.assertIsNotNone(self.registry.get("42"))
        self.assertIsNotNone(self.registry.get("50"))
        self.assertIsNone(self.registry.get("9"))

    def test_dispatch_hands_work_to_the_runner_keyed_by_chat(self):
        queued = []
        app.dispatch({"update_id": 1, "message": {"chat": {"id": 42}, "text": "hallo"}}, self.registry,
                     handle=self.handle, run=lambda chat, fn: queued.append((chat, fn)))
        self.assertEqual([chat for chat, _ in queued], ["42"])
        self.assertEqual(self.handled, [])
        queued[0][1]()
        self.assertEqual(self.handled, [("anna", "hallo", "42")])


class ChatWorkersTest(unittest.TestCase):
    def test_a_slow_chat_does_not_block_others_and_keeps_its_order(self):
        import threading
        workers = app.ChatWorkers()
        release, done, order = threading.Event(), threading.Event(), []
        workers.submit("a", lambda: (release.wait(5), order.append("a1")))
        workers.submit("a", lambda: order.append("a2"))
        workers.submit("b", lambda: (order.append("b1"), done.set()))
        self.assertTrue(done.wait(5))
        self.assertEqual(order, ["b1"])
        release.set()
        workers.join()
        self.assertEqual(order, ["b1", "a1", "a2"])

    def test_a_failing_job_does_not_stop_the_chat(self):
        workers = app.ChatWorkers()
        ran = []
        workers.submit("a", lambda: 1 / 0)
        workers.submit("a", lambda: ran.append(1))
        workers.join()
        self.assertEqual(ran, [1])
