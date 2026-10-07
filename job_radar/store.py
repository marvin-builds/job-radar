"""SQLite store: dedupe postings and keep their triage score and status."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, fields
from pathlib import Path

from .dedup import fingerprint
from .model import Posting

_STORED = [f.name for f in fields(Posting) if f.name != "id"]

# fetched, new and matched postings; pending = the backlog waiting for triage, sent = what went to
# triage this poll, omitted = sent in a batch that answered but left them out; duplicates took an
# earlier copy's rating, unscored gave up after MAX_ATTEMPTS, expired waited longer than the search goes back
_POLL_COUNTS = ["fetched", "new", "matched", "pending", "sent", "scored", "omitted", "failed_batches",
                "page_hits", "page_misses", "triage_calls", "notified", "duplicates", "unscored", "expired"]
# fingerprint: dedup.fingerprint, the same job from another source or posted again; scored_at: when
# triage rated it; attempts: triage calls that answered without rating it
_POSTING_EXTRA = {"fingerprint": "TEXT", "scored_at": "TEXT", "attempts": "INTEGER DEFAULT 0"}
DUPLICATE_DAYS = 60  # a rating stands in for a copy found this many days later
UNDEDUPED = ("initiativ", "manual")  # an employer or a link sent by the person, never rated as a copy

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(f"{name} TEXT" for name in _STORED)},
    first_seen TEXT DEFAULT (datetime('now')),
    notified_at TEXT,
    {", ".join(f"{name} {kind}" for name, kind in _POSTING_EXTRA.items())},
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    kind TEXT, model TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER,
    five_hour REAL, five_hour_resets INTEGER, seven_day REAL, seven_day_resets INTEGER
);
-- one row per poll, to measure where the AI usage goes (#17); usage.poll_id points here
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    trigger TEXT,             -- timer | cli | suchen | setup
    {", ".join(f"{name} INTEGER" for name in _POLL_COUNTS)},
    finished TEXT
);
CREATE TABLE IF NOT EXISTS interview (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    role TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS marks (
    posting_id INTEGER PRIMARY KEY,
    kind TEXT,          -- saved | applied | removed
    seq INTEGER,        -- newest first
    at TEXT DEFAULT (datetime('now')),
    remind_at TEXT,     -- applied: when to ask "heard anything?"
    outcome TEXT DEFAULT ''  -- applied: gespraech | absage
);
"""


_LIMIT_COLUMNS = {"five_hour": "REAL", "five_hour_resets": "INTEGER", "seven_day": "REAL",
                  "seven_day_resets": "INTEGER"}
# input_tokens is the total; cache_write and cache_read are parts of it. items: postings sent in
# the call, where the caller knows. ok = 0: failed or timed out, error says why.
_MEASURE_COLUMNS = {"cache_write": "INTEGER", "cache_read": "INTEGER", "ok": "INTEGER DEFAULT 1",
                    "items": "INTEGER", "poll_id": "INTEGER", "trigger": "TEXT", "error": "TEXT"}


def _serialized(cls):
    """Run every public method under the store's lock: the listener's chat workers and its
    background jobs (reports, board searches, /suchen) share one connection, and sqlite3
    raises "bad parameter or other API misuse" when two threads use it at once."""
    for name, fn in list(vars(cls).items()):
        if callable(fn) and not name.startswith("_"):
            def locked(self, *args, _fn=fn, **kw):
                with self._lock:
                    return _fn(self, *args, **kw)
            locked.__name__, locked.__doc__ = fn.__name__, fn.__doc__
            setattr(cls, name, locked)
    return cls


def row_posting(row: sqlite3.Row) -> Posting:
    """A postings row as a Posting; columns an older database lacks keep their defaults."""
    data = {n: row[n] for n in _STORED if n in row.keys() and row[n] is not None}
    for n, empty in (("tags", []), ("remote", False), ("score", None)):
        data[n] = json.loads(data[n]) if n in data else empty
        if data[n] is None:
            data[n] = empty
    return Posting(id=row["id"], **data)


