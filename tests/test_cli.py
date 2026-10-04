import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from job_radar import cli


class DoctorTest(unittest.TestCase):
    def test_reports_missing_secrets_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"db_path": str(Path(tmp) / "db"), "profile_files": [str(Path(tmp) / "nope.md")],
                   "report": {"reports_dir": tmp}, "sources": {}}
            out = io.StringIO()
            with redirect_stdout(out):
                ok = cli.doctor(cfg, env={}, which=lambda name: "/usr/bin/claude")
        text = out.getvalue()
        self.assertFalse(ok)
        self.assertIn("TELEGRAM_BOT_TOKEN", text)
        self.assertIn("nope.md", text)
        self.assertIn("claude", text)


class FindChatsTest(unittest.TestCase):
    def test_lists_private_chats_from_updates(self):
        updates = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "M"}, "text": "hi"}},
            {"update_id": 2, "message": {"chat": {"id": 42, "type": "private", "first_name": "M"}, "text": "again"}},
            {"update_id": 3, "message": {"chat": {"id": -5, "type": "group", "title": "G"}, "text": "x"}},
        ]}
        self.assertEqual(cli.find_chats(lambda url, payload, timeout=60: updates), [(42, "M")])
