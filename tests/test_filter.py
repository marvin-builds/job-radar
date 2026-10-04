import unittest

from job_radar.filter import matches
from job_radar.model import Posting

CFG = {
    "include": ["kotlin", "java", "backend"],
    "exclude_title": ["junior", "werkstudent"],
    "locations": ["Hamburg"],
    "contract_types": ["permanent", "freelance", ""],
}


def posting(**kw):
    base = dict(source="t", external_id="1", url="u", title="Senior Kotlin Engineer", location="Hamburg")
    base.update(kw)
    return Posting(**base)


class FilterTest(unittest.TestCase):
    def test_accepts_matching_posting(self):
        self.assertTrue(matches(posting(), CFG))

    def test_include_matches_title_tags_or_text_case_insensitive(self):
        self.assertTrue(matches(posting(title="Engineer", tags=["Backend"]), CFG))
        self.assertTrue(matches(posting(title="Engineer", text="We use JAVA 21"), CFG))
        self.assertFalse(matches(posting(title="Engineer", text="Python shop"), CFG))

    def test_include_needs_word_boundary(self):
        self.assertFalse(matches(posting(title="JavaScript Developer"), CFG))

    def test_exclude_title_wins(self):
        self.assertFalse(matches(posting(title="Junior Kotlin Developer"), CFG))

    def test_location_remote_or_listed_or_nearby_tag(self):
        self.assertFalse(matches(posting(location="München"), CFG))
        self.assertTrue(matches(posting(location="München", remote=True), CFG))
        self.assertTrue(matches(posting(location="Norderstedt", tags=["nearby"]), CFG))
        self.assertTrue(matches(posting(location="München"), {**CFG, "locations": []}))

    def test_nearby_postings_can_skip_include_keywords(self):
        local = posting(title="Informatiker (m/w/d)", tags=["nearby"])
        self.assertFalse(matches(local, CFG))
        self.assertTrue(matches(local, {**CFG, "nearby_skips_include": True}))
        junior = posting(title="Junior Informatiker", tags=["nearby"])
        self.assertFalse(matches(junior, {**CFG, "nearby_skips_include": True}))

    def test_listed_location_counts_as_local_for_skipping_include(self):
        cfg = {**CFG, "nearby_skips_include": True}
        self.assertTrue(matches(posting(title="Informatiker (m/w/d)", location="Hamburg"), cfg))
        self.assertFalse(matches(posting(title="Informatiker (m/w/d)", location="Berlin", remote=True), cfg))

    def test_contract_type(self):
        self.assertFalse(matches(posting(contract_type="temporary"), CFG))

    def test_posting_without_location_field_matches_a_location_in_title_or_text(self):
        self.assertTrue(matches(posting(location="", title="Kotlin Engineer (Hamburg) - ACME"), CFG))
        self.assertTrue(matches(posting(location="", text="Kotlin, Standort Hamburg-Altona"), CFG))
        self.assertFalse(matches(posting(location="", title="Kotlin Engineer (Köln) - ACME"), CFG))
        self.assertFalse(matches(posting(location="Köln", title="Kotlin Engineer, nicht Hamburg"), CFG))
