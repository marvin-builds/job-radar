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
)
"""


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)  # report threads update status
        self.db.row_factory = sqlite3.Row
        self.db.execute(_SCHEMA)

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

    def find(self, source: str, external_id: str) -> Posting | None:
        row = self.db.execute("SELECT id FROM postings WHERE source = ? AND external_id = ?",
                              (source, external_id)).fetchone()
        return self.get(row["id"]) if row else None

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
