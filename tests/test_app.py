import dataclasses
import os
import re
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import json

from job_radar import app
from job_radar.interview import InterviewError
from job_radar.config import DEFAULTS, _merge
from job_radar.model import Posting
from job_radar.sources import pages
from job_radar.store import Store


class FakeBot:
    def __init__(self):
        self.sent, self.answers, self.docs = [], [], []
        self.notes, self.deleted = [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))

    def answer(self, cid, text=""):
        self.answers.append(text)

    def send_document(self, path, caption="", buttons=None):
        self.docs.append((Path(path), caption))
        self.doc_buttons = getattr(self, "doc_buttons", []) + [buttons]

    def typing(self):
        self.typed = getattr(self, "typed", 0) + 1

    def note(self, text):
        self.notes.append((len(self.notes) + 1, text))
        return self.notes[-1][0]

    def delete(self, message_id):
        self.deleted.append(message_id)

    def edit(self, message_id, text, buttons=None):
        self.edited = getattr(self, "edited", []) + [(message_id, text, buttons)]


def p(i, title="Senior Kotlin Engineer", **kw):
    kw.setdefault("location", f"Hamburg-{i}")  # another job each, not a duplicate
    return Posting(source="t", external_id=str(i), url=f"https://x/{i}", title=title, company="ACME", **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "db.sqlite")
        self.bot = FakeBot()
        self.cfg = _merge(DEFAULTS, {"filter": {"locations": [], "include": ["kotlin"], "exclude_title": ["junior"]}, "triage": {"threshold": 6},
                                     "report": {"reports_dir": str(self.dir / "reports")},
                                     "profile_files": [str(self.dir / "profile.md")], "config_dir": str(self.dir)})
        (self.dir / "profile.md").write_text("Kotlin backend developer")
        self.cfg["sources"]["initiativ"] = {"enabled": False}  # EmployersTest turns it on with fakes
        self.web, self.fetched = {}, []  # url -> html a page serves; any other is down, so no test goes online
        patcher = mock.patch.object(app, "get_public_text", self.serve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, url, **kw):
        self.fetched.append(url)
        if url not in self.web:
            raise OSError(f"offline: {url}")
        return self.web[url]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class PollTest(Base):
    def fetch(self, postings):
        return lambda cfg: postings

    def test_card_shows_the_website_triage_named_if_it_is_the_employers(self):  # #32
        triage = lambda batch, **kw: {1: (8, "great", "Lifts", "https://acme.example"),
                                      2: (8, "great", "Lifts", "https://other.example")}
        checked = []

        def check(url, company):
            checked.append((url, company))
            return "acme" in url

        self.cfg["notify"] = {"max_per_run": 2}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1), p(2), p(4)]),
                 triage_fn=lambda batch, **kw: {**triage(batch), 3: (7, "ok", "Lifts", "https://late.example")},
                 check_site=check)
        cards = [text for text, _ in self.bot.sent]
        self.assertEqual(len(cards), 2)  # posting 4 waits for the next run, its site is checked only then
        self.assertIn('🌐 <a href="https://acme.example">acme.example</a>', cards[0] + cards[1])
        self.assertEqual(sum("🌐" in c for c in cards), 1)
        self.assertEqual(sorted(checked), [("https://acme.example", "ACME"), ("https://other.example", "ACME")])
        self.assertEqual(self.store.get(1).website, "https://acme.example")

    def test_a_website_from_the_source_wins_over_the_one_triage_names(self):
        def enrich(postings):
            for x in postings:
                x.text, x.website = "details", "https://www.acme.example"

        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), enrich=enrich,
                 triage_fn=lambda batch, **kw: {1: (8, "great", "Lifts", "https://guess.example")},
                 check_site=lambda url, company: True)
        self.assertEqual(self.store.get(1).website, "https://www.acme.example")
        self.assertIn("acme.example</a>", self.bot.sent[0][0])

    def test_the_site_triage_names_stands_in_when_the_sources_fails_the_check(self):
        def enrich(postings):
            for x in postings:
                x.text, x.website = "details", "https://jobs-hamburg.example"

        checked = []

        def check(url, company):
            checked.append(url)
            return url == "https://acme.example"

        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), enrich=enrich,
                 triage_fn=lambda batch, **kw: {1: (8, "great", "Lifts", "https://acme.example")},
                 check_site=check)
        self.assertEqual(self.store.get(1).website, "https://acme.example")
        self.assertIn("acme.example</a>", self.bot.sent[0][0])
        self.assertEqual(checked, ["https://jobs-hamburg.example", "https://acme.example"])  # each once

    def test_a_low_rated_posting_keeps_the_sources_site_unchecked(self):
        def enrich(postings):
            for x in postings:
                x.text, x.website = "details", "https://www.acme.example"

        checked = []
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), enrich=enrich,
                 triage_fn=lambda batch, **kw: {1: (3, "meh", "Lifts", "https://guess.example")},
                 check_site=lambda url, company: checked.append(url))
        self.assertEqual(checked, [])  # no request for a posting that is never sent
        self.assertEqual(self.store.get(1).website, "https://www.acme.example")

    def test_a_website_from_the_source_is_kept_when_triage_fails(self):
        def enrich(postings):
            for x in postings:
                x.text, x.website = "details", "https://www.acme.example"

        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), enrich=enrich,
                 triage_fn=lambda batch, **kw: {})
        self.assertEqual(self.store.get(1).website, "https://www.acme.example")

    def test_website_check(self):
        page = lambda url: "<title>ACME Aufzüge GmbH – Startseite</title>"
        self.assertTrue(app.check_website("https://acme.example", "ACME Aufzüge GmbH", get=page))
        self.assertFalse(app.check_website("https://acme.example", "Müller Bau GmbH", get=page))
        self.assertFalse(app.check_website("https://acme.example", "GmbH", get=page))

        # every distinctive word of the name must be on the page, so "Co." or a city alone is not enough
        self.assertFalse(app.check_website("https://x.example", "Müller GmbH & Co. KG", get=lambda u: "Smith & Co. KG"))
        self.assertTrue(app.check_website("https://x.example", "Müller GmbH & Co. KG", get=lambda u: "Müller Bau"))
        self.assertFalse(app.check_website("https://x.example", "Stadtwerke Kassel",
                                           get=lambda u: "Stadtwerke Bochum"))

        def down(url):
            raise OSError("timeout")
        self.assertFalse(app.check_website("https://acme.example", "ACME", get=down))

    def test_filters_triages_and_notifies_above_threshold(self):
        postings = [p(1), p(2), p(3, title="Junior Kotlin Dev")]
        triage = lambda batch, **kw: {1: (8, "great"), 2: (3, "meh")}
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch(postings), triage_fn=triage)
        self.assertEqual(result, {"fetched": 3, "new": 3, "matched": 2, "notified": 1, "triage_calls": 1, "scored": 2})
        self.assertEqual(len([s for s in self.bot.sent if s[1]]), 1)
        self.assertIn("great", self.bot.sent[-1][0])
        self.assertEqual(self.store.get(2).status, "low")
        self.assertEqual(self.store.get(3).status, "filtered")
        self.assertEqual(self.store.get(1).status, "notified")

    def test_employer_description_is_stored_and_shown(self):
        triage = lambda batch, **kw: {1: (8, "fit", "Makes elevators")}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(self.store.get(1).employer, "Makes elevators")
        self.assertIn("Makes elevators", self.bot.sent[-1][0])

    def test_triage_usage_is_recorded(self):
        def triage(batch, on_usage=None, **kw):
            on_usage({"model": "haiku", "cost_usd": 0.01, "input_tokens": 5, "output_tokens": 1})
            return {}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(self.store.usage_summary(days=1)["triage"]["calls"], 1)

    def poll_row(self):
        return dict(self.store.db.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone())

    def test_poll_writes_a_polls_row_and_tags_its_triage_usage(self):  # #17
        self.store.add_new([p(90)])
        self.store.set_status(1, "pending")  # backlog from an earlier poll
        postings = [p(i) for i in range(1, 13)] + [p(13, title="Junior Kotlin Dev")]

        def triage(batch, on_usage=None, **kw):
            on_usage({"model": "haiku", "cost_usd": 0.01, "input_tokens": 5, "output_tokens": 1})
            if batch[0].external_id == "2":  # newest first: the second batch is 2, 1 and 90, then 2 alone
                raise RuntimeError("bad JSON")
            return {x.id: (8 if x.external_id == "3" else 2, "ok") for x in batch if x.external_id != "5"}

        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch(postings), triage_fn=triage, trigger="suchen")
        row = self.poll_row()
        self.assertEqual({k: row[k] for k in ("trigger", "fetched", "new", "matched", "pending", "sent", "scored",
                                              "omitted", "failed_batches", "triage_calls", "notified")},
                         {"trigger": "suchen", "fetched": 13, "new": 13, "matched": 12, "pending": 13, "sent": 13,
                          "scored": 11, "omitted": 1, "failed_batches": 2, "triage_calls": 4, "notified": 1})
        self.assertTrue(row["finished"])
        usage = [dict(r) for r in self.store.db.execute("SELECT items, poll_id, trigger FROM usage ORDER BY id")]
        self.assertEqual(usage, [{"items": n, "poll_id": row["id"], "trigger": "suchen"} for n in (10, 3, 1, 2)])

    def test_poll_counts_board_page_cache_hits(self):
        def fetch_all(cfg, on_usage=None, stats=None):
            stats.update(page_hits=3, page_misses=1)
            return []

        with mock.patch.object(app, "fetch_all", fetch_all):
            app.poll(self.cfg, self.store, self.bot, triage_fn=lambda batch, **kw: {})
        row = self.poll_row()
        self.assertEqual((row["trigger"], row["page_hits"], row["page_misses"]), ("timer", 3, 1))

    def test_a_poll_without_profile_writes_no_row(self):
        (self.dir / "profile.md").unlink()
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]))
        self.assertIsNone(self.store.db.execute("SELECT * FROM polls").fetchone())

    def test_cards_per_run_are_capped_and_rest_follows_best_first(self):
        self.cfg["notify"] = {"max_per_run": 2}
        scores = {1: 6, 2: 9, 3: 7, 4: 8}
        triage = lambda batch, **kw: {x.id: (scores[x.id], "") for x in batch}
        first = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(1, 5)]), triage_fn=triage)
        self.assertEqual(first["notified"], 2)
        self.assertEqual([self.store.get(i).status for i in (2, 4)], ["notified", "notified"])
        self.assertEqual(self.store.get(3).status, "queued")
        self.bot.sent.clear()
        second = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([]), triage_fn=triage)
        self.assertEqual(second["notified"], 2)
        self.assertEqual([self.store.get(i).status for i in (3, 1)], ["notified", "notified"])
        self.assertIn("Senior Kotlin Engineer", self.bot.sent[0][0])

    def test_second_poll_sends_nothing(self):
        triage = lambda batch, **kw: {x.id: (9, "") for x in batch}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.bot.sent.clear()
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=triage)
        self.assertEqual(result["new"], 0)
        self.assertEqual(self.bot.sent, [])

    def test_triage_failure_keeps_postings_pending_for_next_run(self):
        def broken(batch, **kw):
            raise RuntimeError("claude down")
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(1)]), triage_fn=broken)
        self.assertEqual(result["notified"], 0)
        self.assertEqual(self.store.get(1).status, "pending")
        triage = lambda batch, **kw: {x.id: (9, "now") for x in batch}
        result = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([]), triage_fn=triage)
        self.assertEqual(result["notified"], 1)
        self.assertEqual(self.store.get(1).status, "notified")

    def test_overflow_beyond_max_per_run_waits_for_next_run(self):
        self.cfg["triage"]["max_per_run"] = 2
        triage = lambda batch, **kw: {x.id: (9, "") for x in batch}
        first = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(3)]), triage_fn=triage)
        self.assertEqual(first["notified"], 2)
        second = app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([]), triage_fn=triage)
        self.assertEqual(second["notified"], 1)

    def test_triage_runs_in_batches_and_respects_max_per_run(self):
        self.cfg["triage"]["max_per_run"] = 15
        batches = []
        triage = lambda batch, **kw: batches.append(len(batch)) or {}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(30)]), triage_fn=triage)
        self.assertEqual(batches, [10, 5])

    def test_batch_size_is_configurable(self):
        self.cfg["triage"]["batch_size"] = 4
        batches = []
        triage = lambda batch, **kw: batches.append(len(batch)) or {}
        app.poll(self.cfg, self.store, self.bot, fetch=self.fetch([p(i) for i in range(10)]), triage_fn=triage)
        self.assertEqual(batches, [4, 4, 2])


class PollingBase(Base):
    def pending(self, *ids):
        for i in ids:
            [x] = self.store.add_new([p(i)])
            self.store.set_status(x.id, "pending")

    def poll(self, fetched=(), triage=None):
        sent = []

        def rate(batch, **kw):
            sent.append([x.id for x in batch])
            return (triage or (lambda b: {x.id: (2, "") for x in b}))(batch)
        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: list(fetched), triage_fn=rate)
        return sent

    def poll_row(self):
        return dict(self.store.db.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone())


