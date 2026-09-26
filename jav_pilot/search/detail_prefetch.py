from __future__ import annotations

import json
import queue
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol, cast

from ..core.catalog_code import normalize_catalog_code
from ..config.source_catalog import MAX_SEARCH_SOURCES
from ..core.migrations import MigrationError, SQLiteMigration, migrate_sqlite, require_columns
from ..core.models import (
    FieldSource,
    FieldSourceOrigin,
    MagnetGroup,
    MagnetSourceRef,
    Rating,
    RelatedRef,
    SourceDetails,
    SourceImage,
    WorkResult,
    WorkSource,
)


DETAIL_PREFETCH_SCHEMA_COMPONENT = "detail_prefetch"
DETAIL_PREFETCH_SCHEMA_VERSION = 2
MAX_DETAIL_PREFETCH_ITEMS = 999
MAX_DETAIL_PREFETCH_BATCHES = 200
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_COMPACT_SNAPSHOT_BYTES = 128 * 1024
MAX_BATCH_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_CACHE_ENTRIES = 10_000
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_ACTIVE_SNAPSHOT_BYTES = 128 * 1024 * 1024
_WORK_ID_RE = re.compile(
    r"^(?:code:[A-Za-z0-9._-]{2,72}|record:[A-Za-z0-9._-]{4,72})$"
)
_SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ERROR_MESSAGES = {
    "cancelled": "Detail prefetch was cancelled",
    "upstream_timeout": "Detail lookup timed out",
    "upstream_rate_limited": "Detail lookup was rate limited",
    "upstream_server_error": "Detail source is temporarily unavailable",
    "work_not_found": "Work details were not found",
    "parse_failed": "Work details could not be parsed",
    "invalid_result": "Detail source returned an invalid result",
    "internal_failure": "Detail prefetch failed",
}


class DetailPrefetchError(RuntimeError):
    pass


class DetailPrefetchValidationError(DetailPrefetchError):
    pass


class DetailPrefetchNotFoundError(DetailPrefetchError):
    pass


class DetailPrefetchUnavailableError(DetailPrefetchError):
    pass


