import json
import subprocess
import unittest

from job_radar import claude
from job_radar.model import Posting


class FakeRunner:
    def __init__(self, result, returncode=0):
        self.result, self.returncode, self.calls = result, returncode, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        out = json.dumps({"result": self.result, "is_error": self.returncode != 0, "total_cost_usd": 0.0125,
                          "duration_ms": 1500,
                          "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5990,
                                    "cache_read_input_tokens": 4286, "output_tokens": 48}})
        return subprocess.CompletedProcess(args, self.returncode, stdout=out, stderr="boom")


def posting(i):
    return Posting(source="t", external_id=str(i), url="u", title=f"Job {i}", company="C", id=i, text="x" * 5000)


class RunTest(unittest.TestCase):
    def test_triage_call_has_no_tools(self):
        run = FakeRunner("hi")
        self.assertEqual(claude.run("prompt", model="haiku", tools=[], runner=run), "hi")
        args, kw = run.calls[0]
        self.assertEqual(args[0], "claude")
        self.assertIn("-p", args)
        self.assertIn("--restricted", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--model") + 1], "haiku")
        self.assertEqual(kw["input"], "prompt")

    def test_tools_are_named_and_allowed(self):
        run = FakeRunner("hi")
        claude.run("p", model="sonnet", tools=["WebSearch", "WebFetch"], runner=run)
        args, _ = run.calls[0]
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(args[args.index("--allowedTools") + 1], "WebSearch,WebFetch")

    def test_usage_is_reported(self):
        seen = []
        claude.run("p", model="haiku", tools=[], runner=FakeRunner("hi"), on_usage=seen.append)
        self.assertEqual(seen, [{"model": "haiku", "cost_usd": 0.0125, "input_tokens": 10 + 5990 + 4286,
                                 "output_tokens": 48, "duration_ms": 1500}])

    def test_failure_raises(self):
        with self.assertRaises(claude.ClaudeError):
            claude.run("p", model="haiku", tools=[], runner=FakeRunner("", returncode=1))


class TriageTest(unittest.TestCase):
    def test_parses_scores_from_fenced_json(self):
        reply = 'Sure:\n```json\n[{"id": 1, "score": 8, "why": "Kotlin backend"}, {"id": 2, "score": 2, "why": "frontend"}]\n```'
        run = FakeRunner(reply)
        scores = claude.triage([posting(1), posting(2)], profile="PROFILE", model="haiku", runner=run)
        self.assertEqual(scores, {1: (8, "Kotlin backend", ""), 2: (2, "frontend", "")})
        prompt = run.calls[0][1]["input"]
        self.assertIn("PROFILE", prompt)
        self.assertIn("Job 2", prompt)
        self.assertLess(len(prompt), 2 * 1500 + 3000)  # posting text is truncated

    def test_returns_employer_description(self):
        reply = '[{"id": 1, "score": 8, "why": "fit", "employer": "Makes elevators"}]'
        run = FakeRunner(reply)
        scores = claude.triage([posting(1)], profile="", model="haiku", runner=run)
        self.assertEqual(scores, {1: (8, "fit", "Makes elevators")})
        self.assertIn("employer", run.calls[0][1]["input"])

    def test_ignores_unknown_ids_and_bad_entries(self):
        reply = '[{"id": 1, "score": "7", "why": "ok"}, {"id": 99, "score": 9}, {"id": 2}]'
        scores = claude.triage([posting(1), posting(2)], profile="", model="haiku", runner=FakeRunner(reply))
        self.assertEqual(scores, {1: (7, "ok", "")})

    def test_unparseable_reply_raises(self):
        with self.assertRaises(claude.ClaudeError):
            claude.triage([posting(1)], profile="", model="haiku", runner=FakeRunner("no json here"))


class ReportTest(unittest.TestCase):
    def test_report_uses_web_tools_and_returns_markdown(self):
        run = FakeRunner("# Report\nbody")
        md = claude.deep_report(posting(1), profile="PROFILE", model="sonnet", runner=run)
        self.assertEqual(md, "# Report\nbody")
        args, kw = run.calls[0]
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertIn("PROFILE", kw["input"])
        self.assertIn("Job 1", kw["input"])