class BacklogTest(PollingBase):
    """Postings waiting for a rating: newest first, and not forever."""

    def test_newest_postings_are_rated_first(self):
        self.cfg["triage"]["max_per_run"] = 3
        self.pending(1, 2)
        self.assertEqual(self.poll([p(3), p(4)]), [[4, 3, 2]])
        self.assertEqual(self.poll(), [[1]])

    def test_a_posting_older_than_the_search_expires_unrated(self):
        self.pending(1, 2)
        self.store.db.execute("UPDATE postings SET first_seen = datetime('now', '-29 days') WHERE id = 1")
        self.assertEqual(self.poll(), [[2]])
        self.assertEqual(self.store.get(1).status, "expired")
        self.assertEqual(self.poll_row()["expired"], 1)

    def test_expiry_follows_the_saved_search_period(self):
        (self.dir / "search.json").write_text(json.dumps({"sources": {"ba": {"published_since_days": 60}}}))
        self.pending(1)
        self.store.db.execute("UPDATE postings SET first_seen = datetime('now', '-40 days') WHERE id = 1")
        self.assertEqual(self.poll(), [[1]])

    def test_a_posting_triage_keeps_leaving_out_gives_up_after_three_tries(self):
        self.pending(1, 2)
        only_2 = lambda batch: {x.id: (2, "") for x in batch if x.id == 2}
        self.assertEqual(self.poll(triage=only_2), [[2, 1]])
        self.assertEqual(self.poll(triage=only_2), [[1]])
        self.assertEqual(self.store.get(1).status, "pending")
        self.assertEqual(self.poll(triage=only_2), [[1]])
        self.assertEqual(self.store.get(1).status, "unscored")
        self.assertEqual(self.poll_row()["unscored"], 1)
        self.assertEqual(self.poll(triage=only_2), [])

    def test_an_outage_costs_no_tries(self):
        def down(batch):
            raise RuntimeError("claude down")
        self.pending(1)
        for _ in range(4):
            self.assertEqual(self.poll(triage=down), [[1]])
        self.assertEqual(self.store.get(1).status, "pending")
        self.assertEqual(self.poll(), [[1]])
        self.assertEqual(self.store.get(1).status, "low")

    def test_a_failed_batch_is_halved_down_to_the_posting_that_breaks_it(self):
        self.pending(1, 2, 3, 4, 5)

        def bad_1(batch):  # a posting that breaks the answer, say
            if any(x.id == 1 for x in batch):
                raise RuntimeError("bad JSON")
            return {x.id: (2, "") for x in batch}
        self.assertEqual(self.poll(triage=bad_1), [[5, 4, 3, 2, 1], [5, 4], [3, 2, 1], [3], [2, 1], [2], [1]])
        self.assertEqual([self.store.get(i).status for i in range(1, 6)], ["pending"] + ["low"] * 4)
        row = self.poll_row()
        self.assertEqual((row["triage_calls"], row["failed_batches"], row["scored"]), (7, 4, 4))
        attempts = [r[0] for r in self.store.db.execute("SELECT attempts FROM postings ORDER BY id")]
        self.assertEqual(attempts, [1] + [0] * 4)  # only 1 failed alone

    def test_a_posting_that_breaks_triage_costs_its_neighbours_no_tries(self):
        self.pending(*range(1, 21))

        def bad_1(batch):
            if any(x.id == 1 for x in batch):
                raise RuntimeError("bad JSON")
            return {x.id: (8, "") for x in batch}
        for _ in range(5):
            self.poll(triage=bad_1)
        self.assertEqual([self.store.get(i).score for i in range(2, 21)], [8] * 19)
        self.assertEqual(self.store.get(1).status, "pending")  # failed alone once: one try
        self.assertEqual(self.store.db.execute("SELECT attempts FROM postings WHERE id = 1").fetchone()[0], 1)

    def test_neighbours_of_a_posting_that_breaks_triage_are_rated_while_new_postings_arrive(self):
        self.pending(1, 2, 5)

        def bad_1(batch):
            if any(x.id == 1 for x in batch):
                raise RuntimeError("bad JSON")
            return {x.id: (8, "") for x in batch}
        for n in range(4):
            self.poll([p(100 + 3 * n + k) for k in range(3)], triage=bad_1)
        self.assertEqual((self.store.get(2).score, self.store.get(3).score), (8, 8))  # posted as 2 and 5
        self.assertEqual(self.store.get(1).status, "unscored")  # failed alone, three polls in a row
        self.assertEqual(self.poll([p(200)], triage=bad_1), [[16]])

    def test_when_the_halves_fail_too_the_rest_waits_for_the_next_poll(self):
        def down(batch):
            raise RuntimeError("claude down")
        self.pending(*range(1, 13))
        self.assertEqual([len(b) for b in self.poll(triage=down)], [10, 5, 5])
        self.assertEqual(self.poll_row()["failed_batches"], 3)

    def test_an_outage_while_halving_costs_no_tries(self):  # the plan's limit reached mid-poll, say
        calls = []

        def runs_out(batch):
            calls.append(batch)
            if len(calls) != 2:
                raise RuntimeError("limit reached")
            return {x.id: (8, "") for x in batch}
        self.pending(*range(1, 13))
        self.assertEqual([len(b) for b in self.poll(triage=runs_out)], [10, 5, 5, 2, 3])
        attempts = [r[0] for r in self.store.db.execute("SELECT attempts FROM postings ORDER BY id")]
        self.assertEqual(attempts, [0] * 12)
        self.assertEqual(sum(self.store.get(i).status == "pending" for i in range(1, 13)), 7)


class DuplicatesTest(PollingBase):
    """The same job from another source, or posted again, is rated once."""

    def job(self, i, source="t", **kw):
        return Posting(**{"source": source, "external_id": str(i), "url": f"https://x/{i}", "location": "Kassel",
                          "title": "Kotlin Engineer (m/w/d)", "company": "ACME", **kw})

    def test_a_copy_of_a_rated_job_takes_its_rating_and_is_never_sent(self):
        self.assertEqual(self.poll([self.job(1)], triage=lambda b: {x.id: (8, "fits") for x in b}), [[1]])
        self.assertEqual(len(self.bot.sent), 1)
        copy = self.job(2, source="other", title="Kotlin Engineer (w/m/d)", company="ACME GmbH")
        self.assertEqual(self.poll([copy]), [])
        got = self.store.get(2)
        self.assertEqual((got.status, got.score, got.why), ("duplicate", 8, "fits"))
        self.assertEqual(len(self.bot.sent), 1)
        row = self.poll_row()
        self.assertEqual((row["duplicates"], row["sent"], row["triage_calls"]), (1, 0, 0))

    def test_copies_waiting_together_are_rated_once(self):
        good = lambda b: {x.id: (8, "") for x in b}
        self.assertEqual(self.poll([self.job(1), self.job(2, source="other"), p(3)], triage=good), [[3, 2]])
        self.assertEqual(self.store.get(1).status, "pending")
        self.assertEqual(self.poll(), [])
        self.assertEqual((self.store.get(1).status, self.store.get(1).score), ("duplicate", 8))

    def test_a_copy_from_the_same_source_under_a_low_rating_is_rated_again(self):
        # the same employer can post the same title twice for different sites or hours
        self.poll([self.job(1)], triage=lambda b: {x.id: (3, "full time") for x in b})
        self.assertEqual(self.poll([self.job(2)], triage=lambda b: {x.id: (8, "part time") for x in b}), [[2]])
        self.assertEqual((self.store.get(2).status, self.store.get(2).score), ("notified", 8))

    def test_a_copy_from_the_same_source_of_a_job_already_sent_is_a_duplicate(self):
        self.poll([self.job(1)], triage=lambda b: {x.id: (8, "fits") for x in b})
        self.assertEqual(self.poll([self.job(2)]), [])
        self.assertEqual(self.store.get(2).status, "duplicate")

    def test_a_copy_from_another_source_under_a_low_rating_is_rated_again(self):
        # its own source may have the employer's other ad for that title, which the copy cannot stand for
        self.poll([self.job(1, source="other")], triage=lambda b: {x.id: (3, "full time") for x in b})
        self.assertEqual(self.poll([self.job(2)], triage=lambda b: {x.id: (8, "part time") for x in b}), [[2]])
        self.assertEqual((self.store.get(2).status, self.store.get(2).score), ("notified", 8))

    def test_another_city_or_employer_is_rated(self):
        self.poll([self.job(1)])
        self.assertEqual(self.poll([self.job(2, location="Fulda"), self.job(3, company="Other")]), [[3, 2]])

    def test_after_a_profile_change_copies_are_rated_again(self):
        self.poll([self.job(1)])
        later = time.time() + 5
        os.utime(self.dir / "profile.md", (later, later))
        self.assertEqual(self.poll([self.job(2, source="other")]), [[2]])


class PollStatsTest(Base):
    def test_poll_reports_triage_calls_and_scored_postings(self):
        triage = lambda batch, **kw: {x.id: (8, "fits") for x in batch}
        result = app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [p(i) for i in range(12)],
                          triage_fn=triage)
        self.assertEqual((result["triage_calls"], result["scored"]), (2, 12))

    def test_more_postings_are_scored_per_poll_than_before(self):
        self.assertEqual(DEFAULTS["triage"]["max_per_run"], 60)

    def test_batches_stay_at_ten_by_default(self):
        # 20 per call cost less, but moved scores more than the model's own noise (phase 3 check)
        self.assertEqual(DEFAULTS["triage"]["batch_size"], 10)


class EnrichTest(Base):
    def test_poll_adds_details_before_triage_and_keeps_them(self):
        seen = []

        def enrich(postings):
            for x in postings:
                x.text = "Arbeitszeit: Teilzeit"

        def triage(batch, **kw):
            seen.extend(x.text for x in batch)
            return {x.id: (8, "fits") for x in batch}

        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [p(1)], triage_fn=triage, enrich=enrich)
        self.assertEqual(seen, ["Arbeitszeit: Teilzeit"])
        self.assertEqual(self.store.by_status("notified")[0].text, "Arbeitszeit: Teilzeit")

    def test_poll_keeps_the_tags_the_details_added(self):
        def enrich(postings):
            for x in postings:
                x.text, x.tags = "Zeitarbeit: ja", [*x.tags, "Zeitarbeit"]

        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [p(1, tags=["Kotlin"])],
                 triage_fn=lambda batch, **kw: {}, enrich=enrich)
        self.assertEqual(self.store.get(1).tags, ["Kotlin", "Zeitarbeit"])

    def test_details_are_loaded_only_for_postings_that_get_scored(self):
        got = []
        cfg = _merge(self.cfg, {"triage": {"max_per_run": 2}})
        app.poll(cfg, self.store, self.bot, fetch=lambda cfg: [p(i) for i in range(5)],
                 triage_fn=lambda batch, **kw: {}, enrich=got.extend)
        self.assertEqual(len(got), 2)

    def test_enrich_postings_uses_the_source_module_and_survives_failures(self):
        class Good:
            @staticmethod
            def enrich(posting, http_get):
                posting.text = "details"

        class Bad:
            @staticmethod
            def enrich(posting, http_get):
                raise OSError("down")

        a, b, c = p(1), p(2), p(3)
        a.source, b.source, c.source = "good", "bad", "plain"
        app.enrich_postings([a, b, c], registry={"good": Good, "bad": Bad, "plain": object()})
        self.assertEqual((a.text, b.text, c.text), ("details", "", ""))


class FetchAllTest(unittest.TestCase):
    def test_broken_source_does_not_stop_others(self):
        class Good:
            @staticmethod
            def fetch(cfg, http_get): return [p(1)]

        class Bad:
            @staticmethod
            def fetch(cfg, http_get): raise OSError("down")

        cfg = {"sources": {"good": {"enabled": True}, "bad": {"enabled": True}, "off": {"enabled": False}}}
        errors = []
        got = app.fetch_all(cfg, registry={"good": Good, "bad": Bad, "off": Bad}, on_error=errors.append)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(errors), 1)


class PagesFetchTest(Base):
    def test_poll_reads_board_pages_with_a_cache_and_records_the_usage(self):
        seen = {}

        class Pages:
            @staticmethod
            def fetch(scfg, http_get, extract=None, cache_path=None):
                seen["cache"] = cache_path
                extract("[1] Botaniker")
                return []

        def extract_fn(page, model, on_usage):
            on_usage({"model": model, "cost_usd": 0.001, "input_tokens": 1, "output_tokens": 1, "duration_ms": 1})
            return []

        cfg = _merge(self.cfg, {"sources": {"ba": {"enabled": False}, "arbeitnow": {"enabled": False},
                                            "feeds": {"enabled": False}},
                                "db_path": str(self.dir / "db.sqlite")})
        app.fetch_all(cfg, registry={"pages": Pages}, on_usage=lambda u: self.store.record_usage("pages", u),
                      extract_fn=extract_fn)
        self.assertEqual(seen["cache"], self.dir / "pages-cache.json")
        self.assertIn("pages", self.store.usage_summary(1))

    def test_the_shared_board_page_cache_is_used_when_set(self):
        seen = {}

        class Pages:
            @staticmethod
            def fetch(scfg, http_get, extract=None, cache_path=None):
                seen["cache"] = cache_path
                return []

        cfg = _merge(self.cfg, {"sources": {"ba": {"enabled": False}, "arbeitnow": {"enabled": False},
                                            "feeds": {"enabled": False}},
                                "db_path": str(self.dir / "anna" / "db.sqlite")})
        with mock.patch.object(pages, "CACHE_PATH", self.dir / "pages-cache.json"):
            app.fetch_all(cfg, registry={"pages": Pages}, on_usage=lambda u: None)
        self.assertEqual(seen["cache"], self.dir / "pages-cache.json")

    def test_board_page_cache_counts_reach_the_caller(self):  # #17
        class Pages:
            @staticmethod
            def fetch(scfg, http_get, extract=None, cache_path=None, stats=None):
                stats.update(page_hits=3, page_misses=1)
                return []

        cfg = _merge(self.cfg, {"sources": {"ba": {"enabled": False}, "arbeitnow": {"enabled": False},
                                            "feeds": {"enabled": False}},
                                "db_path": str(self.dir / "db.sqlite")})
        stats = {}
        app.fetch_all(cfg, registry={"pages": Pages}, on_usage=lambda u: None, stats=stats)
        self.assertEqual(stats, {"page_hits": 3, "page_misses": 1})

    def test_without_usage_tracking_no_page_is_read(self):
        class Pages:
            @staticmethod
            def fetch(scfg, http_get, extract=None, cache_path=None):
                return [] if extract is None else [p(1)]

        cfg = _merge(self.cfg, {"sources": {"ba": {"enabled": False}, "arbeitnow": {"enabled": False},
                                            "feeds": {"enabled": False}}})
        self.assertEqual(app.fetch_all(cfg, registry={"pages": Pages}), [])


class CountMatchesTest(Base):
    def test_failed_source_raises_instead_of_counting_zero(self):
        def fetch(cfg, on_error):
            on_error("ba: HTTP 503")
            return []
        with self.assertRaises(RuntimeError):
            app.count_matches(self.cfg, fetch=fetch)

    def test_counts_matching_postings(self):
        (self.dir / "search.json").write_text(json.dumps({"filter": {"exclude_title": ["junior"]}}))
        self.assertEqual(app.count_matches(self.cfg, fetch=lambda cfg, on_error: [p(1), p(2, "Junior Kotlin")]), 1)


