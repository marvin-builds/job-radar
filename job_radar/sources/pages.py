"""Job boards without a feed: code reads the board's list page, a cheap model only where code cannot.

A page that lists its postings as JSON-LD (JobPosting, or an ItemList of them) is read without
a model, and one that names a feed of its postings is read from that feed. Otherwise the page is
reduced to its text with numbered links ("[3] Botaniker (m/w/d)"); the model names postings by
link number only, so a page cannot slip in a URL of its own. The model is asked again only for
links shaped like the board's postings that are new since the last poll (one cache for all
instances, CACHE_PATH).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import jsonld
from ..http import get_text as _get_text
from ..model import Posting
from . import feeds

log = logging.getLogger("job-radar")
MAX_LINKS = 300
MAX_CHARS = 15000  # of reduced page text per model call
MAX_POSTINGS = 50
MAX_TEXT = 6000  # of a JSON-LD description; triage reads less
FULL_EVERY_S = 7 * 86400  # a whole page read now and then, in case a posting's address has a new shape
MIN_JSONLD = 2  # one posting in a list page's JSON-LD is more often a featured job than the list
MAX_SHAPES = 3  # more address shapes among a board's postings are no pattern
MAX_FEEDS = 3  # feeds named by one page that are tried
KEEP_S = 30 * 86400  # a board read by no instance for this long leaves the cache
CACHE_PATH: Path | None = None  # shared by all instances; set by the CLI, else next to the database
_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br", "section", "article", "ul", "ol",
          "table", "dt", "dd", "nav", "footer", "header"}
# The site's own navigation: its links rotate (news, tracking) and are never postings. A header or
# footer inside these belongs to a job card ("<article><header><a>Title</a>") and is kept.
_CHROME = {"nav", "header", "footer"}
_CONTENT = {"main", "article", "section", "li"}


class _Reducer(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.ids = base, [], {}, {}
        self.skip, self.href, self.label = 0, None, []
        self.content, self.chrome = 0, []  # open content tags; per open chrome tag, whether it is skipped

    def handle_starttag(self, tag, attrs):
        if tag in _CONTENT:
            self.content += 1
        if tag in _CHROME:
            skipped = tag == "nav" or not self.content
            self.chrome.append(skipped)
            self.skip += skipped
            if skipped:
                return
        if tag in _SKIP:
            self.skip += 1
        elif tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            url = urljoin(self.base, href.strip())
            self.href, self.label = (url, []) if re.match(r"https?://", url) else (None, [])
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _CONTENT:
            self.content = max(0, self.content - 1)
        if tag in _CHROME and self.chrome:
            if self.chrome.pop():
                self.skip = max(0, self.skip - 1)
                return
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            label = " ".join("".join(self.label).split())
            if label and len(self.links) < MAX_LINKS:
                n = self.ids.setdefault(self.href, len(self.ids) + 1)
                self.links[n] = self.href
                self.parts.append(f"[{n}] {label}")
            self.href = None
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.href:
            self.label.append(data)
        else:
            self.parts.append(data)


def _reduce(html: str, base: str) -> tuple[str, dict[int, str]]:
    parser = _Reducer(base)
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line), parser.links


def reduce(html: str, base: str) -> tuple[str, dict[int, str]]:
    """The page as plain text with numbered links, and the numbers' absolute URLs."""
    text, links = _reduce(html, base)
    return text[:MAX_CHARS], links


def excerpt(text: str, numbers: set[int]) -> str:
    """The lines of the page around the given links: each link's line and up to two lines before
    and after it, as long as those carry no other link (a card's title above a "Details" link, its
    employer and place)."""
    lines = text.splitlines()
    marks = [{int(n) for n in re.findall(r"\[(\d+)\]", line)} for line in lines]
    keep = set()
    for i, found in enumerate(marks):
        if found & numbers:
            keep.add(i)
            for j in range(i - 1, max(i - 3, -1), -1):
                if marks[j]:
                    break
                keep.add(j)
            for j in range(i + 1, min(i + 3, len(lines))):
                if marks[j]:
                    break
                keep.add(j)
    return "\n".join(lines[i] for i in sorted(keep))[:MAX_CHARS]


def read(html: str, url: str, name: str, extract) -> list[Posting]:
    """Postings on one list page; `extract(page_text)` returns [{link, title, employer, location}]."""
    text, links = reduce(html, url)
    return _postings(extract(text), links, name)


def _postings(items, links: dict[int, str], name: str, allowed: set[int] | None = None) -> list[Posting]:
    postings = []
    for item in items[:MAX_POSTINGS]:
        link = item.get("link") if isinstance(item, dict) else None
        title = str(item.get("title") or "").strip() if isinstance(item, dict) else ""
        if not isinstance(link, int) or link not in links or not title or (allowed and link not in allowed):
            continue  # only links that are really on the page
        postings.append(Posting(source="page", external_id=links[link], url=links[link], title=title[:200],
                                company=str(item.get("employer") or "")[:120],
                                location=str(item.get("location") or "")[:120], tags=[name]))
    return postings


