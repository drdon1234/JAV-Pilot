"""Durable submission of explicit catalog and library selections to the task managers."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing, contextmanager

from ...config.paths import default_database_path
from ...config.settings import BUILTIN_WEB_DOWNLOAD_SITE_IDS, load_settings, site_by_id
from ...web_download.jobs import normalize_web_download_code
from ...web_download.config import WebDownloadConfig
from ...web_download.variant import normalize_web_download_variant
from .media import media_library_manager, media_metadata_manager
from .subtitles import subtitle_manager
from .web_downloads import web_download_manager

_LOG = logging.getLogger(__name__)
_WAKE = threading.Event()
_LIMIT = 5000
_FILTERS = {
    "query",
    "actor",
    "maker",
    "tag",
    "series",
    "source",
    "presence",
    "completeness",
    "anomaly",
    "min_height",
    "max_height",
}


@contextmanager
def _connection():
    path = default_database_path("selection_batches.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, action TEXT NOT NULL,
                digest TEXT NOT NULL, skipped INTEGER NOT NULL, created INTEGER NOT NULL,
                total INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0)""")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS items (
                batch TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE, seq INTEGER NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(batch,seq))""")
            db.execute("CREATE INDEX IF NOT EXISTS selection_status ON items(status)")
            yield db
    finally:
        db.close()


def _catalog_items(provider, items):
    if provider not in BUILTIN_WEB_DOWNLOAD_SITE_IDS:
        raise ValueError("Invalid download source")
    site = site_by_id(provider, load_settings())
    if not site or not site.get("enabled") or not WebDownloadConfig.from_env().enabled:
        raise ValueError("Download source unavailable")
    chosen = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"code", "variant"}:
            raise ValueError("Invalid download selection")
        code, key = normalize_web_download_code(item["code"])
        variant = normalize_web_download_variant(item["variant"])
        chosen[key, variant] = [code, variant]
    return list(chosen.values()), len(items) - len(chosen)


def _library_items(action, selection, grouped, filters):
    if (
        type(grouped) is not bool
        or not isinstance(filters, dict)
        or filters.keys() - _FILTERS
        or any(type(v) not in (str, int) or len(str(v)) > 512 for v in filters.values())
        or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in selection)
    ):
        raise ValueError("Invalid library selection")
    index = media_library_manager().index
    entries = []
    for key in dict.fromkeys(selection):
        if grouped:
            offset = 0
            while True:
                page = index.store.list_entries(
                    root_key=index.root_key,
                    group_key=key,
                    limit=200,
                    offset=offset,
                    **filters,
                )
                entries.extend(page["items"])
                if len(entries) > _LIMIT:
                    raise ValueError("每次最多处理 5000 部作品。")
                if not page["has_more"] or not page["items"]:
                    break
                offset += len(page["items"])
        else:
            entries.append(index.store.get_entry(key, root_key=index.root_key))
    chosen = {}
    for entry in entries:
        code = entry.get("code")
        if entry.get("presence") != "present" or not code:
            continue
        if action == "metadata":
            chosen[code] = [index.root_key, code]
        elif len(entry["media_paths"]) == 1:
            chosen[entry["entry_id"]] = [index.root_key, entry["entry_id"]]
    return list(chosen.values()), len(entries) - len(chosen)


def _existing(db, batch, digest):
    row = db.execute("SELECT digest FROM batches WHERE id=?", (batch,)).fetchone()
    if row and row["digest"] != digest:
        raise ValueError("Batch identifier already used for a different selection")
    return bool(row)


def submit(payload):
    batch, action, scope = (payload.get(k) for k in ("id", "action", "scope"))
    items = payload.get("items")
    if (
        not isinstance(batch, str)
        or not re.fullmatch(r"[0-9a-f]{32}", batch)
        or not isinstance(scope, str)
        or not isinstance(items, list)
        or not 1 <= len(items) <= _LIMIT
    ):
        raise ValueError("Invalid selection batch")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with _connection() as db:
        if _existing(db, batch, digest):
            return batch
    if action == "download":
        chosen, skipped = _catalog_items(scope, items)
    elif action in ("metadata", "subtitles") and scope == "library":
        chosen, skipped = _library_items(
            action, items, payload.get("grouped"), payload.get("filters")
        )
    else:
        raise ValueError("Invalid batch action")
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not _existing(db, batch, digest):
            db.execute(
                """INSERT INTO batches(id,scope,action,digest,skipped,total,created)
                   VALUES (?,?,?,?,?,?,strftime('%s','now'))""",
                (batch, scope, action, digest, skipped, len(chosen)),
            )
            db.executemany(
                "INSERT INTO items(batch,seq,payload) VALUES (?,?,?)",
                ((batch, i, json.dumps(item)) for i, item in enumerate(chosen)),
            )
    _WAKE.set()
    return batch


def recent(scope):
    with _connection() as db:
        return [
            dict(r)
            for r in db.execute(
                """
            SELECT id,action,skipped,created,total,done,failed FROM batches WHERE scope=:scope AND
                (done+failed<total OR id IN (SELECT id FROM batches WHERE scope=:scope ORDER BY rowid DESC LIMIT 10))
            ORDER BY rowid DESC
        """,
                {"scope": scope},
            )
        ]


def _dispatch(row):
    item = json.loads(row["payload"])
    if row["action"] == "download":
        provider = row["scope"]
        site = site_by_id(provider, load_settings())
        if not site or not site.get("enabled"):
            raise ValueError("Download source unavailable")
        key = hashlib.sha256(f"{row['batch']}:{row['seq']}".encode()).hexdigest()[:32]
        web_download_manager().start(item[0], key, variant=item[1], provider=provider)
        return
    index = media_library_manager().index
    root, key = item
    if index.root_key != root:
        raise ValueError("Media library root changed")
    if row["action"] == "metadata":
        media_metadata_manager().scan(key)
    else:
        entry = index.store.get_entry(key, root_key=root)
        if (
            entry["presence"] != "present"
            or len(entry["media_paths"]) != 1
            or not entry.get("code")
        ):
            raise ValueError("Media unavailable")
        subtitle_manager().request(
            entry["media_paths"][0], entry["code"], entry.get("variant")
        )


def _next():
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT i.*,b.action,b.scope FROM items i JOIN batches b ON b.id=i.batch
                            WHERE i.status='pending' ORDER BY i.rowid LIMIT 1""").fetchone()
        if row:
            db.execute(
                "UPDATE items SET status='sending' WHERE batch=? AND seq=?",
                (row["batch"], row["seq"]),
            )
        return row


