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
