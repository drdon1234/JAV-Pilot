"""SQLite schema creation, migrations and verification for the media library."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

from ..core.catalog_code import normalize_catalog_code
from ..core.migrations import SQLiteMigration, migrate_sqlite, require_columns
from ..web_download.variant import WEB_DOWNLOAD_VARIANTS
from .errors import MediaLibraryError
from .filesystem import make_entry_id
from .models import CURRENT_SCHEMA_VERSION, SCHEMA_COMPONENT

__all__ = [
    "initialize_media_library_schema",
]


def initialize_media_library_schema(
    database_path: Path | str,
    *,
    clock: Callable[[], float] = time.time,
) -> None:
    path = Path(database_path)
    if not path.is_absolute():
        raise MediaLibraryError("media library database path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(path, timeout=30.0, isolation_level=None)
    ) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        migrate_sqlite(
            connection,
            component=SCHEMA_COMPONENT,
            current_version=CURRENT_SCHEMA_VERSION,
            migrations=(
                SQLiteMigration(1, _create_schema, _verify_schema_v1),
                SQLiteMigration(
                    2,
                    _migrate_media_library_v2,
                    _verify_history_facts_schema,
                ),
                SQLiteMigration(
                    3,
                    _migrate_media_library_v3,
                    _verify_history_cleanup_schema_v3,
                ),
                SQLiteMigration(
                    4,
                    _migrate_media_library_v4,
                    _verify_history_cleanup_schema,
                ),
                SQLiteMigration(
                    5,
                    _migrate_media_library_v5,
                    _verify_quality_cache_schema,
                ),
                SQLiteMigration(
                    6,
                    _migrate_media_library_v6,
                    _verify_variant_schema,
                ),
                SQLiteMigration(
                    7,
                    _migrate_media_library_copy_on_write,
                    _verify_copy_on_write_schema,
                ),
                SQLiteMigration(
                    8,
                    _migrate_media_library_v8,
                    _verify_schema,
                ),
            ),
            clock=clock,
            verify_current=_verify_schema,
        )


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE media_library_roots (
            root_key TEXT PRIMARY KEY CHECK (length(root_key) = 64),
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('available', 'unknown')),
            published_generation_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            last_success_at REAL,
            last_full_scan_at REAL,
            updated_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE media_library_generations (
            generation_id TEXT PRIMARY KEY CHECK (length(generation_id) = 32),
            root_key TEXT NOT NULL REFERENCES media_library_roots(root_key),
            base_generation_id TEXT,
            scan_kind TEXT NOT NULL CHECK (scan_kind IN ('full', 'incremental')),
            status TEXT NOT NULL CHECK (status IN ('building', 'published', 'failed')),
            root_device TEXT NOT NULL,
            root_inode TEXT NOT NULL,
            started_at REAL NOT NULL,
            completed_at REAL,
            error_code TEXT
        )
        """,
        "CREATE UNIQUE INDEX media_library_one_building ON media_library_generations(root_key) WHERE status = 'building'",
        "CREATE INDEX media_library_generations_root ON media_library_generations(root_key, started_at DESC)",
        """
        CREATE TABLE media_library_directories (
            generation_id TEXT NOT NULL REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            modified_ns INTEGER NOT NULL,
            changed_ns INTEGER NOT NULL,
            PRIMARY KEY (generation_id, relative_path)
        )
        """,
        """
        CREATE TABLE media_library_files (
            generation_id TEXT NOT NULL REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            parent_path TEXT NOT NULL,
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            scope_path TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            source TEXT NOT NULL CHECK (source IN ('path', 'nfo')),
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size > 0),
            modified_ns INTEGER NOT NULL,
            suffix TEXT NOT NULL,
            part_key TEXT,
            quality_height INTEGER,
            nfo_status TEXT NOT NULL CHECK (nfo_status IN ('present', 'missing', 'invalid', 'unknown')),
            nfo_path TEXT,
            nfo_json TEXT,
            portrait_status TEXT NOT NULL CHECK (portrait_status IN ('present', 'missing', 'invalid', 'unknown')),
            landscape_status TEXT NOT NULL CHECK (landscape_status IN ('present', 'missing', 'invalid', 'unknown')),
            PRIMARY KEY (generation_id, relative_path)
        )
        """,
        "CREATE INDEX media_library_files_entry ON media_library_files(generation_id, entry_id)",
        "CREATE INDEX media_library_files_parent ON media_library_files(generation_id, parent_path)",
        """
        CREATE TABLE media_library_entries (
            generation_id TEXT NOT NULL REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            scope_path TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            title TEXT,
            release_date TEXT,
            source TEXT NOT NULL CHECK (source IN ('path', 'nfo')),
            presence TEXT NOT NULL CHECK (presence IN ('present', 'missing')),
            primary_media_path TEXT NOT NULL,
            nfo_status TEXT NOT NULL CHECK (nfo_status IN ('present', 'missing', 'invalid', 'unknown')),
            nfo_path TEXT,
            portrait_status TEXT NOT NULL CHECK (portrait_status IN ('present', 'missing', 'invalid', 'unknown')),
            landscape_status TEXT NOT NULL CHECK (landscape_status IN ('present', 'missing', 'invalid', 'unknown')),
            quality_height INTEGER,
            duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
            updated_at REAL NOT NULL,
            PRIMARY KEY (generation_id, entry_id)
        )
        """,
        "CREATE INDEX media_library_entries_code ON media_library_entries(generation_id, code_key, scope_path)",
        "CREATE INDEX media_library_entries_presence ON media_library_entries(generation_id, presence, code_key)",
        "CREATE INDEX media_library_entries_quality ON media_library_entries(generation_id, quality_height)",
        "CREATE INDEX media_library_entries_completeness ON media_library_entries(generation_id, nfo_status, portrait_status, landscape_status)",
        """
        CREATE TABLE media_library_terms (
            generation_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('actor', 'maker', 'publisher', 'tag', 'series', 'director')),
            value TEXT NOT NULL,
            value_key TEXT NOT NULL,
            PRIMARY KEY (generation_id, entry_id, kind, value_key),
            FOREIGN KEY (generation_id, entry_id)
                REFERENCES media_library_entries(generation_id, entry_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX media_library_terms_filter ON media_library_terms(kind, value_key, generation_id, entry_id)",
    )
    for statement in statements:
        connection.execute(statement)


def _migrate_media_library_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE media_library_history_facts (
            fact_id TEXT PRIMARY KEY CHECK (length(fact_id) = 64),
            source_type TEXT NOT NULL CHECK (
                source_type IN ('web', 'batch', 'metadata')
            ),
            source_id TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            relative_media_path TEXT,
            quality_height INTEGER CHECK (
                quality_height IS NULL OR
                (quality_height >= 144 AND quality_height <= 4320)
            ),
            nfo_provenance_json TEXT,
            replaces_source_id TEXT,
            superseded_by_source_id TEXT,
            publication_outcome TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            source_created_at REAL NOT NULL,
            archived_at REAL NOT NULL,
            fact_digest TEXT NOT NULL CHECK (length(fact_digest) = 64),
            UNIQUE (source_type, source_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX media_library_history_facts_code "
        "ON media_library_history_facts(code_key, source_type, archived_at DESC)"
    )
    connection.execute(
        "CREATE INDEX media_library_history_facts_source "
        "ON media_library_history_facts(source_type, source_created_at DESC)"
    )


def _migrate_media_library_v3(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE media_library_history_cleanup_operations (
            operation_id TEXT PRIMARY KEY CHECK (length(operation_id) = 64),
            revision INTEGER NOT NULL CHECK (revision = 1),
            status TEXT NOT NULL CHECK (status IN ('prepared', 'completed')),
            operation_digest TEXT NOT NULL CHECK (length(operation_digest) = 64),
            candidate_count INTEGER NOT NULL CHECK (
                candidate_count >= 0 AND candidate_count <= 10000
            ),
            fact_count INTEGER NOT NULL CHECK (
                fact_count >= 0 AND fact_count <= 100000
            ),
            payload_bytes INTEGER NOT NULL CHECK (
                payload_bytes >= 0 AND payload_bytes <= 33554432
            ),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE media_library_history_cleanup_candidates (
            operation_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (
                position >= 0 AND position < 10000
            ),
            task_type TEXT NOT NULL CHECK (
                task_type IN ('web', 'batch', 'metadata')
            ),
            source_id TEXT NOT NULL CHECK (
                length(source_id) >= 1 AND length(source_id) <= 256
            ),
            fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
            status TEXT NOT NULL CHECK (
                length(status) >= 1 AND length(status) <= 32
            ),
            record_count INTEGER NOT NULL CHECK (
                record_count >= 1 AND record_count <= 10000
            ),
            PRIMARY KEY (operation_id, position),
            UNIQUE (operation_id, task_type, source_id),
            FOREIGN KEY (operation_id)
                REFERENCES media_library_history_cleanup_operations(operation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE media_library_history_cleanup_facts (
            operation_id TEXT NOT NULL,
            candidate_position INTEGER NOT NULL,
            fact_id TEXT NOT NULL CHECK (length(fact_id) = 64),
            fact_digest TEXT NOT NULL CHECK (length(fact_digest) = 64),
            created_by_operation INTEGER NOT NULL CHECK (
                created_by_operation IN (0, 1)
            ),
            PRIMARY KEY (operation_id, fact_id),
            FOREIGN KEY (operation_id, candidate_position)
                REFERENCES media_library_history_cleanup_candidates(
                    operation_id, position
                ) ON DELETE CASCADE,
            FOREIGN KEY (fact_id)
                REFERENCES media_library_history_facts(fact_id)
                ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX media_library_history_cleanup_one_prepared "
        "ON media_library_history_cleanup_operations(status) "
        "WHERE status = 'prepared'"
    )
    connection.execute(
        "CREATE INDEX media_library_history_cleanup_completed "
        "ON media_library_history_cleanup_operations(status, updated_at DESC)"
    )


def _migrate_media_library_v4(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE media_library_history_cleanup_operations "
        "ADD COLUMN payload_digest TEXT CHECK ("
        "payload_digest IS NULL OR length(payload_digest) = 64)"
    )


def _migrate_media_library_v5(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE media_library_files ADD COLUMN changed_ns INTEGER")
    connection.execute(
        "ALTER TABLE media_library_files ADD COLUMN quality_source TEXT "
        "CHECK (quality_source IS NULL OR quality_source IN ('probe', 'filename'))"
    )
    connection.execute(
        "UPDATE media_library_files SET quality_source = 'filename' "
        "WHERE quality_height IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX media_library_files_quality_cache ON media_library_files("
        "generation_id, device, inode, size, modified_ns, changed_ns, quality_source)"
    )
    connection.execute("UPDATE media_library_roots SET last_full_scan_at = NULL")


def _migrate_media_library_v6(connection: sqlite3.Connection) -> None:
    variants = ", ".join(f"'{value}'" for value in WEB_DOWNLOAD_VARIANTS)
    connection.execute(
        "ALTER TABLE media_library_files ADD COLUMN variant TEXT "
        f"CHECK (variant IS NULL OR variant IN ({variants}))"
    )
    connection.execute(
        "ALTER TABLE media_library_entries ADD COLUMN variant TEXT "
        f"CHECK (variant IS NULL OR variant IN ({variants}))"
    )
    connection.execute(
        "CREATE INDEX media_library_entries_variant "
        "ON media_library_entries(generation_id, code_key, variant, scope_path)"
    )
    connection.execute("UPDATE media_library_roots SET last_full_scan_at = NULL")


def _migrate_media_library_copy_on_write(connection: sqlite3.Connection) -> None:
    """Replace full generation snapshots with temporal published state and deltas.

    The legacy tables contain complete copies for every retained published
    generation.  Copies belonging to a root with a published current view are
    retained as immutable temporal versions during migration, while only that
    root's published generation remains live.  Detached, failed, and interrupted
    snapshots were never user-visible and are safely discarded; their generation
    audit rows remain intact.
    """

    invalid_root = connection.execute(
        """
        SELECT 1
        FROM media_library_roots roots
        LEFT JOIN media_library_generations generations
          ON generations.generation_id = roots.published_generation_id
         AND generations.root_key = roots.root_key
         AND generations.status = 'published'
        WHERE roots.published_generation_id IS NOT NULL
          AND generations.generation_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if invalid_root is not None:
        raise MediaLibraryError("media library published generation is inconsistent")

    for table in (
        "media_library_terms",
        "media_library_entries",
        "media_library_files",
        "media_library_directories",
    ):
        connection.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
    for index in (
        "media_library_terms_filter",
        "media_library_entries_code",
        "media_library_entries_presence",
        "media_library_entries_quality",
        "media_library_entries_completeness",
        "media_library_entries_variant",
        "media_library_files_entry",
        "media_library_files_parent",
        "media_library_files_quality_cache",
    ):
        connection.execute(f"DROP INDEX {index}")

    _create_copy_on_write_schema(connection)

    retired_generation = (
        "CASE WHEN legacy.generation_id = roots.published_generation_id "
        "THEN NULL ELSE roots.published_generation_id END"
    )
    connection.execute(
        f"""
        INSERT INTO media_library_directories (
            root_key, relative_path, created_generation_id,
            retired_generation_id, device, inode, modified_ns, changed_ns
        )
        SELECT generations.root_key, legacy.relative_path,
               legacy.generation_id, {retired_generation},
               legacy.device, legacy.inode, legacy.modified_ns,
               legacy.changed_ns
        FROM legacy_media_library_directories legacy
        JOIN media_library_generations generations
          ON generations.generation_id = legacy.generation_id
         AND generations.status = 'published'
        JOIN media_library_roots roots
          ON roots.root_key = generations.root_key
        WHERE roots.published_generation_id IS NOT NULL
        """
    )
    connection.execute(
        f"""
        INSERT INTO media_library_files (
            root_key, relative_path, created_generation_id,
            retired_generation_id, parent_path, entry_id, scope_path,
            code, code_key, variant, source, device, inode, size, modified_ns,
            suffix, changed_ns, part_key, quality_height, quality_source,
            nfo_status, nfo_path, nfo_json, portrait_status, landscape_status
        )
        SELECT generations.root_key, legacy.relative_path,
               legacy.generation_id, {retired_generation},
               legacy.parent_path, legacy.entry_id, legacy.scope_path,
               legacy.code, legacy.code_key, legacy.variant, legacy.source,
               legacy.device, legacy.inode, legacy.size, legacy.modified_ns,
               legacy.suffix, legacy.changed_ns, legacy.part_key,
               legacy.quality_height, legacy.quality_source,
               legacy.nfo_status, legacy.nfo_path, legacy.nfo_json,
               legacy.portrait_status, legacy.landscape_status
        FROM legacy_media_library_files legacy
        JOIN media_library_generations generations
          ON generations.generation_id = legacy.generation_id
         AND generations.status = 'published'
        JOIN media_library_roots roots
          ON roots.root_key = generations.root_key
        WHERE roots.published_generation_id IS NOT NULL
        """
    )
    connection.execute(
        f"""
        INSERT INTO media_library_entries (
            root_key, entry_id, created_generation_id, retired_generation_id,
            scope_path, code, code_key, variant, title, release_date, source,
            presence, primary_media_path, nfo_status, nfo_path,
            portrait_status, landscape_status, quality_height,
            duplicate_count, updated_at
        )
        SELECT generations.root_key, legacy.entry_id,
               legacy.generation_id, {retired_generation},
               legacy.scope_path, legacy.code, legacy.code_key,
               legacy.variant, legacy.title, legacy.release_date,
               legacy.source, legacy.presence, legacy.primary_media_path,
               legacy.nfo_status, legacy.nfo_path, legacy.portrait_status,
               legacy.landscape_status, legacy.quality_height,
               legacy.duplicate_count, legacy.updated_at
        FROM legacy_media_library_entries legacy
        JOIN media_library_generations generations
          ON generations.generation_id = legacy.generation_id
         AND generations.status = 'published'
        JOIN media_library_roots roots
          ON roots.root_key = generations.root_key
        WHERE roots.published_generation_id IS NOT NULL
        """
    )
    connection.execute(
        """
        INSERT INTO media_library_terms (
            root_key, entry_id, entry_generation_id, kind, value, value_key
        )
        SELECT generations.root_key, legacy.entry_id, legacy.generation_id,
               legacy.kind, legacy.value, legacy.value_key
        FROM legacy_media_library_terms legacy
        JOIN media_library_generations generations
          ON generations.generation_id = legacy.generation_id
         AND generations.status = 'published'
        JOIN media_library_roots roots
          ON roots.root_key = generations.root_key
        JOIN media_library_entries entries
          ON entries.root_key = generations.root_key
         AND entries.entry_id = legacy.entry_id
         AND entries.created_generation_id = legacy.generation_id
        WHERE roots.published_generation_id IS NOT NULL
        """
    )

    connection.execute("DROP TABLE legacy_media_library_terms")
    connection.execute("DROP TABLE legacy_media_library_entries")
    connection.execute("DROP TABLE legacy_media_library_files")
    connection.execute("DROP TABLE legacy_media_library_directories")


