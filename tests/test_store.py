import tempfile
import unittest
from pathlib import Path

from job_radar.model import Posting
from job_radar.store import Store


def posting(ext_id="1", **kw):
    base = dict(source="test", external_id=ext_id, url=f"https://x/{ext_id}",
                title="Kotlin Developer", company="ACME")
    base.update(kw)
    return Posting(**base)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "radar.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_new_returns_only_unseen_postings_with_ids(self):
        first = self.store.add_new([posting("1"), posting("2")])
        self.assertEqual([p.external_id for p in first], ["1", "2"])
        self.assertTrue(all(p.id for p in first))
        second = self.store.add_new([posting("2"), posting("3")])
        self.assertEqual([p.external_id for p in second], ["3"])

    def test_same_external_id_from_other_source_is_new(self):
        self.store.add_new([posting("1")])
        self.assertEqual(len(self.store.add_new([posting("1", source="other")])), 1)

    def test_round_trip_keeps_fields_score_and_status(self):
        [p] = self.store.add_new([posting("1", remote=True, tags=["a", "b"], text="body")])
        self.store.set_score(p.id, 8, "good fit")
        self.store.set_status(p.id, "interesting")
        got = self.store.get(p.id)
        self.assertEqual((got.title, got.remote, got.tags, got.text), ("Kotlin Developer", True, ["a", "b"], "body"))
        self.assertEqual((got.score, got.why, got.status), (8, "good fit", "interesting"))

    def test_sql_null_values_are_read_as_empty(self):
        [p] = self.store.add_new([posting("1")])
        self.store.db.execute("UPDATE postings SET score = NULL, tags = NULL, remote = NULL WHERE id = ?", (p.id,))
        got = self.store.get(p.id)
        self.assertEqual((got.score, got.tags, got.remote), (None, [], False))

    def test_get_unknown_id_returns_none(self):
        self.assertIsNone(self.store.get(999))

    def test_db_parent_directory_is_created(self):
        nested = Store(Path(self.tmp.name) / "a" / "b" / "radar.db")
        nested.close()
        self.assertTrue((Path(self.tmp.name) / "a" / "b" / "radar.db").exists())


class MigrationAndUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "radar.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_database_gets_new_columns(self):
        import sqlite3
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE postings (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, external_id TEXT,"
                   " url TEXT, title TEXT, UNIQUE (source, external_id))")
        db.commit(); db.close()
        store = Store(self.path)
        [p] = store.add_new([posting("1", employer="Builds elevators")])
        self.assertEqual(store.get(p.id).employer, "Builds elevators")
        store.close()

    def test_usage_summary_groups_by_kind(self):
        store = Store(self.path)
        store.record_usage("triage", {"model": "haiku", "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 10})
        store.record_usage("triage", {"model": "haiku", "cost_usd": 0.02, "input_tokens": 200, "output_tokens": 20})
        store.record_usage("report", {"model": "sonnet", "cost_usd": 0.5, "input_tokens": 1000, "output_tokens": 500})
        summary = store.usage_summary(days=7)
        self.assertEqual(summary["triage"]["calls"], 2)
        self.assertAlmostEqual(summary["triage"]["cost_usd"], 0.03)
        self.assertEqual(summary["triage"]["input_tokens"], 300)
        self.assertEqual(summary["report"]["output_tokens"], 500)
        self.assertEqual(store.usage_summary(days=0), {})
        store.close()

    def test_latest_limits(self):
        store = Store(self.path)
        self.assertIsNone(store.latest_limits())
        store.record_usage("triage", {"model": "haiku", "cost_usd": 0.01, "five_hour": 0.05,
                                      "five_hour_resets": 100, "seven_day": 0.03, "seven_day_resets": 200})
        store.record_usage("report", {"model": "sonnet", "cost_usd": 0.5})  # no limit info
        limits = store.latest_limits()
        self.assertEqual((limits["five_hour"], limits["seven_day"], limits["seven_day_resets"]), (0.05, 0.03, 200))
        self.assertLess(limits["age_s"], 60)
        store.close()

    def test_old_usage_table_gets_limit_columns(self):
        import sqlite3
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now')),"
                   " kind TEXT, model TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER,"
                   " duration_ms INTEGER)")
        db.commit(); db.close()
        store = Store(self.path)
        store.record_usage("triage", {"model": "haiku", "five_hour": 0.1, "seven_day": 0.2})
        self.assertEqual(store.latest_limits()["seven_day"], 0.2)
        store.close()


class PollLockTest(unittest.TestCase):
    def test_second_lock_fails_while_first_is_held(self):
        from job_radar.store import PollLock, PollLocked
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poll.lock"
            with PollLock(path):
                with self.assertRaises(PollLocked):
                    with PollLock(path):
                        pass
            with PollLock(path):  # released again
                pass


class InterviewStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "radar.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_interview_log_keeps_order_and_survives_reopen(self):
        self.store.interview_add("bot", "Hallo, was suchst du?")
        self.store.interview_add("person", "Pflege in Kassel")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.interview_log(), [("bot", "Hallo, was suchst du?"), ("person", "Pflege in Kassel")])
        self.store.interview_clear()
        self.assertEqual(self.store.interview_log(), [])

    def test_state_values_default_empty_and_overwrite(self):
        self.assertEqual(self.store.get_state("interview"), "")
        self.store.set_state("interview", "active")
        self.store.set_state("interview", "proposed")
        self.assertEqual(self.store.get_state("interview"), "proposed")


