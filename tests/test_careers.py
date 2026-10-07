import unittest

from job_radar import careers

KARRIERE = ("<html><head><title>Karriere bei Gärtnerei Grün</title></head>"
            "<body>Wir freuen uns über deine Initiativbewerbung!</body></html>")


class Web:
    """Serves fixed pages and records each request; any other address is down."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, url):
        self.asked.append(url)
        if url not in self.pages:
            raise OSError(f"404: {url}")
        return self.pages[url]


class FetcherTest(unittest.TestCase):
    def test_waits_between_requests_but_not_before_the_first(self):
        slept = []
        fetch = careers.Fetcher(Web({"https://a/": "x"}), budget=3, sleep=slept.append)
        fetch("https://a/")
        self.assertEqual(slept, [])
        with self.assertRaises(OSError):
            fetch("https://a/gone")
        self.assertEqual(slept, [careers.GAP_S])
        self.assertEqual(fetch.left, 1)

    def test_refuses_a_request_beyond_its_budget(self):
        web = Web({"https://a/": "x"})
        fetch = careers.Fetcher(web, budget=1, sleep=lambda s: None)
        fetch("https://a/")
        with self.assertRaises(careers.OutOfRequests):
            fetch("https://a/")
        self.assertEqual(len(web.asked), 1)


class CheckTest(unittest.TestCase):
    def check(self, pages, website="https://gruen.example/", company="Gärtnerei Grün GmbH"):
        web = Web(pages)
        found = careers.check(website, company, careers.Fetcher(web, budget=10, sleep=lambda s: None))
        return found, web.asked

    def test_finds_the_careers_page_that_invites_unsolicited_applications(self):
        found, asked = self.check({"https://gruen.example/karriere": KARRIERE})
        self.assertEqual(found, ("https://gruen.example/karriere", []))
        self.assertEqual(asked, ["https://gruen.example/karriere"])  # found: no further request

    def test_tries_the_other_paths_from_the_site_root(self):
        found, asked = self.check({"https://gruen.example/stellenangebote": KARRIERE},
                                  website="https://www.gruen.example/ueber-uns?x=1")
        self.assertEqual(found, ("", []))  # www. is another host: nothing served there
        found, asked = self.check({"https://gruen.example/stellenangebote": KARRIERE},
                                  website="https://gruen.example/ueber-uns?x=1")
        self.assertEqual(found[0], "https://gruen.example/stellenangebote")
        self.assertEqual(asked, [f"https://gruen.example{p}" for p in careers.PATHS])

    def test_a_page_without_the_employers_name_belongs_to_someone_else(self):
        page = "<body>Domain zu verkaufen. Initiativbewerbung bei uns!</body>"
        found, asked = self.check({f"https://gruen.example{p}": page for p in careers.PATHS})
        self.assertEqual(found, ("", []))
        self.assertEqual(len(asked), len(careers.PATHS))

    def test_a_page_without_the_word_is_no_invitation(self):
        found, _ = self.check({"https://gruen.example/jobs": "<body>Gärtnerei Grün sucht Gärtner</body>"})
        self.assertEqual(found, ("", []))

    def test_eigeninitiative_is_no_invitation(self):
        page = "<body>Gärtnerei Grün sucht Gärtner. Du bringst Eigeninitiative mit und ergreifst die Initiative.</body>"
        found, _ = self.check({"https://gruen.example/jobs": page})
        self.assertEqual(found, ("", []))

    def test_the_ways_pages_invite_unsolicited_applications(self):
        for text in ("Initiativbewerbungen sind willkommen", "Schick uns deine Initiativ-Bewerbung",
                     "Bewirb dich gerne initiativ", "Bewerben Sie sich einfach initiativ",
                     "Du möchtest dich initiativ bewerben?", "We welcome unsolicited applications"):
            found, _ = self.check({"https://gruen.example/karriere": f"<body>Gärtnerei Grün. {text}</body>"})
            self.assertEqual(found[0], "https://gruen.example/karriere", text)

    def test_a_name_of_only_generic_words_is_not_checked(self):
        found, asked = self.check({"https://gruen.example/karriere": KARRIERE}, company="GmbH & Co. KG")
        self.assertEqual((found, asked), (("", []), []))

    def test_keeps_a_job_feed_the_careers_pages_name_but_no_news_or_comment_feed(self):
        head = ('<link rel="alternate" type="application/rss+xml" href="/feed/">'
                '<link rel="alternate" type="application/rss+xml" href="/karriere/comments/feed/">'
                '<link rel="alternate" type="application/rss+xml" href="https://gruen.example/jobs/feed.xml">')
        jobs = f"<html><head>{head}</head><body>Gärtnerei Grün: offene Stellen</body></html>"
        found, _ = self.check({"https://gruen.example/jobs": jobs, "https://gruen.example/stellenangebote": KARRIERE})
        self.assertEqual(found, ("https://gruen.example/stellenangebote", ["https://gruen.example/jobs/feed.xml"]))

    def test_a_site_that_is_no_web_address_is_not_checked(self):
        found, asked = self.check({}, website="ftp://gruen.example")
        self.assertEqual((found, asked), (("", []), []))

    def test_a_malformed_website_is_not_checked(self):
        found, asked = self.check({}, website="https://www.[firma].de")
        self.assertEqual((found, asked), (("", []), []))

    def test_a_malformed_feed_link_is_left_out(self):
        head = '<link rel="alternate" type="application/rss+xml" href="http://[jobs/feed">'
        page = f"<html><head>{head}</head><body>Gärtnerei Grün: Initiativbewerbung willkommen</body></html>"
        found, _ = self.check({"https://gruen.example/karriere": page})
        self.assertEqual(found, ("https://gruen.example/karriere", []))


class NameWordsTest(unittest.TestCase):
    def test_leaves_out_legal_forms_and_short_words(self):
        self.assertEqual(careers.name_words("Müller GmbH & Co. KG"), ["müller"])
        self.assertEqual(careers.name_words("GmbH"), [])


if __name__ == "__main__":
    unittest.main()
