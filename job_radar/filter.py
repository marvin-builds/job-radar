"""Cheap keyword/location/contract filter that runs before any Claude call."""
from __future__ import annotations

import re

from .model import Posting


def _has_word(words: list[str], text: str) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text, re.I) for w in words)


def matches(p: Posting, cfg: dict) -> bool:
    if _has_word(cfg.get("exclude_title", []), p.title):
        return False
    locations = cfg.get("locations", [])
    local = "nearby" in p.tags or any(loc.lower() in p.location.lower() for loc in locations)
    # Local postings are rare and often have generic titles; let triage judge them.
    skip_include = cfg.get("nearby_skips_include") and local
    haystack = " ".join([p.title, " ".join(p.tags), p.text])
    if cfg.get("include") and not skip_include and not _has_word(cfg["include"], haystack):
        return False
    if p.contract_type not in cfg.get("contract_types", [p.contract_type]):
        return False
    if locations and not (p.remote or local):
        return False
    return True
