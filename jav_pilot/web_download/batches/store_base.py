"""Shared state of WebDownloadBatchStore: the database, schema setup and batch reads."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from ...core.migrations import (
    MigrationError,
    SchemaTooNewError,
    SQLiteMigration,
    migrate_sqlite,
)
from ...notifications.outbox import initialize_notification_schema
from ..errors import WebDownloadConfigError
from .errors import WebDownloadBatchConflictError, WebDownloadBatchNotFoundError
from .models import RESOURCE_SEARCH_SELECTION_PROVENANCE
from .records import (
    PREVIEW_TTL_SECONDS,
    enqueue_batch_terminal_notification,
    expire_ready_rows,
    row_to_batch,
)
from .schema import (
    BATCH_SCHEMA_COMPONENT,
    BATCH_SCHEMA_VERSION,
    migrate_web_download_batch_v1,
    migrate_web_download_batch_v2,
    migrate_web_download_batch_v3,
    migrate_web_download_batch_v4,
    migrate_web_download_batch_v5,
    migrate_web_download_batch_v6,
    migrate_web_download_batch_v7,
    migrate_web_download_batch_v8,
    migrate_web_download_batch_v9,
    migrate_web_download_batch_v10,
    verify_web_download_batch_schema,
)
from .validation import validate_batch_id


class BatchStoreBase:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise WebDownloadConfigError("web download database path must be absolute")
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.fail_interrupted()
        self.expire_ready()

    def get(self, batch_id: str) -> dict[str, object]:
        clean_id = validate_batch_id(batch_id)
        self._expire_ready(clean_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            items = connection.execute(
                "SELECT code, code_key, status, job_id, selected, quality_status, "
                "available_heights_json, default_height, quality_strategy, "
                "requested_height, quality_error_code, available_variants_json, "
                "variant "
                "FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
        return row_to_batch(row, items, self._clock())

    def fail_interrupted(self) -> int:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM web_download_batch_items WHERE batch_id IN ("
                "SELECT batch_id FROM web_download_batches "
                "WHERE auto_commit = 1 "
                "AND status IN ('queued', 'discovering', 'ready'))"
            )
            connection.execute(
                "UPDATE web_download_batches SET status = 'queued', "
                "discovered_count = 0, discovery_complete = NULL, "
                "quality_complete = 1, updated_at = ?, expires_at = NULL, "
                "error = NULL WHERE auto_commit = 1 "
                "AND status IN ('queued', 'discovering', 'ready')",
                (now,),
            )
            connection.execute(
                "DELETE FROM web_download_batches "
                "WHERE direct_queue_key_hash IS NOT NULL "
                "AND auto_commit = 0 AND status != 'committed'"
            )
            connection.execute(
                "UPDATE web_download_batches SET status = 'ready', updated_at = ?, "
                "expires_at = ?, error = NULL WHERE provenance_type = ? "
                "AND quality_complete = 1 AND status IN ('queued', 'discovering')",
                (
                    now,
                    now + PREVIEW_TTL_SECONDS,
                    RESOURCE_SEARCH_SELECTION_PROVENANCE,
                ),
            )
            interrupted = connection.execute(
                "SELECT batch_id, status FROM web_download_batches "
                "WHERE auto_commit = 0 AND (status IN ('queued', 'discovering') "
                "OR (status = 'ready' AND quality_complete = 0)) ORDER BY batch_id"
            ).fetchall()
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'failed', updated_at = ?, "
                "quality_complete = 1, expires_at = NULL, error = CASE "
                "WHEN status = 'ready' THEN 'Batch quality resolution was interrupted' "
                "WHEN provenance_type = ? "
                "THEN 'Selected resource preview was interrupted' "
                "ELSE 'Batch discovery was interrupted' END "
                "WHERE auto_commit = 0 AND (status IN ('queued', 'discovering') "
                "OR (status = 'ready' AND quality_complete = 0))",
                (now, RESOURCE_SEARCH_SELECTION_PROVENANCE),
            ).rowcount
            if changed != len(interrupted):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "interrupted web download batches changed concurrently"
                )
            for row in interrupted:
                enqueue_batch_terminal_notification(
                    connection,
                    str(row["batch_id"]),
                    status="failed",
                    occurred_at=now,
                    clock=self._clock,
                )
            connection.commit()
        return int(changed)

    def expire_ready(self, batch_id: str | None = None) -> tuple[str, ...]:
        clean_id = validate_batch_id(batch_id) if batch_id is not None else None
        now = self._clock()
        with self._connect() as connection:
            probe = connection.execute(
                "SELECT 1 FROM web_download_batches WHERE status = 'ready' "
                "AND quality_complete = 1 AND expires_at IS NOT NULL "
                "AND expires_at <= ? "
                + ("AND batch_id = ? " if clean_id is not None else "")
                + "LIMIT 1",
                (now, clean_id) if clean_id is not None else (now,),
            ).fetchone()
            if probe is None:
                return ()
            connection.execute("BEGIN IMMEDIATE")
            expired = expire_ready_rows(connection, now=now, batch_id=clean_id)
            connection.commit()
        return expired

    def _expire_ready(self, batch_id: str) -> None:
        self.expire_ready(batch_id)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                migrate_sqlite(
                    connection,
                    component=BATCH_SCHEMA_COMPONENT,
                    current_version=BATCH_SCHEMA_VERSION,
                    migrations=(
                        SQLiteMigration(1, migrate_web_download_batch_v1),
                        SQLiteMigration(2, migrate_web_download_batch_v2),
                        SQLiteMigration(3, migrate_web_download_batch_v3),
                        SQLiteMigration(4, migrate_web_download_batch_v4),
                        SQLiteMigration(5, migrate_web_download_batch_v5),
                        SQLiteMigration(6, migrate_web_download_batch_v6),
                        SQLiteMigration(7, migrate_web_download_batch_v7),
                        SQLiteMigration(8, migrate_web_download_batch_v8),
                        SQLiteMigration(9, migrate_web_download_batch_v9),
                        SQLiteMigration(10, migrate_web_download_batch_v10),
                    ),
                    clock=self._clock,
                    verify_current=verify_web_download_batch_schema,
                )
                initialize_notification_schema(connection, clock=self._clock)
            except SchemaTooNewError as exc:
                raise WebDownloadConfigError(
                    "web download batch database schema is newer than this application supports"
                ) from exc
            except MigrationError as exc:
                raise WebDownloadConfigError(str(exc)) from exc

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