class ButtonTest(Base):
    def setUp(self):
        super().setUp()
        [self.posting] = self.store.add_new([p(1)])

    def press(self, data, report_fn=None):
        upd = {"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}}
        return app.handle_update(upd, self.cfg, self.store, self.bot,
                                 report_fn=report_fn or (lambda posting, **kw: "---\ntitle: x\n---\n# R\n## TL;DR\n- good"),
                                 background=lambda fn: fn())

    def test_skip_sets_status(self):
        self.press("s:1")
        self.assertEqual([x.id for x in self.store.disliked(5)], [1])

    def test_applied_appends_tracker_row(self):
        tracker = self.dir / "tracker.md"
        tracker.write_text("| Date | Company |\n|---|---|\n")
        self.cfg["tracker_path"] = str(tracker)
        self.press("a:1")
        self.assertEqual([x.id for x, _ in self.store.marked("applied")], [1])
        self.assertIn("| ACME | Senior Kotlin Engineer |", tracker.read_text())

    def test_report_on_a_card_does_not_fetch_its_page(self):
        self.press("r:1")
        self.assertEqual(self.fetched, [])

    def test_report_writes_file_and_sends_it(self):
        self.press("r:1")
        files = list((self.dir / "reports").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{2}-\d{2}-\d{2}-acme-senior-kotlin-engineer\.md$")
        self.assertEqual(self.bot.docs[0][0], files[0])
        self.assertEqual(self.store.get(1).status, "reported")

    def test_report_comes_with_a_bookmark_button(self):
        self.press("r:1")
        self.assertEqual(self.bot.doc_buttons, [[[("⭐ Vormerken", "v:1")]]])
        self.press("v:1")
        self.assertEqual([x.id for x, _ in self.store.marked("saved")], [1])

    def test_bookmark_keeps_an_application(self):
        self.press("a:1")
        self.press("v:1")
        self.assertEqual([x.id for x, _ in self.store.marked("applied")], [1])
        self.assertEqual(self.store.marked("saved"), [])

    def test_report_caption_is_readable_html(self):
        self.press("r:1", report_fn=lambda posting, **kw: "# R\n## TL;DR\n- **gut** & passend")
        self.assertEqual(self.bot.docs[0][1], "<b>ACME - Senior Kotlin Engineer</b>\n\n• <b>gut</b> &amp; passend")

    def test_report_file_name_transliterates_umlauts(self):
        q = p(2, title="Entwickler Größe")
        q.company = "Dr. Güldener Ärzte"
        path = app.write_report(self.cfg, q, "x")
        self.assertTrue(path.name.endswith("-dr-gueldener-aerzte-entwickler-groesse.md"), path.name)

    def counting_report(self):
        calls = []
        return calls, lambda posting, **kw: calls.append(posting.id) or "---\ntitle: x\n---\n# R\n## TL;DR\n- good"

    def test_report_again_sends_the_file_it_has(self):
        calls, report = self.counting_report()
        self.press("r:1", report_fn=report)
        self.press("r:1", report_fn=report)
        self.assertEqual(calls, [1])
        self.assertEqual(len(self.bot.docs), 2)
        self.assertEqual(self.bot.docs[0][0], self.bot.docs[1][0])
        self.assertIn(f"Bericht vom {date.today():%d.%m.}", self.bot.docs[1][1])
        self.assertIn("• good", self.bot.docs[1][1])
        self.assertEqual(self.bot.doc_buttons[1], [[("⭐ Vormerken", "v:1")]])
        self.assertEqual(len(json.loads(self.store.get_state("rate:report"))), 1)  # costs nothing of the day's limit
        self.assertEqual(self.store.get(1).status, "reported")

    def test_report_from_before_is_found_by_its_name(self):
        calls, report = self.counting_report()
        old = self.dir / "reports" / "26-09-30-acme-senior-kotlin-engineer.md"
        old.parent.mkdir()
        old.write_text("# R\n## TL;DR\n- older")
        self.store.set_status(1, "reported")
        self.press("r:1", report_fn=report)
        self.assertEqual((calls, self.bot.docs[0][0]), ([], old))

    def test_the_same_job_in_another_city_gets_its_own_report(self):
        calls, report = self.counting_report()
        self.press("r:1", report_fn=report)
        [other] = self.store.add_new([p(2, location="München")])
        self.press(f"r:{other.id}", report_fn=report)
        self.assertEqual(calls, [1, other.id])

    def test_a_report_sent_again_is_this_postings_not_another_citys(self):
        report = lambda posting, **kw: f"# R\n## TL;DR\n- in {posting.location}"
        self.press("r:1", report_fn=report)
        [other] = self.store.add_new([p(2, location="München")])
        self.press(f"r:{other.id}", report_fn=report)
        self.press("r:1", report_fn=report)
        self.assertNotEqual(self.bot.docs[0][0], self.bot.docs[1][0])
        self.assertEqual(self.bot.docs[2][0], self.bot.docs[0][0])
        self.assertIn("in Hamburg-1", self.bot.docs[2][0].read_text())

    def test_a_report_older_than_two_months_is_researched_anew(self):
        calls, report = self.counting_report()
        old = self.dir / "reports" / "26-06-03-acme-senior-kotlin-engineer.md"
        old.parent.mkdir()
        old.write_text("# R\n## TL;DR\n- stale")
        then = time.time() - 61 * 86400
        os.utime(old, (then, then))
        self.press("r:1", report_fn=report)
        self.assertEqual(calls, [1])

    def test_report_runs_again_when_its_file_is_gone(self):
        calls, report = self.counting_report()
        self.press("r:1", report_fn=report)
        self.bot.docs[0][0].unlink()
        self.press("r:1", report_fn=report)
        self.assertEqual(calls, [1, 1])

    def test_a_running_report_is_not_started_twice(self):
        calls, report = self.counting_report()
        self.store.set_status(1, "interesting")
        self.press("r:1", report_fn=report)
        self.assertEqual(calls, [])
        self.assertIn("läuft schon", self.bot.answers[-1])

    def test_a_failed_report_is_not_resumed(self):
        def broken(posting, **kw):
            raise RuntimeError("timeout")
        self.press("r:1", report_fn=broken)
        self.assertEqual(self.store.get(1).status, "report_failed")
        calls, report = self.counting_report()
        self.press("r:1", report_fn=report)  # tapping again tries again
        self.assertEqual(calls, [1])

    def resume(self):
        resumed = []
        app.resume_reports(self.cfg, self.store, self.bot, background=lambda fn: resumed.append(fn))
        return resumed

    def test_listen_resumes_an_interrupted_report_once(self):
        self.press("r:1", report_fn=lambda posting, **kw: 1 / 0)
        self.store.set_status(1, "interesting")
        self.assertEqual(len(self.resume()), 1)
        self.assertEqual(len(json.loads(self.store.get_state("rate:report"))), 2)  # counts toward the limit
        self.assertEqual(self.resume(), [])  # restarted again before it finished
        self.assertEqual(self.store.get(1).status, "report_failed")
        self.assertIn("unterbrochen", self.bot.sent[-1][0])

    def test_a_report_older_than_a_day_or_from_before_is_not_resumed(self):
        self.press("r:1", report_fn=lambda posting, **kw: 1 / 0)
        self.store.set_status(1, "interesting")
        run = json.loads(self.store.get_state("report_run:1"))
        self.store.set_state("report_run:1", json.dumps({**run, "at": time.time() - 86401}))
        self.assertEqual(self.resume(), [])
        self.assertEqual(self.store.get(1).status, "report_failed")
        [other] = self.store.add_new([p(2)])
        self.store.set_status(other.id, "interesting")  # started before reports were recorded
        self.assertEqual(self.resume(), [])
        self.assertEqual(self.store.get(other.id).status, "report_failed")

    def test_no_resume_past_the_days_limit(self):
        self.press("r:1", report_fn=lambda posting, **kw: 1 / 0)
        self.store.set_status(1, "interesting")
        self.store.set_state("rate:report", json.dumps([time.time()] * app.LIMITS["report"][0]))
        self.assertEqual(self.resume(), [])
        self.assertEqual(self.store.get(1).status, "report_failed")

    def test_report_failure_is_reported_to_chat(self):
        def broken(posting, **kw):
            raise RuntimeError("timeout")
        self.press("r:1", report_fn=broken)
        self.assertIn("fehlgeschlagen", self.bot.sent[-1][0].lower())

    def test_expired_button_answer_still_applies_action(self):
        def expired(cid, text=""):
            raise OSError("HTTP Error 400: query is too old")
        self.bot.answer = expired
        self.press("v:1")
        self.assertEqual([x.id for x, _ in self.store.marked("saved")], [1])

    def test_one_failing_update_does_not_drop_the_rest(self):
        handled = []

        def handle(upd, *args, **kw):
            if upd["update_id"] == 1:
                raise RuntimeError("boom")
            handled.append(upd["update_id"])

        batches = [[{"update_id": 1}, {"update_id": 2}]]
        self.bot.updates = lambda wait=0: batches.pop(0) if batches else []
        self.bot.set_commands = lambda c: None
        registry = type("R", (), {"all": lambda self: []})()
        app.listen(registry, self.bot, stop=lambda: not batches, handle=handle)
        self.assertEqual(handled, [2])

    def test_unknown_posting_is_answered(self):
        self.press("s:999")
        self.assertIn("unbekannt", self.bot.answers[-1].lower())


class LinkTest(Base):
    REPORT = '---\ncompany: "Open Co"\nrole: "Backend Engineer"\n---\n## 1. TL;DR\n- nice'

    def send_link(self, text):
        upd = {"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}
        app.handle_update(upd, self.cfg, self.store, self.bot,
                          report_fn=getattr(self, "REPORT_FN", lambda posting, **kw: self.REPORT),
                          background=lambda fn: fn())

    def test_pasted_link_produces_report_named_from_frontmatter(self):
        self.send_link("look at https://jobs.example.com/open-co/backend-engineer-123 please")
        files = list((self.dir / "reports").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{2}-\d{2}-\d{2}-open-co-backend-engineer\.md$")
        stored = self.store.get(1)
        self.assertEqual((stored.source, stored.company, stored.status), ("manual", "Open Co", "reported"))
        self.assertIn("Ich recherchiere", self.bot.sent[0][0])

    def test_report_on_a_link_can_be_bookmarked_under_its_real_name(self):
        self.send_link("https://jobs.example.com/x")
        self.assertEqual(self.bot.doc_buttons, [[[("⭐ Vormerken", "v:1")]]])
        self.assertIn("Vormerken", self.bot.sent[0][0])
        self.assertEqual(self.store.get(1).title, "Backend Engineer")

    def test_report_tells_when_a_linked_posting_went_online(self):  # #34
        self.REPORT = '---\ncompany: "Open Co"\nrole: "Dev"\npublished: "2026-09-30"\n---\n## 1. TL;DR\n- nice'
        self.send_link("https://jobs.example.com/x")
        self.assertEqual(self.store.get(1).posted_at, "2026-09-30")

    JOB_PAGE = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Gärtner (m/w/d)", '
                '"datePosted": "2026-09-28", "description": "<p>Du pflegst den Park.</p>", '
                '"hiringOrganization": {"name": "Grün GmbH", "sameAs": "https://gruen.example"}, '
                '"jobLocation": {"address": {"addressLocality": "Kassel"}}}</script>')

    def test_a_linked_page_with_job_data_gives_the_report_its_facts(self):
        seen = []
        self.REPORT = '---\ncompany: "Grün GmbH"\nrole: "Gärtner"\npublished: "unknown"\n---\n## 1. TL;DR\n- x'
        self.REPORT_FN = lambda posting, **kw: seen.append(dataclasses.replace(posting)) or self.REPORT
        self.web["https://jobs.example.com/g"] = self.JOB_PAGE
        self.send_link("https://jobs.example.com/g")
        [p] = seen
        self.assertEqual((p.title, p.company, p.location, p.website), ("Gärtner (m/w/d)", "Grün GmbH", "Kassel",
                                                                      "https://gruen.example"))
        self.assertIn("Du pflegst den Park.", p.text)
        stored = self.store.get(1)
        self.assertEqual((stored.posted_at, stored.website), ("2026-09-28", "https://gruen.example"))
        self.assertIn("Du pflegst den Park.", stored.text)

    def test_a_link_whose_page_fails_or_has_no_job_data_still_gets_its_report(self):
        self.web["https://jobs.example.com/plain"] = "<html>Just a page</html>"
        self.send_link("https://jobs.example.com/plain")
        self.send_link("https://jobs.example.com/down")
        self.assertEqual(len(self.bot.docs), 2)
        self.assertEqual(self.store.get(1).text, "")

    def test_odd_job_data_on_a_linked_page_still_gets_its_report(self):
        self.web["https://jobs.example.com/g"] = self.JOB_PAGE
        with mock.patch.object(app.jsonld, "fields", side_effect=TypeError("odd")):
            self.send_link("https://jobs.example.com/g")
        self.assertEqual(len(self.bot.docs), 1)

    def test_a_link_the_radar_found_before_keeps_its_date_over_the_pages(self):
        self.store.add_new([Posting(source="arbeitnow", external_id="n1", url="https://jobs.example.com/g",
                                    title="Dev", posted_at="2026-10-01T08:00+00:00")])
        self.web["https://jobs.example.com/g"] = self.JOB_PAGE
        self.send_link("https://jobs.example.com/g")
        self.assertEqual(self.store.find("manual", "https://jobs.example.com/g").posted_at, "2026-10-01T08:00+00:00")

    def test_unknown_or_bad_publication_date_is_ignored(self):
        for i, value in enumerate(("unknown", "30.09.2026", "2999-01-01")):
            self.REPORT = f'---\ncompany: "Open Co"\nrole: "Dev"\npublished: "{value}"\n---\n## 1. TL;DR\n- x'
            self.send_link(f"https://jobs.example.com/{i}")
            self.assertEqual(self.store.get(i + 1).posted_at, "", value)

    def test_link_the_radar_found_before_keeps_its_date(self):
        [known] = self.store.add_new([Posting(source="arbeitnow", external_id="n1", url="https://jobs.example.com/n",
                                              title="Dev", posted_at="2026-10-01T08:00+00:00")])
        self.send_link("https://jobs.example.com/n")
        self.assertEqual(self.store.find("manual", "https://jobs.example.com/n").posted_at, "2026-10-01T08:00+00:00")

    def test_recherche_without_link_asks_for_one_and_takes_the_next_message(self):
        self.send_link("/recherche")
        self.assertIn("Link", self.bot.sent[-1][0])
        self.send_link("hier: https://jobs.example.com/x")
        self.assertEqual(len(self.bot.docs), 1)

    def test_recherche_waiting_for_a_link_explains_a_message_without_one(self):
        self.send_link("/recherche")
        self.send_link("die Stelle bei Open Co")
        self.assertEqual(self.bot.docs, [])
        self.assertIn("Link", self.bot.sent[-1][0])
        self.assertNotIn(app.HELP, self.bot.sent[-1][0])

    def test_recherche_with_link_starts_at_once(self):
        self.send_link("/recherche https://jobs.example.com/x")
        self.assertEqual(len(self.bot.docs), 1)

    def test_another_command_drops_a_waiting_recherche(self):
        self.send_link("/recherche")
        self.send_link("/profile")
        self.send_link("hello")
        self.assertIn(app.HELP, self.bot.sent[-1][0])

    def test_recherche_is_in_the_menu_and_the_help(self):
        self.assertIn("recherche", [name for name, _ in app.COMMANDS])
        self.assertIn("/recherche", app.HELP)

    def test_same_link_twice_reuses_posting_and_its_report(self):
        calls = []
        self.REPORT_FN = lambda posting, **kw: calls.append(1) or self.REPORT
        self.send_link("https://jobs.example.com/x")
        self.send_link("https://jobs.example.com/x")
        self.assertIsNone(self.store.get(2))
        self.assertEqual(len(self.bot.docs), 2)
        self.assertEqual(calls, [1])

    def test_usage_command_replies_with_summary(self):
        self.store.record_usage("triage", {"model": "haiku", "cost_usd": 0.25, "input_tokens": 5, "output_tokens": 1,
                                           "five_hour": 0.05, "five_hour_resets": 1791146400,
                                           "seven_day": 0.031, "seven_day_resets": 1791702000})
        self.send_link("/usage")
        text = self.bot.sent[-1][0]
        self.assertIn("0.25", text)
        self.assertIn("triage", text)
        self.assertIn("5-Stunden-Fenster: 5%", text)
        self.assertIn("Woche: 3%", text)
        self.assertIn(app._resets(1791702000).split()[0], app._WEEKDAYS)

    def test_usage_probes_when_limits_are_stale(self):
        probes = []
        text = app.usage_text(self.store, probe=lambda: probes.append(1) or self.store.record_usage(
            "probe", {"model": "haiku", "five_hour": 0.4, "seven_day": 0.5}))
        self.assertEqual(probes, [1])
        self.assertIn("Woche: 50%", text)

    def test_usage_without_limits_or_probe_says_unknown(self):
        self.assertIn("unbekannt", app.usage_text(self.store).lower())

    def test_help_and_start_list_commands_and_buttons(self):
        for cmd in ("/help", "/start"):
            self.send_link(cmd)
            text = self.bot.sent[-1][0]
            for word in ("/usage", "/help", "/setup", "/profile", "Recherche", "Vormerken", "/merkliste", "Link"):
                self.assertIn(word, text)

    def test_listen_registers_command_menu(self):
        registered = []
        self.bot.set_commands = registered.extend
        app.listen(type("R", (), {"all": lambda self: []})(), self.bot, stop=lambda: True)
        self.assertIn("usage", [name for name, _ in registered])
        self.assertIn("help", [name for name, _ in registered])

    def test_text_without_link_gets_help(self):
        self.send_link("hello")
        self.assertIn("link", self.bot.sent[-1][0].lower())


class ProfileTextTest(Base):
    def test_joins_existing_profile_files(self):
        (self.dir / "a.md").write_text("Nurse, part-time")
        self.cfg["profile_files"] = [str(self.dir / "a.md"), str(self.dir / "missing.md")]
        self.assertEqual(app.profile_text(self.cfg), "Nurse, part-time")

    def test_fallback_assumes_no_field(self):
        self.cfg["profile_files"] = [str(self.dir / "missing.md")]
        text = app.profile_text(self.cfg).lower()
        self.assertIn("no profile", text)
        self.assertNotIn("backend", text)


SEARCH = {"filter": {"include": ["pflegefachkraft"], "locations": ["Kassel"]}}


def ask(text):
    return {"done": False, "say": text}


DONE = {"done": True, "summary": "Teilzeit Pflege bei Kassel", "profile_md": "# Profil\nPflege", "search": SEARCH}


class InterviewFlowTest(Base):
    def setUp(self):
        super().setUp()
        (self.dir / "profile.md").unlink()
        self.replies, self.calls = [], []
        self.polls, self.poll_result, self.count = [], {"notified": 3}, 12
        self.found, self.finds, self.before_find = [], [], None

    def fake_poll(self, cfg, store, bot, trigger=None):
        self.polls.append(cfg["instance"] if "instance" in cfg else "x")
        if isinstance(self.poll_result, Exception):
            raise self.poll_result
        return self.poll_result

    def fake_find(self, field, known, model, on_usage=None):
        self.finds.append((field, known))
        self.polls_before_find = len(self.polls)
        *self.before_find, notice = [text for text, _ in self.bot.sent]
        assert notice.startswith("🔎"), notice
        return self.found

    def fake_turn(self, log, previous):
        self.calls.append((list(log), previous))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def update(self, upd):
        app.handle_update(upd, self.cfg, self.store, self.bot, interview_fn=self.fake_turn,
                          count_fn=lambda cfg: self.count, background=lambda fn: fn(),
                          poll_fn=self.fake_poll, find_sources_fn=self.fake_find, check_feed_fn=lambda url: 3)

    def send(self, text, *replies):
        self.replies += replies
        self.update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}})

    def press(self, data):
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}})

    def propose(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Pflege, Teilzeit, Kassel", DONE)

    def test_start_without_profile_begins_interview(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.assertIn("Was machst du beruflich?", self.bot.sent[-1][0])
        self.assertEqual(self.store.get_state("interview"), "active")
        self.assertEqual(self.store.interview_log(), [("bot", "Was machst du beruflich?")])
        self.assertGreater(self.bot.typed, 0)

    def test_keeps_typing_while_claude_is_slow_and_stops_after(self):
        def slow_turn(log, previous):
            time.sleep(0.2)
            return ask("Was machst du beruflich?")
        with mock.patch.object(app, "TYPING_EVERY_S", 0.02):
            app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/start"}},
                              self.cfg, self.store, self.bot, interview_fn=slow_turn)
            typed = self.bot.typed
            time.sleep(0.1)
        self.assertGreaterEqual(typed, 4)
        self.assertEqual(self.bot.typed, typed)

    def test_one_failed_typing_call_does_not_stop_the_indicator(self):
        calls = []
        def flaky_typing():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("network hiccup")
        self.bot.typing = flaky_typing
        def slow_turn(log, previous):
            time.sleep(0.2)
            return ask("Was machst du beruflich?")
        with mock.patch.object(app, "TYPING_EVERY_S", 0.02):
            app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/start"}},
                              self.cfg, self.store, self.bot, interview_fn=slow_turn)
        self.assertGreaterEqual(len(calls), 3)

    def test_shows_a_wait_note_until_the_answer_and_then_removes_it(self):
        def turn(log, previous):
            self.assertEqual(self.bot.notes, [(1, app.THINKING)])
            self.assertEqual(self.bot.deleted, [])
            return ask("Was machst du beruflich?")
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/start"}},
                          self.cfg, self.store, self.bot, interview_fn=turn)
        self.assertEqual(self.bot.deleted, [1])
        self.assertIn("Was machst du beruflich?", self.bot.sent[-1][0])

    def test_start_with_profile_shows_help(self):
        (self.dir / "profile.md").write_text("x")
        self.send("/start")
        self.assertIn("/setup", self.bot.sent[-1][0])
        self.assertEqual(self.calls, [])

    def test_answers_go_to_the_interview_even_with_a_link(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Mein Profil: https://example.com/me", ask("Danke! Wo wohnst du?"))
        self.assertEqual(self.calls[-1][0][-1], ("person", "Mein Profil: https://example.com/me"))
        self.assertIsNone(self.store.get(1))
        self.assertIn("Wo wohnst du?", self.bot.sent[-1][0])

    def test_finished_interview_proposes_summary_with_buttons(self):
        self.propose()
        text, buttons = self.bot.sent[-1]
        self.assertIn("Teilzeit Pflege bei Kassel", text)
        self.assertEqual([data for _, data in buttons[0]], ["i:ok", "i:edit"])
        self.assertEqual(self.store.get_state("interview"), "proposed")

    def test_confirm_writes_profile_and_search_and_reports_count(self):
        self.propose()
        self.press("i:ok")
        self.assertEqual((self.dir / "profile.md").read_text(), "# Profil\nPflege\n")
        self.assertEqual(json.loads((self.dir / "search.json").read_text()), SEARCH)
        self.assertEqual(self.store.get_state("interview"), "")
        self.assertEqual(self.store.interview_log(), [])
        self.assertTrue(any("12" in text for text, _ in self.bot.sent))

    def test_confirm_starts_the_first_poll_right_away_when_no_board_is_offered(self):
        self.propose()
        self.press("i:ok")
        self.assertEqual(len(self.polls), 1)
        texts = [text for text, _ in self.bot.sent]
        self.assertTrue(any("12" in t for t in texts))
        self.assertIn("regelmäßig", self.bot.sent[-1][0])

    def test_boards_are_offered_before_the_first_poll(self):
        self.found = [{"name": "greenjobs", "url": "https://g/feed", "why": "Naturschutz"}]
        self.propose()
        self.press("i:ok")
        self.assertEqual(self.polls_before_find, 0)
        self.assertEqual(self.polls, [])
        text, buttons = self.bot.sent[-1]
        self.assertIn("Jetzt suchen", text)
        self.assertEqual(buttons, [[("🔎 Jetzt suchen", "q:go")]])
        self.press("q:add:0")
        self.press("q:go")
        self.assertEqual(len(self.polls), 1)

    def test_search_button_shares_the_cooldown_of_suchen(self):
        self.found = [{"name": "greenjobs", "url": "https://g/feed", "why": "Naturschutz"}]
        self.propose()
        self.press("i:ok")
        self.press("q:go")
        self.press("q:go")
        self.assertEqual(len(self.polls), 1)
        self.assertIn("gerade erst gesucht", self.bot.sent[-1][0])

    def test_first_poll_uses_the_settings_just_saved(self):
        seen = []
        self.fake_poll = lambda cfg, store, bot, trigger: seen.append(cfg) or seen.append(trigger) or {"notified": 1}
        self.propose()
        self.press("i:ok")
        self.assertEqual(seen[1], "setup")  # measured apart from the timer's polls (#17)
        self.assertEqual(seen[0]["filter"]["include"], ["pflegefachkraft"])
        self.assertEqual(seen[0]["db_path"], self.cfg["db_path"])

    def test_first_poll_without_good_matches_says_so(self):
        self.poll_result = {"notified": 0}
        self.propose()
        self.press("i:ok")
        self.assertIn("keine", self.bot.sent[-1][0].lower())

    def test_no_first_poll_when_nothing_matches(self):
        self.count = 0
        self.propose()
        self.press("i:ok")
        self.assertEqual(self.polls, [])

    def test_first_poll_skipped_by_a_running_poll_stays_quiet(self):
        from job_radar.store import PollLocked
        self.poll_result = PollLocked("busy")
        self.propose()
        self.press("i:ok")
        sent = len(self.bot.sent)
        self.assertEqual(len(self.polls), 1)
        self.assertNotIn("regelmäßig", self.bot.sent[-1][0])
        self.assertEqual(len(self.bot.sent), sent)

    def test_failed_first_poll_points_to_the_hourly_run(self):
        self.poll_result = RuntimeError("portal down")
        self.propose()
        self.press("i:ok")
        self.assertIn("nächsten", self.bot.sent[-1][0])

    def test_board_search_gets_field_and_region_but_not_the_profile(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Pflege", {**DONE, "profile_md": "# Profil\nAnna Muster, Tel. 0171 123456, Pflege"})
        self.press("i:ok")
        self.assertIn("0171", (self.dir / "profile.md").read_text())
        field = self.finds[0][0]
        self.assertIn("pflegefachkraft", field)
        self.assertIn("Kassel", field)
        self.assertNotIn("0171", field)

    def test_confirm_searches_job_boards_before_the_first_poll(self):
        self.propose()
        self.press("i:ok")
        self.assertEqual(len(self.finds), 1)
        self.assertIn("pflege", self.finds[0][0])
        self.assertEqual(self.polls_before_find, 0)

    def test_confirm_searches_job_boards_even_when_nothing_matches(self):
        self.count = 0
        self.propose()
        self.press("i:ok")
        self.assertEqual(len(self.finds), 1)

    def test_setup_keeps_the_feeds_the_person_added(self):
        feeds = [{"name": "greenjobs", "url": "https://g/feed"}]
        (self.dir / "search.json").write_text(json.dumps({"sources": {"feeds": {"feeds": feeds}}}))
        self.propose()
        self.press("i:ok")
        saved = json.loads((self.dir / "search.json").read_text())
        self.assertEqual(saved["sources"]["feeds"]["feeds"], feeds)
        self.assertEqual(saved["filter"], SEARCH["filter"])
        self.assertEqual(self.finds[0][1], ["https://g/feed"])

    def test_setup_keeps_the_boards_without_feed(self):
        boards = [{"name": "NABU", "url": "https://nabu/jobs"}]
        (self.dir / "search.json").write_text(json.dumps({"sources": {"pages": {"pages": boards}}}))
        self.propose()
        self.press("i:ok")
        self.assertEqual(json.loads((self.dir / "search.json").read_text())["sources"]["pages"]["pages"], boards)

    def test_setup_keeps_the_persons_poll_times(self):
        times = {"weekdays": [8], "weekend": []}
        (self.dir / "search.json").write_text(json.dumps({"schedule": times}))
        self.propose()
        self.press("i:ok")
        self.assertEqual(json.loads((self.dir / "search.json").read_text())["schedule"], times)

    def test_setup_keeps_the_persons_posting_age(self):
        (self.dir / "search.json").write_text(json.dumps({"sources": {"ba": {"published_since_days": 60}}}))
        self.propose()
        self.press("i:ok")
        saved = json.loads((self.dir / "search.json").read_text())
        self.assertEqual(saved["sources"]["ba"]["published_since_days"], 60)
        self.assertTrue(any("letzten 60 Tagen" in text for text, _ in self.bot.sent))

    def test_link_asked_for_by_recherche_during_setup_starts_a_report(self):
        reports = []
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("/recherche")
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "https://jobs.example.com/x"}},
                          self.cfg, self.store, self.bot, interview_fn=self.fake_turn, background=lambda fn: None,
                          report_fn=lambda posting, **kw: reports.append(posting))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.get(1).status, "interesting")
        self.assertEqual(self.store.get_state("interview"), "active")

    def test_change_reopens_interview_with_the_proposal_in_the_transcript(self):
        self.propose()
        self.press("i:edit")
        self.assertEqual(self.store.get_state("interview"), "active")
        self.send("Lieber 50 km", DONE)
        log = self.calls[-1][0]
        self.assertTrue(any("Teilzeit Pflege bei Kassel" in text for _, text in log[:-1]))
        self.assertEqual(log[-1], ("person", "Lieber 50 km"))

    def test_setup_with_existing_profile_passes_it_as_previous(self):
        (self.dir / "profile.md").write_text("# Altes Profil")
        self.send("/setup", ask("Was hat sich geändert?"))
        self.assertEqual(self.calls[0][1], "# Altes Profil")
        self.assertEqual(self.store.get_state("interview"), "active")

    def test_setup_drops_an_old_proposal_even_if_the_first_turn_fails(self):
        self.propose()
        self.send("/setup", InterviewError("bad"))
        self.press("i:ok")
        self.assertFalse((self.dir / "search.json").exists())
        self.assertIn("nichts zu bestätigen", self.bot.sent[-1][0])

    def test_abbrechen_ends_the_interview_and_forgets_it(self):
        self.propose()
        self.send("/abbrechen")
        self.assertIn("abgebrochen", self.bot.sent[-1][0])
        self.assertEqual(self.store.get_state("interview"), "")
        self.assertEqual(self.store.get_state("interview_proposal"), "")
        self.assertEqual(self.store.interview_log(), [])
        self.send("Noch was")  # no longer goes to the interview
        self.assertEqual(len(self.calls), 2)
        self.press("i:ok")
        self.assertIn("nichts zu bestätigen", self.bot.sent[-1][0])

    def test_abbrechen_keeps_an_existing_profile(self):
        (self.dir / "profile.md").write_text("# Altes Profil")
        self.send("/setup", ask("Was hat sich geändert?"))
        self.send("/abbrechen")
        self.assertIn("bisheriges Profil bleibt", self.bot.sent[-1][0])
        self.assertEqual((self.dir / "profile.md").read_text(), "# Altes Profil")

    def test_abbrechen_without_interview_says_so(self):
        self.send("/abbrechen")
        self.assertIn("läuft gerade keine Einrichtung", self.bot.sent[-1][0])

    def test_abbrechen_is_in_the_menu_help_and_prompt(self):
        self.assertIn("abbrechen", [name for name, _ in app.COMMANDS])
        self.assertIn("/abbrechen", app.HELP)
        self.assertIn("/abbrechen", (Path(app.__file__).parent.parent / "prompts" / "interview.md").read_text())

    def test_profile_is_shown_readably(self):
        (self.dir / "profile.md").write_text("# Profil\n- **Ort:** Kassel <Nord>")
        self.send("/profile")
        self.assertEqual(self.bot.sent[-1][0], "<b>Profil</b>\n• <b>Ort:</b> Kassel &lt;Nord&gt;")

    def test_proposal_summary_is_shown_readably(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Pflege", {**DONE, "summary": "- **Teilzeit** in Kassel"})
        self.assertIn("• <b>Teilzeit</b> in Kassel", self.bot.sent[-1][0])

    def test_profile_command_shows_profile_or_hint(self):
        self.send("/profile")
        self.assertIn("/setup", self.bot.sent[-1][0])
        (self.dir / "profile.md").write_text("# Profil <Pflege>")
        self.send("/profile")
        self.assertIn("<b>Profil &lt;Pflege&gt;</b>", self.bot.sent[-1][0])

    def test_failed_turn_keeps_the_transcript_and_asks_to_resend(self):
        self.send("/start", ask("Was machst du beruflich?"))
        self.send("Pflege", InterviewError("bad"))
        self.assertEqual(self.store.interview_log(), [("bot", "Was machst du beruflich?")])
        self.assertIn("nochmal", self.bot.sent[-1][0].lower())

    def test_poll_without_profile_nudges_once_and_fetches_nothing(self):
        def fetch(cfg):
            raise AssertionError("must not fetch")
        for _ in range(2):
            result = app.poll(self.cfg, self.store, self.bot, fetch=fetch)
        self.assertEqual(result, {"skipped": "no profile"})
        self.assertEqual(len([t for t, _ in self.bot.sent if "/setup" in t]), 1)


class CheckPageTest(Base):
    def test_checking_a_board_is_recorded_apart_from_poll_extraction(self):  # #17
        def extract(page, model, on_usage):
            on_usage({"model": model, "cost_usd": 0.002})
            return [{"link": 1, "title": "Gärtner"}]

        n = app.check_page("https://b/", self.store, "haiku", get=lambda url: '<a href="/j/1">Gärtner</a>',
                           extract_fn=extract, cache_path=self.dir / "pages-cache.json")
        self.assertEqual(n, (1, ""))
        self.assertEqual(set(self.store.usage_summary(1)), {"pages_check"})

    @staticmethod
    def no_model(page, model, on_usage):
        raise AssertionError("the model must not be asked")

    def test_a_board_with_json_ld_postings_is_checked_by_the_model_once(self):
        cache = self.dir / "pages-cache.json"
        jobs = "".join('<script type="application/ld+json">%s</script>' % json.dumps(
            {"@type": "JobPosting", "title": f"Gärtner {i}", "url": f"https://b/jobs/{i}"}) for i in (1, 2))
        page = f'<head>{jobs}</head><a href="/jobs/1">Gärtner 1</a><a href="/jobs/2">Gärtner 2</a>'
        n = app.check_page("https://b/", self.store, "haiku", get=lambda url: page, cache_path=cache,
                           extract_fn=lambda page, model, on_usage: [{"link": 1, "title": "Gärtner 1"},
                                                                     {"link": 2, "title": "Gärtner 2"}])
        self.assertEqual(n, (2, ""))
        found = pages.fetch({"pages": [{"name": "B", "url": "https://b/"}]}, None, get_text=lambda url: page,
                            extract=lambda page: self.no_model(page, None, None), cache_path=cache)
        self.assertEqual(len(found), 2)

    def test_a_board_whose_page_names_a_feed_of_its_postings_gives_the_feed(self):
        web = {"https://b/jobs": '<head><link rel="alternate" type="application/rss+xml" href="/feed"></head>'
                                 '<ul><li><a href="/jobs/1">Gärtner 1</a></li><li><a href="/jobs/2">Gärtner 2</a></li></ul>',
               "https://b/feed": '<rss><channel><item><title>Gärtner 1</title><link>https://b/jobs/1</link></item>'
                                 '<item><title>Gärtner 2</title><link>https://b/jobs/2</link></item></channel></rss>'}

        def extract(page, model, on_usage):
            return [{"link": 1, "title": "Gärtner 1"}, {"link": 2, "title": "Gärtner 2"}]

        n = app.check_page("https://b/jobs", self.store, "haiku", get=web.__getitem__, extract_fn=extract,
                           cache_path=self.dir / "pages-cache.json")
        self.assertEqual(n, (2, "https://b/feed"))

    def test_the_check_fills_the_cache_so_the_first_poll_asks_no_model(self):
        cache = self.dir / "pages-cache.json"
        page = '<ul><li><a href="/jobs/1">Gärtner 1</a></li></ul>'
        app.check_page("https://b/jobs", self.store, "haiku", get=lambda url: page, cache_path=cache,
                       extract_fn=lambda page, model, on_usage: [{"link": 1, "title": "Gärtner 1"}])
        found = pages.fetch({"pages": [{"name": "B", "url": "https://b/jobs"}]}, None, get_text=lambda url: page,
                            extract=lambda text: self.fail("asked again"), cache_path=cache)
        self.assertEqual([(p.title, p.tags) for p in found], [("Gärtner 1", ["B"])])

    def test_locked_poll_passes_the_trigger_on(self):
        self.cfg["db_path"] = str(self.dir / "db.sqlite")
        with mock.patch.object(app, "poll", return_value={}) as poll:
            app.locked_poll(self.cfg, self.store, self.bot, trigger="suchen")
        self.assertEqual(poll.call_args.kwargs["trigger"], "suchen")


class SourcesTest(Base):
    def setUp(self):
        super().setUp()
        self.found = [{"name": "greenjobs", "url": "https://g/feed", "why": "Umweltjobs"},
                      {"name": "kaputt", "url": "https://broken/feed", "why": "x"},
                      {"name": "schon da", "url": "https://known/feed", "why": "x"},
                      {"name": "leer", "url": "https://empty/feed", "why": "x"}]
        self.finds = []

    def fake_find(self, field, known, model, on_usage=None):
        self.finds.append(known)
        if isinstance(self.found, Exception):
            raise self.found
        return self.found

    @staticmethod
    def check(url):
        if "broken" in url:
            raise OSError("404")
        return 0 if "empty" in url else 7

    @classmethod
    def check_page(cls, url):
        return cls.check(url), "https://nabu/feed.xml" if "with-feed" in url else ""

    def update(self, upd):
        app.handle_update(upd, self.cfg, self.store, self.bot, background=lambda fn: fn(),
                          find_sources_fn=self.fake_find, check_feed_fn=self.check, check_page_fn=self.check_page)

    def send(self, text):
        self.update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}})

    def press(self, data):
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}})

    def feeds(self):
        path = self.dir / "search.json"
        return json.loads(path.read_text()).get("sources", {}).get("feeds", {}).get("feeds", []) if path.exists() else []

    def search(self):
        self.send("/quellen")
        self.press("q:find")

    def write_feeds(self, feeds):
        (self.dir / "search.json").write_text(json.dumps({"filter": {"include": ["kotlin"]},
                                                          "sources": {"feeds": {"feeds": feeds}}}))

    def test_quellen_suggests_only_new_feeds_that_load(self):
        self.write_feeds([{"name": "known", "url": "https://known/feed"}])
        self.search()
        self.assertEqual(self.finds, [["https://known/feed"]])
        offers = [(text, buttons) for text, buttons in self.bot.sent if buttons and "q:add" in buttons[0][0][1]]
        self.assertEqual(len(offers), 1)
        self.assertIn("greenjobs", offers[0][0])
        self.assertEqual([data for _, data in offers[0][1][0]], ["q:add:0", "q:no:0"])

    def test_quellen_lists_without_searching_and_offers_a_search_button(self):
        for feeds in ([], [{"name": "known", "url": "https://known/feed"}]):
            self.write_feeds(feeds)
            self.send("/quellen")
            self.assertEqual(self.finds, [])
            self.assertIn("q:find", [data for row in self.bot.sent[-1][1] for _, data in row])

    def test_quellen_names_the_big_portals_too(self):
        self.write_feeds([{"name": "known", "url": "https://known/feed"}])
        self.send("/quellen")
        text = self.bot.sent[-1][0]
        self.assertIn("Bundesagentur für Arbeit", text)
        self.assertIn("Arbeitnow", text)
        self.cfg["sources"]["arbeitnow"]["enabled"] = False
        self.send("/quellen")
        self.assertNotIn("Arbeitnow", self.bot.sent[-1][0])

    def test_search_button_searches(self):
        self.send("/quellen")
        self.press("q:find")
        self.assertEqual(len(self.finds), 1)

    def test_quellen_lists_the_current_feeds_with_remove_buttons(self):
        self.write_feeds([{"name": "known", "url": "https://known/feed"}])
        self.send("/quellen")
        text, buttons = next((t, b) for t, b in self.bot.sent if b and b[0][0][1].startswith("q:del"))
        self.assertIn("known", buttons[0][0][0])

    def test_add_button_puts_the_feed_into_the_search_settings(self):
        self.write_feeds([])
        self.search()
        self.press("q:add:0")
        self.assertEqual(self.feeds(), [{"name": "greenjobs", "url": "https://g/feed"}])
        self.assertEqual(json.loads((self.dir / "search.json").read_text())["filter"], {"include": ["kotlin"]})
        self.press("q:add:0")
        self.assertEqual(len(self.feeds()), 1)

    def test_add_works_without_a_search_file(self):
        self.search()
        self.press("q:add:0")
        self.assertEqual(self.feeds(), [{"name": "greenjobs", "url": "https://g/feed"}])

    def test_remove_button_drops_the_feed(self):
        self.write_feeds([{"name": "a", "url": "https://a/feed"}, {"name": "b", "url": "https://b/feed"}])
        self.found = []
        self.search()
        self.press("q:del:0")
        self.assertEqual(self.feeds(), [{"name": "b", "url": "https://b/feed"}])
        self.press("q:del:0")  # the old button still points at "a", which is gone
        self.assertEqual(self.feeds(), [{"name": "b", "url": "https://b/feed"}])

    def pages(self):
        path = self.dir / "search.json"
        return json.loads(path.read_text()).get("sources", {}).get("pages", {}).get("pages", []) if path.exists() else []

    def test_boards_without_feed_are_offered_when_postings_can_be_read(self):
        self.found = [{"name": "NABU", "url": "https://nabu/jobs", "why": "Naturschutz", "kind": "page"},
                      {"name": "leer", "url": "https://empty/jobs", "why": "x", "kind": "page"}]
        self.search()
        offers = [text for text, buttons in self.bot.sent if buttons and "q:add" in buttons[0][0][1]]
        self.assertEqual(len(offers), 1)
        self.assertIn("ohne Feed: ich lese die Seite bei jeder Suche, wo nötig mit KI", offers[0])
        self.press("q:add:0")
        self.assertEqual(self.pages(), [{"name": "NABU", "url": "https://nabu/jobs"}])
        self.assertEqual(self.feeds(), [])

    def test_a_board_whose_page_names_a_feed_of_its_postings_is_offered_as_that_feed(self):
        self.found = [{"name": "NABU", "url": "https://nabu/with-feed/jobs", "why": "Naturschutz", "kind": "page"}]
        self.search()
        offers = [text for text, buttons in self.bot.sent if buttons and "q:add" in buttons[0][0][1]]
        self.assertNotIn("ohne Feed", offers[0])
        self.press("q:add:0")
        self.assertEqual(self.feeds(), [{"name": "NABU", "url": "https://nabu/feed.xml"}])
        self.assertEqual(self.pages(), [])

    def test_pages_are_listed_known_and_removable(self):
        (self.dir / "search.json").write_text(json.dumps({"sources": {
            "feeds": {"feeds": [{"name": "G", "url": "https://g/feed"}]},
            "pages": {"pages": [{"name": "NABU", "url": "https://nabu/jobs"}]}}}))
        self.found = []
        self.search()
        self.assertEqual(self.finds, [["https://g/feed", "https://nabu/jobs"]])
        text, buttons = next((t, b) for t, b in self.bot.sent if b and b[0][0][1].startswith("q:del"))
        self.assertEqual([row[0][0] for row in buttons], ["🗑 G", "🗑 NABU", "🔎 Neue Jobbörsen suchen",
                                                         "🏢 Initiativbewerbungen einschalten"])
        self.press("q:del:1")
        self.assertEqual(self.pages(), [])
        self.assertEqual(self.feeds(), [{"name": "G", "url": "https://g/feed"}])

    def test_stale_or_bad_buttons_are_harmless(self):
        for data in ("q:add:5", "q:add:x", "q:del:3", "q:no:0", "q:zap:1"):
            self.press(data)
        self.assertFalse((self.dir / "search.json").exists())

    def test_source_search_is_marked_busy_while_it_runs(self):
        seen = []
        self.find = lambda **kw: seen.append(self.store.get_state("busy:/quellen search")) or []
        app.suggest_sources(self.cfg, self.store, self.bot, find_fn=lambda **kw: self.find(**kw),
                            check_fn=lambda url: 3)
        self.assertTrue(seen[0])
        self.assertEqual(self.store.get_state("busy:/quellen search"), "")

    def test_failed_source_search_clears_its_busy_mark(self):
        def boom(**kw):
            raise RuntimeError("x")
        app.suggest_sources(self.cfg, self.store, self.bot, find_fn=boom, check_fn=lambda url: 3)
        self.assertEqual(self.store.get_state("busy:/quellen search"), "")

    def test_nothing_found_says_so(self):
        self.found = []
        self.search()
        self.assertIn("keine", self.bot.sent[-1][0].lower())

    def test_failed_search_says_so(self):
        self.found = RuntimeError("claude down")
        self.search()
        self.assertIn("/quellen", self.bot.sent[-1][0])

    def test_help_and_menu_mention_quellen(self):
        self.assertIn("/quellen", app.HELP)
        self.assertIn("quellen", [name for name, _ in app.COMMANDS])


