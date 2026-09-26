"""SQLite store for metadata reviews, drafts and publications."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from ...config.source_catalog import METADATA_PROFILES
from ...core.migrations import SQLiteMigration, migrate_sqlite
from ..publish import safe_relative_media_path
from .drafts import (
    latest_images,
    latest_source_values,
    load_draft_rows,
    normalize_field_value,
    normalized_snapshot_fields,
    resolve_field,
    source_choice,
    validated_field_name,
)
from .errors import (
    MediaMetadataReviewConflict,
    MediaMetadataReviewNotFound,
    MediaMetadataReviewValidationError,
)
from .fields import (
    bounded_int,
    enum,
    hex_id,
    json_digest,
    json_load,
    json_text,
    reject_sensitive_text,
    strict_bool,
    timestamp,
    validated_code,
    validated_error_code,
    validated_sha256,
    validated_source_id,
)
from .journal import publication_artifact
from .models import (
    CURRENT_SCHEMA_VERSION,
    IMAGE_KINDS,
    IMAGE_SOURCES,
    MAX_REVIEW_IMAGE_PIXELS,
    METADATA_FIELDS,
    NON_DESCRIPTION_FIELDS,
    REFETCH_CONFLICT_ERROR_CODE,
    REFETCH_RESTART_ERROR_CODE,
    REMOTE_SOURCES,
    SCHEMA_COMPONENT,
    SNAPSHOT_SOURCES,
    SOURCE_FIELDS,
)
from .schema import (
    create_schema,
    migrate_catalog_sources,
    migrate_schema_v2,
    migrate_schema_v3,
    migrate_schema_v4,
    migrate_schema_v5,
    verify_catalog_sources,
    verify_schema,
    verify_schema_v1,
    verify_schema_v2,
    verify_schema_v5,
)

class MediaMetadataReviewStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise MediaMetadataReviewValidationError(
                "metadata review database path must be absolute"
            )
        self._clock = clock
        self._id_factory = id_factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_review(
        self, code: object, relative_media_path: object
    ) -> dict[str, object]:
        display_code, code_key = validated_code(code)
        relative = safe_relative_media_path(relative_media_path)
        reject_sensitive_text(relative, "media path")
        now = timestamp(self._clock())
        review_id = hex_id(self._id_factory(), "review id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM media_metadata_reviews "
                "WHERE code_key = ? AND relative_media_path = ?",
                (code_key, relative),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO media_metadata_reviews (
                        review_id, code, code_key, relative_media_path,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (review_id, display_code, code_key, relative, now, now),
                )
            else:
                review_id = str(existing["review_id"])
            connection.commit()
        return self.get_review(review_id)

    def get_review(self, review_id: object) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        with self._connect() as connection:
            row = _review_row(connection, clean_id)
            return _public_review(connection, row)

    def capture_source_snapshot(
        self,
        review_id: object,
        source_id: object,
        fields: Mapping[str, object],
        *,
        fetched_at: float | None = None,
        source_digest: str | None = None,
        deduplicate: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        source = validated_source_id(source_id, SNAPSHOT_SOURCES)
        normalized = normalized_snapshot_fields(source, fields)
        fetched = timestamp(self._clock() if fetched_at is None else fetched_at)
        now = timestamp(self._clock())
        digest = (
            validated_sha256(source_digest, "source digest")
            if source_digest is not None
            else json_digest(normalized)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = _review_row(connection, clean_id)
            _require_revision(review, expected_revision)
            if deduplicate:
                existing = connection.execute(
                    "SELECT * FROM media_metadata_review_snapshots "
                    "WHERE review_id = ? AND source_id = ? AND source_digest = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (clean_id, source, digest),
                ).fetchone()
                if existing is not None:
                    existing_fields = _snapshot_values(
                        connection, str(existing["snapshot_id"])
                    )
                    connection.commit()
                    return _snapshot_public(existing, existing_fields)
            snapshot_id = hex_id(self._id_factory(), "snapshot id")
            connection.execute(
                """
                INSERT INTO media_metadata_review_snapshots (
                    snapshot_id, review_id, source_id, source_digest,
                    fetched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, clean_id, source, digest, fetched, now),
            )
            for field_name, value in normalized.items():
                connection.execute(
                    "INSERT INTO media_metadata_review_values "
                    "(snapshot_id, field_name, value_json) VALUES (?, ?, ?)",
                    (snapshot_id, field_name, json_text(value)),
                )
            _bump_revision(connection, clean_id, now)
            connection.commit()
        return {
            "snapshot_id": snapshot_id,
            "review_id": clean_id,
            "source_id": source,
            "source_digest": digest,
            "fetched_at": fetched,
            "fields": normalized,
        }

    def capture_image_snapshot(
        self,
        review_id: object,
        *,
        kind: object,
        source_id: object,
        sha256: object,
        width: object,
        height: object,
        fetched_at: float | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        clean_kind = enum(kind, IMAGE_KINDS, "image kind")
        source = validated_source_id(source_id, IMAGE_SOURCES)
        digest = validated_sha256(sha256, "image digest")
        clean_width = bounded_int(width, "image width", 1, 20_000)
        clean_height = bounded_int(height, "image height", 1, 20_000)
        if clean_width * clean_height > MAX_REVIEW_IMAGE_PIXELS:
            raise MediaMetadataReviewValidationError(
                "metadata review image is too large"
            )
        fetched = timestamp(self._clock() if fetched_at is None else fetched_at)
        now = timestamp(self._clock())
        image_id = hex_id(self._id_factory(), "image snapshot id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = _review_row(connection, clean_id)
            _require_revision(review, expected_revision)
            connection.execute(
                """
                INSERT INTO media_metadata_review_images (
                    image_id, review_id, kind, source_id, sha256,
                    width, height, fetched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    clean_id,
                    clean_kind,
                    source,
                    digest,
                    clean_width,
                    clean_height,
                    fetched,
                    now,
                ),
            )
            _bump_revision(connection, clean_id, now)
            connection.commit()
        return {
            "image_id": image_id,
            "review_id": clean_id,
            "kind": clean_kind,
            "source_id": source,
            "sha256": digest,
            "width": clean_width,
            "height": clean_height,
            "fetched_at": fetched,
        }

    def update_draft(
        self,
        review_id: object,
        *,
        manual_values: Mapping[str, object] | None = None,
        source_choices: Mapping[str, object] | None = None,
        locks: Mapping[str, object] | None = None,
        clear_manual: Iterable[str] = (),
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        manual = dict(manual_values or {})
        choices = dict(source_choices or {})
        lock_values = dict(locks or {})
        clears = tuple(validated_field_name(field_name) for field_name in clear_manual)
        if set(manual) & set(clears):
            raise MediaMetadataReviewValidationError(
                "metadata review manual field cannot be set and cleared together"
            )
        for field_name in (*manual, *choices, *lock_values):
            validated_field_name(field_name)
        if not manual and not choices and not lock_values and not clears:
            raise MediaMetadataReviewValidationError(
                "metadata review draft is unchanged"
            )
        normalized_manual = {
            validated_field_name(field_name): normalize_field_value(
                validated_field_name(field_name), value, allow_none=True
            )
            for field_name, value in manual.items()
        }
        normalized_choices = {
            validated_field_name(field_name): source_choice(validated_field_name(field_name), value)
            for field_name, value in choices.items()
        }
        normalized_locks = {
            validated_field_name(field_name): strict_bool(value, "field lock")
            for field_name, value in lock_values.items()
        }
        now = timestamp(self._clock())
        touched = (
            set(normalized_manual)
            | set(normalized_choices)
            | set(normalized_locks)
            | set(clears)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = _review_row(connection, clean_id)
            _require_revision(review, expected_revision)
            source_values = latest_source_values(connection, clean_id)
            draft_rows = load_draft_rows(connection, clean_id)
            for field_name in sorted(touched):
                current = dict(
                    draft_rows.get(field_name)
                    or {
                        "manual_json": None,
                        "selected_source": None,
                        "locked": 0,
                        "locked_value_json": None,
                        "locked_source": None,
                    }
                )
                if field_name in normalized_manual:
                    current["manual_json"] = json_text(normalized_manual[field_name])
                if field_name in clears:
                    current["manual_json"] = None
                if field_name in normalized_choices:
                    current["selected_source"] = normalized_choices[field_name]
                if field_name in normalized_locks:
                    current["locked"] = 1 if normalized_locks[field_name] else 0
                if bool(current["locked"]):
                    locked_value, locked_source = resolve_field(
                        field_name,
                        str(review["code"]),
                        source_values,
                        current,
                        ignore_lock=True,
                    )
                    current["locked_value_json"] = json_text(locked_value)
                    current["locked_source"] = locked_source
                else:
                    current["locked_value_json"] = None
                    current["locked_source"] = None
                connection.execute(
                    """
                    INSERT INTO media_metadata_review_drafts (
                        review_id, field_name, manual_json, selected_source,
                        locked, locked_value_json, locked_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(review_id, field_name) DO UPDATE SET
                        manual_json = excluded.manual_json,
                        selected_source = excluded.selected_source,
                        locked = excluded.locked,
                        locked_value_json = excluded.locked_value_json,
                        locked_source = excluded.locked_source,
                        updated_at = excluded.updated_at
                    """,
                    (
                        clean_id,
                        field_name,
                        current["manual_json"],
                        current["selected_source"],
                        current["locked"],
                        current["locked_value_json"],
                        current["locked_source"],
                        now,
                    ),
                )
            _bump_revision(connection, clean_id, now)
            connection.commit()
        return self.get_review(clean_id)

    def abandon_review(
        self,
        review_id: object,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        now = timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = _review_row(connection, clean_id)
            if expected_revision is None:
                connection.rollback()
                raise MediaMetadataReviewConflict(
                    "metadata review revision is required"
                )
            _require_revision(review, expected_revision)
            if _review_is_abandoned(review):
                connection.commit()
                return _public_review(connection, review)
            next_revision = int(review["revision"]) + 1
            connection.execute(
                "UPDATE media_metadata_reviews "
                "SET revision = ?, abandoned_revision = ?, abandoned_at = ?, "
                "abandon_generation = abandon_generation + 1, updated_at = ? "
                "WHERE review_id = ?",
                (next_revision, next_revision, now, now, clean_id),
            )
            connection.commit()
        return self.get_review(clean_id)

    def request_refetch(
        self,
        review_id: object,
        *,
        sources: Sequence[object] = (),
        fields: Sequence[object] = (),
        images: Sequence[object] = (),
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        clean_fields = tuple(dict.fromkeys(validated_field_name(item) for item in fields))
        clean_images = tuple(
            dict.fromkeys(enum(item, IMAGE_KINDS, "image kind") for item in images)
        )
        if not clean_fields and not clean_images:
            raise MediaMetadataReviewValidationError(
                "metadata review refetch intent is empty"
            )
        clean_sources = tuple(
            dict.fromkeys(validated_source_id(item, REMOTE_SOURCES) for item in sources)
        )
        intent_id = hex_id(self._id_factory(), "refetch intent id")
        now = timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = _review_row(connection, clean_id)
            if not clean_sources:
                defaults: list[str] = []
                if set(clean_fields) & NON_DESCRIPTION_FIELDS or clean_images:
                    defaults.extend(("javbus", "javdb"))
                    if str(review["code_key"] or "").startswith("FC2PPV"):
                        defaults.append("fc2")
                if "description" in clean_fields:
                    defaults.append("missav")
                clean_sources = tuple(defaults)
            if any(
                not any(
                    field_name in SOURCE_FIELDS[source] for source in clean_sources
                )
                for field_name in clean_fields
            ):
                connection.rollback()
                raise MediaMetadataReviewValidationError(
                    "metadata review source cannot provide a requested field"
                )
            if clean_images and not set(clean_sources) & METADATA_PROFILES:
                connection.rollback()
                raise MediaMetadataReviewValidationError(
                    "metadata review image refetch requires a metadata source"
                )
            if expected_revision is None:
                connection.rollback()
                raise MediaMetadataReviewConflict(
                    "metadata review revision is required"
                )
            _require_revision(review, expected_revision)
            active = connection.execute(
                "SELECT 1 FROM media_metadata_review_refetch "
                "WHERE review_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (clean_id,),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise MediaMetadataReviewConflict(
                    "metadata review refetch is already running"
                )
            connection.execute(
                """
                INSERT INTO media_metadata_review_refetch (
                    intent_id, review_id, base_revision, base_abandon_generation,
                    sources_json, fields_json, images_json, status, error_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)
                """,
                (
                    intent_id,
                    clean_id,
                    int(review["revision"]),
                    int(review["abandon_generation"]),
                    json_text(list(clean_sources)),
                    json_text(list(clean_fields)),
                    json_text(list(clean_images)),
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get_refetch_intent(intent_id)

    def get_refetch_intent(self, intent_id: object) -> dict[str, object]:
        clean_id = hex_id(intent_id, "refetch intent id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_metadata_review_refetch WHERE intent_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            raise MediaMetadataReviewNotFound(
                "metadata review refetch intent was not found"
            )
        return _refetch_public(row)

    def claim_refetch_intent(self, intent_id: object) -> dict[str, object]:
        clean_id = hex_id(intent_id, "refetch intent id")
        now = timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM media_metadata_review_refetch WHERE intent_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise MediaMetadataReviewNotFound(
                    "metadata review refetch intent was not found"
                )
            if str(row["status"]) != "queued":
                raise MediaMetadataReviewConflict(
                    "metadata review refetch intent cannot be claimed"
                )
            review = _review_row(connection, str(row["review_id"]))
            if not _refetch_revision_matches(review, row):
                connection.execute(
                    "UPDATE media_metadata_review_refetch "
                    "SET status = 'failed', error_code = ?, updated_at = ? "
                    "WHERE intent_id = ? AND status = 'queued'",
                    (REFETCH_CONFLICT_ERROR_CODE, now, clean_id),
                )
                connection.commit()
                raise MediaMetadataReviewConflict(
                    "metadata review changed before refetch started"
                )
            connection.execute(
                "UPDATE media_metadata_review_refetch "
                "SET status = 'running', error_code = NULL, updated_at = ? "
                "WHERE intent_id = ?",
                (now, clean_id),
            )
            connection.commit()
        return self.get_refetch_intent(clean_id)

    def recover_refetch_intents(self) -> int:
        now = timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE media_metadata_review_refetch "
                "SET status = 'failed', error_code = ?, updated_at = ? "
                "WHERE status IN ('queued', 'running')",
                (REFETCH_RESTART_ERROR_CODE, now),
            )
            recovered = max(0, int(cursor.rowcount))
            connection.commit()
        return recovered

    def complete_refetch_intent(
        self,
        intent_id: object,
        *,
        status: object,
        error_code: object | None = None,
        expected_revision: int | None = None,
        expected_abandon_generation: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(intent_id, "refetch intent id")
        clean_status = enum(
            status, {"completed", "failed", "cancelled"}, "refetch status"
        )
        clean_error = validated_error_code(error_code) if error_code is not None else None
        if clean_status == "failed" and clean_error is None:
            raise MediaMetadataReviewValidationError(
                "failed metadata refetch requires an error code"
            )
        if clean_status != "failed" and clean_error is not None:
            raise MediaMetadataReviewValidationError(
                "metadata refetch error code is only valid for failures"
            )
        now = timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM media_metadata_review_refetch WHERE intent_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise MediaMetadataReviewNotFound(
                    "metadata review refetch intent was not found"
                )
            if str(row["status"]) != "running":
                raise MediaMetadataReviewConflict(
                    "metadata review refetch intent is already complete"
                )
            if (expected_revision is None) != (expected_abandon_generation is None):
                connection.rollback()
                raise MediaMetadataReviewValidationError(
                    "metadata refetch completion revision is incomplete"
                )
            if expected_revision is not None:
                review = _review_row(connection, str(row["review_id"]))
                if not _review_state_matches(
                    review,
                    revision=expected_revision,
                    abandon_generation=expected_abandon_generation,
                ):
                    connection.execute(
                        "UPDATE media_metadata_review_refetch "
                        "SET status = 'failed', error_code = ?, updated_at = ? "
                        "WHERE intent_id = ? AND status = 'running'",
                        (REFETCH_CONFLICT_ERROR_CODE, now, clean_id),
                    )
                    connection.commit()
                    raise MediaMetadataReviewConflict(
                        "metadata review changed during refetch"
                    )
            connection.execute(
                "UPDATE media_metadata_review_refetch "
                "SET status = ?, error_code = ?, updated_at = ? WHERE intent_id = ?",
                (clean_status, clean_error, now, clean_id),
            )
            connection.commit()
        return self.get_refetch_intent(clean_id)

    def list_publications(self, review_id: object) -> list[dict[str, object]]:
        clean_id = hex_id(review_id, "review id")
        with self._connect() as connection:
            _review_row(connection, clean_id)
            rows = connection.execute(
                "SELECT * FROM media_metadata_review_publications "
                "WHERE review_id = ? ORDER BY created_at DESC, publication_id DESC",
                (clean_id,),
            ).fetchall()
        return [_publication_public(row) for row in rows]

    def reserve_publication_id(self) -> str:
        return hex_id(self._id_factory(), "publication id")

    def find_publication(self, publication_id: object) -> dict[str, object] | None:
        clean_id = hex_id(publication_id, "publication id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_metadata_review_publications "
                "WHERE publication_id = ?",
                (clean_id,),
            ).fetchone()
        return _publication_public(row) if row is not None else None

    def record_publication(
        self,
        review_id: object,
        *,
        expected_revision: int,
        draft: Mapping[str, object],
        artifacts: Sequence[Mapping[str, object]],
        publication_id: object | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        clean_draft = _publication_draft(draft)
        clean_artifacts = [publication_artifact(item) for item in artifacts]
        if not clean_artifacts:
            raise MediaMetadataReviewValidationError(
                "metadata review publication is empty"
            )
        clean_publication_id = (
            self.reserve_publication_id()
            if publication_id is None
            else hex_id(publication_id, "publication id")
        )
        now = timestamp(self._clock())
        published_revision = expected_revision + 1
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = _review_row(connection, clean_id)
            _require_revision(review, expected_revision)
            connection.execute(
                """
                INSERT INTO media_metadata_review_publications (
                    publication_id, review_id, review_revision,
                    draft_json, artifacts_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_publication_id,
                    clean_id,
                    published_revision,
                    json_text(clean_draft),
                    json_text(clean_artifacts),
                    now,
                ),
            )
            _bump_revision(connection, clean_id, now)
            connection.commit()
        return {
            "publication_id": clean_publication_id,
            "review_id": clean_id,
            "review_revision": published_revision,
            "draft": clean_draft,
            "artifacts": clean_artifacts,
            "created_at": now,
        }

    def _initialize(self) -> None:
        with closing(
            sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            migrate_sqlite(
                connection,
                component=SCHEMA_COMPONENT,
                current_version=CURRENT_SCHEMA_VERSION,
                migrations=(
                    SQLiteMigration(1, create_schema, verify_schema_v1),
                    SQLiteMigration(2, migrate_schema_v2, verify_schema_v2),
                    SQLiteMigration(3, migrate_schema_v3, verify_schema),
                    SQLiteMigration(4, migrate_schema_v4, verify_schema),
                    SQLiteMigration(5, migrate_schema_v5, verify_schema_v5),
                    SQLiteMigration(6, migrate_catalog_sources, verify_catalog_sources),
                ),
                clock=self._clock,
                verify_current=verify_catalog_sources,
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()


def _review_row(connection: sqlite3.Connection, review_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM media_metadata_reviews WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    if row is None:
        raise MediaMetadataReviewNotFound("metadata review was not found")
    return row


def _require_revision(row: sqlite3.Row, expected: int | None) -> None:
    if expected is None:
        return
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected != int(row["revision"])
    ):
        raise MediaMetadataReviewConflict("metadata review revision changed")


def _review_state_matches(
    row: sqlite3.Row,
    *,
    revision: object,
    abandon_generation: object,
) -> bool:
    return bool(
        not isinstance(revision, bool)
        and isinstance(revision, int)
        and not isinstance(abandon_generation, bool)
        and isinstance(abandon_generation, int)
        and revision == int(row["revision"])
        and abandon_generation == int(row["abandon_generation"])
    )


def _refetch_revision_matches(review: sqlite3.Row, intent: sqlite3.Row) -> bool:
    return _review_state_matches(
        review,
        revision=int(intent["base_revision"]),
        abandon_generation=int(intent["base_abandon_generation"]),
    )


def _review_is_abandoned(row: sqlite3.Row) -> bool:
    abandoned_revision = row["abandoned_revision"]
    return abandoned_revision is not None and int(abandoned_revision) == int(
        row["revision"]
    )


def _bump_revision(connection: sqlite3.Connection, review_id: str, now: float) -> None:
    connection.execute(
        "UPDATE media_metadata_reviews "
        "SET revision = revision + 1, updated_at = ? WHERE review_id = ?",
        (now, review_id),
    )


def _public_review(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, object]:
    review_id = str(row["review_id"])
    abandoned = _review_is_abandoned(row)
    source_values = latest_source_values(connection, review_id)
    draft_rows = load_draft_rows(connection, review_id)
    fields: dict[str, dict[str, object]] = {}
    for field_name in METADATA_FIELDS:
        draft = draft_rows.get(field_name) or {}
        final_value, final_source = resolve_field(
            field_name,
            str(row["code"]),
            source_values,
            draft,
        )
        values = {
            source_id: dict(value)
            for source_id, value in source_values.get(field_name, {}).items()
        }
        fields[field_name] = {
            "sources": values,
            "manual_set": draft.get("manual_json") is not None,
            "manual_value": (
                json_load(str(draft["manual_json"]))
                if draft.get("manual_json") is not None
                else None
            ),
            "selected_source": draft.get("selected_source"),
            "locked": bool(draft.get("locked", 0)),
            "final_value": final_value,
            "final_source": final_source,
            "differs": len({json_text(item["value"]) for item in values.values()}) > 1,
        }
    return {
        "review_id": review_id,
        "code": str(row["code"]),
        "relative_media_path": str(row["relative_media_path"]),
        "revision": int(row["revision"]),
        "abandon_generation": int(row["abandon_generation"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "abandoned": abandoned,
        "abandoned_at": (
            float(row["abandoned_at"])
            if abandoned and row["abandoned_at"] is not None
            else None
        ),
        "fields": fields,
        "images": latest_images(connection, review_id),
    }


def _snapshot_public(
    row: sqlite3.Row, normalized: Mapping[str, object]
) -> dict[str, object]:
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "review_id": str(row["review_id"]),
        "source_id": str(row["source_id"]),
        "source_digest": str(row["source_digest"]),
        "fetched_at": float(row["fetched_at"]),
        "fields": dict(normalized),
        "deduplicated": True,
    }


def _snapshot_values(
    connection: sqlite3.Connection, snapshot_id: str
) -> dict[str, object]:
    return {
        str(row["field_name"]): json_load(str(row["value_json"]))
        for row in connection.execute(
            "SELECT field_name, value_json FROM media_metadata_review_values "
            "WHERE snapshot_id = ? ORDER BY field_name",
            (snapshot_id,),
        ).fetchall()
    }


def _refetch_public(row: sqlite3.Row) -> dict[str, object]:
    return {
        "intent_id": str(row["intent_id"]),
        "review_id": str(row["review_id"]),
        "base_revision": int(row["base_revision"]),
        "base_abandon_generation": int(row["base_abandon_generation"]),
        "sources": json_load(str(row["sources_json"])),
        "fields": json_load(str(row["fields_json"])),
        "images": json_load(str(row["images_json"])),
        "status": str(row["status"]),
        "error_code": str(row["error_code"]) if row["error_code"] else None,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _publication_public(row: sqlite3.Row) -> dict[str, object]:
    return {
        "publication_id": str(row["publication_id"]),
        "review_id": str(row["review_id"]),
        "review_revision": int(row["review_revision"]),
        "draft": json_load(str(row["draft_json"])),
        "artifacts": json_load(str(row["artifacts_json"])),
        "created_at": float(row["created_at"]),
    }


def _publication_draft(value: Mapping[str, object]) -> dict[str, object]:
    code = validated_code(value.get("code"))[0]
    fields = value.get("fields")
    if not isinstance(fields, Mapping):
        raise MediaMetadataReviewValidationError(
            "metadata review publication draft is invalid"
        )
    clean_fields: dict[str, dict[str, object]] = {}
    for field_name in METADATA_FIELDS:
        field = fields.get(field_name)
        if not isinstance(field, Mapping):
            raise MediaMetadataReviewValidationError(
                "metadata review publication field is invalid"
            )
        source = str(field.get("final_source") or "").strip()
        if source not in SNAPSHOT_SOURCES | {"default", "manual"}:
            raise MediaMetadataReviewValidationError(
                "metadata review publication provenance is invalid"
            )
        clean_fields[field_name] = {
            "value": normalize_field_value(
                field_name,
                field.get("final_value"),
                allow_none=True,
            ),
            "source": source,
            "locked": strict_bool(field.get("locked"), "publication field lock"),
        }
    return {"code": code, "fields": clean_fields}
