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

    def test_defaults_carry_no_field_specific_search_terms(self):
        cfg = config.load(self.dir / "missing.json")
        self.assertEqual(cfg["sources"]["ba"]["keywords"], [])
        self.assertEqual(cfg["filter"]["include"], [])
        self.assertEqual(cfg["filter"]["exclude_title"], [])

    def test_profile_and_reports_default_next_to_config_and_data(self):
        cfg = config.load(self.dir / "config.json")
        self.assertEqual(cfg["profile_files"], [str(self.dir / "profile.md")])
        self.assertTrue(cfg["report"]["reports_dir"].endswith("reports"))

    def test_search_file_sits_between_defaults_and_config(self):
        (self.dir / "search.json").write_text(json.dumps(
            {"filter": {"include": ["pflege"], "locations": ["Kassel"]}}))
        (self.dir / "config.json").write_text(json.dumps({"filter": {"locations": ["Fulda"]}}))
        cfg = config.load(self.dir / "config.json")
        self.assertEqual(cfg["filter"]["include"], ["pflege"])
        self.assertEqual(cfg["filter"]["locations"], ["Fulda"])

    def test_search_file_outside_the_whitelist_is_ignored(self):
        (self.dir / "search.json").write_text(json.dumps({"db_path": "/etc/x", "filter": {"include": ["a"]}}))
        cfg = config.load(self.dir / "config.json")
        self.assertNotEqual(cfg["db_path"], "/etc/x")
        self.assertEqual(cfg["filter"]["include"], [])


