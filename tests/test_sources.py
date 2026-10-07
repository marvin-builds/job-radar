import json
import unittest
from pathlib import Path

from job_radar.sources import arbeitnow, ba, feeds, pages

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

    def test_parse_maps_internships_and_apprenticeships(self):
        jobs = [{"referenznummer": str(i), "stellenangebotsart": kind}
                for i, kind in enumerate(["PRAKTIKUM_TRAINEE", "AUSBILDUNG", "SELBSTSTAENDIGKEIT", "NEU"])]
        kinds = [p.contract_type for p in ba.parse({"ergebnisliste": jobs})]
        self.assertEqual(kinds, ["internship", "apprenticeship", "freelance", ""])

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


class BundesagenturDetailsTest(unittest.TestCase):
    def posting(self):
        return ba.parse(load("ba.json"))[0]

    def test_details_put_working_hours_first_then_the_description(self):
        text = ba.details_text(load("ba-detail.json"))
        self.assertTrue(text.startswith("Arbeitszeit: Teilzeit (vormittags, nachmittags, abends)"))
        self.assertIn("Art: Praktikum/Trainee", text)
        self.assertIn("15 bis 20 Stunden pro Woche", text)

    def test_details_name_full_time_and_shifts(self):
        text = ba.details_text({"arbeitszeitVollzeit": True, "arbeitszeitSchichtNachtWochenende": True,
                                "stellenangebotsart": "ARBEIT", "vertragsdauer": "BEFRISTET"})
        self.assertIn("Arbeitszeit: Vollzeit, Schicht/Nacht/Wochenende", text)
        self.assertIn("Vertrag: befristet", text)

    def test_details_name_start_main_occupation_and_staffing_before_the_description(self):
        job = {**load("ba-detail.json"), "istArbeitnehmerUeberlassung": True,
               "eintrittszeitraum": {"von": "2026-11-01", "bis": "2026-12-01"}}
        text = ba.details_text(job)
        head = text[:text.index("Auf Ihr Engagement")]
        self.assertIn("Eintritt: 2026-11-01 bis 2026-12-01\n", head)
        self.assertIn("Hauptberuf: Verkäufer/in\n", head)
        self.assertIn("Zeitarbeit: ja (Arbeitnehmerüberlassung)\n", head)
        text = ba.details_text(load("ba-detail.json"))
        self.assertIn("Eintritt: ab 2026-11-01\n", text)
        self.assertNotIn("Zeitarbeit", text)
        self.assertEqual(ba.details_text({}), "Arbeitszeit: keine Angabe")

    def test_enrich_tags_staffing_firms(self):
        p = self.posting()
        ba.enrich(p, lambda url, headers=None: {**load("ba-detail.json"), "istArbeitnehmerUeberlassung": True})
        self.assertIn("Zeitarbeit", p.tags)
        p = self.posting()
        ba.enrich(p, lambda url, headers=None: load("ba-detail.json"))
        self.assertNotIn("Zeitarbeit", p.tags)

    def test_enrich_fetches_the_details_by_base64_reference(self):
        calls = []

        def http_get(url, headers=None):
            calls.append((url, headers))
            return load("ba-detail.json")

        p = self.posting()
        ba.enrich(p, http_get)
        url, headers = calls[0]
        self.assertEqual(url, ba.DETAILS + "MTIyNjUtMzA4NDE5X0pCNTI0NjQ5NS1T")
        self.assertEqual(headers, {"X-API-Key": "jobboerse-jobsuche"})
        self.assertTrue(p.text.startswith("Berufe: "))
        self.assertIn("Arbeitszeit: Teilzeit", p.text)

    def test_enrich_skips_postings_that_already_have_details(self):
        calls = []
        p = self.posting()
        ba.enrich(p, lambda url, headers=None: calls.append(url) or load("ba-detail.json"))
        ba.enrich(p, lambda url, headers=None: calls.append(url) or load("ba-detail.json"))
        self.assertEqual(len(calls), 1)


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


