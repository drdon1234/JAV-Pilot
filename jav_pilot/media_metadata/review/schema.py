"""SQLite schema creation, migrations and verification for metadata reviews."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping

from ...core.migrations import require_columns
from .drafts import normalize_field_value
from .errors import MediaMetadataReviewError, MediaMetadataReviewValidationError
from .fields import json_load, json_text, strict_bool, validated_code
from .models import IMAGE_SOURCES, METADATA_FIELDS, SNAPSHOT_SOURCES

def create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE media_metadata_reviews (
            review_id TEXT PRIMARY KEY CHECK (length(review_id) = 32),
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            relative_media_path TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (code_key, relative_media_path)
        )
        """,
        """
        CREATE TABLE media_metadata_review_snapshots (
            snapshot_id TEXT PRIMARY KEY CHECK (length(snapshot_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            source_id TEXT NOT NULL CHECK (source_id IN ('nfo', 'javbus', 'javdb', 'fc2', 'missav')),
            source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
            fetched_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """,
        "CREATE INDEX media_metadata_review_snapshots_lookup "
        "ON media_metadata_review_snapshots(review_id, source_id, fetched_at DESC)",
        """
        CREATE TABLE media_metadata_review_values (
            snapshot_id TEXT NOT NULL REFERENCES media_metadata_review_snapshots(snapshot_id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, field_name)
        )
        """,
        """
        CREATE TABLE media_metadata_review_images (
            image_id TEXT PRIMARY KEY CHECK (length(image_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('portrait', 'landscape')),
            source_id TEXT NOT NULL CHECK (source_id IN ('nfo', 'javbus', 'javdb', 'fc2', 'manual')),
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            width INTEGER NOT NULL CHECK (width > 0),
            height INTEGER NOT NULL CHECK (height > 0),
            fetched_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """,
        "CREATE INDEX media_metadata_review_images_lookup "
        "ON media_metadata_review_images(review_id, kind, fetched_at DESC)",
        """
        CREATE TABLE media_metadata_review_drafts (
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            manual_json TEXT,
            selected_source TEXT,
            locked INTEGER NOT NULL CHECK (locked IN (0, 1)),
            locked_value_json TEXT,
            locked_source TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (review_id, field_name)
        )
        """,
        """
        CREATE TABLE media_metadata_review_refetch (
            intent_id TEXT PRIMARY KEY CHECK (length(intent_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            sources_json TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            images_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'completed', 'failed', 'cancelled')),
            error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        "CREATE INDEX media_metadata_review_refetch_queue "
        "ON media_metadata_review_refetch(status, created_at)",
        """
        CREATE TABLE media_metadata_review_publications (
            publication_id TEXT PRIMARY KEY CHECK (length(publication_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            review_revision INTEGER NOT NULL CHECK (review_revision > 0),
            draft_json TEXT NOT NULL,
            artifacts_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """,
        "CREATE INDEX media_metadata_review_publications_lookup "
        "ON media_metadata_review_publications(review_id, created_at DESC)",
    )
    for statement in statements:
        connection.execute(statement)


def migrate_schema_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE media_metadata_reviews ADD COLUMN abandoned_revision INTEGER"
    )
    connection.execute(
        "ALTER TABLE media_metadata_reviews ADD COLUMN abandoned_at REAL"
    )
    connection.execute("DROP INDEX media_metadata_review_refetch_queue")
    connection.execute(
        "ALTER TABLE media_metadata_review_refetch "
        "RENAME TO media_metadata_review_refetch_v1"
    )
    connection.execute(
        """
        CREATE TABLE media_metadata_review_refetch (
            intent_id TEXT PRIMARY KEY CHECK (length(intent_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            sources_json TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            images_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
            ),
            error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO media_metadata_review_refetch "
        "SELECT * FROM media_metadata_review_refetch_v1"
    )
    connection.execute("DROP TABLE media_metadata_review_refetch_v1")
    connection.execute(
        "CREATE INDEX media_metadata_review_refetch_queue "
        "ON media_metadata_review_refetch(status, created_at)"
    )


def migrate_schema_v3(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE media_metadata_reviews "
        "ADD COLUMN abandon_generation INTEGER NOT NULL DEFAULT 0"
    )
    connection.execute("DROP INDEX media_metadata_review_refetch_queue")
    connection.execute(
        "ALTER TABLE media_metadata_review_refetch "
        "RENAME TO media_metadata_review_refetch_v2"
    )
    connection.execute(
        """
        CREATE TABLE media_metadata_review_refetch (
            intent_id TEXT PRIMARY KEY CHECK (length(intent_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            base_revision INTEGER NOT NULL CHECK (base_revision > 0),
            base_abandon_generation INTEGER NOT NULL CHECK (base_abandon_generation >= 0),
            sources_json TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            images_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
            ),
            error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO media_metadata_review_refetch (
            intent_id, review_id, base_revision, base_abandon_generation,
            sources_json, fields_json, images_json, status, error_code,
            created_at, updated_at
        )
        SELECT f.intent_id, f.review_id, r.revision, r.abandon_generation,
               f.sources_json, f.fields_json, f.images_json,
               CASE WHEN f.status IN ('queued', 'running') THEN 'failed' ELSE f.status END,
               CASE WHEN f.status IN ('queued', 'running')
                    THEN 'service_restarted' ELSE f.error_code END,
               f.created_at, f.updated_at
        FROM media_metadata_review_refetch_v2 f
        JOIN media_metadata_reviews r ON r.review_id = f.review_id
        """
    )
    connection.execute("DROP TABLE media_metadata_review_refetch_v2")
    connection.execute(
        "CREATE INDEX media_metadata_review_refetch_queue "
        "ON media_metadata_review_refetch(status, created_at)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX media_metadata_review_refetch_one_active "
        "ON media_metadata_review_refetch(review_id) "
        "WHERE status IN ('queued', 'running')"
    )


def migrate_schema_v4(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    identities: dict[tuple[str, str], str] = {}
    review_identities: dict[str, tuple[str, str]] = {}
    updates: list[tuple[str, str, str, str]] = []
    rows = connection.execute(
        "SELECT review_id, code, code_key, relative_media_path "
        "FROM media_metadata_reviews ORDER BY review_id"
    ).fetchall()
    for row in rows:
        try:
            display_code, code_key = validated_code(row["code"])
        except MediaMetadataReviewValidationError as exc:
            raise MediaMetadataReviewError(
                "metadata review catalog code is invalid"
            ) from exc
        review_id = str(row["review_id"])
        review_identities[review_id] = (display_code, code_key)
        identity = (code_key, str(row["relative_media_path"]))
        collided = identities.setdefault(identity, review_id)
        if collided != review_id:
            raise MediaMetadataReviewError(
                "metadata reviews collide after catalog code normalization"
            )
        if str(row["code"]) != display_code or str(row["code_key"]) != code_key:
            updates.append(
                (
                    review_id,
                    str(row["relative_media_path"]),
                    display_code,
                    code_key,
                )
            )

    publication_updates: list[tuple[str, str]] = []
    for row in connection.execute(
        "SELECT publication_id, review_id, draft_json "
        "FROM media_metadata_review_publications ORDER BY publication_id"
    ).fetchall():
        publication_id = str(row["publication_id"])
        review_id = str(row["review_id"])
        expected_identity = review_identities.get(review_id)
        if expected_identity is None:
            raise MediaMetadataReviewError(
                "metadata review publication identity is invalid"
            )
        migrated_json = _migrate_publication_draft_json(
            row["draft_json"],
            expected_identity=expected_identity,
        )
        if migrated_json != str(row["draft_json"]):
            publication_updates.append((migrated_json, publication_id))

    reserved = {
        (str(row["code_key"]), str(row["relative_media_path"])) for row in rows
    } | set(identities)
    temporary_keys: dict[str, str] = {}
    for review_id, relative_path, _display_code, _code_key in updates:
        for salt in range(1024):
            temporary_key = "MIGRATION" + hashlib.sha256(
                f"{review_id}\0{salt}".encode("ascii")
            ).hexdigest().upper()
            if (temporary_key, relative_path) not in reserved:
                reserved.add((temporary_key, relative_path))
                temporary_keys[review_id] = temporary_key
                break
        else:
            raise MediaMetadataReviewError(
                "metadata review identity migration collided"
            )
    connection.executemany(
        "UPDATE media_metadata_reviews SET code_key = ? WHERE review_id = ?",
        ((temporary_keys[review_id], review_id) for review_id, *_rest in updates),
    )
    connection.executemany(
        "UPDATE media_metadata_reviews SET code = ?, code_key = ? "
        "WHERE review_id = ?",
        (
            (display_code, code_key, review_id)
            for review_id, _relative_path, display_code, code_key in updates
        ),
    )
    connection.executemany(
        "UPDATE media_metadata_review_publications SET draft_json = ? "
        "WHERE publication_id = ?",
        publication_updates,
    )


def migrate_schema_v5(connection: sqlite3.Connection) -> None:
    """Extend persisted source checks with the FC2 metadata provider.

    SQLite cannot alter a CHECK constraint in place.  Rebuild the two tables
    whose source identifiers are constrained, copying their rows in stable
    row order so the existing latest-value tie breakers remain deterministic.
    The dependent values table is rebuilt together with snapshots so foreign
    keys stay enabled for the whole migration transaction.
    """

    connection.execute(
        """
        CREATE TABLE media_metadata_review_snapshots_v5 (
            snapshot_id TEXT PRIMARY KEY CHECK (length(snapshot_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            source_id TEXT NOT NULL CHECK (
                source_id IN ('nfo', 'javbus', 'javdb', 'fc2', 'missav')
            ),
            source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
            fetched_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO media_metadata_review_snapshots_v5 (
            snapshot_id, review_id, source_id, source_digest, fetched_at, created_at
        )
        SELECT snapshot_id, review_id, source_id, source_digest, fetched_at, created_at
        FROM media_metadata_review_snapshots
        ORDER BY rowid
        """
    )
    connection.execute(
        """
        CREATE TABLE media_metadata_review_values_v5 (
            snapshot_id TEXT NOT NULL
                REFERENCES media_metadata_review_snapshots_v5(snapshot_id)
                ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, field_name)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO media_metadata_review_values_v5 (snapshot_id, field_name, value_json)
        SELECT snapshot_id, field_name, value_json
        FROM media_metadata_review_values
        ORDER BY rowid
        """
    )
    connection.execute("DROP TABLE media_metadata_review_values")
    connection.execute("DROP TABLE media_metadata_review_snapshots")
    connection.execute(
        "ALTER TABLE media_metadata_review_snapshots_v5 "
        "RENAME TO media_metadata_review_snapshots"
    )
    connection.execute(
        "ALTER TABLE media_metadata_review_values_v5 "
        "RENAME TO media_metadata_review_values"
    )
    connection.execute(
        "CREATE INDEX media_metadata_review_snapshots_lookup "
        "ON media_metadata_review_snapshots(review_id, source_id, fetched_at DESC)"
    )

    connection.execute(
        """
        CREATE TABLE media_metadata_review_images_v5 (
            image_id TEXT PRIMARY KEY CHECK (length(image_id) = 32),
            review_id TEXT NOT NULL REFERENCES media_metadata_reviews(review_id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('portrait', 'landscape')),
            source_id TEXT NOT NULL CHECK (
                source_id IN ('nfo', 'javbus', 'javdb', 'fc2', 'manual')
            ),
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            width INTEGER NOT NULL CHECK (width > 0),
            height INTEGER NOT NULL CHECK (height > 0),
            fetched_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO media_metadata_review_images_v5 (
            image_id, review_id, kind, source_id, sha256,
            width, height, fetched_at, created_at
        )
        SELECT image_id, review_id, kind, source_id, sha256,
               width, height, fetched_at, created_at
        FROM media_metadata_review_images
        ORDER BY rowid
        """
    )
    connection.execute("DROP TABLE media_metadata_review_images")
    connection.execute(
        "ALTER TABLE media_metadata_review_images_v5 "
        "RENAME TO media_metadata_review_images"
    )
    connection.execute(
        "CREATE INDEX media_metadata_review_images_lookup "
        "ON media_metadata_review_images(review_id, kind, fetched_at DESC)"
    )


def migrate_catalog_sources(connection: sqlite3.Connection) -> None:
    """Expand source CHECKs transactionally, retaining IDs, row order and BLOBs."""
    source_sets = {
        "media_metadata_review_snapshots": SNAPSHOT_SOURCES,
        "media_metadata_review_images": IMAGE_SOURCES,
    }
    tables = ("media_metadata_review_snapshots", "media_metadata_review_values",
              "media_metadata_review_images")
    replacements = {table: f"{table}_source_migration" for table in tables}
    schema_objects = []
    for table in tables:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not row or not row[0]:
            raise MediaMetadataReviewError("metadata review source migration table is missing")
        sql = str(row[0])
        if table in source_sets:
            quoted = ", ".join(f"'{source}'" for source in sorted(source_sets[table]))
            sql, count = re.subn(r"source_id\s+IN\s*\([^)]*\)", f"source_id IN ({quoted})", sql, flags=re.I)
            if count != 1:
                raise MediaMetadataReviewError("metadata review source constraint is invalid")
        for original, temporary in replacements.items():
            sql = re.sub(rf"\b{re.escape(original)}\b", temporary, sql)
        connection.execute(sql)
        columns = [str(column[1]) for column in connection.execute(f"PRAGMA table_info({table})")]
        projection = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
        connection.execute(
            f"INSERT INTO {replacements[table]} (rowid, {projection}) "
            f"SELECT rowid, {projection} FROM {table} ORDER BY rowid"
        )
        schema_objects.extend(str(item[0]) for item in connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name", (table,)
        ).fetchall())
    # Only values references snapshots. Drop child first with foreign keys enabled.
    for table in (tables[1], tables[0], tables[2]):
        connection.execute(f"DROP TABLE {table}")
    for table in tables:
        connection.execute(f"ALTER TABLE {replacements[table]} RENAME TO {table}")
    for statement in schema_objects:
        connection.execute(statement)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MediaMetadataReviewError("metadata review source migration broke a reference")


def verify_catalog_sources(connection: sqlite3.Connection) -> None:
    verify_schema(connection)
    for table, sources in {
        "media_metadata_review_snapshots": SNAPSHOT_SOURCES,
        "media_metadata_review_images": IMAGE_SOURCES,
    }.items():
        row = connection.execute("SELECT sql FROM sqlite_master WHERE name = ? AND type = 'table'", (table,)).fetchone()
        sql = str(row[0] or "") if row else ""
        if not all(f"'{source}'" in sql for source in sources):
            raise MediaMetadataReviewError("metadata review source schema is incomplete")
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE source_id NOT IN ({','.join('?' for _ in sources)}) LIMIT 1",
            tuple(sources),
        ).fetchone() is not None:
            raise MediaMetadataReviewError("metadata review source identifier is invalid")


def _migrate_publication_draft_json(
    value: object,
    *,
    expected_identity: tuple[str, str],
) -> str:
    try:
        draft = json_load(str(value))
    except MediaMetadataReviewError as exc:
        raise MediaMetadataReviewError(
            "metadata review publication draft is invalid"
        ) from exc
    if not isinstance(draft, Mapping) or set(draft) != {"code", "fields"}:
        raise MediaMetadataReviewError(
            "metadata review publication draft is invalid"
        )
    try:
        _display_code, code_key = validated_code(draft.get("code"))
    except MediaMetadataReviewValidationError as exc:
        raise MediaMetadataReviewError(
            "metadata review publication catalog code is invalid"
        ) from exc
    if code_key != expected_identity[1]:
        raise MediaMetadataReviewError(
            "metadata review publication catalog identity is inconsistent"
        )
    fields = draft.get("fields")
    if not isinstance(fields, Mapping) or set(fields) != set(METADATA_FIELDS):
        raise MediaMetadataReviewError(
            "metadata review publication fields are invalid"
        )
    migrated_fields: dict[str, dict[str, object]] = {}
    for field_name in METADATA_FIELDS:
        field = fields.get(field_name)
        if not isinstance(field, Mapping) or set(field) != {
            "value",
            "source",
            "locked",
        }:
            raise MediaMetadataReviewError(
                "metadata review publication field is invalid"
            )
        source = str(field.get("source") or "").strip()
        if source not in SNAPSHOT_SOURCES | {"default", "manual"}:
            raise MediaMetadataReviewError(
                "metadata review publication provenance is invalid"
            )
        try:
            normalized_value = normalize_field_value(
                field_name,
                field.get("value"),
                allow_none=True,
            )
            locked = strict_bool(field.get("locked"), "publication field lock")
        except MediaMetadataReviewValidationError as exc:
            raise MediaMetadataReviewError(
                "metadata review publication field is invalid"
            ) from exc
        migrated_fields[field_name] = {
            "value": normalized_value,
            "source": source,
            "locked": locked,
        }
    return json_text(
        {
            "code": expected_identity[0],
            "fields": migrated_fields,
        }
    )


def verify_schema_v1(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "media_metadata_reviews",
        ("review_id", "code_key", "relative_media_path", "revision"),
    )
    require_columns(
        connection,
        "media_metadata_review_snapshots",
        ("snapshot_id", "review_id", "source_id", "source_digest", "fetched_at"),
    )
    require_columns(
        connection,
        "media_metadata_review_drafts",
        ("review_id", "field_name", "manual_json", "locked", "locked_value_json"),
    )
    require_columns(
        connection,
        "media_metadata_review_refetch",
        (
            "intent_id",
            "review_id",
            "sources_json",
            "fields_json",
            "images_json",
            "status",
            "error_code",
        ),
    )
    require_columns(
        connection,
        "media_metadata_review_publications",
        (
            "publication_id",
            "review_id",
            "review_revision",
            "draft_json",
            "artifacts_json",
        ),
    )


def verify_schema_v2(connection: sqlite3.Connection) -> None:
    verify_schema_v1(connection)
    require_columns(
        connection,
        "media_metadata_reviews",
        ("abandoned_revision", "abandoned_at"),
    )
    row = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'media_metadata_review_refetch'"
    ).fetchone()
    if row is None or "'running'" not in str(row[0] or ""):
        raise MediaMetadataReviewError("metadata review refetch schema is incomplete")


def verify_schema(connection: sqlite3.Connection) -> None:
    verify_schema_v2(connection)
    require_columns(
        connection,
        "media_metadata_reviews",
        ("abandon_generation",),
    )
    require_columns(
        connection,
        "media_metadata_review_refetch",
        ("base_revision", "base_abandon_generation"),
    )
    active_index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'media_metadata_review_refetch_one_active'"
    ).fetchone()
    if active_index is None or "WHERE status IN" not in str(active_index[0] or ""):
        raise MediaMetadataReviewError("metadata review refetch schema is incomplete")
    for row in connection.execute(
        "SELECT code, code_key FROM media_metadata_reviews"
    ).fetchall():
        try:
            expected_code, expected_key = validated_code(row["code"])
        except MediaMetadataReviewValidationError as exc:
            raise MediaMetadataReviewError(
                "metadata review catalog code is invalid"
            ) from exc
        if (
            str(row["code"]) != expected_code
            or str(row["code_key"]) != expected_key
        ):
            raise MediaMetadataReviewError(
                "metadata review catalog identity is invalid"
            )


def verify_schema_v5(connection: sqlite3.Connection) -> None:
    verify_schema(connection)
    expected_sources = {
        "media_metadata_review_snapshots": {"nfo", "javbus", "javdb", "fc2", "missav"},
        "media_metadata_review_images": {"nfo", "javbus", "javdb", "fc2", "manual"},
    }
    for table, sources in expected_sources.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        sql = " ".join(str(row[0] or "").split()).casefold() if row else ""
        if not sql or not all(f"'{source}'" in sql for source in sources):
            raise MediaMetadataReviewError(
                f"{table} source schema is incomplete"
            )
        invalid = connection.execute(
            f"SELECT 1 FROM {table} WHERE source_id NOT IN "
            f"({','.join('?' for _ in sources)}) LIMIT 1",
            tuple(sources),
        ).fetchone()
        if invalid is not None:
            raise MediaMetadataReviewError(
                f"{table} contains an invalid source identifier"
            )
