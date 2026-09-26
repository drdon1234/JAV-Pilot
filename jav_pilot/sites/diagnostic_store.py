from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.migrations import SQLiteMigration, migrate_sqlite, require_columns
from ..notifications.events import NotificationEvent
from ..notifications.outbox import (
    enqueue_notification_event,
    initialize_notification_schema,
)
from .diagnostics import (
    DIAGNOSTIC_ERROR_CODES,
    DIAGNOSTIC_STAGES,
    DiagnosticResult,
    DiagnosticStatus,
    _validate_site_id,
    _validate_stage,
)


SITE_DIAGNOSTIC_SCHEMA_COMPONENT = "site_diagnostics"
SITE_DIAGNOSTIC_SCHEMA_VERSION = 1
MAX_CONSECUTIVE_FAILURES = 1_000_000


class SiteDiagnosticStoreError(RuntimeError):
    pass


class SQLiteSiteDiagnosticStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        failure_notification_threshold: int = 3,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise SiteDiagnosticStoreError(
                "site diagnostic database path must be absolute"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        if (
            isinstance(failure_notification_threshold, bool)
            or not 1 <= failure_notification_threshold <= 10_000
        ):
            raise SiteDiagnosticStoreError(
                "site notification threshold is invalid"
            )
        self._failure_notification_threshold = failure_notification_threshold
        self._initialize()

    def record(self, result: DiagnosticResult) -> DiagnosticStatus | None:
        if result.status == "deferred":
            return None
        success = result.status == "ok"
        error_code = None if success else result.error_code
        if not success and error_code not in DIAGNOSTIC_ERROR_CODES:
            raise SiteDiagnosticStoreError("site diagnostic result is invalid")
        latency = result.latency_ms
        if latency is None:
            raise SiteDiagnosticStoreError("site diagnostic latency is required")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO site_diagnostic_status (
                    site_id, stage, last_checked_at, last_success_at,
                    last_latency_ms, consecutive_failures, last_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id, stage) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = CASE
                        WHEN excluded.last_error_code IS NULL
                        THEN excluded.last_checked_at
                        ELSE site_diagnostic_status.last_success_at
                    END,
                    last_latency_ms = excluded.last_latency_ms,
                    consecutive_failures = CASE
                        WHEN excluded.last_error_code IS NULL THEN 0
                        ELSE MIN(
                            site_diagnostic_status.consecutive_failures + 1,
                            ?
                        )
                    END,
                    last_error_code = excluded.last_error_code
                """,
                (
                    result.site,
                    result.stage,
                    result.checked_at,
                    result.checked_at if success else None,
                    latency,
                    0 if success else 1,
                    error_code,
                    MAX_CONSECUTIVE_FAILURES,
                ),
            )
            row = connection.execute(
                "SELECT * FROM site_diagnostic_status "
                "WHERE site_id = ? AND stage = ?",
                (result.site, result.stage),
            ).fetchone()
            if row is not None and not success:
                last_success = (
                    f"{float(row['last_success_at']):.6f}"
                    if row["last_success_at"] is not None
                    else "initial"
                )
                event = NotificationEvent.site_failure(
                    site=result.site,
                    stage=result.stage,
                    incident_id=f"{result.site}:{result.stage}:{last_success}",
                    consecutive_failures=int(row["consecutive_failures"]),
                    threshold=self._failure_notification_threshold,
                    error_code=str(row["last_error_code"]),
                    occurred_at=result.checked_at,
                )
                if event is not None:
                    enqueue_notification_event(connection, event, clock=self._clock)
            connection.commit()
        if row is None:
            raise SiteDiagnosticStoreError("site diagnostic result was not persisted")
        return _row_to_status(row)

    def get(self, site: str, stage: str) -> DiagnosticStatus | None:
        clean_site = _safe_site_id(site)
        clean_stage = _safe_stage(stage)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM site_diagnostic_status "
                "WHERE site_id = ? AND stage = ?",
                (clean_site, clean_stage),
            ).fetchone()
        return _row_to_status(row) if row is not None else None

    def list(self, *, site: str | None = None) -> list[DiagnosticStatus]:
        clean_site = _safe_site_id(site) if site is not None else None
        with self._connect() as connection:
            if clean_site is None:
                rows = connection.execute(
                    "SELECT * FROM site_diagnostic_status "
                    "ORDER BY site_id, stage"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM site_diagnostic_status "
                    "WHERE site_id = ? ORDER BY stage",
                    (clean_site,),
                ).fetchall()
        return [_row_to_status(row) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            migrate_sqlite(
                connection,
                component=SITE_DIAGNOSTIC_SCHEMA_COMPONENT,
                current_version=SITE_DIAGNOSTIC_SCHEMA_VERSION,
                migrations=(SQLiteMigration(1, _create_schema, _verify_schema),),
                clock=self._clock,
                verify_current=_verify_schema,
            )
            initialize_notification_schema(connection, clock=self._clock)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
        finally:
            connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    stages = ", ".join(f"'{stage}'" for stage in DIAGNOSTIC_STAGES)
    error_codes = ", ".join(f"'{code}'" for code in sorted(DIAGNOSTIC_ERROR_CODES))
    connection.execute(
        f"""
        CREATE TABLE site_diagnostic_status (
            site_id TEXT NOT NULL CHECK (
                length(site_id) BETWEEN 1 AND 64
            ),
            stage TEXT NOT NULL CHECK (stage IN ({stages})),
            last_checked_at REAL NOT NULL CHECK (last_checked_at >= 0),
            last_success_at REAL CHECK (
                last_success_at IS NULL OR last_success_at >= 0
            ),
            last_latency_ms INTEGER NOT NULL CHECK (
                last_latency_ms BETWEEN 0 AND 180000
            ),
            consecutive_failures INTEGER NOT NULL CHECK (
                consecutive_failures BETWEEN 0 AND {MAX_CONSECUTIVE_FAILURES}
            ),
            last_error_code TEXT CHECK (
                last_error_code IS NULL OR last_error_code IN ({error_codes})
            ),
            PRIMARY KEY (site_id, stage),
            CHECK (
                (consecutive_failures = 0 AND last_error_code IS NULL)
                OR
                (consecutive_failures > 0 AND last_error_code IS NOT NULL)
            )
        )
        """
    )


def _verify_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "site_diagnostic_status",
        (
            "site_id",
            "stage",
            "last_checked_at",
            "last_success_at",
            "last_latency_ms",
            "consecutive_failures",
            "last_error_code",
        ),
    )


def _row_to_status(row: sqlite3.Row) -> DiagnosticStatus:
    return DiagnosticStatus(
        site=str(row["site_id"]),
        stage=str(row["stage"]),
        last_checked_at=float(row["last_checked_at"]),
        last_success_at=(
            float(row["last_success_at"])
            if row["last_success_at"] is not None
            else None
        ),
        last_latency_ms=int(row["last_latency_ms"]),
        consecutive_failures=int(row["consecutive_failures"]),
        last_error_code=(
            str(row["last_error_code"])
            if row["last_error_code"] is not None
            else None
        ),
    )


def _safe_site_id(value: object) -> str:
    return _validate_site_id(value)


def _safe_stage(value: object) -> str:
    return _validate_stage(value)


__all__ = [
    "SITE_DIAGNOSTIC_SCHEMA_VERSION",
    "SQLiteSiteDiagnosticStore",
    "SiteDiagnosticStoreError",
]
