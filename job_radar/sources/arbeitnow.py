"""Arbeitnow free job board API (DE/EU, many English and remote roles)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..model import Posting
from ..text import html_to_text

URL = "https://www.arbeitnow.com/api/job-board-api"


def _contract(job_types: list[str]) -> str:
    joined = " ".join(job_types).lower()
    if "freelance" in joined or "contract" in joined:
        return "freelance"
    if "permanent" in joined:
        return "permanent"
    return ""


def parse(payload: dict) -> list[Posting]:
    out = []
    for job in payload.get("data", []):
        out.append(Posting(
            source="arbeitnow",
            external_id=job["slug"],
            url=job["url"],
            title=job["title"],
            company=job.get("company_name", ""),
            location=job.get("location", ""),
            remote=bool(job.get("remote")),
            contract_type=_contract(job.get("job_types", [])),
            text=html_to_text(job.get("description", "")),
            tags=list(job.get("tags", [])),
            posted_at=datetime.fromtimestamp(job["created_at"], timezone.utc).isoformat(timespec="minutes")
            if job.get("created_at") else "",
        ))
    return out


def fetch(cfg: dict, http_get) -> list[Posting]:
    postings, url = [], URL
    for i in range(cfg.get("max_pages", 1)):
        if i:
            time.sleep(cfg.get("page_delay_s", 2))  # the API rate-limits fast paging
        page = http_get(url)
        postings += parse(page)
        url = (page.get("links") or {}).get("next")
        if not url:
            break
    return postings
