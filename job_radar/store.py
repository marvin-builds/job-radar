"""SQLite store: dedupe postings and keep their triage score and status."""
from __future__ import annotations

import json
import sqlite3
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
    kind TEXT, model TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER
);
"""


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)  # report threads update status
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        existing = {r["name"] for r in self.db.execute("PRAGMA table_info(postings)")}
        for name in [*_STORED, "first_seen", "notified_at"]:
            if name not in existing:  # databases from older versions
                self.db.execute(f"ALTER TABLE postings ADD COLUMN {name} TEXT")

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
        for n in ("tags", "remote", "score"):
            data[n] = json.loads(data[n])
        return Posting(id=row["id"], **data)

    def record_usage(self, kind: str, u: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO usage (kind, model, cost_usd, input_tokens, output_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, u.get("model"), u.get("cost_usd", 0), u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("duration_ms", 0)))

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
