from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Posting:
    source: str
    external_id: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    remote: bool = False
    contract_type: str = ""  # permanent | freelance | temporary | ""
    language: str = ""
    employer: str = ""  # one line on what the company does (from triage)
    salary: str = ""
    text: str = ""
    tags: list[str] = field(default_factory=list)
    posted_at: str = ""
    # set by the store
    id: int | None = None
    score: int | None = None
    why: str = ""
    status: str = "new"
