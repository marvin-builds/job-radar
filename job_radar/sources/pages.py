"""Job boards without a feed: code reads the board's list page, a cheap model only where code cannot.

A page that lists its postings as JSON-LD (JobPosting, or an ItemList of them) is read without
a model. Otherwise the page is reduced to its text with numbered links ("[3] Botaniker
(m/w/d)"); the model names postings by link number only, so a page cannot slip in a URL of its
own. The model is asked again only when the page's links changed since the last poll (cache next
to the instance's database).
"""
from __future__ import annotations

import json
import logging
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import jsonld
from ..http import get_text as _get_text
from ..model import Posting

log = logging.getLogger("job-radar")
MAX_LINKS = 300
MAX_CHARS = 15000  # of reduced page text per model call
MAX_POSTINGS = 50
MAX_TEXT = 6000  # of a JSON-LD description; triage reads less
FULL_EVERY_S = 7 * 86400  # a whole page read now and then, in case a posting's address has a new shape
MIN_JSONLD = 2  # one posting in a list page's JSON-LD is more often a featured job than the list
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
    """The lines of the page around the given links: each link's line, the line before it and up
    to two after it, as long as those carry no other link (a card's employer and place)."""
    lines = text.splitlines()
    marks = [{int(n) for n in re.findall(r"\[(\d+)\]", line)} for line in lines]
    keep = set()
    for i, found in enumerate(marks):
        if found & numbers:
            keep.add(i)
            if i and not marks[i - 1]:
                keep.add(i - 1)
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


def from_jsonld(html: str, base: str, name: str, links: dict[int, str]) -> list[Posting] | None:
    """The page's postings from its JSON-LD, or None when it lists too few to stand for the page:
    the page links more addresses of the same shape than the JSON-LD names."""
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
    named, shapes = {p.url for p in postings}, learn(p.url for p in postings)
    unnamed = {u for u in links.values() if u not in named and pattern(u) in shapes}
    return postings[:MAX_LINKS] if len(unnamed) <= len(postings) // 4 else None


def _load(path: Path | None) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    except ValueError:
        return {}


def _board(board: dict, html: str, entry: dict, extract, now: float) -> tuple[list[Posting], dict, bool]:
    """The board's postings, its new cache entry and whether the model was asked.

    The model reads the whole page once and names its postings; their address shapes are kept.
    After that only links of those shapes count: when new ones show up the model sees just their
    lines, when some are gone their postings are dropped. Once a week, or when no link of a known
    shape is left (a new site), it reads the whole page again."""
    url, name = board["url"], board["name"]
    text, links = _reduce(html, url)
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
    shapes = learn(p.url for p in postings)
    watched = {u for u in links.values() if not shapes or pattern(u) in shapes}
    return postings, {"postings": [_plain(p) for p in postings], "shapes": shapes, "seen": sorted(watched),
                      "full_at": now}, True


def fetch(cfg: dict, http_get, get_text=_get_text, extract=None, cache_path: Path | None = None,
          stats: dict | None = None, now: float | None = None) -> list[Posting]:
    """`stats` counts the boards read without the model (page_hits) and those it read (page_misses)."""
    if extract is None:  # e.g. the match count right after setup: no model calls there
        return []
    stats = {} if stats is None else stats
    now = time.time() if now is None else now
    cache, postings = _load(cache_path), []
    for board in cfg.get("pages", []):
        try:
            html = get_text(board["url"])
            links = reduce(html, board["url"])[1]
            found = from_jsonld(html, board["url"], board["name"], links)
            asked = False
            if found is None:
                found, cache[board["url"]], asked = _board(board, html, cache.get(board["url"], {}), extract, now)
            stats["page_misses" if asked else "page_hits"] = stats.get("page_misses" if asked else "page_hits", 0) + 1
            postings += found
        except Exception as e:  # one broken board must not hide the others
            log.warning("page %s failed: %s", board.get("name"), e)
    if cache_path:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return postings


def _plain(p: Posting) -> dict:
    return {"source": p.source, "external_id": p.external_id, "url": p.url, "title": p.title,
            "company": p.company, "location": p.location, "tags": p.tags}
