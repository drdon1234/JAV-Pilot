"""SQLite schema creation, migrations and verification for web download batches."""

from __future__ import annotations

import hmac
import json
import sqlite3

from ...core.migrations import MigrationError, add_column_if_missing, require_columns
from ..errors import WebDownloadError
from ..jobs import normalize_web_download_code
from ..variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
    normalize_web_download_variant,
)
from .errors import WebDownloadBatchError
from .models import (
    ABSOLUTE_MAX_BATCH_PAGES,
    DEFAULT_BATCH_PAGE_BUDGET,
    RESOURCE_SEARCH_SELECTION_PROVENANCE,
    SERIES_DISCOVERY_PROVENANCE,
    BatchRequest,
    SelectedBatchItem,
)
from .validation import (
    auto_download_request_hash,
    selected_batch_items_hash,
    split_full_code,
    validate_direct_queue_hash,
    validate_selected_batch_items,
    validate_source_revision,
    validate_source_session_id,
    variant_priority_from_json,
    variants_from_json,
    verify_selected_batch_provenance,
)

BATCH_SCHEMA_COMPONENT = "web_download_batches"
BATCH_SCHEMA_VERSION = 10

BATCH_STATUSES = (
    "queued",
    "discovering",
    "ready",
    "too_many",
    "incomplete",
    "failed",
    "cancelled",
    "expired",
    "committed",
)
ITEM_STATUSES = ("discovered", "created", "reused", "skipped_completed")


_BATCH_PROVENANCE_TYPES = (
    SERIES_DISCOVERY_PROVENANCE,
    RESOURCE_SEARCH_SELECTION_PROVENANCE,
)


def _require_web_download_jobs(connection: sqlite3.Connection) -> None:
    jobs_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'web_download_jobs'"
    ).fetchone()
    if jobs_table is None:
        raise MigrationError("web download jobs must be initialized before batches")


def migrate_web_download_batch_v1(connection: sqlite3.Connection) -> None:
    _require_web_download_jobs(connection)
    statuses = ", ".join(f"'{status}'" for status in BATCH_STATUSES)
    item_statuses = ", ".join(f"'{status}'" for status in ITEM_STATUSES)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS web_download_batches (
            batch_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            mode TEXT NOT NULL CHECK (mode IN ('exact', 'all', 'range')),
            code_or_prefix TEXT NOT NULL,
            prefix TEXT NOT NULL,
            suffix_width INTEGER CHECK (
                suffix_width IS NULL OR
                (suffix_width >= 1 AND suffix_width <= 9)
            ),
            start_suffix TEXT,
            end_suffix TEXT,
            max_height INTEGER NOT NULL CHECK (
                max_height >= 144 AND max_height <= 4320
            ),
            discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (
                discovered_count >= 0
            ),
            discovery_complete INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            error TEXT
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS web_download_batch_items (
            batch_id TEXT NOT NULL REFERENCES web_download_batches(batch_id)
                ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({item_statuses})),
            job_id TEXT,
            PRIMARY KEY (batch_id, code_key),
            UNIQUE (batch_id, position)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS web_download_batches_created_at "
        "ON web_download_batches(created_at DESC)"
    )


def migrate_web_download_batch_v2(connection: sqlite3.Connection) -> None:
    batch_columns = (
        ("parent_batch_id", "TEXT"),
        ("page_number", "INTEGER NOT NULL DEFAULT 1"),
        ("next_start_suffix", "TEXT"),
        ("continuation_batch_id", "TEXT"),
        (
            "existing_policy",
            "TEXT NOT NULL DEFAULT 'higher_quality' CHECK "
            "(existing_policy IN ('higher_quality', 'overwrite', 'skip'))",
        ),
        ("commit_intent_hash", "TEXT"),
    )
    for column, definition in batch_columns:
        add_column_if_missing(
            connection,
            "web_download_batches",
            column,
            definition,
        )
    add_column_if_missing(
        connection,
        "web_download_batch_items",
        "selected",
        "INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1))",
    )


