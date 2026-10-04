import json
import unittest

from job_radar import interview
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
