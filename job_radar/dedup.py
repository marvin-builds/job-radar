"""The same job found by several sources, or posted again, shares one fingerprint."""
from __future__ import annotations

import re

from .model import Posting

# "(m/w/d)", "(w/m/d)", "m/f/d", "(all genders)", "(gn*)"
_GENDER = re.compile(r"\(?(?<!\w)[mwfdx](?:\s*/\s*[mwfdx]){1,3}(?!\w)\)?|\((?:all genders|gn\*?)\)", re.I)
# "Gärtner*in", "Mitarbeiter:innen", "Pfleger/in", "Koch_in"
_INNEN = re.compile(r"(?<=\w)[*:/_]in(?:nen)?(?!\w)", re.I)
_LEGAL = re.compile(r"(?<!\w)(?:ggmbh|gmbh|mbh|ag|kgaa|kg|se|ohg|ug|e\.\s?v\.?|ev)(?!\w)|&\s*co\.?(?!\w)", re.I)


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def fingerprint(p: Posting) -> str:
    """Title without gender forms, employer without legal form, and the first city.

    "" (never a duplicate) without a title or an employer: feeds name no employer, and a generic title
    like "Erzieher" alone would merge different employers' jobs.
    """
    title = _words(_INNEN.sub("", _GENDER.sub(" ", p.title)))
    company = _words(_LEGAL.sub(" ", p.company))
    if not title or not company:
        return ""
    city = _words(p.location.split(",")[0])
    return f"{title}|{company}|{city}"
