"""SQLite-backed key/value cache with per-key TTL.

Every data adapter routes through this cache. Keys are namespaced strings
(e.g. "yf:quote:IONQ"), values are JSON blobs. Expired rows are returned only
if `allow_stale=True`, which is what graceful-degradation paths use.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..utils.time import utc_now


class Cache:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    written_at TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL
                )"""
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def get(self, key: str, allow_stale: bool = False) -> tuple[Any, bool] | None:
        """Returns (value, is_stale) or None if missing."""
        with self._conn() as c:
            row = c.execute(
                "SELECT value_json, written_at, ttl_seconds FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        from datetime import datetime
        written = datetime.fromisoformat(row["written_at"])
        age = (utc_now() - written).total_seconds()
        is_stale = age > row["ttl_seconds"]
        if is_stale and not allow_stale:
            return None
        return json.loads(row["value_json"]), is_stale

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO cache (key, value_json, written_at, ttl_seconds)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       written_at = excluded.written_at,
                       ttl_seconds = excluded.ttl_seconds""",
                (key, json.dumps(value, default=str), utc_now().isoformat(), ttl_seconds),
            )

    def invalidate(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM cache WHERE key = ?", (key,))
