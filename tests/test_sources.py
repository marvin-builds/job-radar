import json
import unittest
from pathlib import Path

from job_radar.sources import arbeitnow, ba, feeds

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

        postings = arbeitnow.fetch({"max_pages": 2, "page_delay_s": 0}, http_get)
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
        self.assertEqual(sum("wo=Hamburg" in u for u in urls), 2)
        self.assertEqual(len(postings), 2)
        self.assertIn("nearby", postings[0].tags)

    def test_area_keywords_override_source_keywords(self):
        urls = []
        cfg = {"keywords": ["Kotlin"], "areas": [{"where": "Stuttgart", "keywords": ["Python", "Informatiker"]}, {}]}
        ba.fetch(cfg, lambda url, headers=None: urls.append(url) or {"ergebnisliste": []})
        self.assertEqual(len(urls), 3)
        self.assertTrue(all("wo=Stuttgart" in u for u in urls[:2]))
        self.assertIn("was=Python", urls[0])
        self.assertIn("was=Kotlin", urls[2])
        self.assertNotIn("wo=", urls[2])

    def test_fetch_passes_extra_params(self):
        urls = []
        cfg = {"keywords": ["Java"], "extra_params": {"zeitarbeit": "false", "pav": "false"}}
        ba.fetch(cfg, lambda url, headers=None: urls.append(url) or {"ergebnisliste": []})
        self.assertIn("zeitarbeit=false", urls[0])
        self.assertIn("pav=false", urls[0])


class FeedsTest(unittest.TestCase):
    def test_parse_atom(self):
        postings = feeds.parse((FIXTURES / "feed-atom.xml").read_text(encoding="utf-8"), "greenjobs")
        self.assertEqual(len(postings), 2)
        p = postings[0]
        self.assertEqual(p.source, "feed")
        self.assertEqual(p.external_id, "https://www.greenjobs.de/angebote/index.html?id=1&anz=html")
        self.assertEqual(p.url, "https://www.greenjobs.de/angebote/index.html?id=1&anz=html")
        self.assertIn("(Stuttgart)", p.title)
        self.assertEqual(p.location, "")
        self.assertEqual(p.tags, ["greenjobs"])
        self.assertEqual(p.posted_at, "2026-10-02T19:48:27+02:00")
        self.assertEqual(postings[1].url, "https://www.greenjobs.de/angebote/index.html?id=2")
        self.assertEqual(postings[1].text, "Rinder & Weide")

    def test_parse_rss(self):
        postings = feeds.parse((FIXTURES / "feed-rss.xml").read_text(encoding="utf-8"), "verband")
        self.assertEqual([p.title for p in postings], ["Aushilfe Biotoppflege", "Ohne Guid"])
        self.assertEqual(postings[0].external_id, "job-7")
        self.assertEqual(postings[0].text, "Teilzeit in Esslingen")
        self.assertEqual(postings[1].external_id, "https://example.org/jobs/8")

    def test_parse_rejects_what_is_not_a_feed(self):
        with self.assertRaises(ValueError):
            feeds.parse("<html><body>Jobs</body></html>", "x")
        with self.assertRaises(ValueError):
            feeds.parse("not xml", "x")

    def test_fetch_reads_every_feed_and_skips_a_broken_one(self):
        texts = {"https://a/feed": (FIXTURES / "feed-rss.xml").read_text(encoding="utf-8")}

        def get_text(url):
            if url not in texts:
                raise OSError("404")
            return texts[url]

        cfg = {"feeds": [{"name": "broken", "url": "https://b/feed"}, {"name": "a", "url": "https://a/feed"}]}
        postings = feeds.fetch(cfg, http_get=None, get_text=get_text)
        self.assertEqual(len(postings), 2)
        self.assertEqual(postings[0].tags, ["a"])
