import json
import re
import tempfile
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

    def test_enrich_takes_the_partner_site_only_when_it_is_the_employers(self):
        # The BA's partner is often a job board or the BA itself, not the employer.
        cases = [("Dirk Rossmann GmbH", "www.rossmann.de", "https://www.rossmann.de"),
                 ("Müller Bau GmbH", "https://www.mueller-bau.de/karriere", "https://www.mueller-bau.de/karriere"),
                 ("König Fachpersonal GmbH, Niederlassung Hamburg", "www.bewerbung.jobs", ""),
                 ("Ludwig Personalservice GmbH", "http://www.arbeitsagentur.de", ""),
                 ("Personal Service GmbH", "www.personal-jobs.de", ""),
                 ("ACME GmbH", "javascript:alert(1)//acme", ""),
                 ("ACME GmbH", None, "")]
        for company, url, website in cases:
            p = self.posting()
            p.company = company
            ba.enrich(p, lambda u, headers=None: {**load("ba-detail.json"), "allianzpartnerUrl": url})
            self.assertEqual(p.website, website, company)

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
        self.assertEqual(links, {1: "https://board.example/jobs/1", 2: "https://other.example/job/2?x=1&y=2"})
        self.assertIn("[1] Botaniker (m/w/d) – Kartierung", text)
        self.assertIn("NABU Stuttgart, Teilzeit", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("javascript", text)

    def test_reduce_skips_the_site_navigation_but_not_a_job_card_header(self):
        html = """<header><a href="/login">Login</a></header><nav><a href="/">Start</a></nav>
        <main><article><header><h2><a href="/jobs/7">Gärtner (m/w/d)</a></h2></header>
        <footer><a href="/jobs/7/apply">Bewerben</a></footer></article></main>
        <footer><a href="/impressum">Impressum</a> Rotating <a href="/news/123">News</a></footer>"""
        text, links = pages.reduce(html, "https://b.example/")
        self.assertEqual(sorted(links.values()), ["https://b.example/jobs/7", "https://b.example/jobs/7/apply"])
        self.assertNotIn("Rotating", text)

    def test_read_maps_link_numbers_to_postings(self):
        def extract(page):
            self.assertIn("[2] Werkstudent", page)
            return [{"link": 1, "title": "Botaniker (m/w/d)", "employer": "NABU", "location": "Stuttgart"},
                    {"link": 99, "title": "invented"}, {"link": 2, "title": ""}, {"title": "no link"},
                    {"link": 2, "title": "Werkstudent Vegetationsökologie", "employer": "Uni Hohenheim"}]

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
            return [{"link": 1, "title": "Botaniker"}]

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
            extract = lambda page: [{"link": 1, "title": "Botaniker"}]
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
        found = pages.fetch(cfg, None, get_text=get, extract=lambda page: [{"link": 1, "title": "Botaniker"}])
        self.assertEqual(len(found), 1)


def job(i, **kw):
    return {"@type": "JobPosting", "title": f"Gärtner {i}", "url": f"https://b.example/jobs/{i}",
            "datePosted": "2026-09-30", "hiringOrganization": {"name": f"Grün {i}"},
            "jobLocation": {"address": {"addressLocality": "Kassel"}}, "description": "<p>Pflegen</p>", **kw}


def board(jobs=(), links=(), feed="", extra=""):
    """A list page: JSON-LD postings, a link per id in `links`, an optional feed link."""
    head = f'<link rel="alternate" type="application/rss+xml" title="Jobs" href="{feed}">' if feed else ""
    head += "".join(f'<script type="application/ld+json">{json.dumps(j)}</script>' for j in jobs)
    items = "".join(f'<li><a href="/jobs/{i}">Gärtner {i}</a> Grün {i}, Kassel</li>' for i in links)
    return f"<html><head>{head}</head><body><ul>{items}</ul>{extra}</body></html>"


def no_model(page):
    raise AssertionError("the model must not be asked")


class PagesJsonLdTest(unittest.TestCase):
    def fetch(self, html, extract=no_model, stats=None):
        cfg = {"pages": [{"name": "B", "url": "https://b.example/jobs/"}]}
        return pages.fetch(cfg, None, get_text={"https://b.example/jobs/": html}.get, extract=extract, stats=stats)

    def test_a_board_with_json_ld_postings_needs_no_model(self):
        stats = {}
        found = self.fetch(board([job(1), job(2), job(3)], links=[1, 2, 3]), stats=stats)
        self.assertEqual([(p.title, p.url, p.company, p.location, p.posted_at) for p in found][0],
                         ("Gärtner 1", "https://b.example/jobs/1", "Grün 1", "Kassel", "2026-09-30"))
        self.assertEqual(len(found), 3)
        self.assertEqual((found[0].source, found[0].external_id, found[0].tags), ("page", found[0].url, ["B"]))
        self.assertIn("Pflegen", found[0].text)
        self.assertEqual(stats, {"page_hits": 1})

    def test_an_item_list_of_postings_counts_too(self):
        html = board([{"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i, "item": job(i)} for i in (1, 2)]}], links=[1, 2])
        self.assertEqual([p.title for p in self.fetch(html)], ["Gärtner 1", "Gärtner 2"])

    def test_json_ld_for_a_few_featured_jobs_of_a_longer_list_is_not_trusted(self):
        calls = []

        def extract(page):
            calls.append(page)
            return [{"link": n, "title": f"Gärtner {n}"} for n in range(1, 11)]

        found = self.fetch(board([job(1), job(2)], links=range(1, 11)), extract=extract)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(found), 10)

    def test_json_ld_naming_addresses_the_page_does_not_link_is_not_trusted(self):
        calls = []
        others = [job(i, url=f"https://b.example/stellen/{i}") for i in (1, 2)]
        self.fetch(board(others, links=range(1, 6)), extract=lambda page: calls.append(page) or [])
        self.assertEqual(len(calls), 1)

    def test_a_single_json_ld_posting_is_left_to_the_model(self):
        calls = []
        self.fetch(board([job(1)], links=[1]), extract=lambda page: calls.append(page) or [])
        self.assertEqual(len(calls), 1)

    def test_postings_without_a_web_link_or_title_are_left_out(self):
        found = self.fetch(board([job(1), job(2), job(3, url="javascript:x"), job(4, title=""), job(5, url="")],
                                links=[1, 2]))
        self.assertEqual([p.url for p in found], ["https://b.example/jobs/1", "https://b.example/jobs/2"])


DAY = 86400


class PagesPatternTest(unittest.TestCase):
    """The model reads a board once; then only links shaped like its postings count."""

    def setUp(self):
        self.calls, self.now = [], 1_000_000.0
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name) / "pages-cache.json"

    def extract(self, page):
        self.calls.append(page)
        return [{"link": int(n), "title": title} for n, title in re.findall(r"\[(\d+)\] (Gärtner \d+)", page)]

    def fetch(self, links, news=1, days=0):
        side = f'<div class="teaser"><a href="/news/{news}?utm={news}">Neu im Blog</a></div>'
        cfg = {"pages": [{"name": "B", "url": "https://b.example/jobs/"}]}
        return pages.fetch(cfg, None, get_text=lambda url: board(links=links, extra=side), extract=self.extract,
                           cache_path=self.cache, now=self.now + days * DAY)

    def test_shapes_follow_the_postings_folder_with_digits_open(self):
        self.assertEqual(pages.pattern("https://B.example/jobs/12?x=1"), "b.example/jobs/")
        self.assertEqual(pages.pattern("https://b.example/2026/10/gaertner/"), "b.example/#/#/")
        self.assertEqual(pages.pattern("https://b.example/job.php?id=5"), "b.example/job.php?")
        self.assertEqual(pages.pattern("https://b.example/gaertner-5"), "b.example/")

    def test_rotating_links_elsewhere_on_the_page_do_not_ask_the_model_again(self):
        first = self.fetch([1, 2], news=1)
        again = self.fetch([1, 2], news=2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([p.url for p in again], [p.url for p in first])
        self.assertEqual(len(again), 2)

    def test_a_new_posting_sends_only_its_lines_to_the_model(self):
        self.fetch([1, 2])
        found = self.fetch([3, 1, 2], news=2)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("Gärtner 3", self.calls[1])
        self.assertIn("Grün 3, Kassel", self.calls[1])
        self.assertNotIn("Gärtner 1", self.calls[1])
        self.assertEqual(sorted(p.title for p in found), ["Gärtner 1", "Gärtner 2", "Gärtner 3"])

    def test_an_excerpt_keeps_a_card_title_above_its_details_link(self):
        text = "[1] Home\nBotaniker (m/w/d)\nFirma X, Berlin\n[2] Details\nab sofort\n[3] Impressum"
        self.assertEqual(pages.excerpt(text, {2}), "Botaniker (m/w/d)\nFirma X, Berlin\n[2] Details\nab sofort")

    def test_a_posting_gone_from_the_page_is_dropped_without_the_model(self):
        self.fetch([1, 2])
        self.assertEqual([p.title for p in self.fetch([2])], ["Gärtner 2"])
        self.assertEqual(len(self.calls), 1)

    def test_the_whole_page_is_read_again_once_a_week(self):
        self.fetch([1, 2])
        self.fetch([1, 2], days=6)
        self.assertEqual(len(self.calls), 1)
        self.fetch([1, 2], days=7)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("Gärtner 1", self.calls[1])

    def test_without_a_known_shape_any_link_change_reads_the_whole_page(self):
        self.extract = lambda page: self.calls.append(page) or []
        self.fetch([1, 2], news=1)
        self.fetch([1, 2], news=2)
        self.assertEqual(len(self.calls), 2)

    def test_a_board_linking_out_to_each_employer_counts_every_new_link(self):
        cfg = {"pages": [{"name": "B", "url": "https://b.example/jobs/"}]}
        web = {"links": [f"https://{firm}.example/karriere/{i}" for i, firm in enumerate(("gruen", "wald", "flora"), 1)]}

        def get(url):
            return "".join(f'<p><a href="{u}">Gärtner {i}</a></p>' for i, u in enumerate(web["links"], 1))

        pages.fetch(cfg, None, get_text=get, extract=self.extract, cache_path=self.cache, now=self.now)
        web["links"].append("https://neufirma.example/jobs/7")
        found = pages.fetch(cfg, None, get_text=get, extract=self.extract, cache_path=self.cache, now=self.now)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("https://neufirma.example/jobs/7", [p.url for p in found])

    def test_a_page_without_any_link_of_the_known_shape_is_read_again(self):
        self.fetch([1, 2])
        side = '<p><a href="/stellen/9">Gärtner 9</a></p>'
        cfg = {"pages": [{"name": "B", "url": "https://b.example/jobs/"}]}
        found = pages.fetch(cfg, None, get_text=lambda url: board(extra=side), extract=self.extract,
                            cache_path=self.cache, now=self.now)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([p.url for p in found], ["https://b.example/stellen/9"])


def rss(*items):
    """A feed of (title, link) items."""
    body = "".join(f"<item><title>{t}</title><link>{u}</link><guid>g-{u}</guid>"
                   f"<description>Pflege in Kassel</description></item>" for t, u in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class PagesFeedTest(unittest.TestCase):
    """A board whose page names a feed of its postings is read from that feed."""

    def setUp(self):
        self.calls, self.now = [], 1_000_000.0
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name) / "pages-cache.json"
        self.web, self.fetched = {}, []
        self.page([1, 2])

    def page(self, links, feed="https://b.example/feed.xml"):
        self.web["https://b.example/jobs/"] = board(links=links, feed=feed)

    extract = PagesPatternTest.extract

    def get(self, url):
        self.fetched.append(url)
        if url not in self.web:
            raise OSError("404")
        return self.web[url]

    def poll(self, days=0):
        self.fetched = []
        cfg = {"pages": [{"name": "B", "url": "https://b.example/jobs/"}]}
        return pages.fetch(cfg, None, get_text=self.get, extract=self.extract, cache_path=self.cache,
                           now=self.now + days * DAY)

    def jobs_feed(self, *ids):
        self.web["https://b.example/feed.xml"] = rss(*[(f"Gärtner {i}", f"https://b.example/jobs/{i}") for i in ids])

    def test_finds_the_feeds_a_page_names(self):
        html = ('<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
                '<link rel="Alternate" type="application/atom+xml" href="https://c.example/atom">'
                '<link rel="stylesheet" href="/a.css"><link rel="alternate" hreflang="en" href="/en/">')
        self.assertEqual(pages.feed_links(html, "https://b.example/jobs/"),
                         ["https://b.example/feed.xml", "https://c.example/atom"])

    def test_a_feed_of_the_postings_replaces_the_page(self):
        self.jobs_feed(1, 2)
        self.poll()
        self.assertEqual(len(self.calls), 1)
        self.jobs_feed(3, 1, 2)
        found = self.poll(days=1)
        self.assertEqual(self.fetched, ["https://b.example/feed.xml"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([(p.title, p.source, p.external_id, p.tags) for p in found][0],
                         ("Gärtner 3", "page", "https://b.example/jobs/3", ["B"]))
        self.assertIn("Kassel", found[0].text)

    def test_only_feed_entries_with_a_web_address_count(self):
        found = pages.from_feed(rss(("Gärtner 1", "/jobs/1"), ("Gärtner 2", "https://b.example/jobs/2")), "B")
        self.assertEqual([p.url for p in found], ["https://b.example/jobs/2"])

    def test_a_feed_of_other_things_is_not_used(self):
        self.web["https://b.example/feed.xml"] = rss(("Unser Sommerfest", "https://b.example/blog/1"),
                                                     ("Neue Website", "https://b.example/blog/2"))
        self.poll()
        self.poll(days=1)
        self.assertEqual(self.fetched, ["https://b.example/jobs/"])

    def test_once_a_week_the_page_must_still_name_the_feed(self):
        self.jobs_feed(1, 2)
        self.poll()
        self.poll(days=7)
        self.assertEqual(self.fetched, ["https://b.example/jobs/", "https://b.example/feed.xml"])
        self.assertEqual(len(self.calls), 1)
        self.page([1, 2], feed="")
        self.poll(days=14)
        self.assertEqual(len(self.calls), 2)

    def test_a_broken_feed_falls_back_to_the_page(self):
        self.jobs_feed(1, 2)
        self.poll()
        del self.web["https://b.example/feed.xml"]
        found = self.poll(days=1)
        self.assertEqual(sorted(p.title for p in found), ["Gärtner 1", "Gärtner 2"])
        self.assertIn("https://b.example/jobs/", self.fetched)


class PagesCacheTest(unittest.TestCase):
    """One cache file for every instance's boards, written whole and only when it changed."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name) / "pages-cache.json"
        self.calls = []

    def extract(self, page):
        self.calls.append(page)
        return [{"link": 1, "title": "Gärtner 1"}]

    def fetch(self, name="NABU", now=1_000_000.0, url="https://b.example/jobs/"):
        return pages.fetch({"pages": [{"name": name, "url": url}]}, None, get_text=lambda u: board(links=[1]),
                           extract=self.extract, cache_path=self.cache, now=now)

    def test_another_instance_reads_the_same_board_from_the_cache_under_its_own_name(self):
        self.fetch(name="NABU")
        found = self.fetch(name="NABU Jobs")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(found[0].tags, ["NABU Jobs"])

    def test_an_unchanged_board_writes_nothing(self):
        self.fetch()
        before = self.cache.stat().st_mtime_ns
        self.fetch()
        self.assertEqual(self.cache.stat().st_mtime_ns, before)

    def test_a_save_keeps_what_another_instance_wrote_meanwhile(self):
        pages.save(self.cache, {"https://a/": {"full_at": 10.0}}, now=10.0)
        pages.save(self.cache, {"https://b/": {"full_at": 10.0}}, now=10.0)
        self.assertEqual(set(json.loads(self.cache.read_text())), {"https://a/", "https://b/"})
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir() if p.name.endswith(".tmp")], [])

    def test_boards_nobody_read_for_a_month_are_dropped(self):
        pages.save(self.cache, {"https://old/": {"full_at": 0.0}}, now=0.0)
        pages.save(self.cache, {"https://new/": {"full_at": 31 * DAY}}, now=31 * DAY)
        self.assertEqual(set(json.loads(self.cache.read_text())), {"https://new/"})

    def test_a_broken_cache_file_counts_as_empty(self):
        self.cache.write_text("{half")
        self.fetch()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("https://b.example/jobs/", json.loads(self.cache.read_text()))
