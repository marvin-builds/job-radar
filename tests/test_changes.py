import subprocess
import tempfile
import unittest
from pathlib import Path

from job_radar import changes
from job_radar.store import Store
from tests.test_app import FakeBot

TEXT = """# Neuigkeiten

Intro, not a section.

## 2026-10-07
- Neu: /abbrechen beendet das Gespräch
- Links gehen <schneller>

## 2026-10-05
- Neu: /feedback
"""


class ParseTest(unittest.TestCase):
    def test_sections_newest_first_with_their_bullets(self):
        self.assertEqual(changes.parse(TEXT), [
            ("2026-10-07", ["Neu: /abbrechen beendet das Gespräch", "Links gehen <schneller>"]),
            ("2026-10-05", ["Neu: /feedback"]),
        ])

    def test_repo_changelog_parses(self):
        self.assertTrue(changes.parse(changes.PATH.read_text()))


class AnnounceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "db.sqlite")
        self.addCleanup(self.store.close)
        self.bot = FakeBot()
        self.sections = changes.parse(TEXT)

    def test_existing_person_without_record_gets_only_the_newest_section(self):
        changes.announce(self.store, self.bot, self.sections)
        self.assertEqual(len(self.bot.sent), 1)
        text = self.bot.sent[0][0]
        self.assertIn("• Neu: /abbrechen beendet das Gespräch", text)
        self.assertIn("&lt;schneller&gt;", text)
        self.assertNotIn("/feedback", text)
        self.assertEqual(self.store.get_state(changes.STATE_KEY), "2026-10-07")

    def test_sends_every_section_after_the_last_announced_one(self):
        self.store.set_state(changes.STATE_KEY, "2026-10-01")
        older = self.sections + [("2026-10-01", ["alt"])]
        changes.announce(self.store, self.bot, older)
        text = self.bot.sent[0][0]
        self.assertIn("/abbrechen", text)
        self.assertIn("/feedback", text)
        self.assertNotIn("alt", text)
        self.assertEqual(len(self.bot.sent), 1)

    def test_nothing_new_sends_nothing(self):
        self.store.set_state(changes.STATE_KEY, "2026-10-07")
        changes.announce(self.store, self.bot, self.sections)
        self.assertEqual(self.bot.sent, [])

    def test_no_sections_sends_nothing(self):
        changes.announce(self.store, self.bot, [])
        self.assertEqual(self.bot.sent, [])

    def test_failed_send_is_retried_next_time(self):
        def broken(text, buttons=None):
            raise OSError("offline")
        self.bot.send = broken
        changes.announce(self.store, self.bot, self.sections)
        self.assertEqual(self.store.get_state(changes.STATE_KEY), "")

    def test_mark_seen_skips_the_backlog_for_a_new_person(self):
        changes.mark_seen(self.store, self.sections)
        changes.announce(self.store, self.bot, self.sections)
        self.assertEqual(self.bot.sent, [])


class UnannouncedTest(unittest.TestCase):
    """`unannounced` lists main-line commits that change what people use without telling them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.commit("start", {"README.md": "x"})
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              capture_output=True, text=True, check=True).stdout

    def commit(self, message, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def merge(self, branch, message, files):
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"work on {branch}", files)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", branch, "-m", message)

    def unannounced(self):
        return changes.unannounced(self.base, "HEAD", repo=self.repo)

    def test_code_change_without_section_or_trailer_is_listed(self):
        self.merge("quiet", "Merge branch 'quiet'", {"job_radar/app.py": "1"})
        self.assertEqual([line.split(" ", 1)[1] for line in self.unannounced()], ["Merge branch 'quiet'"])

    def test_changes_section_announces_it(self):
        self.merge("loud", "Merge branch 'loud'", {"prompts/x.md": "1", "CHANGES.md": "## 1\n- neu\n"})
        self.assertEqual(self.unannounced(), [])

    def test_no_news_trailer_with_reason_lets_it_pass(self):
        self.merge("inner", "Merge branch 'inner'\n\nNo-news: internal refactor", {"job_radar/app.py": "1"})
        self.assertEqual(self.unannounced(), [])

    def test_empty_trailer_does_not_count(self):
        self.merge("inner", "Merge branch 'inner'\n\nNo-news:", {"systemd/x.timer": "1"})
        self.assertEqual(len(self.unannounced()), 1)

    def test_docs_and_tests_need_nothing(self):
        self.commit("Docs", {"docs/status.md": "1", "tests/test_x.py": "1"})
        self.assertEqual(self.unannounced(), [])

    def test_each_merge_is_judged_on_its_own(self):
        self.merge("loud", "Merge branch 'loud'", {"job_radar/a.py": "1", "CHANGES.md": "## 1\n- neu\n"})
        self.merge("quiet", "Merge branch 'quiet'", {"job_radar/b.py": "1"})
        self.assertEqual(len(self.unannounced()), 1)

    def test_branch_commits_count_only_through_their_merge(self):
        self.git("checkout", "-q", "-b", "feature")
        self.commit("Code", {"job_radar/a.py": "1"})
        self.commit("Announce it", {"CHANGES.md": "## 1\n- neu\n"})
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "feature", "-m", "Merge branch 'feature'")
        self.assertEqual(self.unannounced(), [])