def _migrate_media_library_v8(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA defer_foreign_keys = ON")
    changed_roots: set[str] = set()
    base_entry_ids = _migrate_published_catalog_rows(
        connection,
        changed_roots=changed_roots,
    )
    _migrate_workspace_catalog_rows(
        connection,
        base_entry_ids,
        changed_roots=changed_roots,
    )

    fact_updates: list[tuple[str, str, str, str, str]] = []
    changed_fact_ids: list[str] = []
    for row in connection.execute(
        "SELECT * FROM media_library_history_facts ORDER BY fact_id"
    ).fetchall():
        fact = _migration_history_fact_mapping(row)
        actual_digest = _migration_history_fact_digest(fact)
        if actual_digest != str(row["fact_digest"]):
            raise MediaLibraryError("media library history fact digest is invalid")
        expected_code, expected_key = _stored_catalog_identity(
            row["code"], row["code_key"]
        )
        if expected_code == row["code"] and expected_key == row["code_key"]:
            continue
        fact["code"] = expected_code
        fact["code_key"] = expected_key
        fact_digest = _migration_history_fact_digest(fact)
        fact_id = str(row["fact_id"])
        fact_updates.append(
            (str(expected_code or ""), str(expected_key or ""), fact_digest, fact_id, str(row["fact_digest"]))
        )
        changed_fact_ids.append(fact_id)

    if changed_fact_ids:
        slots = ", ".join("?" for _ in changed_fact_ids)
        operation_ids = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT operation_id "
                "FROM media_library_history_cleanup_facts "
                f"WHERE fact_id IN ({slots}) ORDER BY operation_id",
                tuple(changed_fact_ids),
            ).fetchall()
        )
        # Validate every affected cleanup ledger before changing a fact or its
        # mapping.  A migration must never re-sign an already-corrupt record.
        for operation_id in operation_ids:
            _validate_history_cleanup_operation(connection, operation_id)
        for code, code_key, fact_digest, fact_id, _old_digest in fact_updates:
            connection.execute(
                "UPDATE media_library_history_facts "
                "SET code = ?, code_key = ?, fact_digest = ? WHERE fact_id = ?",
                (code or None, code_key or None, fact_digest, fact_id),
            )
            connection.execute(
                "UPDATE media_library_history_cleanup_facts SET fact_digest = ? "
                "WHERE fact_id = ?",
                (fact_digest, fact_id),
            )
        for operation_id in operation_ids:
            _refresh_history_cleanup_operation(connection, operation_id)

    _recompute_migration_duplicate_counts(connection, changed_roots)
    for root_key in sorted(changed_roots):
        connection.execute(
            "UPDATE media_library_roots SET revision = revision + 1 "
            "WHERE root_key = ?",
            (root_key,),
        )


