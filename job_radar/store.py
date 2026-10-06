"""SQLite store: dedupe postings and keep their triage score and status."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, fields
from pathlib import Path

from .model import Posting

_STORED = [f.name for f in fields(Posting) if f.name != "id"]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(f"{name} TEXT" for name in _STORED)},
    first_seen TEXT DEFAULT (datetime('now')),
    notified_at TEXT,
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    kind TEXT, model TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER,
    five_hour REAL, five_hour_resets INTEGER, seven_day REAL, seven_day_resets INTEGER
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
        usage_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(usage)")}
        for name, kind in _LIMIT_COLUMNS.items():
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
                    f"INSERT OR IGNORE INTO postings ({', '.join(_STORED)}) VALUES ({', '.join('?' * len(_STORED))})",
                    values,
                )
                if cur.rowcount:
                    p.id = cur.lastrowid
                    new.append(p)
        return new

    def get(self, posting_id: int) -> Posting | None:
        row = self.db.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
        if row is None:
            return None
        data = {n: row[n] for n in _STORED}
        for n, empty in (("tags", []), ("remote", False), ("score", None)):
            data[n] = json.loads(data[n]) if data[n] is not None else empty
            if data[n] is None:
                data[n] = empty
        return Posting(id=row["id"], **data)

    def record_usage(self, kind: str, u: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO usage (kind, model, cost_usd, input_tokens, output_tokens, duration_ms,"
                " five_hour, five_hour_resets, seven_day, seven_day_resets) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, u.get("model"), u.get("cost_usd", 0), u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("duration_ms", 0), *(u.get(c) for c in _LIMIT_COLUMNS)))

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

    def set_employer(self, posting_id: int, employer: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET employer = ? WHERE id = ?", (employer, posting_id))

    def set_text(self, posting_id: int, text: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET text = ? WHERE id = ?", (text, posting_id))

    def set_meta(self, posting_id: int, company: str, title: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET company = ?, title = ? WHERE id = ?", (company, title, posting_id))

    def set_score(self, posting_id: int, score: int, why: str) -> None:
        with self.db:
            self.db.execute("UPDATE postings SET score = ?, why = ? WHERE id = ?", (json.dumps(score), why, posting_id))

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
        rows = self.db.execute("SELECT * FROM marks WHERE kind = ? ORDER BY seq DESC LIMIT ?", (kind, limit)).fetchall()
        return [(self.get(r["posting_id"]), dict(r)) for r in rows]

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
