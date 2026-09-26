"""SQLite outbox that records notification events and their per-adapter deliveries."""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..core.migrations import (
    SQLiteMigration,
    add_column_if_missing,
    migrate_sqlite,
    require_columns,
)
from .errors import NotificationError, NotificationStoreError
from .events import (
    EVENT_ID_RE,
    EVENT_TYPES,
    MAX_EVENT_OCCURRENCES,
    NotificationEvent,
    bounded_limit,
    event_catalog_code,
    event_timestamp,
    safe_name,
)

__all__ = [
    "DeliveryAttemptResult",
    "NOTIFICATION_SCHEMA_COMPONENT",
    "NOTIFICATION_SCHEMA_VERSION",
    "PendingDelivery",
    "SQLiteNotificationOutbox",
    "enqueue_notification_event",
    "initialize_notification_schema",
]


NOTIFICATION_SCHEMA_COMPONENT = "notifications"
NOTIFICATION_SCHEMA_VERSION = 3


AGGREGATE_EVENT_TYPES = frozenset({"disk_low", "site_failure"})
DELIVERY_STATES = frozenset({"pending", "inflight", "delivered", "dead"})

MAX_HISTORY_PER_DELIVERY = 64

MAX_ENABLE_BACKFILL_EVENTS = 100
ENABLE_BACKFILL_WINDOW_SECONDS = 5 * 60


@dataclass(frozen=True)
class PendingDelivery:
    event: NotificationEvent
    adapter: str
    attempt: int
    claim_token: str = field(repr=False)


@dataclass(frozen=True)
class DeliveryAttemptResult:
    outcome: Literal["delivered", "retry", "dead"]
    error_code: str | None = None
    http_status: int | None = None
    next_attempt_at: float | None = None

    def __post_init__(self) -> None:
        if self.outcome == "delivered":
            if self.error_code is not None or self.next_attempt_at is not None:
                raise NotificationStoreError("delivered result is invalid")
        else:
            if (
                safe_name(self.error_code, "delivery error code")
                != self.error_code
            ):
                raise NotificationStoreError("delivery error code is invalid")
            if self.outcome == "retry":
                if self.next_attempt_at is None or not math.isfinite(
                    self.next_attempt_at
                ) or self.next_attempt_at < 0:
                    raise NotificationStoreError("retry timestamp is invalid")
            elif self.next_attempt_at is not None:
                raise NotificationStoreError("dead delivery result is invalid")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise NotificationStoreError("delivery HTTP status is invalid")


def initialize_notification_schema(
    connection: sqlite3.Connection,
    *,
    clock: Callable[[], float] = time.time,
) -> int:
    return migrate_sqlite(
        connection,
        component=NOTIFICATION_SCHEMA_COMPONENT,
        current_version=NOTIFICATION_SCHEMA_VERSION,
            migrations=(
                SQLiteMigration(1, _create_schema, _verify_schema_v1),
                SQLiteMigration(2, _add_event_subject_context, _verify_schema),
                SQLiteMigration(3, _migrate_catalog_codes, _verify_schema),
            ),
        clock=clock,
        verify_current=_verify_schema,
    )


