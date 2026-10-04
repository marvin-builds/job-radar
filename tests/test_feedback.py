import unittest
from datetime import datetime

from job_radar import feedback

CFG = {"instance": "family1", "feedback": {"repo": "owner/job-radar"}}
ENV = {"GITHUB_TOKEN": "secret"}
NOW = lambda: datetime(2026, 10, 5, 9, 30)


class FakePost:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        return {"html_url": "https://github.com/owner/job-radar/issues/9"}


class SendTest(unittest.TestCase):
    def test_creates_an_issue_with_label_and_instance(self):
        post = FakePost()
        url = feedback.send(CFG, "Die Karten sind zu lang.\nSonst super", env=ENV, post=post, now=NOW)
        self.assertEqual(url, "https://github.com/owner/job-radar/issues/9")
        api, payload, headers = post.calls[0]
        self.assertEqual(api, "https://api.github.com/repos/owner/job-radar/issues")
        self.assertEqual(payload["title"], "Feedback von family1: Die Karten sind zu lang.")
        self.assertEqual(payload["labels"], ["feedback"])
        self.assertIn("Die Karten sind zu lang.\nSonst super", payload["body"])
        self.assertIn("family1", payload["body"])
        self.assertIn("2026-10-05 09:30", payload["body"])
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_long_first_line_is_cut_in_the_title(self):
        post = FakePost()
        feedback.send(CFG, "x" * 200, env=ENV, post=post, now=NOW)
        title = post.calls[0][1]["title"]
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("…"))

    def test_not_configured_without_repo_or_token(self):
        for cfg, env in (({**CFG, "feedback": {"repo": ""}}, ENV), (CFG, {})):
            with self.assertRaises(feedback.NotConfigured):
                feedback.send(cfg, "hallo", env=env, post=FakePost(), now=NOW)


if __name__ == "__main__":
    unittest.main()
