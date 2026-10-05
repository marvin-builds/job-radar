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
        lines = [
            {"type": "system", "subtype": "init"},
            {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 0.05, "resetsAt": 1791146400},
                "seven_day": {"utilization": 0.03, "resetsAt": 1791702000}}}},
            {"type": "result", "result": self.result, "is_error": self.returncode != 0, "total_cost_usd": 0.0125,
             "duration_ms": 1500,
             "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5990,
                       "cache_read_input_tokens": 4286, "output_tokens": 48}},
        ]
        out = "\n".join(json.dumps(line) for line in lines)
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

    def test_logs_lock_wait_run_time_and_api_time(self):
        with self.assertLogs("job-radar", "INFO") as logs:
            claude.run("p", model="sonnet", tools=[], runner=FakeRunner("hi"), lock_wait=1)
        line = logs.output[-1]
        self.assertIn("sonnet", line)
        self.assertRegex(line, r"lock \d+\.\d s")
        self.assertRegex(line, r"ran \d+\.\d s")
        self.assertIn("API 1.5 s", line)

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
                                 "output_tokens": 48, "duration_ms": 1500,
                                 "five_hour": 0.05, "five_hour_resets": 1791146400,
                                 "seven_day": 0.03, "seven_day_resets": 1791702000}])

    def test_uses_stream_json(self):
        run = FakeRunner("hi")
        claude.run("p", model="haiku", tools=[], runner=run)
        args, _ = run.calls[0]
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", args)

    def test_probe_limits_reports_usage(self):
        seen = []
        claude.probe_limits(runner=FakeRunner("ok"), on_usage=seen.append)
        self.assertEqual(seen[0]["seven_day"], 0.03)

    def test_failure_raises(self):
        with self.assertRaises(claude.ClaudeError):
            claude.run("p", model="haiku", tools=[], runner=FakeRunner("", returncode=1))


