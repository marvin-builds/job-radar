import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_radar import dedup, measure
from job_radar.model import Posting
from job_radar.sources import ba
from job_radar.store import Store


def posting(ext_id, title="Kotlin Developer", source="t", location="Kassel", **kw):
    return Posting(source=source, external_id=ext_id, url=f"https://x/{ext_id}", title=title, company="ACME",
                   location=location, **kw)


class FingerprintTest(unittest.TestCase):
    def test_same_job_from_two_sources_has_one_fingerprint(self):
        a = Posting(source="ba", external_id="1", url="u", title="Pflegefachkraft (m/w/d) Intensivstation",
                    company="Klinikum Kassel GmbH", location="Kassel, Hessen")
        b = Posting(source="arbeitnow", external_id="x", url="v", title="Pflegefachkraft Intensivstation (w/m/d)",
                    company="Klinikum Kassel", location="Kassel")
        self.assertEqual(dedup.fingerprint(a), dedup.fingerprint(b))

    def test_gendered_forms_legal_forms_and_punctuation_do_not_count(self):
        a = Posting(source="a", external_id="1", url="u", title="Gärtner*in - Staudenpflege",
                    company="NABU e.V.", location="Stuttgart")
        b = Posting(source="b", external_id="2", url="v", title="Gärtner:in Staudenpflege",
                    company="Nabu", location="stuttgart")
        self.assertEqual(dedup.fingerprint(a), dedup.fingerprint(b))

    def test_other_city_or_employer_is_another_job(self):
        a = Posting(source="a", external_id="1", url="u", title="Koch", company="Hotel Post", location="Kassel")
        self.assertNotEqual(dedup.fingerprint(a), dedup.fingerprint(Posting(**{**a.__dict__, "location": "Fulda"})))
        self.assertNotEqual(dedup.fingerprint(a), dedup.fingerprint(Posting(**{**a.__dict__, "company": "Hotel Rose"})))

    def test_no_employer_no_fingerprint(self):
        # feeds and boards without a named employer: a generic title alone says nothing about the job
        a = Posting(source="feed", external_id="a", url="https://a.example/1", title="Erzieher (m/w/d)")
        b = Posting(source="feed", external_id="b", url="https://b.example/2", title="Erzieher*in (w/m/d)",
                    location="Kassel")
        self.assertEqual(dedup.fingerprint(a), "")
        self.assertEqual(dedup.fingerprint(b), "")
        self.assertEqual(dedup.fingerprint(Posting(**{**a.__dict__, "company": "GmbH"})), "")

    def test_no_city_no_fingerprint(self):
        # the same title at the same employer without a city may be another branch's job
        a = Posting(source="a", external_id="1", url="u", title="Koch", company="Hotel Post")
        self.assertEqual(dedup.fingerprint(a), "")
        self.assertEqual(dedup.fingerprint(Posting(**{**a.__dict__, "location": " , Hessen"})), "")

    def test_postal_code_does_not_count(self):
        a = Posting(source="a", external_id="1", url="u", title="Koch", company="Hotel Post", location="34117 Kassel")
        self.assertEqual(dedup.fingerprint(a), dedup.fingerprint(Posting(**{**a.__dict__, "location": "Kassel"})))

    def test_no_title_no_fingerprint(self):
        self.assertEqual(dedup.fingerprint(Posting(source="a", external_id="1", url="u", title=" (m/w/d) ")), "")


