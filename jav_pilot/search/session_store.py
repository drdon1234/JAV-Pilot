from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.catalog_code import normalize_catalog_code
from ..core.migrations import MigrationError, SQLiteMigration, migrate_sqlite, require_columns


METADATA_SEARCH_SCHEMA_COMPONENT = "metadata_search_sessions"
METADATA_SEARCH_SCHEMA_VERSION = 3
METADATA_SEARCH_TERMINAL_EVENTS = frozenset({"done", "cancelled", "error"})
METADATA_SEARCH_EVENTS = frozenset(
    {"source", "delta", "base", "result", *METADATA_SEARCH_TERMINAL_EVENTS}
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{8,80}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_REQUEST_JSON_BYTES = 64 * 1024
_MAX_EVENT_JSON_BYTES = 8 * 1024 * 1024
_MAX_CONTINUATION_JSON_BYTES = 32 * 1024 * 1024
_MAX_EVENTS_PER_READ = 5_000
_MAX_SNAPSHOT_EVENTS = 100_001
DEFAULT_MAX_SESSIONS = 12
DEFAULT_MAX_EVENTS_PER_SESSION = 5_000
DEFAULT_MAX_EVENT_BYTES_PER_SESSION = 32 * 1024 * 1024


class MetadataSearchStoreError(RuntimeError):
    pass


class MetadataSearchConflictError(MetadataSearchStoreError):
    pass


class MetadataSearchNotFoundError(MetadataSearchStoreError):
    pass


@dataclass(frozen=True, slots=True)
class MetadataSearchAppendResult:
    event_id: int | None
    accepted: bool
    session_running: bool
    storage_limited: bool

    @property
    def should_continue(self) -> bool:
        return self.accepted and not self.storage_limited


class SQLiteMetadataSearchStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_events_per_session: int = DEFAULT_MAX_EVENTS_PER_SESSION,
        max_event_bytes_per_session: int = DEFAULT_MAX_EVENT_BYTES_PER_SESSION,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise MetadataSearchStoreError(
                "metadata search database path must be absolute"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._max_sessions = _bounded_integer(
            max_sessions, minimum=1, maximum=1_000, label="session limit"
        )
        self._max_events_per_session = _bounded_integer(
            max_events_per_session,
            minimum=10,
            maximum=100_000,
            label="event limit",
        )
        self._max_event_bytes_per_session = _bounded_integer(
            max_event_bytes_per_session,
            minimum=64 * 1024,
            maximum=1024 * 1024 * 1024,
            label="event byte limit",
        )
        self._initialize()

    def create_or_get(
        self,
        *,
        request_id: object,
        fingerprint: object,
        request: Mapping[str, object],
    ) -> tuple[dict[str, object], bool]:
        clean_id = _request_id(request_id)
        clean_fingerprint = _fingerprint(fingerprint)
        request_json = _json_object(request, maximum=_MAX_REQUEST_JSON_BYTES)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["request_fingerprint"]) != clean_fingerprint
                    or str(row["request_json"]) != request_json
                ):
                    connection.rollback()
                    raise MetadataSearchConflictError(
                        "metadata search request id is already in use"
                    )
                connection.commit()
                return _row_to_session(row), False
            now = _next_session_timestamp_locked(connection, now)
            self._prune_terminal_sessions_locked(connection)
            capacity = connection.execute(
                "SELECT COUNT(*) AS count FROM metadata_search_sessions"
            ).fetchone()
            if (
                int(capacity["count"] if capacity is not None else 0)
                >= self._max_sessions
            ):
                connection.rollback()
                raise MetadataSearchStoreError(
                    "metadata search session capacity is occupied by active searches"
                )
            connection.execute(
                """
                INSERT INTO metadata_search_sessions (
                    request_id, request_fingerprint, request_json, status,
                    terminal_event, last_event_id, event_bytes, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', NULL, 0, 0, ?, ?)
                """,
                (clean_id, clean_fingerprint, request_json, now, now),
            )
            row = connection.execute(
                "SELECT * FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise MetadataSearchStoreError("metadata search was not persisted")
        return _row_to_session(row), True

    def append_event(
        self,
        request_id: object,
        event: object,
        payload: Mapping[str, object],
        *,
        continuation: Mapping[str, object] | None = None,
        continuation_mode: str | None = None,
    ) -> MetadataSearchAppendResult:
        clean_id = _request_id(request_id)
        clean_event = str(event or "").strip().lower()
        if clean_event not in METADATA_SEARCH_EVENTS:
            raise MetadataSearchStoreError("metadata search event is invalid")
        payload_json = _json_object(payload, maximum=_MAX_EVENT_JSON_BYTES)
        if (continuation is None) != (continuation_mode is None):
            raise MetadataSearchStoreError("metadata search continuation is invalid")
        if continuation is not None and clean_event not in {"done", "cancelled"}:
            raise MetadataSearchStoreError("metadata search continuation is invalid")
        if continuation_mode is not None and continuation_mode not in {
            "retry",
            "extend",
        }:
            raise MetadataSearchStoreError("metadata search continuation is invalid")
        continuation_limited = False
        try:
            continuation_json = (
                _json_object(continuation, maximum=_MAX_CONTINUATION_JSON_BYTES)
                if continuation is not None
                else None
            )
        except MetadataSearchStoreError as exc:
            if str(exc) != "metadata search JSON value is too large":
                raise
            # Continuation state is optional. A terminal event must still be
            # committed when a large result set cannot fit in its resume slot.
            continuation_json = None
            continuation_mode = None
            continuation_limited = True
        now = _timestamp(self._clock())
        status = {
            "done": "complete",
            "cancelled": "cancelled",
            "error": "error",
        }.get(clean_event, "running")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, last_event_id, event_bytes, created_at, updated_at "
                "FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return MetadataSearchAppendResult(None, False, False, False)
            if str(row["status"]) != "running":
                connection.commit()
                return MetadataSearchAppendResult(
                    int(row["last_event_id"]), False, False, False
                )
            now = max(now, float(row["created_at"]), float(row["updated_at"]))
            event_id = int(row["last_event_id"]) + 1
            payload_bytes = len(payload_json.encode("utf-8"))
            over_limit = clean_event not in METADATA_SEARCH_TERMINAL_EVENTS and (
                event_id > self._max_events_per_session
                or int(row["event_bytes"]) + payload_bytes
                > self._max_event_bytes_per_session
            )
            if over_limit:
                clean_event = "error"
                payload_json = _json_object(
                    {
                        "request_id": clean_id,
                        "error": "Search result storage limit reached; partial results were kept",
                        "code": "search_storage_limit",
                    },
                    maximum=_MAX_EVENT_JSON_BYTES,
                )
                payload_bytes = len(payload_json.encode("utf-8"))
                status = "error"
                continuation_json = None
                continuation_mode = None
            connection.execute(
                """
                INSERT INTO metadata_search_events (
                    request_id, event_id, event_name, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (clean_id, event_id, clean_event, payload_json, now),
            )
            connection.execute(
                """
                UPDATE metadata_search_sessions
                SET status = ?, terminal_event = ?, last_event_id = ?,
                    event_bytes = event_bytes + ?, updated_at = ?,
                    continuation_json = ?, continuation_mode = ?
                WHERE request_id = ?
                """,
                (
                    status,
                    clean_event
                    if clean_event in METADATA_SEARCH_TERMINAL_EVENTS
                    else None,
                    event_id,
                    payload_bytes,
                    now,
                    continuation_json,
                    continuation_mode,
                    clean_id,
                ),
            )
            if clean_event in METADATA_SEARCH_TERMINAL_EVENTS:
                self._compact_terminal_events_locked(connection, clean_id)
            connection.commit()
        return MetadataSearchAppendResult(
            event_id=event_id,
            accepted=True,
            session_running=status == "running",
            storage_limited=over_limit or continuation_limited,
        )

    def get(self, request_id: object) -> dict[str, object]:
        clean_id = _request_id(request_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            raise MetadataSearchNotFoundError("metadata search was not found")
        return _row_to_session(row)

    def latest(self) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM metadata_search_sessions "
                "ORDER BY created_at DESC, request_id DESC LIMIT 1"
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def events_since(
        self,
        request_id: object,
        after_event_id: object = 0,
        *,
        limit: int = _MAX_EVENTS_PER_READ,
    ) -> list[dict[str, object]]:
        clean_id = _request_id(request_id)
        try:
            clean_after = int(after_event_id)
            clean_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise MetadataSearchStoreError(
                "metadata search event cursor is invalid"
            ) from exc
        if clean_after < 0 or not 1 <= clean_limit <= _MAX_EVENTS_PER_READ:
            raise MetadataSearchStoreError("metadata search event cursor is invalid")
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            if exists is None:
                raise MetadataSearchNotFoundError("metadata search was not found")
            rows = connection.execute(
                """
                SELECT event_id, event_name, payload_json, created_at
                FROM metadata_search_events
                WHERE request_id = ? AND event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (clean_id, clean_after, clean_limit),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def snapshot(self, request_id: object) -> dict[str, object]:
        clean_id = _request_id(request_id)
        events: list[dict[str, object]] = []
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MetadataSearchNotFoundError("metadata search was not found")
            session = _row_to_session(row)
            last_event_id = int(row["last_event_id"])
            cursor = 0
            while cursor < last_event_id:
                rows = connection.execute(
                    """
                    SELECT event_id, event_name, payload_json, created_at
                    FROM metadata_search_events
                    WHERE request_id = ? AND event_id > ? AND event_id <= ?
                    ORDER BY event_id
                    LIMIT ?
                    """,
                    (clean_id, cursor, last_event_id, _MAX_EVENTS_PER_READ),
                ).fetchall()
                if not rows:
                    connection.rollback()
                    raise MetadataSearchStoreError(
                        "metadata search event history is incomplete"
                    )
                events.extend(_row_to_event(event_row) for event_row in rows)
                if len(events) > _MAX_SNAPSHOT_EVENTS:
                    connection.rollback()
                    raise MetadataSearchStoreError(
                        "metadata search snapshot is too large"
                    )
                cursor = int(rows[-1]["event_id"])
            connection.commit()
        session["events"] = events
        return session

    def latest_snapshot(self) -> dict[str, object] | None:
        session = self.latest()
        return self.snapshot(session["request_id"]) if session is not None else None

    def continuation(self, request_id: object) -> dict[str, object] | None:
        clean_id = _request_id(request_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, terminal_event, continuation_json, continuation_mode "
                "FROM metadata_search_sessions WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            raise MetadataSearchNotFoundError("metadata search was not found")
        if (
            str(row["status"]) not in {"complete", "cancelled"}
            or str(row["terminal_event"]) not in {"done", "cancelled"}
            or row["continuation_json"] is None
            or str(row["continuation_mode"] or "") not in {"retry", "extend"}
        ):
            return None
        try:
            payload = json.loads(str(row["continuation_json"]))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MetadataSearchStoreError(
                "metadata search continuation is corrupted"
            ) from exc
        if not isinstance(payload, dict):
            raise MetadataSearchStoreError("metadata search continuation is corrupted")
        return {
            "mode": str(row["continuation_mode"]),
            "state": payload,
        }

    def running_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id FROM metadata_search_sessions "
                "WHERE status = 'running' ORDER BY created_at, request_id"
            ).fetchall()
        return tuple(str(row["request_id"]) for row in rows)

    def fail_interrupted(self) -> int:
        interrupted = self.running_ids()
        for request_id in interrupted:
            self.append_event(
                request_id,
                "error",
                {
                    "request_id": request_id,
                    "error": "搜索服务已重启，已保存此前接收的结果，请重新搜索",
                    "code": "search_interrupted",
                },
            )
        return len(interrupted)

    def clear(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM metadata_search_sessions"
            ).fetchone()
            count = int(row["count"] if row is not None else 0)
            connection.execute("DELETE FROM metadata_search_sessions")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count

    def _prune_terminal_sessions_locked(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM metadata_search_sessions"
        ).fetchone()
        excess = int(row["count"] if row is not None else 0) - self._max_sessions + 1
        if excess <= 0:
            return
        connection.execute(
            """
            DELETE FROM metadata_search_sessions
            WHERE request_id IN (
                SELECT request_id FROM metadata_search_sessions
                WHERE status != 'running'
                ORDER BY created_at, request_id
                LIMIT ?
            )
            """,
            (excess,),
        )

    @staticmethod
    def _compact_terminal_events_locked(
        connection: sqlite3.Connection, request_id: str
    ) -> None:
        base = connection.execute(
            "SELECT 1 FROM metadata_search_events "
            "WHERE request_id = ? AND event_name = 'base' LIMIT 1",
            (request_id,),
        ).fetchone()
        if base is not None:
            connection.execute(
                "DELETE FROM metadata_search_events "
                "WHERE request_id = ? AND event_name IN ('source', 'delta')",
                (request_id,),
            )
        row = connection.execute(
            "SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) AS bytes "
            "FROM metadata_search_events WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        connection.execute(
            "UPDATE metadata_search_sessions SET event_bytes = ? WHERE request_id = ?",
            (int(row["bytes"] if row is not None else 0), request_id),
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            migrate_sqlite(
                connection,
                component=METADATA_SEARCH_SCHEMA_COMPONENT,
                current_version=METADATA_SEARCH_SCHEMA_VERSION,
                migrations=(
                    SQLiteMigration(1, _create_schema, None),
                    SQLiteMigration(2, _migrate_schema_v2, _verify_schema),
                    SQLiteMigration(3, _migrate_schema_v3, _verify_schema),
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
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        finally:
            connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE metadata_search_sessions (
            request_id TEXT PRIMARY KEY CHECK (length(request_id) BETWEEN 8 AND 80),
            request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
            request_json TEXT NOT NULL CHECK (length(request_json) BETWEEN 2 AND 65536),
            status TEXT NOT NULL CHECK (
                status IN ('running', 'complete', 'cancelled', 'error')
            ),
            terminal_event TEXT CHECK (
                terminal_event IS NULL OR terminal_event IN ('done', 'cancelled', 'error')
            ),
            last_event_id INTEGER NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
            event_bytes INTEGER NOT NULL DEFAULT 0 CHECK (event_bytes >= 0),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= created_at),
            CHECK (
                (status = 'running' AND terminal_event IS NULL)
                OR (status != 'running' AND terminal_event IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE metadata_search_events (
            request_id TEXT NOT NULL,
            event_id INTEGER NOT NULL CHECK (event_id > 0),
            event_name TEXT NOT NULL CHECK (
                event_name IN ('source', 'delta', 'base', 'result', 'done', 'cancelled', 'error')
            ),
            payload_json TEXT NOT NULL CHECK (length(payload_json) BETWEEN 2 AND 8388608),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            PRIMARY KEY (request_id, event_id),
            FOREIGN KEY (request_id) REFERENCES metadata_search_sessions(request_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX metadata_search_sessions_latest "
        "ON metadata_search_sessions(created_at DESC, request_id DESC)"
    )


def _migrate_schema_v2(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(metadata_search_sessions)"
        ).fetchall()
    }
    if "continuation_json" not in columns:
        connection.execute(
            "ALTER TABLE metadata_search_sessions ADD COLUMN continuation_json TEXT"
        )
    if "continuation_mode" not in columns:
        connection.execute(
            "ALTER TABLE metadata_search_sessions ADD COLUMN continuation_mode TEXT "
            "CHECK (continuation_mode IS NULL OR continuation_mode IN ('retry', 'extend'))"
        )


def _migrate_schema_v3(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    session_rows = connection.execute(
        "SELECT request_id, continuation_json FROM metadata_search_sessions "
        "ORDER BY request_id"
    ).fetchall()
    for session in session_rows:
        request_id = str(session["request_id"])
        work_id_targets: dict[str, str] = {}
        continuation_json = session["continuation_json"]
        if continuation_json is not None:
            continuation = _migration_json_object(
                continuation_json,
                "metadata search continuation",
            )
            migrated_continuation = _migrate_search_value(
                continuation,
                work_id_targets,
            )
            if not isinstance(migrated_continuation, dict):
                raise MigrationError("metadata search continuation is invalid")
            continuation_json = _json_object(
                migrated_continuation,
                maximum=_MAX_CONTINUATION_JSON_BYTES,
            )

        event_updates: list[tuple[str, int]] = []
        event_bytes = 0
        for event in connection.execute(
            "SELECT event_id, payload_json FROM metadata_search_events "
            "WHERE request_id = ? ORDER BY event_id",
            (request_id,),
        ).fetchall():
            payload = _migration_json_object(
                event["payload_json"],
                "metadata search event",
            )
            migrated_payload = _migrate_search_value(payload, work_id_targets)
            if not isinstance(migrated_payload, dict):
                raise MigrationError("metadata search event is invalid")
            payload_json = _json_object(
                migrated_payload,
                maximum=_MAX_EVENT_JSON_BYTES,
            )
            event_bytes += len(payload_json.encode("utf-8"))
            event_updates.append((payload_json, int(event["event_id"])))
        connection.executemany(
            "UPDATE metadata_search_events SET payload_json = ? "
            "WHERE request_id = ? AND event_id = ?",
            (
                (payload_json, request_id, event_id)
                for payload_json, event_id in event_updates
            ),
        )
        connection.execute(
            "UPDATE metadata_search_sessions "
            "SET continuation_json = ?, event_bytes = ? WHERE request_id = ?",
            (continuation_json, event_bytes, request_id),
        )


def _migration_json_object(value: object, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{label} is invalid") from exc
    if not isinstance(decoded, dict):
        raise MigrationError(f"{label} is invalid")
    return decoded


def _fc2_migration_code(value: object) -> tuple[str, str] | None:
    normalized = normalize_catalog_code(value, max_length=64)
    if normalized is None or re.fullmatch(r"FC2PPV\d{2,9}", normalized[1]) is None:
        return None
    return normalized


def _register_migrated_work_id(
    old_work_id: str,
    new_work_id: str,
    targets: dict[str, str],
) -> None:
    existing = targets.setdefault(new_work_id, old_work_id)
    if existing != old_work_id:
        raise MigrationError(
            "metadata search works collide after catalog code normalization"
        )


def _migrate_work_id(value: str, targets: dict[str, str]) -> str:
    if not value.startswith("code:"):
        return value
    normalized = _fc2_migration_code(value.removeprefix("code:"))
    if normalized is None:
        return value
    migrated = f"code:{normalized[1]}"
    _register_migrated_work_id(value, migrated, targets)
    return migrated


def _migrate_search_value(
    value: object,
    work_id_targets: dict[str, str],
    *,
    field_name: str = "",
) -> object:
    if isinstance(value, list):
        if field_name == "records":
            identities: dict[str, str] = {}
            for item in value:
                if not isinstance(item, dict):
                    continue
                raw_code = item.get("code")
                normalized = _fc2_migration_code(raw_code)
                if normalized is None:
                    continue
                legacy_key = "".join(
                    character
                    for character in str(raw_code).strip().upper()
                    if character.isalnum()
                )
                existing = identities.setdefault(normalized[1], legacy_key)
                if existing != legacy_key:
                    raise MigrationError(
                        "metadata search continuation records collide after "
                        "catalog code normalization"
                    )
        migrated = [
            _migrate_search_value(
                item,
                work_id_targets,
                field_name=field_name,
            )
            for item in value
        ]
        if field_name == "works":
            work_ids = [item for item in migrated if isinstance(item, str)]
            if len(work_ids) != len(migrated) or len(set(work_ids)) != len(work_ids):
                raise MigrationError(
                    "metadata search continuation work identities collide"
                )
        return migrated
    if not isinstance(value, dict):
        if field_name in {"work_id", "works"} and isinstance(value, str):
            return _migrate_work_id(value, work_id_targets)
        return value

    migrated = {
        str(key): _migrate_search_value(
            item,
            work_id_targets,
            field_name=str(key),
        )
        for key, item in value.items()
    }
    normalized_code = _fc2_migration_code(migrated.get("code"))
    normalized_canonical = _fc2_migration_code(migrated.get("canonical_code"))
    identity = normalized_code or normalized_canonical
    if normalized_code is not None:
        migrated["code"] = normalized_code[0]
    if normalized_canonical is not None:
        migrated["canonical_code"] = normalized_canonical[1]
    if "code_key" in migrated and identity is not None:
        migrated["code_key"] = identity[1]
    if "work_id" in migrated and isinstance(migrated["work_id"], str):
        old_work_id = str(value.get("work_id") or migrated["work_id"])
        if identity is not None:
            new_work_id = f"code:{identity[1]}"
            _register_migrated_work_id(
                old_work_id,
                new_work_id,
                work_id_targets,
            )
            migrated["work_id"] = new_work_id
        else:
            migrated["work_id"] = _migrate_work_id(
                migrated["work_id"],
                work_id_targets,
            )
    return migrated


def _verify_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "metadata_search_sessions",
        (
            "request_id",
            "request_fingerprint",
            "request_json",
            "status",
            "terminal_event",
            "last_event_id",
            "event_bytes",
            "continuation_json",
            "continuation_mode",
            "created_at",
            "updated_at",
        ),
    )
    require_columns(
        connection,
        "metadata_search_events",
        ("request_id", "event_id", "event_name", "payload_json", "created_at"),
    )


def _row_to_session(row: sqlite3.Row) -> dict[str, object]:
    try:
        request = json.loads(str(row["request_json"]))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MetadataSearchStoreError("metadata search request is corrupted") from exc
    if not isinstance(request, dict):
        raise MetadataSearchStoreError("metadata search request is corrupted")
    return {
        "request_id": str(row["request_id"]),
        "request": request,
        "status": str(row["status"]),
        "terminal_event": (
            str(row["terminal_event"]) if row["terminal_event"] is not None else None
        ),
        "last_event_id": int(row["last_event_id"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _row_to_event(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MetadataSearchStoreError("metadata search event is corrupted") from exc
    if not isinstance(payload, dict):
        raise MetadataSearchStoreError("metadata search event is corrupted")
    return {
        "id": int(row["event_id"]),
        "event": str(row["event_name"]),
        "payload": payload,
        "created_at": float(row["created_at"]),
    }


def _json_object(value: Mapping[str, object], *, maximum: int) -> str:
    if not isinstance(value, Mapping):
        raise MetadataSearchStoreError("metadata search JSON value is invalid")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MetadataSearchStoreError("metadata search JSON value is invalid") from exc
    if not 2 <= len(encoded.encode("utf-8")) <= maximum:
        raise MetadataSearchStoreError("metadata search JSON value is too large")
    return encoded


def _request_id(value: object) -> str:
    clean = str(value or "").strip()
    if not _REQUEST_ID_RE.fullmatch(clean):
        raise MetadataSearchStoreError("metadata search request id is invalid")
    return clean


def _fingerprint(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _FINGERPRINT_RE.fullmatch(clean):
        raise MetadataSearchStoreError("metadata search fingerprint is invalid")
    return clean


def _timestamp(value: object) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetadataSearchStoreError("metadata search timestamp is invalid") from exc
    if not 0 <= timestamp < float("inf"):
        raise MetadataSearchStoreError("metadata search timestamp is invalid")
    return timestamp


def _next_session_timestamp_locked(
    connection: sqlite3.Connection,
    candidate: float,
) -> float:
    row = connection.execute(
        "SELECT MAX(created_at) AS max_created_at, "
        "MAX(updated_at) AS max_updated_at FROM metadata_search_sessions"
    ).fetchone()
    if row is None:
        return candidate
    retained = [
        float(value)
        for value in (row["max_created_at"], row["max_updated_at"])
        if value is not None
    ]
    if not retained:
        return candidate
    next_retained = math.nextafter(max(retained), math.inf)
    if not next_retained < math.inf:
        raise MetadataSearchStoreError("metadata search timestamp is invalid")
    return max(candidate, next_retained)


def _bounded_integer(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool):
        raise MetadataSearchStoreError(f"metadata search {label} is invalid")
    try:
        clean = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetadataSearchStoreError(f"metadata search {label} is invalid") from exc
    if not minimum <= clean <= maximum:
        raise MetadataSearchStoreError(f"metadata search {label} is invalid")
    return clean


def metadata_search_schema_ready(
    database_path: Path | str,
    *,
    timeout_seconds: float = 1.0,
) -> bool:
    path = Path(database_path)
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or not 0.05 <= timeout <= 5.0
    ):
        return False
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=timeout,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations WHERE component = ?",
                (METADATA_SEARCH_SCHEMA_COMPONENT,),
            ).fetchone()
            if row != (METADATA_SEARCH_SCHEMA_VERSION,):
                return False
            _verify_schema(connection)
            return True
        finally:
            connection.close()
    except (MigrationError, OSError, TypeError, ValueError, sqlite3.Error):
        return False


__all__ = [
    "DEFAULT_MAX_EVENT_BYTES_PER_SESSION",
    "METADATA_SEARCH_SCHEMA_COMPONENT",
    "METADATA_SEARCH_SCHEMA_VERSION",
    "MetadataSearchAppendResult",
    "MetadataSearchConflictError",
    "MetadataSearchNotFoundError",
    "MetadataSearchStoreError",
    "SQLiteMetadataSearchStore",
    "metadata_search_schema_ready",
]