class RouterTest(unittest.TestCase):
    def setUp(self):
        import os
        from unittest import mock
        from job_radar import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d")})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.config_dir().mkdir(parents=True)
        (config.config_dir() / "env").write_text("TELEGRAM_BOT_TOKEN=t\n")
        config.create_instance("anna")
        config.set_env_value(config.config_dir("anna") / "env", "TELEGRAM_CHAT_ID", "42")
        self.bots = {}

        def make_bot(token, chat_id):
            bot = FakeBot()
            bot.chat_id = chat_id
            self.bots[chat_id] = bot
            return bot

        self.registry = app.Registry("t", make_bot=make_bot)
        self.addCleanup(self.registry.close)
        self.handled = []

    def handle(self, upd, cfg, store, bot):
        self.handled.append((cfg["instance"], upd.get("message", {}).get("text"), bot.chat_id))

    def msg(self, chat, text):
        app.dispatch({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}, self.registry, handle=self.handle)

    def test_known_chat_goes_to_its_instance(self):
        self.msg(42, "hallo")
        self.assertEqual(self.handled, [("anna", "hallo", "42")])

    def test_buttons_are_routed_by_their_message_chat(self):
        upd = {"update_id": 1, "callback_query": {"id": "c", "data": "s:1", "message": {"chat": {"id": 42}}}}
        app.dispatch(upd, self.registry, handle=self.handle)
        self.assertEqual(self.handled[0][0], "anna")

    def test_unknown_chat_is_told_it_needs_an_invite(self):
        for text in ("/start", "/start wrongcode"):
            self.msg(7, text)
            self.assertIn("Einladungslink", self.bots.pop("7").sent[0][0])
        self.assertEqual(self.handled, [])
        self.assertIsNone(self.registry.get("7"))

    def test_unknown_chat_buttons_get_no_reply(self):
        self.registry.reload()
        upd = {"update_id": 1, "callback_query": {"id": "c", "data": "s:1", "message": {"chat": {"id": 7}}}}
        app.dispatch(upd, self.registry, handle=self.handle)
        self.assertNotIn("7", self.bots)

    def test_invite_code_binds_chat_and_starts(self):
        code = self.config.create_invite("ben")
        self.msg(7, f"/start {code}")
        self.assertEqual(self.handled, [("ben", "/start", "7")])
        self.assertEqual(self.config.instance_env("ben")["TELEGRAM_CHAT_ID"], "7")
        self.msg(8, f"/start {code}")
        self.assertEqual(len(self.handled), 1)

    def test_invite_from_a_bound_chat_moves_the_chat(self):
        code = self.config.create_invite("ben")
        self.msg(42, f"/start {code}")
        self.assertEqual(self.handled, [("ben", "/start", "42")])
        self.assertEqual(self.config.instance_env("ben")["TELEGRAM_CHAT_ID"], "42")
        self.assertFalse(self.config.instance_env("anna").get("TELEGRAM_CHAT_ID"))
        self.assertEqual(self.registry.get("42")[0]["instance"], "ben")
        self.assertIn("neuen Einladungslink", self.bots["42"].sent[-1][0])
        self.assertEqual(self.config.open_invites(), 0)

    def test_invite_for_the_own_instance_just_starts(self):
        code = self.config.create_invite("anna")
        self.msg(42, f"/start {code}")
        self.assertEqual(self.handled, [("anna", "/start", "42")])
        self.assertEqual(self.config.instance_env("anna")["TELEGRAM_CHAT_ID"], "42")

    def test_used_invite_from_a_bound_chat_starts_nothing(self):
        self.msg(42, "/start usedcode")
        self.assertEqual(self.handled, [])
        self.assertIn("nicht mehr gültig", self.bots["42"].sent[-1][0])

    def test_each_update_is_logged_without_text_or_chat_id(self):
        now = int(time.time())
        with self.assertLogs("job-radar", "INFO") as logs:
            app.dispatch({"update_id": 1, "message": {"chat": {"id": 42}, "date": now - 3,
                                                      "text": "Ich heiße Anna"}}, self.registry, handle=self.handle)
            app.dispatch({"update_id": 2, "message": {"chat": {"id": 42}, "date": now, "text": "/quellen jetzt"}},
                         self.registry, handle=self.handle)
            app.dispatch({"update_id": 3, "callback_query": {"id": "c", "data": "q:add:2",
                                                             "message": {"chat": {"id": 42}, "date": now - 60}}},
                         self.registry, handle=self.handle)
        out = "\n".join(logs.output)
        self.assertRegex(out, r"anna: message, sent [34] s ago")
        self.assertIn("anna: /quellen", out)
        self.assertRegex(out, r"anna: button q:add:2, card sent 6\d s ago")
        self.assertRegex(out, r"anna: button q:add:2 handled in \d+\.\d s, waited \d+\.\d s")
        self.assertNotIn("Anna", out)
        self.assertNotIn("jetzt", out)
        self.assertNotIn("42", out)

    def test_handling_an_update_records_when(self):
        self.msg(42, "hallo")
        last = float(self.registry.get("42")[1].get_state("last_update"))
        self.assertLess(abs(time.time() - last), 5)

    def test_a_slow_news_send_does_not_hold_up_updates(self):
        import threading
        release, news_done_when_polled = threading.Event(), []
        self.make_news_slow(release)

        class Receiver(FakeBot):
            def updates(self, wait=0):
                news_done_when_polled.append(release.is_set())
                raise app.Stopping()

        threading.Timer(1, release.set).start()  # the news send blocks for 1 s
        app.listen(self.registry, Receiver())
        self.assertEqual(news_done_when_polled, [False])

    def make_news_slow(self, release):
        bot = self.registry.get("42")[2]
        slow_send = bot.send

        def send(text, buttons=None):
            release.wait(5)
            slow_send(text, buttons)
        bot.send = send

    def test_stop_during_startup_is_clean(self):
        class Receiver(FakeBot):
            def set_commands(self, commands):
                raise app.Stopping()

        app.listen(self.registry, Receiver())  # returns instead of raising

    def test_listen_stops_on_request_and_lets_queued_jobs_finish(self):
        import threading
        done = threading.Event()

        class Receiver(FakeBot):
            calls = 0

            def updates(self, wait=0):
                Receiver.calls += 1
                if Receiver.calls == 1:
                    return [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "hallo"}}]
                raise app.Stopping()

        def slow(upd, cfg, store, bot):
            time.sleep(0.2)
            done.set()

        app.listen(self.registry, Receiver(), handle=lambda upd, reg, run: app.dispatch(upd, reg, handle=slow, run=run))
        self.assertTrue(done.is_set())

    def test_unknown_chats_and_joins_are_logged_without_chat_id(self):
        code = self.config.create_invite("ben")
        with self.assertLogs("job-radar", "INFO") as logs:
            self.msg(4711, "hallo")
            self.msg(4711, f"/start {code}")
        out = "\n".join(logs.output)
        self.assertNotIn("4711", out)
        self.assertNotIn(code, out)
        self.assertIn("joined as instance ben", out)

    def test_a_redeemed_invite_starts_the_instances_timer(self):
        started = []
        self.registry.on_join = started.append
        code = self.config.create_invite("ben")
        self.msg(7, f"/start {code}")
        self.assertEqual(started, ["ben"])

    def test_admin_gets_einladen_in_the_menu(self):
        (self.config.config_dir() / "operator.json").write_text('{"admin": "anna"}')
        receiver = FakeBot()
        receiver.commands = []
        receiver.set_commands = lambda commands, chat_id=None: receiver.commands.append((chat_id, [c for c, _ in commands]))
        app.listen(self.registry, receiver, stop=lambda: True)
        everyone = dict(receiver.commands)[None]
        admin = dict(receiver.commands)["42"]
        self.assertNotIn("einladen", everyone)
        self.assertIn("einladen", admin)

    def test_listen_tells_everyone_what_is_new(self):
        from job_radar import changes
        app.listen(self.registry, FakeBot(), stop=lambda: True)
        newest = changes.load()[0][0]
        self.assertIn("Neu im Job-Radar", self.bots["42"].sent[0][0])
        self.assertEqual(self.registry.get("42")[1].get_state(changes.STATE_KEY), newest)

    def test_new_person_gets_no_changelog_backlog(self):
        from job_radar import changes
        code = self.config.create_invite("ben")
        self.msg(7, f"/start {code}")
        self.assertEqual(self.registry.get("7")[1].get_state(changes.STATE_KEY), changes.load()[0][0])

    def test_registry_lists_bound_instances(self):
        self.config.create_instance("unbound")
        self.assertEqual([cfg["instance"] for cfg, _, _ in self.registry.all()], ["anna"])

    def test_broken_instance_config_does_not_drop_the_others(self):
        self.config.create_instance("bad")
        self.config.set_env_value(self.config.config_dir("bad") / "env", "TELEGRAM_CHAT_ID", "9")
        (self.config.config_dir("bad") / "config.json").write_text("{")
        self.config.create_instance("zed")
        self.config.set_env_value(self.config.config_dir("zed") / "env", "TELEGRAM_CHAT_ID", "50")
        self.registry.reload()
        self.assertIsNotNone(self.registry.get("42"))
        self.assertIsNotNone(self.registry.get("50"))
        self.assertIsNone(self.registry.get("9"))

    def test_dispatch_hands_work_to_the_runner_keyed_by_chat(self):
        queued = []
        app.dispatch({"update_id": 1, "message": {"chat": {"id": 42}, "text": "hallo"}}, self.registry,
                     handle=self.handle, run=lambda chat, fn: queued.append((chat, fn)))
        self.assertEqual([chat for chat, _ in queued], ["42"])
        self.assertEqual(self.handled, [])
        queued[0][1]()
        self.assertEqual(self.handled, [("anna", "hallo", "42")])


