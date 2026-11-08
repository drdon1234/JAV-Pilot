"""Small independent bookmark database; playback credentials never enter it."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from ..config.paths import default_database_path
from .catalog import detail_url, sites


@contextmanager
def connection():
    path = default_database_path("bookmarks.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS bookmarks (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, code TEXT NOT NULL, source TEXT NOT NULL,
                cover TEXT NOT NULL, editions TEXT NOT NULL, created_at REAL NOT NULL
            )""")
            yield db
    finally:
        db.close()


def list_bookmarks() -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 5000").fetchall()
    return [{**dict(row), "editions": json.loads(row["editions"])} for row in rows]


def save_bookmark(payload: dict) -> dict:
    kind = payload.get("kind")
    url = str(payload.get("url") or "")
    source = str(payload.get("source") or "")
    if len(url) > 4096 or any(c in url for c in "\r\n\0\\"):
        raise ValueError("收藏地址无效")
    if kind == "detail":
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc or parsed.fragment or not re.fullmatch(r"/works/[^/]+", parsed.path):
            raise ValueError("资料收藏必须指向应用内作品详情页")
        source = ""
    elif kind in {"work", "playback"}:
        site = next((item for item in sites() if item["id"] == source), None)
        if site is None:
            raise ValueError("收藏站点无效")
        url = detail_url(source, site["base_url"].rstrip("/"), url)
    else:
        raise ValueError("请选择资料页、站点作品或播放链接")
    title = str(payload.get("title") or payload.get("code") or url).strip()[:500]
    code = str(payload.get("code") or "")[:64]
    cover = str(payload.get("cover") or "")[:2048]
    if urlsplit(cover).scheme != "https":
        cover = ""
    editions = payload.get("editions", [])
    if not isinstance(editions, list) or len(editions) > 8 or any(not isinstance(x, str) or len(x) > 32 for x in editions):
        raise ValueError("收藏版本信息无效")
    bookmark_id = hashlib.sha256(f"{kind}:{source}:{url}".encode()).hexdigest()[:32]
    with connection() as db:
        if db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] >= 5000 and not db.execute(
            "SELECT 1 FROM bookmarks WHERE id=?", (bookmark_id,)
        ).fetchone():
            raise ValueError("收藏已达 5000 条，请先整理收藏")
        db.execute("""INSERT INTO bookmarks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, code=excluded.code,
            cover=excluded.cover, editions=excluded.editions""",
            (bookmark_id, kind, url, title, code, source, cover, json.dumps(editions), time.time()))
    return {"id": bookmark_id}


def remove_bookmark(bookmark_id: object) -> None:
    remove_bookmarks([str(bookmark_id)])


def remove_bookmarks(ids: list[str]) -> None:
    with connection() as db:
        db.executemany("DELETE FROM bookmarks WHERE id=?", ((item,) for item in ids))
