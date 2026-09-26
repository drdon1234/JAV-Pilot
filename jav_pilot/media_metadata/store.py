from __future__ import annotations

import json
import math
import posixpath
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..core.catalog_code import normalize_catalog_code
from ..core.migrations import (
    MigrationError,
    SQLiteMigration,
    SchemaTooNewError,
    add_column_if_missing,
    migrate_sqlite,
    require_columns,
)
from ..notifications.events import completed_event, failed_event
from ..notifications.outbox import (
    enqueue_notification_event,
    initialize_notification_schema,
)
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    normalize_web_download_variant,
)


KINDS = ("qb", "web", "manual")
STATUSES = (
    "waiting_media",
    "queued",
    "running",
    "retry",
    "completed",
    "failed",
)
READY_STATUSES = ("waiting_media", "queued", "retry")
SCHEMA_COMPONENT = "media_metadata"
CURRENT_SCHEMA_VERSION = 3
_ASSET_SUFFIXES = frozenset({".jpg", ".nfo"})

_DOWNLOAD_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,79}$")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:authorization|cookie|header|manifest|password|referer|secret|session|token|url)",
    flags=re.IGNORECASE,
)
_SENSITIVE_MARKER_RE = re.compile(
    r"(?:"
    r"(?:https?|wss?|ftp)://|://|www\."
    r"|\b(?:authorization|cookie|headers?|manifest(?:_url)?|password|referer|secret|session|token|url)\b"
    r")",
    flags=re.IGNORECASE,
)


class MediaMetadataStoreError(RuntimeError):
    pass


class MediaMetadataNotFoundError(MediaMetadataStoreError):
    pass


class MediaMetadataConflictError(MediaMetadataStoreError):
    pass