class ChatWorkersTest(unittest.TestCase):
    def test_a_slow_chat_does_not_block_others_and_keeps_its_order(self):
        import threading
        workers = app.ChatWorkers()
        release, done, order = threading.Event(), threading.Event(), []
        workers.submit("a", lambda: (release.wait(5), order.append("a1")))
        workers.submit("a", lambda: order.append("a2"))
        workers.submit("b", lambda: (order.append("b1"), done.set()))
        self.assertTrue(done.wait(5))
        self.assertEqual(order, ["b1"])
        release.set()
        workers.join()
        self.assertEqual(order, ["b1", "a1", "a2"])

    def test_join_gives_up_after_its_timeout(self):
        import threading
        workers = app.ChatWorkers()
        release = threading.Event()
        workers.submit("a", lambda: release.wait(5))
        self.assertFalse(workers.join(timeout=0.1))
        release.set()
        self.assertTrue(workers.join(timeout=5))

    def test_a_growing_queue_is_logged(self):
        import threading
        workers = app.ChatWorkers()
        release = threading.Event()
        with self.assertLogs("job-radar", "INFO") as logs:
            workers.submit("a", lambda: release.wait(5))
            time.sleep(0.05)  # the first job is running, not waiting
            workers.submit("a", lambda: None)
            workers.submit("a", lambda: None)
        release.set()
        workers.join()
        self.assertTrue(any("2 updates waiting" in line for line in logs.output), logs.output)

    def test_a_failing_job_does_not_stop_the_chat(self):
        workers = app.ChatWorkers()
        ran = []
        workers.submit("a", lambda: 1 / 0)
        workers.submit("a", lambda: ran.append(1))
        workers.join()
        self.assertEqual(ran, [1])


