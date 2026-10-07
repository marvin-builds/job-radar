"""Cheap keyword/location/contract filter that runs before any Claude call."""
from __future__ import annotations

import re

from .model import Posting


# BA kinds of position that had no contract type before.
KINDS = ("internship", "apprenticeship")


def _contract_ok(p: Posting, allowed: list[str] | None) -> bool:
    if allowed is None:
        return True
    kind = p.contract_type
    if kind in KINDS and not any(k in allowed for k in KINDS):
        kind = ""  # a list from before the kinds were told apart: they pass as unknown, as they did
    return kind in allowed


def _has_word(words: list[str], text: str) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text, re.I) for w in words)


def matches(p: Posting, cfg: dict) -> bool:
    if _has_word(cfg.get("exclude_title", []), p.title):
        return False
    locations = cfg.get("locations", [])
    # Feeds have no location field; their boards write it into the title ("Title (City) - Employer").
    where = (p.location or f"{p.title} {p.text}").lower()
    local = "nearby" in p.tags or any(loc.lower() in where for loc in locations)
    # Local postings are rare and often have generic titles; let triage judge them.
    skip_include = cfg.get("nearby_skips_include") and local
    haystack = " ".join([p.title, " ".join(p.tags), p.text])
    if cfg.get("include") and not skip_include and not _has_word(cfg["include"], haystack):
        return False
    if not _contract_ok(p, cfg.get("contract_types")):
        return False
    if locations and not (p.remote or local):
        return False
    return True
