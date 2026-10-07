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

    def test_employer_sites_are_those_of_well_enough_rated_postings_best_first(self):
        rows = [("1", "Grün GmbH", "https://gruen.example", 8), ("2", "Grün Verleih", "https://gruen.example", 5),
                ("3", "Blau AG", "https://blau.example", 9), ("4", "Rot KG", "https://rot.example", 3),
                ("5", "Gelb", "", 9), ("6", "Lila", "https://lila.example", None)]
        for ext, company, site, score in rows:
            [p] = self.store.add_new([posting(ext, company=company, website=site, location=f"Ort {ext}")])
            if score is not None:
                self.store.set_score(p.id, score, "")
        self.store.add_new([posting("7", source="initiativ", company="Weiß", website="https://weiss.example")])
        self.assertEqual(self.store.employer_sites(min_score=4),
                         [("Blau AG", "https://blau.example", "Ort 3"), ("Grün GmbH", "https://gruen.example", "Ort 1")])

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


    def test_old_usage_table_gets_measurement_columns(self):  # #17
        import sqlite3
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now')),"
                   " kind TEXT, model TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER,"
                   " duration_ms INTEGER)")
        db.execute("INSERT INTO usage (kind, model, cost_usd) VALUES ('triage', 'haiku', 0.01)")
        db.commit(); db.close()
        store = Store(self.path)
        store.record_usage("triage", {"model": "haiku", "cost_usd": 0.02, "input_tokens": 300, "cache_write": 100,
                                      "cache_read": 150, "ok": 0, "error": "timeout after 300 s"},
                           items=7, poll_id=3, trigger="timer")
        old, new = [dict(r) for r in store.db.execute("SELECT * FROM usage ORDER BY id")]
        self.assertEqual(old["ok"], 1)  # rows from before counted only successful calls
        self.assertIsNone(old["items"])
        self.assertEqual({k: new[k] for k in ("cache_write", "cache_read", "ok", "items", "poll_id", "trigger", "error")},
                         {"cache_write": 100, "cache_read": 150, "ok": 0, "items": 7, "poll_id": 3,
                          "trigger": "timer", "error": "timeout after 300 s"})
        store.close()

    def test_successful_call_is_recorded_as_ok(self):
        store = Store(self.path)
        store.record_usage("report", {"model": "sonnet", "cost_usd": 0.5})
        row = store.db.execute("SELECT ok, items, poll_id, trigger FROM usage").fetchone()
        self.assertEqual(tuple(row), (1, None, None, None))
        store.close()

    def test_poll_row_is_started_then_filled(self):
        store = Store(self.path)
        pid = store.start_poll("suchen")
        self.assertNotEqual(store.start_poll("timer"), pid)
        store.finish_poll(pid, {"fetched": 40, "new": 12, "matched": 5, "pending": 7, "sent": 7, "scored": 6,
                                "omitted": 1, "failed_batches": 0, "page_hits": 2, "page_misses": 1, "notified": 3,
                                "triage_calls": 1, "not_a_column": 9})
        row = dict(store.db.execute("SELECT * FROM polls WHERE id = ?", (pid,)).fetchone())
        self.assertEqual(row["trigger"], "suchen")
        self.assertTrue(row["ts"])
        self.assertEqual({k: row[k] for k in ("fetched", "new", "matched", "pending", "sent", "scored", "omitted",
                                              "failed_batches", "page_hits", "page_misses", "notified")},
                         {"fetched": 40, "new": 12, "matched": 5, "pending": 7, "sent": 7, "scored": 6,
                          "omitted": 1, "failed_batches": 0, "page_hits": 2, "page_misses": 1, "notified": 3})
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


