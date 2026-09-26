from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..core.migrations import MigrationError, SQLiteMigration, migrate_sqlite, require_columns
from ..config.runtime_config import runtime_config_path


SCHEMA_COMPONENT = "magnet_selection"
SCHEMA_VERSION = 1

RUN_STATES = frozenset({
    "running",
    "cleaning",
    "complete",
    "failed",
    "cancelled",
    "inconclusive",
})
CANDIDATE_STATES = frozenset({
    "pending",
    "submitted",
    "observing",
    "observed",
    "unavailable",
    "deferred",
    "selected",
    "discarded",
})
TERMINAL_CANDIDATE_STATES = frozenset({
    "observed",
    "unavailable",
    "selected",
    "discarded",
})
ORIGINS = frozenset({"preexisting", "temporary"})


class SelectionLedgerError(RuntimeError):
    pass


class SelectionLedgerStore:
    """Durable state for smart-selection candidates.

    Magnet URIs are deliberately absent. Tracker query parameters may contain
    credentials, while the ledger only needs a hash and safe observation data.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else (
            runtime_config_path().parent / "qb-smart-selections.sqlite3"
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._initialized = False

    def create(
        self,
        selection_id: str,
        *,
        created_at: int,
        category: str,
        tag: str,
        save_path: str,
        candidates: Sequence[Mapping[str, object]],
    ) -> None:
        clean_id = _required_text(selection_id, "selection id")
        if not candidates:
            raise SelectionLedgerError("smart selection ledger needs candidates")
        now = _timestamp(self._clock())
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO selection_runs "
                    "(selection_id, created_at, updated_at, category, tag, save_path, status, selected_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'running', NULL)",
                    (clean_id, int(created_at), now, category, tag, save_path),
                )
                for ordinal, candidate in enumerate(candidates):
                    info_hash = _required_hash(candidate.get("info_hash"))
                    origin = str(candidate.get("origin") or "temporary")
                    if origin not in ORIGINS:
                        raise SelectionLedgerError("smart selection candidate origin is invalid")
                    display_name = _optional_text(candidate.get("display_name"))
                    connection.execute(
                        "INSERT INTO selection_candidates "
                        "(selection_id, ordinal, info_hash, display_name, origin, state, batch_no, "
                        "qb_state, attempted, metadata_received, seeders, connected_seeders, "
                        "leechers, availability, download_speed, peak_download_speed, total_size, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)",
                        (clean_id, ordinal, info_hash, display_name, origin, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise SelectionLedgerError("smart selection ledger identity already exists") from exc

    def begin_batch(
        self,
        selection_id: str,
        hashes: Sequence[str],
        *,
        batch_no: int,
    ) -> None:
        clean_id = _required_text(selection_id, "selection id")
        clean_hashes = tuple(_required_hash(value) for value in hashes)
        if not clean_hashes:
            return
        now = _timestamp(self._clock())
        with self._transaction() as connection:
            for info_hash in clean_hashes:
                connection.execute(
                    "UPDATE selection_candidates SET state = 'submitted', batch_no = ?, "
                    "qb_state = NULL, updated_at = ? WHERE selection_id = ? AND info_hash = ?",
                    (int(batch_no), now, clean_id, info_hash),
                )
            connection.execute(
                "UPDATE selection_runs SET status = 'running', updated_at = ? WHERE selection_id = ?",
                (now, clean_id),
            )

    def record_snapshots(
        self,
        selection_id: str,
        snapshots: Mapping[str, Mapping[str, object]],
    ) -> None:
        clean_id = _required_text(selection_id, "selection id")
        if not snapshots:
            return
        now = _timestamp(self._clock())
        with self._transaction() as connection:
            for raw_hash, snapshot in snapshots.items():
                info_hash = _required_hash(raw_hash)
                row = connection.execute(
                    "SELECT attempted, metadata_received, state, seeders, connected_seeders, "
                    "leechers, availability, download_speed, peak_download_speed, total_size "
                    "FROM selection_candidates "
                    "WHERE selection_id = ? AND info_hash = ?",
                    (clean_id, info_hash),
                ).fetchone()
                if row is None:
                    continue
                qb_state = _optional_text(snapshot.get("state"))
                metadata_received = bool(snapshot.get("_metadata_received"))
                attempted = (
                    bool(row[0])
                    or bool(snapshot.get("attempted"))
                    or _state_was_attempted(qb_state)
                    or (metadata_received and not _state_is_deferred(qb_state))
                )
                current_state = str(row[2] or "pending")
                if attempted:
                    next_state = "observing"
                elif current_state in {"pending", "submitted"}:
                    next_state = "submitted"
                else:
                    next_state = current_state
                connection.execute(
                    "UPDATE selection_candidates SET state = ?, qb_state = ?, attempted = ?, "
                    "metadata_received = ?, seeders = ?, connected_seeders = ?, leechers = ?, "
                    "availability = ?, download_speed = ?, peak_download_speed = ?, total_size = ?, "
                    "display_name = COALESCE(?, display_name), updated_at = ? "
                    "WHERE selection_id = ? AND info_hash = ?",
                    (
                        next_state,
                        qb_state,
                        int(attempted),
                        int(bool(row[1]) or metadata_received),
                        _maximum_optional(row[3], snapshot.get("seeders"), parser=_optional_int),
                        _maximum_optional(row[4], snapshot.get("connected_seeders"), parser=_optional_int),
                        _maximum_optional(row[5], snapshot.get("leechers"), parser=_optional_int),
                        _maximum_optional(row[6], snapshot.get("availability"), parser=_optional_float),
                        _maximum_optional(row[7], snapshot.get("download_speed"), parser=_optional_int),
                        _maximum_optional(
                            row[8],
                            snapshot.get("peak_download_speed"),
                            snapshot.get("download_speed"),
                            parser=_optional_int,
                        ),
                        _maximum_optional(row[9], snapshot.get("total_size"), parser=_optional_int),
                        _optional_text(snapshot.get("name")),
                        now,
                        clean_id,
                        info_hash,
                    ),
                )
            connection.execute(
                "UPDATE selection_runs SET updated_at = ? WHERE selection_id = ?",
                (now, clean_id),
            )

    def mark_candidates(
        self,
        selection_id: str,
        states: Mapping[str, str],
    ) -> None:
        clean_id = _required_text(selection_id, "selection id")
        if not states:
            return
        now = _timestamp(self._clock())
        with self._transaction() as connection:
            for raw_hash, state in states.items():
                info_hash = _required_hash(raw_hash)
                clean_state = str(state or "").strip().lower()
                if clean_state not in CANDIDATE_STATES:
                    raise SelectionLedgerError("smart selection candidate state is invalid")
                connection.execute(
                    "UPDATE selection_candidates SET state = ?, updated_at = ? "
                    "WHERE selection_id = ? AND info_hash = ?",
                    (clean_state, now, clean_id, info_hash),
                )
            connection.execute(
                "UPDATE selection_runs SET updated_at = ? WHERE selection_id = ?",
                (now, clean_id),
            )

    def mark_selection(
        self,
        selection_id: str,
        *,
        status: str,
        selected_hash: str | None = None,
    ) -> None:
        clean_id = _required_text(selection_id, "selection id")
        clean_status = str(status or "").strip().lower()
        if clean_status not in RUN_STATES:
            raise SelectionLedgerError("smart selection run state is invalid")
        clean_selected = None if selected_hash is None else _required_hash(selected_hash)
        now = _timestamp(self._clock())
        with self._transaction() as connection:
            connection.execute(
                "UPDATE selection_runs SET status = ?, selected_hash = ?, updated_at = ? "
                "WHERE selection_id = ?",
                (clean_status, clean_selected, now, clean_id),
            )

    def records(self, selection_id: str) -> dict[str, dict[str, object]]:
        clean_id = _required_text(selection_id, "selection id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT ordinal, info_hash, display_name, origin, state, batch_no, qb_state, "
                "attempted, metadata_received, seeders, connected_seeders, leechers, availability, "
                "download_speed, peak_download_speed, total_size, updated_at "
                "FROM selection_candidates WHERE selection_id = ? ORDER BY ordinal",
                (clean_id,),
            ).fetchall()
        return {
            str(row[1]): {
                "ordinal": int(row[0]),
                "info_hash": str(row[1]),
                "name": row[2],
                "origin": str(row[3]),
                "ledger_state": str(row[4]),
                "batch_no": row[5],
                "state": row[6],
                "attempted": bool(row[7]),
                "_metadata_received": bool(row[8]),
                "seeders": row[9],
                "connected_seeders": row[10],
                "leechers": row[11],
                "availability": row[12],
                "download_speed": row[13],
                "peak_download_speed": row[14],
                "total_size": row[15],
                "updated_at": row[16],
            }
            for row in rows
        }

    def run(self, selection_id: str) -> dict[str, object] | None:
        clean_id = _required_text(selection_id, "selection id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT selection_id, created_at, updated_at, category, tag, save_path, status, selected_hash "
                "FROM selection_runs WHERE selection_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "selection_id": str(row[0]),
            "created_at": int(row[1]),
            "updated_at": float(row[2]),
            "category": str(row[3]),
            "tag": str(row[4]),
            "save_path": str(row[5]),
            "status": str(row[6]),
            "selected_hash": row[7],
        }

    def stale_runs(self, *, older_than: float) -> list[dict[str, object]]:
        cutoff = float(older_than)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT selection_id, created_at, updated_at, category, tag, save_path, status, selected_hash "
                "FROM selection_runs WHERE updated_at <= ? ORDER BY updated_at",
                (cutoff,),
            ).fetchall()
        return [
            {
                "selection_id": str(row[0]),
                "created_at": int(row[1]),
                "updated_at": float(row[2]),
                "category": str(row[3]),
                "tag": str(row[4]),
                "save_path": str(row[5]),
                "status": str(row[6]),
                "selected_hash": row[7],
            }
            for row in rows
        ]

    def remove(self, selection_id: str) -> None:
        clean_id = _required_text(selection_id, "selection id")
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM selection_runs WHERE selection_id = ?",
                (clean_id,),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._ensure_initialized()
        connection = sqlite3.connect(self.path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _ensure_initialized(self) -> None:
        with self._lock:
            if self._initialized:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA busy_timeout = 30000")
                    migrate_sqlite(
                        connection,
                        component=SCHEMA_COMPONENT,
                        current_version=SCHEMA_VERSION,
                        migrations=(SQLiteMigration(1, _create_schema),),
                        clock=self._clock,
                        verify_current=_verify_schema,
                    )
                finally:
                    connection.close()
            except (OSError, sqlite3.Error, MigrationError) as exc:
                raise SelectionLedgerError("smart selection ledger is unavailable") from exc
            self._initialized = True


def _create_schema(connection: sqlite3.Connection) -> None:
    run_states = ", ".join(f"'{value}'" for value in sorted(RUN_STATES))
    candidate_states = ", ".join(f"'{value}'" for value in sorted(CANDIDATE_STATES))
    origins = ", ".join(f"'{value}'" for value in sorted(ORIGINS))
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS selection_runs (
            selection_id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL CHECK (created_at > 0),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),
            category TEXT NOT NULL,
            tag TEXT NOT NULL,
            save_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({run_states})),
            selected_hash TEXT
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS selection_candidates (
            selection_id TEXT NOT NULL REFERENCES selection_runs(selection_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            info_hash TEXT NOT NULL,
            display_name TEXT,
            origin TEXT NOT NULL CHECK (origin IN ({origins})),
            state TEXT NOT NULL CHECK (state IN ({candidate_states})),
            batch_no INTEGER CHECK (batch_no IS NULL OR batch_no > 0),
            qb_state TEXT,
            attempted INTEGER NOT NULL CHECK (attempted IN (0, 1)),
            metadata_received INTEGER NOT NULL CHECK (metadata_received IN (0, 1)),
            seeders INTEGER CHECK (seeders IS NULL OR seeders >= 0),
            connected_seeders INTEGER CHECK (connected_seeders IS NULL OR connected_seeders >= 0),
            leechers INTEGER CHECK (leechers IS NULL OR leechers >= 0),
            availability REAL CHECK (availability IS NULL OR availability >= 0),
            download_speed INTEGER CHECK (download_speed IS NULL OR download_speed >= 0),
            peak_download_speed INTEGER CHECK (peak_download_speed IS NULL OR peak_download_speed >= 0),
            total_size INTEGER CHECK (total_size IS NULL OR total_size >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),
            PRIMARY KEY (selection_id, info_hash),
            UNIQUE (selection_id, ordinal)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS selection_candidates_state "
        "ON selection_candidates(selection_id, state)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS selection_runs_updated "
        "ON selection_runs(updated_at)"
    )


def _verify_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "selection_runs",
        ("selection_id", "created_at", "updated_at", "category", "tag", "save_path", "status", "selected_hash"),
    )
    require_columns(
        connection,
        "selection_candidates",
        (
            "selection_id",
            "ordinal",
            "info_hash",
            "display_name",
            "origin",
            "state",
            "batch_no",
            "qb_state",
            "attempted",
            "metadata_received",
            "seeders",
            "connected_seeders",
            "leechers",
            "availability",
            "download_speed",
            "peak_download_speed",
            "total_size",
            "updated_at",
        ),
    )


def _required_text(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise SelectionLedgerError(f"smart selection {label} is invalid")
    return clean


def _required_hash(value: object) -> str:
    clean = str(value or "").strip().lower()
    if len(clean) != 40 or any(character not in "0123456789abcdef" for character in clean):
        raise SelectionLedgerError("smart selection candidate hash is invalid")
    return clean


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _timestamp(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = 0.0
    return max(0.0, parsed)


def _maximum_optional(
    *values: object,
    parser: Callable[[object], int | float | None],
) -> int | float | None:
    parsed = [candidate for value in values if (candidate := parser(value)) is not None]
    return max(parsed) if parsed else None


def _state_was_attempted(state: str | None) -> bool:
    return str(state or "") in {
        "metaDL",
        "forcedMetaDL",
        "downloadingMetadata",
        "downloading",
        "forcedDL",
        "stalledDL",
        "stalledUP",
        "allocating",
        "checkingDL",
        "checkingUP",
        "checkingResumeData",
        "moving",
        "error",
        "missingFiles",
    }


def _state_is_deferred(state: str | None) -> bool:
    return str(state or "") in {
        "queuedDL",
        "queuedUP",
        "pausedDL",
        "pausedUP",
        "stoppedDL",
        "stoppedUP",
        "unknown",
    }
