"""Automatically kept history of site searches and Web resource searches.

Each record stores the parameters needed to run the same search again. The
same search repeated later updates its existing record instead of adding a
duplicate, and only the newest ``limit`` records are kept.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from ..config.paths import default_database_path

KINDS = ("metadata", "resource")
MAX_PARAMS_BYTES = 8 * 1024
DEFAULT_LIMIT = 100
_LOCK = threading.Lock()


class SearchHistoryError(ValueError):
    pass


class SearchHistoryStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self.path = Path(database_path or default_database_path("search_history.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS search_history ("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, "
                "query TEXT NOT NULL, params_json TEXT NOT NULL, result_count INTEGER, "
                "use_count INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, "
                "used_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS search_history_used_at ON search_history(used_at DESC)"
            )
            connection.commit()

    def record(
        self,
        kind: str,
        query: str,
        params: dict[str, object],
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, object]:
        if kind not in KINDS:
            raise SearchHistoryError("search history kind is invalid")
        clean_query = " ".join(str(query or "").split())[:200]
        if not clean_query:
            raise SearchHistoryError("search history query is empty")
        params_json = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(params_json.encode("utf-8")) > MAX_PARAMS_BYTES:
            raise SearchHistoryError("search history parameters are too large")
        fingerprint = hashlib.sha256(f"{kind}\0{params_json}".encode("utf-8")).hexdigest()
        now = time.time()
        with _LOCK, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM search_history WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is None:
                record_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO search_history (id, kind, fingerprint, query, params_json, "
                    "use_count, created_at, used_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (record_id, kind, fingerprint, clean_query, params_json, now, now),
                )
            else:
                record_id = str(row[0])
                connection.execute(
                    "UPDATE search_history SET used_at = ?, use_count = use_count + 1 WHERE id = ?",
                    (now, record_id),
                )
            keep = max(10, min(int(limit), 1000))
            connection.execute(
                "DELETE FROM search_history WHERE id NOT IN "
                "(SELECT id FROM search_history ORDER BY used_at DESC LIMIT ?)",
                (keep,),
            )
            connection.commit()
        return self.get(record_id)

    def set_result_count(self, kind: str, params: dict[str, object], count: int) -> None:
        params_json = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(f"{kind}\0{params_json}".encode("utf-8")).hexdigest()
        with _LOCK, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE search_history SET result_count = ? WHERE fingerprint = ?",
                (max(0, int(count)), fingerprint),
            )
            connection.commit()

    def get(self, record_id: str) -> dict[str, object]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM search_history WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise SearchHistoryError("search history record was not found")
        return _public(row)

    def list(self, *, limit: int = 100, offset: int = 0, kind: str | None = None) -> dict[str, object]:
        clean_limit = max(1, min(int(limit), 200))
        clean_offset = max(0, int(offset))
        clauses, values = ([], []) if kind is None else (["kind = ?"], [kind])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM search_history {where}", values).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM search_history {where} ORDER BY used_at DESC LIMIT ? OFFSET ?",
                (*values, clean_limit, clean_offset),
            ).fetchall()
        return {"items": [_public(row) for row in rows], "total": total}

    def remove(self, record_id: str) -> bool:
        with _LOCK, closing(self._connect()) as connection:
            changed = connection.execute(
                "DELETE FROM search_history WHERE id = ?", (record_id,)
            ).rowcount
            connection.commit()
        return bool(changed)

    def clear(self) -> int:
        with _LOCK, closing(self._connect()) as connection:
            changed = connection.execute("DELETE FROM search_history").rowcount
            connection.commit()
        return int(changed)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def _public(row: sqlite3.Row) -> dict[str, object]:
    try:
        params = json.loads(str(row["params_json"]))
    except ValueError:
        params = {}
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "query": str(row["query"]),
        "params": params if isinstance(params, dict) else {},
        "result_count": row["result_count"],
        "use_count": int(row["use_count"]),
        "created_at": float(row["created_at"]),
        "used_at": float(row["used_at"]),
    }
