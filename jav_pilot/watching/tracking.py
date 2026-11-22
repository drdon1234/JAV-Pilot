"""Persistent collection intents, separate from download jobs and media files."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager

from ..config.settings import load_settings, source_list
from ..core.catalog_code import normalize_catalog_code
from ..core.models import SearchBounds
from ..core.observability import emit_json_log
from ..search.engine import search, default_indexers
from .store import connection


_INTENT_SCHEMA = """CREATE TABLE IF NOT EXISTS tracked_items (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, label TEXT NOT NULL,
    aliases TEXT NOT NULL, variant TEXT NOT NULL, height INTEGER NOT NULL, UNIQUE(kind,identity))"""
_CHECK_SCHEMA = """CREATE TABLE IF NOT EXISTS tracking_checks (
    item_id TEXT PRIMARY KEY REFERENCES tracked_items(id) ON DELETE CASCADE,
    checked REAL, due REAL NOT NULL DEFAULT 0, token TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', results TEXT NOT NULL DEFAULT '[]')"""


@contextmanager
def collection():
    with connection() as db:
        db.execute("PRAGMA foreign_keys=ON")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracking_checks'").fetchone():
            with db:
                db.execute("BEGIN IMMEDIATE")
                columns = [r["name"] for r in db.execute("PRAGMA table_info(tracked_items)")]
                legacy = "due" in columns
                if legacy:
                    db.execute("ALTER TABLE tracked_items RENAME TO saved_tracking")
                db.execute(_INTENT_SCHEMA)
                db.execute(_CHECK_SCHEMA)
                if legacy:
                    db.execute("""INSERT INTO tracked_items SELECT id,kind,identity,label,aliases,variant,height
                                  FROM saved_tracking""")
                    db.execute("""INSERT INTO tracking_checks SELECT id,checked,due,token,status,results
                                  FROM saved_tracking""")
                    db.execute("DROP TABLE saved_tracking")
                db.execute("""CREATE TABLE IF NOT EXISTS tracking_alerts (
                    item_id TEXT NOT NULL, code TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(item_id,code))""")
                db.execute("CREATE INDEX IF NOT EXISTS tracking_due ON tracking_checks(due,item_id)")
        yield db


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or not value.strip():
        raise ValueError("请输入有效的名称或番号")
    return unicodedata.normalize("NFKC", value).strip()


def items():
    with collection() as db:
        rows = db.execute("SELECT t.*,c.checked,c.status,c.results FROM tracked_items t JOIN tracking_checks c ON c.item_id=t.id ORDER BY kind,label")
        return [{**{key: row[key] for key in ('id','kind','label','variant','height','checked','status')},
                 'aliases': json.loads(row['aliases']), 'results': json.loads(row['results'])}
                for row in rows]


def update(payload):
    action = payload.get("action")
    with collection() as db:
        db.execute("BEGIN IMMEDIATE")
        if action in {"remove", "recheck"}:
            item_id = _text(payload.get("id"), 32)
            if action == "remove":
                db.execute("DELETE FROM tracking_alerts WHERE item_id=?", (item_id,))
                db.execute("DELETE FROM tracked_items WHERE id=?", (item_id,))
            else:
                db.execute("UPDATE tracking_checks SET due=0,token='' WHERE item_id=?", (item_id,))
            return
        if action != "save" or payload.get("kind") not in {"want","person"}:
            raise ValueError("追踪操作无效")
        kind = payload["kind"]
        label = _text(payload.get("label"), 80)
        identity = label.casefold()
        if kind == "want":
            parsed = normalize_catalog_code(label)
            if not parsed:
                raise ValueError("请输入有效番号")
            label, identity = parsed
        aliases = payload.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 4:
            raise ValueError("最多可登记四个别名")
        aliases = list(dict.fromkeys(_text(alias, 80) for alias in aliases))
        height = payload.get("height", 0)
        variant = payload.get("variant", "")
        if type(height) is not int or height not in {0,720,1080,2160} or variant not in {"","subtitle","original","uncensored"}:
            raise ValueError("版本或画质无效")
        if kind == "person":
            names = {name.casefold() for name in [label,*aliases]}
            for row in db.execute("SELECT identity,label,aliases FROM tracked_items WHERE kind='person'"):
                if row["identity"] != identity and names.intersection(
                    name.casefold() for name in [row["label"],*json.loads(row["aliases"])]
                ):
                    raise ValueError("该别名已关联其他人物")
        if db.execute("SELECT COUNT(*) FROM tracked_items").fetchone()[0] >= 1000 and not db.execute(
            "SELECT 1 FROM tracked_items WHERE kind=? AND identity=?", (kind,identity)
        ).fetchone():
            raise ValueError("追踪清单已满")
        db.execute("""INSERT INTO tracked_items(id,kind,identity,label,aliases,variant,height)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,identity) DO UPDATE SET
            aliases=excluded.aliases,variant=excluded.variant,height=excluded.height""",
            (uuid.uuid4().hex,kind,identity,label,json.dumps(aliases),variant,height))
        db.execute("""INSERT INTO tracking_checks(item_id) SELECT id FROM tracked_items WHERE kind=? AND identity=?
                      ON CONFLICT(item_id) DO UPDATE SET due=0, token='', status='pending', results='[]'""", (kind,identity))


def _matches(magnet, variant, height):
    name = " ".join([magnet.display_name or "", *(badge for ref in magnet.source_refs for badge in ref.badges)])
    labels = {"subtitle": r"中文|中字|字幕|CHS|CHT", "uncensored": r"无码|無碼|uncensored",
              "original": r"原版|原片|original"}
    if variant and not re.search(labels[variant], name, re.I):
        return False
    heights = [int(h) for h in re.findall(r"(?<!\d)(720|1080|2160|4320)[pPiI]?(?!\d)", name)]
    if re.search(r"\b4K\b", name, re.I):
        heights.append(2160)
    return not height or max(heights, default=0) >= height


def discover(row, stopped):
    settings = load_settings()
    sources = source_list("all", settings)
    registry = default_indexers(settings)
    if not registry or not sources:
        return "error", []
    person = row["kind"] == "person"
    names = [row["label"], *json.loads(row["aliases"])] if person else [row["label"]]
    known_names = {name.casefold() for name in names}
    found = {}
    incomplete = False
    for name in names:
        if stopped():
            return "error", []
        response = search(name, sources=sources, indexers=registry, cancelled=stopped,
                          bounds=SearchBounds(search_kind="actor" if person else "code",
                                              fetch_magnets=not person, result_limit=20))
        incomplete |= bool(response.errors) or len(response.skipped) == len(sources)
        for work in response.results:
            code = normalize_catalog_code(work.code)
            if not code:
                continue
            if person:
                if not known_names.intersection(name.casefold() for name in work.actors):
                    continue
            elif code[1] != row["identity"] or not any(
                _matches(magnet,row["variant"],row["height"]) for magnet in work.magnets
            ):
                incomplete |= any(source.magnet_error or source.details_error for source in work.sources)
                continue
            found[code[1]] = {"code": code[0], "work_id": work.work_id, "title": work.title}
    return ("found" if found else "error" if incomplete else "empty"), list(found.values())


def check_one(stopped):
    now = time.time()
    with collection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT t.* FROM tracking_checks c JOIN tracked_items t ON t.id=c.item_id WHERE c.due<=? ORDER BY c.due,c.item_id LIMIT 1", (now,)).fetchone()
        if row is None:
            return
        token = uuid.uuid4().hex
        db.execute("UPDATE tracking_checks SET token=?,due=? WHERE item_id=?", (token,now+1800,row["id"]))
    try:
        status, results = discover(row, stopped)
    except Exception:
        status, results = "error", []
    if stopped():
        return
    with collection() as db:
        cursor = db.execute("""UPDATE tracking_checks SET status=?,results=CASE WHEN ?='error' THEN results ELSE ? END,
            checked=?,due=?,token='' WHERE item_id=? AND token=?""",
            (status,status,json.dumps(results),now,now+(21600 if status=="error" else 86400),row["id"],token))
        if cursor.rowcount:
            db.executemany("INSERT OR IGNORE INTO tracking_alerts(item_id,code) VALUES(?,?)", ((row["id"], r["code"]) for r in results))


def run(stop: threading.Event):
    from ..api.services.notifications import primary_notification_outbox
    from ..notifications.events import NotificationEvent
    while not stop.is_set():
        try:
            check_one(stop.is_set)
            with collection() as db:
                for row in db.execute("SELECT a.*,t.kind FROM tracking_alerts a JOIN tracked_items t ON t.id=a.item_id WHERE a.sent=0 LIMIT 32"):
                    identity = row["item_id"] + ":" + row["code"]
                    primary_notification_outbox().enqueue(NotificationEvent.terminal(
                        source="person_updates" if row["kind"]=="person" else "wanted_resource",
                        entity_id=identity, status="completed", code=row["code"]))
                    db.execute("UPDATE tracking_alerts SET sent=1 WHERE item_id=? AND code=?", (row["item_id"],row["code"]))
        except Exception:
            emit_json_log("tracking","check_deferred",level="warning")
        stop.wait(30)