def _stored_catalog_identity(
    code: object,
    stored_key: object,
) -> tuple[str | None, str | None]:
    if code is None:
        if stored_key is not None:
            raise MediaLibraryError("media library catalog identity is invalid")
        return None, None
    if not isinstance(code, str) or not isinstance(stored_key, str):
        raise MediaLibraryError("media library catalog identity is invalid")
    raw = unicodedata.normalize("NFKC", code).strip().upper()
    if not raw or not raw.isascii():
        raise MediaLibraryError("media library catalog identity is invalid")
    legacy_key = "".join(character for character in raw if character.isalnum())
    fc2_candidate = re.fullmatch(
        r"FC2(?:[-_. ]?(?:PPV[-_. ]?)?)?\d{2,9}", raw,
        flags=re.IGNORECASE,
    )
    if fc2_candidate is None:
        if not stored_key or stored_key != legacy_key:
            raise MediaLibraryError("media library catalog identity is invalid")
        return code, stored_key
    normalized = normalize_catalog_code(raw, max_length=40)
    if normalized is None or not normalized[1].startswith("FC2PPV"):
        raise MediaLibraryError("media library catalog code is invalid")
    if stored_key not in {legacy_key, normalized[1]}:
        raise MediaLibraryError("media library catalog identity is invalid")
    return normalized