class DuplicatesAndBacklogTest(unittest.TestCase):
    """Fingerprints, rating attempts and expiry, so no posting is paid for twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "radar.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, ext_id, **kw):
        [p] = self.store.add_new([posting(ext_id, location="Kassel", **kw)])
        return p

    def column(self, name, pid):
        return self.store.db.execute(f"SELECT {name} FROM postings WHERE id = ?", (pid,)).fetchone()[0]

    def test_fingerprint_is_stored_with_an_index(self):
        p = self.add("1", title="Kotlin Developer (m/w/d)")
        self.assertEqual(self.column("fingerprint", p.id), "kotlin developer|acme|kassel")
        indexed = [r["name"] for r in self.store.db.execute("PRAGMA index_list(postings)")]
        self.assertIn("postings_fingerprint", indexed)

    def test_old_database_gets_fingerprints_and_counts(self):
        import sqlite3
        self.store.close()
        self.path.unlink()
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE postings (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, external_id TEXT,"
                   " url TEXT, title TEXT, company TEXT, location TEXT, UNIQUE (source, external_id))")
        db.execute("ALTER TABLE postings ADD COLUMN score TEXT")
        db.execute("ALTER TABLE postings ADD COLUMN first_seen TEXT")
        db.execute("INSERT INTO postings (source, external_id, url, title, company, location, score, first_seen)"
                   " VALUES ('ba', '1', 'u', 'Koch', 'Hotel Post GmbH', 'Kassel', '3', datetime('now', '-2 days'))")
        db.execute("INSERT INTO postings (source, external_id, url, title, score) VALUES ('ba', '2', 'u', 'X', 'null')")
        db.execute("CREATE TABLE polls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, trigger TEXT, fetched INTEGER,"
                   " finished TEXT)")
        db.commit(); db.close()
        self.store = Store(self.path)
        self.assertEqual(self.column("fingerprint", 1), "koch|hotel post|kassel")
        self.assertEqual(self.column("attempts", 1), 0)
        # rated before scored_at existed: about when it was found
        self.assertEqual(self.column("scored_at", 1), self.column("first_seen", 1))
        self.assertIsNone(self.column("scored_at", 2))
        self.assertEqual(self.store.scored_twin(self.add("3", title="Koch", company="Hotel Post").id).id, 1)
        pid = self.store.start_poll("timer")
        self.store.finish_poll(pid, {"duplicates": 2, "unscored": 1, "expired": 3})
        row = self.store.db.execute("SELECT duplicates, unscored, expired FROM polls").fetchone()
        self.assertEqual(tuple(row), (2, 1, 3))

    def test_new_title_from_a_report_updates_the_fingerprint(self):
        p = self.add("1")
        self.store.set_meta(p.id, "Open Co", "Backend Engineer")
        self.assertEqual(self.column("fingerprint", p.id), "backend engineer|open co|kassel")

    def test_attempts_count_up(self):
        p = self.add("1")
        self.assertEqual([self.store.add_attempt(p.id) for _ in range(3)], [1, 2, 3])

    def test_pending_postings_older_than_the_search_expire(self):
        old, new, rated = self.add("1"), self.add("2"), self.add("3")
        for x in (old, new, rated):
            self.store.set_status(x.id, "pending")
        self.store.set_status(rated.id, "low")
        self.store.db.execute("UPDATE postings SET first_seen = datetime('now', '-29 days') WHERE id IN (?, ?)",
                              (old.id, rated.id))
        self.assertEqual(self.store.expire_pending(28), 1)
        self.assertEqual([self.store.get(x.id).status for x in (old, new, rated)], ["expired", "pending", "low"])

    def test_scored_twin_is_the_rated_copy_of_the_same_job(self):
        first = self.add("1", source="ba", title="Kotlin Developer (m/w/d)")
        self.store.set_score(first.id, 8, "fits")
        copy = self.add("2", source="arbeitnow", title="Kotlin Developer (w/m/d)", company="ACME GmbH")
        self.assertEqual(self.store.scored_twin(copy.id).id, first.id)
        self.store.mark_duplicate(copy.id, self.store.scored_twin(copy.id))
        got = self.store.get(copy.id)
        self.assertEqual((got.score, got.why, got.status), (8, "fits", "duplicate"))

    def test_no_twin_without_a_rating_or_from_another_job(self):
        unrated = self.add("1")
        self.assertIsNone(self.store.scored_twin(self.add("2").id))
        self.store.set_score(unrated.id, 2, "")
        for kw in ({"location": "Fulda"}, {"company": "Other"}, {"title": "Kotlin Lead"}):
            [other] = self.store.add_new([posting(f"x{len(kw)}{list(kw.values())[0]}", **{"location": "Kassel", **kw})])
            self.assertIsNone(self.store.scored_twin(other.id), kw)
        [nowhere] = self.store.add_new([posting("n1")])
        [nowhere2] = self.store.add_new([posting("n2")])
        self.store.set_score(nowhere.id, 9, "")
        self.assertIsNone(self.store.scored_twin(nowhere2.id))  # no city: never a duplicate

    def test_no_twin_rated_before_the_profile_changed_or_long_ago(self):
        first = self.add("1")
        self.store.set_score(first.id, 2, "")
        copy = self.add("2")
        self.assertIsNone(self.store.scored_twin(copy.id, since="2999-01-01 00:00:00"))
        self.assertEqual(self.store.scored_twin(copy.id, since="2000-01-01 00:00:00").id, first.id)
        self.store.db.execute("UPDATE postings SET scored_at = datetime('now', '-61 days') WHERE id = ?", (first.id,))
        self.assertIsNone(self.store.scored_twin(copy.id))

    def test_a_duplicate_or_a_report_is_no_twin(self):
        first = self.add("1")
        self.store.set_score(first.id, 8, "")
        dup = self.add("2")
        self.store.mark_duplicate(dup.id, self.store.get(first.id))
        self.store.db.execute("UPDATE postings SET scored_at = datetime('now', '-61 days') WHERE id = ?", (first.id,))
        self.assertIsNone(self.store.scored_twin(self.add("3").id))  # the copy does not carry the rating on
        for source in ("manual", "initiativ"):
            [x] = self.store.add_new([posting("m", source=source, location="Kassel")])
            self.store.set_score(x.id, 9, "")
            self.assertIsNone(self.store.scored_twin(self.add(f"4{source}").id), source)

    def test_an_employer_or_a_link_has_no_twin(self):
        rated = self.add("1")
        self.store.set_score(rated.id, 8, "")
        for source in ("manual", "initiativ"):
            [x] = self.store.add_new([posting("m", source=source, location="Kassel")])
            self.assertIsNone(self.store.scored_twin(x.id), source)
