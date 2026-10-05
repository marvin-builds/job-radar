import json
import tempfile
import unittest
from pathlib import Path

from job_radar import slot


class SlotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, slot, "PATH", slot.PATH)
        slot.PATH = Path(self.tmp.name) / "interview.slot"

    def test_one_owner_at_a_time(self):
        self.assertTrue(slot.take("anna"))
        self.assertFalse(slot.take("ben"))
        self.assertTrue(slot.take("anna"))
        self.assertTrue(slot.holds("anna"))
        self.assertFalse(slot.holds("ben"))

    def test_release_only_by_the_owner(self):
        slot.take("anna")
        slot.release("ben")
        self.assertFalse(slot.take("ben"))
        slot.release("anna")
        self.assertTrue(slot.take("ben"))

    def test_stale_slot_can_be_taken(self):
        slot.take("anna")
        data = json.loads(slot.PATH.read_text())
        data["at"] -= slot.TIMEOUT_S + 1
        slot.PATH.write_text(json.dumps(data))
        self.assertFalse(slot.holds("anna"))
        self.assertTrue(slot.take("ben"))

    def test_no_path_means_no_limit(self):
        slot.PATH = None
        self.assertTrue(slot.take("anna"))
        self.assertTrue(slot.take("ben"))
        self.assertTrue(slot.holds("ben"))

    def test_broken_file_counts_as_free(self):
        slot.PATH.write_text("{")
        self.assertTrue(slot.take("anna"))


if __name__ == "__main__":
    unittest.main()