def _migrate_nfo_json(
    value: object,
    *,
    old_code: object,
    old_key: object,
    new_code: str | None,
    new_key: str | None,
) -> str | None:
    if value is None or new_code is None or new_key is None:
        return None if value is None else str(value)
    if not isinstance(old_code, str) or not re.fullmatch(
        r"FC2(?:[-_. ]?(?:PPV[-_. ]?)?)?\d{2,9}",
        old_code,
        flags=re.IGNORECASE,
    ):
        return str(value)
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaLibraryError("media library NFO metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise MediaLibraryError("media library NFO metadata is invalid")
    nfo_code = payload.get("code")
    nfo_key = payload.get("code_key")
    if nfo_code is None or nfo_key is None:
        raise MediaLibraryError("media library NFO identity is invalid")
    nfo_identity = _stored_catalog_identity(nfo_code, nfo_key)
    old_identity = _stored_catalog_identity(old_code, old_key)
    if nfo_identity[1] != old_identity[1]:
        raise MediaLibraryError("media library NFO identity is inconsistent")
    payload["code"] = new_code
    payload["code_key"] = new_key
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _migrate_published_catalog_rows(
    connection: sqlite3.Connection,
    *,
    changed_roots: set[str] | None = None,
) -> dict[tuple[str, str], str]:
    entry_rows = connection.execute(
        "SELECT root_key, entry_id, created_generation_id, retired_generation_id, "
        "scope_path, code, code_key, variant FROM media_library_entries "
        "ORDER BY root_key, created_generation_id, entry_id"
    ).fetchall()
    entry_targets: dict[tuple[str, str, str], str] = {}
    live_targets: dict[tuple[str, str], tuple[str, str]] = {}
    id_targets: dict[tuple[str, str], str] = {}
    entry_updates: list[tuple[str, str, str, str, str]] = []
    reserved_ids: set[tuple[str, str]] = set()
    for row in entry_rows:
        root_key = str(row["root_key"])
        old_id = str(row["entry_id"])
        generation_id = str(row["created_generation_id"])
        display_code, code_key = _stored_catalog_identity(
            row["code"], row["code_key"]
        )
        target_id = old_id
        if display_code != row["code"] or code_key != row["code_key"]:
            target_id = make_entry_id(
                root_key,
                str(row["scope_path"]),
                code_key,
                row["variant"],
            )
        target_identity = (root_key, target_id, generation_id)
        collided = entry_targets.setdefault(target_identity, old_id)
        if collided != old_id:
            raise MediaLibraryError(
                "media library entries collide after catalog code normalization"
            )
        if row["retired_generation_id"] is None:
            live_identity = (root_key, target_id)
            live_collided = live_targets.setdefault(
                live_identity,
                (old_id, generation_id),
            )
            if live_collided != (old_id, generation_id):
                raise MediaLibraryError(
                    "live media library entries collide after catalog code normalization"
                )
        identity = (root_key, old_id)
        mapped = id_targets.setdefault(identity, target_id)
        if mapped != target_id:
            raise MediaLibraryError("media library entry identity is inconsistent")
        reserved_ids.add(identity)
        reserved_ids.add((root_key, target_id))
        entry_updates.append(
            (root_key, generation_id, old_id, display_code or "", code_key or "")
        )
        if (
            display_code != row["code"] or code_key != row["code_key"]
        ) and changed_roots is not None:
            changed_roots.add(root_key)

    file_rows = connection.execute(
        "SELECT root_key, relative_path, created_generation_id, entry_id, "
        "scope_path, code, code_key, variant, nfo_json FROM media_library_files "
        "ORDER BY root_key, created_generation_id, relative_path"
    ).fetchall()
    file_updates: list[
        tuple[str, str, str, str | None, str | None, str | None]
    ] = []
    for row in file_rows:
        root_key = str(row["root_key"])
        old_id = str(row["entry_id"])
        display_code, code_key = _stored_catalog_identity(
            row["code"], row["code_key"]
        )
        target_id = old_id
        if display_code != row["code"] or code_key != row["code_key"]:
            target_id = make_entry_id(
                root_key,
                str(row["scope_path"]),
                code_key,
                row["variant"],
            )
        identity = (root_key, old_id)
        mapped = id_targets.setdefault(identity, target_id)
        if mapped != target_id:
            raise MediaLibraryError("media library file identity is inconsistent")
        reserved_ids.add(identity)
        reserved_ids.add((root_key, target_id))
        file_updates.append(
            (
                root_key,
                str(row["relative_path"]),
                str(row["created_generation_id"]),
                display_code,
                code_key,
                _migrate_nfo_json(
                    row["nfo_json"],
                    old_code=row["code"],
                    old_key=row["code_key"],
                    new_code=display_code,
                    new_key=code_key,
                ),
            )
        )
        if (
            display_code != row["code"]
            or code_key != row["code_key"]
            or file_updates[-1][5] != row["nfo_json"]
        ) and changed_roots is not None:
            changed_roots.add(root_key)

    changed_ids = {
        identity: target_id
        for identity, target_id in id_targets.items()
        if identity[1] != target_id
    }
    temporary_ids = _temporary_entry_ids(changed_ids, reserved_ids)
    for (root_key, old_id), temporary_id in temporary_ids.items():
        for table in (
            "media_library_terms",
            "media_library_files",
            "media_library_entries",
        ):
            connection.execute(
                f"UPDATE {table} SET entry_id = ? "
                "WHERE root_key = ? AND entry_id = ?",
                (temporary_id, root_key, old_id),
            )
    for identity, temporary_id in temporary_ids.items():
        root_key, _old_id = identity
        target_id = changed_ids[identity]
        for table in (
            "media_library_terms",
            "media_library_files",
            "media_library_entries",
        ):
            connection.execute(
                f"UPDATE {table} SET entry_id = ? "
                "WHERE root_key = ? AND entry_id = ?",
                (target_id, root_key, temporary_id),
            )

    for root_key, generation_id, old_id, display_code, code_key in entry_updates:
        target_id = id_targets[(root_key, old_id)]
        connection.execute(
            "UPDATE media_library_entries SET code = ?, code_key = ? "
            "WHERE root_key = ? AND created_generation_id = ? AND entry_id = ?",
            (
                display_code or None,
                code_key or None,
                root_key,
                generation_id,
                target_id,
            ),
        )
    for (
        root_key,
        relative_path,
        generation_id,
        display_code,
        code_key,
        nfo_json,
    ) in file_updates:
        connection.execute(
            "UPDATE media_library_files SET code = ?, code_key = ?, nfo_json = ? "
            "WHERE root_key = ? AND relative_path = ? "
            "AND created_generation_id = ?",
            (
                display_code,
                code_key,
                nfo_json,
                root_key,
                relative_path,
                generation_id,
            ),
        )
    return id_targets


def _migrate_workspace_catalog_rows(
    connection: sqlite3.Connection,
    published_ids: Mapping[tuple[str, str], str],
    *,
    changed_roots: set[str] | None = None,
) -> None:
    generation_roots = {
        str(row["generation_id"]): str(row["root_key"])
        for row in connection.execute(
            "SELECT generation_id, root_key FROM media_library_generations"
        ).fetchall()
    }
    entry_rows = connection.execute(
        "SELECT generation_id, entry_id, scope_path, code, code_key, variant "
        "FROM media_library_workspace_entries ORDER BY generation_id, entry_id"
    ).fetchall()
    target_rows: dict[tuple[str, str], str] = {}
    id_targets: dict[tuple[str, str], str] = {}
    entry_updates: list[tuple[str, str, str | None, str | None]] = []
    reserved_ids: set[tuple[str, str]] = set()
    for row in entry_rows:
        generation_id = str(row["generation_id"])
        root_key = generation_roots[generation_id]
        old_id = str(row["entry_id"])
        display_code, code_key = _stored_catalog_identity(
            row["code"], row["code_key"]
        )
        target_id = old_id
        if display_code != row["code"] or code_key != row["code_key"]:
            target_id = make_entry_id(
                root_key,
                str(row["scope_path"]),
                code_key,
                row["variant"],
            )
        target_identity = (generation_id, target_id)
        collided = target_rows.setdefault(target_identity, old_id)
        if collided != old_id:
            raise MediaLibraryError(
                "media library workspace entries collide after catalog code normalization"
            )
        identity = (generation_id, old_id)
        mapped = id_targets.setdefault(identity, target_id)
        if mapped != target_id:
            raise MediaLibraryError("media library workspace identity is inconsistent")
        reserved_ids.add(identity)
        reserved_ids.add((generation_id, target_id))
        entry_updates.append((generation_id, old_id, display_code, code_key))
        if (
            display_code != row["code"] or code_key != row["code_key"]
        ) and changed_roots is not None:
            changed_roots.add(root_key)

    file_rows = connection.execute(
        "SELECT generation_id, relative_path, entry_id, scope_path, code, "
        "code_key, variant, nfo_json FROM media_library_workspace_files "
        "ORDER BY generation_id, relative_path"
    ).fetchall()
    file_updates: list[
        tuple[str, str, str | None, str | None, str | None]
    ] = []
    for row in file_rows:
        generation_id = str(row["generation_id"])
        root_key = generation_roots[generation_id]
        old_id = str(row["entry_id"])
        display_code, code_key = _stored_catalog_identity(
            row["code"], row["code_key"]
        )
        target_id = old_id
        if display_code != row["code"] or code_key != row["code_key"]:
            target_id = make_entry_id(
                root_key,
                str(row["scope_path"]),
                code_key,
                row["variant"],
            )
        identity = (generation_id, old_id)
        mapped = id_targets.setdefault(identity, target_id)
        if mapped != target_id:
            raise MediaLibraryError(
                "media library workspace file identity is inconsistent"
            )
        reserved_ids.add(identity)
        reserved_ids.add((generation_id, target_id))
        file_updates.append(
            (
                generation_id,
                str(row["relative_path"]),
                display_code,
                code_key,
                _migrate_nfo_json(
                    row["nfo_json"],
                    old_code=row["code"],
                    old_key=row["code_key"],
                    new_code=display_code,
                    new_key=code_key,
                ),
            )
        )
        if (
            display_code != row["code"]
            or code_key != row["code_key"]
            or file_updates[-1][4] != row["nfo_json"]
        ) and changed_roots is not None:
            changed_roots.add(root_key)

    deletion_targets: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        "SELECT generation_id, entry_id "
        "FROM media_library_workspace_entry_deletions "
        "ORDER BY generation_id, entry_id"
    ).fetchall():
        generation_id = str(row["generation_id"])
        old_id = str(row["entry_id"])
        root_key = generation_roots[generation_id]
        target_id = id_targets.get(
            (generation_id, old_id),
            published_ids.get((root_key, old_id), old_id),
        )
        identity = (generation_id, old_id)
        mapped = id_targets.setdefault(identity, target_id)
        if mapped != target_id:
            raise MediaLibraryError(
                "media library workspace deletion identity is inconsistent"
            )
        collided = deletion_targets.setdefault((generation_id, target_id), old_id)
        if collided != old_id:
            raise MediaLibraryError(
                "media library workspace deletions collide after normalization"
            )
        reserved_ids.add(identity)
        reserved_ids.add((generation_id, target_id))

    changed_ids = {
        identity: target_id
        for identity, target_id in id_targets.items()
        if identity[1] != target_id
    }
    temporary_ids = _temporary_entry_ids(changed_ids, reserved_ids)
    for (generation_id, old_id), temporary_id in temporary_ids.items():
        for table in (
            "media_library_workspace_terms",
            "media_library_workspace_entry_deletions",
            "media_library_workspace_files",
            "media_library_workspace_entries",
        ):
            connection.execute(
                f"UPDATE {table} SET entry_id = ? "
                "WHERE generation_id = ? AND entry_id = ?",
                (temporary_id, generation_id, old_id),
            )
    for identity, temporary_id in temporary_ids.items():
        generation_id, _old_id = identity
        target_id = changed_ids[identity]
        for table in (
            "media_library_workspace_terms",
            "media_library_workspace_entry_deletions",
            "media_library_workspace_files",
            "media_library_workspace_entries",
        ):
            connection.execute(
                f"UPDATE {table} SET entry_id = ? "
                "WHERE generation_id = ? AND entry_id = ?",
                (target_id, generation_id, temporary_id),
            )
    for generation_id, old_id, display_code, code_key in entry_updates:
        connection.execute(
            "UPDATE media_library_workspace_entries SET code = ?, code_key = ? "
            "WHERE generation_id = ? AND entry_id = ?",
            (
                display_code,
                code_key,
                generation_id,
                id_targets[(generation_id, old_id)],
            ),
        )
    for generation_id, relative_path, display_code, code_key, nfo_json in file_updates:
        connection.execute(
            "UPDATE media_library_workspace_files SET code = ?, code_key = ?, nfo_json = ? "
            "WHERE generation_id = ? AND relative_path = ?",
            (display_code, code_key, nfo_json, generation_id, relative_path),
        )