class ThreadTest(unittest.TestCase):
    """The listener's chat workers and background jobs share one Store (#23)."""

    def test_concurrent_use_from_many_threads(self):
        import tempfile
        import threading
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "db.sqlite")
            errors = []

            def work(n):
                try:
                    for i in range(60):
                        store.set_state(f"k{n}", str(i))
                        store.get_state(f"k{(n + 1) % 8}")
                        store.states("k")
                        store.record_usage("t", {"model": "m", "cost_usd": 0.0, "input_tokens": 1,
                                                 "output_tokens": 1, "duration_ms": 1})
                        store.usage_summary(1)
                except Exception as e:
                    errors.append(repr(e))

            threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            store.close()
        self.assertEqual(errors, [])


class MarksTest(unittest.TestCase):
    """Bookmarks, applications and their follow-up (#26)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "db.sqlite")
        self.addCleanup(self.store.close)
        self.ids = [p.id for p in self.store.add_new([
            Posting(source="t", external_id=str(i), url=f"https://x/{i}", title=f"Job {i}", company="ACME")
            for i in range(4)])]

    def test_bookmarks_survive_a_report_and_list_newest_first(self):
        a, b = self.ids[:2]
        self.store.mark(a, "saved")
        self.store.mark(b, "saved")
        self.store.set_status(a, "reported")  # a report must not drop the bookmark
        self.assertEqual([p.id for p, _ in self.store.marked("saved")], [b, a])

    def test_mark_carries_when_the_posting_was_found(self):
        self.store.mark(self.ids[0], "saved")
        [(_, mark)] = self.store.marked("saved")
        self.assertRegex(mark["first_seen"], r"^\d{4}-\d{2}-\d{2} ")

    def test_applying_moves_it_and_sets_a_reminder(self):
        a = self.ids[0]
        self.store.mark(a, "saved")
        self.store.mark(a, "applied", remind_in_days=14)
        self.assertEqual(self.store.marked("saved"), [])
        [(p, mark)] = self.store.marked("applied")
        self.assertEqual(p.id, a)
        self.assertEqual(self.store.due_reminders(), [])
        self.assertEqual([p.id for p, _ in self.store.due_reminders(days_ahead=15)], [a])

    def test_outcome_and_asking_again(self):
        a = self.ids[0]
        self.store.mark(a, "applied", remind_in_days=0)
        self.assertEqual(len(self.store.due_reminders()), 1)
        self.store.remind_later(a, days=14)
        self.assertEqual(self.store.due_reminders(), [])
        self.store.set_outcome(a, "gespraech")
        self.assertEqual(self.store.due_reminders(days_ahead=30), [])
        self.assertEqual(self.store.marked("applied")[0][1]["outcome"], "gespraech")

    def test_likes_and_dislikes_for_triage(self):
        a, b, c, d = self.ids
        self.store.mark(a, "saved")
        self.store.mark(b, "applied", remind_in_days=14)
        self.store.mark(c, "removed")
        self.assertEqual(sorted(p.id for p in self.store.liked(10)), [a, b])
        self.assertEqual([p.id for p in self.store.disliked(10)], [c])
        self.assertEqual(len(self.store.liked(1)), 1)
