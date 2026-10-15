"""
Review history, persisted to SQLite so the dashboard survives restarts.

One row per review run, whatever triggered it (webhook, REST API or the
landing-page demo). `steps` and `result` are JSON blobs written by the runner.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    owner       TEXT NOT NULL,
    repo        TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    is_public   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    steps       TEXT NOT NULL DEFAULT '{}',
    result      TEXT,
    client_hash TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews (created_at DESC);
CREATE TABLE IF NOT EXISTS installations (
    id             INTEGER PRIMARY KEY,
    account        TEXT NOT NULL,
    account_type   TEXT,
    repo_selection TEXT,
    status         TEXT NOT NULL,
    updated_at     REAL NOT NULL
);
"""

# Statuses that mean "still in flight".
ACTIVE = ("queued", "running")


class ReviewStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(_SCHEMA)
            # Anything still in flight was killed by the last restart.
            db.execute(
                "UPDATE reviews SET status = 'failed', finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (time.time(),),
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def create(self, *, source: str, owner: str, repo: str, pr_number: int,
               is_public: bool, client_hash: Optional[str] = None) -> str:
        review_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO reviews (id, source, owner, repo, pr_number, is_public, "
                "status, client_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (review_id, source, owner, repo, pr_number, int(is_public),
                 client_hash, time.time()),
            )
        return review_id

    def update(self, review_id: str, **fields: Any) -> None:
        for key in ("steps", "result"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], default=str)
        if "is_public" in fields:
            fields["is_public"] = int(fields["is_public"])
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE reviews SET {columns} WHERE id = ?",
                       (*fields.values(), review_id))

    def get(self, review_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list(self, *, limit: int = 50, include_private: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_private else "WHERE is_public = 1"
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM reviews {where} ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def count_recent(self, client_hash: str, since: float) -> int:
        with self._connect() as db:
            (count,) = db.execute(
                "SELECT COUNT(*) FROM reviews WHERE client_hash = ? AND created_at >= ?",
                (client_hash, since),
            ).fetchone()
        return count

    def save_installation(self, installation_id: int, *, account: str, account_type: str,
                          repo_selection: str, status: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO installations (id, account, account_type, repo_selection, status, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "account = excluded.account, account_type = excluded.account_type, "
                "repo_selection = excluded.repo_selection, status = excluded.status, "
                "updated_at = excluded.updated_at",
                (installation_id, account, account_type, repo_selection, status, time.time()),
            )

    def count_installations(self) -> int:
        with self._connect() as db:
            (count,) = db.execute(
                "SELECT COUNT(*) FROM installations WHERE status = 'active'"
            ).fetchone()
        return count

    def count_active(self) -> int:
        with self._connect() as db:
            (count,) = db.execute(
                "SELECT COUNT(*) FROM reviews WHERE status IN ('queued', 'running')"
            ).fetchone()
        return count


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["is_public"] = bool(data["is_public"])
    data["steps"] = json.loads(data["steps"] or "{}")
    data["result"] = json.loads(data["result"]) if data["result"] else None
    data.pop("client_hash", None)
    return data