def enqueue_notification_event(
    connection: sqlite3.Connection,
    event: NotificationEvent,
    *,
    clock: Callable[[], float] = time.time,
) -> bool:
    if not connection.in_transaction:
        raise NotificationStoreError(
            "notification event must be enqueued inside the state transaction"
        )
    if not isinstance(event, NotificationEvent):
        raise NotificationStoreError("notification event is invalid")
    created_at = event_timestamp(clock())
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO notification_events (
            event_id, event_type, source, code, status, error_code,
            occurrence_count, first_occurred_at, last_occurred_at, created_at,
            subject_kind, subject_id, stage
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.event_type,
            event.source,
            event.code,
            event.status,
            event.error_code,
            event.occurrence_count,
            event.occurred_at,
            event.occurred_at,
            created_at,
            event.subject_kind,
            event.subject_id,
            event.stage,
        ),
    )
    if cursor.rowcount == 1:
        connection.execute(
            """
            INSERT INTO notification_deliveries (
                event_id, adapter, state, attempt_count, next_attempt_at,
                lease_until, claim_token, last_error_code, last_http_status,
                last_attempt_at, delivered_at, manual_retry_count,
                created_at, updated_at
            )
            SELECT ?, adapter, 'pending', 0, ?, NULL, NULL, NULL, NULL,
                   NULL, NULL, 0, ?, ? FROM notification_adapter_registry
            """,
            (event.event_id, created_at, created_at, created_at),
        )
        return True
    row = connection.execute(
        "SELECT event_type, source, status, subject_kind, subject_id, stage "
        "FROM notification_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone()
    stored = tuple(row) if row is not None else ()
    expected_identity = (event.event_type, event.source, event.status)
    expected_context = (event.subject_kind, event.subject_id, event.stage)
    if (
        row is None
        or stored[:3] != expected_identity
        or any(
            stored_value not in (None, "unknown") and stored_value != expected_value
            for stored_value, expected_value in zip(
                stored[3:], expected_context, strict=True
            )
        )
    ):
        raise NotificationStoreError("notification event identity collision")
    # Event identities intentionally describe the logical source/entity/status,
    # while code and error_code are diagnostic details that may be refined when
    # a retry or recovery observes the same terminal event again.  Treat that
    # as an idempotent replay instead of crashing the producer transaction.
    connection.execute(
        "UPDATE notification_events SET "
        "code = CASE WHEN last_occurred_at <= ? THEN ? ELSE code END, "
        "error_code = CASE WHEN last_occurred_at <= ? THEN ? ELSE error_code END, "
        "subject_kind = CASE WHEN subject_kind IS NULL OR subject_kind = 'unknown' "
        "THEN ? ELSE subject_kind END, "
        "subject_id = CASE WHEN subject_id IS NULL OR subject_id = 'unknown' "
        "THEN ? ELSE subject_id END, "
        "stage = CASE WHEN stage IS NULL OR stage = 'unknown' "
        "THEN ? ELSE stage END, "
        "occurrence_count = CASE WHEN event_type IN ('disk_low', 'site_failure') "
        "THEN MAX(occurrence_count, ?) ELSE occurrence_count END, "
        "last_occurred_at = MAX(last_occurred_at, ?) WHERE event_id = ?",
        (
            event.occurred_at,
            event.code,
            event.occurred_at,
            event.error_code,
            event.subject_kind,
            event.subject_id,
            event.stage,
            event.occurrence_count,
            event.occurred_at,
            event.event_id,
        ),
    )
    return False


class SQLiteNotificationOutbox:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise NotificationStoreError("notification database path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._initialize()

    def enqueue(self, event: NotificationEvent) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                created = enqueue_notification_event(
                    connection,
                    event,
                    clock=self._clock,
                )
                connection.commit()
                return created
            except BaseException:
                connection.rollback()
                raise

    def get(self, event_id: object) -> NotificationEvent | None:
        clean_id = _safe_event_id(event_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_events WHERE event_id = ?",
                (clean_id,),
            ).fetchone()
        return _row_to_event(row) if row is not None else None

    def list_events(self, *, limit: int = 100) -> list[dict[str, object]]:
        clean_limit = bounded_limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, event_type, source, code, status, error_code, "
                "subject_kind, subject_id, stage, "
                "occurrence_count, first_occurred_at, last_occurred_at, created_at "
                "FROM notification_events ORDER BY created_at DESC, event_id DESC "
                "LIMIT ?",
                (clean_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim(
        self,
        adapters: Sequence[str],
        *,
        now: float,
        lease_seconds: float,
        max_attempts: int,
    ) -> PendingDelivery | None:
        names = _adapter_names(adapters)
        if not names:
            return None
        clean_now = event_timestamp(now)
        if not 5 <= lease_seconds <= 3600:
            raise NotificationStoreError("notification lease duration is invalid")
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise NotificationStoreError("notification attempt bound is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._recover_expired(
                    connection,
                    names,
                    clean_now,
                    max_attempts,
                )
                self._close_exhausted(
                    connection,
                    names,
                    clean_now,
                    max_attempts,
                )
                slots = ", ".join("?" for _ in names)
                row = connection.execute(
                    f"""
                    SELECT e.*, d.adapter AS delivery_adapter,
                           d.attempt_count AS delivery_attempt_count
                    FROM notification_deliveries d
                    JOIN notification_events e ON e.event_id = d.event_id
                    WHERE d.adapter IN ({slots}) AND d.state = 'pending'
                      AND d.next_attempt_at <= ? AND d.attempt_count < ?
                    ORDER BY d.next_attempt_at, e.created_at, e.event_id, d.adapter
                    LIMIT 1
                    """,
                    (*names, clean_now, max_attempts),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                token = uuid.uuid4().hex
                attempt = int(row["delivery_attempt_count"]) + 1
                updated = connection.execute(
                    "UPDATE notification_deliveries SET state = 'inflight', "
                    "attempt_count = ?, lease_until = ?, claim_token = ?, "
                    "updated_at = ? WHERE event_id = ? AND adapter = ? "
                    "AND state = 'pending'",
                    (
                        attempt,
                        clean_now + float(lease_seconds),
                        token,
                        clean_now,
                        str(row["event_id"]),
                        str(row["delivery_adapter"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise NotificationStoreError("notification claim was lost")
                delivery = PendingDelivery(
                    event=_row_to_event(row),
                    adapter=str(row["delivery_adapter"]),
                    attempt=attempt,
                    claim_token=token,
                )
                connection.commit()
                return delivery
            except BaseException:
                connection.rollback()
                raise

    def finish(
        self,
        delivery: PendingDelivery,
        result: DeliveryAttemptResult,
        *,
        now: float,
    ) -> None:
        clean_now = event_timestamp(now)
        state = {
            "delivered": "delivered",
            "retry": "pending",
            "dead": "dead",
        }[result.outcome]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    """
                    UPDATE notification_deliveries
                    SET state = ?, next_attempt_at = ?, lease_until = NULL,
                        claim_token = NULL, last_error_code = ?,
                        last_http_status = ?, last_attempt_at = ?,
                        delivered_at = ?, updated_at = ?
                    WHERE event_id = ? AND adapter = ? AND state = 'inflight'
                      AND claim_token = ? AND attempt_count = ?
                    """,
                    (
                        state,
                        result.next_attempt_at if state == "pending" else clean_now,
                        result.error_code,
                        result.http_status,
                        clean_now,
                        clean_now if state == "delivered" else None,
                        clean_now,
                        delivery.event.event_id,
                        delivery.adapter,
                        delivery.claim_token,
                        delivery.attempt,
                    ),
                )
                if updated.rowcount != 1:
                    raise NotificationStoreError("notification delivery claim is stale")
                self._record_history(
                    connection,
                    event_id=delivery.event.event_id,
                    adapter=delivery.adapter,
                    attempt=delivery.attempt,
                    outcome=result.outcome,
                    error_code=result.error_code,
                    http_status=result.http_status,
                    attempted_at=clean_now,
                    next_attempt_at=result.next_attempt_at,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def manual_retry(
        self,
        event_id: object,
        *,
        adapter: object | None = None,
        now: float | None = None,
    ) -> int:
        clean_id = _safe_event_id(event_id)
        clean_adapter = (
            safe_name(adapter, "notification adapter")
            if adapter is not None
            else None
        )
        clean_now = event_timestamp(self._clock() if now is None else now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if clean_adapter is None:
                    rows = connection.execute(
                        "SELECT adapter, attempt_count FROM notification_deliveries "
                        "WHERE event_id = ? AND state = 'dead'",
                        (clean_id,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT adapter, attempt_count FROM notification_deliveries "
                        "WHERE event_id = ? AND adapter = ? AND state = 'dead'",
                        (clean_id, clean_adapter),
                    ).fetchall()
                for row in rows:
                    self._record_history(
                        connection,
                        event_id=clean_id,
                        adapter=str(row["adapter"]),
                        attempt=int(row["attempt_count"]),
                        outcome="manual_retry",
                        error_code=None,
                        http_status=None,
                        attempted_at=clean_now,
                        next_attempt_at=clean_now,
                    )
                if rows:
                    names = tuple(str(row["adapter"]) for row in rows)
                    slots = ", ".join("?" for _ in names)
                    connection.execute(
                        f"UPDATE notification_deliveries SET state = 'pending', "
                        f"attempt_count = 0, next_attempt_at = ?, lease_until = NULL, "
                        f"claim_token = NULL, last_error_code = NULL, "
                        f"last_http_status = NULL, "
                        f"manual_retry_count = MIN(manual_retry_count + 1, 1000), "
                        f"updated_at = ? "
                        f"WHERE event_id = ? AND adapter IN ({slots}) AND state = 'dead'",
                        (clean_now, clean_now, clean_id, *names),
                    )
                connection.commit()
                return len(rows)
            except BaseException:
                connection.rollback()
                raise

    def delivery_history(
        self,
        event_id: object,
        *,
        limit: int = MAX_HISTORY_PER_DELIVERY,
    ) -> list[dict[str, object]]:
        clean_id = _safe_event_id(event_id)
        clean_limit = bounded_limit(limit, maximum=MAX_HISTORY_PER_DELIVERY * 4)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, adapter, attempt_number, outcome, error_code, "
                "http_status, attempted_at, next_attempt_at "
                "FROM notification_delivery_history WHERE event_id = ? "
                "ORDER BY history_id DESC LIMIT ?",
                (clean_id, clean_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery_states(self, event_id: object) -> list[dict[str, object]]:
        clean_id = _safe_event_id(event_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, adapter, state, attempt_count, next_attempt_at, "
                "last_error_code, last_http_status, last_attempt_at, delivered_at, "
                "manual_retry_count, created_at, updated_at "
                "FROM notification_deliveries WHERE event_id = ? ORDER BY adapter",
                (clean_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def configure_adapters(
        self,
        adapters: Sequence[str],
        *,
        now: float | None = None,
    ) -> None:
        names = _adapter_names(adapters)
        clean_now = event_timestamp(self._clock() if now is None else now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if names:
                    slots = ", ".join("?" for _ in names)
                    connection.execute(
                        f"DELETE FROM notification_adapter_registry "
                        f"WHERE adapter NOT IN ({slots})",
                        names,
                    )
                else:
                    connection.execute("DELETE FROM notification_adapter_registry")
                self._prepare_deliveries(connection, names, clean_now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        with self._connect() as connection:
            initialize_notification_schema(connection, clock=self._clock)

    def _prepare_deliveries(
        self,
        connection: sqlite3.Connection,
        adapters: Sequence[str],
        now: float,
    ) -> None:
        for adapter in adapters:
            registered = connection.execute(
                "INSERT OR IGNORE INTO notification_adapter_registry "
                "(adapter, enabled_at) VALUES (?, ?)",
                (adapter, now),
            )
            if registered.rowcount != 1:
                continue
            # Preserve only a small recent startup window (events can be
            # emitted before lazy dispatcher registration) and cap it so
            # enabling a channel never replays the unbounded historical table.
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    event_id, adapter, state, attempt_count, next_attempt_at,
                    lease_until, claim_token, last_error_code, last_http_status,
                    last_attempt_at, delivered_at, manual_retry_count,
                    created_at, updated_at
                )
                SELECT event_id, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL,
                       NULL, NULL, 0, ?, ? FROM (
                    SELECT event_id FROM notification_events
                    WHERE created_at >= ?
                    ORDER BY created_at DESC, event_id DESC
                    LIMIT ?
                )
                """,
                (
                    adapter,
                    now,
                    now,
                    now,
                    now - ENABLE_BACKFILL_WINDOW_SECONDS,
                    MAX_ENABLE_BACKFILL_EVENTS,
                ),
            )

    def _recover_expired(
        self,
        connection: sqlite3.Connection,
        adapters: Sequence[str],
        now: float,
        max_attempts: int,
    ) -> None:
        slots = ", ".join("?" for _ in adapters)
        rows = connection.execute(
            f"SELECT event_id, adapter, attempt_count FROM notification_deliveries "
            f"WHERE adapter IN ({slots}) AND state = 'inflight' "
            f"AND lease_until <= ?",
            (*adapters, now),
        ).fetchall()
        for row in rows:
            attempts = int(row["attempt_count"])
            dead = attempts >= max_attempts
            self._record_history(
                connection,
                event_id=str(row["event_id"]),
                adapter=str(row["adapter"]),
                attempt=attempts,
                outcome="lease_expired",
                error_code="dispatcher_crash",
                http_status=None,
                attempted_at=now,
                next_attempt_at=None if dead else now,
            )
            connection.execute(
                "UPDATE notification_deliveries SET state = ?, next_attempt_at = ?, "
                "lease_until = NULL, claim_token = NULL, "
                "last_error_code = 'dispatcher_crash', updated_at = ? "
                "WHERE event_id = ? AND adapter = ? AND state = 'inflight'",
                (
                    "dead" if dead else "pending",
                    now,
                    now,
                    str(row["event_id"]),
                    str(row["adapter"]),
                ),
            )

    def _record_history(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        adapter: str,
        attempt: int,
        outcome: str,
        error_code: str | None,
        http_status: int | None,
        attempted_at: float,
        next_attempt_at: float | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO notification_delivery_history (
                event_id, adapter, attempt_number, outcome, error_code,
                http_status, attempted_at, next_attempt_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                adapter,
                attempt,
                outcome,
                error_code,
                http_status,
                attempted_at,
                next_attempt_at,
            ),
        )
        connection.execute(
            """
            DELETE FROM notification_delivery_history
            WHERE event_id = ? AND adapter = ? AND history_id NOT IN (
                SELECT history_id FROM notification_delivery_history
                WHERE event_id = ? AND adapter = ?
                ORDER BY history_id DESC LIMIT ?
            )
            """,
            (
                event_id,
                adapter,
                event_id,
                adapter,
                MAX_HISTORY_PER_DELIVERY,
            ),
        )

    def _close_exhausted(
        self,
        connection: sqlite3.Connection,
        adapters: Sequence[str],
        now: float,
        max_attempts: int,
    ) -> None:
        slots = ", ".join("?" for _ in adapters)
        rows = connection.execute(
            f"SELECT event_id, adapter, attempt_count FROM notification_deliveries "
            f"WHERE adapter IN ({slots}) AND state = 'pending' "
            f"AND attempt_count >= ?",
            (*adapters, max_attempts),
        ).fetchall()
        for row in rows:
            self._record_history(
                connection,
                event_id=str(row["event_id"]),
                adapter=str(row["adapter"]),
                attempt=int(row["attempt_count"]),
                outcome="dead",
                error_code="retry_exhausted",
                http_status=None,
                attempted_at=now,
                next_attempt_at=None,
            )
            connection.execute(
                "UPDATE notification_deliveries SET state = 'dead', "
                "last_error_code = 'retry_exhausted', updated_at = ? "
                "WHERE event_id = ? AND adapter = ? AND state = 'pending'",
                (now, str(row["event_id"]), str(row["adapter"])),
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


def _create_schema(connection: sqlite3.Connection) -> None:
    event_types = ", ".join(f"'{item}'" for item in sorted(EVENT_TYPES))
    delivery_states = ", ".join(f"'{item}'" for item in sorted(DELIVERY_STATES))
    connection.execute(
        f"""
        CREATE TABLE notification_events (
            event_id TEXT PRIMARY KEY CHECK (
                length(event_id) = 68
                AND substr(event_id, 1, 4) = 'evt_'
                AND substr(event_id, 5) NOT GLOB '*[^0-9a-f]*'
            ),
            event_type TEXT NOT NULL CHECK (event_type IN ({event_types})),
            source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 64),
            code TEXT CHECK (code IS NULL OR length(code) BETWEEN 1 AND 64),
            status TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 64),
            error_code TEXT CHECK (
                error_code IS NULL OR length(error_code) BETWEEN 1 AND 64
            ),
            occurrence_count INTEGER NOT NULL CHECK (
                occurrence_count BETWEEN 1 AND {MAX_EVENT_OCCURRENCES}
            ),
            first_occurred_at REAL NOT NULL CHECK (first_occurred_at >= 0),
            last_occurred_at REAL NOT NULL CHECK (
                last_occurred_at >= first_occurred_at
            ),
            created_at REAL NOT NULL CHECK (created_at >= 0)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE notification_adapter_registry (
            adapter TEXT PRIMARY KEY CHECK (length(adapter) BETWEEN 1 AND 64),
            enabled_at REAL NOT NULL CHECK (enabled_at >= 0)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE notification_deliveries (
            event_id TEXT NOT NULL REFERENCES notification_events(event_id)
                ON DELETE CASCADE,
            adapter TEXT NOT NULL CHECK (length(adapter) BETWEEN 1 AND 64),
            state TEXT NOT NULL CHECK (state IN ({delivery_states})),
            attempt_count INTEGER NOT NULL CHECK (
                attempt_count BETWEEN 0 AND 1000
            ),
            next_attempt_at REAL NOT NULL CHECK (next_attempt_at >= 0),
            lease_until REAL CHECK (lease_until IS NULL OR lease_until >= 0),
            claim_token TEXT CHECK (
                claim_token IS NULL OR length(claim_token) BETWEEN 1 AND 64
            ),
            last_error_code TEXT CHECK (
                last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 64
            ),
            last_http_status INTEGER CHECK (
                last_http_status IS NULL
                OR last_http_status BETWEEN 100 AND 599
            ),
            last_attempt_at REAL CHECK (
                last_attempt_at IS NULL OR last_attempt_at >= 0
            ),
            delivered_at REAL CHECK (delivered_at IS NULL OR delivered_at >= 0),
            manual_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (
                manual_retry_count BETWEEN 0 AND 1000
            ),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),
            PRIMARY KEY (event_id, adapter),
            CHECK (
                (state = 'inflight' AND lease_until IS NOT NULL AND claim_token IS NOT NULL)
                OR
                (state != 'inflight' AND lease_until IS NULL AND claim_token IS NULL)
            )
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE notification_delivery_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES notification_events(event_id)
                ON DELETE CASCADE,
            adapter TEXT NOT NULL CHECK (length(adapter) BETWEEN 1 AND 64),
            attempt_number INTEGER NOT NULL CHECK (
                attempt_number BETWEEN 0 AND 1000
            ),
            outcome TEXT NOT NULL CHECK (
                outcome IN (
                    'delivered', 'retry', 'dead',
                    'lease_expired', 'manual_retry'
                )
            ),
            error_code TEXT CHECK (
                error_code IS NULL OR length(error_code) BETWEEN 1 AND 64
            ),
            http_status INTEGER CHECK (
                http_status IS NULL OR http_status BETWEEN 100 AND 599
            ),
            attempted_at REAL NOT NULL CHECK (attempted_at >= 0),
            next_attempt_at REAL CHECK (
                next_attempt_at IS NULL OR next_attempt_at >= 0
            )
        )
        """
    )
    connection.execute(
        "CREATE INDEX notification_delivery_due_idx "
        "ON notification_deliveries (state, next_attempt_at, adapter)"
    )
    connection.execute(
        "CREATE INDEX notification_history_event_idx "
        "ON notification_delivery_history (event_id, adapter, history_id DESC)"
    )


def _add_event_subject_context(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "notification_events",
        "subject_kind",
        "TEXT CHECK (subject_kind IS NULL OR length(subject_kind) BETWEEN 1 AND 64)",
    )
    add_column_if_missing(
        connection,
        "notification_events",
        "subject_id",
        "TEXT CHECK (subject_id IS NULL OR length(subject_id) BETWEEN 1 AND 64)",
    )
    add_column_if_missing(
        connection,
        "notification_events",
        "stage",
        "TEXT CHECK (stage IS NULL OR length(stage) BETWEEN 1 AND 64)",
    )
    connection.execute(
        "UPDATE notification_events SET "
        "subject_kind = COALESCE(subject_kind, 'site'), "
        "subject_id = COALESCE(subject_id, 'unknown'), "
        "stage = COALESCE(stage, 'unknown') "
        "WHERE event_type = 'site_failure'"
    )


def _migrate_catalog_codes(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    updates: list[tuple[str, str]] = []
    for row in connection.execute(
        "SELECT event_id, code FROM notification_events WHERE code IS NOT NULL"
    ).fetchall():
        try:
            display_code = event_catalog_code(row["code"])
        except NotificationError as exc:
            raise NotificationStoreError(
                "notification catalog code is invalid"
            ) from exc
        if display_code != str(row["code"]):
            updates.append((display_code, str(row["event_id"])))
    connection.executemany(
        "UPDATE notification_events SET code = ? WHERE event_id = ?",
        updates,
    )


def _verify_schema_v1(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "notification_events",
        (
            "event_id",
            "event_type",
            "source",
            "code",
            "status",
            "error_code",
            "occurrence_count",
            "first_occurred_at",
            "last_occurred_at",
            "created_at",
        ),
    )
    require_columns(
        connection,
        "notification_adapter_registry",
        ("adapter", "enabled_at"),
    )
    require_columns(
        connection,
        "notification_deliveries",
        (
            "event_id",
            "adapter",
            "state",
            "attempt_count",
            "next_attempt_at",
            "lease_until",
            "claim_token",
            "last_error_code",
            "last_http_status",
            "last_attempt_at",
            "delivered_at",
            "manual_retry_count",
            "created_at",
            "updated_at",
        ),
    )


def _verify_schema(connection: sqlite3.Connection) -> None:
    _verify_schema_v1(connection)
    require_columns(
        connection,
        "notification_events",
        ("subject_kind", "subject_id", "stage"),
    )
    require_columns(
        connection,
        "notification_delivery_history",
        (
            "history_id",
            "event_id",
            "adapter",
            "attempt_number",
            "outcome",
            "error_code",
            "http_status",
            "attempted_at",
            "next_attempt_at",
        ),
    )
    for row in connection.execute(
        "SELECT code FROM notification_events WHERE code IS NOT NULL"
    ).fetchall():
        try:
            expected = event_catalog_code(row["code"])
        except NotificationError as exc:
            raise NotificationStoreError(
                "notification catalog code is invalid"
            ) from exc
        if str(row["code"]) != expected:
            raise NotificationStoreError("notification catalog code is invalid")


def _row_to_event(row: sqlite3.Row) -> NotificationEvent:
    return NotificationEvent(
        event_id=str(row["event_id"]),
        event_type=str(row["event_type"]),
        source=str(row["source"]),
        code=str(row["code"]) if row["code"] is not None else None,
        status=str(row["status"]),
        error_code=(
            str(row["error_code"]) if row["error_code"] is not None else None
        ),
        occurrence_count=int(row["occurrence_count"]),
        occurred_at=float(row["last_occurred_at"]),
        subject_kind=(
            str(row["subject_kind"]) if row["subject_kind"] is not None else None
        ),
        subject_id=str(row["subject_id"]) if row["subject_id"] is not None else None,
        stage=str(row["stage"]) if row["stage"] is not None else None,
    )


def _safe_event_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not EVENT_ID_RE.fullmatch(clean):
        raise NotificationStoreError("notification event identity is invalid")
    return clean


def _adapter_names(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise NotificationStoreError("notification adapters are invalid")
    names = tuple(sorted({safe_name(value, "notification adapter") for value in values}))
    if len(names) > 16:
        raise NotificationStoreError("too many notification adapters")
    return names
