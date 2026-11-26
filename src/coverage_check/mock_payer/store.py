"""The payer's own records, in SQLite: every request it received, and the answers by key.

The request log is the evidence for invariant 1, so it belongs to the payer and not to the
system it judges (ADR 0009). It holds no member details.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT,
    body_sha256 TEXT NOT NULL,
    received_at REAL NOT NULL,
    replied_at REAL,
    status INTEGER,
    behaviour TEXT,
    occurrence INTEGER,
    replay INTEGER NOT NULL DEFAULT 0,
    billable INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS requests_body ON requests (body_sha256);
CREATE TABLE IF NOT EXISTS answers (
    idempotency_key TEXT PRIMARY KEY,
    body_sha256 TEXT NOT NULL,
    reply TEXT NOT NULL
);
"""


class Store:
    """One connection, used only from the server's event loop. Each write commits at once."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        # Left over from a payer that stopped while handling them.
        self._db.execute("UPDATE requests SET behaviour = 'aborted' WHERE behaviour IS NULL")

    def close(self) -> None:
        self._db.close()

    def received(self, key: str | None, body_sha256: str, at: float) -> int:
        cursor = self._db.execute(
            "INSERT INTO requests (idempotency_key, body_sha256, received_at) VALUES (?, ?, ?)",
            (key, body_sha256, at),
        )
        assert cursor.lastrowid is not None  # noqa: S101 - set by every insert
        return cursor.lastrowid

    def next_occurrence(self, request_id: int, body_sha256: str) -> int:
        """Number this request among those with the same body that got as far as a fault draw."""
        (occurrence,) = self._db.execute(
            "SELECT count(occurrence) FROM requests WHERE body_sha256 = ?", (body_sha256,)
        ).fetchone()
        self._db.execute(
            "UPDATE requests SET occurrence = ? WHERE id = ?", (occurrence, request_id)
        )
        return int(occurrence)

    def answer(self, key: str) -> tuple[str, str] | None:
        """The body hash and reply stored under this key."""
        row = self._db.execute(
            "SELECT body_sha256, reply FROM answers WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else (row["body_sha256"], row["reply"])

    def bill(self, request_id: int, key: str, body_sha256: str, reply: str) -> None:
        """Keep the answer and mark the request billable, together, before replying."""
        self._db.execute("BEGIN")
        with self._db:
            self._db.execute("INSERT INTO answers VALUES (?, ?, ?)", (key, body_sha256, reply))
            self._db.execute("UPDATE requests SET billable = 1 WHERE id = ?", (request_id,))

    def replied(
        self, request_id: int, at: float, status: int | None, behaviour: str, replay: bool
    ) -> None:
        self._db.execute(
            "UPDATE requests SET replied_at = ?, status = ?, behaviour = ?, replay = ?"
            " WHERE id = ?",
            (at, status, behaviour, replay, request_id),
        )

    def log(self, after: int, limit: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM requests WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
        ).fetchall()
        return [
            {**dict(r), "replay": bool(r["replay"]), "billable": bool(r["billable"])} for r in rows
        ]