def _finish(db, row, *, failed):
    db.execute(
        "UPDATE batches SET done=done+?,failed=failed+? WHERE id=?",
        (int(not failed), int(failed), row["batch"]),
    )
    db.execute("DELETE FROM items WHERE batch=? AND seq=?", (row["batch"], row["seq"]))


def _run_owned(stop):
    recovery_needed = True
    while not stop.is_set():
        try:
            if recovery_needed:
                with _connection() as db:
                    # Downloads have stable per-item intent keys. A library request interrupted
                    # after dispatch has an unknown outcome and must not be submitted twice.
                    for interrupted in db.execute("""SELECT i.batch,i.seq FROM items i JOIN batches b ON b.id=i.batch
                            WHERE i.status='sending' AND b.action!='download'""").fetchall():
                        _finish(db, interrupted, failed=True)
                    db.execute(
                        "UPDATE items SET status='pending' WHERE status='sending'"
                    )
                recovery_needed = False
            _WAKE.clear()
            row = _next()
            if row:
                try:
                    _dispatch(row)
                    failed = False
                except Exception:
                    _LOG.exception(
                        "Selection item failed (%s/%s)", row["batch"], row["seq"]
                    )
                    failed = True
                with _connection() as db:
                    _finish(db, row, failed=failed)
                continue
        except (sqlite3.Error, OSError):
            _LOG.exception("Selection batch storage unavailable")
            recovery_needed = True
        _WAKE.wait(2)


def run(stop):
    # Hold a separate SQLite write lock for the dispatcher's lifetime. Readers
    # and submissions use the queue database and can proceed during a scan.
    # SQLite releases ownership even if an old container exits mid-dispatch.
    path = default_database_path(".selection-dispatch.lock")
    while not stop.is_set():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=1)) as owner:
                owner.execute("BEGIN IMMEDIATE")
                _run_owned(stop)
        except (sqlite3.Error, OSError):
            stop.wait(2)
