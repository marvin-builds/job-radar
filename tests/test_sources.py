import json
import unittest
from pathlib import Path

from job_radar.sources import arbeitnow, ba

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ArbeitnowTest(unittest.TestCase):
    def test_parse_maps_fields(self):
        postings = arbeitnow.parse(load("arbeitnow.json"))
        self.assertEqual(len(postings), 3)
        p = postings[1]
        self.assertEqual(p.source, "arbeitnow")
        self.assertTrue(p.external_id)
        self.assertTrue(p.url.startswith("https://www.arbeitnow.com/"))
        self.assertEqual(p.company, "Time to hire")
        self.assertTrue(p.remote)
        self.assertEqual(p.contract_type, "freelance")
        self.assertNotIn("&lt;", p.text)
        self.assertNotIn("<p>", p.text)
        self.assertRegex(p.posted_at, r"^\d{4}-\d{2}-\d{2}")

    def test_fetch_follows_next_link_up_to_max_pages(self):
        page = load("arbeitnow.json")
        page["links"]["next"] = "https://www.arbeitnow.com/api/job-board-api?page=2"
        calls = []

        def http_get(url, headers=None):
            calls.append(url)
            return page

        postings = arbeitnow.fetch({"max_pages": 2}, http_get)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(postings), 6)


class BundesagenturTest(unittest.TestCase):
    def test_parse_maps_fields(self):
        postings = ba.parse(load("ba.json"))
        self.assertEqual(len(postings), 2)
        p = postings[0]
        self.assertEqual(p.source, "ba")
        self.assertEqual(p.external_id, "12265-308419_JB5246495-S")
        self.assertEqual(p.url, "https://www.arbeitsagentur.de/jobsuche/jobdetail/12265-308419_JB5246495-S")
        self.assertEqual(p.title, "Kotlin Developer (m/w/d)")
        self.assertEqual(p.company, "FERCHAU GmbH Niederlassung Bielefeld")
        self.assertEqual(p.location, "Gütersloh")
        self.assertEqual(p.contract_type, "permanent")
        self.assertIn("48000", p.salary)
        self.assertEqual(p.posted_at, "2026-09-14")

    def test_parse_prefers_external_url(self):
        p = ba.parse(load("ba.json"))[1]
        self.assertTrue(p.url.startswith("https://www.get-in-it.de/"))

    def test_fetch_runs_one_query_per_keyword_and_area_and_dedupes(self):
        urls = []

        def http_get(url, headers=None):
            urls.append(url)
            self.assertEqual(headers, {"X-API-Key": "jobboerse-jobsuche"})
            return load("ba.json")

        cfg = {"keywords": ["Kotlin", "Java"], "published_since_days": 7,
               "areas": [{"where": "Hamburg", "radius_km": 50}, {}]}
        postings = ba.fetch(cfg, http_get)
        self.assertEqual(len(urls), 4)
        self.assertIn("wo=Hamburg", urls[0])
        self.assertIn("umkreis=50", urls[0])
        self.assertIn("veroeffentlichtseit=7", urls[0])
        self.assertNotIn("wo=", urls[1])
        self.assertEqual(len(postings), 2)
        self.assertIn("nearby", postings[0].tags)
