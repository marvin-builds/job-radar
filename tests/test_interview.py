import json
import unittest

from job_radar import interview
from job_radar.config import validate_search
from tests.test_claude import FakeRunner

SEARCH = {"sources": {"ba": {"keywords": ["Pflegefachkraft"], "areas": [{"where": "Kassel", "radius_km": 30}]}},
          "filter": {"include": ["pflegefachkraft"], "locations": ["Kassel"]}}
DONE = {"done": True, "summary": "Ich suche Pflege-Jobs bei Kassel.", "profile_md": "# Profil\nPflege", "search": SEARCH}


class SequenceRunner:
    """Answers successive claude calls with the given replies."""

    def __init__(self, *replies):
        self.runners = [FakeRunner(r) for r in replies]
        self.calls = []

    def __call__(self, args, **kw):
        runner = self.runners[len(self.calls)]
        self.calls.append((args, kw))
        return runner(args, **kw)


class TurnTest(unittest.TestCase):
    def turn(self, *replies, log=(), previous=""):
        self.runner = SequenceRunner(*replies)
        return interview.turn(list(log), previous=previous, model="sonnet", runner=self.runner)

    def test_question_reply(self):
        reply = self.turn('{"say": "Welchen Beruf hast du?", "done": false}')
        self.assertEqual(reply, {"say": "Welchen Beruf hast du?", "done": False})

    def test_finished_reply_with_valid_settings(self):
        self.assertEqual(self.turn("Here you go:\n" + json.dumps(DONE)), DONE)

    def test_call_has_no_tools_and_carries_transcript_and_previous_profile(self):
        self.turn('{"say": "ok", "done": false}', log=[("bot", "Was suchst du?"), ("person", "Teilzeit")],
                  previous="# Altes Profil")
        args, kw = self.runner.calls[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("Was suchst du?", kw["input"])
        self.assertIn("Teilzeit", kw["input"])
        self.assertIn("# Altes Profil", kw["input"])

    def test_invalid_settings_are_sent_back_once_for_a_fix(self):
        bad = {**DONE, "search": {"db_path": "/etc/passwd"}}
        self.assertEqual(self.turn(json.dumps(bad), json.dumps(DONE)), DONE)
        self.assertIn("db_path", self.runner.calls[1][1]["input"])

    def test_gives_up_after_the_retry(self):
        with self.assertRaises(interview.InterviewError):
            self.turn("no json here", "still none")

    def test_finished_reply_needs_summary_and_profile(self):
        with self.assertRaises(interview.InterviewError):
            self.turn(json.dumps({**DONE, "profile_md": ""}), json.dumps({**DONE, "summary": 3}))

    def test_finished_reply_needs_search_terms_and_include_words(self):
        no_include = {**DONE, "search": {**SEARCH, "filter": {"include": [], "locations": ["Kassel"]}}}
        no_keywords = {**DONE, "search": {"filter": SEARCH["filter"]}}
        with self.assertRaises(interview.InterviewError):
            self.turn(json.dumps(no_include), json.dumps(no_keywords))
        self.assertIn("include", self.runner.calls[1][1]["input"])


ANSWERS = {"kind": "job", "hours": "part", "km": "25", "remote": "site", "term": "perm", "temp": "no", "lang": "de"}


class ButtonQuestionTest(unittest.TestCase):
    """Closed questions of a first setup: code asks them with buttons and writes their settings."""

    def test_questions_are_asked_in_order_until_all_are_answered(self):
        self.assertEqual(interview.next_question({}), interview.QUESTIONS[0].key)
        self.assertEqual(interview.next_question({"kind": "job"}), interview.QUESTIONS[1].key)
        self.assertIsNone(interview.next_question(ANSWERS))

    def test_the_plan_covers_the_closed_questions(self):
        self.assertEqual([q.key for q in interview.QUESTIONS], list(ANSWERS))

    def test_button_data_fits_telegram(self):
        for q in interview.QUESTIONS:
            text, buttons = interview.ask(q.key)
            self.assertTrue(text)
            for row in buttons:
                for label, data in row:
                    self.assertTrue(data.startswith("b:"))
                    self.assertLessEqual(len(data.encode()), 64)
                    self.assertEqual(interview.answer_of(data), (q.key, data.rsplit(":", 1)[1]))

    def test_an_unknown_answer_is_none(self):
        self.assertIsNone(interview.answer_of("b:kind:nope"))
        self.assertIsNone(interview.answer_of("b:nope:job"))
        self.assertIsNone(interview.answer_of("b:kind"))

    def test_every_option_has_a_fact_and_only_allowed_settings(self):
        for q in interview.QUESTIONS:
            for o in q.options:
                self.assertTrue(o.fact)
                self.assertLessEqual(set(o.settings), {"radius_km", "remote", *config_params()})

    def test_facts_name_each_answer_with_its_key(self):
        facts = interview.facts({"km": "25", "temp": "no"})
        self.assertIn("- km: ", facts)
        self.assertIn("25 km", facts)
        self.assertIn("- temp: ", facts)
        self.assertEqual(interview.facts({}), "(none)")

    def test_apply_writes_the_settings_of_the_answers(self):
        search = {"sources": {"ba": {"keywords": ["Pflege"], "areas": [{"where": "Kassel", "radius_km": 40},
                                                                       {"where": "Baunatal", "radius_km": 10}, {}],
                                     "extra_params": {"pav": "false", "angebotsart": "1;34"}}},
                  "filter": {"include": ["pflege"]}}
        out = interview.apply(search, ANSWERS)
        ba = out["sources"]["ba"]
        self.assertEqual(ba["areas"], [{"where": "Kassel", "radius_km": 25}, {"where": "Baunatal", "radius_km": 25}])
        self.assertEqual(ba["extra_params"], {"pav": "false", "angebotsart": "1", "befristung": "2;3", "zeitarbeit": "false"})
        self.assertEqual(search["sources"]["ba"]["areas"][0]["radius_km"], 40)  # the input stays as it was
        validate_search(out)

    def test_apply_removes_what_an_open_answer_does_not_restrict(self):
        search = {"sources": {"ba": {"keywords": ["Pflege"], "areas": [{"where": "Kassel", "radius_km": 30}],
                                     "extra_params": {"angebotsart": "1", "zeitarbeit": "false", "befristung": "2"}}}}
        out = interview.apply(search, {"kind": "any", "remote": "remote", "term": "any", "temp": "ok"})
        ba = out["sources"]["ba"]
        self.assertEqual(ba["areas"], [{"where": "Kassel", "radius_km": 30}, {}])
        self.assertNotIn("extra_params", ba)

    def test_apply_keeps_a_nationwide_area_when_no_town_is_named(self):
        search = {"sources": {"ba": {"keywords": ["Pflege"], "areas": [{}]}}}
        self.assertEqual(interview.apply(search, {"remote": "site", "km": "10"})["sources"]["ba"]["areas"], [{}])

    def test_apply_leaves_what_the_person_changed_in_the_chat(self):
        search = {"sources": {"ba": {"keywords": ["Pflege"], "areas": [{"where": "Kassel", "radius_km": 50}]}}}
        out = interview.apply(search, {"km": "25", "temp": "no"}, changed=["km"])
        self.assertEqual(out["sources"]["ba"]["areas"], [{"where": "Kassel", "radius_km": 50}])
        self.assertEqual(out["sources"]["ba"]["extra_params"], {"zeitarbeit": "false"})

    def test_apply_ignores_answers_it_does_not_know(self):
        search = {"sources": {"ba": {"keywords": ["Pflege"]}}}
        self.assertEqual(interview.apply(search, {"km": "7", "x": "y"}), search)


def config_params():
    from job_radar.config import _BA_PARAMS
    return _BA_PARAMS


class FactsInTurnTest(unittest.TestCase):
    def test_the_answers_reach_the_model_as_facts(self):
        runner = SequenceRunner('{"say": "Was machst du beruflich?", "done": false}')
        interview.turn([], previous="", model="sonnet", runner=runner, facts=interview.facts({"temp": "no"}))
        prompt = runner.calls[0][1]["input"]
        self.assertIn("- temp: ", prompt)
        self.assertIn("Answers from buttons", prompt)

    def test_a_finished_reply_may_name_answers_the_person_changed(self):
        runner = SequenceRunner(json.dumps({**DONE, "changed": ["km", 3]}))
        reply = interview.turn([], previous="", model="sonnet", runner=runner)
        self.assertEqual(reply["changed"], ["km"])