class FeedbackTest(Base):
    def setUp(self):
        super().setUp()
        self.given, self.error = [], None

    def fake_feedback(self, cfg, text):
        if self.error:
            raise self.error
        self.given.append(text)
        return "https://github.com/o/r/issues/1"

    def send(self, text):
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, self.cfg, self.store,
                          self.bot, background=lambda fn: fn(), feedback_fn=self.fake_feedback,
                          interview_fn=lambda *a, **kw: self.fail_test("interview ran"))

    def fail_test(self, why):
        raise AssertionError(why)

    def test_feedback_with_text_opens_an_issue(self):
        self.send("/feedback Die Karten sind zu lang")
        self.assertEqual(self.given, ["Die Karten sind zu lang"])
        self.assertIn("Danke", self.bot.sent[-1][0])

    def test_bare_feedback_asks_and_takes_the_next_message(self):
        self.store.set_state("interview", "active")
        self.send("/feedback")
        self.assertEqual(self.given, [])
        self.assertIn("Schreib", self.bot.sent[-1][0])
        self.send("Mehr Teilzeitstellen bitte")
        self.assertEqual(self.given, ["Mehr Teilzeitstellen bitte"])
        self.assertEqual(self.store.get_state("feedback"), "")

    def test_another_command_drops_the_waiting_feedback(self):
        self.send("/feedback")
        self.send("/help")
        self.send("hallo")
        self.assertEqual(self.given, [])
        self.assertIn("keinen Link", self.bot.sent[-1][0])

    def test_errors_are_told_to_the_person(self):
        from job_radar.feedback import NotConfigured
        self.error = NotConfigured("no token")
        self.send("/feedback text")
        self.assertIn("nicht eingerichtet", self.bot.sent[-1][0])
        self.error = OSError("down")
        self.send("/feedback text")
        self.assertIn("nicht geklappt", self.bot.sent[-1][0])

    def test_feedback_is_in_the_menu_and_help(self):
        self.assertIn("feedback", [name for name, _ in app.COMMANDS])
        self.assertIn("/feedback", app.HELP)


