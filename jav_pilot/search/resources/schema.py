"""SQLite schema creation, migrations and verification for resource searches."""

from __future__ import annotations

import hashlib
import sqlite3

from ...core.catalog_code import normalize_catalog_code
from ...core.migrations import require_columns
from .errors import ResourceSearchError
from .validation import make_item_id

def create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE resource_search_sessions (
            session_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            query TEXT NOT NULL,
            result_limit INTEGER NOT NULL CHECK (result_limit BETWEEN 1 AND 999),
            suffix_width INTEGER,
            range_start INTEGER,
            range_end INTEGER,
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'limit_reached', 'completed', 'failed',
                'cancelled'
            )),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            item_count INTEGER NOT NULL CHECK (item_count BETWEEN 0 AND 999),
            next_page INTEGER,
            pending_cursor INTEGER NOT NULL CHECK (pending_cursor >= 0),
            total_pages INTEGER,
            scanned_pages INTEGER NOT NULL CHECK (scanned_pages >= 0),
            error_code TEXT,
            retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            started_at REAL,
            heartbeat_at REAL,
            finished_at REAL
        )
        """,
        """
        CREATE TABLE resource_search_items (
            item_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES resource_search_sessions(session_id)
                ON DELETE CASCADE,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            created_at REAL NOT NULL,
            UNIQUE (session_id, code_key),
            UNIQUE (session_id, ordinal)
        )
        """,
        """
        CREATE TABLE resource_search_variants (
            item_id TEXT NOT NULL REFERENCES resource_search_items(item_id)
                ON DELETE CASCADE,
            variant TEXT NOT NULL CHECK (variant IN (
                'original', 'chinese_subtitle', 'uncensored_leak'
            )),
            PRIMARY KEY (item_id, variant)
        )
        """,
        """
        CREATE TABLE resource_search_pending (
            session_id TEXT NOT NULL REFERENCES resource_search_sessions(session_id)
                ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            variants_json TEXT NOT NULL,
            PRIMARY KEY (session_id, position),
            UNIQUE (session_id, code_key)
        )
        """,
        """
        CREATE INDEX resource_search_items_session_code
            ON resource_search_items(session_id, code)
        """,
    )
    for statement in statements:
        connection.execute(statement)


def migrate_schema_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE resource_search_sessions "
        "ADD COLUMN scan_generation_revision INTEGER NOT NULL DEFAULT 1 "
        "CHECK (scan_generation_revision >= 1 "
        "AND scan_generation_revision <= revision)"
    )
    connection.execute(
        "UPDATE resource_search_sessions SET scan_generation_revision = revision"
    )


def migrate_schema_v3(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE resource_search_items ADD COLUMN title TEXT")
    connection.execute("ALTER TABLE resource_search_pending ADD COLUMN title TEXT")


def migrate_schema_v4(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA defer_foreign_keys = ON")
    item_rows = connection.execute(
        "SELECT item_id, session_id, code, code_key FROM resource_search_items "
        "ORDER BY session_id, ordinal"
    ).fetchall()
    item_targets: dict[tuple[str, str], str] = {}
    item_updates: list[tuple[str, str, str, str, str]] = []
    occupied_ids = {str(row["item_id"]) for row in item_rows}
    existing_item_ids = set(occupied_ids)
    target_ids: set[str] = set()
    for row in item_rows:
        normalized = normalize_catalog_code(row["code"], max_length=32)
        if normalized is None:
            raise ResourceSearchError("resource search item is invalid")
        display_code, code_key = normalized
        session_id = str(row["session_id"])
        item_id = str(row["item_id"])
        identity = (session_id, code_key)
        collided = item_targets.setdefault(identity, item_id)
        if collided != item_id:
            raise ResourceSearchError(
                "resource search items collide after catalog code normalization"
            )
        target_id = make_item_id(session_id, code_key)
        if target_id in target_ids and target_id != item_id:
            raise ResourceSearchError(
                "resource search item identities collide after normalization"
            )
        if target_id in existing_item_ids and target_id != item_id:
            raise ResourceSearchError(
                "resource search item identity collides with an existing item"
            )
        target_ids.add(target_id)
        if (
            display_code != str(row["code"])
            or code_key != str(row["code_key"])
            or target_id != item_id
        ):
            temporary_id = _migration_item_id(
                session_id,
                item_id,
                occupied_ids | target_ids,
            )
            occupied_ids.add(temporary_id)
            item_updates.append(
                (item_id, temporary_id, target_id, display_code, code_key)
            )
    for item_id, temporary_id, _target_id, _display, _code_key in item_updates:
        connection.execute(
            "UPDATE resource_search_items SET item_id = ? WHERE item_id = ?",
            (temporary_id, item_id),
        )
        connection.execute(
            "UPDATE resource_search_variants SET item_id = ? WHERE item_id = ?",
            (temporary_id, item_id),
        )
    for _old_item_id, temporary_id, target_id, display, code_key in item_updates:
        connection.execute(
            "UPDATE resource_search_items SET item_id = ?, code = ?, code_key = ? "
            "WHERE item_id = ?",
            (target_id, display, code_key, temporary_id),
        )
        connection.execute(
            "UPDATE resource_search_variants SET item_id = ? WHERE item_id = ?",
            (target_id, temporary_id),
        )

    pending_rows = connection.execute(
        "SELECT session_id, position, code, code_key "
        "FROM resource_search_pending ORDER BY session_id, position"
    ).fetchall()
    pending_targets: dict[tuple[str, str], int] = {}
    pending_updates: list[tuple[str, int, str, str, str]] = []
    for row in pending_rows:
        normalized = normalize_catalog_code(row["code"], max_length=32)
        if normalized is None:
            raise ResourceSearchError("resource search pending item is invalid")
        display_code, code_key = normalized
        session_id = str(row["session_id"])
        position = int(row["position"])
        identity = (session_id, code_key)
        collided = pending_targets.setdefault(identity, position)
        if collided != position:
            raise ResourceSearchError(
                "resource search pending items collide after catalog code normalization"
            )
        if display_code != str(row["code"]) or code_key != str(row["code_key"]):
            temporary_key = f"MIGRATION{position:09d}{hashlib.sha256(f'{session_id}:{position}'.encode('ascii')).hexdigest()[:16]}"
            pending_updates.append(
                (session_id, position, temporary_key, display_code, code_key)
            )
    for session_id, position, temporary_key, _display, _code_key in pending_updates:
        connection.execute(
            "UPDATE resource_search_pending SET code_key = ? "
            "WHERE session_id = ? AND position = ?",
            (temporary_key, session_id, position),
        )
    for session_id, position, _temporary_key, display, code_key in pending_updates:
        connection.execute(
            "UPDATE resource_search_pending SET code = ?, code_key = ? "
            "WHERE session_id = ? AND position = ?",
            (display, code_key, session_id, position),
        )


def _migration_item_id(
    session_id: str,
    item_id: str,
    occupied: set[str],
) -> str:
    for salt in range(1024):
        candidate = hashlib.sha256(
            f"catalog-migration:{session_id}:{item_id}:{salt}".encode("ascii")
        ).hexdigest()[:32]
        if candidate not in occupied:
            return candidate
    raise ResourceSearchError("resource search item identity migration collided")


def verify_schema_v1(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "resource_search_sessions",
        (
            "session_id",
            "source_id",
            "query",
            "result_limit",
            "suffix_width",
            "range_start",
            "range_end",
            "status",
            "revision",
            "item_count",
            "next_page",
            "pending_cursor",
            "total_pages",
            "scanned_pages",
            "error_code",
            "retryable",
            "created_at",
            "updated_at",
            "started_at",
            "heartbeat_at",
            "finished_at",
        ),
    )
    require_columns(
        connection,
        "resource_search_items",
        ("item_id", "session_id", "code", "code_key", "ordinal", "created_at"),
    )
    require_columns(
        connection,
        "resource_search_variants",
        ("item_id", "variant"),
    )
    require_columns(
        connection,
        "resource_search_pending",
        ("session_id", "position", "code", "code_key", "variants_json"),
    )


def verify_schema_v2(connection: sqlite3.Connection) -> None:
    verify_schema_v1(connection)
    require_columns(
        connection,
        "resource_search_sessions",
        ("scan_generation_revision",),
    )


def verify_schema(connection: sqlite3.Connection) -> None:
    verify_schema_v2(connection)
    require_columns(connection, "resource_search_items", ("title",))
    require_columns(connection, "resource_search_pending", ("title",))
    for row in connection.execute(
        "SELECT item_id, session_id, code, code_key FROM resource_search_items"
    ).fetchall():
        normalized = normalize_catalog_code(row["code"], max_length=32)
        if (
            normalized is None
            or normalized[0] != str(row["code"])
            or normalized[1] != str(row["code_key"])
            or make_item_id(str(row["session_id"]), normalized[1]) != str(row["item_id"])
        ):
            raise ResourceSearchError("resource search item identity is invalid")
    for row in connection.execute(
        "SELECT code, code_key FROM resource_search_pending"
    ).fetchall():
        normalized = normalize_catalog_code(row["code"], max_length=32)
        if (
            normalized is None
            or normalized[0] != str(row["code"])
            or normalized[1] != str(row["code_key"])
        ):
            raise ResourceSearchError(
                "resource search pending item identity is invalid"
            )