class DetailPrefetchResolveError(DetailPrefetchError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        clean_code = str(code or "").strip()
        if clean_code not in _ERROR_MESSAGES:
            clean_code = "internal_failure"
            retryable = False
        super().__init__(_ERROR_MESSAGES[clean_code])
        self.code = clean_code
        self.retryable = bool(retryable)


class DetailResolver(Protocol):
    def __call__(
        self,
        snapshot: dict[str, object],
        source_scope: str,
        cancel_event: threading.Event,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class DetailPrefetchJob:
    batch_id: str
    position: int
    snapshot: dict[str, object]
    source_scope: str
    source_key: str
    config_fingerprint: str
    attempts: int


def normalize_work_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch item must be an object")
    work_id = _required_text(value.get("work_id"), "work_id", 80)
    if _WORK_ID_RE.fullmatch(work_id) is None:
        raise DetailPrefetchValidationError("detail prefetch work_id is invalid")
    title = _required_text(value.get("title"), "title", 2_000)
    sources_value = value.get("sources")
    if not isinstance(sources_value, (list, tuple)) or not sources_value:
        raise DetailPrefetchValidationError(
            "detail prefetch item must include at least one source"
        )
    if len(sources_value) > MAX_SEARCH_SOURCES:
        raise DetailPrefetchValidationError("detail prefetch item has too many sources")
    sources = tuple(_work_source(item) for item in sources_value)
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise DetailPrefetchValidationError("detail prefetch sources must be unique")
    magnets_value = value.get("magnets") or ()
    if not isinstance(magnets_value, (list, tuple)) or len(magnets_value) > 500:
        raise DetailPrefetchValidationError("detail prefetch magnets are invalid")
    work = WorkResult(
        work_id=work_id,
        canonical_code=_optional_text(value.get("canonical_code"), 128),
        code=_optional_text(value.get("code"), 128),
        title=title,
        release_date=_optional_text(value.get("release_date"), 32),
        release_date_conflict=bool(value.get("release_date_conflict", False)),
        actors=_text_tuple(value.get("actors"), maximum=500, item_maximum=300),
        tags=_text_tuple(value.get("tags"), maximum=500, item_maximum=300),
        sources=sources,
        magnets=tuple(_magnet_group(item) for item in magnets_value),
    )
    normalized = work.to_dict()
    size = len(_json(normalized).encode("utf-8"))
    if size > MAX_SNAPSHOT_BYTES:
        raise DetailPrefetchValidationError("detail prefetch item is too large")
    return normalized


def compact_work_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch item must be an object")
    work_id = _work_id(value.get("work_id"))
    sources_value = value.get("sources")
    if not isinstance(sources_value, (list, tuple)) or not sources_value:
        raise DetailPrefetchValidationError(
            "detail prefetch item must include at least one source"
        )
    if len(sources_value) > MAX_SEARCH_SOURCES:
        raise DetailPrefetchValidationError("detail prefetch item has too many sources")
    sources = tuple(_compact_work_source(item) for item in sources_value)
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise DetailPrefetchValidationError("detail prefetch sources must be unique")
    work = WorkResult(
        work_id=work_id,
        canonical_code=_optional_text(value.get("canonical_code"), 128),
        code=_optional_text(value.get("code"), 128),
        title=_required_text(value.get("title"), "title", 2_000),
        release_date=_optional_text(value.get("release_date"), 32),
        release_date_conflict=bool(value.get("release_date_conflict", False)),
        sources=sources,
    )
    normalized = work.to_dict()
    if len(_json(normalized).encode("utf-8")) > MAX_COMPACT_SNAPSHOT_BYTES:
        raise DetailPrefetchValidationError("detail prefetch item is too large")
    return normalized


def work_result_from_snapshot(value: object) -> WorkResult:
    normalized = normalize_work_snapshot(value)
    return WorkResult(
        work_id=str(normalized["work_id"]),
        canonical_code=_optional_text(normalized.get("canonical_code"), 128),
        code=_optional_text(normalized.get("code"), 128),
        title=str(normalized["title"]),
        release_date=_optional_text(normalized.get("release_date"), 32),
        release_date_conflict=bool(normalized.get("release_date_conflict", False)),
        actors=tuple(str(item) for item in normalized.get("actors") or ()),
        tags=tuple(str(item) for item in normalized.get("tags") or ()),
        sources=tuple(_work_source(item) for item in normalized.get("sources") or ()),
        magnets=tuple(
            _magnet_group(item) for item in normalized.get("magnets") or ()
        ),
    )


def select_snapshot_sources(
    value: object,
    source_scope: object,
) -> dict[str, object]:
    normalized = compact_work_snapshot(value)
    scope = _source_scope(source_scope)
    if scope == "all":
        return normalized
    selected = [
        source
        for source in normalized.get("sources") or []
        if isinstance(source, dict) and source.get("source_id") == scope
    ]
    if not selected:
        raise DetailPrefetchValidationError(
            "detail prefetch source is not present in the work snapshot"
        )
    return {**normalized, "sources": selected}


class DetailPrefetchStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise DetailPrefetchUnavailableError(
                "detail prefetch database path must be absolute"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_hex(16))
        self._initialize()
        self.recover_interrupted()

    def create(
        self,
        items: Sequence[dict[str, object]],
        *,
        source_scope: str,
        config_fingerprint: str,
    ) -> dict[str, object]:
        if not items or len(items) > MAX_DETAIL_PREFETCH_ITEMS:
            raise DetailPrefetchValidationError(
                "detail prefetch items must contain between 1 and 999 works"
            )
        clean_scope = _source_scope(source_scope)
        clean_fingerprint = _fingerprint(config_fingerprint)
        normalized: list[tuple[dict[str, object], str, str]] = []
        seen: set[str] = set()
        total_bytes = 0
        for candidate in items:
            item = select_snapshot_sources(candidate, clean_scope)
            work_id = str(item["work_id"])
            if work_id in seen:
                continue
            seen.add(work_id)
            snapshot_json = _json(item)
            total_bytes += len(snapshot_json.encode("utf-8"))
            if total_bytes > MAX_BATCH_SNAPSHOT_BYTES:
                raise DetailPrefetchValidationError(
                    "detail prefetch batch is too large"
                )
            normalized.append((item, snapshot_json, _source_key(item)))
        if not normalized:
            raise DetailPrefetchValidationError("detail prefetch batch is empty")

        batch_id = _batch_id(self._id_factory())
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune_batches_locked(connection)
            active_bytes_row = connection.execute(
                """
                SELECT COALESCE(SUM(length(CAST(i.snapshot_json AS BLOB))), 0)
                FROM detail_prefetch_items AS i
                JOIN detail_prefetch_batches AS b USING (batch_id)
                WHERE b.status != 'completed'
                """
            ).fetchone()
            active_bytes = int(active_bytes_row[0] if active_bytes_row else 0)
            if active_bytes + total_bytes > MAX_ACTIVE_SNAPSHOT_BYTES:
                connection.rollback()
                raise DetailPrefetchUnavailableError(
                    "detail prefetch snapshot capacity is occupied by active batches"
                )
            connection.execute(
                """
                INSERT INTO detail_prefetch_batches (
                    batch_id, source_scope, config_fingerprint, status, total,
                    queued_count, running_count, completed_count, failed_count,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, 0, 0, 0, ?, ?, NULL)
                """,
                (
                    batch_id,
                    clean_scope,
                    clean_fingerprint,
                    len(normalized),
                    len(normalized),
                    now,
                    now,
                ),
            )
            for position, (item, snapshot_json, source_key) in enumerate(normalized):
                cached = connection.execute(
                    """
                    SELECT 1 FROM detail_prefetch_cache
                    WHERE work_id = ? AND source_key = ? AND config_fingerprint = ?
                    """,
                    (str(item["work_id"]), source_key, clean_fingerprint),
                ).fetchone()
                status = "completed" if cached is not None else "queued"
                connection.execute(
                    """
                    INSERT INTO detail_prefetch_items (
                        batch_id, position, work_id, source_key, status, attempts,
                        next_attempt_at, snapshot_json, error_code,
                        cache_hit, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        position,
                        str(item["work_id"]),
                        source_key,
                        status,
                        now,
                        snapshot_json,
                        int(cached is not None),
                        now,
                        now,
                    ),
                )
            self._refresh_batch_locked(connection, batch_id, now)
            connection.commit()
        return self.get(batch_id)

    def get(self, batch_id: object) -> dict[str, object]:
        clean_id = _batch_id(batch_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM detail_prefetch_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise DetailPrefetchNotFoundError(
                    "detail prefetch batch was not found"
                )
            items = connection.execute(
                """
                SELECT position, work_id, status, attempts, error_code, cache_hit,
                       created_at, updated_at
                FROM detail_prefetch_items WHERE batch_id = ? ORDER BY position
                """,
                (clean_id,),
            ).fetchall()
        return _batch_payload(row, items=items)

    def list(self, *, limit: object = 20) -> list[dict[str, object]]:
        clean_limit = _bounded_int(limit, "limit", 1, 100)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM detail_prefetch_batches
                ORDER BY created_at DESC, batch_id DESC LIMIT ?
                """,
                (clean_limit,),
            ).fetchall()
        return [_batch_payload(row) for row in rows]

    def cancel(self, batch_id: object) -> dict[str, object]:
        clean_id = _batch_id(batch_id)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM detail_prefetch_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if exists is None:
                connection.rollback()
                raise DetailPrefetchNotFoundError(
                    "detail prefetch batch was not found"
                )
            changed = connection.execute(
                """
                UPDATE detail_prefetch_items
                SET status = 'failed', next_attempt_at = ?,
                    error_code = 'cancelled', cache_hit = 0, updated_at = ?
                WHERE batch_id = ? AND status IN ('queued', 'running')
                """,
                (now, now, clean_id),
            ).rowcount
            if changed:
                self._refresh_batch_locked(connection, clean_id, now)
            connection.commit()
        return self.get(clean_id)

    def queued_refs(self) -> list[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT batch_id, position FROM detail_prefetch_items
                WHERE status = 'queued' ORDER BY created_at, batch_id, position
                """
            ).fetchall()
        return [(str(row["batch_id"]), int(row["position"])) for row in rows]

    def claim(self, batch_id: object, position: object) -> DetailPrefetchJob | None:
        clean_id = _batch_id(batch_id)
        clean_position = _bounded_int(position, "position", 0, 100_000)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.*, b.source_scope, b.config_fingerprint
                FROM detail_prefetch_items AS i
                JOIN detail_prefetch_batches AS b USING (batch_id)
                WHERE i.batch_id = ? AND i.position = ?
                """,
                (clean_id, clean_position),
            ).fetchone()
            if row is None or str(row["status"]) != "queued":
                connection.commit()
                return None
            try:
                snapshot = normalize_work_snapshot(
                    json.loads(str(row["snapshot_json"]))
                )
            except (TypeError, json.JSONDecodeError) as exc:
                connection.rollback()
                raise DetailPrefetchUnavailableError(
                    "detail prefetch snapshot is corrupt"
                ) from exc
            connection.execute(
                """
                UPDATE detail_prefetch_items
                SET status = 'running', attempts = attempts + 1,
                    error_code = NULL, cache_hit = 0, updated_at = ?
                WHERE batch_id = ? AND position = ? AND status = 'queued'
                """,
                (now, clean_id, clean_position),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                connection.commit()
                return None
            self._refresh_batch_locked(connection, clean_id, now)
            connection.commit()
        return DetailPrefetchJob(
            batch_id=clean_id,
            position=clean_position,
            snapshot=snapshot,
            source_scope=str(row["source_scope"]),
            source_key=str(row["source_key"]),
            config_fingerprint=str(row["config_fingerprint"]),
            attempts=int(row["attempts"]) + 1,
        )

    def fail_queued(self, batch_id: object, position: object, code: str) -> None:
        clean_id = _batch_id(batch_id)
        clean_position = _bounded_int(position, "position", 0, 100_000)
        clean_code = code if code in _ERROR_MESSAGES else "internal_failure"
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE detail_prefetch_items SET status = 'failed', "
                "error_code = ?, updated_at = ? WHERE batch_id = ? "
                "AND position = ? AND status = 'queued'",
                (clean_code, now, clean_id, clean_position),
            )
            self._refresh_batch_locked(connection, clean_id, now)
            connection.commit()

    def cached_result(
        self,
        work_id: object,
        sources: Sequence[object],
        config_fingerprint: object,
    ) -> dict[str, object] | None:
        clean_work_id = _work_id(work_id)
        source_key = _source_key_from_ids(sources)
        fingerprint = _fingerprint(config_fingerprint)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_json FROM detail_prefetch_cache
                WHERE work_id = ? AND source_key = ? AND config_fingerprint = ?
                """,
                (clean_work_id, source_key, fingerprint),
            ).fetchone()
        if row is None:
            return None
        try:
            return normalize_work_snapshot(json.loads(str(row["result_json"])))
        except (DetailPrefetchError, TypeError, json.JSONDecodeError):
            return None

    def cached_for_job(self, job: DetailPrefetchJob) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_json FROM detail_prefetch_cache
                WHERE work_id = ? AND source_key = ? AND config_fingerprint = ?
                """,
                (
                    str(job.snapshot["work_id"]),
                    job.source_key,
                    job.config_fingerprint,
                ),
            ).fetchone()
        if row is None:
            return None
        try:
            return normalize_work_snapshot(json.loads(str(row["result_json"])))
        except (DetailPrefetchError, TypeError, json.JSONDecodeError):
            return None

    def finish_success(
        self,
        job: DetailPrefetchJob,
        result: dict[str, object],
        *,
        cache_hit: bool = False,
    ) -> None:
        normalized = normalize_work_snapshot(result)
        if str(normalized["work_id"]) != str(job.snapshot["work_id"]):
            raise DetailPrefetchValidationError(
                "detail prefetch result work_id does not match"
            )
        if _source_key(normalized) != job.source_key:
            raise DetailPrefetchValidationError(
                "detail prefetch result sources do not match"
            )
        result_json = _json(normalized)
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise DetailPrefetchValidationError("detail prefetch result is too large")
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE detail_prefetch_items
                SET status = 'completed', error_code = NULL,
                    cache_hit = ?, updated_at = ?
                WHERE batch_id = ? AND position = ? AND status = 'running'
                """,
                (
                    int(cache_hit),
                    now,
                    job.batch_id,
                    job.position,
                ),
            )
            if changed.rowcount == 1:
                previous = connection.execute(
                    """
                    SELECT length(CAST(result_json AS BLOB)) AS result_bytes
                    FROM detail_prefetch_cache
                    WHERE work_id = ? AND source_key = ? AND config_fingerprint = ?
                    """,
                    (
                        str(normalized["work_id"]),
                        job.source_key,
                        job.config_fingerprint,
                    ),
                ).fetchone()
                previous_bytes = (
                    int(previous["result_bytes"] or 0) if previous is not None else 0
                )
                connection.execute(
                    """
                    INSERT INTO detail_prefetch_cache (
                        work_id, source_key, config_fingerprint, result_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(work_id, source_key, config_fingerprint) DO UPDATE SET
                        result_json = excluded.result_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(normalized["work_id"]),
                        job.source_key,
                        job.config_fingerprint,
                        result_json,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE detail_prefetch_cache_usage
                    SET entry_count = entry_count + ?,
                        total_bytes = total_bytes + ?
                    WHERE singleton = 1
                    """,
                    (
                        int(previous is None),
                        len(result_json.encode("utf-8")) - previous_bytes,
                    ),
                )
                self._prune_cache_locked(connection)
                self._refresh_batch_locked(connection, job.batch_id, now)
            connection.commit()

    def finish_failure(
        self,
        job: DetailPrefetchJob,
        code: str,
        *,
        retryable: bool,
        max_attempts: int,
        retry_delay: float,
    ) -> bool:
        clean_code = code if code in _ERROR_MESSAGES else "internal_failure"
        retry = bool(retryable and job.attempts < max_attempts)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE detail_prefetch_items
                SET status = ?, next_attempt_at = ?,
                    error_code = ?, cache_hit = 0, updated_at = ?
                WHERE batch_id = ? AND position = ? AND status = 'running'
                """,
                (
                    "queued" if retry else "failed",
                    now + max(0.0, float(retry_delay)) if retry else now,
                    clean_code,
                    now,
                    job.batch_id,
                    job.position,
                ),
            )
            if changed.rowcount == 1:
                self._refresh_batch_locked(connection, job.batch_id, now)
            connection.commit()
        return retry and changed.rowcount == 1

    def requeue_interrupted(self, job: DetailPrefetchJob) -> None:
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE detail_prefetch_items
                SET status = 'queued', attempts = MAX(0, attempts - 1),
                    next_attempt_at = ?, error_code = NULL, updated_at = ?
                WHERE batch_id = ? AND position = ? AND status = 'running'
                """,
                (now, now, job.batch_id, job.position),
            )
            if changed.rowcount == 1:
                self._refresh_batch_locked(connection, job.batch_id, now)
            connection.commit()

    def recover_interrupted(self) -> int:
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT DISTINCT batch_id FROM detail_prefetch_items "
                "WHERE status = 'running'"
            ).fetchall()
            changed = connection.execute(
                """
                UPDATE detail_prefetch_items
                SET status = 'queued', attempts = MAX(0, attempts - 1),
                    next_attempt_at = ?, error_code = NULL, updated_at = ?
                WHERE status = 'running'
                """,
                (now, now),
            ).rowcount
            for row in rows:
                self._refresh_batch_locked(connection, str(row["batch_id"]), now)
            connection.commit()
        return int(changed)

    def _refresh_batch_locked(
        self, connection: sqlite3.Connection, batch_id: str, now: float
    ) -> None:
        counts = connection.execute(
            """
            SELECT
                SUM(status = 'queued') AS queued_count,
                SUM(status = 'running') AS running_count,
                SUM(status = 'completed') AS completed_count,
                SUM(status = 'failed') AS failed_count,
                COUNT(*) AS total
            FROM detail_prefetch_items WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        if counts is None:
            return
        queued = int(counts["queued_count"] or 0)
        running = int(counts["running_count"] or 0)
        completed = int(counts["completed_count"] or 0)
        failed = int(counts["failed_count"] or 0)
        if queued + running == 0:
            status = "completed"
            completed_at: float | None = now
        elif running or completed or failed:
            status = "running"
            completed_at = None
        else:
            status = "queued"
            completed_at = None
        connection.execute(
            """
            UPDATE detail_prefetch_batches
            SET status = ?, total = ?, queued_count = ?, running_count = ?,
                completed_count = ?, failed_count = ?, updated_at = ?,
                completed_at = ?
            WHERE batch_id = ?
            """,
            (
                status,
                int(counts["total"] or 0),
                queued,
                running,
                completed,
                failed,
                now,
                completed_at,
                batch_id,
            ),
        )
        if status == "completed":
            connection.execute(
                "UPDATE detail_prefetch_items SET snapshot_json = '{}' "
                "WHERE batch_id = ?",
                (batch_id,),
            )

    def _prune_batches_locked(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM detail_prefetch_batches
            WHERE status = 'completed' AND batch_id IN (
                SELECT batch_id FROM detail_prefetch_batches
                WHERE status = 'completed'
                ORDER BY created_at DESC, batch_id DESC LIMIT -1 OFFSET ?
            )
            """,
            (MAX_DETAIL_PREFETCH_BATCHES,),
        )

    def _prune_cache_locked(self, connection: sqlite3.Connection) -> None:
        usage = connection.execute(
            "SELECT entry_count, total_bytes FROM detail_prefetch_cache_usage "
            "WHERE singleton = 1"
        ).fetchone()
        if usage is None:
            raise DetailPrefetchUnavailableError(
                "detail prefetch cache usage is unavailable"
            )
        entry_count = int(usage["entry_count"])
        total_bytes = int(usage["total_bytes"])
        if entry_count <= MAX_CACHE_ENTRIES and total_bytes <= MAX_CACHE_BYTES:
            return
        rows = connection.execute(
            """
            SELECT rowid, length(CAST(result_json AS BLOB)) AS result_bytes
            FROM detail_prefetch_cache ORDER BY updated_at, rowid
            """
        ).fetchall()
        remove: list[tuple[int]] = []
        removed_bytes = 0
        for row in rows:
            size = int(row["result_bytes"] or 0)
            if (
                entry_count - len(remove) <= MAX_CACHE_ENTRIES
                and total_bytes - removed_bytes <= MAX_CACHE_BYTES
            ):
                break
            remove.append((int(row["rowid"]),))
            removed_bytes += size
        if remove:
            connection.executemany(
                "DELETE FROM detail_prefetch_cache WHERE rowid = ?", remove
            )
            connection.execute(
                """
                UPDATE detail_prefetch_cache_usage
                SET entry_count = entry_count - ?, total_bytes = total_bytes - ?
                WHERE singleton = 1
                """,
                (len(remove), removed_bytes),
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            migrate_sqlite(
                connection,
                component=DETAIL_PREFETCH_SCHEMA_COMPONENT,
                current_version=DETAIL_PREFETCH_SCHEMA_VERSION,
                migrations=(
                    SQLiteMigration(1, _create_schema, _verify_schema),
                    SQLiteMigration(2, _migrate_schema_v2, _verify_schema),
                ),
                clock=self._clock,
                verify_current=_verify_schema,
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


class DetailPrefetchManager:
    def __init__(
        self,
        database_path: Path | str,
        resolver: DetailResolver,
        config_fingerprint: Callable[[], str],
        *,
        worker_count: int = 2,
        max_attempts: int = 3,
        retry_delays: Sequence[float] = (0.25, 1.0),
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(worker_count, bool) or not 1 <= worker_count <= 2:
            raise DetailPrefetchValidationError(
                "detail prefetch worker count must be between 1 and 2"
            )
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 5:
            raise DetailPrefetchValidationError(
                "detail prefetch attempt limit is invalid"
            )
        self.store = DetailPrefetchStore(
            database_path, clock=clock, id_factory=id_factory
        )
        self._resolver = resolver
        self._fingerprint = config_fingerprint
        self._max_attempts = max_attempts
        self._retry_delays = tuple(max(0.0, float(item)) for item in retry_delays)
        self._queue: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._stopping = False
        self._batch_cancel_events: dict[str, threading.Event] = {}
        self._resolution_locks = tuple(threading.Lock() for _ in range(64))
        self._claim_failures: dict[tuple[str, int], int] = {}
        for ref in self.store.queued_refs():
            self._queue.put(ref)
        self._workers = tuple(
            threading.Thread(
                target=self._run,
                name=f"jav-detail-prefetch-{position + 1}",
                daemon=True,
            )
            for position in range(worker_count)
        )
        try:
            for worker in self._workers:
                worker.start()
        except RuntimeError as exc:
            self._stop_event.set()
            for _ in self._workers:
                self._queue.put(None)
            raise DetailPrefetchUnavailableError(
                "detail prefetch workers could not start"
            ) from exc

    def create(
        self,
        items: Sequence[dict[str, object]],
        *,
        source_scope: object = "all",
    ) -> dict[str, object]:
        with self._lock:
            if self._stopping:
                raise DetailPrefetchUnavailableError(
                    "detail prefetch manager is stopping"
                )
        fingerprint = _fingerprint(self._fingerprint())
        batch = self.store.create(
            items,
            source_scope=_source_scope(source_scope),
            config_fingerprint=fingerprint,
        )
        for item in batch.get("items") or []:
            if isinstance(item, dict) and item.get("status") == "queued":
                self._queue.put((str(batch["batch_id"]), int(item["position"])))
        return batch

    def get(self, batch_id: object) -> dict[str, object]:
        return self.store.get(batch_id)

    def list(self, *, limit: object = 20) -> list[dict[str, object]]:
        return self.store.list(limit=limit)

    def cancel(self, batch_id: object) -> dict[str, object]:
        clean_id = _batch_id(batch_id)
        with self._lock:
            cancel_event = self._batch_cancel_events.setdefault(
                clean_id, threading.Event()
            )
            try:
                batch = self.store.cancel(clean_id)
            except Exception:
                if self._batch_cancel_events.get(clean_id) is cancel_event:
                    self._batch_cancel_events.pop(clean_id, None)
                raise
            cancel_event.set()
            if self._batch_cancel_events.get(clean_id) is cancel_event:
                self._batch_cancel_events.pop(clean_id, None)
            return batch

    def cached_result(
        self, work_id: object, sources: Sequence[object]
    ) -> dict[str, object] | None:
        return self.store.cached_result(work_id, sources, self._fingerprint())

    def is_alive(self) -> bool:
        return bool(self._workers) and all(worker.is_alive() for worker in self._workers)

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        with self._lock:
            if not self._stopping:
                self._stopping = True
                self._stop_event.set()
                for cancel_event in self._batch_cancel_events.values():
                    cancel_event.set()
                for _ in self._workers:
                    self._queue.put(None)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in self._workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in self._workers)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            ref = self._queue.get()
            if ref is None:
                return
            cancel_event = self._batch_cancel_event(ref[0])
            if cancel_event.is_set():
                self._discard_terminal_batch_event(ref[0], cancel_event)
                continue
            try:
                job = self.store.claim(*ref)
            except DetailPrefetchError:
                try:
                    self.store.fail_queued(*ref, "internal_failure")
                except (DetailPrefetchError, OSError, sqlite3.Error):
                    self._requeue_claim_failure(ref, cancel_event)
                else:
                    with self._lock:
                        self._claim_failures.pop(ref, None)
                    self._discard_terminal_batch_event(ref[0], cancel_event)
                continue
            except (OSError, sqlite3.Error):
                self._requeue_claim_failure(ref, cancel_event)
                continue
            if job is None:
                with self._lock:
                    self._claim_failures.pop(ref, None)
                self._discard_terminal_batch_event(ref[0], cancel_event)
                continue
            with self._lock:
                self._claim_failures.pop(ref, None)
            if self._stop_event.is_set():
                self.store.requeue_interrupted(job)
                return
            key = (
                str(job.snapshot["work_id"]),
                job.source_key,
                job.config_fingerprint,
            )
            resolution_lock = self._resolution_lock(key)
            acquired = False
            while (
                not acquired
                and not self._stop_event.is_set()
                and not cancel_event.is_set()
            ):
                acquired = resolution_lock.acquire(timeout=0.1)
            if not acquired:
                outcome = self._finish_interrupted(job, cancel_event)
                if outcome == "shutdown":
                    return
                self._discard_terminal_batch_event(job.batch_id, cancel_event)
                continue
            try:
                cached = self.store.cached_for_job(job)
                if cached is not None:
                    outcome = self._finish_success(
                        job, cached, cancel_event, cache_hit=True
                    )
                    if outcome == "shutdown":
                        return
                    continue
                try:
                    result = self._resolver(
                        job.snapshot, job.source_scope, cancel_event
                    )
                    outcome = self._finish_success(job, result, cancel_event)
                    if outcome == "shutdown":
                        return
                except DetailPrefetchResolveError as exc:
                    outcome = self._finish_interrupted(job, cancel_event)
                    if outcome == "shutdown":
                        return
                    if outcome == "cancelled":
                        continue
                    self._handle_failure(
                        job,
                        exc.code,
                        retryable=exc.retryable,
                        cancel_event=cancel_event,
                    )
                except TimeoutError:
                    outcome = self._finish_interrupted(job, cancel_event)
                    if outcome == "shutdown":
                        return
                    if outcome == "cancelled":
                        continue
                    self._handle_failure(
                        job,
                        "upstream_timeout",
                        retryable=True,
                        cancel_event=cancel_event,
                    )
                except DetailPrefetchValidationError:
                    outcome = self._finish_interrupted(job, cancel_event)
                    if outcome == "shutdown":
                        return
                    if outcome == "cancelled":
                        continue
                    self._handle_failure(
                        job,
                        "invalid_result",
                        retryable=False,
                        cancel_event=cancel_event,
                    )
                except Exception:
                    outcome = self._finish_interrupted(job, cancel_event)
                    if outcome == "shutdown":
                        return
                    if outcome == "cancelled":
                        continue
                    self._handle_failure(
                        job,
                        "internal_failure",
                        retryable=False,
                        cancel_event=cancel_event,
                    )
            finally:
                resolution_lock.release()
                self._discard_terminal_batch_event(job.batch_id, cancel_event)

    def _handle_failure(
        self,
        job: DetailPrefetchJob,
        code: str,
        *,
        retryable: bool,
        cancel_event: threading.Event,
    ) -> None:
        delay_index = min(max(0, job.attempts - 1), len(self._retry_delays) - 1)
        delay = self._retry_delays[delay_index] if self._retry_delays else 0.0
        with self._lock:
            if self._stop_event.is_set():
                self.store.requeue_interrupted(job)
                return
            if cancel_event.is_set():
                self.store.cancel(job.batch_id)
                return
            retry = self.store.finish_failure(
                job,
                code,
                retryable=retryable,
                max_attempts=self._max_attempts,
                retry_delay=delay,
            )
        if retry and not cancel_event.wait(delay) and not self._stop_event.is_set():
            self._queue.put((job.batch_id, job.position))

    def _resolution_lock(self, key: tuple[str, str, str]) -> threading.Lock:
        return self._resolution_locks[hash(key) % len(self._resolution_locks)]

    def _batch_cancel_event(self, batch_id: str) -> threading.Event:
        with self._lock:
            return self._batch_cancel_events.setdefault(batch_id, threading.Event())

    def _finish_success(
        self,
        job: DetailPrefetchJob,
        result: dict[str, object],
        cancel_event: threading.Event,
        *,
        cache_hit: bool = False,
    ) -> str:
        with self._lock:
            outcome = self._finish_interrupted_locked(job, cancel_event)
            if outcome is not None:
                return outcome
            self.store.finish_success(job, result, cache_hit=cache_hit)
            return "completed"

    def _finish_interrupted(
        self, job: DetailPrefetchJob, cancel_event: threading.Event
    ) -> str | None:
        with self._lock:
            return self._finish_interrupted_locked(job, cancel_event)

    def _finish_interrupted_locked(
        self, job: DetailPrefetchJob, cancel_event: threading.Event
    ) -> str | None:
        if self._stop_event.is_set():
            self.store.requeue_interrupted(job)
            return "shutdown"
        if cancel_event.is_set():
            self.store.cancel(job.batch_id)
            return "cancelled"
        return None

    def _discard_terminal_batch_event(
        self, batch_id: str, cancel_event: threading.Event
    ) -> None:
        try:
            status = str(self.store.get(batch_id)["status"])
        except (DetailPrefetchError, OSError, sqlite3.Error):
            return
        if status not in {"completed", "partial", "failed"}:
            return
        with self._lock:
            if self._batch_cancel_events.get(batch_id) is cancel_event:
                self._batch_cancel_events.pop(batch_id, None)

    def _requeue_claim_failure(
        self, ref: tuple[str, int], cancel_event: threading.Event
    ) -> None:
        with self._lock:
            failures = min(self._claim_failures.get(ref, 0) + 1, 8)
            self._claim_failures[ref] = failures
        delay = min(5.0, 0.1 * (2 ** (failures - 1)))
        if not cancel_event.wait(delay) and not self._stop_event.is_set():
            self._queue.put(ref)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE detail_prefetch_batches (
            batch_id TEXT PRIMARY KEY,
            source_scope TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed')),
            total INTEGER NOT NULL CHECK (total BETWEEN 1 AND 999),
            queued_count INTEGER NOT NULL CHECK (queued_count >= 0),
            running_count INTEGER NOT NULL CHECK (running_count >= 0),
            completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
            failed_count INTEGER NOT NULL CHECK (failed_count >= 0),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),
            completed_at REAL CHECK (completed_at IS NULL OR completed_at >= 0),
            CHECK (queued_count + running_count + completed_count + failed_count = total)
        );

        CREATE TABLE detail_prefetch_items (
            batch_id TEXT NOT NULL REFERENCES detail_prefetch_batches(batch_id)
                ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 998),
            work_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'completed', 'failed')
            ),
            attempts INTEGER NOT NULL CHECK (attempts BETWEEN 0 AND 100),
            next_attempt_at REAL NOT NULL CHECK (next_attempt_at >= 0),
            snapshot_json TEXT NOT NULL,
            error_code TEXT,
            cache_hit INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0, 1)),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),
            PRIMARY KEY (batch_id, position),
            UNIQUE (batch_id, work_id)
        );

        CREATE INDEX detail_prefetch_items_status_idx
            ON detail_prefetch_items(status, next_attempt_at, created_at);

        CREATE TABLE detail_prefetch_cache (
            work_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at REAL NOT NULL CHECK (created_at >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),
            PRIMARY KEY (work_id, source_key, config_fingerprint)
        );

        CREATE INDEX detail_prefetch_cache_updated_idx
            ON detail_prefetch_cache(updated_at DESC);

        CREATE TABLE detail_prefetch_cache_usage (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            entry_count INTEGER NOT NULL CHECK (entry_count >= 0),
            total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0)
        );

        INSERT INTO detail_prefetch_cache_usage (
            singleton, entry_count, total_bytes
        ) VALUES (1, 0, 0);
        """
    )


def _migrate_schema_v2(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    item_rows = connection.execute(
        "SELECT i.batch_id, i.position, i.work_id, i.status, "
        "b.status AS batch_status, i.snapshot_json "
        "FROM detail_prefetch_items AS i "
        "JOIN detail_prefetch_batches AS b USING (batch_id) "
        "ORDER BY i.batch_id, i.position"
    ).fetchall()
    item_targets: dict[tuple[str, str], int] = {}
    item_updates: list[tuple[str, int, str, str]] = []
    for row in item_rows:
        batch_id = str(row["batch_id"])
        position = int(row["position"])
        old_work_id = str(row["work_id"])
        snapshot = _migration_json_object(
            row["snapshot_json"],
            "detail prefetch snapshot",
        )
        # Terminal batches intentionally discard item snapshots after the
        # result is persisted.  The empty object is therefore a valid compact
        # marker and has no embedded identity to compare with the indexed ID.
        if not snapshot:
            if (
                str(row["batch_status"]) != "completed"
                or str(row["status"]) not in {"completed", "failed"}
            ):
                raise MigrationError("detail prefetch snapshot is invalid")
            migrated: object = {}
        else:
            migrated = _migrate_prefetch_value(snapshot)
        if not isinstance(migrated, dict):
            raise MigrationError("detail prefetch snapshot is invalid")
        new_work_id = _migrate_prefetch_work_id(old_work_id)
        if migrated and migrated.get("work_id") != new_work_id:
            raise MigrationError("detail prefetch snapshot identity is inconsistent")
        snapshot_json = _json(migrated)
        if len(snapshot_json.encode("utf-8")) > MAX_COMPACT_SNAPSHOT_BYTES:
            raise MigrationError("detail prefetch snapshot is too large")
        identity = (batch_id, new_work_id)
        collided = item_targets.setdefault(identity, position)
        if collided != position:
            raise MigrationError(
                "detail prefetch items collide after catalog code normalization"
            )
        if new_work_id != old_work_id or snapshot_json != str(row["snapshot_json"]):
            item_updates.append((batch_id, position, new_work_id, snapshot_json))

    cache_rows = connection.execute(
        "SELECT rowid, work_id, source_key, config_fingerprint, result_json "
        "FROM detail_prefetch_cache ORDER BY rowid"
    ).fetchall()
    cache_targets: dict[tuple[str, str, str], int] = {}
    cache_updates: list[tuple[int, str, str]] = []
    for row in cache_rows:
        row_id = int(row["rowid"])
        old_work_id = str(row["work_id"])
        result = _migration_json_object(
            row["result_json"],
            "detail prefetch result",
        )
        migrated = _migrate_prefetch_value(result)
        if not isinstance(migrated, dict):
            raise MigrationError("detail prefetch result is invalid")
        new_work_id = _migrate_prefetch_work_id(old_work_id)
        if migrated.get("work_id") != new_work_id:
            raise MigrationError("detail prefetch result identity is inconsistent")
        result_json = _json(migrated)
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise MigrationError("detail prefetch result is too large")
        identity = (
            new_work_id,
            str(row["source_key"]),
            str(row["config_fingerprint"]),
        )
        collided = cache_targets.setdefault(identity, row_id)
        if collided != row_id:
            raise MigrationError(
                "detail prefetch cache entries collide after catalog code normalization"
            )
        if new_work_id != old_work_id or result_json != str(row["result_json"]):
            cache_updates.append((row_id, new_work_id, result_json))

    reserved_work_ids = {
        str(row["work_id"]) for row in item_rows
    } | {str(row["work_id"]) for row in cache_rows}
    temporary_work_ids: dict[tuple[str, int], str] = {}
    for namespace, row_id in (
        *((batch_id, position) for batch_id, position, *_rest in item_updates),
        *(("cache", row_id) for row_id, *_rest in cache_updates),
    ):
        for salt in range(1024):
            temporary = "migration:" + secrets.token_hex(16)
            if temporary not in reserved_work_ids:
                reserved_work_ids.add(temporary)
                temporary_work_ids[(namespace, row_id)] = temporary
                break
        else:
            raise MigrationError("detail prefetch identity migration collided")

    connection.executemany(
        "UPDATE detail_prefetch_items SET work_id = ? "
        "WHERE batch_id = ? AND position = ?",
        (
            (temporary_work_ids[(batch_id, position)], batch_id, position)
            for batch_id, position, _work_id, _snapshot_json in item_updates
        ),
    )
    connection.executemany(
        "UPDATE detail_prefetch_items SET work_id = ?, snapshot_json = ? "
        "WHERE batch_id = ? AND position = ?",
        (
            (work_id, snapshot_json, batch_id, position)
            for batch_id, position, work_id, snapshot_json in item_updates
        ),
    )
    connection.executemany(
        "UPDATE detail_prefetch_cache SET work_id = ? WHERE rowid = ?",
        (
            (temporary_work_ids[("cache", row_id)], row_id)
            for row_id, _work_id, _result_json in cache_updates
        ),
    )
    connection.executemany(
        "UPDATE detail_prefetch_cache SET work_id = ?, result_json = ? "
        "WHERE rowid = ?",
        (
            (work_id, result_json, row_id)
            for row_id, work_id, result_json in cache_updates
        ),
    )
    usage = connection.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(length(CAST(result_json AS BLOB))), 0) "
        "FROM detail_prefetch_cache"
    ).fetchone()
    connection.execute(
        "UPDATE detail_prefetch_cache_usage "
        "SET entry_count = ?, total_bytes = ? WHERE singleton = 1",
        (int(usage[0]), int(usage[1])) if usage is not None else (0, 0),
    )