class ValidateSearchTest(unittest.TestCase):
    GOOD = {
        "sources": {"ba": {"keywords": ["Pflegefachkraft"],
                           "areas": [{"where": "Kassel", "radius_km": 25, "keywords": ["Pflege"]}, {}],
                           "extra_params": {"arbeitszeit": "tz"}}},
        "filter": {"include": ["pflege"], "exclude_title": ["leitung"], "locations": ["Kassel"],
                   "contract_types": ["permanent", ""]},
    }

    def test_accepts_whitelisted_settings(self):
        self.assertEqual(config.validate_search(self.GOOD), self.GOOD)

    def test_rejects_keys_outside_the_whitelist(self):
        for bad in ({"db_path": "/tmp/x"},
                    {"triage": {"model": "opus"}},
                    {"sources": {"ba": {"enabled": False}}},
                    {"sources": {"arbeitnow": {"max_pages": 99}}},
                    {"sources": {"ba": {"areas": [{"where": "X", "url": "http://evil"}]}}},
                    {"sources": {"ba": {"extra_params": {"host": "evil"}}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate_search(bad)

    def test_rejects_wrong_types(self):
        for bad in ({"filter": {"include": "pflege"}},
                    {"filter": {"include": [1]}},
                    {"sources": {"ba": {"areas": [{"radius_km": "far"}]}}},
                    {"sources": {"ba": {"extra_params": {"arbeitszeit": 1}}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate_search(bad)

    def test_write_search_validates_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "search.json"
            with self.assertRaises(ValueError):
                config.write_search(path, {"db_path": "x"})
            self.assertFalse(path.exists())
            config.write_search(path, self.GOOD)
            self.assertEqual(json.loads(path.read_text()), self.GOOD)


class ScheduleSettingTest(unittest.TestCase):
    def test_schedule_in_search_settings(self):
        config.validate_search({"schedule": {"weekdays": [9, 13], "weekend": []}})
        for bad in ({"weekdays": [3]}, {"weekdays": ["9"]}, {"daily": [9]}, {"weekdays": [True]}):
            with self.assertRaises(ValueError, msg=bad):
                config.validate_search({"schedule": bad})

    def test_default_schedule(self):
        self.assertEqual(config.DEFAULTS["schedule"], {"weekdays": [9, 13, 18], "weekend": [9, 13, 18]})


class SetEnvTest(unittest.TestCase):
    def test_set_env_value_replaces_or_appends_and_keeps_mode(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "env"
            env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=\n")
            env.chmod(0o600)
            config.set_env_value(env, "TELEGRAM_CHAT_ID", "42")
            config.set_env_value(env, "OTHER", "x")
            self.assertEqual(config.load_env(env), {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42", "OTHER": "x"})
            self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)


class ConfigDirTest(unittest.TestCase):
    def test_load_records_the_config_dir_and_interview_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(Path(tmp) / "config.json")
            self.assertEqual(cfg["config_dir"], tmp)
            self.assertEqual(cfg["interview"]["model"], "sonnet")


class InstanceTest(unittest.TestCase):
    def setUp(self):
        import os
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = root

    def test_instance_paths_live_under_instances(self):
        self.assertEqual(config.config_dir("anna"), self.root / "c" / "job-radar" / "instances" / "anna")
        self.assertEqual(config.data_dir("anna"), self.root / "d" / "job-radar" / "instances" / "anna")

    def test_rejects_unsafe_instance_names(self):
        for bad in ("../x", "", "Anna", "a b", "a/b", "-x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.config_dir(bad)

    def test_load_instance_uses_its_own_files_and_data(self):
        config.create_instance("anna")
        (config.config_dir("anna") / "search.json").write_text(json.dumps({"filter": {"locations": ["Kassel"]}}))
        cfg = config.load_instance("anna")
        self.assertEqual(cfg["instance"], "anna")
        self.assertEqual(cfg["filter"]["locations"], ["Kassel"])
        self.assertEqual(cfg["db_path"], str(self.root / "d" / "job-radar" / "instances" / "anna" / "radar.db"))
        self.assertTrue(cfg["report"]["reports_dir"].startswith(str(self.root / "d" / "job-radar" / "instances" / "anna")))
        self.assertEqual(cfg["config_dir"], str(config.config_dir("anna")))

    def test_instances_lists_created_ones(self):
        self.assertEqual(config.instances(), [])
        config.create_instance("ben")
        config.create_instance("anna")
        self.assertEqual(config.instances(), ["anna", "ben"])

    def test_instance_env_layers_shared_token_and_own_chat(self):
        config.create_instance("anna")
        (config.config_dir() / "env").write_text("TELEGRAM_BOT_TOKEN=t\n")
        config.set_env_value(config.config_dir("anna") / "env", "TELEGRAM_CHAT_ID", "42")
        self.assertEqual(config.instance_env("anna"), {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"})

    def test_admin_comes_from_the_operator_file(self):
        self.assertIsNone(config.admin())
        config.config_dir().mkdir(parents=True)
        (config.config_dir() / "operator.json").write_text('{"admin": "anna"}')
        self.assertEqual(config.admin(), "anna")

    def test_next_label_skips_existing_instances(self):
        self.assertEqual(config.next_label("family"), "family1")
        config.create_instance("family1")
        config.create_instance("family3")
        self.assertEqual(config.next_label("family"), "family2")

    def test_open_invites_are_listed_by_label(self):
        config.create_invite("family1")
        code = config.create_invite("family2")
        config.redeem_invite(code)
        self.assertEqual(config.open_invite_names(), ["family1"])

    def test_invite_can_be_redeemed_once(self):
        import os
        code = config.create_invite("anna")
        self.assertIn("anna", config.instances())
        self.assertEqual(os.stat(config.config_dir() / "invites.json").st_mode & 0o777, 0o600)
        self.assertIsNone(config.redeem_invite("wrong"))
        self.assertEqual(config.redeem_invite(code), "anna")
        self.assertIsNone(config.redeem_invite(code))

    def test_counts_open_invites(self):
        self.assertEqual(config.open_invites(), 0)
        code = config.create_invite("anna")
        config.create_invite("bob")
        config.redeem_invite(code)
        self.assertEqual(config.open_invites(), 1)


class FeedSettingsTest(unittest.TestCase):
    def test_defaults_have_an_empty_feed_source(self):
        cfg = config.load(Path(tempfile.mkdtemp()) / "config.json")
        self.assertEqual(cfg["sources"]["feeds"], {"enabled": True, "feeds": []})

    def test_search_settings_may_list_feeds(self):
        good = {"sources": {"feeds": {"feeds": [{"name": "greenjobs", "url": "https://greenjobs.de/feed"}]}}}
        self.assertEqual(config.validate_search(good), good)

    def test_feeds_need_a_web_url_and_nothing_else(self):
        for bad in ({"name": "x", "url": "file:///etc/passwd"},
                    {"name": "x", "url": "https://a", "headers": "x"},
                    {"name": "x", "url": 5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate_search({"sources": {"feeds": {"feeds": [bad]}}})