BOARD = """<html><head><title>Stellen</title><script>var x = "<a href='/evil'>";</script><style>a{}</style></head>
<body><nav><a href="/">Start</a> <a href="/kontakt">Kontakt</a></nav>
<h1>Offene Stellen</h1>
<ul>
 <li><a href="/jobs/1">Botaniker (m/w/d) – Kartierung</a> NABU Stuttgart, Teilzeit</li>
 <li><a href="https://other.example/job/2?x=1&amp;y=2">Werkstudent Vegetationsökologie</a> Uni Hohenheim</li>
 <li><a href="/jobs/1">Botaniker (m/w/d) – Kartierung</a></li>
 <li><a href="javascript:void(0)">Merken</a> <a href="mailto:a@b.de">Mail</a></li>
</ul></body></html>"""


class PagesTest(unittest.TestCase):
    def test_reduce_numbers_the_links_and_keeps_the_text(self):
        text, links = pages.reduce(BOARD, "https://board.example/stellen/")
        self.assertEqual(links, {1: "https://board.example/", 2: "https://board.example/kontakt",
                                 3: "https://board.example/jobs/1", 4: "https://other.example/job/2?x=1&y=2"})
        self.assertIn("[3] Botaniker (m/w/d) – Kartierung", text)
        self.assertIn("NABU Stuttgart, Teilzeit", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("javascript", text)

    def test_read_maps_link_numbers_to_postings(self):
        def extract(page):
            self.assertIn("[4] Werkstudent", page)
            return [{"link": 3, "title": "Botaniker (m/w/d)", "employer": "NABU", "location": "Stuttgart"},
                    {"link": 99, "title": "invented"}, {"link": 4, "title": ""}, {"title": "no link"},
                    {"link": 4, "title": "Werkstudent Vegetationsökologie", "employer": "Uni Hohenheim"}]

        found = pages.read(BOARD, "https://board.example/stellen/", "Board", extract)
        self.assertEqual([(p.title, p.url, p.company, p.location) for p in found],
                         [("Botaniker (m/w/d)", "https://board.example/jobs/1", "NABU", "Stuttgart"),
                          ("Werkstudent Vegetationsökologie", "https://other.example/job/2?x=1&y=2", "Uni Hohenheim", "")])
        self.assertEqual(found[0].source, "page")
        self.assertEqual(found[0].external_id, "https://board.example/jobs/1")
        self.assertEqual(found[0].tags, ["Board"])

    def test_fetch_asks_the_model_only_when_the_page_changed(self):
        import tempfile
        calls, html = [], {"https://b/": BOARD}

        def extract(page):
            calls.append(page)
            return [{"link": 3, "title": "Botaniker"}]

        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "pages-cache.json"
            cfg = {"pages": [{"name": "B", "url": "https://b/"}]}
            first = pages.fetch(cfg, None, get_text=html.get, extract=extract, cache_path=cache)
            again = pages.fetch(cfg, None, get_text=html.get, extract=extract, cache_path=cache)
            html["https://b/"] = BOARD.replace("Uni Hohenheim", "Uni Tübingen, vor 2 Stunden")  # same links
            pages.fetch(cfg, None, get_text=html.get, extract=extract, cache_path=cache)
            self.assertEqual(len(calls), 1)
            html["https://b/"] = BOARD.replace('<li><a href="/jobs/1">', '<li><a href="/jobs/3">Neu</a></li><li><a href="/jobs/1">', 1)
            pages.fetch(cfg, None, get_text=html.get, extract=extract, cache_path=cache)
        self.assertEqual(len(calls), 2)
        self.assertEqual([p.url for p in first], [p.url for p in again])

    def test_fetch_counts_cache_hits_and_misses(self):  # #17
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "pages-cache.json"
            cfg = {"pages": [{"name": "B", "url": "https://b/"}]}
            extract = lambda page: [{"link": 3, "title": "Botaniker"}]
            stats = {}
            pages.fetch(cfg, None, get_text={"https://b/": BOARD}.get, extract=extract, cache_path=cache, stats=stats)
            pages.fetch(cfg, None, get_text={"https://b/": BOARD}.get, extract=extract, cache_path=cache, stats=stats)
        self.assertEqual(stats, {"page_misses": 1, "page_hits": 1})

    def test_one_broken_page_does_not_hide_the_others(self):
        def get(url):
            if "broken" in url:
                raise OSError("404")
            return BOARD

        cfg = {"pages": [{"name": "X", "url": "https://broken/"}, {"name": "B", "url": "https://b/"}]}
        found = pages.fetch(cfg, None, get_text=get, extract=lambda page: [{"link": 3, "title": "Botaniker"}])
        self.assertEqual(len(found), 1)
