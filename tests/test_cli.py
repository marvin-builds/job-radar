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
