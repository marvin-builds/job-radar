"""Job board feeds (RSS 2.0, Atom) that a person added, e.g. boards of their own field.

Feed entries rarely carry a location field; the filter looks for location words in the
title and text instead (boards often write "Title (City) - Employer").
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from ..http import get_text as _get_text
from ..model import Posting
from ..text import html_to_text

log = logging.getLogger("job-radar")
ATOM = "{http://www.w3.org/2005/Atom}"


def _text(el, tag: str) -> str:
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _atom_link(entry) -> str:
    for link in entry.findall(f"{ATOM}link"):
        if link.get("rel", "alternate") == "alternate":
            return link.get("href", "")
    return ""


def parse(xml: str, name: str) -> list[Posting]:
    """Postings of one feed; raises ValueError for anything that is not an RSS or Atom feed."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from e
    if root.tag == f"{ATOM}feed":
        items = [(_text(e, f"{ATOM}title"), _atom_link(e), _text(e, f"{ATOM}id"),
                  _text(e, f"{ATOM}summary") or _text(e, f"{ATOM}content"),
                  _text(e, f"{ATOM}updated") or _text(e, f"{ATOM}published"))
                 for e in root.findall(f"{ATOM}entry")]
    elif root.tag == "rss":
        items = [(_text(i, "title"), _text(i, "link"), _text(i, "guid"), _text(i, "description"),
                  _text(i, "pubDate")) for i in root.iter("item")]
    else:
        raise ValueError(f"not an RSS or Atom feed (root <{root.tag}>)")
    return [Posting(source="feed", external_id=uid or url, url=url, title=title, text=html_to_text(summary),
                    tags=[name], posted_at=posted)
            for title, url, uid, summary, posted in items if url and title]


def fetch(cfg: dict, http_get, get_text=_get_text) -> list[Posting]:
    postings = []
    for feed in cfg.get("feeds", []):
        try:
            postings += parse(get_text(feed["url"]), feed["name"])
        except Exception as e:  # one broken board must not hide the others
            log.warning("feed %s failed: %s", feed.get("name"), e)
    return postings
