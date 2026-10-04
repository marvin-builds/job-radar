import json
import tempfile
import unittest
from pathlib import Path

from job_radar import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_defaults(self):
        cfg = config.load(self.dir / "config.json")
        self.assertIn("ba", cfg["sources"])
        self.assertIn("include", cfg["filter"])

    def test_user_values_deep_merge_over_defaults(self):
        (self.dir / "config.json").write_text(json.dumps({"filter": {"include": ["rust"]}, "triage": {"threshold": 3}}))
        cfg = config.load(self.dir / "config.json")
        self.assertEqual(cfg["filter"]["include"], ["rust"])
        self.assertIn("exclude_title", cfg["filter"])
        self.assertEqual(cfg["triage"]["threshold"], 3)
        self.assertIn("model", cfg["triage"])

    def test_paths_expand_user(self):
        cfg = config.load(self.dir / "missing.json")
        self.assertNotIn("~", cfg["db_path"])

    def test_tracker_path_defaults_empty_and_expands(self):
        self.assertEqual(config.load(self.dir / "missing.json")["tracker_path"], "")
        (self.dir / "config.json").write_text(json.dumps({"tracker_path": "~/t.md"}))
        self.assertNotIn("~", config.load(self.dir / "config.json")["tracker_path"])

    def test_load_env_parses_key_values_and_ignores_comments(self):
        env = self.dir / "env"
        env.write_text("# secret\nTELEGRAM_BOT_TOKEN=abc:def\nTELEGRAM_CHAT_ID = 42\n\n")
        self.assertEqual(config.load_env(env), {"TELEGRAM_BOT_TOKEN": "abc:def", "TELEGRAM_CHAT_ID": "42"})
        self.assertEqual(config.load_env(self.dir / "nope"), {})
