"""Bundesagentur fuer Arbeit Jobsuche API (public key, used by the official app)."""
from __future__ import annotations

import base64
import re
from urllib.parse import urlencode, urlsplit

from ..model import Posting

URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
DETAILS = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/"
STAFFING = "Zeitarbeit"  # tag of a posting by a temp agency (Arbeitnehmerüberlassung)
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


# Words of an employer's name that say nothing about which employer it is.
_GENERIC = {"gmbh", "niederlassung", "gesellschaft", "deutschland", "germany", "group", "gruppe", "holding",
            "service", "services", "personal", "verwaltung", "filiale"}
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _website(url, company: str) -> str:
    """The homepage of the partner that sent the posting to the BA, if its host names the
    employer: the partner is often a job board or the BA itself."""
    url = str(url or "").strip()
    if not re.match(r"https?://", url):
        if re.match(r"[a-z][\w+.-]*:", url, re.I):  # another scheme
            return ""
        url = "https://" + url
    host = (urlsplit(url).hostname or "").casefold()
    words = [w for w in re.findall(r"[^\W_]+", company.casefold()) if len(w) >= 4 and w not in _GENERIC]
    return url if host and any(w in host or w.translate(_UMLAUTS) in host for w in words) else ""


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
    start = job.get("eintrittszeitraum") or {}
    if start.get("von"):
        lines.append(f"Eintritt: {start['von']} bis {start['bis']}" if start.get("bis") else f"Eintritt: ab {start['von']}")
    if job.get("hauptberuf"):
        lines.append(f"Hauptberuf: {job['hauptberuf']}")
    if job.get("istArbeitnehmerUeberlassung"):
        lines.append("Zeitarbeit: ja (Arbeitnehmerüberlassung)")
    description = (job.get("stellenangebotsBeschreibung") or "").strip()[:MAX_DESCRIPTION]
    return "\n".join(lines) + (f"\n\n{description}" if description else "")


def enrich(posting: Posting, http_get) -> None:
    """Adds the posting's own text; the search result carries only title and occupation (#8)."""
    if HOURS in posting.text:
        return
    ref = base64.b64encode(posting.external_id.encode()).decode()
    job = http_get(DETAILS + ref, headers=HEADERS)
    details = details_text(job)
    posting.text = f"{posting.text}\n{details}" if posting.text else details
    if job.get("istArbeitnehmerUeberlassung") and STAFFING not in posting.tags:
        posting.tags.append(STAFFING)
    posting.website = posting.website or _website(job.get("allianzpartnerUrl"), posting.company)


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
