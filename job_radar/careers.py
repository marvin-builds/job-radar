"""Employers that welcome unsolicited applications, found in code: the careers pages of employers
the radar has seen postings of. Cheaper than the web search with a model (#31), which then runs
less often."""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from .sources import pages

PATHS = ("/karriere", "/jobs", "/stellenangebote")  # tried from the root of the employer's site
MAX_REQUESTS = 20  # per poll, one at a time
GAP_S = 1.0  # between two requests
_JOB_FEED = re.compile(r"job|karriere|stellen|career|vacanc", re.I)
# The application itself, not "Eigeninitiative" or "Initiative", which many job pages ask for
_INVITES = re.compile(r"initiativ[\s-]*bewerb|\b(?:bewirb|bewerben)\b[^.!?<]{0,40}\binitiativ\b"
                      r"|\binitiativ\b[^.!?<]{0,20}\bbewerben\b|unsolicited applications?", re.I)

_GENERIC_WORDS = {"gmbh", "mbh", "ag", "kg", "co", "ohg", "gbr", "ug", "se", "ev", "kgaa", "haftungsbeschränkt",
                  "und", "der", "die", "das", "für", "von", "the", "and", "deutschland", "germany"}


def name_words(company: str) -> list[str]:
    """The distinctive words of an employer's name, which its own pages carry."""
    return [w for w in re.findall(r"[^\W_]+", company.casefold()) if len(w) >= 3 and w not in _GENERIC_WORDS]


class OutOfRequests(Exception):
    """The poll's requests are used up; the rest waits for the next poll."""


class Fetcher:
    """`get` for at most `budget` requests, `GAP_S` apart, so a poll stays light on the Pi and polite."""

    def __init__(self, get, budget: int = MAX_REQUESTS, sleep=time.sleep):
        self.get, self.left, self.sleep, self.used = get, budget, sleep, 0

    def __call__(self, url: str) -> str:
        if self.left <= 0:
            raise OutOfRequests(url)
        if self.used:
            self.sleep(GAP_S)
        self.left -= 1
        self.used += 1
        return self.get(url)


def check(website: str, company: str, fetch: Fetcher) -> tuple[str, list[str]]:
    """The careers page of the employer's site that invites unsolicited applications ("" if none), and
    the job feeds its careers pages name. A page must carry the employer's name, so a guessed domain
    of someone else is never offered. Raises OutOfRequests when the budget ends on the way."""
    try:
        parts, words = urlsplit(website), name_words(company)
    except ValueError:  # a website triage named, e.g. with a placeholder in brackets
        return "", []
    if parts.scheme not in ("http", "https") or not parts.netloc or not words:
        return "", []
    feeds = []
    for path in PATHS:
        url = f"{parts.scheme}://{parts.netloc}{path}"
        try:
            page = fetch(url)
        except OutOfRequests:
            raise
        except Exception:  # most sites have only one of the paths
            continue
        low = page.casefold()
        if not all(w in low for w in words):
            continue
        feeds += [f for f in _feed_links(page, url) if f not in feeds]
        if _INVITES.search(low):
            return url, feeds
    return "", feeds


def _feed_links(page: str, url: str) -> list[str]:
    """The job feeds a careers page names; none if one of its links is no valid address."""
    try:
        return [f for f in pages.feed_links(page, url) if _JOB_FEED.search(urlsplit(f).path) and "comments" not in f]
    except ValueError:
        return []