def _temporary_entry_ids(
    targets: Mapping[tuple[str, str], str],
    reserved: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    temporary: dict[tuple[str, str], str] = {}
    occupied = set(reserved)
    for namespace, old_id in sorted(targets):
        for salt in range(1024):
            candidate = hashlib.sha1(
                f"catalog-migration\0{namespace}\0{old_id}\0{salt}".encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()
            if (namespace, candidate) not in occupied:
                occupied.add((namespace, candidate))
                temporary[(namespace, old_id)] = candidate
                break
        else:
            raise MediaLibraryError("media library entry migration identity collided")
    return temporary


_HISTORY_FACT_COLUMNS = (
    "fact_id",
    "source_type",
    "source_id",
    "code",
    "code_key",
    "relative_media_path",
    "quality_height",
    "nfo_provenance_json",
    "replaces_source_id",
    "superseded_by_source_id",
    "publication_outcome",
    "details_json",
    "source_created_at",
    "archived_at",
    "fact_digest",
)


def _migration_digest(value: object) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _migration_history_fact_mapping(row: Mapping[str, object]) -> dict[str, object]:
    return {column: row[column] for column in _HISTORY_FACT_COLUMNS}


def _migration_history_fact_digest(fact: Mapping[str, object]) -> str:
    try:
        provenance = json.loads(str(fact["nfo_provenance_json"] or "{}"))
        details = json.loads(str(fact["details_json"] or "{}"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaLibraryError("media library history fact is invalid") from exc
    if not isinstance(provenance, dict) or not isinstance(details, dict):
        raise MediaLibraryError("media library history fact is invalid")
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in provenance.items()
    ):
        raise MediaLibraryError("media library history fact is invalid")
    _migration_assert_history_safe(provenance)
    _migration_assert_history_safe(details)
    _migration_assert_history_safe(fact.get("code"))
    _migration_assert_history_safe(fact.get("code_key"))
    _migration_assert_history_safe(fact.get("relative_media_path"))
    _migration_assert_history_safe(fact.get("source_type"))
    _migration_assert_history_safe(fact.get("source_id"))
    _migration_assert_history_safe(fact.get("replaces_source_id"))
    _migration_assert_history_safe(fact.get("superseded_by_source_id"))
    _migration_assert_history_safe(fact.get("publication_outcome"))
    try:
        source_created_at = float(fact["source_created_at"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError("media library history fact is invalid") from exc
    if not math.isfinite(source_created_at) or source_created_at < 0:
        raise MediaLibraryError("media library history fact is invalid")
    try:
        quality_height = (
            None
            if fact["quality_height"] is None
            else int(fact["quality_height"])
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError("media library history fact is invalid") from exc
    if quality_height is not None and not 144 <= quality_height <= 4320:
        raise MediaLibraryError("media library history fact is invalid")
    payload = {
        "source_type": str(fact["source_type"]),
        "source_id": str(fact["source_id"]),
        "code": None if fact["code"] is None else str(fact["code"]),
        "code_key": None if fact["code_key"] is None else str(fact["code_key"]),
        "relative_media_path": (
            None
            if fact["relative_media_path"] is None
            else str(fact["relative_media_path"])
        ),
        "quality_height": quality_height,
        "nfo_provenance": [
            {"name": name, "provenance": value}
            for name, value in sorted(provenance.items())
        ],
        "replaces_source_id": (
            None
            if fact["replaces_source_id"] is None
            else str(fact["replaces_source_id"])
        ),
        "superseded_by_source_id": (
            None
            if fact["superseded_by_source_id"] is None
            else str(fact["superseded_by_source_id"])
        ),
        "publication_outcome": (
            None
            if fact["publication_outcome"] is None
            else str(fact["publication_outcome"])
        ),
        "details": details,
        "source_created_at": source_created_at,
    }
    _migration_assert_history_safe(payload)
    return _migration_digest(payload)


def _migration_assert_history_safe(value: object, *, key: str | None = None) -> None:
    if key is not None and re.search(
        r"(?:authorization|cookies?|headers?|manifest(?:_url)?|password|"
        r"referer|secrets?|sessions?|tokens?|urls?)",
        key,
        flags=re.IGNORECASE,
    ):
        raise MediaLibraryError("media library history fact is unsafe")
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise MediaLibraryError("media library history fact is invalid")
            _migration_assert_history_safe(item, key=raw_key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _migration_assert_history_safe(item)
    elif isinstance(value, str) and re.search(
        r"(?:https?|wss?|ftp)://|://|www\.|"
        r"\b(?:authorization|cookies?|headers?|manifest(?:_url)?|password|"
        r"referer|secret|session|token|url)\s*[:=]|"
        r"[?&](?:authorization|cookie|password|secret|session|token)\s*=",
        value,
        flags=re.IGNORECASE,
    ):
        raise MediaLibraryError("media library history fact is unsafe")
    elif isinstance(value, float) and not math.isfinite(value):
        raise MediaLibraryError("media library history fact is invalid")


def _migration_archived_at(value: object) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError("media library history archive timestamp is invalid") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise MediaLibraryError("media library history archive timestamp is invalid")
    return timestamp


def _migration_int(value: object, message: str) -> int:
    """Parse a persisted integer without leaking Python conversion errors."""
    if isinstance(value, bool):
        raise MediaLibraryError(message)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError(message) from exc
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise MediaLibraryError(message)
    return parsed


def _validate_history_cleanup_operation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> None:
    operation = connection.execute(
        "SELECT * FROM media_library_history_cleanup_operations "
        "WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if operation is None:
        raise MediaLibraryError("media library history cleanup is invalid")
    revision = _migration_int(
        operation["revision"], "media library history cleanup is invalid"
    )
    candidate_count = _migration_int(
        operation["candidate_count"], "media library history cleanup is invalid"
    )
    fact_count = _migration_int(
        operation["fact_count"], "media library history cleanup is invalid"
    )
    payload_bytes = _migration_int(
        operation["payload_bytes"], "media library history cleanup is invalid"
    )
    if (
        revision != 1
        or str(operation["status"]) not in {"prepared", "completed"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(operation["operation_digest"]))
    ):
        raise MediaLibraryError("media library history cleanup is invalid")
    candidate_rows = connection.execute(
        "SELECT * FROM media_library_history_cleanup_candidates "
        "WHERE operation_id = ? ORDER BY position",
        (operation_id,),
    ).fetchall()
    if (
        len(candidate_rows) != candidate_count
        or len(candidate_rows) > 10_000
        or tuple(
            _migration_int(
                row["position"], "media library history cleanup is invalid"
            )
            for row in candidate_rows
        )
        != tuple(range(len(candidate_rows)))
    ):
        raise MediaLibraryError("media library history cleanup is invalid")
    candidates: list[dict[str, object]] = []
    for row in candidate_rows:
        position = _migration_int(
            row["position"], "media library history cleanup is invalid"
        )
        record_count = _migration_int(
            row["record_count"], "media library history cleanup is invalid"
        )
        if (
            str(row["task_type"]) not in {"web", "batch", "metadata"}
            or not str(row["source_id"])
            or len(str(row["source_id"])) > 256
            or not re.fullmatch(r"[0-9a-f]{64}", str(row["fingerprint"]))
            or not str(row["status"])
            or len(str(row["status"])) > 32
            or not 1 <= record_count <= 10_000
        ):
            raise MediaLibraryError("media library history cleanup is invalid")
        candidates.append(
            {
                "position": position,
                "task_type": str(row["task_type"]),
                "source_id": str(row["source_id"]),
                "fingerprint": str(row["fingerprint"]),
                "status": str(row["status"]),
                "record_count": record_count,
            }
        )
    fact_rows = connection.execute(
        "SELECT mapping.candidate_position, mapping.fact_id, "
        "mapping.fact_digest AS expected_digest, facts.* "
        "FROM media_library_history_cleanup_facts mapping "
        "LEFT JOIN media_library_history_facts facts "
        "ON facts.fact_id = mapping.fact_id "
        "WHERE mapping.operation_id = ? "
        "ORDER BY mapping.candidate_position, mapping.fact_id",
        (operation_id,),
    ).fetchall()
    if len(fact_rows) != fact_count or len(fact_rows) > 100_000:
        raise MediaLibraryError("media library history cleanup is invalid")
    fact_digests: list[tuple[int, str, str]] = []
    fact_summaries: list[dict[str, object]] = []
    mapped_facts: list[dict[str, object]] = []
    for row in fact_rows:
        position = _migration_int(
            row["candidate_position"],
            "media library history cleanup mapping is invalid",
        )
        fact_id = str(row["fact_id"] or "")
        expected = str(row["expected_digest"] or "")
        if not 0 <= position < len(candidates) or not fact_id:
            raise MediaLibraryError("media library history cleanup mapping is invalid")
        fact = _migration_history_fact_mapping(row)
        actual = _migration_history_fact_digest(fact)
        if str(fact["fact_digest"]) != expected or actual != expected:
            raise MediaLibraryError("media library history cleanup fact digest is invalid")
        archived_at = _migration_archived_at(fact["archived_at"])
        fact_digests.append((position, fact_id, expected))
        fact_summaries.append(
            {
                "candidate_position": position,
                "fact_id": fact_id,
                "fact_digest": expected,
                "archived_at": archived_at,
            }
        )
        mapped_facts.append({"candidate_position": position, **fact})
    payload = {
        "revision": 1,
        "candidates": candidates,
        "facts": mapped_facts,
    }
    payload_body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload_body) != payload_bytes or len(payload_body) > 32 * 1024 * 1024:
        raise MediaLibraryError("media library history cleanup payload is invalid")
    stored_payload_digest = operation["payload_digest"]
    if stored_payload_digest is None:
        operation_payload = {
            "revision": 1,
            "candidates": candidates,
            "facts": [
                {
                    "candidate_position": item["candidate_position"],
                    "fact_id": item["fact_id"],
                    "fact_digest": item["fact_digest"],
                }
                for item in fact_summaries
            ],
        }
        expected_operation = _migration_digest(operation_payload)
    else:
        if not re.fullmatch(r"[0-9a-f]{64}", str(stored_payload_digest)):
            raise MediaLibraryError("media library history cleanup payload digest is invalid")
        payload_digest = hashlib.sha256(payload_body).hexdigest()
        if payload_digest != str(stored_payload_digest):
            raise MediaLibraryError("media library history cleanup payload digest is invalid")
        expected_operation = _migration_digest(
            {
                "revision": 1,
                "payload_digest": payload_digest,
                "candidates": candidates,
                "facts": fact_summaries,
            }
        )
    if expected_operation != str(operation["operation_digest"]):
        raise MediaLibraryError("media library history cleanup operation digest is invalid")


def _recompute_migration_duplicate_counts(
    connection: sqlite3.Connection,
    changed_roots: set[str],
) -> None:
    """Rebuild duplicate counts after FC2 keys change entry identities."""

    for root_key in sorted(changed_roots):
        connection.execute(
            "UPDATE media_library_entries SET duplicate_count = 0 "
            "WHERE root_key = ? AND retired_generation_id IS NULL",
            (root_key,),
        )
        live_groups = connection.execute(
            "SELECT code_key, variant, COUNT(*) AS count_value "
            "FROM media_library_entries WHERE root_key = ? "
            "AND retired_generation_id IS NULL AND presence = 'present' "
            "AND code_key IS NOT NULL GROUP BY code_key, variant "
            "HAVING COUNT(*) > 1",
            (root_key,),
        ).fetchall()
        for group in live_groups:
            connection.execute(
                "UPDATE media_library_entries SET duplicate_count = ? "
                "WHERE root_key = ? AND retired_generation_id IS NULL "
                "AND presence = 'present' AND code_key = ? AND variant IS ?",
                (
                    int(group["count_value"]),
                    root_key,
                    str(group["code_key"]),
                    group["variant"],
                ),
            )

        generations = connection.execute(
            "SELECT generation_id, scan_kind FROM media_library_generations "
            "WHERE root_key = ? ORDER BY generation_id",
            (root_key,),
        ).fetchall()
        for generation in generations:
            generation_id = str(generation["generation_id"])
            connection.execute(
                "UPDATE media_library_workspace_entries SET duplicate_count = 0 "
                "WHERE generation_id = ?",
                (generation_id,),
            )
            workspace_rows = connection.execute(
                "SELECT * FROM media_library_workspace_entries "
                "WHERE generation_id = ? AND presence = 'present' "
                "AND code_key IS NOT NULL",
                (generation_id,),
            ).fetchall()
            if str(generation["scan_kind"]) == "full":
                effective = {
                    str(row["entry_id"]): row for row in workspace_rows
                }
            else:
                changed_ids = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT entry_id FROM media_library_workspace_entries "
                        "WHERE generation_id = ? UNION SELECT entry_id "
                        "FROM media_library_workspace_entry_deletions "
                        "WHERE generation_id = ?",
                        (generation_id, generation_id),
                    ).fetchall()
                }
                effective = {
                    str(row["entry_id"]): row
                    for row in connection.execute(
                        "SELECT * FROM media_library_entries "
                        "WHERE root_key = ? AND retired_generation_id IS NULL "
                        "AND presence = 'present' AND code_key IS NOT NULL",
                        (root_key,),
                    ).fetchall()
                    if str(row["entry_id"]) not in changed_ids
                }
                effective.update({str(row["entry_id"]): row for row in workspace_rows})
            groups: dict[tuple[str, object], list[sqlite3.Row]] = defaultdict(list)
            for row in effective.values():
                groups[(str(row["code_key"]), row["variant"])].append(row)
            for (code_key, variant), rows in groups.items():
                desired = len(rows) if len(rows) > 1 else 0
                if desired == 0:
                    continue
                for row in rows:
                    if "generation_id" in row.keys():
                        connection.execute(
                            "UPDATE media_library_workspace_entries "
                            "SET duplicate_count = ? WHERE generation_id = ? "
                            "AND entry_id = ?",
                            (desired, generation_id, str(row["entry_id"])),
                        )


def _refresh_history_cleanup_operation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> None:
    operation = connection.execute(
        "SELECT * FROM media_library_history_cleanup_operations "
        "WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if operation is None:
        raise MediaLibraryError("media library history cleanup is invalid")
    revision = _migration_int(
        operation["revision"], "media library history cleanup is invalid"
    )
    candidate_count = _migration_int(
        operation["candidate_count"], "media library history cleanup is invalid"
    )
    fact_count = _migration_int(
        operation["fact_count"], "media library history cleanup is invalid"
    )
    if revision != 1:
        raise MediaLibraryError("media library history cleanup is invalid")
    candidate_rows = connection.execute(
        "SELECT * FROM media_library_history_cleanup_candidates "
        "WHERE operation_id = ? ORDER BY position",
        (operation_id,),
    ).fetchall()
    if len(candidate_rows) != candidate_count or len(candidate_rows) > 10_000:
        raise MediaLibraryError("media library history cleanup is invalid")
    candidates = []
    for row in candidate_rows:
        candidates.append(
            {
                "position": _migration_int(
                    row["position"], "media library history cleanup is invalid"
                ),
                "task_type": str(row["task_type"]),
                "source_id": str(row["source_id"]),
                "fingerprint": str(row["fingerprint"]),
                "status": str(row["status"]),
                "record_count": _migration_int(
                    row["record_count"],
                    "media library history cleanup is invalid",
                ),
            }
        )
    if [item["position"] for item in candidates] != list(range(len(candidates))):
        raise MediaLibraryError("media library history cleanup is invalid")
    fact_rows = connection.execute(
        "SELECT mapping.candidate_position, facts.* "
        "FROM media_library_history_cleanup_facts mapping "
        "JOIN media_library_history_facts facts ON facts.fact_id = mapping.fact_id "
        "WHERE mapping.operation_id = ? "
        "ORDER BY mapping.candidate_position, mapping.fact_id",
        (operation_id,),
    ).fetchall()
    if len(fact_rows) != fact_count or len(fact_rows) > 100_000:
        raise MediaLibraryError("media library history cleanup is invalid")
    mapped_facts = [
        {
            "candidate_position": _migration_int(
                row["candidate_position"],
                "media library history cleanup mapping is invalid",
            ),
            **_migration_history_fact_mapping(row),
        }
        for row in fact_rows
    ]
    payload = {
        "revision": 1,
        "candidates": candidates,
        "facts": mapped_facts,
    }
    payload_body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fact_summaries = [
        {
            "candidate_position": _migration_int(
                row["candidate_position"],
                "media library history cleanup mapping is invalid",
            ),
            "fact_id": str(row["fact_id"]),
            "fact_digest": str(row["fact_digest"]),
            "archived_at": _migration_archived_at(row["archived_at"]),
        }
        for row in fact_rows
    ]
    if operation["payload_digest"] is None:
        operation_payload = {
            "revision": 1,
            "candidates": candidates,
            "facts": [
                {
                    key: item[key]
                    for key in (
                        "candidate_position",
                        "fact_id",
                        "fact_digest",
                    )
                }
                for item in fact_summaries
            ],
        }
        payload_digest = None
    else:
        payload_digest = hashlib.sha256(payload_body).hexdigest()
        operation_payload = {
            "revision": 1,
            "payload_digest": payload_digest,
            "candidates": candidates,
            "facts": fact_summaries,
        }
    connection.execute(
        "UPDATE media_library_history_cleanup_operations "
        "SET operation_digest = ?, payload_bytes = ?, payload_digest = ? "
        "WHERE operation_id = ?",
        (
            _migration_digest(operation_payload),
            len(payload_body),
            payload_digest,
            operation_id,
        ),
    )


def _create_copy_on_write_schema(connection: sqlite3.Connection) -> None:
    variants = ", ".join(f"'{value}'" for value in WEB_DOWNLOAD_VARIANTS)
    statements = (
        """
        CREATE TABLE media_library_directories (
            root_key TEXT NOT NULL REFERENCES media_library_roots(root_key),
            relative_path TEXT NOT NULL,
            created_generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id),
            retired_generation_id TEXT
                REFERENCES media_library_generations(generation_id),
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            modified_ns INTEGER NOT NULL,
            changed_ns INTEGER NOT NULL,
            CHECK (retired_generation_id IS NULL OR
                   retired_generation_id != created_generation_id),
            PRIMARY KEY (root_key, relative_path, created_generation_id)
        )
        """,
        "CREATE UNIQUE INDEX media_library_directories_live ON "
        "media_library_directories(root_key, relative_path) "
        "WHERE retired_generation_id IS NULL",
        f"""
        CREATE TABLE media_library_files (
            root_key TEXT NOT NULL REFERENCES media_library_roots(root_key),
            relative_path TEXT NOT NULL,
            created_generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id),
            retired_generation_id TEXT
                REFERENCES media_library_generations(generation_id),
            parent_path TEXT NOT NULL,
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            scope_path TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            variant TEXT CHECK (variant IS NULL OR variant IN ({variants})),
            source TEXT NOT NULL CHECK (source IN ('path', 'nfo')),
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size > 0),
            modified_ns INTEGER NOT NULL,
            suffix TEXT NOT NULL,
            changed_ns INTEGER,
            part_key TEXT,
            quality_height INTEGER,
            quality_source TEXT CHECK (
                quality_source IS NULL OR quality_source IN ('probe', 'filename')
            ),
            nfo_status TEXT NOT NULL CHECK (
                nfo_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            nfo_path TEXT,
            nfo_json TEXT,
            portrait_status TEXT NOT NULL CHECK (
                portrait_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            landscape_status TEXT NOT NULL CHECK (
                landscape_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            CHECK (retired_generation_id IS NULL OR
                   retired_generation_id != created_generation_id),
            PRIMARY KEY (root_key, relative_path, created_generation_id)
        )
        """,
        "CREATE UNIQUE INDEX media_library_files_live ON "
        "media_library_files(root_key, relative_path) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_files_entry ON "
        "media_library_files(root_key, entry_id) WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_files_parent ON "
        "media_library_files(root_key, parent_path) WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_files_quality_cache ON media_library_files("
        "root_key, device, inode, size, modified_ns, changed_ns, quality_source) "
        "WHERE retired_generation_id IS NULL",
        f"""
        CREATE TABLE media_library_entries (
            root_key TEXT NOT NULL REFERENCES media_library_roots(root_key),
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            created_generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id),
            retired_generation_id TEXT
                REFERENCES media_library_generations(generation_id),
            scope_path TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            variant TEXT CHECK (variant IS NULL OR variant IN ({variants})),
            title TEXT,
            release_date TEXT,
            source TEXT NOT NULL CHECK (source IN ('path', 'nfo')),
            presence TEXT NOT NULL CHECK (presence IN ('present', 'missing')),
            primary_media_path TEXT NOT NULL,
            nfo_status TEXT NOT NULL CHECK (
                nfo_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            nfo_path TEXT,
            portrait_status TEXT NOT NULL CHECK (
                portrait_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            landscape_status TEXT NOT NULL CHECK (
                landscape_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            quality_height INTEGER,
            duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
            updated_at REAL NOT NULL,
            CHECK (retired_generation_id IS NULL OR
                   retired_generation_id != created_generation_id),
            PRIMARY KEY (root_key, entry_id, created_generation_id)
        )
        """,
        "CREATE UNIQUE INDEX media_library_entries_live ON "
        "media_library_entries(root_key, entry_id) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_entries_lookup ON "
        "media_library_entries(entry_id, root_key) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_entries_code ON "
        "media_library_entries(root_key, code_key, scope_path) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_entries_presence ON "
        "media_library_entries(root_key, presence, code_key) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_entries_quality ON "
        "media_library_entries(root_key, quality_height) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_entries_completeness ON "
        "media_library_entries(root_key, nfo_status, portrait_status, landscape_status) "
        "WHERE retired_generation_id IS NULL",
        "CREATE INDEX media_library_entries_variant ON "
        "media_library_entries(root_key, code_key, variant, scope_path) "
        "WHERE retired_generation_id IS NULL",
        """
        CREATE TABLE media_library_terms (
            root_key TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            entry_generation_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (
                kind IN ('actor', 'maker', 'publisher', 'tag', 'series', 'director')
            ),
            value TEXT NOT NULL,
            value_key TEXT NOT NULL,
            PRIMARY KEY (
                root_key, entry_id, entry_generation_id, kind, value_key
            ),
            FOREIGN KEY (root_key, entry_id, entry_generation_id)
                REFERENCES media_library_entries(
                    root_key, entry_id, created_generation_id
                ) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX media_library_terms_filter ON "
        "media_library_terms(kind, value_key, root_key, entry_id)",
        """
        CREATE TABLE media_library_workspace_directories (
            generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            modified_ns INTEGER NOT NULL,
            changed_ns INTEGER NOT NULL,
            PRIMARY KEY (generation_id, relative_path)
        )
        """,
        """
        CREATE TABLE media_library_workspace_deletions (
            generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            PRIMARY KEY (generation_id, relative_path)
        )
        """,
        f"""
        CREATE TABLE media_library_workspace_files (
            generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            parent_path TEXT NOT NULL,
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            scope_path TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            variant TEXT CHECK (variant IS NULL OR variant IN ({variants})),
            source TEXT NOT NULL CHECK (source IN ('path', 'nfo')),
            device TEXT NOT NULL,
            inode TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size > 0),
            modified_ns INTEGER NOT NULL,
            suffix TEXT NOT NULL,
            changed_ns INTEGER,
            part_key TEXT,
            quality_height INTEGER,
            quality_source TEXT CHECK (
                quality_source IS NULL OR quality_source IN ('probe', 'filename')
            ),
            nfo_status TEXT NOT NULL CHECK (
                nfo_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            nfo_path TEXT,
            nfo_json TEXT,
            portrait_status TEXT NOT NULL CHECK (
                portrait_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            landscape_status TEXT NOT NULL CHECK (
                landscape_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            PRIMARY KEY (generation_id, relative_path)
        )
        """,
        "CREATE INDEX media_library_workspace_files_entry ON "
        "media_library_workspace_files(generation_id, entry_id)",
        "CREATE INDEX media_library_workspace_files_parent ON "
        "media_library_workspace_files(generation_id, parent_path)",
        f"""
        CREATE TABLE media_library_workspace_entries (
            generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            scope_path TEXT NOT NULL,
            code TEXT,
            code_key TEXT,
            variant TEXT CHECK (variant IS NULL OR variant IN ({variants})),
            title TEXT,
            release_date TEXT,
            source TEXT NOT NULL CHECK (source IN ('path', 'nfo')),
            presence TEXT NOT NULL CHECK (presence IN ('present', 'missing')),
            primary_media_path TEXT NOT NULL,
            nfo_status TEXT NOT NULL CHECK (
                nfo_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            nfo_path TEXT,
            portrait_status TEXT NOT NULL CHECK (
                portrait_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            landscape_status TEXT NOT NULL CHECK (
                landscape_status IN ('present', 'missing', 'invalid', 'unknown')
            ),
            quality_height INTEGER,
            duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
            updated_at REAL NOT NULL,
            PRIMARY KEY (generation_id, entry_id)
        )
        """,
        """
        CREATE TABLE media_library_workspace_entry_deletions (
            generation_id TEXT NOT NULL
                REFERENCES media_library_generations(generation_id) ON DELETE CASCADE,
            entry_id TEXT NOT NULL CHECK (length(entry_id) = 40),
            PRIMARY KEY (generation_id, entry_id)
        )
        """,
        """
        CREATE TABLE media_library_workspace_terms (
            generation_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (
                kind IN ('actor', 'maker', 'publisher', 'tag', 'series', 'director')
            ),
            value TEXT NOT NULL,
            value_key TEXT NOT NULL,
            PRIMARY KEY (generation_id, entry_id, kind, value_key),
            FOREIGN KEY (generation_id, entry_id)
                REFERENCES media_library_workspace_entries(generation_id, entry_id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX media_library_workspace_terms_filter ON "
        "media_library_workspace_terms(kind, value_key, generation_id, entry_id)",
    )
    for statement in statements:
        connection.execute(statement)


def _verify_schema_v1(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "media_library_roots",
        (
            "root_key",
            "device",
            "inode",
            "state",
            "published_generation_id",
            "revision",
        ),
    )
    require_columns(
        connection,
        "media_library_generations",
        (
            "generation_id",
            "root_key",
            "scan_kind",
            "status",
            "root_device",
            "root_inode",
        ),
    )
    require_columns(
        connection,
        "media_library_entries",
        ("generation_id", "entry_id", "code_key", "presence", "primary_media_path"),
    )
    require_columns(
        connection,
        "media_library_files",
        ("generation_id", "relative_path", "entry_id", "parent_path"),
    )


def _verify_history_facts_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "media_library_history_facts",
        (
            "fact_id",
            "source_type",
            "source_id",
            "code",
            "code_key",
            "relative_media_path",
            "quality_height",
            "nfo_provenance_json",
            "replaces_source_id",
            "superseded_by_source_id",
            "publication_outcome",
            "details_json",
            "source_created_at",
            "archived_at",
            "fact_digest",
        ),
    )


def _verify_history_cleanup_schema_v3(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "media_library_history_cleanup_operations",
        (
            "operation_id",
            "revision",
            "status",
            "operation_digest",
            "candidate_count",
            "fact_count",
            "payload_bytes",
            "created_at",
            "updated_at",
        ),
    )
    require_columns(
        connection,
        "media_library_history_cleanup_candidates",
        (
            "operation_id",
            "position",
            "task_type",
            "source_id",
            "fingerprint",
            "status",
            "record_count",
        ),
    )
    require_columns(
        connection,
        "media_library_history_cleanup_facts",
        (
            "operation_id",
            "candidate_position",
            "fact_id",
            "fact_digest",
            "created_by_operation",
        ),
    )


def _verify_history_cleanup_schema(connection: sqlite3.Connection) -> None:
    _verify_history_cleanup_schema_v3(connection)
    require_columns(
        connection,
        "media_library_history_cleanup_operations",
        ("payload_digest",),
    )


def _verify_quality_cache_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "media_library_files",
        ("changed_ns", "quality_source"),
    )
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
        "AND name = 'media_library_files_quality_cache'"
    ).fetchone()
    if row is None:
        raise MediaLibraryError("media library quality cache index is unavailable")


def _verify_variant_schema(connection: sqlite3.Connection) -> None:
    require_columns(connection, "media_library_files", ("variant",))
    require_columns(connection, "media_library_entries", ("variant",))
    invalid = connection.execute(
        "SELECT 1 FROM ("
        "SELECT variant FROM media_library_files UNION ALL "
        "SELECT variant FROM media_library_entries"
        ") WHERE variant IS NOT NULL AND variant NOT IN (?, ?, ?) LIMIT 1",
        WEB_DOWNLOAD_VARIANTS,
    ).fetchone()
    if invalid is not None:
        raise MediaLibraryError("media library variant is invalid")


def _verify_copy_on_write_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "media_library_roots",
        (
            "root_key",
            "device",
            "inode",
            "state",
            "published_generation_id",
            "revision",
        ),
    )
    require_columns(
        connection,
        "media_library_generations",
        (
            "generation_id",
            "root_key",
            "base_generation_id",
            "scan_kind",
            "status",
        ),
    )
    require_columns(
        connection,
        "media_library_directories",
        (
            "root_key",
            "relative_path",
            "created_generation_id",
            "retired_generation_id",
        ),
    )
    require_columns(
        connection,
        "media_library_files",
        (
            "root_key",
            "relative_path",
            "created_generation_id",
            "retired_generation_id",
            "entry_id",
            "parent_path",
        ),
    )
    require_columns(
        connection,
        "media_library_entries",
        (
            "root_key",
            "entry_id",
            "created_generation_id",
            "retired_generation_id",
            "code_key",
            "presence",
            "primary_media_path",
        ),
    )
    require_columns(
        connection,
        "media_library_terms",
        ("root_key", "entry_id", "entry_generation_id", "kind", "value_key"),
    )
    for table, columns in (
        (
            "media_library_workspace_directories",
            ("generation_id", "relative_path", "device", "inode"),
        ),
        (
            "media_library_workspace_deletions",
            ("generation_id", "relative_path"),
        ),
        (
            "media_library_workspace_files",
            ("generation_id", "relative_path", "entry_id", "parent_path"),
        ),
        (
            "media_library_workspace_entries",
            ("generation_id", "entry_id", "presence", "primary_media_path"),
        ),
        (
            "media_library_workspace_entry_deletions",
            ("generation_id", "entry_id"),
        ),
        (
            "media_library_workspace_terms",
            ("generation_id", "entry_id", "kind", "value_key"),
        ),
    ):
        require_columns(connection, table, columns)
    required_indexes = {
        "media_library_directories_live",
        "media_library_files_live",
        "media_library_files_quality_cache",
        "media_library_entries_live",
        "media_library_entries_lookup",
        "media_library_entries_code",
        "media_library_terms_filter",
    }
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    if not required_indexes.issubset(indexes):
        raise MediaLibraryError("media library copy-on-write indexes are unavailable")
    invalid_live = connection.execute(
        """
        SELECT 1
        FROM media_library_roots roots
        LEFT JOIN media_library_generations generations
          ON generations.generation_id = roots.published_generation_id
         AND generations.root_key = roots.root_key
         AND generations.status = 'published'
        WHERE roots.published_generation_id IS NOT NULL
          AND generations.generation_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if invalid_live is not None:
        raise MediaLibraryError("media library published generation is inconsistent")
    foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_error is not None:
        raise MediaLibraryError("media library copy-on-write schema is inconsistent")


def _verify_schema(connection: sqlite3.Connection) -> None:
    _verify_copy_on_write_schema(connection)
    _verify_history_facts_schema(connection)
    _verify_history_cleanup_schema(connection)
    _verify_quality_cache_schema(connection)
    _verify_variant_schema(connection)