@_serialized
class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)  # shared by threads, see _serialized
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        existing = {r["name"] for r in self.db.execute("PRAGMA table_info(postings)")}
        for name in [*_STORED, "first_seen", "notified_at"]:
            if name not in existing:  # databases from older versions
                self.db.execute(f"ALTER TABLE postings ADD COLUMN {name} TEXT")
        for name, kind in _POSTING_EXTRA.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE postings ADD COLUMN {name} {kind}")
        if "fingerprint" not in existing:  # once: older postings find their copies too
            with self.db:
                for row in self.db.execute("SELECT * FROM postings").fetchall():
                    self.db.execute("UPDATE postings SET fingerprint = ? WHERE id = ?",
                                    (fingerprint(row_posting(row)), row["id"]))
        if "scored_at" not in existing:  # rated before it was recorded: about when it was found
            with self.db:
                self.db.execute("UPDATE postings SET scored_at = first_seen WHERE COALESCE(score, 'null') != 'null'")
        self.db.execute("CREATE INDEX IF NOT EXISTS postings_fingerprint ON postings (fingerprint)")
        poll_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(polls)")}
        for name in _POLL_COUNTS:
            if name not in poll_cols:
                self.db.execute(f"ALTER TABLE polls ADD COLUMN {name} INTEGER")
        usage_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(usage)")}
        for name, kind in {**_LIMIT_COLUMNS, **_MEASURE_COLUMNS}.items():
            if name not in usage_cols:
                self.db.execute(f"ALTER TABLE usage ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.db.close()

    def add_new(self, postings: list[Posting]) -> list[Posting]:
        """Insert unseen postings; return them with their ids set."""
        new = []
        with self.db:
            for p in postings:
                row = asdict(p)
                values = [json.dumps(row[n]) if n in ("tags", "remote", "score") else row[n] for n in _STORED]
                cur = self.db.execute(
                    f"INSERT OR IGNORE INTO postings ({', '.join(_STORED)}, fingerprint)"
                    f" VALUES ({', '.join('?' * len(_STORED))}, ?)",
                    [*values, fingerprint(p)],
                )
                if cur.rowcount:
                    p.id = cur.lastrowid
                    new.append(p)
        return new

    def get(self, posting_id: int) -> Posting | None:
        row = self.db.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
        return None if row is None else row_posting(row)

    def record_usage(self, kind: str, u: dict, *, items: int | None = None, poll_id: int | None = None,
                     trigger: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO usage (kind, model, cost_usd, input_tokens, output_tokens, duration_ms,"
                " five_hour, five_hour_resets, seven_day, seven_day_resets, cache_write, cache_read, ok, items,"
                " poll_id, trigger, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, u.get("model"), u.get("cost_usd", 0), u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("duration_ms", 0), *(u.get(c) for c in _LIMIT_COLUMNS), u.get("cache_write"),
                 u.get("cache_read"), u.get("ok", 1), items, poll_id, trigger, u.get("error")))

    def start_poll(self, trigger: str) -> int:
        with self.db:
            return self.db.execute("INSERT INTO polls (trigger) VALUES (?)", (trigger,)).lastrowid

    def finish_poll(self, poll_id: int, counts: dict) -> None:
        known = {k: v for k, v in counts.items() if k in _POLL_COUNTS}
        with self.db:
            self.db.execute(f"UPDATE polls SET {''.join(f'{k} = ?, ' for k in known)}finished = datetime('now')"
                            " WHERE id = ?", (*known.values(), poll_id))

    def latest_limits(self) -> dict | None:
        row = self.db.execute(
            "SELECT five_hour, five_hour_resets, seven_day, seven_day_resets,"
            " CAST(strftime('%s', 'now') - strftime('%s', ts) AS INTEGER) age_s"
            " FROM usage WHERE seven_day IS NOT NULL OR five_hour IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def usage_summary(self, days: int) -> dict:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) calls, SUM(cost_usd) cost_usd, SUM(input_tokens) input_tokens,"
            " SUM(output_tokens) output_tokens FROM usage WHERE ts > datetime('now', ?) GROUP BY kind",
            (f"-{days} days",)).fetchall()
        return {r["kind"]: {k: r[k] for k in ("calls", "cost_usd", "input_tokens", "output_tokens")} for r in rows}

    def by_status(self, status: str) -> list[Posting]:
        rows = self.db.execute("SELECT id FROM postings WHERE status = ? ORDER BY id", (status,)).fetchall()
        return [self.get(r["id"]) for r in rows]

    def find(self, source: str, external_id: str) -> Posting | None:
        row = self.db.execute("SELECT id FROM postings WHERE source = ? AND external_id = ?",
                              (source, external_id)).fetchone()
        return self.get(row["id"]) if row else None

    def companies(self, source: str) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT company FROM postings WHERE source = ? AND company != '' ORDER BY id",
                               (source,)).fetchall()
        return [r["company"] for r in rows]

    def employer_sites(self, min_score: int) -> list[tuple[str, str, str]]:
        """(company, website, location) of employers with a posting triage rated at least `min_score`,
        one per website as its best posting has them, best rated first."""
        rows = self.db.execute(
            "SELECT company, website, location, MAX(CAST(score AS REAL)) best FROM postings"
            " WHERE COALESCE(website, '') != '' AND source != 'initiativ' AND CAST(score AS REAL) >= ?"
            " GROUP BY website ORDER BY best DESC, MAX(id) DESC", (min_score,)).fetchall()
        return [(r["company"], r["website"], r["location"] or "") for r in rows]

    def set_employer(self, posting_id: int, employer: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET employer = ? WHERE id = ?", (employer, posting_id))

    def set_website(self, posting_id: int, website: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET website = ? WHERE id = ?", (website, posting_id))

    def set_posted_at(self, posting_id: int, posted_at: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET posted_at = ? WHERE id = ?", (posted_at, posting_id))

    def posted_at_of(self, url: str) -> str:
        """When a posting at this URL went online, if a source found it with a date."""
        row = self.db.execute("SELECT posted_at FROM postings WHERE url = ? AND COALESCE(posted_at, '') != ''"
                              " ORDER BY id LIMIT 1", (url,)).fetchone()
        return row["posted_at"] if row else ""

    def set_details(self, posting_id: int, text: str, tags: list[str], website: str) -> None:
        """What a source added before triage, in one write."""
        with self.db:
            self.db.execute("UPDATE postings SET text = ?, tags = ?, website = ? WHERE id = ?",
                            (text, json.dumps(tags), website, posting_id))

    def set_meta(self, posting_id: int, company: str, title: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET company = ?, title = ? WHERE id = ?", (company, title, posting_id))
            p = self.get(posting_id)
            self.db.execute("UPDATE postings SET fingerprint = ? WHERE id = ?", (fingerprint(p), posting_id))

    def set_score(self, posting_id: int, score: int, why: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET score = ?, why = ?, scored_at = datetime('now') WHERE id = ?",
                            (json.dumps(score), why, posting_id))

    def scored_twin(self, posting_id: int, since: str = "") -> Posting | None:
        """The latest posting triage rated with the same fingerprint, in the last DUPLICATE_DAYS
        days and not before `since` (UTC, as SQLite writes it: when the profile last changed).
        Copies never stand in, so a rating does not live on past its days through them."""
        row = self.db.execute(
            "SELECT t.id FROM postings p JOIN postings t ON t.fingerprint = p.fingerprint AND t.id != p.id"
            f" WHERE p.id = ? AND p.fingerprint != '' AND p.source NOT IN ({', '.join('?' * len(UNDEDUPED))})"
            f" AND t.source NOT IN ({', '.join('?' * len(UNDEDUPED))})"
            " AND COALESCE(t.score, 'null') != 'null' AND COALESCE(t.status, '') != 'duplicate'"
            " AND t.scored_at > datetime('now', ?) AND t.scored_at >= ? ORDER BY t.scored_at DESC, t.id DESC LIMIT 1",
            (posting_id, *UNDEDUPED, *UNDEDUPED, f"-{DUPLICATE_DAYS} days", since)).fetchone()
        return self.get(row["id"]) if row else None

    def mark_duplicate(self, posting_id: int, twin: Posting) -> None:
        """Takes the twin's rating without a new scored_at; a duplicate is never sent."""
        with self.db:
            self.db.execute("UPDATE postings SET score = ?, why = ?, status = 'duplicate' WHERE id = ?",
                            (json.dumps(twin.score), twin.why, posting_id))

    def add_attempt(self, posting_id: int) -> int:
        """Count one triage call that answered without rating the posting; returns the count."""
        with self.db:
            self.db.execute("UPDATE postings SET attempts = COALESCE(attempts, 0) + 1 WHERE id = ?", (posting_id,))
        return self.db.execute("SELECT attempts FROM postings WHERE id = ?", (posting_id,)).fetchone()[0]

    def expire_pending(self, days: int) -> int:
        """Pending postings found more than `days` ago, longer than the search goes back, become
        "expired"; returns how many."""
        with self.db:
            return self.db.execute("UPDATE postings SET status = 'expired' WHERE status = 'pending'"
                                   " AND first_seen < datetime('now', ?)", (f"-{days} days",)).rowcount

    def set_status(self, posting_id: int, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET status = ? WHERE id = ?", (status, posting_id))

    def mark_notified(self, posting_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET notified_at = datetime('now') WHERE id = ?", (posting_id,))

    def mark(self, posting_id: int, kind: str, remind_in_days: int | None = None) -> None:
        """Bookmark, apply or remove a posting; kept apart from its status, which reports change."""
        remind = f"+{remind_in_days} days" if remind_in_days is not None else None
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO marks (posting_id, kind, seq, at, remind_at, outcome) VALUES"
                " (?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM marks), datetime('now'),"
                " CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END, '')",
                (posting_id, kind, remind, remind))

    def mark_of(self, posting_id: int) -> str:
        row = self.db.execute("SELECT kind FROM marks WHERE posting_id = ?", (posting_id,)).fetchone()
        return row["kind"] if row else ""

    def marked(self, kind: str, limit: int = 50) -> list[tuple[Posting, dict]]:
        """Marked postings, newest first; each mark also says when the radar found the posting."""
        rows = self.db.execute("SELECT m.*, p.first_seen FROM marks m JOIN postings p ON p.id = m.posting_id"
                               " WHERE m.kind = ? ORDER BY m.seq DESC LIMIT ?", (kind, limit)).fetchall()
        return [(self.get(r["posting_id"]), dict(r)) for r in rows]

    def count_marked(self, kind: str) -> int:
        return self.db.execute("SELECT COUNT(*) n FROM marks WHERE kind = ?", (kind,)).fetchone()["n"]

    def first_seen(self, posting_id: int) -> str:
        """When the radar found the posting, in UTC as SQLite writes it."""
        row = self.db.execute("SELECT first_seen FROM postings WHERE id = ?", (posting_id,)).fetchone()
        return row["first_seen"] or "" if row else ""

    def due_reminders(self, days_ahead: int = 0) -> list[tuple[Posting, dict]]:
        rows = self.db.execute(
            "SELECT * FROM marks WHERE kind = 'applied' AND outcome = '' AND remind_at IS NOT NULL"
            " AND remind_at <= datetime('now', ?) ORDER BY seq", (f"+{days_ahead} days",)).fetchall()
        return [(self.get(r["posting_id"]), dict(r)) for r in rows]

    def remind_later(self, posting_id: int, days: int) -> None:
        with self.db:
            self.db.execute("UPDATE marks SET remind_at = datetime('now', ?) WHERE posting_id = ?",
                            (f"+{days} days", posting_id))

    def set_outcome(self, posting_id: int, outcome: str) -> None:
        with self.db:
            self.db.execute("UPDATE marks SET outcome = ? WHERE posting_id = ?", (outcome, posting_id))

    def liked(self, limit: int) -> list[Posting]:
        """The latest bookmarked or applied postings: examples for triage of what this person wants."""
        rows = self.db.execute("SELECT posting_id FROM marks WHERE kind IN ('saved', 'applied')"
                               " ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [self.get(r["posting_id"]) for r in rows]

    def disliked(self, limit: int) -> list[Posting]:
        rows = self.db.execute("SELECT posting_id FROM marks WHERE kind = 'removed' ORDER BY seq DESC LIMIT ?",
                               (limit,)).fetchall()
        return [self.get(r["posting_id"]) for r in rows]

    def interview_add(self, role: str, text: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO interview (role, text) VALUES (?, ?)", (role, text))

    def interview_log(self) -> list[tuple[str, str]]:
        return [(r["role"], r["text"]) for r in self.db.execute("SELECT role, text FROM interview ORDER BY id")]

    def interview_clear(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM interview")

    def get_state(self, key: str) -> str:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def states(self, prefix: str) -> dict[str, str]:
        """The non-empty state values whose key starts with prefix."""
        rows = self.db.execute("SELECT key, value FROM state WHERE key LIKE ? AND value != ''",
                               (prefix.replace("%", r"\%").replace("_", r"\_") + "%",))
        return {r["key"]: r["value"] for r in rows}

    def set_state(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))


class PollLocked(RuntimeError):
    pass


class PollLock:
    """Non-blocking file lock so a manual poll and the timer never overlap."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise PollLocked(f"another poll holds {self.path}")
        return self

    def __exit__(self, *exc):
        self.file.close()  # closing releases the lock
