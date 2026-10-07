"""Reports on an instance's database for measuring AI usage (#17), with no AI call.

Both open the database read-only, so they can run against the live databases on the Pi
while the bot runs, and they read databases from older versions too.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .dedup import fingerprint
from .filter import matches
from .sources import ba
from .store import _POLL_COUNTS, row_posting

BUCKETS = [("0-2", 0, 2), ("3-5", 3, 5), ("6-7", 6, 7), ("8-10", 8, 10)]
DEDUP_DAYS = 60
UNFILTERED = ("initiativ", "manual")  # the poll never filtered these


def open_ro(path: Path) -> sqlite3.Connection:
    """Raises if the file is missing instead of creating it."""
    db = sqlite3.connect(f"file:{quote(str(Path(path).resolve()))}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # fails here, not later, if it cannot be opened
    return db


def _tables(db) -> set[str]:
    return {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _as_filtered(p):
    """The poll filters BA postings before their details are added; cut those off again. A
    posting stored before parse told the kinds of position apart gets its kind from them."""
    if p.source == "ba" and ba.HOURS in p.text:
        if not p.contract_type:
            p.contract_type = next((ba.CONTRACTS[k] for k, name in ba.KINDS.items()
                                    if f"\nArt: {name}\n" in p.text + "\n"), "")
        p.text = p.text[:p.text.index(ba.HOURS)].rstrip("\n")
    return p


def _seen(row) -> datetime | None:
    try:
        return datetime.fromisoformat(row["first_seen"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def replay_filter(path: Path, fcfg: dict, threshold: int, dedup: bool = False) -> dict:
    """Run the filter `fcfg` again on the postings triage already scored. It can only show
    what a stricter rule would drop: postings an older filter dropped were never scored."""
    db = open_ro(path)
    try:
        rows = db.execute("SELECT * FROM postings WHERE score IS NOT NULL AND score != 'null'"
                          f" AND source NOT IN ({', '.join('?' * len(UNFILTERED))}) ORDER BY id",
                          UNFILTERED).fetchall()
        marked = set()
        if "marks" in _tables(db):
            marked = {r["posting_id"] for r in
                      db.execute("SELECT posting_id FROM marks WHERE kind IN ('saved', 'applied')")}
    finally:
        db.close()
    result = {"replayed": 0, "kept": 0, "dropped": 0, "buckets": {name: [0, 0] for name, _, _ in BUCKETS},
              "lost": [], "duplicates": 0, "dedup_lost": [], "pairs": []}
    first = {}  # fingerprint -> (score, first seen, entry) of its latest rated copy
    for row in rows:
        p = _as_filtered(row_posting(row))
        if p.score is None:
            continue
        kept = matches(p, fcfg)
        good = p.score >= threshold or p.id in marked
        entry = {"id": p.id, "score": p.score, "source": p.source, "title": p.title, "url": p.url,
                 "bookmarked": p.id in marked}
        result["replayed"] += 1
        result["kept" if kept else "dropped"] += 1
        bucket = next(name for name, low, high in BUCKETS if p.score <= high or high == 10)
        result["buckets"][bucket][0 if kept else 1] += 1
        if not kept:
            if good:
                result["lost"].append(entry)
            continue
        if not dedup:
            continue
        fp, seen = fingerprint(p), _seen(row)
        earlier = first.get(fp) if fp else None
        if earlier and earlier[0] >= threshold and \
                (seen is None or earlier[1] is None or seen - earlier[1] <= timedelta(days=DEDUP_DAYS)):
            result["duplicates"] += 1  # would get the earlier copy's score instead of a rating
            result["pairs"].append((earlier[2], entry))  # listed so a person can check it is the same job
            if good and earlier[0] < threshold:
                result["dedup_lost"].append(entry)
        elif fp:
            first[fp] = (p.score, seen, entry)  # rated again: the latest rating stands in, as in the bot
    return result


def _line(x: dict) -> str:
    mark = ", bookmarked" if x["bookmarked"] else ""
    return f"  #{x['id']} score {x['score']}{mark} [{x['source']}] {x['title']}"


def replay_text(r: dict) -> str:
    lines = [f"replayed {r['replayed']} scored postings: kept {r['kept']}, dropped {r['dropped']}",
             "score  kept  dropped"]
    lines += [f"{name:5}  {k:4}  {d:7}" for name, (k, d) in r["buckets"].items()]
    lines.append(f"good or bookmarked postings lost: {len(r['lost'])}")
    lines += [_line(x) for x in r["lost"]]
    if r["duplicates"] or r["dedup_lost"]:
        lines.append(f"dedup ({DEDUP_DAYS} days): {r['duplicates']} ratings saved, "
                     f"{len(r['dedup_lost'])} good postings hidden behind a low-rated copy")
        lines += [_line(x) for x in r["dedup_lost"]]
        lines.append("pairs (first copy, then the copy that would not be rated):")
        for a, b in r.get("pairs", []):
            lines += [_line(a) + f" {a['url']}", "  " + _line(b) + f" {b['url']}"]
    return "\n".join(lines)


def _fit(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    """Least squares cost = fixed + per_item * items; None without two different item counts."""
    if len({x for x, _ in points}) < 2:
        return None
    n = len(points)
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    slope = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return my - slope * mx, slope


def _avg(values: list) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def usage_stats(path: Path, days: int) -> dict:
    db = open_ro(path)
    try:
        since = f"-{days} days"
        rows = [dict(r) for r in db.execute("SELECT * FROM usage WHERE ts > datetime('now', ?) ORDER BY id", (since,))]
        polls = None
        if "polls" in _tables(db):
            polls = [dict(r) for r in db.execute("SELECT * FROM polls WHERE ts > datetime('now', ?)", (since,))]
    finally:
        db.close()
    kinds = {}
    for kind in sorted({r["kind"] for r in rows}):
        calls = [r for r in rows if r["kind"] == kind]
        ok = [r for r in calls if r.get("ok", 1) != 0]
        sized = [(r["items"], r["cost_usd"] or 0) for r in ok if r.get("items")]
        fit = _fit(sized)
        kinds[kind] = {
            "calls": len(calls), "failed": len(calls) - len(ok), "cost": sum(r["cost_usd"] or 0 for r in calls),
            "cost_per_call": _avg([r["cost_usd"] or 0 for r in ok]),
            **{k: _avg([r.get(k) for r in ok]) for k in ("input_tokens", "cache_write", "cache_read", "output_tokens")},
            "seconds": (_avg([r["duration_ms"] for r in ok]) or 0) / 1000,
            "items_per_call": _avg([x for x, _ in sized]),
            "cost_per_item": sum(c for _, c in sized) / sum(x for x, _ in sized) if sized else None,
            "fixed": fit[0] if fit else None, "per_item": fit[1] if fit else None}
    poll_stats = None
    if polls:
        trigger_of = {p["id"]: p["trigger"] for p in polls}
        poll_stats = {"polls": len(polls), "triggers": {}, "trigger_cost": {},
                      "unfinished": sum(1 for p in polls if not p.get("finished")),  # crashed or still running
                      "sum": {k: sum(p.get(k) or 0 for p in polls) for k in _POLL_COUNTS},
                      "cost": sum(r["cost_usd"] or 0 for r in rows if r.get("poll_id") in trigger_of)}
        for p in polls:
            poll_stats["triggers"][p["trigger"]] = poll_stats["triggers"].get(p["trigger"], 0) + 1
            poll_stats["trigger_cost"].setdefault(p["trigger"], 0)
        for r in rows:
            if r.get("poll_id") in trigger_of:
                poll_stats["trigger_cost"][trigger_of[r["poll_id"]]] += r["cost_usd"] or 0
    return {"days": days, "kinds": kinds, "polls": poll_stats}


def _money(v: float | None, digits: int = 4) -> str:
    return "-" if v is None else f"${v:.{digits}f}"


def _k(v: float | None) -> str:
    return "-" if v is None else f"{v / 1000:.1f}k"


def usage_text(s: dict) -> str:
    lines = [f"AI calls of the last {s['days']} days (list prices; input = uncached + cache write + cache read)"]
    if not s["kinds"]:
        lines.append("no calls")
    for kind, k in s["kinds"].items():
        lines.append(f"{kind}: {k['calls']} calls ({k['failed']} failed), {_money(k['cost'], 2)}, "
                     f"{_money(k['cost_per_call'])} per call, {k['seconds']:.0f} s per call")
        lines.append(f"  per call: input {_k(k['input_tokens'])} (cache write {_k(k['cache_write'])}, "
                     f"read {_k(k['cache_read'])}), output {_k(k['output_tokens'])}")
        if k["cost_per_item"] is not None:
            per = f"  {k['items_per_call']:.1f} postings per call, {_money(k['cost_per_item'])} per posting"
            if k["fixed"] is not None:
                per += f"; fitted: fixed {_money(k['fixed'])} per call + {_money(k['per_item'])} per posting"
            lines.append(per)
    p = s["polls"]
    if not p:
        lines.append("polls: none")
        return "\n".join(lines)
    triggers = ", ".join(f"{t} {n} ({_money(p['trigger_cost'][t], 2)})"
                         for t, n in sorted(p["triggers"].items(), key=lambda kv: str(kv[0])))
    lines.append(f"polls: {p['polls']} ({triggers}; {p['unfinished']} unfinished), AI in polls "
                 f"{_money(p['cost'], 2)}, {_money(p['cost'] / p['polls'])} per poll")
    lines.append("  in total: " + ", ".join(f"{k} {v}" for k, v in p["sum"].items()))
    return "\n".join(lines)
