"""schema.org JobPosting data that many job pages embed as JSON-LD, read without a model."""
from __future__ import annotations

import json
import re
from datetime import date

from .text import days_since, html_to_text

_SCRIPT = re.compile(r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _nodes(data):
    """Every object in a JSON-LD block, also inside lists and @graph."""
    if isinstance(data, list):
        for item in data:
            yield from _nodes(item)
    elif isinstance(data, dict):
        yield data
        yield from _nodes(data.get("@graph"))


def _is_posting(node: dict) -> bool:
    kind = node.get("@type")
    return "JobPosting" in (kind if isinstance(kind, list) else [kind])


def job_posting(html: str) -> dict | None:
    """The page's JobPosting object, or None. A page with several different ones (a careers page,
    "similar jobs") does not say which is the linked job, so it gives None as well."""
    found = {}
    for block in _SCRIPT.findall(html or ""):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for node in _nodes(data):
            if _is_posting(node):
                found.setdefault(json.dumps(node, sort_keys=True, default=str), node)
    return next(iter(found.values())) if len(found) == 1 else None


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _str(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""


def _http(value) -> str:
    url = _str(_first(value))
    return url if re.match(r"https?://", url) else ""


def _salary(value) -> str:
    value = _first(value)
    if not isinstance(value, dict):
        return _str(value)
    amount, currency = value.get("value"), _str(value.get("currency"))
    unit = ""
    if isinstance(amount, dict):
        unit = _str(amount.get("unitText"))
        low, high = _str(amount.get("minValue")), _str(amount.get("maxValue"))
        amount = f"{low}-{high}" if low and high and low != high else low or high or _str(amount.get("value"))
    else:
        amount = _str(amount)
    if not amount:
        return ""
    return f"{amount} {currency}".strip() + (f"/{unit}" if unit else "")


def _locality(place) -> str:
    if not isinstance(place, dict):
        return _str(place)
    address = place.get("address")
    if isinstance(address, dict):
        return _str(address.get("addressLocality"))
    return _str(address)


def fields(job: dict, today: date | None = None) -> dict:
    """What a Posting can take from a JobPosting object; empty where the page leaves it out."""
    org = _first(job.get("hiringOrganization"))
    company = _str(org.get("name")) if isinstance(org, dict) else _str(org)
    website = (_http(org.get("sameAs")) or _http(org.get("url"))) if isinstance(org, dict) else ""
    places = job.get("jobLocation")
    places = places if isinstance(places, list) else [places] if places else []
    locations = list(dict.fromkeys(filter(None, (_locality(p) for p in places))))
    published = _str(job.get("datePosted"))[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published) or days_since(published, today or date.today()) is None:
        published = ""
    kinds = job.get("employmentType")
    kinds = ", ".join(filter(None, map(_str, kinds if isinstance(kinds, list) else [kinds])))
    description = job.get("description")
    text = html_to_text(description if isinstance(description, str) else "")
    if kinds:
        text = f"employment type: {kinds}" + (f"\n\n{text}" if text else "")
    return {"title": _str(job.get("title")), "company": company, "location": ", ".join(locations),
            "posted_at": published, "website": website, "salary": _salary(job.get("baseSalary")), "text": text,
            "remote": "TELECOMMUTE" in _str(_first(job.get("jobLocationType"))).upper()}