class SearchNowTest(Base):
    def setUp(self):
        super().setUp()
        self.polls, self.result = [], {"notified": 2}

    def poll(self, cfg, store, bot, trigger=None):
        self.trigger = trigger
        self.polls.append(store.get_state("busy:/suchen"))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def send(self, text="/suchen"):
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, self.cfg, self.store,
                          self.bot, background=lambda fn: fn(), poll_fn=self.poll)

    def texts(self):
        return [t for t, _ in self.bot.sent]

    def test_searches_now_and_reports_the_cards(self):
        self.send()
        self.assertEqual(len(self.polls), 1)
        self.assertTrue(self.polls[0])  # marked busy for deploys while it runs
        self.assertEqual(self.trigger, "suchen")
        self.assertEqual(self.store.get_state("busy:/suchen"), "")
        self.assertIn("Ich suche jetzt", self.texts()[0])
        self.assertIn("2 neue", self.texts()[-1])

    def test_nothing_new_says_so(self):
        self.result = {"notified": 0}
        self.send()
        self.assertIn("nichts Neues", self.texts()[-1])

    def test_refused_while_another_poll_runs(self):
        from job_radar.store import PollLocked
        self.result = PollLocked("busy")
        self.send()
        self.assertIn("läuft gerade schon", self.texts()[-1])
        self.result = {"notified": 0}
        self.send()  # a refused search does not start the cooldown
        self.assertEqual(len(self.polls), 2)

    def test_cooldown_between_searches(self):
        self.send()
        self.send()
        self.assertEqual(len(self.polls), 1)
        self.assertRegex(self.texts()[-1], r"in \d+ Minuten")

    def test_cooldown_ends(self):
        self.send()
        self.store.set_state("search_now_at", str(time.time() - app.SEARCH_NOW_COOLDOWN_S - 1))
        self.send()
        self.assertEqual(len(self.polls), 2)

    def test_without_profile_points_to_setup(self):
        (self.dir / "profile.md").unlink()
        self.send()
        self.assertEqual(self.polls, [])
        self.assertEqual(self.texts(), [app.NO_PROFILE])

    def test_failure_is_told(self):
        self.result = RuntimeError("portal down")
        self.send()
        self.assertIn("nicht geklappt", self.texts()[-1])

    def test_a_failed_search_starts_no_cooldown(self):
        self.result = RuntimeError("portal down")
        self.send()
        self.result = {"notified": 0}
        self.send()
        self.assertEqual(len(self.polls), 2)

    def test_cooldown_does_not_sound_like_a_running_search(self):
        self.send()
        self.send()
        self.assertNotIn("läuft", self.texts()[-1])
        self.assertIn("wieder", self.texts()[-1])

    def test_in_menu_and_help(self):
        self.assertIn("suchen", [name for name, _ in app.COMMANDS])
        self.assertIn("/suchen", app.HELP)


class OneInterviewTest(Base):
    """Only one interview at a time across instances; the others are asked to come back later."""

    def setUp(self):
        super().setUp()
        from job_radar import slot
        self.slot = slot
        self.addCleanup(setattr, slot, "PATH", slot.PATH)
        slot.PATH = self.dir / "interview.slot"
        (self.dir / "profile.md").unlink()
        self.people = {}
        for name in ("anna", "ben"):
            d = self.dir / name
            d.mkdir()
            self.people[name] = (dict(self.cfg, instance=name, config_dir=str(d), profile_files=[str(d / "profile.md")]),
                                 Store(d / "db.sqlite"), FakeBot())
            self.addCleanup(self.people[name][1].close)
        self.turns = []

    def turn(self, log, previous):
        self.turns.append(len(log))
        return {"done": False, "say": "Was machst du beruflich?"}

    def send(self, who, text):
        cfg, store, bot = self.people[who]
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, cfg, store, bot,
                          interview_fn=self.turn, background=lambda fn: fn(), count_fn=lambda cfg: 0,
                          poll_fn=lambda *a: {}, find_sources_fn=lambda **kw: [], check_feed_fn=lambda url: 1)

    def last(self, who):
        return self.people[who][2].sent[-1][0]

    def test_second_person_is_asked_to_come_back_and_nothing_is_stored(self):
        self.send("anna", "/start")
        self.send("ben", "/setup")
        self.assertIn("Gerade richtet jemand anderes", self.last("ben"))
        self.assertEqual(self.people["ben"][1].get_state("interview"), "")
        self.assertEqual(len(self.turns), 1)

    def test_the_person_in_the_interview_continues(self):
        self.send("anna", "/start")
        self.send("anna", "Gärtnerin")
        self.send("anna", "/setup")  # starting over keeps the own slot
        self.assertEqual(len(self.turns), 3)

    def test_slot_is_free_after_cancel(self):
        self.send("anna", "/start")
        self.send("anna", "/abbrechen")
        self.send("ben", "/setup")
        self.assertEqual(len(self.turns), 2)

    def test_slot_is_free_after_confirm(self):
        cfg, store, bot = self.people["anna"]
        self.send("anna", "/start")
        store.set_state("interview", "proposed")
        store.set_state("interview_proposal", json.dumps(DONE))
        app.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": "i:ok", "message": {"chat": {"id": 1}}}},
                          cfg, store, bot, interview_fn=self.turn, background=lambda fn: fn(), count_fn=lambda cfg: 0,
                          poll_fn=lambda *a: {}, find_sources_fn=lambda **kw: [], check_feed_fn=lambda url: 1)
        self.send("ben", "/setup")
        self.assertEqual(len(self.turns), 2)

    def test_stale_slot_is_released_and_the_old_interview_must_start_again(self):
        self.send("anna", "/start")
        data = json.loads(self.slot.PATH.read_text())
        data["at"] -= self.slot.TIMEOUT_S + 1
        self.slot.PATH.write_text(json.dumps(data))
        self.send("ben", "/setup")
        self.assertEqual(len(self.turns), 2)
        self.send("anna", "Gärtnerin")
        self.assertIn("/setup", self.last("anna"))
        self.assertEqual(self.people["anna"][1].get_state("interview"), "")
        self.assertEqual(len(self.turns), 2)


class AdminTest(unittest.TestCase):
    def setUp(self):
        import os
        from job_radar import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d")})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.create_instance("anna")
        config.create_instance("family1")
        (config.config_dir() / "operator.json").write_text('{"admin": "anna"}')
        self.bot = FakeBot()
        self.bot.username = lambda: "radar_bot"

    def send(self, who, text):
        cfg = self.config.load_instance(who)
        store = Store(Path(cfg["db_path"]))
        self.addCleanup(store.close)
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, cfg, store, self.bot)
        return self.bot.sent[-1][0]

    def test_admin_gets_a_link_for_a_new_neutral_label(self):
        reply = self.send("anna", "/einladen")
        self.assertIn("family2", reply)
        code = re.search(r"https://t\.me/radar_bot\?start=(\S+)", reply).group(1)
        self.assertEqual(self.config.redeem_invite(code), "family2")

    def test_labels_do_not_collide(self):
        self.send("anna", "/einladen")
        self.assertIn("family3", self.send("anna", "/einladen"))

    def test_others_get_the_normal_answer_and_nothing_is_created(self):
        reply = self.send("family1", "/einladen")
        self.assertNotIn("t.me", reply)
        self.assertEqual(self.config.open_invites(), 0)
        self.assertEqual(self.config.instances(), ["anna", "family1"])

    def test_admin_lists_open_invites(self):
        self.send("anna", "/einladen")
        self.assertIn("family2", self.send("anna", "/einladungen"))
        self.assertNotIn("/einladungen", self.send("family1", "/einladungen").split("\n")[0])

    def test_admin_commands_are_not_in_the_public_help(self):
        self.assertNotIn("einladen", app.HELP)
        self.assertNotIn("einladen", [name for name, _ in app.COMMANDS])


class TimesTest(Base):
    def send(self, text):
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, self.cfg, self.store, self.bot)
        return self.bot.sent[-1]

    def press(self, data):
        app.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}},
                          self.cfg, self.store, self.bot)

    def saved(self):
        path = self.dir / "search.json"
        return json.loads(path.read_text()).get("schedule") if path.exists() else None

    def test_zeiten_shows_the_current_times_and_presets(self):
        text, buttons = self.send("/zeiten")
        self.assertIn("jeden Tag um 9, 13 und 18 Uhr", text)
        self.assertIn("z:hourly", [data for row in buttons for _, data in row])

    def test_preset_button_saves(self):
        (self.dir / "search.json").write_text('{"filter": {"include": ["gärtner"]}}')
        self.press("z:workdays")
        self.assertEqual(self.saved(), {"weekdays": [9, 13, 18], "weekend": []})
        self.assertEqual(json.loads((self.dir / "search.json").read_text())["filter"], {"include": ["gärtner"]})
        self.assertIn("am Wochenende nicht", self.bot.sent[-1][0])

    def test_own_hours_by_text(self):
        self.send("/zeiten 8 12 17")
        self.assertEqual(self.saved(), {"weekdays": [8, 12, 17], "weekend": [8, 12, 17]})

    def test_invalid_hours_are_refused_and_not_stored(self):
        text, _ = self.send("/zeiten 3 Uhr nachts")
        self.assertIn("7 und 22", text)
        self.assertIsNone(self.saved())

    def test_unknown_preset_is_harmless(self):
        self.press("z:nope")
        self.assertIsNone(self.saved())

    def test_in_menu_and_help(self):
        self.assertIn("zeiten", [name for name, _ in app.COMMANDS])
        self.assertIn("/zeiten", app.HELP)
        self.assertIn("3× am Tag", app.HELP)


class LimitsTest(Base):
    """One person (or a stolen chat) must not burn the shared Claude quota."""

    def update(self, upd, **kw):
        app.handle_update(upd, self.cfg, self.store, self.bot, background=lambda fn: fn(), **kw)

    def send(self, text, **kw):
        self.update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, **kw)
        return self.bot.sent[-1][0]

    def test_reports_per_day_are_limited(self):
        reports = []
        report = lambda posting, **kw: reports.append(1) or "# R\n## TL;DR\n- ok"
        for i in range(app.LIMITS["report"][0]):
            self.send(f"https://x.example/job/{i}", report_fn=report)
        self.assertIn("heute schon", self.send("https://x.example/job/last", report_fn=report))
        self.assertEqual(len(reports), app.LIMITS["report"][0])

    def test_report_button_counts_too(self):
        [posting] = self.store.add_new([p(1)])
        self.store.set_state("rate:report", json.dumps([time.time()] * app.LIMITS["report"][0]))
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": f"r:{posting.id}",
                                                        "message": {"chat": {"id": 1}}}},
                    report_fn=lambda posting, **kw: self.fail("no report beyond the limit"))
        self.assertIn("heute schon", self.bot.answers[-1] + self.bot.sent[-1][0])

    def test_old_calls_do_not_count(self):
        self.store.set_state("rate:report", json.dumps([time.time() - 90000] * 50))
        reports = []
        self.send("https://x.example/job/1", report_fn=lambda posting, **kw: reports.append(1) or "# R")
        self.assertEqual(reports, [1])

    def test_source_searches_per_day_are_limited(self):
        self.store.set_state("rate:quellen", json.dumps([time.time()] * app.LIMITS["quellen"][0]))
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": "q:find", "message": {"chat": {"id": 1}}}},
                    find_sources_fn=lambda **kw: self.fail("no search beyond the limit"))
        self.assertIn("heute schon", self.bot.sent[-1][0])

    def test_interview_turns_per_day_are_limited(self):
        (self.dir / "profile.md").unlink()
        self.store.set_state("rate:interview", json.dumps([time.time()] * app.LIMITS["interview"][0]))
        self.send("/setup", interview_fn=lambda log, previous: self.fail("no turn beyond the limit"))
        self.assertIn("heute schon", self.bot.sent[-1][0])

    def test_overlong_interview_answer_is_refused(self):
        (self.dir / "profile.md").unlink()
        turns = []
        turn = lambda log, previous: turns.append(log) or {"done": False, "say": "Was machst du?"}
        self.send("/start", interview_fn=turn)
        reply = self.send("x" * (app.MAX_TEXT + 1), interview_fn=turn)
        self.assertIn("kürzer", reply)
        self.assertEqual(len(turns), 1)


