"""SQLite store for web download jobs, queue order and control state."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, Sequence

from ..core.migrations import (
    MigrationError,
    SchemaTooNewError,
    SQLiteMigration,
    migrate_sqlite,
)
from ..notifications.events import NotificationEvent, completed_event, failed_event
from ..notifications.outbox import (
    enqueue_notification_event,
    initialize_notification_schema,
)
from .control import (
    WebDownloadControlError,
    normalize_bandwidth_limit,
    normalize_schedule,
    normalize_target_concurrency,
    normalize_timezone,
    schedule_is_open,
)
from .policy import LEGACY_EXISTING_POLICY, validate_existing_policy
from .quality import validate_selected_height
from .variant import DEFAULT_WEB_DOWNLOAD_VARIANT
from .config import normalize_idempotency_key
from .errors import (
    WebDownloadArchiveUnavailableError,
    WebDownloadConfigError,
    WebDownloadConflictError,
    WebDownloadError,
    WebDownloadNotFoundError,
    WebDownloadRunnerError,
)
from .job_schema import (
    migrate_web_download_v1,
    migrate_web_download_v2,
    migrate_web_download_v3,
    migrate_web_download_v4,
    migrate_web_download_v5,
    migrate_web_download_v6,
    migrate_web_download_v7,
    migrate_web_download_v8,
    migrate_web_download_v9,
    normalize_pending_queue_positions,
    verify_web_download_schema,
)
from .jobs import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    ARCHIVE_MISSING,
    ARCHIVE_UNKNOWN,
    DISK_LOW_NOTIFICATION_INTERVAL_SECONDS,
    DISK_LOW_VOLUME_KEYS,
    JOB_ID_RE,
    LONG_RETRY_DELAYS_SECONDS,
    LONG_RETRY_JITTER_RATIO,
    MAX_CHECKPOINT_ENTRIES,
    MAX_CONCURRENCY,
    MAX_QUEUE_PRIORITY,
    PAUSABLE_WORKER_STATUSES,
    PAUSE_STATUSES,
    PENDING_STATUSES,
    QUALITY_STRATEGIES,
    QUEUED_STATUS,
    RETRY_PROGRESS_BYTES,
    RETRY_PROGRESS_FRAGMENTS,
    RETRY_WAIT_STATUS,
    SLOT_STATUSES,
    TERMINAL_STATUSES,
    UNSET,
    WEB_DOWNLOAD_PROVIDERS,
    WEB_DOWNLOAD_SCHEMA_COMPONENT,
    WEB_DOWNLOAD_SCHEMA_VERSION,
    WORKER_STATUSES,
    normalize_web_download_code,
    redact_worker_error,
    replacement_revision_timestamp,
    sql_slots,
    validate_relative_output_path,
    validate_requested_height,
    validate_web_download_variant,
)

class WebDownloadStore:
    def __init__(
        self, database_path: Path | str, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise WebDownloadConfigError("web download database path must be absolute")
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_or_get(
        self,
        *,
        provider: str,
        code: str,
        code_key: str,
        variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
        idempotency_key: str | None,
        job_id: str,
        requested_height: object | None = None,
        quality_strategy: str = "selected",
        existing_policy: object | None = LEGACY_EXISTING_POLICY,
        incumbent_output_path: object | None = None,
        replaces_job_id: object | None = None,
    ) -> dict[str, object]:
        if provider not in WEB_DOWNLOAD_PROVIDERS or not JOB_ID_RE.fullmatch(
            str(job_id)
        ):
            raise WebDownloadError("web download identity is invalid")
        normalized_code, normalized_key = normalize_web_download_code(code)
        if normalized_code != code or normalized_key != code_key:
            raise WebDownloadError("web download catalog code is not normalized")
        clean_variant = validate_web_download_variant(variant)
        idempotency_key = normalize_idempotency_key(idempotency_key)
        clean_height = (
            None
            if requested_height is None
            else validate_requested_height(requested_height)
        )
        clean_strategy = _validate_quality_strategy(quality_strategy)
        try:
            clean_policy = validate_existing_policy(
                existing_policy,
                default=LEGACY_EXISTING_POLICY,
                allow_legacy=True,
            )
        except ValueError as exc:
            raise WebDownloadError(str(exc)) from exc
        clean_incumbent_path = (
            None
            if incumbent_output_path is None
            else validate_relative_output_path(incumbent_output_path)
        )
        clean_replaces_job_id = (
            None if replaces_job_id is None else str(replaces_job_id)
        )
        if clean_replaces_job_id is not None and not JOB_ID_RE.fullmatch(
            clean_replaces_job_id
        ):
            raise WebDownloadError("replacement job identity is invalid")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = connection.execute(
                    "SELECT * FROM web_download_jobs WHERE provider = ? AND idempotency_key = ?",
                    (provider, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing_job = _row_to_job(row)
                    if (
                        str(existing_job["code"]) != code
                        or existing_job["variant"] != clean_variant
                        or existing_job["requested_height"] != clean_height
                        or existing_job["quality_strategy"] != clean_strategy
                        or existing_job["existing_policy"] != clean_policy
                    ):
                        connection.rollback()
                        raise WebDownloadConflictError(
                            "idempotency key was already used for a different download"
                        )
                    connection.commit()
                    return existing_job
            row = None
            if clean_replaces_job_id is None:
                row = connection.execute(
                    f"SELECT * FROM web_download_jobs WHERE provider = ? AND code_key = ? "
                    f"AND variant = ? "
                    f"AND status IN ({sql_slots(ACTIVE_STATUSES)}) ORDER BY created_at LIMIT 1",
                    (provider, code_key, clean_variant, *ACTIVE_STATUSES),
                ).fetchone()
            if row is not None:
                connection.commit()
                return _row_to_job(row)
            try:
                connection.execute(
                    """
                    INSERT INTO web_download_jobs (
                        job_id, provider, code, code_key, variant, idempotency_key,
                        requested_height, selected_height, quality_strategy,
                        verified_height, existing_policy, incumbent_output_path,
                        replaces_job_id, superseded_by_job_id, publication_outcome,
                        status, progress,
                        downloaded_bytes, total_bytes, speed, eta, created_at,
                        updated_at, error, output_path, priority, queue_position
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, NULL,
                              NULL, 'queued', 0, 0, NULL, 0, NULL, ?, ?, NULL,
                              NULL, 0, (
                                  SELECT COALESCE(MAX(queue_position), 0) + 1
                                  FROM web_download_jobs
                                  WHERE status IN ('queued', 'retry_wait')
                              ))
                    """,
                    (
                        job_id,
                        provider,
                        code,
                        code_key,
                        clean_variant,
                        idempotency_key,
                        clean_height,
                        clean_strategy,
                        clean_policy,
                        clean_incumbent_path,
                        clean_replaces_job_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise WebDownloadConflictError(
                    "a matching web download already exists"
                ) from exc
            row = connection.execute(
                "SELECT * FROM web_download_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise WebDownloadError("web download could not be persisted")
        return _row_to_job(row)

    def get(self, job_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_download_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise WebDownloadNotFoundError("web download was not found")
        return _row_to_job(row)

    def record_disk_low(self, volume_keys: Sequence[str]) -> int:
        clean_keys = _disk_low_volume_keys(volume_keys)
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadError("disk notification timestamp is invalid") from exc
        if not math.isfinite(now) or now < 0:
            raise WebDownloadError("disk notification timestamp is invalid")
        with self._connect(timeout_seconds=0.05) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                created = self._enqueue_disk_low_locked(
                    connection,
                    clean_keys,
                    occurred_at=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return created

    def _enqueue_disk_low_locked(
        self,
        connection: sqlite3.Connection,
        volume_keys: Sequence[str],
        *,
        occurred_at: float,
    ) -> int:
        incident_id = (
            f"window_{int(occurred_at // DISK_LOW_NOTIFICATION_INTERVAL_SECONDS)}"
        )
        created = 0
        for volume_key in volume_keys:
            base_event = NotificationEvent.disk_low(
                volume_key=f"web_download_{volume_key}",
                incident_id=incident_id,
                occurred_at=occurred_at,
            )
            existing = connection.execute(
                "SELECT occurrence_count FROM notification_events WHERE event_id = ?",
                (base_event.event_id,),
            ).fetchone()
            occurrence_count = min(
                int(existing["occurrence_count"]) + 1 if existing is not None else 1,
                1_000_000,
            )
            event = NotificationEvent.disk_low(
                volume_key=f"web_download_{volume_key}",
                incident_id=incident_id,
                occurrence_count=occurrence_count,
                occurred_at=occurred_at,
            )
            created += int(
                enqueue_notification_event(
                    connection,
                    event,
                    clock=self._clock,
                )
            )
        return created

    def list(
        self,
        *,
        status_filter: str = "all",
        limit: int = 100,
        offset: int = 0,
        code_key: str | None = None,
        query_key: str | None = None,
    ) -> list[dict[str, object]]:
        try:
            clean_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise WebDownloadError("web download list limit is invalid") from exc
        clean_limit = max(1, min(clean_limit, 500))
        try:
            clean_offset = int(offset)
        except (TypeError, ValueError) as exc:
            raise WebDownloadError("web download list offset is invalid") from exc
        if clean_offset < 0 or clean_offset > 10_000_000:
            raise WebDownloadError("web download list offset is invalid")
        status = str(status_filter or "all").strip().lower()
        if status != "all" and status not in ALL_STATUSES:
            raise WebDownloadError("web download status filter is invalid")
        clauses: list[str] = []
        values: list[object] = []
        if status != "all":
            clauses.append("status = ?")
            values.append(status)
        if code_key is not None:
            clauses.append("code_key = ?")
            values.append(code_key)
        if query_key is not None:
            clauses.append("code_key LIKE ?")
            values.append(f"%{query_key}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((clean_limit, clean_offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM web_download_jobs{where} "
                "ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count(
        self,
        *,
        status_filter: str = "all",
        code_key: str | None = None,
        query_key: str | None = None,
    ) -> int:
        status = str(status_filter or "all").strip().lower()
        if status != "all" and status not in ALL_STATUSES:
            raise WebDownloadError("web download status filter is invalid")
        clauses: list[str] = []
        values: list[object] = []
        if status != "all":
            clauses.append("status = ?")
            values.append(status)
        if code_key is not None:
            clauses.append("code_key = ?")
            values.append(code_key)
        if query_key is not None:
            clauses.append("code_key LIKE ?")
            values.append(f"%{query_key}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM web_download_jobs{where}", values
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def summary(self) -> dict[str, int | float]:
        running_slots = ", ".join("?" for _ in SLOT_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ({running_slots}) THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status = 'retry_wait' THEN 1 ELSE 0 END) AS retrying,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status IN ({running_slots}) THEN speed ELSE 0 END) AS speed
                FROM web_download_jobs
                """,
                (*SLOT_STATUSES, *SLOT_STATUSES),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "running": int(row["running"] or 0),
            "queued": int(row["queued"] or 0),
            "retrying": int(row["retrying"] or 0),
            "completed": int(row["completed"] or 0),
            "failed": int(row["failed"] or 0),
            "speed": float(row["speed"] or 0),
        }

    def control(self, *, hard_limit: int = MAX_CONCURRENCY) -> dict[str, object]:
        clean_hard_limit = normalize_target_concurrency(
            hard_limit, hard_limit=MAX_CONCURRENCY
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_download_control WHERE id = 1"
            ).fetchone()
            if (
                row is not None
                and int(row["target_concurrency"]) <= clean_hard_limit
                and float(row["updated_at"]) > 0
            ):
                return _control_row_to_dict(row)
            connection.execute("BEGIN IMMEDIATE")
            row = self._control_row_locked(connection, clean_hard_limit)
            result = _control_row_to_dict(row)
            connection.commit()
        return result

    def update_control(
        self,
        *,
        hard_limit: int = MAX_CONCURRENCY,
        target_concurrency: object = UNSET,
        bandwidth_limit: object = UNSET,
        timezone: object = UNSET,
        schedule: object = UNSET,
    ) -> dict[str, object]:
        clean_hard_limit = normalize_target_concurrency(
            hard_limit, hard_limit=MAX_CONCURRENCY
        )
        values: dict[str, object] = {}
        try:
            if target_concurrency is not UNSET:
                values["target_concurrency"] = normalize_target_concurrency(
                    target_concurrency, hard_limit=clean_hard_limit
                )
            if bandwidth_limit is not UNSET:
                values["bandwidth_limit"] = normalize_bandwidth_limit(bandwidth_limit)
            if timezone is not UNSET:
                values["timezone"] = normalize_timezone(timezone)
            if schedule is not UNSET:
                values["schedule_json"] = normalize_schedule(schedule)
        except WebDownloadControlError as exc:
            raise WebDownloadError(str(exc)) from exc
        if not values:
            raise WebDownloadError("web download control update is empty")
        values["updated_at"] = self._clock()
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._control_row_locked(connection, clean_hard_limit)
            connection.execute(
                f"UPDATE web_download_control SET {assignments} WHERE id = 1",
                tuple(values.values()),
            )
            row = connection.execute(
                "SELECT * FROM web_download_control WHERE id = 1"
            ).fetchone()
            connection.commit()
        if row is None:
            raise WebDownloadError("web download control could not be persisted")
        return _control_row_to_dict(row)

    def set_global_paused(
        self, paused: bool, *, hard_limit: int = MAX_CONCURRENCY
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        if not isinstance(paused, bool):
            raise WebDownloadError("global pause state is invalid")
        clean_hard_limit = normalize_target_concurrency(
            hard_limit, hard_limit=MAX_CONCURRENCY
        )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._control_row_locked(connection, clean_hard_limit)
            connection.execute(
                "UPDATE web_download_control SET global_paused = ?, updated_at = ? "
                "WHERE id = 1",
                (int(paused), now),
            )
            pausing_ids: tuple[str, ...] = ()
            if paused:
                rows = connection.execute(
                    f"SELECT job_id FROM web_download_jobs "
                    f"WHERE status IN ({sql_slots(PAUSABLE_WORKER_STATUSES)})",
                    PAUSABLE_WORKER_STATUSES,
                ).fetchall()
                pausing_ids = tuple(str(row["job_id"]) for row in rows)
                connection.execute(
                    f"UPDATE web_download_jobs SET status = 'pausing', speed = 0, "
                    f"eta = NULL, pause_origin = 'global', updated_at = ? "
                    f"WHERE status IN ({sql_slots(PAUSABLE_WORKER_STATUSES)})",
                    (now, *PAUSABLE_WORKER_STATUSES),
                )
            else:
                rows = connection.execute(
                    "SELECT job_id FROM web_download_jobs "
                    "WHERE status = 'paused' AND pause_origin = 'global' "
                    "ORDER BY priority DESC, queue_position, created_at, job_id"
                ).fetchall()
                next_position_row = connection.execute(
                    "SELECT COALESCE(MAX(queue_position), 0) AS position "
                    "FROM web_download_jobs "
                    "WHERE status IN ('queued', 'retry_wait')"
                ).fetchone()
                next_position = int(
                    next_position_row["position"]
                    if next_position_row is not None
                    else 0
                )
                for resumed in rows:
                    next_position += 1
                    connection.execute(
                        "UPDATE web_download_jobs SET status = 'queued', "
                        "pause_origin = NULL, queue_position = ?, updated_at = ? "
                        "WHERE job_id = ? AND status = 'paused' "
                        "AND pause_origin = 'global'",
                        (next_position, now, str(resumed["job_id"])),
                    )
            row = connection.execute(
                "SELECT * FROM web_download_control WHERE id = 1"
            ).fetchone()
            connection.commit()
        if row is None:
            raise WebDownloadError("web download control could not be persisted")
        return _control_row_to_dict(row), pausing_ids

    def pause(self, job_id: str) -> dict[str, object]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            status = str(row["status"])
            if status in {QUEUED_STATUS, RETRY_WAIT_STATUS}:
                new_status = "paused"
            elif status in PAUSABLE_WORKER_STATUSES:
                new_status = "pausing"
            elif status in PAUSE_STATUSES:
                new_status = status
            elif status in {"verifying", "archiving"}:
                connection.rollback()
                raise WebDownloadConflictError(
                    "web download is finalizing and cannot be paused"
                )
            else:
                connection.rollback()
                raise WebDownloadConflictError("web download can no longer be paused")
            if new_status != status:
                connection.execute(
                    "UPDATE web_download_jobs SET status = ?, speed = 0, eta = NULL, "
                    "pause_origin = 'manual', updated_at = ? "
                    "WHERE job_id = ? AND status = ?",
                    (new_status, now, str(job_id), status),
                )
            elif status in PAUSE_STATUSES:
                connection.execute(
                    "UPDATE web_download_jobs SET pause_origin = 'manual', "
                    "updated_at = ? WHERE job_id = ?",
                    (now, str(job_id)),
                )
            connection.commit()
        return self.get(job_id)

    def resume(self, job_id: str) -> dict[str, object]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, next_retry_at FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            status = str(row["status"])
            if status == QUEUED_STATUS:
                connection.commit()
                return self.get(job_id)
            if status == "pausing":
                connection.rollback()
                raise WebDownloadConflictError("web download has not finished pausing")
            if status != "paused":
                connection.rollback()
                raise WebDownloadConflictError("web download can no longer be resumed")
            if row["next_retry_at"] is not None:
                connection.execute(
                    "UPDATE web_download_jobs SET status = 'retry_wait', speed = 0, "
                    "eta = NULL, updated_at = ?, pause_origin = NULL, "
                    "queue_position = ("
                    "SELECT COALESCE(MAX(queue_position), 0) + 1 "
                    "FROM web_download_jobs "
                    "WHERE status IN ('queued', 'retry_wait')) "
                    "WHERE job_id = ? AND status = 'paused'",
                    (now, str(job_id)),
                )
            else:
                connection.execute(
                    """
                    UPDATE web_download_jobs
                    SET status = 'queued', speed = 0, eta = NULL, updated_at = ?,
                        error = NULL, pause_origin = NULL, queue_position = (
                            SELECT COALESCE(MAX(queue_position), 0) + 1
                            FROM web_download_jobs
                            WHERE status IN ('queued', 'retry_wait')
                        )
                    WHERE job_id = ? AND status = 'paused'
                    """,
                    (now, str(job_id)),
                )
            connection.commit()
        return self.get(job_id)

    def set_priority(
        self,
        job_id: str,
        priority: object,
        *,
        expected_revision: object | None = None,
    ) -> dict[str, object]:
        clean_priority = _normalize_queue_priority(priority)
        clean_revision = _normalize_queue_revision(expected_revision)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = self._control_row_locked(connection, MAX_CONCURRENCY)
            if (
                clean_revision is not None
                and int(control["queue_revision"]) != clean_revision
            ):
                connection.rollback()
                raise WebDownloadConflictError("web download queue revision is stale")
            row = connection.execute(
                "SELECT status FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            if str(row["status"]) not in {*PENDING_STATUSES, "paused"}:
                connection.rollback()
                raise WebDownloadConflictError(
                    "only pending or paused downloads can change priority"
                )
            connection.execute(
                "UPDATE web_download_jobs SET priority = ?, updated_at = ? "
                "WHERE job_id = ?",
                (clean_priority, self._clock(), str(job_id)),
            )
            connection.commit()
        return self.get(job_id)

    def reorder(
        self,
        job_ids: Sequence[str],
        *,
        expected_revision: object,
    ) -> dict[str, object]:
        clean_revision = _normalize_queue_revision(expected_revision)
        if clean_revision is None:
            raise WebDownloadError("web download queue revision is required")
        clean_ids = tuple(str(job_id) for job_id in job_ids)
        if (
            not clean_ids
            or len(clean_ids) > 10_000
            or len(set(clean_ids)) != len(clean_ids)
            or any(not JOB_ID_RE.fullmatch(job_id) for job_id in clean_ids)
        ):
            raise WebDownloadError("web download queue order is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = self._control_row_locked(connection, MAX_CONCURRENCY)
            if int(control["queue_revision"]) != clean_revision:
                connection.rollback()
                raise WebDownloadConflictError("web download queue revision is stale")
            rows = connection.execute(
                "SELECT job_id, status, queue_position FROM web_download_jobs "
                "WHERE status IN ('queued', 'retry_wait') "
                "ORDER BY priority DESC, queue_position, created_at, job_id"
            ).fetchall()
            current = [str(row["job_id"]) for row in rows]
            queued = {
                str(row["job_id"])
                for row in rows
                if str(row["status"]) == QUEUED_STATUS
            }
            old_positions = {
                str(row["job_id"]): int(row["queue_position"]) for row in rows
            }
            selected = set(clean_ids)
            if not selected.issubset(queued):
                connection.rollback()
                raise WebDownloadConflictError(
                    "web download queue changed before it could be reordered"
                )
            positions = [
                index for index, job_id in enumerate(current) if job_id in selected
            ]
            for index, job_id in zip(positions, clean_ids, strict=True):
                current[index] = job_id
            now = self._clock()
            for position, job_id in enumerate(current, start=1):
                if old_positions[job_id] == position:
                    continue
                connection.execute(
                    "UPDATE web_download_jobs SET queue_position = ?, updated_at = ? "
                    "WHERE job_id = ? AND status IN ('queued', 'retry_wait')",
                    (position, now, job_id),
                )
            row = connection.execute(
                "SELECT * FROM web_download_control WHERE id = 1"
            ).fetchone()
            connection.commit()
        if row is None:
            raise WebDownloadError("web download queue could not be persisted")
        return _control_row_to_dict(row)

    def _control_row_locked(
        self, connection: sqlite3.Connection, hard_limit: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM web_download_control WHERE id = 1"
        ).fetchone()
        if row is None:
            raise WebDownloadError("web download control is unavailable")
        target_concurrency = min(int(row["target_concurrency"]), hard_limit)
        if (
            target_concurrency != int(row["target_concurrency"])
            or float(row["updated_at"]) <= 0
        ):
            connection.execute(
                "UPDATE web_download_control SET target_concurrency = ?, updated_at = ? "
                "WHERE id = 1",
                (target_concurrency, self._clock()),
            )
            row = connection.execute(
                "SELECT * FROM web_download_control WHERE id = 1"
            ).fetchone()
        if row is None:
            raise WebDownloadError("web download control is unavailable")
        return row

    def peek_next_runnable(self) -> dict[str, object] | None:
        timestamp = self._clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_download_jobs "
                "WHERE status = 'queued' OR "
                "(status = 'retry_wait' AND next_retry_at <= ?) "
                "ORDER BY priority DESC, queue_position, created_at, job_id LIMIT 1",
                (timestamp,),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def claim_next_runnable(
        self,
        expected_job_id: str | None = None,
        *,
        hard_limit: int = MAX_CONCURRENCY,
        now: datetime | None = None,
    ) -> dict[str, object] | None:
        clean_hard_limit = normalize_target_concurrency(
            hard_limit, hard_limit=MAX_CONCURRENCY
        )
        updated_at = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = self._control_row_locked(connection, clean_hard_limit)
            schedule_now = (
                now
                if isinstance(now, datetime)
                else datetime.fromtimestamp(float(updated_at), UTC)
            )
            if not self._admission_open_locked(connection, control, schedule_now):
                connection.commit()
                return None
            row = connection.execute(
                "SELECT job_id FROM web_download_jobs "
                "WHERE status = 'queued' OR "
                "(status = 'retry_wait' AND next_retry_at <= ?) "
                "ORDER BY priority DESC, queue_position, created_at, job_id LIMIT 1",
                (updated_at,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job_id = str(row["job_id"])
            if expected_job_id is not None and job_id != str(expected_job_id):
                connection.commit()
                return None
            changed = connection.execute(
                "UPDATE web_download_jobs SET status = 'locating', "
                "next_retry_at = NULL, error = NULL, speed = 0, eta = NULL, "
                "failure_stage = NULL, failure_code = NULL, "
                "retry_progress_bytes = CASE WHEN retry_count > 0 "
                "THEN checkpoint_bytes ELSE NULL END, "
                "retry_progress_fragments = CASE WHEN retry_count > 0 "
                "THEN checkpoint_fragments ELSE NULL END, "
                "updated_at = ? WHERE job_id = ? AND (status = 'queued' OR "
                "(status = 'retry_wait' AND next_retry_at <= ?))",
                (updated_at, job_id, updated_at),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM web_download_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        return _row_to_claimed_job(claimed) if claimed is not None else None

    def claim_admission_open(
        self,
        *,
        hard_limit: int = MAX_CONCURRENCY,
        now: datetime | None = None,
    ) -> bool:
        clean_hard_limit = normalize_target_concurrency(
            hard_limit, hard_limit=MAX_CONCURRENCY
        )
        timestamp = self._clock()
        schedule_now = (
            now
            if isinstance(now, datetime)
            else datetime.fromtimestamp(float(timestamp), UTC)
        )
        with self._connect() as connection:
            connection.execute("BEGIN")
            control = connection.execute(
                "SELECT * FROM web_download_control WHERE id = 1"
            ).fetchone()
            if control is None:
                connection.rollback()
                raise WebDownloadError("web download control is unavailable")
            result = self._admission_open_locked(
                connection,
                control,
                schedule_now,
                target_concurrency=min(
                    int(control["target_concurrency"]), clean_hard_limit
                ),
            )
            connection.commit()
        return result

    @staticmethod
    def _admission_open_locked(
        connection: sqlite3.Connection,
        control: sqlite3.Row,
        now: datetime,
        *,
        target_concurrency: int | None = None,
    ) -> bool:
        active_row = connection.execute(
            f"SELECT COUNT(*) AS count FROM web_download_jobs "
            f"WHERE status IN ({sql_slots(SLOT_STATUSES)})",
            SLOT_STATUSES,
        ).fetchone()
        active_count = int(active_row["count"] if active_row is not None else 0)
        return (
            not bool(control["global_paused"])
            and active_count
            < (
                int(control["target_concurrency"])
                if target_concurrency is None
                else target_concurrency
            )
            and schedule_is_open(
                control["schedule_json"],
                control["timezone"],
                now=now,
            )
        )

    def has_runnable(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM web_download_jobs WHERE status = 'queued' OR "
                "(status = 'retry_wait' AND next_retry_at <= ?) LIMIT 1",
                (self._clock(),),
            ).fetchone()
        return row is not None

    def update(
        self,
        job_id: str,
        *,
        disk_low_volume_key: str | None = None,
        checkpoint_reset: bool = False,
        checkpoint_reconcile: bool = False,
        **fields: object,
    ) -> dict[str, object]:
        allowed = {
            "status",
            "progress",
            "downloaded_bytes",
            "total_bytes",
            "speed",
            "eta",
            "error",
            "failure_stage",
            "failure_code",
            "resolved_provider",
            "output_path",
            "selected_height",
            "verified_height",
            "publication_outcome",
            "superseded_by_job_id",
            "checkpoint_bytes",
            "checkpoint_fragments",
        }
        if not fields or not set(fields).issubset(allowed):
            raise WebDownloadError("web download update contains invalid fields")
        if not isinstance(checkpoint_reset, bool):
            raise WebDownloadError("web download checkpoint reset is invalid")
        if not isinstance(checkpoint_reconcile, bool):
            raise WebDownloadError("web download checkpoint reconciliation is invalid")
        if checkpoint_reset and checkpoint_reconcile:
            raise WebDownloadError("web download checkpoint progress mode is invalid")
        clean_disk_low_keys = (
            ()
            if disk_low_volume_key is None
            else _disk_low_volume_keys((disk_low_volume_key,))
        )
        if clean_disk_low_keys and fields.get("status") != "failed":
            raise WebDownloadError("disk notification requires a failed web download")
        if "error" in fields and fields["error"] is not None:
            fields["error"] = redact_worker_error(fields["error"])
        for name, maximum in (("failure_stage", 32), ("failure_code", 64)):
            if name in fields and fields[name] is not None:
                value = fields[name]
                if not isinstance(value, str) or not value or len(value) > maximum:
                    raise WebDownloadError(
                        "web download failure classification is invalid"
                    )
        if "resolved_provider" in fields:
            value = str(fields["resolved_provider"] or "").strip().lower()
            if value not in {"missav", "jable", "supjav"}:
                raise WebDownloadError("web download source provider is invalid")
            fields["resolved_provider"] = value
        if "output_path" in fields and fields["output_path"] is not None:
            fields["output_path"] = validate_relative_output_path(
                fields["output_path"]
            )
        if "selected_height" in fields and fields["selected_height"] is not None:
            if isinstance(fields["selected_height"], bool) or not isinstance(
                fields["selected_height"], int
            ):
                raise WebDownloadError("selected video height is invalid")
            try:
                fields["selected_height"] = validate_selected_height(
                    fields["selected_height"]
                )
            except ValueError as exc:
                raise WebDownloadError("selected video height is invalid") from exc
        if "verified_height" in fields and fields["verified_height"] is not None:
            if isinstance(fields["verified_height"], bool) or not isinstance(
                fields["verified_height"], int
            ):
                raise WebDownloadError("verified video height is invalid")
            try:
                fields["verified_height"] = validate_selected_height(
                    fields["verified_height"]
                )
            except ValueError as exc:
                raise WebDownloadError("verified video height is invalid") from exc
        if (
            "publication_outcome" in fields
            and fields["publication_outcome"] is not None
        ):
            if fields["publication_outcome"] not in {
                "published",
                "replaced",
                "kept_existing",
            }:
                raise WebDownloadError("web download publication outcome is invalid")
        if (
            "superseded_by_job_id" in fields
            and fields["superseded_by_job_id"] is not None
        ):
            if not isinstance(
                fields["superseded_by_job_id"], str
            ) or not JOB_ID_RE.fullmatch(fields["superseded_by_job_id"]):
                raise WebDownloadError("replacement job identity is invalid")
        checkpoint_field_names = {"checkpoint_bytes", "checkpoint_fragments"}
        present_checkpoint_fields = checkpoint_field_names.intersection(fields)
        if (
            present_checkpoint_fields
            and present_checkpoint_fields != checkpoint_field_names
        ):
            raise WebDownloadError("web download checkpoint progress is incomplete")
        for name in present_checkpoint_fields:
            value = fields[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WebDownloadError("web download checkpoint progress is invalid")
        if checkpoint_reset and present_checkpoint_fields != checkpoint_field_names:
            raise WebDownloadError("web download checkpoint reset is invalid")
        if checkpoint_reconcile and present_checkpoint_fields != checkpoint_field_names:
            raise WebDownloadError("web download checkpoint reconciliation is invalid")
        if checkpoint_reset:
            reset_downloaded = fields.get("downloaded_bytes")
            if (
                "progress" not in fields
                or isinstance(reset_downloaded, bool)
                or not isinstance(reset_downloaded, int)
                or reset_downloaded != fields["checkpoint_bytes"]
            ):
                raise WebDownloadError("web download checkpoint reset is invalid")
        if checkpoint_reconcile:
            reconciled_downloaded = fields.get("downloaded_bytes")
            if (
                "progress" not in fields
                or isinstance(reconciled_downloaded, bool)
                or not isinstance(reconciled_downloaded, int)
                or reconciled_downloaded != fields["checkpoint_bytes"]
            ):
                raise WebDownloadError(
                    "web download checkpoint reconciliation is invalid"
                )
        fields["updated_at"] = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT status, code, retry_count, retry_progress_bytes, "
                "retry_progress_fragments, checkpoint_bytes, "
                "checkpoint_fragments FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if previous is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            if present_checkpoint_fields:
                checkpoint_bytes = int(fields["checkpoint_bytes"])
                checkpoint_fragments = int(fields["checkpoint_fragments"])
                moved_backwards = checkpoint_bytes < int(
                    previous["checkpoint_bytes"]
                ) or checkpoint_fragments < int(previous["checkpoint_fragments"])
                if moved_backwards and not checkpoint_reset:
                    connection.rollback()
                    raise WebDownloadError(
                        "web download checkpoint progress moved backwards"
                    )
            if (
                present_checkpoint_fields
                and int(previous["retry_count"]) > 0
                and not checkpoint_reconcile
            ):
                baseline_bytes = previous["retry_progress_bytes"]
                baseline_fragments = previous["retry_progress_fragments"]
                if (
                    baseline_bytes is None
                    or baseline_fragments is None
                    or checkpoint_reset
                ):
                    fields["retry_progress_bytes"] = checkpoint_bytes
                    fields["retry_progress_fragments"] = checkpoint_fragments
                elif (
                    checkpoint_bytes - int(baseline_bytes) >= RETRY_PROGRESS_BYTES
                    or checkpoint_fragments - int(baseline_fragments)
                    >= RETRY_PROGRESS_FRAGMENTS
                ):
                    fields["retry_count"] = 0
                    fields["retry_progress_bytes"] = None
                    fields["retry_progress_fragments"] = None
            if fields.get("status") in TERMINAL_STATUSES:
                fields["next_retry_at"] = None
                fields["retry_progress_bytes"] = None
                fields["retry_progress_fragments"] = None
                if fields.get("status") != "failed":
                    fields["failure_stage"] = None
                    fields["failure_code"] = None
                if fields.get("status") == "completed":
                    fields["retry_count"] = 0
            assignments = ", ".join(f"{key} = ?" for key in fields)
            changed = connection.execute(
                f"UPDATE web_download_jobs SET {assignments} WHERE job_id = ?",
                (*fields.values(), str(job_id)),
            ).rowcount
            if changed == 1 and fields.get("status") == "completed":
                completed = connection.execute(
                    "SELECT replaces_job_id, publication_outcome "
                    "FROM web_download_jobs WHERE job_id = ?",
                    (str(job_id),),
                ).fetchone()
                if (
                    completed is not None
                    and completed["replaces_job_id"] is not None
                    and str(completed["publication_outcome"]) == "replaced"
                ):
                    connection.execute(
                        "UPDATE web_download_jobs SET superseded_by_job_id = ?, "
                        "updated_at = ? WHERE job_id = ? AND status = 'completed'",
                        (
                            str(job_id),
                            fields["updated_at"],
                            str(completed["replaces_job_id"]),
                        ),
                    )
            next_status = str(fields.get("status") or previous["status"])
            if (
                next_status in {"completed", "failed"}
                and str(previous["status"]) != next_status
            ):
                event = (
                    completed_event(
                        source="web_download",
                        entity_id=str(job_id),
                        code=str(previous["code"]),
                    )
                    if next_status == "completed"
                    else failed_event(
                        source="web_download",
                        entity_id=str(job_id),
                        code=str(previous["code"]),
                        error_code="download_failed",
                    )
                )
                enqueue_notification_event(connection, event, clock=self._clock)
            if clean_disk_low_keys:
                self._enqueue_disk_low_locked(
                    connection,
                    clean_disk_low_keys,
                    occurred_at=float(fields["updated_at"]),
                )
            connection.commit()
        if changed != 1:
            raise WebDownloadNotFoundError("web download was not found")
        return self.get(job_id)

    def defer_transient_failure(
        self,
        job_id: str,
        error: object,
        *,
        checkpoint_bytes: int | None,
        checkpoint_fragments: int | None,
        max_checkpoint_bytes: int,
        checkpoint_reset: bool = False,
        failure_stage: str = "media",
        failure_code: str = "media_transport_transient",
    ) -> dict[str, object]:
        if not isinstance(checkpoint_reset, bool):
            raise WebDownloadError("web download checkpoint reset is invalid")
        if (checkpoint_bytes is None) != (checkpoint_fragments is None):
            raise WebDownloadError("web download checkpoint progress is incomplete")
        if (
            isinstance(max_checkpoint_bytes, bool)
            or not isinstance(max_checkpoint_bytes, int)
            or max_checkpoint_bytes <= 0
        ):
            raise WebDownloadError("web download checkpoint progress is invalid")
        if checkpoint_bytes is not None and checkpoint_fragments is not None:
            for value in (checkpoint_bytes, checkpoint_fragments):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise WebDownloadError(
                        "web download checkpoint progress is invalid"
                    )
        elif checkpoint_reset:
            raise WebDownloadError("web download checkpoint reset is invalid")
        if (
            checkpoint_bytes is not None
            and checkpoint_fragments is not None
            and (
                checkpoint_bytes > max_checkpoint_bytes
                or checkpoint_fragments > MAX_CHECKPOINT_ENTRIES
            )
        ):
            raise WebDownloadError("web download checkpoint progress is invalid")
        if (
            not isinstance(failure_stage, str)
            or not failure_stage
            or len(failure_stage) > 32
            or not isinstance(failure_code, str)
            or not failure_code
            or len(failure_code) > 64
        ):
            raise WebDownloadError("web download failure classification is invalid")
        now = self._clock()
        safe_error = redact_worker_error(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, code, retry_count, checkpoint_bytes, "
                "checkpoint_fragments, retry_progress_bytes, "
                "retry_progress_fragments FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            if str(row["status"]) not in WORKER_STATUSES:
                connection.rollback()
                raise WebDownloadConflictError("web download can no longer be deferred")
            has_checkpoint_snapshot = checkpoint_bytes is not None
            effective_checkpoint_bytes = (
                int(checkpoint_bytes)
                if checkpoint_bytes is not None
                else int(row["checkpoint_bytes"])
            )
            effective_checkpoint_fragments = (
                int(checkpoint_fragments)
                if checkpoint_fragments is not None
                else int(row["checkpoint_fragments"])
            )
            retry_count = int(row["retry_count"])
            baseline_bytes = row["retry_progress_bytes"]
            baseline_fragments = row["retry_progress_fragments"]
            if has_checkpoint_snapshot:
                moved_backwards = effective_checkpoint_bytes < int(
                    row["checkpoint_bytes"]
                ) or effective_checkpoint_fragments < int(row["checkpoint_fragments"])
                if moved_backwards and not checkpoint_reset:
                    connection.rollback()
                    raise WebDownloadError(
                        "web download checkpoint progress moved backwards"
                    )
            if (
                retry_count > 0
                and has_checkpoint_snapshot
                and baseline_bytes is not None
                and baseline_fragments is not None
                and not checkpoint_reset
            ):
                if (
                    effective_checkpoint_bytes - int(baseline_bytes)
                    >= RETRY_PROGRESS_BYTES
                    or effective_checkpoint_fragments - int(baseline_fragments)
                    >= RETRY_PROGRESS_FRAGMENTS
                ):
                    retry_count = 0
            if retry_count < len(LONG_RETRY_DELAYS_SECONDS):
                next_count = retry_count + 1
                next_retry_at = now + _long_retry_delay_seconds(str(job_id), next_count)
                changed = connection.execute(
                    """
                    UPDATE web_download_jobs
                    SET status = 'retry_wait', retry_count = ?, next_retry_at = ?,
                        retry_progress_bytes = NULL,
                        retry_progress_fragments = NULL,
                        checkpoint_bytes = ?, checkpoint_fragments = ?,
                        downloaded_bytes = ?, speed = 0, eta = NULL,
                        updated_at = ?, error = ?, failure_stage = ?, failure_code = ?,
                        output_path = NULL, verified_height = NULL,
                        publication_outcome = NULL,
                        selected_height = CASE
                            WHEN quality_strategy = 'highest' THEN selected_height
                            ELSE NULL
                        END,
                        queue_position = (
                            SELECT COALESCE(MAX(queue_position), 0) + 1
                            FROM web_download_jobs
                            WHERE status IN ('queued', 'retry_wait')
                        )
                    WHERE job_id = ? AND status = ?
                    """,
                    (
                        next_count,
                        next_retry_at,
                        effective_checkpoint_bytes,
                        effective_checkpoint_fragments,
                        effective_checkpoint_bytes,
                        now,
                        safe_error,
                        failure_stage,
                        failure_code,
                        str(job_id),
                        str(row["status"]),
                    ),
                ).rowcount
            else:
                changed = connection.execute(
                    "UPDATE web_download_jobs SET status = 'failed', "
                    "next_retry_at = NULL, retry_progress_bytes = NULL, "
                    "retry_progress_fragments = NULL, speed = 0, eta = NULL, "
                    "checkpoint_bytes = ?, checkpoint_fragments = ?, "
                    "downloaded_bytes = ?, updated_at = ?, error = ?, "
                    "failure_stage = ?, failure_code = ?, "
                    "output_path = NULL "
                    "WHERE job_id = ? AND status = ?",
                    (
                        effective_checkpoint_bytes,
                        effective_checkpoint_fragments,
                        effective_checkpoint_bytes,
                        now,
                        safe_error,
                        failure_stage,
                        failure_code,
                        str(job_id),
                        str(row["status"]),
                    ),
                ).rowcount
                if changed == 1:
                    enqueue_notification_event(
                        connection,
                        failed_event(
                            source="web_download",
                            entity_id=str(job_id),
                            code=str(row["code"]),
                            error_code="download_failed",
                        ),
                        clock=self._clock,
                    )
            if changed != 1:
                connection.rollback()
                raise WebDownloadConflictError(
                    "web download failure state changed before it was persisted"
                )
            connection.commit()
        return self.get(job_id)

    def recover_interrupted(self) -> list[dict[str, str]]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resumable = connection.execute(
                f"SELECT job_id, code FROM web_download_jobs "
                f"WHERE status IN ({sql_slots(WORKER_STATUSES)})",
                WORKER_STATUSES,
            ).fetchall()
            cancelling = connection.execute(
                "SELECT job_id, code FROM web_download_jobs WHERE status = 'cancelling'"
            ).fetchall()
            pausing = connection.execute(
                "SELECT job_id, code, pause_origin FROM web_download_jobs "
                "WHERE status = 'pausing'"
            ).fetchall()
            control = connection.execute(
                "SELECT global_paused FROM web_download_control WHERE id = 1"
            ).fetchone()
            if control is None:
                connection.rollback()
                raise WebDownloadError("web download control is unavailable")
            resumed = connection.execute(
                f"""
                UPDATE web_download_jobs
                SET status = 'queued', progress = 0, downloaded_bytes = 0,
                    total_bytes = NULL, speed = 0, eta = NULL, updated_at = ?,
                    error = NULL, failure_stage = NULL, failure_code = NULL,
                    resolved_provider = CASE
                        WHEN checkpoint_bytes = 0 AND checkpoint_fragments = 0
                            THEN NULL
                        ELSE resolved_provider
                    END,
                    output_path = NULL, pause_origin = NULL,
                    verified_height = NULL, publication_outcome = NULL,
                    retry_progress_bytes = NULL,
                    retry_progress_fragments = NULL,
                    selected_height = CASE
                        WHEN quality_strategy = 'highest' THEN selected_height
                        ELSE NULL
                    END
                WHERE status IN ({sql_slots(WORKER_STATUSES)})
                """,
                (now, *WORKER_STATUSES),
            ).rowcount
            cancelled = connection.execute(
                """
                UPDATE web_download_jobs
                SET status = 'cancelled', speed = 0, eta = NULL,
                    updated_at = ?, error = NULL, output_path = NULL,
                    pause_origin = NULL, next_retry_at = NULL,
                    retry_progress_bytes = NULL,
                    retry_progress_fragments = NULL
                WHERE status = 'cancelling'
                """,
                (now,),
            ).rowcount
            next_position_row = connection.execute(
                "SELECT COALESCE(MAX(queue_position), 0) AS position "
                "FROM web_download_jobs "
                "WHERE status IN ('queued', 'retry_wait')"
            ).fetchone()
            next_position = int(
                next_position_row["position"] if next_position_row is not None else 0
            )
            paused = 0
            for interrupted in pausing:
                resume_global = str(
                    interrupted["pause_origin"] or ""
                ) == "global" and not bool(control["global_paused"])
                if resume_global:
                    next_position += 1
                    changed = connection.execute(
                        "UPDATE web_download_jobs SET status = 'queued', speed = 0, "
                        "eta = NULL, updated_at = ?, error = NULL, "
                        "output_path = NULL, pause_origin = NULL, "
                        "retry_progress_bytes = NULL, "
                        "retry_progress_fragments = NULL, queue_position = ? "
                        "WHERE job_id = ? AND status = 'pausing'",
                        (now, next_position, str(interrupted["job_id"])),
                    ).rowcount
                else:
                    changed = connection.execute(
                        "UPDATE web_download_jobs SET status = 'paused', speed = 0, "
                        "eta = NULL, updated_at = ?, error = NULL, output_path = NULL, "
                        "retry_progress_bytes = NULL, "
                        "retry_progress_fragments = NULL "
                        "WHERE job_id = ? AND status = 'pausing'",
                        (now, str(interrupted["job_id"])),
                    ).rowcount
                paused += changed
            if (
                resumed != len(resumable)
                or cancelled != len(cancelling)
                or paused != len(pausing)
            ):
                connection.rollback()
                raise WebDownloadError(
                    "interrupted web downloads could not be recovered consistently"
                )
            normalize_pending_queue_positions(connection, updated_at=now)
            connection.commit()
        return [
            {"job_id": str(row["job_id"]), "code": str(row["code"])}
            for row in (*resumable, *cancelling, *pausing)
        ]

    def browser_profile_cleanup_job_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT job_id FROM web_download_jobs").fetchall()
        return tuple(str(row["job_id"]) for row in rows)

    def artifact_cleanup_candidates(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, code, output_path, incumbent_output_path "
                "FROM web_download_jobs WHERE status = 'completed'",
            ).fetchall()
        return [
            {
                "job_id": str(row["job_id"]),
                "code": str(row["code"]),
                "output_path": row["output_path"],
                "incumbent_output_path": row["incumbent_output_path"],
            }
            for row in rows
        ]

    def completed_archive_candidates(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, output_path FROM web_download_jobs "
                "WHERE status = 'completed' AND output_path IS NOT NULL "
                "AND superseded_by_job_id IS NULL"
            ).fetchall()
        return [
            {
                "job_id": str(row["job_id"]),
                "output_path": str(row["output_path"]),
            }
            for row in rows
        ]

    def completed_metadata_candidates(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM web_download_jobs "
                "WHERE status = 'completed' AND output_path IS NOT NULL "
                "AND superseded_by_job_id IS NULL "
                "ORDER BY created_at, job_id"
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def reconcile_completed_size(
        self, job_id: str, output_path: str, size: int
    ) -> bool:
        clean_path = validate_relative_output_path(output_path)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise WebDownloadError("archived web download size is invalid")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE web_download_jobs SET downloaded_bytes = ?, total_bytes = ? "
                "WHERE job_id = ? AND status = 'completed' AND output_path = ? "
                "AND (downloaded_bytes != ? OR total_bytes IS NULL OR total_bytes != ?)",
                (size, size, str(job_id), clean_path, size, size),
            ).rowcount
        return changed == 1

    def compare_and_swap_completed_output_path(
        self,
        job_id: str,
        old_path: object,
        new_path: object,
    ) -> dict[str, object]:
        clean_old_path = validate_relative_output_path(old_path)
        clean_new_path = validate_relative_output_path(new_path)
        clean_job_id = str(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, output_path FROM web_download_jobs "
                    "WHERE job_id = ?",
                    (clean_job_id,),
                ).fetchone()
                if row is None:
                    raise WebDownloadNotFoundError("web download was not found")
                if str(row["status"]) != "completed" or row["output_path"] is None:
                    raise WebDownloadConflictError(
                        "web download archive path can no longer be changed"
                    )

                current_path = str(row["output_path"])
                if current_path not in {clean_old_path, clean_new_path}:
                    raise WebDownloadConflictError(
                        "web download archive path changed unexpectedly"
                    )

                if clean_old_path != clean_new_path:
                    output_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM web_download_jobs "
                            "WHERE status = 'completed' AND output_path = ?",
                            (clean_old_path,),
                        ).fetchone()[0]
                    )
                    changed_outputs = connection.execute(
                        "UPDATE web_download_jobs SET output_path = ? "
                        "WHERE status = 'completed' AND output_path = ?",
                        (clean_new_path, clean_old_path),
                    ).rowcount
                    if changed_outputs != output_count:
                        raise WebDownloadConflictError(
                            "web download archive references changed unexpectedly"
                        )

                    incumbent_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM web_download_jobs "
                            "WHERE incumbent_output_path = ?",
                            (clean_old_path,),
                        ).fetchone()[0]
                    )
                    changed_incumbents = connection.execute(
                        "UPDATE web_download_jobs SET incumbent_output_path = ? "
                        "WHERE incumbent_output_path = ?",
                        (clean_new_path, clean_old_path),
                    ).rowcount
                    if changed_incumbents != incumbent_count:
                        raise WebDownloadConflictError(
                            "web download archive references changed unexpectedly"
                        )

                migrated = connection.execute(
                    "SELECT * FROM web_download_jobs WHERE job_id = ?",
                    (clean_job_id,),
                ).fetchone()
                stale_reference = connection.execute(
                    "SELECT 1 FROM web_download_jobs "
                    "WHERE output_path = ? OR incumbent_output_path = ? LIMIT 1",
                    (clean_old_path, clean_old_path),
                ).fetchone()
                if (
                    migrated is None
                    or str(migrated["status"]) != "completed"
                    or str(migrated["output_path"]) != clean_new_path
                    or (
                        clean_old_path != clean_new_path and stale_reference is not None
                    )
                ):
                    raise WebDownloadConflictError(
                        "web download archive path migration was not consistent"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return _row_to_job(migrated)

    def cancel(self, job_id: str) -> dict[str, object]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM web_download_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            status = str(row["status"])
            if status in {QUEUED_STATUS, RETRY_WAIT_STATUS}:
                new_status = "cancelled"
            elif status in WORKER_STATUSES:
                new_status = "cancelling"
            elif status == "paused":
                new_status = "cancelled"
            elif status == "pausing":
                new_status = "cancelling"
            elif status == "cancelling":
                new_status = status
            else:
                connection.rollback()
                raise WebDownloadConflictError(
                    "web download can no longer be cancelled"
                )
            connection.execute(
                "UPDATE web_download_jobs SET status = ?, updated_at = ?, speed = 0, eta = NULL "
                ", pause_origin = NULL, next_retry_at = NULL, "
                "retry_progress_bytes = NULL, retry_progress_fragments = NULL "
                "WHERE job_id = ?",
                (new_status, now, str(job_id)),
            )
            connection.commit()
        return self.get(job_id)

    def retry(
        self, job_id: str, *, reset_checkpoint: bool = False
    ) -> dict[str, object]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT provider, code_key, variant, status "
                "FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            if str(row["status"]) not in {"failed", "cancelled"}:
                connection.rollback()
                raise WebDownloadConflictError("web download is not retryable")
            conflict = connection.execute(
                f"SELECT job_id FROM web_download_jobs WHERE provider = ? AND code_key = ? "
                f"AND variant = ? "
                f"AND status IN ({sql_slots(ACTIVE_STATUSES)}) AND job_id != ? LIMIT 1",
                (
                    row["provider"],
                    row["code_key"],
                    row["variant"],
                    *ACTIVE_STATUSES,
                    str(job_id),
                ),
            ).fetchone()
            if conflict is not None:
                connection.rollback()
                raise WebDownloadConflictError(
                    "another active download exists for this code"
                )
            connection.execute(
                """
                UPDATE web_download_jobs
                SET status = 'queued', progress = 0, downloaded_bytes = 0,
                    total_bytes = NULL, speed = 0, eta = NULL, updated_at = ?,
                    error = NULL, failure_stage = NULL, failure_code = NULL,
                    resolved_provider = CASE
                        WHEN ? OR (
                            checkpoint_bytes = 0 AND checkpoint_fragments = 0
                        ) THEN NULL
                        ELSE resolved_provider
                    END,
                    output_path = NULL, pause_origin = NULL,
                    verified_height = NULL, publication_outcome = NULL,
                    retry_count = 0, next_retry_at = NULL,
                    retry_progress_bytes = NULL,
                    retry_progress_fragments = NULL,
                    checkpoint_bytes = CASE WHEN ? THEN 0 ELSE checkpoint_bytes END,
                    checkpoint_fragments = CASE
                        WHEN ? THEN 0 ELSE checkpoint_fragments
                    END,
                    selected_height = CASE
                        WHEN quality_strategy = 'highest' THEN selected_height
                        ELSE NULL
                    END,
                    queue_position = (
                        SELECT COALESCE(MAX(queue_position), 0) + 1
                        FROM web_download_jobs
                        WHERE status IN ('queued', 'retry_wait')
                    )
                WHERE job_id = ?
                """,
                (
                    now,
                    int(reset_checkpoint),
                    int(reset_checkpoint),
                    int(reset_checkpoint),
                    str(job_id),
                ),
            )
            connection.commit()
        return self.get(job_id)

    def requeue_interrupted(self, job_id: str) -> dict[str, object]:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE web_download_jobs
                SET status = 'queued', progress = 0, downloaded_bytes = 0,
                    total_bytes = NULL, speed = 0, eta = NULL, updated_at = ?,
                    error = NULL, failure_stage = NULL, failure_code = NULL,
                    output_path = NULL, pause_origin = NULL,
                    verified_height = NULL, publication_outcome = NULL,
                    retry_progress_bytes = NULL,
                    retry_progress_fragments = NULL,
                    selected_height = CASE
                        WHEN quality_strategy = 'highest' THEN selected_height
                        ELSE NULL
                    END,
                    queue_position = (
                        SELECT COALESCE(MAX(queue_position), 0) + 1
                        FROM web_download_jobs
                        WHERE status IN ('queued', 'retry_wait')
                    )
                WHERE job_id = ? AND status IN ({sql_slots(WORKER_STATUSES)})
                """,
                (now, str(job_id), *WORKER_STATUSES),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise WebDownloadConflictError("web download can no longer be resumed")
            connection.commit()
        return self.get(job_id)

    def finish_pausing(self, job_id: str) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT pause_origin FROM web_download_jobs "
                "WHERE job_id = ? AND status = 'pausing'",
                (str(job_id),),
            ).fetchone()
            control = connection.execute(
                "SELECT global_paused FROM web_download_control WHERE id = 1"
            ).fetchone()
            if row is None or control is None:
                connection.rollback()
                raise WebDownloadConflictError("web download is no longer pausing")
            resume_global = str(row["pause_origin"] or "") == "global" and not bool(
                control["global_paused"]
            )
            if resume_global:
                changed = connection.execute(
                    "UPDATE web_download_jobs SET status = 'queued', speed = 0, "
                    "eta = NULL, error = NULL, pause_origin = NULL, updated_at = ?, "
                    "retry_progress_bytes = NULL, "
                    "retry_progress_fragments = NULL, "
                    "queue_position = (SELECT COALESCE(MAX(queue_position), 0) + 1 "
                    "FROM web_download_jobs "
                    "WHERE status IN ('queued', 'retry_wait')) "
                    "WHERE job_id = ? AND status = 'pausing'",
                    (self._clock(), str(job_id)),
                ).rowcount
            else:
                changed = connection.execute(
                    "UPDATE web_download_jobs SET status = 'paused', speed = 0, "
                    "eta = NULL, error = NULL, updated_at = ?, "
                    "retry_progress_bytes = NULL, "
                    "retry_progress_fragments = NULL "
                    "WHERE job_id = ? AND status = 'pausing'",
                    (self._clock(), str(job_id)),
                ).rowcount
            connection.commit()
        if changed != 1:
            raise WebDownloadConflictError("web download is no longer pausing")
        return self.get(job_id)

    def remove(self, job_id: str) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM web_download_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            if str(row["status"]) not in TERMINAL_STATUSES:
                connection.rollback()
                raise WebDownloadConflictError("active web downloads cannot be removed")
            connection.execute(
                "DELETE FROM web_download_jobs WHERE job_id = ?", (str(job_id),)
            )
            connection.commit()
        return {"job_id": str(job_id), "removed": True}

    def remove_failed_if_unchanged(
        self,
        job_id: str,
        *,
        expected_updated_at: object,
    ) -> dict[str, object]:
        expected = replacement_revision_timestamp(
            expected_updated_at,
            "web download replacement revision",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadNotFoundError("web download was not found")
            if str(row["status"]) not in {"failed", "cancelled"} or float(row["updated_at"]) != expected:
                connection.rollback()
                raise WebDownloadConflictError(
                    "failed web download changed before replacement cleanup"
                )
            removed = _row_to_job(row)
            changed = connection.execute(
                "DELETE FROM web_download_jobs WHERE job_id = ? "
                "AND status IN ('failed', 'cancelled') AND updated_at = ?",
                (str(job_id), expected),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise WebDownloadConflictError(
                    "failed web download changed before replacement cleanup"
                )
            connection.commit()
        return {**removed, "removed": True}

    def remove_completed_archives_if_missing(
        self,
        candidates: Sequence[dict[str, str]],
        archive_status: Callable[[str], str],
        archive_root_is_stable: Callable[[], bool],
    ) -> list[str]:
        expected_paths: dict[str, str] = {}
        for candidate in candidates:
            job_id = str(candidate.get("job_id") or "")
            output_path = str(candidate.get("output_path") or "")
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            try:
                expected_paths[job_id] = validate_relative_output_path(output_path)
            except WebDownloadRunnerError:
                continue

        removed: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for job_id, expected_path in expected_paths.items():
                    row = connection.execute(
                        "SELECT status, output_path FROM web_download_jobs "
                        "WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if (
                        row is None
                        or str(row["status"]) != "completed"
                        or row["output_path"] is None
                        or str(row["output_path"]) != expected_path
                    ):
                        continue
                    current_archive_status = archive_status(expected_path)
                    if current_archive_status == ARCHIVE_UNKNOWN:
                        raise WebDownloadArchiveUnavailableError(
                            "archive storage could not be verified"
                        )
                    if current_archive_status != ARCHIVE_MISSING:
                        continue
                    changed = connection.execute(
                        "DELETE FROM web_download_jobs WHERE job_id = ? "
                        "AND status = 'completed' AND output_path = ?",
                        (job_id, expected_path),
                    ).rowcount
                    if changed == 1:
                        removed.append(job_id)
                if not archive_root_is_stable():
                    raise WebDownloadArchiveUnavailableError(
                        "archive storage changed during cleanup"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return removed

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                migrate_sqlite(
                    connection,
                    component=WEB_DOWNLOAD_SCHEMA_COMPONENT,
                    current_version=WEB_DOWNLOAD_SCHEMA_VERSION,
                    migrations=(
                        SQLiteMigration(1, migrate_web_download_v1),
                        SQLiteMigration(2, migrate_web_download_v2),
                        SQLiteMigration(3, migrate_web_download_v3),
                        SQLiteMigration(4, migrate_web_download_v4),
                        SQLiteMigration(5, migrate_web_download_v5),
                        SQLiteMigration(6, migrate_web_download_v6),
                        SQLiteMigration(7, migrate_web_download_v7),
                        SQLiteMigration(8, migrate_web_download_v8),
                        SQLiteMigration(9, migrate_web_download_v9),
                    ),
                    clock=self._clock,
                    verify_current=verify_web_download_schema,
                )
                initialize_notification_schema(connection, clock=self._clock)
            except SchemaTooNewError as exc:
                raise WebDownloadConfigError(
                    "web download database schema is newer than this application supports"
                ) from exc
            except MigrationError as exc:
                raise WebDownloadConfigError(str(exc)) from exc

    @contextmanager
    def _connect(
        self, *, timeout_seconds: float = 30.0
    ) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {max(1, int(timeout_seconds * 1000))}"
            )
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()


def _disk_low_volume_keys(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise WebDownloadError("disk notification volumes are invalid")
    clean = tuple(sorted({str(value) for value in values}))
    if not clean or any(value not in DISK_LOW_VOLUME_KEYS for value in clean):
        raise WebDownloadError("disk notification volumes are invalid")
    return clean


def _validate_quality_strategy(value: object) -> str:
    if not isinstance(value, str) or value not in QUALITY_STRATEGIES:
        raise WebDownloadError("web download quality strategy is invalid")
    return value


def _row_to_job(row: sqlite3.Row) -> dict[str, object]:
    status = str(row["status"])
    return {
        "job_id": str(row["job_id"]),
        "provider": str(row["provider"]),
        "resolved_provider": (
            str(row["resolved_provider"])
            if "resolved_provider" in row.keys()
            and row["resolved_provider"] is not None
            else None
        ),
        "code": str(row["code"]),
        "variant": validate_web_download_variant(row["variant"]),
        "requested_height": (
            int(row["requested_height"])
            if row["requested_height"] is not None
            else None
        ),
        "selected_height": (
            int(row["selected_height"]) if row["selected_height"] is not None else None
        ),
        "verified_height": (
            int(row["verified_height"])
            if "verified_height" in row.keys() and row["verified_height"] is not None
            else None
        ),
        "quality_strategy": (
            str(row["quality_strategy"])
            if "quality_strategy" in row.keys()
            else "legacy"
        ),
        "existing_policy": (
            str(row["existing_policy"])
            if "existing_policy" in row.keys()
            else LEGACY_EXISTING_POLICY
        ),
        "incumbent_output_path": (
            str(row["incumbent_output_path"])
            if "incumbent_output_path" in row.keys()
            and row["incumbent_output_path"] is not None
            else None
        ),
        "replaces_job_id": (
            str(row["replaces_job_id"])
            if "replaces_job_id" in row.keys() and row["replaces_job_id"] is not None
            else None
        ),
        "superseded_by_job_id": (
            str(row["superseded_by_job_id"])
            if "superseded_by_job_id" in row.keys()
            and row["superseded_by_job_id"] is not None
            else None
        ),
        "publication_outcome": (
            str(row["publication_outcome"])
            if "publication_outcome" in row.keys()
            and row["publication_outcome"] is not None
            else None
        ),
        "status": status,
        "progress": float(row["progress"]),
        "downloaded_bytes": int(row["downloaded_bytes"]),
        "total_bytes": int(row["total_bytes"]) if row["total_bytes"] is not None else 0,
        "speed": float(row["speed"]),
        "eta": int(row["eta"]) if row["eta"] is not None else 0,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "error": str(row["error"]) if row["error"] is not None else None,
        "failure_stage": (
            str(row["failure_stage"])
            if "failure_stage" in row.keys() and row["failure_stage"] is not None
            else None
        ),
        "failure_code": (
            str(row["failure_code"])
            if "failure_code" in row.keys() and row["failure_code"] is not None
            else None
        ),
        "output_path": (
            str(row["output_path"]) if row["output_path"] is not None else None
        ),
        "priority": int(row["priority"]) if "priority" in row.keys() else 0,
        "queue_position": (
            int(row["queue_position"]) if "queue_position" in row.keys() else 0
        ),
        "retry_count": (int(row["retry_count"]) if "retry_count" in row.keys() else 0),
        "next_retry_at": (
            float(row["next_retry_at"])
            if "next_retry_at" in row.keys() and row["next_retry_at"] is not None
            else None
        ),
        "can_pause": status
        in {QUEUED_STATUS, RETRY_WAIT_STATUS, *PAUSABLE_WORKER_STATUSES},
        "can_resume": status == "paused",
        "can_cancel": status
        in {
            QUEUED_STATUS,
            RETRY_WAIT_STATUS,
            *WORKER_STATUSES,
            *PAUSE_STATUSES,
        },
        "can_retry": status in {"failed", "cancelled"},
        "can_remove": status in TERMINAL_STATUSES,
    }


def _row_to_claimed_job(row: sqlite3.Row) -> dict[str, object]:
    job = _row_to_job(row)
    job["checkpoint_bytes"] = int(row["checkpoint_bytes"])
    job["checkpoint_fragments"] = int(row["checkpoint_fragments"])
    return job


def _control_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    try:
        schedule = json.loads(str(row["schedule_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WebDownloadError("web download schedule is corrupted") from exc
    if not isinstance(schedule, list):
        raise WebDownloadError("web download schedule is corrupted")
    return {
        "queue_revision": int(row["queue_revision"]),
        "global_paused": bool(row["global_paused"]),
        "target_concurrency": int(row["target_concurrency"]),
        "bandwidth_limit": int(row["bandwidth_limit"]),
        "timezone": str(row["timezone"]),
        "schedule": schedule,
        "updated_at": float(row["updated_at"]),
    }


def _normalize_queue_priority(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -MAX_QUEUE_PRIORITY <= value <= MAX_QUEUE_PRIORITY
    ):
        raise WebDownloadError(
            f"web download priority must be between {-MAX_QUEUE_PRIORITY} and "
            f"{MAX_QUEUE_PRIORITY}"
        )
    return value


def _normalize_queue_revision(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WebDownloadError("web download queue revision is invalid")
    return value


def _long_retry_delay_seconds(job_id: str, retry_count: int) -> float:
    if not JOB_ID_RE.fullmatch(job_id) or not 1 <= retry_count <= len(
        LONG_RETRY_DELAYS_SECONDS
    ):
        raise WebDownloadError("web download retry schedule is invalid")
    digest = hashlib.sha256(f"{job_id}:{retry_count}".encode("ascii")).digest()
    sample = int.from_bytes(digest[:8], "big") / 0xFFFFFFFFFFFFFFFF
    factor = 1.0 - LONG_RETRY_JITTER_RATIO + (2.0 * LONG_RETRY_JITTER_RATIO * sample)
    return LONG_RETRY_DELAYS_SECONDS[retry_count - 1] * factor
