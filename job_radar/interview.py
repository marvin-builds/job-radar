"""Telegram onboarding: Claude interviews the person and derives profile and search settings.

Each turn is one stateless `claude -p` call without tools; the transcript lives in the store.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from typing import NamedTuple

from . import claude
from .config import validate_search

ROLES = {"bot": "Interviewer", "person": "Person"}


class Option(NamedTuple):
    code: str
    label: str
    fact: str  # what the model is told, in its prompt's language
    settings: dict  # sources.ba settings: radius_km, remote, or an extra_params code (None removes it)


class Question(NamedTuple):
    key: str
    text: str
    options: tuple[Option, ...]


# Closed questions of a first setup, asked with buttons before the model's interview: each
# costs a tap instead of a model turn, and code writes their settings, so the model neither
# asks them again nor gets the settings wrong.
QUESTIONS = (
    Question("kind", "Was für eine Stelle suchst du?", (
        Option("job", "Eine normale Stelle", "Kind of position: a regular job only, no internship, traineeship or "
               "apprenticeship", {"angebotsart": "1"}),
        Option("intern", "Praktikum oder Trainee", "Kind of position: internship or traineeship only",
               {"angebotsart": "34"}),
        Option("train", "Ausbildung oder duales Studium", "Kind of position: apprenticeship or dual study only",
               {"angebotsart": "4"}),
        Option("any", "Mehreres davon", "Kind of position: open to several kinds (regular job, internship, "
               "apprenticeship)", {"angebotsart": None}))),
    Question("hours", "Möchtest du Vollzeit oder Teilzeit arbeiten?", (
        Option("full", "Vollzeit", "Working time: full-time", {}),
        Option("part", "Teilzeit", "Working time: part-time (ask how many hours)", {}),
        Option("both", "Beides geht", "Working time: full-time or part-time", {}),
        Option("mini", "Minijob", "Working time: a mini-job", {}))),
    Question("km", "Wie weit darf dein Arbeitsweg höchstens sein?", tuple(
        Option(str(km), f"bis {km} km", f"Maximum commute: {km} km", {"radius_km": km}) for km in (10, 25, 50, 100))),
    Question("remote", "Willst du vor Ort arbeiten oder auch von zu Hause?", (
        Option("site", "Vor Ort", "Remote work: wants to work on site", {"remote": False}),
        Option("hybrid", "Teilweise Homeoffice wäre schön", "Remote work: on site, some home office welcome",
               {"remote": False}),
        Option("remote", "Auch ganz von zu Hause", "Remote work: fully remote jobs from anywhere in Germany are "
               "welcome too", {"remote": True}))),
    # 2;3 keeps postings that say nothing about it: only 4 of 314 nursing jobs near Kassel are
    # marked fixed-term, 133 say nothing (checked against the API on 2026-10-07).
    Question("term", "Darf die Stelle befristet sein?", (
        Option("perm", "Nur unbefristet", "Fixed-term: only permanent contracts, fixed-term is a deal-breaker",
               {"befristung": "2;3"}),
        Option("any", "Befristet ist auch okay", "Fixed-term: fixed-term contracts are fine too",
               {"befristung": None}))),
    Question("temp", "Kommt Zeitarbeit für dich in Frage?", (
        Option("no", "Keine Zeitarbeit", "Temp agencies: not wanted", {"zeitarbeit": "false"}),
        Option("ok", "Zeitarbeit ist okay", "Temp agencies: fine", {"zeitarbeit": None}))),
    Question("lang", "In welcher Sprache möchtest du die Berichte zu Stellen lesen?", (
        Option("de", "Deutsch", "Report language: German", {}),
        Option("en", "Englisch", "Report language: English", {}),
        Option("other", "Eine andere", "Report language: another one (ask which)", {}))),
)
_BY_KEY = {q.key: q for q in QUESTIONS}


def next_question(answers: dict) -> str | None:
    """The key of the first question not answered yet."""
    return next((q.key for q in QUESTIONS if q.key not in answers), None)


def ask(key: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """A question's text and its buttons, one option per row."""
    q = _BY_KEY[key]
    return q.text, [[(o.label, f"b:{key}:{o.code}")] for o in q.options]