def _migration_json_object(value: object, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{label} is invalid") from exc
    if not isinstance(decoded, dict):
        raise MigrationError(f"{label} is invalid")
    return decoded


def _fc2_migration_identity(value: object) -> tuple[str, str] | None:
    normalized = normalize_catalog_code(value, max_length=128)
    if normalized is None or re.fullmatch(r"FC2PPV\d{2,9}", normalized[1]) is None:
        return None
    return normalized


def _migrate_prefetch_work_id(value: str) -> str:
    if not value.startswith("code:"):
        return value
    normalized = _fc2_migration_identity(value.removeprefix("code:"))
    return value if normalized is None else f"code:{normalized[1]}"


def _migrate_prefetch_value(value: object) -> object:
    if isinstance(value, list):
        return [_migrate_prefetch_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    migrated = {
        str(key): _migrate_prefetch_value(item) for key, item in value.items()
    }
    code = _fc2_migration_identity(migrated.get("code"))
    canonical = _fc2_migration_identity(migrated.get("canonical_code"))
    identities = {item[1] for item in (code, canonical) if item is not None}
    if len(identities) > 1:
        raise MigrationError("detail prefetch catalog identity is inconsistent")
    identity = code or canonical
    if code is not None:
        migrated["code"] = code[0]
    if canonical is not None:
        migrated["canonical_code"] = canonical[1]
    if "code_key" in migrated and identity is not None:
        migrated["code_key"] = identity[1]
    if isinstance(migrated.get("work_id"), str):
        old_work_id = str(migrated["work_id"])
        migrated_work_id = _migrate_prefetch_work_id(old_work_id)
        if identity is not None:
            expected_work_id = f"code:{identity[1]}"
            if migrated_work_id != expected_work_id:
                raise MigrationError("detail prefetch work identity is inconsistent")
            migrated_work_id = expected_work_id
        migrated["work_id"] = migrated_work_id
    return migrated


def _verify_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "detail_prefetch_batches",
        (
            "batch_id",
            "source_scope",
            "config_fingerprint",
            "status",
            "total",
            "queued_count",
            "running_count",
            "completed_count",
            "failed_count",
            "created_at",
            "updated_at",
            "completed_at",
        ),
    )
    require_columns(
        connection,
        "detail_prefetch_items",
        (
            "batch_id",
            "position",
            "work_id",
            "source_key",
            "status",
            "attempts",
            "next_attempt_at",
            "snapshot_json",
            "error_code",
            "cache_hit",
            "created_at",
            "updated_at",
        ),
    )
    require_columns(
        connection,
        "detail_prefetch_cache",
        (
            "work_id",
            "source_key",
            "config_fingerprint",
            "result_json",
            "created_at",
            "updated_at",
        ),
    )
    require_columns(
        connection,
        "detail_prefetch_cache_usage",
        ("singleton", "entry_count", "total_bytes"),
    )


def _batch_payload(
    row: sqlite3.Row, *, items: Sequence[sqlite3.Row] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "batch_id": str(row["batch_id"]),
        "status": _public_batch_status(row),
        "source_scope": str(row["source_scope"]),
        "total": int(row["total"]),
        "queued": int(row["queued_count"]),
        "running": int(row["running_count"]),
        "completed": int(row["completed_count"]),
        "failed": int(row["failed_count"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "completed_at": (
            float(row["completed_at"]) if row["completed_at"] is not None else None
        ),
    }
    if items is not None:
        payload["items"] = [
            {
                "position": int(item["position"]),
                "work_id": str(item["work_id"]),
                "status": str(item["status"]),
                "attempts": int(item["attempts"]),
                "error_code": (
                    str(item["error_code"])
                    if item["error_code"] is not None
                    else None
                ),
                "error": (
                    _ERROR_MESSAGES.get(str(item["error_code"]), "Detail prefetch failed")
                    if item["error_code"] is not None
                    else None
                ),
                "cached": bool(item["cache_hit"]),
                "created_at": float(item["created_at"]),
                "updated_at": float(item["updated_at"]),
            }
            for item in items
        ]
    return payload


def _public_batch_status(row: sqlite3.Row) -> str:
    queued = int(row["queued_count"])
    running = int(row["running_count"])
    completed = int(row["completed_count"])
    failed = int(row["failed_count"])
    if queued or running:
        return str(row["status"])
    if failed == 0:
        return "completed"
    if completed == 0:
        return "failed"
    return "partial"


def _work_source(value: object) -> WorkSource:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch source is invalid")
    source_id = _required_text(value.get("source_id"), "source_id", 64).lower()
    if _SITE_ID_RE.fullmatch(source_id) is None:
        raise DetailPrefetchValidationError("detail prefetch source_id is invalid")
    images_value = value.get("images") or ()
    if not isinstance(images_value, (list, tuple)) or len(images_value) > 120:
        raise DetailPrefetchValidationError("detail prefetch images are invalid")
    parse_status = str(value.get("parse_status") or "summary").strip().lower()
    if parse_status not in {"summary", "resolved", "error"}:
        parse_status = "summary"
    return WorkSource(
        source_id=source_id,
        raw_code=_optional_text(value.get("raw_code"), 128),
        title=_required_text(value.get("title"), "source title", 2_000),
        detail_url=_optional_text(value.get("detail_url"), 4_096),
        release_date=_optional_text(value.get("release_date"), 32),
        images=tuple(_source_image(item) for item in images_value),
        details=_source_details(value.get("details")),
        magnet_hint=_magnet_hint(value.get("magnet_hint")),
        parse_status=parse_status,  # type: ignore[arg-type]
        error=None,
        details_error=None,
        image_error=None,
        magnet_error=None,
        field_sources=_field_sources(value.get("field_sources")),
        detail_provider=_optional_text(value.get("detail_provider"), 64),
        detail_identity_verified=value.get("detail_identity_verified") is True,
    )


def _compact_work_source(value: object) -> WorkSource:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch source is invalid")
    source_id = _required_text(value.get("source_id"), "source_id", 64).lower()
    if _SITE_ID_RE.fullmatch(source_id) is None:
        raise DetailPrefetchValidationError("detail prefetch source_id is invalid")
    return WorkSource(
        source_id=source_id,
        raw_code=_optional_text(value.get("raw_code"), 128),
        title=_required_text(value.get("title"), "source title", 2_000),
        detail_url=_optional_text(value.get("detail_url"), 4_096),
        release_date=_optional_text(value.get("release_date"), 32),
        magnet_hint=_magnet_hint(value.get("magnet_hint")),
        parse_status="summary",
    )


def _field_sources(value: object) -> dict[str, FieldSource]:
    if not isinstance(value, Mapping):
        return {}

    def origin(entry: Mapping) -> FieldSourceOrigin:
        fields: dict[str, str] = {}
        for key in ("source_id", "provider", "url", "upstream_source", "upstream_url"):
            raw = entry.get(key)
            if not isinstance(raw, str):
                continue
            clean = _optional_text(raw, 4096 if key.endswith("url") else 64)
            if clean:
                fields[key] = clean
        return cast(FieldSourceOrigin, fields)

    output: dict[str, FieldSource] = {}
    for name, entry in islice(value.items(), 40):
        if not isinstance(name, str) or not isinstance(entry, Mapping):
            continue
        fields = cast(FieldSource, origin(entry))
        contributors = entry.get("contributors")
        if isinstance(contributors, (list, tuple)):
            # Contributor origins have the same scalar fields, with no nested list.
            clean_contributors = [
                clean for item in contributors[:8]
                if isinstance(item, Mapping) and (clean := origin(item))
            ]
            if clean_contributors:
                fields["contributors"] = clean_contributors
        if fields:
            output[name[:64]] = fields
    return output


def _source_image(value: object) -> SourceImage:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch image is invalid")
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in {"cover", "backdrop", "sample"}:
        raise DetailPrefetchValidationError("detail prefetch image kind is invalid")
    return SourceImage(
        kind=kind,  # type: ignore[arg-type]
        url=_required_text(value.get("url"), "image url", 4_096),
        thumbnail_url=_optional_text(value.get("thumbnail_url"), 4_096),
        width=_optional_int(value.get("width"), 1, 100_000),
        height=_optional_int(value.get("height"), 1, 100_000),
    )


def _source_details(value: object) -> SourceDetails:
    if not isinstance(value, Mapping):
        return SourceDetails()
    rating_value = value.get("rating")
    rating = None
    if isinstance(rating_value, Mapping):
        raw_value = rating_value.get("value")
        rating = Rating(
            value=(float(raw_value) if isinstance(raw_value, (int, float)) else None),
            votes=_optional_int(rating_value.get("votes"), 0, 1_000_000_000),
            text=_optional_text(rating_value.get("text"), 128),
        )
    return SourceDetails(
        title=_optional_text(value.get("title"), 2_000),
        original_title=_optional_text(value.get("original_title"), 2_000),
        release_date=_optional_text(value.get("release_date"), 32),
        duration_minutes=_optional_int(value.get("duration_minutes"), 0, 100_000),
        duration_text=_optional_text(value.get("duration_text"), 128),
        rating=rating,
        makers=_related_refs(value.get("makers")),
        publishers=_related_refs(value.get("publishers")),
        series=_related_refs(value.get("series")),
        directors=_related_refs(value.get("directors")),
        actors=_related_refs(value.get("actors")),
        tags=_related_refs(value.get("tags")),
    )


def _related_refs(value: object) -> tuple[RelatedRef, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    output: list[RelatedRef] = []
    for candidate in value[:500]:
        if not isinstance(candidate, Mapping):
            continue
        kind = str(candidate.get("kind") or "keyword").strip().lower()
        if kind not in {
            "keyword",
            "code",
            "actor",
            "tag",
            "series",
            "maker",
            "publisher",
            "director",
        }:
            kind = "keyword"
        label = _optional_text(candidate.get("label"), 300)
        if label:
            output.append(
                RelatedRef(
                    kind=kind,  # type: ignore[arg-type]
                    label=label,
                    url=_optional_text(candidate.get("url"), 4_096),
                )
            )
    return tuple(output)


def _magnet_group(value: object) -> MagnetGroup:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch magnet is invalid")
    refs_value = value.get("source_refs") or ()
    if not isinstance(refs_value, (list, tuple)) or len(refs_value) > 32:
        raise DetailPrefetchValidationError("detail prefetch magnet sources are invalid")
    size = value.get("size_bytes")
    if size is not None:
        size = _bounded_int(size, "magnet size", 0, 2**63 - 1)
    return MagnetGroup(
        info_hash=_required_text(value.get("info_hash"), "info_hash", 128),
        display_name=_optional_text(value.get("display_name"), 2_000),
        size_bytes=size,
        size_is_exact=bool(value.get("size_is_exact", False)),
        source_refs=tuple(_magnet_ref(item) for item in refs_value),
    )


def _magnet_ref(value: object) -> MagnetSourceRef:
    if not isinstance(value, Mapping):
        raise DetailPrefetchValidationError("detail prefetch magnet source is invalid")
    source_id = _required_text(value.get("source_id"), "source_id", 64).lower()
    if _SITE_ID_RE.fullmatch(source_id) is None:
        raise DetailPrefetchValidationError("detail prefetch source_id is invalid")
    return MagnetSourceRef(
        source_id=source_id,
        uri=_required_text(value.get("uri"), "magnet uri", 16_384),
        display_name=_optional_text(value.get("display_name"), 2_000),
        reported_size_text=_optional_text(value.get("reported_size_text"), 128),
        reported_size_bytes=_optional_int(
            value.get("reported_size_bytes"), 0, 2**63 - 1
        ),
        badges=_text_tuple(value.get("badges"), maximum=32, item_maximum=128),
        trackers=_text_tuple(value.get("trackers"), maximum=64, item_maximum=4_096),
        reported_seeders=_optional_int(value.get("reported_seeders"), 0, 2**31 - 1),
        reported_leechers=_optional_int(value.get("reported_leechers"), 0, 2**31 - 1),
        reported_at=_optional_text(value.get("reported_at"), 64),
    )


def _source_key(snapshot: Mapping[str, object]) -> str:
    sources = snapshot.get("sources")
    if not isinstance(sources, (list, tuple)):
        raise DetailPrefetchValidationError("detail prefetch sources are invalid")
    return _source_key_from_ids(
        [item.get("source_id") for item in sources if isinstance(item, Mapping)]
    )


def _source_key_from_ids(values: Sequence[object]) -> str:
    output: list[str] = []
    for value in values:
        source_id = str(value or "").strip().lower()
        if _SITE_ID_RE.fullmatch(source_id) is None:
            raise DetailPrefetchValidationError("detail prefetch source_id is invalid")
        if source_id not in output:
            output.append(source_id)
    if not output:
        raise DetailPrefetchValidationError("detail prefetch sources are empty")
    return ",".join(sorted(output))


def _work_id(value: object) -> str:
    clean = str(value or "").strip()
    if _WORK_ID_RE.fullmatch(clean) is None:
        raise DetailPrefetchValidationError("detail prefetch work_id is invalid")
    return clean


def _source_scope(value: object) -> str:
    clean = str(value or "all").strip().lower()
    if clean != "all" and _SITE_ID_RE.fullmatch(clean) is None:
        raise DetailPrefetchValidationError("detail prefetch source scope is invalid")
    return clean


def _fingerprint(value: object) -> str:
    clean = str(value or "").strip().lower()
    if _FINGERPRINT_RE.fullmatch(clean) is None:
        raise DetailPrefetchValidationError(
            "detail prefetch configuration fingerprint is invalid"
        )
    return clean


def _batch_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if _BATCH_ID_RE.fullmatch(clean) is None:
        raise DetailPrefetchValidationError("detail prefetch batch_id is invalid")
    return clean


def _required_text(value: object, label: str, maximum: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > maximum or "\x00" in clean:
        raise DetailPrefetchValidationError(f"detail prefetch {label} is invalid")
    return clean


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    if len(clean) > maximum or "\x00" in clean:
        raise DetailPrefetchValidationError("detail prefetch text is invalid")
    return clean


def _text_tuple(
    value: object, *, maximum: int, item_maximum: int
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    output: list[str] = []
    for candidate in value[:maximum]:
        clean = _optional_text(candidate, item_maximum)
        if clean and clean not in output:
            output.append(clean)
    return tuple(output)


def _optional_int(value: object, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, "integer", minimum, maximum)


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise DetailPrefetchValidationError(f"detail prefetch {label} is invalid")
    try:
        clean = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise DetailPrefetchValidationError(
            f"detail prefetch {label} is invalid"
        ) from exc
    if not minimum <= clean <= maximum:
        raise DetailPrefetchValidationError(f"detail prefetch {label} is invalid")
    return clean


def _magnet_hint(value: object) -> str:
    clean = str(value or "unknown").strip().lower()
    return clean if clean in {"available", "unavailable", "unknown"} else "unknown"


def _timestamp(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DetailPrefetchUnavailableError(
            "detail prefetch clock returned an invalid timestamp"
        ) from exc
    if clean < 0:
        raise DetailPrefetchUnavailableError(
            "detail prefetch clock returned an invalid timestamp"
        )
    return clean


def _json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError) as exc:
        raise DetailPrefetchValidationError(
            "detail prefetch JSON value is invalid"
        ) from exc


__all__ = [
    "DETAIL_PREFETCH_SCHEMA_VERSION",
    "DetailPrefetchError",
    "DetailPrefetchManager",
    "DetailPrefetchNotFoundError",
    "DetailPrefetchResolveError",
    "DetailPrefetchStore",
    "DetailPrefetchUnavailableError",
    "DetailPrefetchValidationError",
    "MAX_DETAIL_PREFETCH_ITEMS",
    "compact_work_snapshot",
    "normalize_work_snapshot",
    "select_snapshot_sources",
    "work_result_from_snapshot",
]
