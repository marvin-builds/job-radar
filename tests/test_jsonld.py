import json
import unittest
from datetime import date

from job_radar import jsonld


def page(*blocks, raw=None):
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks)
    return f"<html><head>{raw or ''}{scripts}</head><body>Hallo</body></html>"


JOB = {
    "@context": "https://schema.org", "@type": "JobPosting", "title": "Gärtner (m/w/d)",
    "datePosted": "2026-09-30T08:00:00+02:00",
    "description": "<p>Du pflegst &amp; gießt.</p><ul><li>Vollzeit</li></ul>",
    "hiringOrganization": {"@type": "Organization", "name": "Grün GmbH", "sameAs": "https://gruen.example"},
    "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Kassel"}},
    "employmentType": ["FULL_TIME", "TEMPORARY"],
    "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR",
                   "value": {"@type": "QuantitativeValue", "minValue": 3000, "maxValue": 3500, "unitText": "MONTH"}},
}


class JobPostingTest(unittest.TestCase):
    def test_finds_the_posting_among_other_blocks(self):
        found = jsonld.job_posting(page({"@type": "WebSite", "name": "Board"}, JOB))
        self.assertEqual(found["title"], "Gärtner (m/w/d)")

    def test_finds_it_in_a_graph_or_a_list_and_with_a_type_list(self):
        self.assertEqual(jsonld.job_posting(page({"@graph": [{"@type": "Organization"}, JOB]}))["title"],
                         "Gärtner (m/w/d)")
        self.assertEqual(jsonld.job_posting(page([{**JOB, "@type": ["JobPosting"]}]))["title"], "Gärtner (m/w/d)")

    def test_no_posting_bad_json_or_no_script_is_none(self):
        self.assertIsNone(jsonld.job_posting(page({"@type": "WebSite"})))
        self.assertIsNone(jsonld.job_posting(page(raw='<script type="application/ld+json">{oops</script>')))
        self.assertIsNone(jsonld.job_posting("<html>nothing</html>"))

    def test_a_page_with_several_postings_says_nothing_about_which_is_the_link(self):
        other = {**JOB, "title": "Koch (m/w/d)"}
        self.assertIsNone(jsonld.job_posting(page(JOB, other)))
        self.assertIsNone(jsonld.job_posting(page({"@graph": [JOB, other]})))

    def test_the_same_posting_twice_is_still_one(self):
        self.assertEqual(jsonld.job_posting(page(JOB, JOB))["title"], "Gärtner (m/w/d)")

    def test_a_bad_block_does_not_hide_a_good_one(self):
        self.assertIsNotNone(jsonld.job_posting(page(JOB, raw='<script type="application/ld+json">{oops</script>')))


class FieldsTest(unittest.TestCase):
    def test_maps_the_posting(self):
        f = jsonld.fields(JOB, today=date(2026, 10, 7))
        self.assertEqual(f["title"], "Gärtner (m/w/d)")
        self.assertEqual(f["company"], "Grün GmbH")
        self.assertEqual(f["location"], "Kassel")
        self.assertEqual(f["posted_at"], "2026-09-30")
        self.assertEqual(f["website"], "https://gruen.example")
        self.assertEqual(f["salary"], "3000-3500 EUR/MONTH")
        self.assertIn("Du pflegst & gießt.", f["text"])
        self.assertNotIn("<p>", f["text"])
        self.assertIn("employment type: FULL_TIME, TEMPORARY", f["text"])
        self.assertFalse(f["remote"])
        self.assertEqual(f["url"], "")

    def test_gives_the_posting_address_as_written(self):
        self.assertEqual(jsonld.fields({**JOB, "url": ["/jobs/7"]})["url"], "/jobs/7")

    def test_missing_or_odd_values_are_left_empty(self):
        f = jsonld.fields({"@type": "JobPosting", "title": "X", "hiringOrganization": "Acme",
                           "datePosted": "2999-01-01", "jobLocationType": "TELECOMMUTE",
                           "baseSalary": {"currency": "EUR", "value": 50000},
                           "jobLocation": [{"address": {"addressLocality": "Berlin"}},
                                           {"address": {"addressLocality": "Hamburg"}}]},
                          today=date(2026, 10, 7))
        self.assertEqual(f["company"], "Acme")
        self.assertEqual(f["posted_at"], "")  # ahead of today
        self.assertEqual(f["website"], "")
        self.assertEqual(f["text"], "")
        self.assertEqual(f["location"], "Berlin, Hamburg")
        self.assertEqual(f["salary"], "50000 EUR")
        self.assertTrue(f["remote"])

    def test_a_website_must_be_http(self):
        job = {**JOB, "hiringOrganization": {"name": "Grün", "sameAs": "javascript:alert(1)", "url": "ftp://x"}}
        self.assertEqual(jsonld.fields(job, today=date(2026, 10, 7))["website"], "")

    def test_a_list_of_organizations_or_sites_takes_the_first(self):
        job = {**JOB, "hiringOrganization": [{"name": "Grün", "sameAs": ["https://a.example", "https://b.example"]}]}
        f = jsonld.fields(job, today=date(2026, 10, 7))
        self.assertEqual((f["company"], f["website"]), ("Grün", "https://a.example"))


if __name__ == "__main__":
    unittest.main()


class JobPostingsTest(unittest.TestCase):
    def test_every_posting_of_a_list_page_also_inside_an_item_list(self):
        other = {**JOB, "title": "Koch (m/w/d)"}
        third = {**JOB, "title": "Förster (m/w/d)"}
        found = jsonld.job_postings(page(JOB, {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "item": other}, third,
            {"@type": "ListItem", "position": 3, "url": "https://b/kategorie", "name": "Kategorie"}]}))
        self.assertEqual([j["title"] for j in found], ["Gärtner (m/w/d)", "Koch (m/w/d)", "Förster (m/w/d)"])

    def test_the_same_posting_twice_counts_once_and_bad_blocks_are_skipped(self):
        found = jsonld.job_postings(page(JOB, JOB, raw='<script type="application/ld+json">{oops</script>'))
        self.assertEqual(len(found), 1)
        self.assertEqual(jsonld.job_postings("<html>nothing</html>"), [])