class Fixture(unittest.TestCase):
    """A database written by the bot, then read only by the reports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "radar.db"
        self.store = Store(self.path)

    def add(self, p, score=None, status="low"):
        [p] = self.store.add_new([p])
        if score is not None:
            self.store.set_score(p.id, score, "why")
        self.store.set_status(p.id, status)
        return p.id

    def done(self):
        self.store.close()


class OpenReadOnlyTest(Fixture):
    def test_nothing_can_be_written(self):
        self.done()
        db = measure.open_ro(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("INSERT INTO state (key, value) VALUES ('x', 'y')")
        db.close()

    def test_a_missing_database_is_not_created(self):
        with self.assertRaises(sqlite3.OperationalError):
            measure.open_ro(Path(self.tmp.name) / "nope.db")
        self.assertFalse((Path(self.tmp.name) / "nope.db").exists())


class ReplayFilterTest(Fixture):
    FILTER = {"include": ["kotlin"], "exclude_title": ["junior"], "locations": []}

    def test_counts_kept_and_dropped_per_bucket_and_names_good_ones_lost(self):
        self.add(posting("1"), score=8, status="notified")
        self.add(posting("2", title="Junior Kotlin Developer"), score=7, status="notified")  # good, lost
        self.add(posting("3", title="Junior Kotlin Tester"), score=2)
        bookmarked = self.add(posting("4", title="Junior Kotlin Lead"), score=4)
        self.store.mark(bookmarked, "saved")
        self.add(posting("5"), status="filtered")  # never scored: nothing to judge
        self.add(posting("6", source="initiativ", title="Initiativbewerbung"), score=9)  # never filtered
        self.done()
        r = measure.replay_filter(self.path, self.FILTER, threshold=6)
        self.assertEqual((r["replayed"], r["kept"], r["dropped"]), (4, 1, 3))
        self.assertEqual(r["buckets"], {"0-2": [0, 1], "3-5": [0, 1], "6-7": [0, 1], "8-10": [1, 0]})
        self.assertEqual([(x["id"], x["score"], x["bookmarked"]) for x in r["lost"]], [(2, 7, False), (4, 4, True)])

    def test_ba_text_is_cut_back_to_what_the_filter_saw(self):
        # The include word is only in the details added after the filter ran.
        p = posting("1", source="ba", title="Entwickler", text="Berufe: Softwareentwickler")
        p.text += f"\n{ba.HOURS} Vollzeit\n\nWir suchen Kotlin-Kenntnisse"
        self.add(p, score=8, status="notified")
        self.done()
        r = measure.replay_filter(self.path, {**self.FILTER, "include": ["kotlin"]}, threshold=6)
        self.assertEqual(r["dropped"], 1)
        r = measure.replay_filter(self.path, {**self.FILTER, "include": ["softwareentwickler"]}, threshold=6)
        self.assertEqual(r["kept"], 1)

    def test_dedup_counts_ratings_saved_and_good_ones_it_would_hide(self):
        self.add(posting("1", source="ba"), score=8, status="notified")
        self.add(posting("2", source="arbeitnow"), score=7, status="notified")  # copy of a good one: saved
        self.add(posting("3", title="Kotlin Lead"), score=3)
        self.add(posting("4", title="Kotlin Lead (m/w/d)", source="feed"), score=7)  # first copy was low: lost
        self.done()
        r = measure.replay_filter(self.path, self.FILTER, threshold=6, dedup=True)
        self.assertEqual(r["duplicates"], 2)
        self.assertEqual([x["id"] for x in r["dedup_lost"]], [4])
        self.assertEqual([(a["id"], b["id"]) for a, b in r["pairs"]], [(1, 2), (3, 4)])
        text = measure.replay_text(r)
        self.assertIn("#1 score 8 [ba] Kotlin Developer https://x/1", text)
        self.assertIn("#2 score 7 [arbeitnow] Kotlin Developer https://x/2", text)

    def test_dedup_rates_a_same_source_copy_of_a_low_rated_job_again(self):
        self.add(posting("1", source="ba"), score=3)
        self.add(posting("2", source="ba"), score=7)  # rated again: another site or hours, maybe
        self.add(posting("3", source="ba"), score=6)  # copy of #2, already sent: saved
        self.done()
        r = measure.replay_filter(self.path, self.FILTER, threshold=6, dedup=True)
        self.assertEqual((r["duplicates"], r["dedup_lost"]), (1, []))
        self.assertEqual([(a["id"], b["id"]) for a, b in r["pairs"]], [(2, 3)])

    def test_text_names_the_lost_postings(self):
        self.add(posting("2", title="Junior Kotlin Developer"), score=7, status="notified")
        self.done()
        text = measure.replay_text(measure.replay_filter(self.path, self.FILTER, threshold=6, dedup=True))
        self.assertIn("Junior Kotlin Developer", text)
        self.assertIn("dropped 1", text)

    def test_old_database_without_marks_or_newer_columns(self):
        self.done()
        self.path.unlink()
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE postings (id INTEGER PRIMARY KEY, source TEXT, external_id TEXT, url TEXT,"
                   " title TEXT, company TEXT, location TEXT, remote TEXT, contract_type TEXT, text TEXT,"
                   " tags TEXT, score TEXT, status TEXT, first_seen TEXT DEFAULT (datetime('now')))")
        db.execute("INSERT INTO postings (source, external_id, url, title, score, status, tags, remote)"
                   " VALUES ('t', '1', 'u', 'Kotlin Dev', '8', 'notified', '[]', 'false')")
        db.commit(); db.close()
        r = measure.replay_filter(self.path, self.FILTER, threshold=6, dedup=True)
        self.assertEqual((r["replayed"], r["kept"]), (1, 1))


class UsageStatsTest(Fixture):
    def record(self, kind, cost, items=None, ok=1, poll_id=None, **u):
        self.store.record_usage(kind, {"model": "haiku", "cost_usd": cost, "input_tokens": 7000, "cache_write": 0,
                                       "cache_read": 4000, "output_tokens": 300, "duration_ms": 9000, "ok": ok, **u},
                                items=items, poll_id=poll_id, trigger="timer" if poll_id else None)

    def test_cost_per_call_per_posting_and_the_fixed_part(self):
        self.record("triage", 0.015, items=5)
        self.record("triage", 0.020, items=10)
        self.record("triage", 0.020, items=10)
        self.record("triage", 0.0, items=10, ok=0, error="timeout after 300 s")
        self.record("report", 0.25)
        self.done()
        s = measure.usage_stats(self.path, days=7)["kinds"]
        t = s["triage"]
        self.assertEqual((t["calls"], t["failed"]), (4, 1))
        self.assertAlmostEqual(t["cost"], 0.055)
        self.assertAlmostEqual(t["cost_per_item"], 0.055 / 25)  # over the calls that worked
        self.assertAlmostEqual(t["fixed"], 0.010)
        self.assertAlmostEqual(t["per_item"], 0.001)
        self.assertEqual(t["cache_read"], 4000)
        self.assertIsNone(s["report"]["fixed"])  # no item counts: nothing to fit
        self.assertIsNone(s["report"]["cost_per_item"])

    def test_polls_are_summed_with_their_cost(self):
        first = self.store.start_poll("timer")
        self.store.finish_poll(first, {"fetched": 40, "new": 10, "sent": 8, "scored": 7, "omitted": 1,
                                       "triage_calls": 1, "page_hits": 2, "page_misses": 1})
        second = self.store.start_poll("suchen")
        self.store.finish_poll(second, {"fetched": 20, "new": 0, "sent": 0, "triage_calls": 0})
        self.record("triage", 0.02, items=8, poll_id=first)
        self.done()
        p = measure.usage_stats(self.path, days=7)["polls"]
        self.assertEqual(p["polls"], 2)
        self.assertEqual(p["triggers"], {"timer": 1, "suchen": 1})
        self.assertEqual((p["sum"]["fetched"], p["sum"]["omitted"], p["sum"]["page_hits"]), (60, 1, 2))
        self.assertAlmostEqual(p["cost"], 0.02)

    def test_cost_per_trigger_and_crashed_polls(self):
        timer = self.store.start_poll("timer")
        self.store.finish_poll(timer, {"fetched": 1})
        self.store.start_poll("suchen")  # crashed: never finished
        self.record("triage", 0.01, items=2, poll_id=timer)
        self.record("employers", 0.15, poll_id=timer + 1)
        self.done()
        s = measure.usage_stats(self.path, days=7)
        p = s["polls"]
        self.assertEqual(p["unfinished"], 1)
        self.assertAlmostEqual(p["trigger_cost"]["timer"], 0.01)
        self.assertAlmostEqual(p["trigger_cost"]["suchen"], 0.15)
        text = measure.usage_text(s)
        self.assertIn("suchen 1 ($0.15)", text)
        self.assertIn("1 unfinished", text)

    def test_text_shows_kinds_and_polls(self):
        self.record("triage", 0.015, items=5)
        self.record("triage", 0.020, items=10)
        self.done()
        text = measure.usage_text(measure.usage_stats(self.path, days=7))
        self.assertIn("triage", text)
        self.assertIn("fixed", text)
        self.assertIn("polls: none", text)

    def test_old_usage_table_without_the_new_columns(self):
        self.done()
        self.path.unlink()
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now')), kind TEXT,"
                   " model TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER)")
        db.execute("INSERT INTO usage (kind, model, cost_usd, input_tokens, output_tokens, duration_ms)"
                   " VALUES ('triage', 'haiku', 0.016, 6700, 400, 8000)")
        db.commit(); db.close()
        stats = measure.usage_stats(self.path, days=7)
        self.assertEqual((stats["kinds"]["triage"]["calls"], stats["kinds"]["triage"]["failed"]), (1, 0))
        self.assertIsNone(stats["polls"])
        self.assertIn("triage", measure.usage_text(stats))


if __name__ == "__main__":
    unittest.main()
