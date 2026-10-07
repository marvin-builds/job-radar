"""Bundesagentur fuer Arbeit Jobsuche API (public key, used by the official app)."""
from __future__ import annotations

import base64
from urllib.parse import urlencode

from ..model import Posting

URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
DETAILS = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/"
HOURS = "Arbeitszeit:"  # first line of the details, so a posting is not fetched twice
MAX_DESCRIPTION = 3000
PART_TIME = (("arbeitszeitTeilzeitVormittag", "vormittags"), ("arbeitszeitTeilzeitNachmittag", "nachmittags"),
             ("arbeitszeitTeilzeitAbend", "abends"), ("arbeitszeitTeilzeitFlexibel", "flexibel"))
KINDS = {"ARBEIT": "Arbeit", "AUSBILDUNG": "Ausbildung/Duales Studium", "PRAKTIKUM_TRAINEE": "Praktikum/Trainee",
         "SELBSTSTAENDIGKEIT": "Selbstständigkeit"}
TERMS = {"BEFRISTET": "befristet", "UNBEFRISTET": "unbefristet"}
CONTRACTS = {"ARBEIT": "permanent", "SELBSTSTAENDIGKEIT": "freelance", "PRAKTIKUM_TRAINEE": "internship",
             "AUSBILDUNG": "apprenticeship"}


def _salary(job: dict) -> str:
    lo, hi = job.get("gehaltsspanneVon"), job.get("gehaltsspanneBis")
    if lo or hi:
        return f"{int(lo or 0)}-{int(hi or 0)} EUR ({job.get('verguetungsangabe', '').lower()})"
    return ""


def parse(payload: dict) -> list[Posting]:
    out = []
    for job in payload.get("ergebnisliste", []):
        locs = job.get("stellenlokationen") or [{}]
        ref = job["referenznummer"]
        out.append(Posting(
            source="ba",
            external_id=ref,
            url=job.get("externeURL") or DETAIL + ref,
            title=job.get("stellenangebotsTitel", ""),
            company=job.get("firma", ""),
            location=", ".join(l.get("adresse", {}).get("ort", "") for l in locs if l.get("adresse")),
            remote=bool(job.get("homeofficemoeglich")),
            contract_type=CONTRACTS.get(job.get("stellenangebotsart"), ""),
            language="de",
            salary=_salary(job),
            text="Berufe: " + ", ".join(job.get("alleBerufe", [])),
            tags=list(job.get("alleBerufe", [])),
            posted_at=job.get("datumErsteVeroeffentlichung", ""),
        ))
    return out


def details_text(job: dict) -> str:
    """Working hours before the description: triage sees only the start of a posting's text."""
    hours = []
    if job.get("arbeitszeitVollzeit"):
        hours.append("Vollzeit")
    part = [name for key, name in PART_TIME if job.get(key)]
    if part:
        hours.append(f"Teilzeit ({', '.join(part)})")
    if job.get("arbeitszeitSchichtNachtWochenende"):
        hours.append("Schicht/Nacht/Wochenende")
    if job.get("istGeringfuegigeBeschaeftigung"):
        hours.append("Minijob")
    lines = [f"{HOURS} {', '.join(hours) or 'keine Angabe'}"]
    if job.get("stellenangebotsart") in KINDS:
        lines.append(f"Art: {KINDS[job['stellenangebotsart']]}")
    if job.get("vertragsdauer") in TERMS:
        lines.append(f"Vertrag: {TERMS[job['vertragsdauer']]}")
    description = (job.get("stellenangebotsBeschreibung") or "").strip()[:MAX_DESCRIPTION]
    return "\n".join(lines) + (f"\n\n{description}" if description else "")


def enrich(posting: Posting, http_get) -> None:
    """Adds the posting's own text; the search result carries only title and occupation (#8)."""
    if HOURS in posting.text:
        return
    ref = base64.b64encode(posting.external_id.encode()).decode()
    details = details_text(http_get(DETAILS + ref, headers=HEADERS))
    posting.text = f"{posting.text}\n{details}" if posting.text else details


def fetch(cfg: dict, http_get) -> list[Posting]:
    seen, postings = set(), []
    for area in cfg.get("areas", [{}]):
        for keyword in area.get("keywords", cfg.get("keywords", [])):
            params = {"was": keyword, "size": cfg.get("page_size", 50),
                      "veroeffentlichtseit": cfg.get("published_since_days", 7),
                      **cfg.get("extra_params", {})}
            if area.get("where"):
                params["wo"] = area["where"]
                params["umkreis"] = area.get("radius_km", 30)
            for p in parse(http_get(f"{URL}?{urlencode(params)}", headers=HEADERS)):
                if p.external_id not in seen:
                    seen.add(p.external_id)
                    if area.get("where"):
                        p.tags.append("nearby")  # inside the radius search
                    postings.append(p)
    return postings