def _option(key: str, code: str) -> Option | None:
    q = _BY_KEY.get(key)
    return next((o for o in q.options if o.code == code), None) if q else None


def answer_of(data: str) -> tuple[str, str] | None:
    """(question key, option code) of a button's callback data, or None if no such option exists."""
    _, key, code = (data.split(":") + ["", ""])[:3]
    return (key, code) if _option(key, code) else None


def label(key: str, code: str) -> str:
    return _option(key, code).label


def facts(answers: dict) -> str:
    lines = [f"- {key}: {o.fact}" for key, code in answers.items() if (o := _option(key, code))]
    return "\n".join(lines) or "(none)"


def apply(search: dict, answers: dict, changed=()) -> dict:
    """The search settings with the answers' settings written in, except for answers the person
    changed later in the chat (the model then wrote them)."""
    search = copy.deepcopy(search)
    ba = search.setdefault("sources", {}).setdefault("ba", {})
    for key, code in answers.items():
        o = _option(key, code)
        if o is None or key in changed:
            continue
        for name, value in o.settings.items():
            areas = ba.get("areas", [])
            towns = [a for a in areas if a.get("where")]
            if name == "radius_km":
                for area in towns:
                    area["radius_km"] = value
            elif name == "remote":
                # An area without a town searches all of Germany; the filter then keeps only its
                # remote postings. Without any town that area is the whole search, so it stays.
                if value and len(towns) == len(areas):
                    ba["areas"] = areas + [{}]
                elif not value and towns:
                    ba["areas"] = towns
            elif value is None:
                ba.get("extra_params", {}).pop(name, None)
            else:
                ba.setdefault("extra_params", {})[name] = value
    if "extra_params" in ba and not ba["extra_params"]:
        del ba["extra_params"]
    return search


class InterviewError(RuntimeError):
    pass


def _transcript(log: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{ROLES.get(role, role)}: {text}" for role, text in log) or "(no messages yet: start)"


def _parse(reply: str) -> dict:
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        raise InterviewError(f"no JSON object in reply: {reply[:200]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise InterviewError(f"bad JSON: {e}") from e
    if not data.get("done"):
        if not isinstance(data.get("say"), str) or not data["say"].strip():
            raise InterviewError('a question reply needs a non-empty "say"')
        return {"say": data["say"], "done": False}
    for key in ("summary", "profile_md"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise InterviewError(f'a finished reply needs a non-empty "{key}"')
    try:
        validate_search(data.get("search"))
    except ValueError as e:
        raise InterviewError(f"invalid search settings: {e}") from e
    search = data["search"]
    if not search.get("sources", {}).get("ba", {}).get("keywords"):
        raise InterviewError("search settings need sources.ba.keywords")
    if not search.get("filter", {}).get("include"):
        raise InterviewError("search settings need filter.include")
    done = {"done": True, "summary": data["summary"], "profile_md": data["profile_md"], "search": data["search"]}
    changed = data.get("changed") if isinstance(data.get("changed"), list) else []
    changed = [k for k in changed if isinstance(k, str)]
    return {**done, "changed": changed} if changed else done


def turn(log: list[tuple[str, str]], *, previous: str, model: str, runner=subprocess.run, on_usage=None,
         facts: str = "(none)") -> dict:
    """The interviewer's next message, or the finished profile and settings. `facts` are the
    answers the person gave with buttons."""
    prompt = claude._fill("interview.md", previous=previous or "(none: first setup)", transcript=_transcript(log),
                          answers=facts)
    error = None
    for _ in range(2):
        text = prompt if error is None else (
            f"{prompt}\n\n## Your previous reply was rejected\n\n{error}\nReply again, fixing this.")
        try:
            return _parse(claude.run(text, model=model, tools=[], runner=runner, on_usage=on_usage))
        except InterviewError as e:
            error = str(e)
    raise InterviewError(error)