def migrate_web_download_batch_v3(connection: sqlite3.Connection) -> None:
    batch_columns = (
        ("root_chain_id", "TEXT"),
        (
            "page_budget",
            f"INTEGER NOT NULL DEFAULT {DEFAULT_BATCH_PAGE_BUDGET} CHECK "
            f"(page_budget >= 1 AND page_budget <= {ABSOLUTE_MAX_BATCH_PAGES})",
        ),
        (
            "limit_reached",
            "INTEGER NOT NULL DEFAULT 0 CHECK (limit_reached IN (0, 1))",
        ),
        ("library_revision", "TEXT"),
        (
            "quality_complete",
            "INTEGER NOT NULL DEFAULT 1 CHECK (quality_complete IN (0, 1))",
        ),
        ("rule_id", "TEXT"),
    )
    for column, definition in batch_columns:
        add_column_if_missing(
            connection,
            "web_download_batches",
            column,
            definition,
        )
    item_columns = (
        (
            "quality_status",
            "TEXT NOT NULL DEFAULT 'legacy' CHECK "
            "(quality_status IN ('pending', 'ready', 'failed', 'legacy'))",
        ),
        ("available_heights_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("default_height", "INTEGER"),
        (
            "quality_strategy",
            "TEXT NOT NULL DEFAULT 'highest' CHECK "
            "(quality_strategy IN ('highest', 'selected'))",
        ),
        ("requested_height", "INTEGER"),
        ("quality_error_code", "TEXT"),
    )
    for column, definition in item_columns:
        add_column_if_missing(
            connection,
            "web_download_batch_items",
            column,
            definition,
        )
    connection.execute(
        "UPDATE web_download_batch_items SET requested_height = ("
        "SELECT max_height FROM web_download_batches b "
        "WHERE b.batch_id = web_download_batch_items.batch_id) "
        "WHERE selected = 1 AND requested_height IS NULL AND batch_id IN ("
        "SELECT batch_id FROM web_download_batches WHERE status = 'committed')"
    )

    rows = connection.execute(
        "SELECT batch_id, parent_batch_id, page_number FROM web_download_batches"
    ).fetchall()
    parents = {
        str(row["batch_id"]): (
            str(row["parent_batch_id"]) if row["parent_batch_id"] is not None else None
        )
        for row in rows
    }
    roots: dict[str, str] = {}
    for row in rows:
        batch_id = str(row["batch_id"])
        page_number = int(row["page_number"])
        if not 1 <= page_number <= ABSOLUTE_MAX_BATCH_PAGES:
            raise MigrationError("web download batch page number is invalid")
        trail: list[str] = []
        current = batch_id
        while current not in roots:
            if current in trail:
                raise MigrationError("web download batch continuation cycle detected")
            trail.append(current)
            parent = parents.get(current)
            if parent is None:
                root = current
                break
            if parent not in parents:
                raise MigrationError("web download batch continuation is disconnected")
            current = parent
        else:
            root = roots[current]
        for member in trail:
            roots[member] = root
    for batch_id, root_id in roots.items():
        connection.execute(
            "UPDATE web_download_batches SET root_chain_id = ? WHERE batch_id = ?",
            (root_id, batch_id),
        )
    connection.execute(
        "CREATE UNIQUE INDEX web_download_batches_chain_page "
        "ON web_download_batches(root_chain_id, page_number)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX web_download_batches_unique_parent "
        "ON web_download_batches(parent_batch_id) WHERE parent_batch_id IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX web_download_batches_chain_created "
        "ON web_download_batches(root_chain_id, page_number, created_at)"
    )
    connection.execute(
        """
        CREATE TABLE web_download_batch_rules (
            rule_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('exact', 'all', 'range')),
            code_or_prefix TEXT NOT NULL,
            prefix TEXT NOT NULL,
            suffix_width INTEGER,
            start_suffix TEXT,
            end_suffix TEXT,
            max_height INTEGER NOT NULL CHECK (
                max_height >= 144 AND max_height <= 4320
            ),
            existing_policy TEXT NOT NULL CHECK (
                existing_policy IN ('higher_quality', 'overwrite', 'skip')
            ),
            default_quality_strategy TEXT NOT NULL CHECK (
                default_quality_strategy IN ('highest', 'selected')
            ),
            default_height INTEGER,
            selection_mode TEXT NOT NULL CHECK (
                selection_mode IN ('all', 'missing', 'upgrades')
            ),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX web_download_batch_rules_updated "
        "ON web_download_batch_rules(updated_at DESC, rule_id)"
    )


def migrate_web_download_batch_v4(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "web_download_batches",
        "rule_revision",
        "INTEGER CHECK (rule_revision IS NULL OR rule_revision >= 1)",
    )
    connection.execute(
        "UPDATE web_download_batches SET status = 'failed', quality_complete = 1, "
        "expires_at = NULL, error = 'Batch rule preview must be recreated after upgrade' "
        "WHERE rule_id IS NOT NULL AND (status IN ('queued', 'discovering') OR "
        "(status = 'ready' AND quality_complete = 0))"
    )
    connection.execute(
        "UPDATE web_download_batches SET rule_id = NULL WHERE rule_id IS NOT NULL"
    )


def migrate_web_download_batch_v5(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "web_download_batch_rules",
        "deleted_at",
        "REAL",
    )


def migrate_web_download_batch_v6(connection: sqlite3.Connection) -> None:
    priority_default = json.dumps(
        DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        separators=(",", ":"),
    )
    available_default = json.dumps(
        (DEFAULT_WEB_DOWNLOAD_VARIANT,),
        separators=(",", ":"),
    )
    add_column_if_missing(
        connection,
        "web_download_batches",
        "variant_priority_json",
        f"TEXT NOT NULL DEFAULT '{priority_default}'",
    )
    add_column_if_missing(
        connection,
        "web_download_batch_items",
        "available_variants_json",
        f"TEXT NOT NULL DEFAULT '{available_default}'",
    )
    add_column_if_missing(
        connection,
        "web_download_batch_items",
        "variant",
        "TEXT NOT NULL DEFAULT 'original' CHECK "
        "(variant IN ('original', 'chinese_subtitle', 'uncensored_leak'))",
    )
    add_column_if_missing(
        connection,
        "web_download_batch_rules",
        "variant_priority_json",
        f"TEXT NOT NULL DEFAULT '{priority_default}'",
    )


def migrate_web_download_batch_v7(connection: sqlite3.Connection) -> None:
    provenance_values = ", ".join(f"'{value}'" for value in _BATCH_PROVENANCE_TYPES)
    add_column_if_missing(
        connection,
        "web_download_batches",
        "provenance_type",
        "TEXT NOT NULL DEFAULT 'series_discovery' CHECK "
        f"(provenance_type IN ({provenance_values}))",
    )
    add_column_if_missing(
        connection,
        "web_download_batches",
        "source_session_id",
        "TEXT",
    )
    add_column_if_missing(
        connection,
        "web_download_batches",
        "source_revision",
        "INTEGER CHECK (source_revision IS NULL OR source_revision >= 1)",
    )
    add_column_if_missing(
        connection,
        "web_download_batches",
        "source_items_hash",
        "TEXT",
    )


def migrate_web_download_batch_v8(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "web_download_batches",
        "direct_queue_key_hash",
        "TEXT",
    )
    add_column_if_missing(
        connection,
        "web_download_batches",
        "direct_queue_request_hash",
        "TEXT",
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "web_download_batches_direct_queue_key "
        "ON web_download_batches(direct_queue_key_hash) "
        "WHERE direct_queue_key_hash IS NOT NULL"
    )


def migrate_web_download_batch_v9(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "web_download_batches",
        "auto_commit",
        "INTEGER NOT NULL DEFAULT 0 CHECK (auto_commit IN (0, 1))",
    )
    add_column_if_missing(
        connection,
        "web_download_batches",
        "direct_code_key",
        "TEXT",
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "web_download_batches_active_auto_code "
        "ON web_download_batches(direct_code_key) "
        "WHERE auto_commit = 1 "
        "AND status IN ('queued', 'discovering', 'ready')"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS web_download_batches_auto_code_history "
        "ON web_download_batches(direct_code_key, created_at DESC, batch_id DESC) "
        "WHERE auto_commit = 1"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS web_download_auto_requests (
            key_hash TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            batch_id TEXT NOT NULL REFERENCES web_download_batches(batch_id)
                ON DELETE CASCADE,
            created_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS web_download_auto_requests_batch "
        "ON web_download_auto_requests(batch_id)"
    )


def migrate_web_download_batch_v10(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    item_updates: list[tuple[str, str, str, str]] = []
    item_identities: dict[tuple[str, str], int] = {}
    changed_batches: set[str] = set()
    old_items_by_batch: dict[str, list[SelectedBatchItem]] = {}
    new_items_by_batch: dict[str, list[SelectedBatchItem]] = {}
    item_rows = connection.execute(
        "SELECT batch_id, position, code, code_key, available_variants_json, "
        "variant "
        "FROM web_download_batch_items ORDER BY batch_id, position"
    ).fetchall()
    for item in item_rows:
        old_code = str(item["code"])
        old_code_key = str(item["code_key"])
        try:
            display_code, code_key = normalize_web_download_code(old_code)
            available_variants = variants_from_json(
                item["available_variants_json"]
            )
            variant = normalize_web_download_variant(item["variant"])
        except (ValueError, WebDownloadError, WebDownloadBatchError) as exc:
            raise MigrationError(
                "web download batch item catalog code is invalid"
            ) from exc
        if old_code_key != _legacy_batch_code_key(old_code):
            raise MigrationError(
                "web download batch item catalog identity is invalid"
            )
        batch_id = str(item["batch_id"])
        position = int(item["position"])
        identity = (batch_id, code_key)
        collided = item_identities.setdefault(identity, position)
        if collided != position:
            raise MigrationError(
                "web download batch items collide after catalog code normalization"
            )
        old_items_by_batch.setdefault(batch_id, []).append(
            SelectedBatchItem(
                old_code,
                old_code_key,
                available_variants,
                variant,
            )
        )
        new_items_by_batch.setdefault(batch_id, []).append(
            SelectedBatchItem(
                display_code,
                code_key,
                available_variants,
                variant,
            )
        )
        if display_code != old_code or code_key != old_code_key:
            item_updates.append(
                (display_code, code_key, batch_id, old_code_key)
            )
            changed_batches.add(batch_id)

    active_auto_identities: dict[str, str] = {}
    exact_updates: list[tuple[str, str, int, str, str, str | None]] = []
    changed_exact_batches: set[str] = set()
    source_hash_updates: list[tuple[str, str]] = []
    auto_request_updates: list[tuple[str, str, str]] = []
    rule_updates: list[tuple[str, str, int, str, str, str]] = []
    batch_rows = connection.execute(
        "SELECT * FROM web_download_batches ORDER BY batch_id"
    ).fetchall()
    for row in batch_rows:
        batch_id = str(row["batch_id"])
        if str(row["provenance_type"]) == RESOURCE_SEARCH_SELECTION_PROVENANCE:
            old_items = tuple(old_items_by_batch.get(batch_id, ()))
            new_items = tuple(new_items_by_batch.get(batch_id, ()))
            try:
                source_session_id = validate_source_session_id(
                    row["source_session_id"]
                )
                source_revision = validate_source_revision(row["source_revision"])
                expected_old_hash = selected_batch_items_hash(
                    source_session_id,
                    source_revision,
                    old_items,
                )
                migrated_hash = selected_batch_items_hash(
                    source_session_id,
                    source_revision,
                    validate_selected_batch_items(new_items),
                )
            except WebDownloadBatchError as exc:
                raise MigrationError(
                    "web download batch provenance is invalid"
                ) from exc
            persisted_hash = str(row["source_items_hash"] or "")
            if not hmac.compare_digest(persisted_hash, expected_old_hash):
                raise MigrationError(
                    "web download batch provenance changed before migration"
                )
            if migrated_hash != persisted_hash:
                source_hash_updates.append((migrated_hash, batch_id))

        if str(row["mode"]) != "exact":
            continue
        old_code = str(row["code_or_prefix"])
        old_code_key = _legacy_batch_code_key(old_code)
        try:
            old_prefix, old_suffix = split_full_code(old_code)
            display_code, code_key = normalize_web_download_code(
                old_code
            )
            prefix, suffix = split_full_code(display_code)
        except (WebDownloadError, WebDownloadBatchError) as exc:
            raise MigrationError(
                "web download batch catalog code is invalid"
            ) from exc
        if (
            str(row["prefix"]) != old_prefix
            or int(row["suffix_width"]) != len(old_suffix)
            or str(row["start_suffix"]) != old_suffix
            or str(row["end_suffix"]) != old_suffix
        ):
            raise MigrationError("web download batch catalog identity is invalid")
        direct_code_key = code_key if bool(row["auto_commit"]) else None
        if bool(row["auto_commit"]) and str(row["direct_code_key"] or "") != old_code_key:
            raise MigrationError(
                "automatic web download catalog identity is invalid"
            )
        if not bool(row["auto_commit"]) and row["direct_code_key"] is not None:
            raise MigrationError("automatic web download catalog identity is invalid")
        if bool(row["auto_commit"]) and str(row["status"]) in {
            "queued",
            "discovering",
            "ready",
        }:
            collided = active_auto_identities.setdefault(code_key, batch_id)
            if collided != batch_id:
                raise MigrationError(
                    "automatic web download batches collide after catalog code normalization"
                )
        exact_updates.append(
            (
                display_code,
                prefix,
                len(suffix),
                suffix,
                batch_id,
                direct_code_key,
            )
        )
        if display_code != old_code or code_key != old_code_key:
            changed_exact_batches.add(batch_id)

        if bool(row["auto_commit"]):
            request = BatchRequest(
                mode="exact",
                code_or_prefix=old_code,
                prefix=str(row["prefix"]),
                suffix_width=int(row["suffix_width"]),
                start=str(row["start_suffix"]),
                end=str(row["end_suffix"]),
                max_height=int(row["max_height"]),
                existing_policy=str(row["existing_policy"]),
                variant_priority=variant_priority_from_json(
                    row["variant_priority_json"]
                ),
                provenance_type=SERIES_DISCOVERY_PROVENANCE,
                auto_commit=True,
            )
            old_request_hash = auto_download_request_hash(request, old_code_key)
            new_request_hash = auto_download_request_hash(request, code_key)
            requests = connection.execute(
                "SELECT key_hash, request_hash FROM web_download_auto_requests "
                "WHERE batch_id = ? ORDER BY key_hash",
                (batch_id,),
            ).fetchall()
            if not requests:
                raise MigrationError("automatic web download request is missing")
            for request_row in requests:
                persisted_hash = str(request_row["request_hash"])
                if not hmac.compare_digest(persisted_hash, old_request_hash):
                    raise MigrationError(
                        "automatic web download request changed before migration"
                    )
                if new_request_hash != persisted_hash:
                    auto_request_updates.append(
                        (
                            new_request_hash,
                            str(request_row["key_hash"]),
                            batch_id,
                        )
                    )

    for row in connection.execute(
        "SELECT rule_id, code_or_prefix, prefix, suffix_width, start_suffix, "
        "end_suffix FROM web_download_batch_rules WHERE mode = 'exact' "
        "ORDER BY rule_id"
    ).fetchall():
        rule_id = str(row["rule_id"])
        old_rule_code = str(row["code_or_prefix"])
        try:
            old_rule_prefix, old_rule_suffix = split_full_code(old_rule_code)
            display_code, _rule_code_key = normalize_web_download_code(old_rule_code)
            rule_prefix, rule_suffix = split_full_code(display_code)
        except (WebDownloadError, WebDownloadBatchError) as exc:
            raise MigrationError(
                "web download batch rule catalog code is invalid"
            ) from exc
        if (
            str(row["prefix"]) != old_rule_prefix
            or int(row["suffix_width"]) != len(old_rule_suffix)
            or str(row["start_suffix"]) != old_rule_suffix
            or str(row["end_suffix"]) != old_rule_suffix
        ):
            raise MigrationError("web download batch rule identity is invalid")
        rule_updates.append(
            (
                display_code,
                rule_prefix,
                len(rule_suffix),
                rule_suffix,
                rule_suffix,
                rule_id,
            )
        )

    connection.executemany(
        "UPDATE web_download_batch_items SET code = ?, code_key = ? "
        "WHERE batch_id = ? AND code_key = ?",
        item_updates,
    )
    connection.executemany(
        "UPDATE web_download_batches SET code_or_prefix = ?, prefix = ?, "
        "suffix_width = ?, start_suffix = ?, end_suffix = ?, direct_code_key = ? "
        "WHERE batch_id = ?",
        (
            (display, prefix, width, suffix, suffix, direct_key, batch_id)
            for display, prefix, width, suffix, batch_id, direct_key in exact_updates
        ),
    )
    connection.executemany(
        "UPDATE web_download_batches SET source_items_hash = ? WHERE batch_id = ?",
        source_hash_updates,
    )
    connection.executemany(
        "UPDATE web_download_auto_requests SET request_hash = ? "
        "WHERE key_hash = ? AND batch_id = ?",
        auto_request_updates,
    )

    connection.executemany(
        "UPDATE web_download_batch_rules SET code_or_prefix = ?, prefix = ?, "
        "suffix_width = ?, start_suffix = ?, end_suffix = ? WHERE rule_id = ?",
        rule_updates,
    )

    for batch_id in changed_batches | changed_exact_batches:
        connection.execute(
            "UPDATE web_download_batches SET commit_intent_hash = NULL "
            "WHERE batch_id = ?",
            (batch_id,),
        )


def _legacy_batch_code_key(value: object) -> str:
    clean = str(value or "").strip().upper()
    if not clean or not clean.isascii():
        raise MigrationError("web download batch catalog code is invalid")
    return "".join(character for character in clean if character.isalnum())


def verify_web_download_batch_schema(connection: sqlite3.Connection) -> None:
    _require_web_download_jobs(connection)
    require_columns(
        connection,
        "web_download_batches",
        (
            "batch_id",
            "token_hash",
            "status",
            "mode",
            "code_or_prefix",
            "prefix",
            "suffix_width",
            "start_suffix",
            "end_suffix",
            "max_height",
            "existing_policy",
            "commit_intent_hash",
            "discovered_count",
            "discovery_complete",
            "created_at",
            "updated_at",
            "expires_at",
            "error",
            "parent_batch_id",
            "page_number",
            "next_start_suffix",
            "continuation_batch_id",
            "root_chain_id",
            "page_budget",
            "limit_reached",
            "library_revision",
            "quality_complete",
            "rule_id",
            "rule_revision",
            "variant_priority_json",
            "provenance_type",
            "source_session_id",
            "source_revision",
            "source_items_hash",
            "direct_queue_key_hash",
            "direct_queue_request_hash",
            "auto_commit",
            "direct_code_key",
        ),
    )
    require_columns(
        connection,
        "web_download_batch_items",
        (
            "batch_id",
            "position",
            "code",
            "code_key",
            "status",
            "job_id",
            "selected",
            "quality_status",
            "available_heights_json",
            "default_height",
            "quality_strategy",
            "requested_height",
            "quality_error_code",
            "available_variants_json",
            "variant",
        ),
    )
    require_columns(
        connection,
        "web_download_batch_rules",
        (
            "rule_id",
            "name",
            "mode",
            "code_or_prefix",
            "prefix",
            "suffix_width",
            "start_suffix",
            "end_suffix",
            "max_height",
            "existing_policy",
            "default_quality_strategy",
            "default_height",
            "selection_mode",
            "revision",
            "created_at",
            "updated_at",
            "deleted_at",
            "variant_priority_json",
        ),
    )
    direct_queue_index = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
        "AND name = 'web_download_batches_direct_queue_key'"
    ).fetchone()
    if direct_queue_index is None:
        raise MigrationError("web download direct queue index is missing")
    auto_code_index = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
        "AND name = 'web_download_batches_active_auto_code'"
    ).fetchone()
    auto_request_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'web_download_auto_requests'"
    ).fetchone()
    if auto_code_index is None or auto_request_table is None:
        raise MigrationError("automatic web download schema objects are missing")
    require_columns(
        connection,
        "web_download_auto_requests",
        ("key_hash", "request_hash", "batch_id", "created_at"),
    )
    invalid_chain = connection.execute(
        "SELECT 1 FROM web_download_batches WHERE root_chain_id IS NULL "
        "OR page_budget < 1 OR page_budget > ? "
        "OR (rule_id IS NULL AND rule_revision IS NOT NULL) "
        "OR (rule_id IS NOT NULL AND rule_revision IS NULL) "
        "OR (rule_revision IS NOT NULL AND rule_revision < 1) "
        "OR ((direct_queue_key_hash IS NULL) != "
        "(direct_queue_request_hash IS NULL)) "
        "OR (direct_queue_key_hash IS NOT NULL AND provenance_type != ?) "
        "OR ((auto_commit = 1) != (direct_code_key IS NOT NULL)) "
        "OR (auto_commit = 1 AND (mode != 'exact' "
        "OR provenance_type != ? OR parent_batch_id IS NOT NULL "
        "OR page_number != 1 OR rule_id IS NOT NULL)) LIMIT 1",
        (
            ABSOLUTE_MAX_BATCH_PAGES,
            RESOURCE_SEARCH_SELECTION_PROVENANCE,
            SERIES_DISCOVERY_PROVENANCE,
        ),
    ).fetchone()
    if invalid_chain is not None:
        raise MigrationError("web download batch chain state is invalid")
    rows = connection.execute("SELECT * FROM web_download_batches").fetchall()
    by_id = {str(row["batch_id"]): row for row in rows}
    invariant_fields = (
        "token_hash",
        "code_or_prefix",
        "prefix",
        "suffix_width",
        "end_suffix",
        "max_height",
        "existing_policy",
        "page_budget",
        "rule_id",
        "rule_revision",
        "variant_priority_json",
        "provenance_type",
        "source_session_id",
        "source_revision",
        "source_items_hash",
        "direct_queue_key_hash",
        "direct_queue_request_hash",
        "auto_commit",
        "direct_code_key",
    )
    for row in rows:
        if str(row["mode"]) == "exact":
            try:
                display_code, _code_key = normalize_web_download_code(
                    row["code_or_prefix"]
                )
            except WebDownloadError as exc:
                raise MigrationError(
                    "web download batch catalog code is invalid"
                ) from exc
            if display_code != str(row["code_or_prefix"]):
                raise MigrationError(
                    "web download batch catalog code is invalid"
                )
        if row["direct_queue_key_hash"] is not None:
            try:
                validate_direct_queue_hash(
                    row["direct_queue_key_hash"],
                    "direct queue identity",
                )
                validate_direct_queue_hash(
                    row["direct_queue_request_hash"],
                    "direct queue request",
                )
            except WebDownloadBatchError as exc:
                raise MigrationError(
                    "web download direct queue identity is invalid"
                ) from exc
        try:
            variant_priority_from_json(row["variant_priority_json"])
        except WebDownloadBatchError as exc:
            raise MigrationError(
                "web download batch variant priority is invalid"
            ) from exc
        if bool(row["auto_commit"]):
            try:
                display_code, code_key = normalize_web_download_code(
                    row["code_or_prefix"]
                )
            except WebDownloadError as exc:
                raise MigrationError(
                    "automatic web download catalog code is invalid"
                ) from exc
            if display_code != str(row["code_or_prefix"]) or code_key != str(
                row["direct_code_key"]
            ):
                raise MigrationError("automatic web download catalog code is invalid")
        provenance_type = str(row["provenance_type"])
        source_values = (
            row["source_session_id"],
            row["source_revision"],
            row["source_items_hash"],
        )
        if provenance_type == SERIES_DISCOVERY_PROVENANCE:
            if any(value is not None for value in source_values):
                raise MigrationError("web download batch provenance is invalid")
        elif provenance_type == RESOURCE_SEARCH_SELECTION_PROVENANCE:
            selected_items = connection.execute(
                "SELECT code, code_key, available_variants_json, variant "
                "FROM web_download_batch_items WHERE batch_id = ? ORDER BY position",
                (str(row["batch_id"]),),
            ).fetchall()
            try:
                verify_selected_batch_provenance(row, selected_items)
            except WebDownloadBatchError as exc:
                raise MigrationError(
                    "web download batch provenance is invalid"
                ) from exc
            if (
                row["parent_batch_id"] is not None
                or row["continuation_batch_id"] is not None
                or row["next_start_suffix"] is not None
                or int(row["page_number"]) != 1
                or int(row["page_budget"]) != 1
            ):
                raise MigrationError("web download batch provenance is invalid")
        else:
            raise MigrationError("web download batch provenance is invalid")
    item_rows = connection.execute(
        "SELECT code, code_key, available_variants_json, variant "
        "FROM web_download_batch_items"
    ).fetchall()
    for item in item_rows:
        try:
            display_code, code_key = normalize_web_download_code(item["code"])
            available = variants_from_json(item["available_variants_json"])
            selected = normalize_web_download_variant(item["variant"])
        except (ValueError, WebDownloadError, WebDownloadBatchError) as exc:
            raise MigrationError("web download batch item variant is invalid") from exc
        if (
            display_code != str(item["code"])
            or code_key != str(item["code_key"])
            or selected not in available
        ):
            raise MigrationError("web download batch item variant is invalid")
    rule_rows = connection.execute(
        "SELECT variant_priority_json FROM web_download_batch_rules"
    ).fetchall()
    for rule in rule_rows:
        try:
            variant_priority_from_json(rule["variant_priority_json"])
        except WebDownloadBatchError as exc:
            raise MigrationError(
                "web download batch rule variant priority is invalid"
            ) from exc
    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check(web_download_auto_requests)"
    ).fetchall()
    if foreign_key_errors:
        raise MigrationError("automatic web download request foreign key is invalid")
    auto_requests = connection.execute(
        "SELECT r.key_hash, r.request_hash, r.batch_id AS request_batch_id, "
        "b.batch_id AS target_batch_id, b.auto_commit "
        "FROM web_download_auto_requests r "
        "LEFT JOIN web_download_batches b ON b.batch_id = r.batch_id"
    ).fetchall()
    for request in auto_requests:
        if request["target_batch_id"] is None or not bool(request["auto_commit"]):
            raise MigrationError("automatic web download request target is invalid")
        try:
            validate_direct_queue_hash(
                request["key_hash"],
                "automatic download identity",
            )
            validate_direct_queue_hash(
                request["request_hash"],
                "automatic download request",
            )
        except WebDownloadBatchError as exc:
            raise MigrationError(
                "automatic web download request identity is invalid"
            ) from exc
    missing_auto_request = connection.execute(
        "SELECT 1 FROM web_download_batches b WHERE b.auto_commit = 1 "
        "AND NOT EXISTS (SELECT 1 FROM web_download_auto_requests r "
        "WHERE r.batch_id = b.batch_id) LIMIT 1"
    ).fetchone()
    if missing_auto_request is not None:
        raise MigrationError("automatic web download request is missing")
    for row in rows:
        batch_id = str(row["batch_id"])
        root_id = str(row["root_chain_id"])
        root = by_id.get(root_id)
        page = int(row["page_number"])
        parent_id = (
            str(row["parent_batch_id"]) if row["parent_batch_id"] is not None else None
        )
        continuation_id = (
            str(row["continuation_batch_id"])
            if row["continuation_batch_id"] is not None
            else None
        )
        if (
            root is None
            or page < 1
            or page > int(row["page_budget"])
            or any(row[field] != root[field] for field in invariant_fields)
        ):
            raise MigrationError("web download batch chain state is invalid")
        if parent_id is None:
            if batch_id != root_id or page != 1:
                raise MigrationError("web download batch chain state is invalid")
        else:
            parent = by_id.get(parent_id)
            if (
                parent is None
                or str(parent["root_chain_id"]) != root_id
                or int(parent["page_number"]) + 1 != page
                or parent["continuation_batch_id"] is None
                or str(parent["continuation_batch_id"]) != batch_id
                or parent["next_start_suffix"] is None
                or row["start_suffix"] is None
                or str(parent["next_start_suffix"]) != str(row["start_suffix"])
            ):
                raise MigrationError("web download batch chain state is invalid")
        if continuation_id is not None:
            continuation = by_id.get(continuation_id)
            if (
                continuation is None
                or str(continuation["root_chain_id"]) != root_id
                or continuation["parent_batch_id"] is None
                or str(continuation["parent_batch_id"]) != batch_id
                or int(continuation["page_number"]) != page + 1
            ):
                raise MigrationError("web download batch chain state is invalid")