class MediaMetadataStore:
    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise MediaMetadataStoreError("metadata database path must be absolute")
        self._clock = clock
        self._id_factory = id_factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(
        self,
        kind: object,
        download_key: object,
        code: object,
        relative_media_path: object | None = None,
        *,
        variant: object | None = None,
    ) -> dict[str, object]:
        clean_kind = _validate_kind(kind)
        clean_key = _validate_download_key(download_key, kind=clean_kind)
        display_code, code_key = _normalize_code(code)
        clean_path = _optional_relative_path(relative_media_path)
        clean_variant = _enqueue_variant(clean_kind, variant)
        now = self._now()
        status = "queued" if clean_path is not None else "waiting_media"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE kind = ? AND download_key = ?",
                (clean_kind, clean_key),
            ).fetchone()
            if row is not None:
                if str(row["code_key"]) != code_key:
                    connection.rollback()
                    raise MediaMetadataConflictError(
                        "download already has a different catalog code"
                    )
                current_variant = _stored_variant(str(row["kind"]), row["variant"])
                if (
                    current_variant is not None
                    and clean_variant is not None
                    and current_variant != clean_variant
                ):
                    connection.rollback()
                    raise MediaMetadataConflictError(
                        "download already has a different media variant"
                    )
                if current_variant is None and clean_variant is not None:
                    connection.execute(
                        "UPDATE jobs SET variant = ?, updated_at = ? WHERE job_id = ?",
                        (clean_variant, now, str(row["job_id"])),
                    )
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (str(row["job_id"]),)
                    ).fetchone()
                current_path = (
                    str(row["relative_media_path"])
                    if row["relative_media_path"] is not None
                    else None
                )
                if clean_path is not None and current_path not in {None, clean_path}:
                    connection.rollback()
                    raise MediaMetadataConflictError(
                        "download already has a different media path"
                    )
                if clean_path is not None and current_path is None:
                    next_status = (
                        "queued"
                        if str(row["status"]) == "waiting_media"
                        else str(row["status"])
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET relative_media_path = ?, status = ?, next_attempt_at = ?,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (clean_path, next_status, now, now, str(row["job_id"])),
                    )
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (str(row["job_id"]),)
                    ).fetchone()
                connection.commit()
                if row is None:
                    raise MediaMetadataStoreError("metadata job could not be read")
                return _row_to_job(row)

            job_id = _validate_job_id(self._id_factory())
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, kind, download_key, code, code_key, variant, status,
                        relative_media_path, attempts, next_attempt_at, error,
                        assets_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, '{}', ?, ?)
                    """,
                    (
                        job_id,
                        clean_kind,
                        clean_key,
                        display_code,
                        code_key,
                        clean_variant,
                        status,
                        clean_path,
                        now,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise MediaMetadataConflictError(
                    "metadata job identity already exists"
                ) from exc
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise MediaMetadataStoreError("metadata job could not be persisted")
        return _row_to_job(row)

    def list(
        self,
        limit: int = 100,
        *,
        offset: int = 0,
        status_filter: str = "all",
        query: object | None = None,
    ) -> list[dict[str, object]]:
        clean_limit = _validate_limit(limit)
        clean_offset = _validate_offset(offset)
        status = _validate_status_filter(status_filter)
        query_key = _normalize_history_query(query)
        clauses: list[str] = []
        values: list[object] = []
        if status != "all":
            clauses.append("status = ?")
            values.append(status)
        if query_key is not None:
            clauses.append("code_key LIKE ?")
            values.append(f"%{query_key}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((clean_limit, clean_offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs{where} "
                "ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count(
        self,
        *,
        status_filter: str = "all",
        query: object | None = None,
    ) -> int:
        status = _validate_status_filter(status_filter)
        query_key = _normalize_history_query(query)
        clauses: list[str] = []
        values: list[object] = []
        if status != "all":
            clauses.append("status = ?")
            values.append(status)
        if query_key is not None:
            clauses.append("code_key LIKE ?")
            values.append(f"%{query_key}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM jobs{where}", values
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('waiting_media', 'queued', 'retry') THEN 1 ELSE 0 END) AS waiting,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM jobs
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "waiting": int(row["waiting"] or 0),
            "running": int(row["running"] or 0),
            "completed": int(row["completed"] or 0),
            "failed": int(row["failed"] or 0),
        }

    def get(self, job_id: object) -> dict[str, object]:
        clean_job_id = _validate_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (clean_job_id,)
            ).fetchone()
        if row is None:
            raise MediaMetadataNotFoundError("metadata job was not found")
        return _row_to_job(row)

    def get_by_download(
        self,
        kind: object,
        download_key: object,
    ) -> dict[str, object] | None:
        clean_kind = _validate_kind(kind)
        clean_key = _validate_download_key(download_key, kind=clean_kind)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE kind = ? AND download_key = ?",
                (clean_kind, clean_key),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def delete_incomplete_qb(
        self,
        download_keys: Iterable[object],
        *,
        include_bound: bool = False,
    ) -> list[dict[str, object]]:
        clean_keys = tuple(
            dict.fromkeys(
                _validate_download_key(value, kind="qb") for value in download_keys
            )
        )
        if not clean_keys:
            return []
        path_clause = "" if include_bound else "AND relative_media_path IS NULL"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE kind = 'qb'
                  AND download_key IN ({_sql_slots(clean_keys)})
                  AND status != 'completed'
                  {path_clause}
                ORDER BY created_at, job_id
                """,
                clean_keys,
            ).fetchall()
            if rows:
                changed = connection.execute(
                    f"""
                    DELETE FROM jobs
                    WHERE kind = 'qb'
                      AND download_key IN ({_sql_slots(clean_keys)})
                      AND status != 'completed'
                      {path_clause}
                    """,
                    clean_keys,
                ).rowcount
                if changed != len(rows):
                    connection.rollback()
                    raise MediaMetadataStoreError(
                        "qBittorrent metadata jobs could not be deleted consistently"
                    )
            connection.commit()
        return [_row_to_job(row) for row in rows]

    def bind_media_path(
        self,
        job_id: object,
        relative_media_path: object,
    ) -> dict[str, object]:
        return self._transition(
            job_id,
            allowed=("running",),
            status="running",
            relative_media_path=_validate_relative_path(relative_media_path),
        )

    def relocate_media_path(
        self,
        job_id: object,
        old_relative_media_path: object,
        new_relative_media_path: object,
    ) -> dict[str, object]:
        """Compare-and-swap a bound path after a verified archive rename."""

        clean_job_id = _validate_job_id(job_id)
        old_path = _validate_relative_path(old_relative_media_path)
        new_path = _validate_relative_path(new_relative_media_path)
        if old_path == new_path:
            return self.get(clean_job_id)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (clean_job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MediaMetadataNotFoundError("metadata job was not found")
            current_path = (
                str(row["relative_media_path"])
                if row["relative_media_path"] is not None
                else None
            )
            if current_path == new_path:
                connection.commit()
                return _row_to_job(row)
            if current_path != old_path or str(row["status"]) != "running":
                connection.rollback()
                raise MediaMetadataConflictError(
                    "metadata media path changed before archive relocation"
                )
            changed = connection.execute(
                "UPDATE jobs SET relative_media_path = ?, updated_at = ? "
                "WHERE job_id = ? AND status = 'running' "
                "AND relative_media_path = ?",
                (new_path, now, clean_job_id, old_path),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise MediaMetadataConflictError(
                    "metadata archive relocation was not persisted"
                )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (clean_job_id,),
            ).fetchone()
            connection.commit()
        if updated is None:
            raise MediaMetadataStoreError("metadata job could not be read")
        return _row_to_job(updated)

    def refresh_qb_registration(self, job_id: object) -> dict[str, object]:
        clean_job_id = _validate_job_id(job_id)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (clean_job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MediaMetadataNotFoundError("metadata job was not found")
            if str(row["kind"]) != "qb":
                connection.rollback()
                raise MediaMetadataConflictError(
                    "metadata job is not a qBittorrent job"
                )
            if (
                row["relative_media_path"] is not None
                or str(row["status"]) == "completed"
            ):
                connection.commit()
                return _row_to_job(row)
            status = "running" if str(row["status"]) == "running" else "waiting_media"
            changed = connection.execute(
                """
                UPDATE jobs
                SET status = ?, attempts = 0, next_attempt_at = ?, error = NULL,
                    created_at = ?, updated_at = ?
                WHERE job_id = ? AND kind = 'qb'
                  AND relative_media_path IS NULL AND status != 'completed'
                """,
                (status, now, now, now, clean_job_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise MediaMetadataConflictError(
                    "qBittorrent metadata registration changed concurrently"
                )
            refreshed = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (clean_job_id,),
            ).fetchone()
            connection.commit()
        if refreshed is None:
            raise MediaMetadataStoreError("metadata job could not be refreshed")
        return _row_to_job(refreshed)

    def nfo_provenance(
        self,
        code: object,
        relative_media_path: object,
        nfo_name: object,
    ) -> str | None:
        _, code_key = _normalize_code(code)
        clean_path = _validate_relative_path(relative_media_path)
        clean_name = _validate_nfo_name(nfo_name)
        return self.nfo_provenance_map().get((code_key, clean_path, clean_name))

    def nfo_provenance_map(self) -> dict[tuple[str, str, str], str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT code_key, relative_media_path, assets_json
                FROM jobs
                WHERE relative_media_path IS NOT NULL
                ORDER BY updated_at DESC, job_id DESC
                """
            ).fetchall()
        result: dict[tuple[str, str, str], str] = {}
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            try:
                assets = json.loads(str(row["assets_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            if not isinstance(assets, dict):
                continue
            code_key = str(row["code_key"] or "")
            relative = str(row["relative_media_path"] or "")
            for name, asset in assets.items():
                if not isinstance(name, str) or not name.lower().endswith(".nfo"):
                    continue
                status_value = asset.get("status") if isinstance(asset, dict) else None
                key = (code_key, relative, name)
                if key in seen:
                    continue
                seen.add(key)
                if status_value == "generated":
                    result[key] = "generated"
                elif status_value == "existing":
                    result[key] = "tracked_existing"
        return result

    def has_generated_nfo(
        self,
        code: object,
        relative_media_path: object,
        nfo_name: object,
    ) -> bool:
        return self.nfo_provenance(code, relative_media_path, nfo_name) == "generated"

    def claim_ready(self, now: float | None = None) -> dict[str, object] | None:
        claim_time = self._now() if now is None else _validate_timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT job_id FROM jobs
                WHERE status IN ({_sql_slots(READY_STATUSES)})
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at, created_at, job_id
                LIMIT 1
                """,
                (*READY_STATUSES, claim_time),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job_id = str(row["job_id"])
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1, updated_at = ?,
                    error = NULL
                WHERE job_id = ? AND status IN ({_sql_slots(READY_STATUSES)})
                  AND next_attempt_at <= ?
                """,
                (claim_time, job_id, *READY_STATUSES, claim_time),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        if claimed is None:
            raise MediaMetadataStoreError("claimed metadata job could not be read")
        return _row_to_job(claimed)

    def set_waiting(
        self,
        job_id: object,
        *,
        relative_media_path: object | None = None,
        next_attempt_at: float | None = None,
    ) -> dict[str, object]:
        return self._transition(
            job_id,
            allowed=("running", "waiting_media"),
            status="waiting_media",
            relative_media_path=relative_media_path,
            next_attempt_at=next_attempt_at,
            error=None,
            decrement_attempts=True,
        )

    def set_retry(
        self,
        job_id: object,
        error: object,
        *,
        next_attempt_at: float | None = None,
        assets: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        clean_assets = _validate_assets(assets)
        encoded_assets = (
            Ellipsis
            if assets is None
            else json.dumps(
                clean_assets,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return self._transition(
            job_id,
            allowed=("running", "retry"),
            status="retry",
            next_attempt_at=next_attempt_at,
            error=_validate_error(error),
            assets_json=encoded_assets,
        )

    def set_completed(
        self,
        job_id: object,
        *,
        relative_media_path: object | None = None,
        assets: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        clean_assets = _validate_assets(assets)
        encoded_assets = (
            Ellipsis
            if assets is None
            else json.dumps(
                clean_assets,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return self._transition(
            job_id,
            allowed=("running", "completed"),
            status="completed",
            relative_media_path=relative_media_path,
            next_attempt_at=0.0,
            error=None,
            assets_json=encoded_assets,
        )

    def set_failed(
        self,
        job_id: object,
        error: object,
        *,
        assets: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        clean_assets = _validate_assets(assets)
        encoded_assets = (
            Ellipsis
            if assets is None
            else json.dumps(
                clean_assets,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return self._transition(
            job_id,
            allowed=("running", "failed"),
            status="failed",
            next_attempt_at=0.0,
            error=_validate_error(error),
            assets_json=encoded_assets,
        )

    def retry(self, job_id: object) -> dict[str, object]:
        return self._transition(
            job_id,
            allowed=("completed", "failed", "retry"),
            status="queued",
            next_attempt_at=self._now(),
            error=None,
            reset_attempts=True,
        )

    def recover_running(self) -> list[dict[str, object]]:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE status = 'running' ORDER BY created_at, job_id"
            ).fetchall()
            job_ids = tuple(str(row["job_id"]) for row in rows)
            if job_ids:
                changed = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'retry', next_attempt_at = ?, error = NULL,
                        updated_at = ?
                    WHERE status = 'running'
                    """,
                    (now, now),
                ).rowcount
                if changed != len(job_ids):
                    connection.rollback()
                    raise MediaMetadataStoreError(
                        "running metadata jobs could not be recovered consistently"
                    )
                recovered = connection.execute(
                    f"SELECT * FROM jobs WHERE job_id IN ({_sql_slots(job_ids)}) "
                    "ORDER BY created_at, job_id",
                    job_ids,
                ).fetchall()
            else:
                recovered = []
            connection.commit()
        return [_row_to_job(row) for row in recovered]

    def _transition(
        self,
        job_id: object,
        *,
        allowed: tuple[str, ...],
        status: str,
        relative_media_path: object | None | type(Ellipsis) = Ellipsis,
        next_attempt_at: float | None | type(Ellipsis) = Ellipsis,
        error: str | None | type(Ellipsis) = Ellipsis,
        assets_json: str | type(Ellipsis) = Ellipsis,
        decrement_attempts: bool = False,
        reset_attempts: bool = False,
    ) -> dict[str, object]:
        clean_job_id = _validate_job_id(job_id)
        now = self._now()
        fields: dict[str, object] = {"status": status, "updated_at": now}
        if relative_media_path is not Ellipsis:
            fields["relative_media_path"] = _optional_relative_path(relative_media_path)
        if next_attempt_at is not Ellipsis:
            fields["next_attempt_at"] = (
                now if next_attempt_at is None else _validate_timestamp(next_attempt_at)
            )
        if error is not Ellipsis:
            fields["error"] = error
        if assets_json is not Ellipsis:
            fields["assets_json"] = assets_json

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, code, relative_media_path, assets_json "
                "FROM jobs WHERE job_id = ?",
                (clean_job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MediaMetadataNotFoundError("metadata job was not found")
            if str(row["status"]) not in allowed:
                connection.rollback()
                raise MediaMetadataConflictError(
                    "metadata job state no longer permits this operation"
                )
            if str(row["status"]) == status:
                requested_path = fields.get("relative_media_path")
                current_path = (
                    str(row["relative_media_path"])
                    if row["relative_media_path"] is not None
                    else None
                )
                if requested_path is not None and current_path not in {
                    None,
                    requested_path,
                }:
                    connection.rollback()
                    raise MediaMetadataConflictError(
                        "completed metadata job already has a different media path"
                    )
                requested_assets = fields.get("assets_json")
                if (
                    requested_assets is not None
                    and str(row["assets_json"]) != requested_assets
                ):
                    connection.rollback()
                    raise MediaMetadataConflictError(
                        "completed metadata job already has different assets"
                    )
            if (
                "relative_media_path" in fields
                and fields["relative_media_path"] is None
                and row["relative_media_path"] is not None
            ):
                fields.pop("relative_media_path")
            assignments = [f"{name} = ?" for name in fields]
            if reset_attempts:
                assignments.append("attempts = 0")
            elif decrement_attempts:
                assignments.append("attempts = MAX(attempts - 1, 0)")
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
                (*fields.values(), clean_job_id),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (clean_job_id,)
            ).fetchone()
            if status in {"completed", "failed"} and str(row["status"]) != status:
                event = (
                    completed_event(
                        source="metadata",
                        entity_id=clean_job_id,
                        code=str(row["code"]),
                    )
                    if status == "completed"
                    else failed_event(
                        source="metadata",
                        entity_id=clean_job_id,
                        code=str(row["code"]),
                        error_code="metadata_failed",
                    )
                )
                enqueue_notification_event(connection, event, clock=self._clock)
            connection.commit()
        if updated is None:
            raise MediaMetadataStoreError("metadata job could not be read")
        return _row_to_job(updated)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                migrate_sqlite(
                    connection,
                    component=SCHEMA_COMPONENT,
                    current_version=CURRENT_SCHEMA_VERSION,
                    migrations=(
                        SQLiteMigration(1, _migrate_metadata_v1),
                        SQLiteMigration(2, _migrate_metadata_v2),
                        SQLiteMigration(3, _migrate_metadata_v3),
                    ),
                    clock=self._clock,
                    verify_current=_verify_metadata_schema,
                )
                initialize_notification_schema(connection, clock=self._clock)
            except SchemaTooNewError as exc:
                raise MediaMetadataStoreError(
                    "metadata database schema is newer than this application supports"
                ) from exc
            except MigrationError as exc:
                raise MediaMetadataStoreError(str(exc)) from exc

    def _now(self) -> float:
        return _validate_timestamp(self._clock())

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


def _migrate_metadata_v1(connection: sqlite3.Connection) -> None:
    kinds = ", ".join(f"'{value}'" for value in KINDS)
    statuses = ", ".join(f"'{value}'" for value in STATUSES)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ({kinds})),
            download_key TEXT NOT NULL,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            relative_media_path TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at REAL NOT NULL CHECK (next_attempt_at >= 0),
            error TEXT,
            assets_json TEXT NOT NULL DEFAULT '{{}}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (kind, download_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS jobs_ready "
        "ON jobs(status, next_attempt_at, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS jobs_created_at "
        "ON jobs(created_at DESC, job_id DESC)"
    )


def _migrate_metadata_v2(connection: sqlite3.Connection) -> None:
    variants = ", ".join(f"'{value}'" for value in WEB_DOWNLOAD_VARIANTS)
    add_column_if_missing(
        connection,
        "jobs",
        "variant",
        f"TEXT CHECK (variant IS NULL OR variant IN ({variants}))",
    )
    connection.execute(
        "UPDATE jobs SET variant = ? WHERE kind = 'web' AND variant IS NULL",
        (DEFAULT_WEB_DOWNLOAD_VARIANT,),
    )


def _migrate_metadata_v3(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    updates: list[tuple[str, str, str]] = []
    for row in connection.execute(
        "SELECT job_id, code, code_key FROM jobs"
    ).fetchall():
        try:
            display_code, code_key = _normalize_code(row["code"])
        except MediaMetadataStoreError as exc:
            raise MigrationError("metadata job catalog code is invalid") from exc
        if str(row["code"]) != display_code or str(row["code_key"]) != code_key:
            updates.append((display_code, code_key, str(row["job_id"])))
    connection.executemany(
        "UPDATE jobs SET code = ?, code_key = ? WHERE job_id = ?",
        updates,
    )


def _verify_metadata_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "jobs",
        (
            "job_id",
            "kind",
            "download_key",
            "code",
            "code_key",
            "variant",
            "status",
            "relative_media_path",
            "attempts",
            "next_attempt_at",
            "error",
            "assets_json",
            "created_at",
            "updated_at",
        ),
    )
    invalid = connection.execute(
        "SELECT 1 FROM jobs WHERE "
        "(kind = 'qb' AND variant IS NOT NULL) OR "
        "(kind = 'web' AND variant IS NULL) OR "
        "(variant IS NOT NULL AND variant NOT IN (?, ?, ?)) LIMIT 1",
        WEB_DOWNLOAD_VARIANTS,
    ).fetchone()
    if invalid is not None:
        raise MigrationError("metadata job variant is invalid")
    for row in connection.execute("SELECT code, code_key FROM jobs").fetchall():
        try:
            expected_code, expected_key = _normalize_code(row["code"])
        except MediaMetadataStoreError as exc:
            raise MigrationError("metadata job catalog code is invalid") from exc
        if (
            str(row["code"]) != expected_code
            or str(row["code_key"]) != expected_key
        ):
            raise MigrationError("metadata job catalog identity is invalid")


def _validate_kind(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in KINDS:
        raise MediaMetadataStoreError("metadata job kind is invalid")
    return clean


def _enqueue_variant(kind: str, value: object | None) -> str | None:
    if kind == "qb":
        if value is not None:
            raise MediaMetadataStoreError("qB metadata jobs cannot have a variant")
        return None
    if kind == "manual" and value is None:
        return None
    if kind == "web" and value is None:
        return DEFAULT_WEB_DOWNLOAD_VARIANT
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise MediaMetadataStoreError("metadata job variant is invalid") from exc


def _stored_variant(
    kind: str,
    value: object | None,
) -> str | None:
    if value is None:
        if kind == "web":
            raise MediaMetadataStoreError("metadata Web job variant is missing")
        return None
    if kind == "qb":
        raise MediaMetadataStoreError("qB metadata job has a variant")
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise MediaMetadataStoreError("metadata job variant could not be read") from exc


def _validate_download_key(value: object, *, kind: str) -> str:
    clean = str(value or "").strip()
    if not clean.isascii() or not _DOWNLOAD_KEY_RE.fullmatch(clean):
        raise MediaMetadataStoreError("metadata download key is invalid")
    if _SENSITIVE_MARKER_RE.search(clean):
        raise MediaMetadataStoreError("metadata download key contains unsafe data")
    return clean.lower() if kind in {"qb", "web"} else clean


def _validate_job_id(value: object) -> str:
    clean = str(value or "").strip()
    if not clean.isascii() or not _JOB_ID_RE.fullmatch(clean):
        raise MediaMetadataStoreError("metadata job id is invalid")
    return clean


def _normalize_code(value: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(value, max_length=40)
    if normalized is None:
        raise MediaMetadataStoreError("metadata catalog code is invalid")
    return normalized


def _optional_relative_path(value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_relative_path(value)


def _validate_relative_path(value: object) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw.encode("utf-8")) > 1024
        or raw in {".", ".."}
        or raw.startswith("/")
        or raw.startswith("//")
        or "\\" in raw
        or "://" in raw
        or any(ord(character) < 32 for character in raw)
    ):
        raise MediaMetadataStoreError("metadata media path is invalid")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
        or posixpath.normpath(raw) != raw
    ):
        raise MediaMetadataStoreError("metadata media path is invalid")
    return raw


def _validate_nfo_name(value: object) -> str:
    clean = str(value or "").strip()
    if (
        not clean
        or len(clean.encode("utf-8")) > 255
        or clean in {".", ".."}
        or "/" in clean
        or "\\" in clean
        or not clean.lower().endswith(".nfo")
        or any(ord(character) < 32 for character in clean)
    ):
        raise MediaMetadataStoreError("metadata NFO name is invalid")
    return clean


def _validate_error(value: object) -> str:
    clean = str(value or "").strip()
    if (
        not clean
        or len(clean.encode("utf-8")) > 1000
        or any(ord(character) < 32 for character in clean)
    ):
        raise MediaMetadataStoreError("metadata error is invalid")
    if _SENSITIVE_MARKER_RE.search(clean):
        raise MediaMetadataStoreError("metadata error contains unsafe data")
    return clean


def _validate_assets(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MediaMetadataStoreError("metadata assets are invalid")
    budget = [0]
    clean = _validate_asset_value(value, depth=0, budget=budget)
    if not isinstance(clean, dict):
        raise MediaMetadataStoreError("metadata assets are invalid")
    encoded = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > 32 * 1024:
        raise MediaMetadataStoreError("metadata assets are invalid")
    return clean


def _validate_asset_value(value: object, *, depth: int, budget: list[int]) -> object:
    if depth > 4:
        raise MediaMetadataStoreError("metadata assets are invalid")
    budget[0] += 1
    if budget[0] > 256:
        raise MediaMetadataStoreError("metadata assets are invalid")
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise MediaMetadataStoreError("metadata assets are invalid")
        output: dict[str, object] = {}
        for raw_name, item in value.items():
            if not isinstance(raw_name, str):
                raise MediaMetadataStoreError("metadata asset name is invalid")
            name = raw_name.strip()
            byte_limit = 255 if depth == 0 else 128
            if (
                not name
                or len(name.encode("utf-8")) > byte_limit
                or (depth == 0 and name in {".", ".."})
                or (depth == 0 and ("/" in name or "\\" in name))
                or (
                    depth == 0
                    and PurePosixPath(name).suffix.lower() not in _ASSET_SUFFIXES
                )
                or any(ord(character) < 32 for character in name)
                or (depth > 0 and _SENSITIVE_FIELD_RE.search(name))
            ):
                raise MediaMetadataStoreError("metadata asset name is invalid")
            output[name] = _validate_asset_value(item, depth=depth + 1, budget=budget)
        return output
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise MediaMetadataStoreError("metadata assets are invalid")
        return [
            _validate_asset_value(item, depth=depth + 1, budget=budget)
            for item in value
        ]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise MediaMetadataStoreError("metadata asset scalar is invalid")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MediaMetadataStoreError("metadata asset scalar is invalid")
        return value
    if isinstance(value, str):
        clean = value.strip()
        if (
            len(clean.encode("utf-8")) > 1024
            or any(ord(character) < 32 for character in clean)
            or _SENSITIVE_MARKER_RE.search(clean)
            or _SENSITIVE_FIELD_RE.search(clean)
        ):
            raise MediaMetadataStoreError("metadata asset scalar is invalid")
        if "/" in clean or "\\" in clean:
            return _validate_relative_path(clean)
        return clean
    raise MediaMetadataStoreError("metadata asset scalar is invalid")


def _validate_limit(value: object) -> int:
    if isinstance(value, bool):
        raise MediaMetadataStoreError("metadata job list limit is invalid")
    try:
        clean = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaMetadataStoreError("metadata job list limit is invalid") from exc
    if clean < 1:
        raise MediaMetadataStoreError("metadata job list limit is invalid")
    return min(clean, 500)


def _validate_offset(value: object) -> int:
    if isinstance(value, bool):
        raise MediaMetadataStoreError("metadata job list offset is invalid")
    try:
        clean = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaMetadataStoreError("metadata job list offset is invalid") from exc
    if clean < 0 or clean > 10_000_000:
        raise MediaMetadataStoreError("metadata job list offset is invalid")
    return clean


def _validate_status_filter(value: object) -> str:
    clean = str(value or "all").strip().lower()
    if clean != "all" and clean not in STATUSES:
        raise MediaMetadataStoreError("metadata job status filter is invalid")
    return clean


def _normalize_history_query(value: object | None) -> str | None:
    if value is None:
        return None
    clean = unicodedata.normalize("NFKC", str(value)).strip().upper()
    if not clean:
        return None
    if (
        len(clean) > 40
        or not clean.isascii()
        or re.fullmatch(r"[A-Z0-9._ -]+", clean) is None
    ):
        raise MediaMetadataStoreError("metadata job history query is invalid")
    query_key = re.sub(r"[^A-Z0-9]", "", clean)
    if not query_key:
        raise MediaMetadataStoreError("metadata job history query is invalid")
    return query_key


def _validate_timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise MediaMetadataStoreError("metadata job timestamp is invalid")
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaMetadataStoreError("metadata job timestamp is invalid") from exc
    if not math.isfinite(clean) or clean < 0:
        raise MediaMetadataStoreError("metadata job timestamp is invalid")
    return clean


def _row_to_job(row: sqlite3.Row) -> dict[str, object]:
    try:
        assets = json.loads(str(row["assets_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise MediaMetadataStoreError("metadata assets could not be read") from exc
    if not isinstance(assets, dict):
        raise MediaMetadataStoreError("metadata assets could not be read")
    clean_assets = _validate_assets(assets)
    return {
        "job_id": str(row["job_id"]),
        "kind": str(row["kind"]),
        "download_key": str(row["download_key"]),
        "code": str(row["code"]),
        "code_key": str(row["code_key"]),
        "variant": _stored_variant(str(row["kind"]), row["variant"]),
        "status": str(row["status"]),
        "relative_media_path": (
            str(row["relative_media_path"])
            if row["relative_media_path"] is not None
            else None
        ),
        "attempts": int(row["attempts"]),
        "next_attempt_at": float(row["next_attempt_at"]),
        "error": str(row["error"]) if row["error"] is not None else None,
        "assets": clean_assets,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _sql_slots(values: tuple[object, ...]) -> str:
    return ", ".join("?" for _ in values)
