import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from job_radar import claude, cli, config
from job_radar.store import Store


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


class MainTest(unittest.TestCase):
    """main() against temp XDG dirs with one instance "anna" whose invite is not redeemed yet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("JOB_RADAR_INSTANCE", None)
        old_lock = claude.LOCK_PATH
        self.addCleanup(setattr, claude, "LOCK_PATH", old_lock)
        config.config_dir().mkdir(parents=True)
        (config.config_dir() / "env").write_text("TELEGRAM_BOT_TOKEN=t\n")
        config.create_instance("anna")

    def run_main(self, *argv, **kw) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv), **kw)
        return code, out.getvalue()

    def test_poll_skips_an_instance_without_chat(self):
        code, out = self.run_main("--instance", "anna", "poll")
        self.assertEqual(code, 0)
        self.assertIn("skipped: no chat yet", out)

    def test_instance_comes_from_env_when_flag_is_missing(self):
        with mock.patch.dict(os.environ, {"JOB_RADAR_INSTANCE": "anna"}):
            code, out = self.run_main("poll")
        self.assertIn("skipped: no chat yet", out)

    def test_flag_wins_over_env(self):
        with mock.patch.dict(os.environ, {"JOB_RADAR_INSTANCE": "bob"}):
            code, out = self.run_main("--instance", "anna", "poll")
        self.assertIn("skipped: no chat yet", out)

    def test_per_instance_command_without_instance_fails_clearly(self):
        with self.assertRaises(SystemExit) as e:
            self.run_main("poll")
        self.assertIn("--instance", str(e.exception.code))

    def test_unknown_instance_fails_clearly(self):
        with self.assertRaises(SystemExit) as e:
            self.run_main("--instance", "bob", "poll")
        self.assertIn("bob", str(e.exception.code))
        self.assertIn("invite", str(e.exception.code))

    def test_dry_run_works_without_chat(self):
        with mock.patch.object(cli.app, "poll", return_value={"new": 0}) as poll:
            code, out = self.run_main("--instance", "anna", "poll", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(poll.call_args.args[0]["instance"], "anna")
        self.assertTrue(poll.call_args.kwargs["dry_run"])

    def test_invite_prints_link_and_timer_hint(self):
        calls = []

        def post(url, payload, timeout=60):
            calls.append(url)
            return {"ok": True, "result": {"username": "radar_bot"}}

        code, out = self.run_main("invite", "bob", post=post)
        self.assertEqual(code, 0)
        self.assertTrue(calls[0].endswith("/getMe"))
        [(code_, name)] = json.loads((config.config_dir() / "invites.json").read_text()).items()
        self.assertEqual(name, "bob")
        self.assertIn(f"https://t.me/radar_bot?start={code_}", out)
        self.assertIn("systemctl --user enable --now job-radar-poll@bob.timer", out)

    def test_main_sets_the_shared_claude_lock(self):
        self.run_main("doctor", which=lambda name, path=None: None)
        self.assertEqual(claude.LOCK_PATH, config.data_dir() / "claude.lock")

    def test_shared_doctor_lists_instances_and_open_invites(self):
        config.create_invite("bob")
        code, out = self.run_main("doctor", which=lambda name: "/usr/bin/claude")
        self.assertIn("anna", out)
        self.assertIn("no chat", out)
        self.assertIn("1 open invite", out)
        self.assertEqual(code, 0)

    def test_instance_doctor_points_to_setup_for_missing_profile(self):
        config.set_env_value(config.config_dir("anna") / "env", "TELEGRAM_CHAT_ID", "42")
        code, out = self.run_main("--instance", "anna", "doctor", which=lambda name: "/usr/bin/claude")
        self.assertIn("run /setup in Telegram", out)
        self.assertNotIn("MISSING", out)
        self.assertEqual(code, 0)

    def test_doctor_finds_claude_where_the_services_look(self):
        # Over plain SSH, PATH lacks ~/.local/bin; the systemd units add it.
        def which(name, path=None):
            return "/home/x/.local/bin/claude" if path and ".local/bin" in path else None
        code, out = self.run_main("doctor", which=which)
        self.assertIn("ok   claude", out)
        self.assertEqual(code, 0)

    def test_listen_without_token_fails_clearly(self):
        (config.config_dir() / "env").write_text("")
        with self.assertRaises(SystemExit) as e:
            self.run_main("listen")
        self.assertIn("TELEGRAM_BOT_TOKEN", str(e.exception.code))


class BusyTest(unittest.TestCase):
    """`busy` tells deploy.sh whether a listener restart would cut off a running interview turn."""

    setUp = MainTest.setUp
    run_main = MainTest.run_main

    def interview(self, state, *roles, bound=True):
        if bound:
            config.set_env_value(config.config_dir("anna") / "env", "TELEGRAM_CHAT_ID", "1")
        store = Store(Path(config.load_instance("anna")["db_path"]))
        store.set_state("interview", state)
        for role in roles:
            store.interview_add(role, "x")
        store.close()

    def test_idle_without_interviews(self):
        self.assertEqual(self.run_main("busy"), (0, ""))

    def test_busy_while_the_bot_owes_an_answer(self):
        self.interview("active", "bot", "person")
        code, out = self.run_main("busy")
        self.assertEqual(code, 1)
        self.assertIn("anna", out)

    def test_idle_when_the_bot_has_answered(self):
        self.interview("active", "bot", "person", "bot")
        self.assertEqual(self.run_main("busy"), (0, ""))

    def test_instance_without_chat_never_blocks(self):
        self.interview("active", "person", bound=False)
        self.assertEqual(self.run_main("busy"), (0, ""))