class EmployersTest(Base):
    """#31: employers that welcome unsolicited applications come with the normal search, at most weekly."""

    def setUp(self):
        super().setUp()
        self.cfg["sources"]["initiativ"] = {"enabled": True, "every_days": 7}
        (self.dir / "search.json").write_text(json.dumps({
            "sources": {"ba": {"keywords": ["Gärtner"], "areas": [{"where": "Kassel", "radius_km": 30}]}},
            "filter": {"include": ["gärtner"]}}))
        self.found = [{"name": "Gärtnerei Grün", "url": "https://gruen/karriere", "location": "Kassel",
                       "about": "Staudengärtnerei", "why": "Nimmt Initiativbewerbungen an"},
                      {"name": "Kaputt", "url": "https://broken/karriere", "location": "", "about": "", "why": "x"},
                      {"name": "Stumm", "url": "https://silent/karriere", "location": "", "about": "", "why": "x"}]
        self.finds = []
        self.triaged = []

    def fake_find(self, field, known, model, on_usage=None):
        self.finds.append((field, known))
        if isinstance(self.found, Exception):
            raise self.found
        on_usage({"model": model, "cost_usd": 0.15, "input_tokens": 1, "output_tokens": 1, "duration_ms": 1})
        return self.found

    @staticmethod
    def check(url):
        if "broken" in url:
            raise OSError("404")
        return "silent" not in url

    def triage(self, batch, **kw):
        self.triaged += batch
        return {x.id: (8, "Passt zu Gärtnerin") for x in batch}

    def poll(self, **kw):
        return app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [], triage_fn=self.triage,
                        employers=lambda cfg, store, **tag: app.due_employers(cfg, store, self.fake_find, self.check, **tag),
                        **kw)

    def cards(self):
        return [(text, buttons) for text, buttons in self.bot.sent if buttons and buttons[0][0][1].startswith("r:")]

    def test_poll_offers_employers_whose_page_loads_and_mentions_unsolicited_applications(self):
        self.poll()
        [(text, buttons)] = self.cards()
        self.assertIn("Gärtnerei Grün", text)
        self.assertIn("Initiativbewerbung", text)
        self.assertIn("Passt zu Gärtnerin", text)
        self.assertIn("https://gruen/karriere", text)
        pid = buttons[0][0][1].split(":")[1]
        self.assertEqual([data for _, data in buttons[0]], [f"r:{pid}", f"v:{pid}"])
        stored = self.store.get(int(pid))
        self.assertEqual((stored.source, stored.company), ("initiativ", "Gärtnerei Grün"))
        self.assertIn("Initiativbewerbung", stored.text)

    def test_employers_skip_the_keyword_filter_but_not_the_rating(self):
        self.poll()  # "gärtner" is not in "Initiativbewerbung", yet the employer is rated
        self.assertEqual([x.company for x in self.triaged], ["Gärtnerei Grün"])

    def test_gets_search_terms_and_region_but_never_the_profile(self):
        self.poll()
        field, known = self.finds[0]
        self.assertIn("Gärtner", field)
        self.assertIn("Kassel", field)
        self.assertNotIn("Kotlin backend developer", field)
        self.assertEqual(known, [])

    def test_the_search_counts_as_part_of_the_poll(self):  # #17
        self.poll(trigger="suchen")
        [(kind, poll_id, trigger)] = self.store.db.execute(
            "SELECT kind, poll_id, trigger FROM usage WHERE kind = 'employers'").fetchall()
        self.assertEqual(trigger, "suchen")
        self.assertEqual(poll_id, self.store.db.execute("SELECT max(id) FROM polls").fetchone()[0])

    def test_searches_at_most_every_few_days(self):
        self.poll()
        self.poll()
        self.assertEqual(len(self.finds), 1)
        self.store.set_state("initiativ_at", str(time.time() - 8 * 86400))
        self.poll()
        self.assertEqual(len(self.finds), 2)

    def test_employers_offered_before_are_not_offered_again(self):
        self.poll()
        self.store.set_state("initiativ_at", "")
        self.poll()
        self.assertEqual(self.finds[1][1], ["Gärtnerei Grün"])
        self.assertEqual(len(self.cards()), 1)

    def test_failed_search_tries_again_a_day_later(self):
        self.found = RuntimeError("claude down")
        self.poll()
        self.poll()
        self.assertEqual(len(self.finds), 1)
        self.store.set_state("initiativ_at", str(float(self.store.get_state("initiativ_at")) - 86400))
        self.found = []
        self.poll()
        self.assertEqual(len(self.finds), 2)

    def test_disabled_or_dry_run_does_not_search(self):
        self.poll(dry_run=True)
        self.cfg["sources"]["initiativ"]["enabled"] = False
        self.poll()
        self.assertEqual(self.finds, [])

    def test_report_and_bookmark_work_on_an_employer(self):
        self.poll()
        pid = self.cards()[0][1][0][0][1].split(":")[1]
        for data in (f"r:{pid}", f"v:{pid}"):
            app.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": data,
                                                                  "message": {"chat": {"id": 1}}}},
                              self.cfg, self.store, self.bot, background=lambda fn: fn(),
                              report_fn=lambda posting, **kw: "# R\n## TL;DR\n- passt")
        self.assertEqual(len(self.bot.docs), 1)
        self.assertEqual([p.company for p, _ in self.store.marked("saved")], ["Gärtnerei Grün"])

    def test_quellen_says_employers_are_searched_too(self):
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/quellen"}},
                          self.cfg, self.store, self.bot)
        self.assertIn("Initiativbewerbungen", self.bot.sent[-1][0])

    def quellen(self):
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/quellen"}},
                          self.cfg, self.store, self.bot)
        return self.bot.sent[-1]

    def press(self, data):
        app.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}},
                          self.cfg, self.store, self.bot)

    def test_quellen_switches_employers_off_and_on(self):  # #32
        _, buttons = self.quellen()
        self.assertIn("q:initiativ:off", [d for row in buttons for _, d in row])
        self.press("q:initiativ:off")
        saved = json.loads((self.dir / "search.json").read_text())
        self.assertIs(saved["sources"]["initiativ"]["enabled"], False)
        self.assertEqual(saved["filter"], {"include": ["gärtner"]})  # the rest stays
        self.assertIn("keine Initiativbewerbungen", self.bot.sent[-1][0])
        text, buttons = self.quellen()  # the listener's cfg is older than the file
        self.assertIn("q:initiativ:on", [d for row in buttons for _, d in row])
        self.assertNotIn("suche ich außerdem nach Arbeitgebern", text)
        self.press("q:initiativ:on")
        self.assertIs(json.loads((self.dir / "search.json").read_text())["sources"]["initiativ"]["enabled"], True)

    def test_switched_off_drops_employers_still_waiting(self):
        self.cfg["notify"] = {"max_per_run": 0}
        self.poll()  # found and rated, but no room for a card
        self.assertEqual([x.source for x in self.store.by_status("queued")], ["initiativ"])
        self.cfg["sources"]["initiativ"]["enabled"] = False
        self.cfg["notify"] = {"max_per_run": 5}
        self.poll()
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.store.by_status("queued"), [])

    def test_switching_on_again_brings_back_waiting_employers(self):
        self.cfg["notify"] = {"max_per_run": 0}
        self.poll()
        self.cfg["sources"]["initiativ"]["enabled"] = False
        self.poll()
        self.press("q:initiativ:off")
        self.press("q:initiativ:on")
        self.assertEqual([x.source for x in self.store.by_status("pending")], ["initiativ"])

    def test_switch_updates_the_quellen_message(self):
        app.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": "q:initiativ:off",
                                                              "message": {"chat": {"id": 1}, "message_id": 9}}},
                          self.cfg, self.store, self.bot)
        mid, text, buttons = self.bot.edited[-1]
        self.assertEqual(mid, 9)
        self.assertIn("q:initiativ:on", [d for row in buttons for _, d in row])

    def test_operator_setting_wins_and_the_switch_says_so(self):
        (self.dir / "config.json").write_text(json.dumps({"sources": {"initiativ": {"enabled": False}}}))
        (self.dir / "search.json").write_text(json.dumps({"sources": {"initiativ": {"enabled": True}}}))
        self.cfg["config_dir"] = str(self.dir)
        text, buttons = self.quellen()
        self.assertNotIn("suche ich außerdem nach Arbeitgebern", text)
        self.press("q:initiativ:on")
        self.assertIn("Betreiber", self.bot.sent[-1][0])

    def test_no_extra_command(self):
        self.assertNotIn("/initiativ", app.HELP)
        self.assertNotIn("initiativ", [name for name, _ in app.COMMANDS])


class MerklisteTest(Base):
    """Bookmark from the card, apply and remove from /merkliste, follow up after 14 days (#26)."""

    def setUp(self):
        super().setUp()
        self.ids = [x.id for x in self.store.add_new([p(1), p(2, title="Kotlin Lead")])]

    def update(self, upd):
        app.handle_update(upd, self.cfg, self.store, self.bot, background=lambda fn: fn())

    def send(self, text):
        self.update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}})
        return self.bot.sent[-1]

    def press(self, data):
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}})

    def test_empty_list_explains_vormerken(self):
        text, _ = self.send("/merkliste")
        self.assertIn("⭐ Vormerken", text)

    def test_bookmark_from_the_card_shows_up_with_link_and_buttons(self):
        a, b = self.ids
        self.press(f"v:{a}")
        self.assertIn("/merkliste", self.bot.answers[-1])
        self.press(f"v:{b}")
        self.send("/merkliste")
        entries = [(text, buttons) for text, buttons in self.bot.sent if buttons]
        self.assertEqual(len(entries), 2)  # one message per posting (#33)
        (newest, newest_buttons), (oldest, oldest_buttons) = entries
        self.assertIn("Kotlin Lead", newest)
        self.assertIn('href="https://x/2"', newest)
        self.assertNotIn("Senior Kotlin Engineer", newest)
        self.assertIn("Senior Kotlin Engineer", oldest)
        self.assertEqual([d for row in newest_buttons for _, d in row], [f"e:a:{b}", f"e:x:{b}"])
        self.assertEqual([d for row in oldest_buttons for _, d in row], [f"e:a:{a}", f"e:x:{a}"])

    def test_entry_of_a_sent_link_without_date_says_so(self):  # #34
        [c] = self.store.add_new([Posting(source="manual", external_id="u", url="https://jobs.example.com/u", title="Dev")])
        self.store.mark(c.id, "saved")
        self.send("/merkliste")
        [text] = [text for text, buttons in self.bot.sent if buttons]
        self.assertIn("Onlinedatum unbekannt", text)
        self.assertIn("heute von dir geschickt", text)

    def test_entry_says_how_long_the_posting_is_online(self):
        [c] = self.store.add_new([p(3, title="Kotlin Dev", posted_at=(date.today() - timedelta(days=12)).isoformat())])
        self.store.mark(c.id, "saved")
        self.store.mark(self.ids[0], "saved")
        self.send("/merkliste")
        texts = [text for text, buttons in self.bot.sent if buttons]
        self.assertIn("online seit 12 Tagen", texts[1])
        self.assertIn("Onlinedatum unbekannt · heute gefunden", texts[0])  # no date from the source

    def entry(self, pid):
        self.update({"update_id": 1, "callback_query": {"id": "c", "data": f"e:a:{pid}" if pid > 0 else f"e:x:{-pid}",
                                                        "message": {"chat": {"id": 1}, "message_id": 55}}})

    def test_applied_on_an_entry_marks_it_and_updates_that_message(self):
        a = self.ids[0]
        self.store.mark(a, "saved")
        self.entry(a)
        self.assertEqual([x.id for x, _ in self.store.marked("applied")], [a])
        mid, text, buttons = self.bot.edited[-1]
        self.assertEqual(mid, 55)
        self.assertIn("Senior Kotlin Engineer", text)
        self.assertIn("beworben", text)
        self.assertIsNone(buttons)

    def test_applied_entry_keeps_when_it_was_found(self):
        a = self.ids[0]
        self.store.mark(a, "saved")
        self.entry(a)
        self.assertIn("heute gefunden", self.bot.edited[-1][1])

    def test_old_entry_cannot_undo_an_application(self):
        a = self.ids[0]
        self.store.mark(a, "applied", remind_in_days=14)  # applied since, e.g. from a newer list
        self.entry(-a)
        self.assertEqual([x.id for x, _ in self.store.marked("applied")], [a])
        self.assertIn("beworben", self.bot.answers[-1])

    def test_header_says_when_not_all_bookmarks_are_shown(self):
        for x in self.store.add_new([p(i) for i in range(10, 33)]):
            self.store.mark(x.id, "saved")
        self.send("/merkliste")
        self.assertIn("20 von 23", [text for text, buttons in self.bot.sent if not buttons][-1])

    def test_removed_on_an_entry_marks_it_and_updates_that_message(self):
        a = self.ids[0]
        self.store.mark(a, "saved")
        self.entry(-a)
        self.assertEqual([x.id for x in self.store.disliked(5)], [a])
        mid, text, buttons = self.bot.edited[-1]
        self.assertIn("entfernt", text)
        self.assertIsNone(buttons)

    def test_applied_and_removed_from_the_list(self):
        a, b = self.ids
        self.press(f"v:{a}")
        self.press(f"v:{b}")
        self.press(f"m:a:{a}")  # buttons of the old list in one message still work
        self.press(f"m:x:{b}")
        self.send("/merkliste")
        text = "\n".join(text for text, _ in self.bot.sent[-2:])
        self.assertIn("Beworben", text)
        self.assertIn("Senior Kotlin Engineer", text)
        self.assertNotIn("Kotlin Lead", text)
        self.assertEqual([x.id for x in self.store.disliked(5)], [b])
        self.assertEqual(len(self.store.due_reminders(days_ahead=15)), 1)
        self.assertFalse(hasattr(self.bot, "edited"))  # the old list keeps its other entries

    def test_old_cards_buttons_still_work(self):
        a, b = self.ids
        self.press(f"s:{a}")
        self.press(f"a:{b}")
        self.assertEqual([x.id for x in self.store.disliked(5)], [a])
        self.assertEqual([x.id for x, _ in self.store.marked("applied")], [b])

    def test_poll_asks_about_due_applications_once(self):
        a = self.ids[0]
        self.store.mark(a, "applied", remind_in_days=0)
        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [], triage_fn=lambda batch, **kw: {})
        text, buttons = self.bot.sent[-1]
        self.assertIn("Hast du", text)
        self.assertIn("Senior Kotlin Engineer", text)
        self.assertEqual([d for row in buttons for _, d in row], [f"o:gespraech:{a}", f"o:absage:{a}", f"o:wait:{a}"])
        sent = len(self.bot.sent)
        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [], triage_fn=lambda batch, **kw: {})
        self.assertEqual(len(self.bot.sent), sent)  # asked again only after 14 days

    def test_dry_run_asks_nothing(self):
        self.store.mark(self.ids[0], "applied", remind_in_days=0)
        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [], triage_fn=lambda batch, **kw: {}, dry_run=True)
        self.assertEqual(self.bot.sent, [])

    def test_outcome_buttons(self):
        a = self.ids[0]
        self.store.mark(a, "applied", remind_in_days=0)
        self.press(f"o:wait:{a}")
        self.assertEqual(self.store.due_reminders(days_ahead=13), [])
        self.press(f"o:gespraech:{a}")
        self.assertEqual(self.store.marked("applied")[0][1]["outcome"], "gespraech")
        self.assertIn("Gespräch", self.send("/merkliste")[0])

    def test_triage_learns_from_marks(self):
        a, b = self.ids
        self.store.mark(a, "saved")
        self.store.mark(b, "removed")
        seen = {}
        triage = lambda batch, **kw: seen.update(kw) or {}
        app.poll(self.cfg, self.store, self.bot, fetch=lambda cfg: [p(3)], triage_fn=triage)
        self.assertEqual([x.id for x in seen["likes"]], [a])
        self.assertEqual([x.id for x in seen["dislikes"]], [b])

    def test_in_menu_and_help(self):
        self.assertIn("merkliste", [name for name, _ in app.COMMANDS])
        self.assertIn("/merkliste", app.HELP)
        self.assertIn("⭐ Vormerken", app.HELP)
        self.assertNotIn("Überspringen", app.HELP)


class PostingAgeTest(Base):
    def send(self, text):
        app.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}, self.cfg, self.store, self.bot)
        return self.bot.sent[-1]

    def press(self, data):
        app.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 1}}}},
                          self.cfg, self.store, self.bot)

    def test_zeitraum_shows_the_current_age_and_choices(self):
        text, buttons = self.send("/zeitraum")
        self.assertIn("28 Tage", text)
        self.assertEqual([data for row in buttons for _, data in row], ["t:7", "t:28", "t:60", "t:100"])

    def test_choice_is_saved_next_to_the_other_settings(self):
        (self.dir / "search.json").write_text('{"filter": {"include": ["gärtner"]}}')
        self.press("t:60")
        saved = json.loads((self.dir / "search.json").read_text())
        self.assertEqual(saved, {"filter": {"include": ["gärtner"]}, "sources": {"ba": {"published_since_days": 60}}})
        self.assertIn("/suchen", self.bot.sent[-1][0])
        self.assertIn("60 Tage", self.send("/zeitraum")[0])

    def test_unknown_choice_is_refused(self):
        self.press("t:365")
        self.assertFalse((self.dir / "search.json").exists())
        self.assertIn("Veraltet", self.bot.answers[-1])

    def test_zeitraum_is_in_the_menu_and_the_help(self):
        self.assertIn("zeitraum", [name for name, _ in app.COMMANDS])
        self.assertIn("/zeitraum", app.HELP)
