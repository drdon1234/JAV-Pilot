"""Playback history stores durable work destinations, never temporary stream URLs."""
from __future__ import annotations

import hashlib
import json
import math
import time

from ..library.playback import progress_db


def history_id(kind: str, destination: str) -> str:
    return hashlib.sha256(f"{kind}:{destination}".encode()).hexdigest()[:40]


def duration_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 7 * 86400:
        raise ValueError("Invalid playback duration")
    return float(value)


def _table(db):
    db.execute("CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)")


def record(item: dict) -> None:
    item = {**item, "updated_at": time.time()}
    with progress_db() as db:
        _table(db)
        db.execute("INSERT INTO history VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (item["id"], json.dumps(item, ensure_ascii=False), item["updated_at"]))
        db.execute("DELETE FROM history WHERE id IN (SELECT id FROM history ORDER BY updated_at DESC LIMIT -1 OFFSET 2000)")


def records() -> list[dict]:
    with progress_db() as db:
        _table(db)
        rows = db.execute("SELECT payload FROM history ORDER BY updated_at DESC LIMIT 2000").fetchall()
    return [json.loads(row[0]) for row in rows]


def remove(item_id: str) -> None:
    remove_many([item_id])


def remove_many(ids: list[str]) -> None:
    with progress_db() as db:
        _table(db)
        db.executemany("DELETE FROM history WHERE id=?", ((item,) for item in ids))