class ThinkingTest(unittest.TestCase):
    def test_triage_disables_thinking_report_keeps_it(self):
        run = FakeRunner("[]")
        claude.triage([posting(1)], profile="", model="haiku", runner=run)
        self.assertEqual(run.calls[0][1]["env"]["MAX_THINKING_TOKENS"], "0")
        run = FakeRunner("# R")
        claude.deep_report(posting(1), profile="", model="sonnet", runner=run)
        self.assertNotIn("MAX_THINKING_TOKENS", run.calls[0][1].get("env") or {})

    def test_runs_in_an_empty_working_directory(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            self.addCleanup(setattr, claude, "WORK_DIR", claude.WORK_DIR)
            claude.WORK_DIR = Path(d) / "claude-cwd"
            run = FakeRunner("ok")
            claude.run("p", model="haiku", tools=[], runner=run)
            cwd = Path(run.calls[0][1]["cwd"])
            self.assertEqual(cwd, claude.WORK_DIR)
            self.assertEqual(list(cwd.iterdir()), [])

    def test_every_call_skips_update_check_and_telemetry(self):
        # Saves about 3 s of CLI start-up per call on the Pi.
        for thinking in (True, False):
            run = FakeRunner("ok")
            claude.run("p", model="sonnet", tools=[], runner=run, thinking=thinking)
            env = run.calls[0][1]["env"]
            self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
            self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
            self.assertIn("PATH", env)


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


class DescribeTest(unittest.TestCase):
    def test_missing_remote_flag_is_not_stated_as_onsite(self):
        p = posting(1)
        self.assertIn("remote/hybrid: not stated", claude.describe(p))
        p.remote = True
        self.assertIn("remote/hybrid: yes", claude.describe(p))

    def test_posting_without_text_says_to_rate_the_title(self):
        p = posting(1)
        p.text = ""
        self.assertIn("text:\n(none in this source: rate on title, company and location)", claude.describe(p))

    def test_triage_prompt_demands_scores_even_for_thin_postings(self):
        run = FakeRunner("[]")
        claude.triage([posting(1)], profile="", model="haiku", runner=run)
        self.assertIn("Never ask for more information", run.calls[0][1]["input"])

    def test_triage_prompt_says_missing_info_is_not_negative(self):
        run = FakeRunner("[]")
        claude.triage([posting(1)], profile="", model="haiku", runner=run)
        self.assertIn("not stated", run.calls[0][1]["input"])


class ReportTest(unittest.TestCase):
    def test_report_researches_without_the_profile_and_judges_without_tools(self):
        class Runs:
            def __init__(self):
                self.calls, self.replies = [], ["NOTES about ACME", "# Report\nbody"]

            def __call__(self, args, **kw):
                self.calls.append((args, kw))
                return FakeRunner(self.replies[len(self.calls) - 1])(args, **kw)

        run, usage = Runs(), []
        md = claude.deep_report(posting(1), profile="PROFILE", model="sonnet", runner=run, on_usage=usage.append)
        self.assertEqual(md, "# Report\nbody")
        (research, r_kw), (judge, j_kw) = run.calls
        self.assertEqual(research[research.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertIn("Job 1", r_kw["input"])
        self.assertNotIn("PROFILE", r_kw["input"])  # an injected page cannot leak what the call never saw
        self.assertEqual(judge[judge.index("--tools") + 1], "")
        self.assertIn("PROFILE", j_kw["input"])
        self.assertIn("NOTES about ACME", j_kw["input"])
        self.assertIn("Job 1", j_kw["input"])
        self.assertEqual(len(usage), 2)


class LockTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "claude.lock"
        self.addCleanup(setattr, claude, "LOCK_PATH", claude.LOCK_PATH)
        self.addCleanup(self.tmp.cleanup)

    def held(self):
        import fcntl
        with open(self.path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True

    def test_no_lock_unless_configured(self):
        claude.LOCK_PATH = None
        claude.run("p", model="haiku", tools=[], runner=FakeRunner("hi"))
        self.assertFalse(self.path.exists())

    def test_lock_is_held_while_claude_runs(self):
        claude.LOCK_PATH = self.path
        seen = []
        inner = FakeRunner("hi")

        def runner(args, **kw):
            seen.append(self.held())
            return inner(args, **kw)

        claude.run("p", model="haiku", tools=[], runner=runner)
        self.assertEqual(seen, [True])
        self.assertFalse(self.held())

    def test_gives_up_when_another_call_holds_it_too_long(self):
        import fcntl
        claude.LOCK_PATH = self.path
        with open(self.path, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(claude.ClaudeError):
                claude.run("p", model="haiku", tools=[], runner=FakeRunner("hi"), lock_wait=0.2)


class FindSourcesTest(unittest.TestCase):
    REPLY = """Hier die Börsen:
```json
[{"name": "greenjobs", "url": "https://www.greenjobs.de/angebote/neueste.html?feed=atom", "why": "Umweltjobs"},
 {"name": "lokal", "url": "file:///etc/passwd", "why": "x"},
 {"name": "", "url": "https://a/feed", "why": "kein Name"},
 {"url": "https://b/feed"}]
```"""

    def test_uses_web_tools_and_returns_only_web_feeds_with_a_name(self):
        run = FakeRunner(self.REPLY)
        found = claude.find_sources(profile="Botanik, Stuttgart", known=["https://known/feed"], model="sonnet",
                                    runner=run)
        self.assertEqual(found, [{"name": "greenjobs", "url": "https://www.greenjobs.de/angebote/neueste.html?feed=atom",
                                  "why": "Umweltjobs", "kind": "feed"}])
        args, kw = run.calls[0]
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertIn("Botanik, Stuttgart", kw["input"])
        self.assertIn("https://known/feed", kw["input"])


class ExtractPostingsTest(unittest.TestCase):
    def test_no_tools_and_only_link_items_come_back(self):
        run = FakeRunner('[{"link": 3, "title": "Botaniker", "employer": "NABU", "location": "Stuttgart"}, "junk", {"title": "x"}]')
        found = claude.extract_postings("[3] Botaniker", model="haiku", runner=run)
        self.assertEqual(found, [{"link": 3, "title": "Botaniker", "employer": "NABU", "location": "Stuttgart"}])
        args, kw = run.calls[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("[3] Botaniker", kw["input"])
        self.assertIn("untrusted", kw["input"])

    def test_find_sources_keeps_the_kind(self):
        run = FakeRunner('[{"name": "NABU", "url": "https://nabu.example/jobs", "why": "x", "kind": "page"},'
                         ' {"name": "G", "url": "https://g/feed", "why": "y"}]')
        found = claude.find_sources(profile="p", known=[], model="sonnet", runner=run)
        self.assertEqual([f["kind"] for f in found], ["page", "feed"])
