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

    def test_get_unknown_id_returns_none(self):
        self.assertIsNone(self.store.get(999))

    def test_db_parent_directory_is_created(self):
        nested = Store(Path(self.tmp.name) / "a" / "b" / "radar.db")
        nested.close()
        self.assertTrue((Path(self.tmp.name) / "a" / "b" / "radar.db").exists())