def pattern(url: str) -> str:
    """The shape of a posting's address: host and folder, digits as '#'. The postings of one board
    share it ("b.example/jobs/"), its navigation and news links mostly do not."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    folder = path[:path.rfind("/") + 1] or "/"
    if folder == "/" and parts.query:  # "/job?id=5": the script, not the site root
        folder = path + "?"
    return re.sub(r"\d+", "#", parts.netloc.lower() + folder)


def learn(urls) -> list[str]:
    return sorted({pattern(u) for u in urls})


def _shapes(urls) -> list[str]:
    """The shapes a board's postings share, or none when they share too few to form a pattern
    (each linking to its employer's own site): then every link counts."""
    urls = list(urls)
    shapes = learn(urls)
    return [] if len(shapes) > MAX_SHAPES or len(shapes) * 2 > len(urls) else shapes


def from_jsonld(html: str, base: str, name: str, links: dict[int, str]) -> list[Posting] | None:
    """The page's postings from its JSON-LD, or None when they may not stand for the page: fewer
    than two, half of them not linked on the page, or the page links more addresses of the same
    shape than the JSON-LD names (a few featured jobs above a longer list)."""
    postings = []
    for job in jsonld.job_postings(html):
        f = jsonld.fields(job)
        url = urljoin(base, f["url"])
        if not f["url"] or not re.match(r"https?://", url) or not f["title"] or any(p.url == url for p in postings):
            continue
        postings.append(Posting(source="page", external_id=url, url=url, title=f["title"][:200],
                                company=f["company"][:120], location=f["location"][:120], remote=f["remote"],
                                website=f["website"], salary=f["salary"], text=f["text"][:MAX_TEXT],
                                posted_at=f["posted_at"], tags=[name]))
    if len(postings) < MIN_JSONLD:
        return None
    named, shapes, linked = {p.url for p in postings}, learn(p.url for p in postings), set(links.values())
    unnamed = {u for u in linked if u not in named and pattern(u) in shapes}
    if len(named & linked) * 2 < len(named) or len(unnamed) > len(postings) // 4:
        return None
    return postings[:MAX_LINKS]


class _FeedLinks(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.found = base, []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if (tag == "link" and "alternate" in a.get("rel", "").lower().split()
                and a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml") and a.get("href")):
            url = urljoin(self.base, a["href"].strip())
            if re.match(r"https?://", url) and url not in self.found:
                self.found.append(url)


def feed_links(html: str, base: str) -> list[str]:
    """The RSS and Atom feeds a page names in its head (autodiscovery)."""
    parser = _FeedLinks(base)
    parser.feed(html)
    parser.close()
    return parser.found[:MAX_FEEDS]


def _same_address(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc.lower() + parts.path.rstrip("/") + (f"?{parts.query}" if parts.query else "")


def from_feed(xml: str, name: str) -> list[Posting]:
    """A board's feed entries as the board's postings: same source and id as when read off the page,
    so a board turning into a feed does not bring its postings back for a second rating."""
    return [replace(p, source="page", external_id=p.url, tags=[name]) for p in feeds.parse(xml, name)
            if re.match(r"https?://", p.url)][:MAX_LINKS]


def job_feed(urls: list[str], postings: list[Posting], name: str, get_text) -> tuple[str, list[Posting]] | None:
    """The first of the page's feeds that lists the page's postings, and its postings. A site's
    news feed is named in the same way; at least half of a job feed's entries are on the page."""
    addresses = {_same_address(p.url) for p in postings}
    titles = {p.title.casefold() for p in postings}
    for url in urls:
        try:
            found = from_feed(get_text(url), name)
        except Exception as e:
            log.info("feed %s of board %s cannot be read: %s", url, name, e)
            continue
        same = sum(_same_address(p.url) in addresses or p.title.casefold() in titles for p in found)
        if same and same * 2 >= len(found):
            return url, found
    return None


def _load(path: Path | None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, changed: dict, now: float) -> None:
    """Merge the boards this poll read into the cache file. Instances poll at the same time: the
    lock keeps one's entries from overwriting another's, and readers see the old file or the new
    one, never half of it."""
    if not changed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = {**_load(path), **changed}
        cache = {url: e for url, e in cache.items() if isinstance(e, dict) and now - e.get("full_at", 0) < KEEP_S}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def _board(board: dict, text: str, links: dict[int, str], entry: dict, extract,
           now: float) -> tuple[list[Posting], dict, bool]:
    """The board's postings, its new cache entry and whether the model was asked.

    The model reads the whole page once and names its postings; their address shapes are kept.
    After that only links of those shapes count: when new ones show up the model sees just their
    lines, when some are gone their postings are dropped. Once a week, or when no link of a known
    shape is left (a new site), it reads the whole page again."""
    name = board["name"]
    shapes = entry.get("shapes") or []
    watched = {u for u in links.values() if not shapes or pattern(u) in shapes}
    cached = [Posting(**{**p, "tags": [name]}) for p in entry.get("postings", [])]  # the board's name here
    due = "seen" not in entry or now - entry.get("full_at", 0) >= FULL_EVERY_S or (shapes and not watched)
    if not due and watched == set(entry["seen"]):
        return cached, entry, False
    if not due and shapes:
        new = watched - set(entry["seen"])
        on_page = set(links.values())
        postings = [p for p in cached if p.url in on_page]
        if new:
            numbers = {n for n, u in links.items() if u in new}
            known = {p.url for p in postings}
            postings += [p for p in _postings(extract(excerpt(text, numbers)), links, name, numbers)
                         if p.url not in known]
        return postings, {**entry, "postings": [_plain(p) for p in postings], "seen": sorted(watched)}, bool(new)
    postings = _postings(extract(text[:MAX_CHARS]), links, name)
    shapes = _shapes(p.url for p in postings)
    watched = {u for u in links.values() if not shapes or pattern(u) in shapes}
    return postings, {"postings": [_plain(p) for p in postings], "shapes": shapes, "seen": sorted(watched),
                      "full_at": now}, True


def _read(board: dict, cache: dict, get_text, extract, now: float) -> tuple[list[Posting], bool]:
    """One board's postings and whether the model was asked; updates the board's cache entry.

    A board whose page names a feed of its postings is read from the feed, and its page only once
    a week, to see that it still names that feed."""
    url, name = board["url"], board["name"]
    entry = cache.get(url, {})
    fresh = now - entry.get("full_at", 0) < FULL_EVERY_S
    if entry.get("feed") and fresh:
        try:
            return from_feed(get_text(entry["feed"]), name), False
        except Exception as e:  # read the page this time; the feed is tried again next poll
            log.info("feed of board %s failed, reading the page: %s", name, e)
    html = get_text(url)
    text, links = _reduce(html, url)
    found = from_jsonld(html, url, name, links)
    if found is not None:
        return found, False
    named = feed_links(html, url)
    if entry.get("feed") and not fresh and entry["feed"] in named:
        try:
            found = from_feed(get_text(entry["feed"]), name)
            cache[url] = {**entry, "full_at": now}
            return found, False
        except Exception as e:
            log.info("feed of board %s failed: %s", name, e)
    if entry.get("feed") and not fresh:
        entry = {k: v for k, v in entry.items() if k != "feed"}  # no longer named: read the page again
    found, entry, asked = _board(board, text, links, entry, extract, now)
    if named and entry.get("full_at") == now:  # the model just named the page's postings
        feed = job_feed(named, found, name, get_text)
        if feed:
            entry["feed"] = feed[0]
            found += [p for p in feed[1] if all(p.url != q.url for q in found)]
    cache[url] = entry
    return found, asked


def check(url: str, get_text, extract, cache_path: Path | None, now: float | None = None) -> tuple[list[Posting], str]:
    """A board's postings read as a poll would, and the feed that lists them ("" if none); raises if
    the page does not load. The reading goes into the cache, so the first poll asks no model."""
    now = time.time() if now is None else now
    cache = _load(cache_path)
    before = cache.get(url)
    found, _ = _read({"name": "check", "url": url}, cache, get_text, extract, now)
    if cache_path and cache.get(url) != before:
        save(cache_path, {url: cache[url]}, now)
    return found, cache.get(url, {}).get("feed", "")


def fetch(cfg: dict, http_get, get_text=_get_text, extract=None, cache_path: Path | None = None,
          stats: dict | None = None, now: float | None = None) -> list[Posting]:
    """`stats` counts the boards read without the model (page_hits) and those it read (page_misses)."""
    if extract is None:  # e.g. the match count right after setup: no model calls there
        return []
    stats = {} if stats is None else stats
    now = time.time() if now is None else now
    cache, postings, changed = _load(cache_path), [], {}
    for board in cfg.get("pages", []):
        try:
            before = cache.get(board["url"])
            found, asked = _read(board, cache, get_text, extract, now)
            if cache.get(board["url"]) != before:
                changed[board["url"]] = cache[board["url"]]
            stats["page_misses" if asked else "page_hits"] = stats.get("page_misses" if asked else "page_hits", 0) + 1
            postings += found
        except Exception as e:  # one broken board must not hide the others
            log.warning("page %s failed: %s", board.get("name"), e)
    if cache_path:
        save(cache_path, changed, now)
    return postings


def _plain(p: Posting) -> dict:
    return {"source": p.source, "external_id": p.external_id, "url": p.url, "title": p.title,
            "company": p.company, "location": p.location, "tags": p.tags}
