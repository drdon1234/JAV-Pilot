from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.catalog_code import normalize_catalog_code
from ..core.migrations import (
    MigrationError,
    SQLiteMigration,
    SchemaTooNewError,
    migrate_sqlite,
    require_columns,
)


SCHEMA_COMPONENT = "download_replacements"
SCHEMA_VERSION = 6
SOURCE_KINDS = ("qb", "web_job", "web_intent")
TARGET_KINDS = ("qb", "web_job")
STATUSES = (
    "open",
    "submitting",
    "replacement_created",
    "completed",
    "cleanup_failed",
    "discarding",
    "discarded",
    "expired",
)
ACTIVE_STATUSES = (
    "open",
    "submitting",
    "replacement_created",
    "cleanup_failed",
    "discarding",
)
DISCOVERY_STATUSES = (
    "idle",
    "queued",
    "running",
    "available",
    "not_found",
    "inconclusive",
)
CHANNEL_STATUSES = ("pending", "available", "not_found", "unavailable")
RECOVERY_MODES = ("idle", "smart_magnet", "web", "manual_magnet")
MAGNET_RECOVERY_MODES = frozenset({"smart_magnet", "manual_magnet"})
SMART_SELECTION_OUTCOMES = (
    "running",
    "selected",
    "not_found",
    "inconclusive",
    "cancelled",
    "failed",
)
SMART_SELECTION_CLEANUP_STATUSES = (
    "pending",
    "complete",
    "not_required",
    "incomplete",
    "unknown",
)
DISPOSITIONS = ("archive", "delete")
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_DISCOVERY_LEASE_SECONDS = 5 * 60.0
MAX_CLEANUP_ERROR_BYTES = 240
MAX_DISCOVERY_ERROR_CODE_BYTES = 64
MAX_WEB_PROVIDERS = 3

_REPLACEMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_QB_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,79}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CLEANUP_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DISCOVERY_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WEB_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_DISPOSABLE_FAILURE_WHERE = (
    "status = 'open' AND ("
    "(magnet_status = 'not_found' AND web_status = 'not_found') OR "
    "(source_kind IN ('web_job', 'web_intent') "
    "AND recovery_mode = 'smart_magnet' "
    "AND smart_selection_outcome IN ('not_found', 'inconclusive') "
    "AND smart_selection_cleanup_status IN ('complete', 'not_required')))"
)


class DownloadReplacementError(RuntimeError):
    pass


class DownloadReplacementNotFoundError(DownloadReplacementError):
    pass


class DownloadReplacementConflictError(DownloadReplacementError):
    pass


class DownloadReplacementStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise DownloadReplacementError(
                "download replacement database path must be absolute"
            )
        self._clock = clock
        self._id_factory = id_factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_or_reuse(
        self,
        *,
        source_kind: object,
        source_id: object,
        source_revision: object,
        code: object,
        ttl_seconds: object = DEFAULT_TTL_SECONDS,
    ) -> dict[str, object]:
        clean_kind = _validate_source_kind(source_kind)
        clean_source_id = _validate_source_id(clean_kind, source_id)
        clean_revision = _validate_revision(source_revision)
        display_code, code_key = _normalize_code(code)
        ttl = _validate_ttl(ttl_seconds)
        now = _timestamp(self._clock())
        expires_at = now + ttl

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            row = connection.execute(
                "SELECT * FROM download_replacements "
                "WHERE source_kind = ? AND source_id = ? "
                "AND status IN ('open', 'submitting', 'replacement_created', "
                "'cleanup_failed', 'discarding') "
                "ORDER BY created_at DESC LIMIT 1",
                (clean_kind, clean_source_id),
            ).fetchone()
            if row is not None:
                if str(row["code_key"]) != code_key:
                    connection.rollback()
                    raise DownloadReplacementConflictError(
                        "download failure changed before replacement selection"
                    )
                if str(row["source_revision"]) == clean_revision:
                    connection.commit()
                    return _row_to_replacement(row)

                changed = connection.execute(
                    "UPDATE download_replacements SET status = 'expired', "
                    "lease_token = NULL, lease_expires_at = NULL, "
                    "discovery_lease_token = NULL, "
                    "discovery_lease_expires_at = NULL, updated_at = ? "
                    "WHERE replacement_id = ? AND status = 'open' "
                    "AND idempotency_key_hash IS NULL AND target_kind IS NULL "
                    "AND target_id IS NULL AND lease_token IS NULL "
                    "AND lease_expires_at IS NULL",
                    (now, str(row["replacement_id"])),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    raise DownloadReplacementConflictError(
                        "download failure changed before replacement selection"
                    )

            replacement_id = _validate_replacement_id(self._id_factory())
            try:
                connection.execute(
                    """
                    INSERT INTO download_replacements (
                        replacement_id, source_kind, source_id, source_revision,
                        code, code_key, status, idempotency_key_hash,
                        target_kind, target_id, lease_token, lease_expires_at,
                        cleanup_error, discovery_status, magnet_status,
                        magnet_count, web_status, web_providers_json,
                        web_variant, magnet_error_code, web_error_code,
                        discovery_started_at, discovery_finished_at,
                        discovery_lease_token, discovery_lease_expires_at,
                        recovery_mode, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, NULL, NULL, NULL,
                              NULL, NULL, 'idle', 'pending', 0, 'pending', '[]',
                              NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'idle',
                              ?, ?, ?)
                    """,
                    (
                        replacement_id,
                        clean_kind,
                        clean_source_id,
                        clean_revision,
                        display_code,
                        code_key,
                        now,
                        now,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement already exists"
                ) from exc
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (replacement_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError(
                "download replacement could not be persisted"
            )
        return _row_to_replacement(row)

    def get(self, replacement_id: object) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            due = connection.execute(
                "SELECT 1 FROM download_replacements WHERE "
                "(status = 'submitting' AND lease_expires_at <= ?) OR "
                "(status IN ('open', 'submitting', 'replacement_created', "
                "'cleanup_failed', 'discarding') AND expires_at <= ?) OR "
                "(discovery_status = 'running' AND discovery_lease_expires_at <= ?) "
                "LIMIT 1",
                (now, now, now),
            ).fetchone()
            if due is not None:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_locked(connection, now)
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            if connection.in_transaction:
                connection.commit()
        if row is None:
            raise DownloadReplacementNotFoundError("download replacement was not found")
        return _row_to_replacement(row)

    def begin_submission(
        self,
        replacement_id: object,
        *,
        idempotency_key: object,
        code: object,
        target_kind: object | None = None,
        target_id: object | None = None,
        target_info_hash: object | None = None,
        lease_seconds: object = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_key = _validate_idempotency_key(idempotency_key)
        if not hmac.compare_digest(clean_key, clean_id):
            raise DownloadReplacementConflictError(
                "download replacement idempotency key does not match"
            )
        key_hash = _idempotency_hash(clean_key)
        if target_info_hash is not None:
            if target_kind is not None or target_id is not None:
                raise DownloadReplacementError(
                    "download replacement target identity is ambiguous"
                )
            target_kind = "qb"
            target_id = target_info_hash
        clean_target_kind = _validate_target_kind(target_kind)
        clean_target_id = _validate_target_id(clean_target_kind, target_id)
        _display_code, code_key = _normalize_code(code)
        lease_duration = _validate_lease(lease_seconds)
        now = _timestamp(self._clock())

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise DownloadReplacementNotFoundError(
                    "download replacement was not found"
                )
            status = str(row["status"])
            if status == "expired":
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement has expired"
                )
            if str(row["code_key"]) != code_key:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "replacement download catalog code does not match"
                )
            if (
                status == "open"
                and str(row["smart_selection_outcome"] or "") == "running"
            ):
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "smart selection is already running"
                )
            if (
                str(row["source_kind"]) == "qb"
                and clean_target_kind == "qb"
                and str(row["source_id"]) == clean_target_id
            ) or (
                str(row["source_kind"]) == "web_job"
                and clean_target_kind == "web_job"
                and str(row["source_id"]) == clean_target_id
            ):
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "replacement target must differ from the failed download"
                )

            persisted_key = row["idempotency_key_hash"]
            persisted_target_kind = row["target_kind"]
            persisted_target_id = row["target_id"]
            if persisted_key is not None and str(persisted_key) != key_hash:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement idempotency key was already used"
                )
            if (
                persisted_target_kind is not None
                and (
                    str(persisted_target_kind) != clean_target_kind
                    or str(persisted_target_id) != clean_target_id
                )
            ):
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement already targets another download"
                )

            if status == "completed":
                connection.commit()
                return {
                    **_row_to_replacement(row),
                    "submission_phase": "complete",
                    "lease_token": None,
                }
            if status in {"replacement_created", "cleanup_failed"}:
                lease_expires_at = float(row["lease_expires_at"] or 0)
                if lease_expires_at > now:
                    connection.rollback()
                    raise DownloadReplacementConflictError(
                        "download replacement cleanup is already in progress"
                    )
                lease_token = secrets.token_hex(16)
                try:
                    changed = connection.execute(
                        "UPDATE download_replacements SET lease_token = ?, "
                        "lease_expires_at = ?, updated_at = ? "
                        "WHERE replacement_id = ? "
                        "AND status IN ('replacement_created', 'cleanup_failed') "
                        "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                        (
                            lease_token,
                            now + lease_duration,
                            now,
                            clean_id,
                            now,
                        ),
                    ).rowcount
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise DownloadReplacementConflictError(
                        "download replacement idempotency key was already used"
                    ) from exc
                if changed != 1:
                    connection.rollback()
                    raise DownloadReplacementConflictError(
                        "download replacement cleanup is already in progress"
                    )
                row = connection.execute(
                    "SELECT * FROM download_replacements WHERE replacement_id = ?",
                    (clean_id,),
                ).fetchone()
                connection.commit()
                if row is None:
                    raise DownloadReplacementError(
                        "download replacement cleanup could not be claimed"
                    )
                return {
                    **_row_to_replacement(row),
                    "submission_phase": "cleanup",
                    "lease_token": lease_token,
                }
            if status == "submitting":
                lease_expires_at = float(row["lease_expires_at"] or 0)
                if lease_expires_at > now:
                    connection.rollback()
                    raise DownloadReplacementConflictError(
                        "download replacement submission is already in progress"
                    )

            lease_token = secrets.token_hex(16)
            try:
                changed = connection.execute(
                    "UPDATE download_replacements SET status = 'submitting', "
                    "idempotency_key_hash = ?, target_kind = ?, target_id = ?, "
                    "lease_token = ?, lease_expires_at = ?, cleanup_error = NULL, "
                    "updated_at = ? "
                    "WHERE replacement_id = ? AND status IN ('open', 'submitting')",
                    (
                        key_hash,
                        clean_target_kind,
                        clean_target_id,
                        lease_token,
                        now + lease_duration,
                        now,
                        clean_id,
                    ),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement idempotency key was already used"
                ) from exc
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement changed before submission"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("download replacement could not be claimed")
        return {
            **_row_to_replacement(row),
            "submission_phase": "submit",
            "lease_token": lease_token,
        }

    def mark_target_created(
        self,
        replacement_id: object,
        lease_token: object,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_lease = _validate_replacement_id(lease_token)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET status = 'replacement_created', "
                "updated_at = ? "
                "WHERE replacement_id = ? AND status = 'submitting' "
                "AND lease_token = ?",
                (now, clean_id, clean_lease),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement submission lease is no longer valid"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("download replacement could not be updated")
        return _row_to_replacement(row)

    def release_submission(
        self,
        replacement_id: object,
        lease_token: object,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_lease = _validate_replacement_id(lease_token)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET status = 'open', "
                "idempotency_key_hash = NULL, target_kind = NULL, target_id = NULL, "
                "lease_token = NULL, lease_expires_at = NULL, cleanup_error = NULL, "
                "updated_at = ? WHERE replacement_id = ? AND status = 'submitting' "
                "AND lease_token = ?",
                (now, clean_id, clean_lease),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement submission lease is no longer valid"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("download replacement could not be released")
        return _row_to_replacement(row)

    def queue_discovery(
        self,
        replacement_id: object,
        *,
        mode: object,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_mode = _validate_recovery_mode(mode, allow_idle=False)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise DownloadReplacementNotFoundError(
                    "download replacement was not found"
                )
            if str(row["status"]) != "open":
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement is no longer open"
                )
            if str(row["smart_selection_outcome"] or "") == "running":
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "smart selection is already running"
                )
            discovery_status = str(row["discovery_status"])
            current_mode = _validate_recovery_mode(row["recovery_mode"])
            lease_expires_at = float(row["discovery_lease_expires_at"] or 0)
            if discovery_status == "queued" and current_mode == clean_mode:
                connection.commit()
                return _row_to_replacement(row)
            if discovery_status == "running" and lease_expires_at > now:
                if current_mode == clean_mode:
                    connection.commit()
                    return _row_to_replacement(row)
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "another download recovery action is already running"
                )
            if clean_mode in MAGNET_RECOVERY_MODES:
                connection.execute(
                    "UPDATE download_replacements SET recovery_mode = ?, "
                    "discovery_status = 'queued', magnet_status = 'pending', "
                    "magnet_count = 0, magnet_json = '[]', magnet_error_code = NULL, "
                    "smart_selection_id = NULL, smart_selection_outcome = NULL, "
                    "smart_selection_cleanup_status = NULL, "
                    "smart_selection_finished_at = NULL, disposition = NULL, "
                    "discovery_started_at = NULL, discovery_finished_at = NULL, "
                    "discovery_lease_token = NULL, discovery_lease_expires_at = NULL, "
                    "updated_at = ? WHERE replacement_id = ?",
                    (clean_mode, now, clean_id),
                )
            else:
                connection.execute(
                    "UPDATE download_replacements SET recovery_mode = ?, "
                    "discovery_status = 'queued', web_status = 'pending', "
                    "web_providers_json = '[]', web_variant = NULL, "
                    "web_error_code = NULL, smart_selection_id = NULL, "
                    "smart_selection_outcome = NULL, "
                    "smart_selection_cleanup_status = NULL, "
                    "smart_selection_finished_at = NULL, disposition = NULL, "
                    "discovery_started_at = NULL, "
                    "discovery_finished_at = NULL, discovery_lease_token = NULL, "
                    "discovery_lease_expires_at = NULL, updated_at = ? "
                    "WHERE replacement_id = ?",
                    (clean_mode, now, clean_id),
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("download replacement could not be queued")
        return _row_to_replacement(row)

    def begin_smart_selection(
        self,
        replacement_id: object,
        *,
        selection_id: object,
        expected_source_revision: object,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_selection_id = _validate_replacement_id(selection_id)
        clean_revision = _validate_revision(expected_source_revision)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET smart_selection_id = ?, "
                "smart_selection_outcome = 'running', "
                "smart_selection_cleanup_status = 'pending', "
                "smart_selection_finished_at = NULL, disposition = NULL, "
                "updated_at = ? WHERE replacement_id = ? AND status = 'open' "
                "AND source_revision = ? AND recovery_mode = 'smart_magnet' "
                "AND discovery_status = 'available' "
                "AND magnet_status = 'available'",
                (clean_selection_id, now, clean_id, clean_revision),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement changed before smart selection"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("smart selection could not be persisted")
        return _row_to_replacement(row)

    def finish_smart_selection(
        self,
        replacement_id: object,
        *,
        selection_id: object,
        expected_source_revision: object,
        outcome: object,
        cleanup_status: object,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_selection_id = _validate_replacement_id(selection_id)
        clean_revision = _validate_revision(expected_source_revision)
        clean_outcome = _validate_smart_selection_outcome(
            outcome, allow_running=False
        )
        clean_cleanup = _validate_smart_selection_cleanup_status(cleanup_status)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET smart_selection_outcome = ?, "
                "smart_selection_cleanup_status = ?, "
                "smart_selection_finished_at = ?, updated_at = ? "
                "WHERE replacement_id = ? AND status = 'open' "
                "AND source_revision = ? AND recovery_mode = 'smart_magnet' "
                "AND smart_selection_id = ?",
                (
                    clean_outcome,
                    clean_cleanup,
                    now,
                    now,
                    clean_id,
                    clean_revision,
                    clean_selection_id,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement changed before smart selection completed"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError(
                "smart selection outcome could not be persisted"
            )
        return _row_to_replacement(row)

    def recover_discoveries(self) -> int:
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET discovery_status = 'queued', "
                "discovery_lease_token = NULL, discovery_lease_expires_at = NULL, "
                "updated_at = ? WHERE status = 'open' AND discovery_status = 'running'",
                (now,),
            ).rowcount
            connection.commit()
        return int(changed)

    def pending_discovery_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        try:
            clean_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError, OverflowError) as exc:
            raise DownloadReplacementError("discovery queue limit is invalid") from exc
        now = _timestamp(self._clock())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT replacement_id FROM download_replacements "
                "WHERE status = 'open' AND "
                "(discovery_status = 'queued' OR "
                "(discovery_status = 'running' AND discovery_lease_expires_at <= ?)) "
                "ORDER BY updated_at ASC LIMIT ?",
                (now, clean_limit),
            ).fetchall()
        return tuple(str(row["replacement_id"]) for row in rows)

    def claim_discovery(
        self,
        replacement_id: object,
        *,
        lease_seconds: object = DEFAULT_DISCOVERY_LEASE_SECONDS,
    ) -> dict[str, object] | None:
        clean_id = _validate_replacement_id(replacement_id)
        lease_duration = _validate_lease(lease_seconds)
        now = _timestamp(self._clock())
        lease_token = secrets.token_hex(16)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET discovery_status = 'running', "
                "discovery_started_at = ?, discovery_finished_at = NULL, "
                "discovery_lease_token = ?, discovery_lease_expires_at = ?, "
                "updated_at = ? WHERE replacement_id = ? AND status = 'open' AND "
                "(discovery_status = 'queued' OR "
                "(discovery_status = 'running' AND discovery_lease_expires_at <= ?))",
                (
                    now,
                    lease_token,
                    now + lease_duration,
                    now,
                    clean_id,
                    now,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            return None
        result = _row_to_replacement(row)
        result["discovery_lease_token"] = lease_token
        return result

    def finish_magnet_discovery(
        self,
        replacement_id: object,
        lease_token: object,
        *,
        magnet_status: object,
        magnet_count: object,
        magnets: object = (),
        magnet_error_code: object | None,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_lease = _validate_replacement_id(lease_token)
        clean_magnet_status = _validate_channel_status(magnet_status)
        try:
            clean_count = int(magnet_count)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DownloadReplacementError("magnet discovery count is invalid") from exc
        if clean_count < 0 or clean_count > 999:
            raise DownloadReplacementError("magnet discovery count is invalid")
        clean_magnet_error = _validate_discovery_error_code(magnet_error_code)
        clean_magnets = _validate_magnet_snapshots(magnets)
        if clean_magnet_status == "available" and (
            clean_count < 1 or not clean_magnets
        ):
            raise DownloadReplacementError("available magnet discovery has no magnets")
        if clean_magnet_status != "available":
            clean_count = 0
            clean_magnets = ()
        overall = _channel_discovery_status(clean_magnet_status)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET discovery_status = ?, "
                "magnet_status = ?, magnet_count = ?, magnet_json = ?, "
                "magnet_error_code = ?, discovery_finished_at = ?, "
                "discovery_lease_token = NULL, discovery_lease_expires_at = NULL, "
                "updated_at = ? WHERE replacement_id = ? AND status = 'open' "
                "AND recovery_mode IN ('smart_magnet', 'manual_magnet') "
                "AND discovery_status = 'running' AND discovery_lease_token = ?",
                (
                    overall,
                    clean_magnet_status,
                    clean_count,
                    json.dumps(clean_magnets, ensure_ascii=True, separators=(",", ":")),
                    clean_magnet_error,
                    now,
                    now,
                    clean_id,
                    clean_lease,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement discovery lease is no longer valid"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("download discovery could not be persisted")
        return _row_to_replacement(row)

    def finish_web_discovery(
        self,
        replacement_id: object,
        lease_token: object,
        *,
        web_status: object,
        web_provider_ids: object,
        web_variant: object | None,
        web_error_code: object | None,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_lease = _validate_replacement_id(lease_token)
        clean_web_status = _validate_channel_status(web_status)
        clean_providers = _validate_web_provider_ids(web_provider_ids)
        clean_variant = _validate_variant(web_variant)
        clean_web_error = _validate_discovery_error_code(web_error_code)
        if clean_web_status == "available" and not clean_providers:
            raise DownloadReplacementError("available Web discovery has no provider")
        if clean_web_status != "available":
            clean_providers = ()
            clean_variant = None
        overall = _channel_discovery_status(clean_web_status)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE download_replacements SET discovery_status = ?, "
                "web_status = ?, web_providers_json = ?, web_variant = ?, "
                "web_error_code = ?, discovery_finished_at = ?, "
                "discovery_lease_token = NULL, discovery_lease_expires_at = NULL, "
                "updated_at = ? WHERE replacement_id = ? AND status = 'open' "
                "AND recovery_mode = 'web' AND discovery_status = 'running' "
                "AND discovery_lease_token = ?",
                (
                    overall,
                    clean_web_status,
                    json.dumps(clean_providers, separators=(",", ":")),
                    clean_variant,
                    clean_web_error,
                    now,
                    now,
                    clean_id,
                    clean_lease,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement discovery lease is no longer valid"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError("download discovery could not be persisted")
        return _row_to_replacement(row)

    def get_for_sources(
        self,
        sources: Iterable[tuple[object, object]],
    ) -> dict[tuple[str, str], dict[str, object]]:
        normalized = []
        for kind, source_id in sources:
            clean_kind = _validate_source_kind(kind)
            normalized.append((clean_kind, _validate_source_id(clean_kind, source_id)))
        if not normalized:
            return {}
        clauses = " OR ".join("(source_kind = ? AND source_id = ?)" for _ in normalized)
        values: list[object] = [value for pair in normalized for value in pair]
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            rows = connection.execute(
                "SELECT * FROM download_replacements WHERE status IN "
                "('open', 'submitting', 'replacement_created', 'cleanup_failed', 'discarding') "
                f"AND ({clauses}) ORDER BY created_at DESC",
                values,
            ).fetchall()
            connection.commit()
        result: dict[tuple[str, str], dict[str, object]] = {}
        for row in rows:
            key = (str(row["source_kind"]), str(row["source_id"]))
            result.setdefault(key, _row_to_replacement(row))
        return result

    def count_no_sources(self) -> int:
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            row = connection.execute(
                "SELECT COUNT(*) FROM download_replacements WHERE status = 'open' "
                "AND magnet_status = 'not_found' AND web_status = 'not_found'"
            ).fetchone()
            connection.commit()
        return int(row[0] if row is not None else 0)

    def disposable_failure_snapshot(
        self,
        *,
        no_sources_only: bool = False,
    ) -> tuple[dict[str, object], ...]:
        if type(no_sources_only) is not bool:
            raise DownloadReplacementError(
                "download replacement disposition scope is invalid"
            )
        predicate = (
            "status = 'open' AND magnet_status = 'not_found' "
            "AND web_status = 'not_found'"
            if no_sources_only
            else _DISPOSABLE_FAILURE_WHERE
        )
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            rows = connection.execute(
                "SELECT replacement_id, code, source_kind "
                f"FROM download_replacements WHERE {predicate} "
                "ORDER BY created_at ASC, replacement_id ASC"
            ).fetchall()
            connection.commit()
        return tuple(
            {
                "replacement_id": _validate_replacement_id(row["replacement_id"]),
                "code": str(row["code"]),
                "source_kind": _validate_source_kind(row["source_kind"]),
            }
            for row in rows
        )

    def recover_interrupted_dispositions(self) -> int:
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._recover_interrupted_dispositions_locked(connection, now)
            connection.commit()
        return changed

    def recover_interrupted_smart_selections(self) -> int:
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._recover_interrupted_smart_selections_locked(
                connection,
                now,
            )
            connection.commit()
        return changed

    def begin_disposition(
        self,
        replacement_id: object,
        *,
        disposition: object,
    ) -> dict[str, object] | None:
        clean_id = _validate_replacement_id(replacement_id)
        clean_disposition = _validate_disposition(disposition)
        now = _timestamp(self._clock())
        lease_token = secrets.token_hex(16)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            changed = connection.execute(
                "UPDATE download_replacements SET status = 'discarding', "
                "disposition = COALESCE(disposition, ?), lease_token = ?, "
                "lease_expires_at = ?, expires_at = MAX(expires_at, ?), "
                "updated_at = ? WHERE replacement_id = ? "
                f"AND {_DISPOSABLE_FAILURE_WHERE}",
                (
                    clean_disposition,
                    lease_token,
                    now + DEFAULT_LEASE_SECONDS,
                    now + DEFAULT_TTL_SECONDS,
                    now,
                    clean_id,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            return None
        result = _row_to_replacement(row)
        result["lease_token"] = lease_token
        return result

    def finish_disposition(
        self,
        replacement_id: object,
        lease_token: object,
        *,
        removed: bool,
        error_code: object | None = None,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        clean_lease = _validate_replacement_id(lease_token)
        if type(removed) is not bool:
            raise DownloadReplacementError(
                "download replacement disposition result is invalid"
            )
        clean_error = None if removed else _validate_cleanup_error(error_code)
        now = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ? "
                "AND status = 'discarding' AND lease_token = ?",
                (clean_id, clean_lease),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement disposition lease is no longer valid"
                )
            result = _row_to_replacement(row)
            disposition = _validate_disposition(row["disposition"])
            if not removed:
                connection.execute(
                    "UPDATE download_replacements SET status = 'open', "
                    "cleanup_error = ?, lease_token = NULL, "
                    "lease_expires_at = NULL, expires_at = MAX(expires_at, ?), "
                    "updated_at = ? "
                    "WHERE replacement_id = ? AND status = 'discarding' "
                    "AND lease_token = ?",
                    (
                        clean_error,
                        now + DEFAULT_TTL_SECONDS,
                        now,
                        clean_id,
                        clean_lease,
                    ),
                )
                connection.commit()
                return {**result, "status": "open", "cleanup_error": clean_error}
            if disposition == "archive":
                connection.execute(
                    "INSERT INTO failed_download_archives "
                    "(code_key, code, archived_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(code_key) DO UPDATE SET "
                    "code = excluded.code, archived_at = excluded.archived_at",
                    (str(row["code_key"]), str(row["code"]), now),
                )
            changed = connection.execute(
                "DELETE FROM download_replacements WHERE replacement_id = ? "
                "AND status = 'discarding' AND lease_token = ?",
                (clean_id, clean_lease),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement disposition lease is no longer valid"
                )
            connection.commit()
        return {
            **result,
            "status": "discarded",
            "disposition": disposition,
            "cleanup_error": None,
        }

    def failed_archive_page(
        self, *, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        try:
            clean_limit = max(1, min(int(limit), 200))
            clean_offset = int(offset)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DownloadReplacementError(
                "failed download archive pagination is invalid"
            ) from exc
        if clean_offset < 0 or clean_offset > 10_000_000:
            raise DownloadReplacementError(
                "failed download archive pagination is invalid"
            )
        with self._connect() as connection:
            count_row = connection.execute(
                "SELECT COUNT(*) FROM failed_download_archives"
            ).fetchone()
            rows = connection.execute(
                "SELECT code, archived_at FROM failed_download_archives "
                "ORDER BY archived_at DESC, code_key ASC LIMIT ? OFFSET ?",
                (clean_limit, clean_offset),
            ).fetchall()
        count = int(count_row[0] if count_row is not None else 0)
        items = [
            {"code": str(row["code"]), "archived_at": float(row["archived_at"])}
            for row in rows
        ]
        return {
            "items": items,
            "count": count,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": clean_offset + len(items) < count,
        }

    def delete_failed_archive(self, code: object) -> bool:
        _display_code, code_key = _normalize_code(code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(
                "DELETE FROM failed_download_archives WHERE code_key = ?",
                (code_key,),
            ).rowcount
            connection.commit()
        return removed == 1

    def finish_cleanup(
        self,
        replacement_id: object,
        *,
        removed: object,
        cleanup_error: object | None = None,
        lease_token: object | None = None,
    ) -> dict[str, object]:
        clean_id = _validate_replacement_id(replacement_id)
        if type(removed) is not bool:
            raise DownloadReplacementError(
                "download replacement cleanup result is invalid"
            )
        clean_error = None if removed else _validate_cleanup_error(cleanup_error)
        now = _timestamp(self._clock())
        next_status = "completed" if removed else "cleanup_failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            if current is None:
                connection.rollback()
                raise DownloadReplacementNotFoundError(
                    "download replacement was not found"
                )
            if str(current["status"]) == "completed" and removed:
                connection.commit()
                return _row_to_replacement(current)
            clean_lease = _validate_replacement_id(lease_token)
            changed = connection.execute(
                "UPDATE download_replacements SET status = ?, cleanup_error = ?, "
                "lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE replacement_id = ? "
                "AND status IN ('replacement_created', 'cleanup_failed') "
                "AND lease_token = ?",
                (next_status, clean_error, now, clean_id, clean_lease),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DownloadReplacementConflictError(
                    "download replacement is not awaiting cleanup"
                )
            row = connection.execute(
                "SELECT * FROM download_replacements WHERE replacement_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise DownloadReplacementError(
                "download replacement cleanup could not be persisted"
            )
        return _row_to_replacement(row)

    def _expire_locked(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "UPDATE download_replacements SET status = 'open', "
            "lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
            "WHERE status = 'submitting' AND lease_expires_at <= ? "
            "AND expires_at > ?",
            (now, now, now),
        )
        connection.execute(
            "UPDATE download_replacements SET status = 'expired', "
            "lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
            "WHERE status IN ('open', 'submitting', 'replacement_created', "
            "'cleanup_failed') AND expires_at <= ?",
            (now, now),
        )

    def _recover_interrupted_dispositions_locked(
        self,
        connection: sqlite3.Connection,
        now: float,
    ) -> int:
        return int(connection.execute(
            "UPDATE download_replacements SET status = 'open', "
            "lease_token = NULL, lease_expires_at = NULL, "
            "expires_at = MAX(expires_at, ?), updated_at = ? "
            "WHERE status = 'discarding'",
            (now + DEFAULT_TTL_SECONDS, now),
        ).rowcount)

    def _recover_interrupted_smart_selections_locked(
        self,
        connection: sqlite3.Connection,
        now: float,
    ) -> int:
        return int(connection.execute(
            "UPDATE download_replacements SET smart_selection_outcome = 'failed', "
            "smart_selection_cleanup_status = 'unknown', "
            "smart_selection_finished_at = ?, updated_at = ? "
            "WHERE smart_selection_outcome = 'running'",
            (now, now),
        ).rowcount)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                migrate_sqlite(
                    connection,
                    component=SCHEMA_COMPONENT,
                    current_version=SCHEMA_VERSION,
                    migrations=(
                        SQLiteMigration(1, _migrate_v1),
                        SQLiteMigration(2, _migrate_v2),
                        SQLiteMigration(3, _migrate_v3),
                        SQLiteMigration(4, _migrate_v4),
                        SQLiteMigration(5, _migrate_v5),
                        SQLiteMigration(6, _migrate_v6),
                    ),
                    clock=self._clock,
                    verify_current=_verify_schema,
                )
                connection.execute("BEGIN IMMEDIATE")
                now = _timestamp(self._clock())
                self._expire_locked(connection, now)
                connection.commit()
            except SchemaTooNewError as exc:
                raise DownloadReplacementError(
                    "download replacement database schema is newer than this application supports"
                ) from exc
            except MigrationError as exc:
                raise DownloadReplacementError(str(exc)) from exc

    @contextmanager
    def _connect(self, *, timeout_seconds: float = 5.0) -> Iterator[sqlite3.Connection]:
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


def _migrate_v1(connection: sqlite3.Connection) -> None:
    source_kinds = ", ".join(f"'{value}'" for value in SOURCE_KINDS)
    statuses = ", ".join(f"'{value}'" for value in STATUSES)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS download_replacements (
            replacement_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL CHECK (source_kind IN ({source_kinds})),
            source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            idempotency_key_hash TEXT,
            target_info_hash TEXT,
            lease_token TEXT,
            lease_expires_at REAL,
            cleanup_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            CHECK ((idempotency_key_hash IS NULL) = (target_info_hash IS NULL)),
            CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS download_replacements_active_source "
        "ON download_replacements(source_kind, source_id) "
        "WHERE status IN ('open', 'submitting', 'replacement_created', "
        "'cleanup_failed')"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS download_replacements_idempotency "
        "ON download_replacements(idempotency_key_hash) "
        "WHERE idempotency_key_hash IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS download_replacements_expiry "
        "ON download_replacements(status, expires_at)"
    )


def _migrate_v2(connection: sqlite3.Connection) -> None:
    """Replace torrent-only targets and add durable dual-channel discovery state."""

    connection.execute("ALTER TABLE download_replacements RENAME TO download_replacements_v1")
    source_kinds = ", ".join(f"'{value}'" for value in SOURCE_KINDS)
    target_kinds = ", ".join(f"'{value}'" for value in TARGET_KINDS)
    statuses = ", ".join(f"'{value}'" for value in STATUSES)
    discovery_statuses = ", ".join(f"'{value}'" for value in DISCOVERY_STATUSES)
    channel_statuses = ", ".join(f"'{value}'" for value in CHANNEL_STATUSES)
    connection.execute(
        f"""
        CREATE TABLE download_replacements (
            replacement_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL CHECK (source_kind IN ({source_kinds})),
            source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            idempotency_key_hash TEXT,
            target_kind TEXT CHECK (target_kind IN ({target_kinds})),
            target_id TEXT,
            lease_token TEXT,
            lease_expires_at REAL,
            cleanup_error TEXT,
            discovery_status TEXT NOT NULL CHECK (discovery_status IN ({discovery_statuses})),
            magnet_status TEXT NOT NULL CHECK (magnet_status IN ({channel_statuses})),
            magnet_count INTEGER NOT NULL CHECK (magnet_count >= 0),
            web_status TEXT NOT NULL CHECK (web_status IN ({channel_statuses})),
            web_providers_json TEXT NOT NULL,
            web_variant TEXT,
            magnet_error_code TEXT,
            web_error_code TEXT,
            discovery_started_at REAL,
            discovery_finished_at REAL,
            discovery_lease_token TEXT,
            discovery_lease_expires_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            CHECK ((idempotency_key_hash IS NULL) = (target_kind IS NULL)),
            CHECK ((target_kind IS NULL) = (target_id IS NULL)),
            CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
            CHECK ((discovery_lease_token IS NULL) = (discovery_lease_expires_at IS NULL))
        )
        """
    )
    connection.execute(
        """
        INSERT INTO download_replacements (
            replacement_id, source_kind, source_id, source_revision, code, code_key,
            status, idempotency_key_hash, target_kind, target_id, lease_token,
            lease_expires_at, cleanup_error, discovery_status, magnet_status,
            magnet_count, web_status, web_providers_json, web_variant,
            magnet_error_code, web_error_code, discovery_started_at,
            discovery_finished_at, discovery_lease_token, discovery_lease_expires_at,
            created_at, updated_at, expires_at
        )
        SELECT replacement_id, source_kind, source_id, source_revision, code, code_key,
            status, idempotency_key_hash,
            CASE WHEN target_info_hash IS NULL THEN NULL ELSE 'qb' END,
            target_info_hash, lease_token, lease_expires_at, cleanup_error,
            'idle', 'pending', 0, 'pending', '[]', NULL, NULL, NULL, NULL, NULL,
            NULL, NULL, created_at, updated_at, expires_at
        FROM download_replacements_v1
        """
    )
    connection.execute("DROP TABLE download_replacements_v1")
    connection.execute(
        "CREATE UNIQUE INDEX download_replacements_active_source "
        "ON download_replacements(source_kind, source_id) "
        "WHERE status IN ('open', 'submitting', 'replacement_created', "
        "'cleanup_failed', 'discarding')"
    )
    connection.execute(
        "CREATE UNIQUE INDEX download_replacements_idempotency "
        "ON download_replacements(idempotency_key_hash) "
        "WHERE idempotency_key_hash IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX download_replacements_expiry "
        "ON download_replacements(status, expires_at)"
    )
    connection.execute(
        "CREATE INDEX download_replacements_discovery "
        "ON download_replacements(discovery_status, updated_at)"
    )


def _migrate_v3(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN magnet_json TEXT NOT NULL DEFAULT '[]'"
    )


def _migrate_v4(connection: sqlite3.Connection) -> None:
    recovery_modes = ", ".join(f"'{value}'" for value in RECOVERY_MODES)
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN recovery_mode "
        f"TEXT NOT NULL DEFAULT 'idle' CHECK (recovery_mode IN ({recovery_modes}))"
    )
    connection.execute(
        "UPDATE download_replacements SET discovery_status = 'idle', "
        "discovery_lease_token = NULL, discovery_lease_expires_at = NULL "
        "WHERE discovery_status IN ('queued', 'running')"
    )


def _migrate_v5(connection: sqlite3.Connection) -> None:
    selection_outcomes = ", ".join(
        f"'{value}'" for value in SMART_SELECTION_OUTCOMES
    )
    cleanup_statuses = ", ".join(
        f"'{value}'" for value in SMART_SELECTION_CLEANUP_STATUSES
    )
    dispositions = ", ".join(f"'{value}'" for value in DISPOSITIONS)
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN smart_selection_id TEXT"
    )
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN smart_selection_outcome "
        f"TEXT CHECK (smart_selection_outcome IN ({selection_outcomes}))"
    )
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN "
        "smart_selection_cleanup_status "
        f"TEXT CHECK (smart_selection_cleanup_status IN ({cleanup_statuses}))"
    )
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN "
        "smart_selection_finished_at REAL"
    )
    connection.execute(
        "ALTER TABLE download_replacements ADD COLUMN disposition "
        f"TEXT CHECK (disposition IN ({dispositions}))"
    )
    connection.execute(
        """
        CREATE TABLE failed_download_archives (
            code_key TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            archived_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX failed_download_archives_time "
        "ON failed_download_archives(archived_at DESC, code_key ASC)"
    )


def _migrate_v6(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    replacement_updates: list[tuple[str, str, str]] = []
    for row in connection.execute(
        "SELECT replacement_id, code, code_key FROM download_replacements"
    ).fetchall():
        try:
            display_code, code_key = _normalize_code(row["code"])
        except DownloadReplacementError as exc:
            raise MigrationError(
                "download replacement catalog code is invalid"
            ) from exc
        if str(row["code"]) != display_code or str(row["code_key"]) != code_key:
            replacement_updates.append(
                (display_code, code_key, str(row["replacement_id"]))
            )
    connection.executemany(
        "UPDATE download_replacements SET code = ?, code_key = ? "
        "WHERE replacement_id = ?",
        replacement_updates,
    )

    archive_updates: list[tuple[str, str, str]] = []
    archive_targets: dict[str, str] = {}
    for row in connection.execute(
        "SELECT code_key, code FROM failed_download_archives"
    ).fetchall():
        try:
            display_code, code_key = _normalize_code(row["code"])
        except DownloadReplacementError as exc:
            raise MigrationError(
                "failed download archive catalog code is invalid"
            ) from exc
        old_key = str(row["code_key"])
        collided = archive_targets.setdefault(code_key, old_key)
        if collided != old_key:
            raise MigrationError(
                "failed download archives collide after catalog code normalization"
            )
        if old_key != code_key or str(row["code"]) != display_code:
            archive_updates.append((old_key, display_code, code_key))

    temporary_updates: list[tuple[str, str]] = []
    reserved_keys = {
        str(row["code_key"])
        for row in connection.execute(
            "SELECT code_key FROM failed_download_archives"
        ).fetchall()
    } | set(archive_targets)
    for position, (old_key, _display_code, _code_key) in enumerate(archive_updates):
        temporary_key = f"MIGRATION{position:08d}"
        while temporary_key in reserved_keys:
            position += len(archive_updates) + 1
            temporary_key = f"MIGRATION{position:08d}"
        reserved_keys.add(temporary_key)
        temporary_updates.append((old_key, temporary_key))
    connection.executemany(
        "UPDATE failed_download_archives SET code_key = ? WHERE code_key = ?",
        ((temporary_key, old_key) for old_key, temporary_key in temporary_updates),
    )
    temporary_by_old_key = dict(temporary_updates)
    connection.executemany(
        "UPDATE failed_download_archives SET code_key = ?, code = ? "
        "WHERE code_key = ?",
        (
            (code_key, display_code, temporary_by_old_key[old_key])
            for old_key, display_code, code_key in archive_updates
        ),
    )


def _verify_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "download_replacements",
        (
            "replacement_id",
            "source_kind",
            "source_id",
            "source_revision",
            "code",
            "code_key",
            "status",
            "idempotency_key_hash",
            "target_kind",
            "target_id",
            "lease_token",
            "lease_expires_at",
            "cleanup_error",
            "discovery_status",
            "magnet_status",
            "magnet_count",
            "magnet_json",
            "web_status",
            "web_providers_json",
            "web_variant",
            "magnet_error_code",
            "web_error_code",
            "discovery_started_at",
            "discovery_finished_at",
            "discovery_lease_token",
            "discovery_lease_expires_at",
            "recovery_mode",
            "smart_selection_id",
            "smart_selection_outcome",
            "smart_selection_cleanup_status",
            "smart_selection_finished_at",
            "disposition",
            "created_at",
            "updated_at",
            "expires_at",
        ),
    )
    require_columns(
        connection,
        "failed_download_archives",
        ("code_key", "code", "archived_at"),
    )
    invalid = connection.execute(
        "SELECT 1 FROM download_replacements WHERE "
        "source_kind NOT IN ('qb', 'web_job', 'web_intent') OR "
        "status NOT IN ('open', 'submitting', 'replacement_created', "
        "'completed', 'cleanup_failed', 'discarding', 'discarded', 'expired') OR "
        "(target_kind IS NOT NULL AND target_kind NOT IN ('qb', 'web_job')) OR "
        "((idempotency_key_hash IS NULL) != (target_kind IS NULL)) OR "
        "((target_kind IS NULL) != (target_id IS NULL)) OR "
        "((lease_token IS NULL) != (lease_expires_at IS NULL)) OR "
        "discovery_status NOT IN ('idle', 'queued', 'running', 'available', "
        "'not_found', 'inconclusive') OR "
        "magnet_status NOT IN ('pending', 'available', 'not_found', 'unavailable') OR "
        "web_status NOT IN ('pending', 'available', 'not_found', 'unavailable') OR "
        "recovery_mode NOT IN ('idle', 'smart_magnet', 'web', 'manual_magnet') OR "
        "(smart_selection_outcome IS NOT NULL AND smart_selection_outcome NOT IN "
        "('running', 'selected', 'not_found', 'inconclusive', 'cancelled', 'failed')) OR "
        "(smart_selection_cleanup_status IS NOT NULL AND "
        "smart_selection_cleanup_status NOT IN "
        "('pending', 'complete', 'not_required', 'incomplete', 'unknown')) OR "
        "(disposition IS NOT NULL AND disposition NOT IN ('archive', 'delete')) OR "
        "((smart_selection_id IS NULL) != (smart_selection_outcome IS NULL)) OR "
        "((smart_selection_outcome IS NULL) != "
        "(smart_selection_cleanup_status IS NULL)) OR "
        "(smart_selection_outcome = 'running' AND "
        "smart_selection_finished_at IS NOT NULL) OR "
        "(smart_selection_outcome IS NOT NULL AND "
        "smart_selection_outcome != 'running' AND "
        "smart_selection_finished_at IS NULL) OR "
        "(discovery_status IN ('queued', 'running') AND recovery_mode = 'idle') OR "
        "magnet_count < 0 OR "
        "((discovery_lease_token IS NULL) != (discovery_lease_expires_at IS NULL)) LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise MigrationError("download replacement schema contains invalid rows")
    for table in ("download_replacements", "failed_download_archives"):
        for row in connection.execute(
            f"SELECT code, code_key FROM {table}"
        ).fetchall():
            try:
                expected_code, expected_key = _normalize_code(row["code"])
            except DownloadReplacementError as exc:
                raise MigrationError(
                    "download replacement catalog code is invalid"
                ) from exc
            if (
                str(row["code"]) != expected_code
                or str(row["code_key"]) != expected_key
            ):
                raise MigrationError(
                    "download replacement catalog identity is invalid"
                )


def _row_to_replacement(row: sqlite3.Row) -> dict[str, object]:
    replacement_id = _validate_replacement_id(row["replacement_id"])
    status = str(row["status"])
    discovery_status = _validate_discovery_status(row["discovery_status"])
    magnet_status = _validate_channel_status(row["magnet_status"])
    web_status = _validate_channel_status(row["web_status"])
    try:
        web_providers = json.loads(str(row["web_providers_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DownloadReplacementError(
            "download replacement Web provider state is invalid"
        ) from exc
    providers = _validate_web_provider_ids(web_providers)
    magnets = _validate_magnet_snapshots(row["magnet_json"])
    return {
        "replacement_id": replacement_id,
        "idempotency_key": replacement_id,
        "source_kind": _validate_source_kind(row["source_kind"]),
        "source_id": str(row["source_id"]),
        "source_revision": str(row["source_revision"]),
        "code": str(row["code"]),
        "status": status,
        "target_kind": (
            _validate_target_kind(row["target_kind"])
            if row["target_kind"] is not None
            else None
        ),
        "target_id": str(row["target_id"]) if row["target_id"] is not None else None,
        "target_info_hash": (
            str(row["target_id"])
            if row["target_kind"] == "qb" and row["target_id"] is not None
            else None
        ),
        "cleanup_error": (
            str(row["cleanup_error"]) if row["cleanup_error"] is not None else None
        ),
        "smart_selection_id": (
            _validate_replacement_id(row["smart_selection_id"])
            if row["smart_selection_id"] is not None
            else None
        ),
        "smart_selection_outcome": (
            _validate_smart_selection_outcome(row["smart_selection_outcome"])
            if row["smart_selection_outcome"] is not None
            else None
        ),
        "smart_selection_cleanup_status": (
            _validate_smart_selection_cleanup_status(
                row["smart_selection_cleanup_status"]
            )
            if row["smart_selection_cleanup_status"] is not None
            else None
        ),
        "smart_selection_finished_at": (
            float(row["smart_selection_finished_at"])
            if row["smart_selection_finished_at"] is not None
            else None
        ),
        "disposition": (
            _validate_disposition(row["disposition"])
            if row["disposition"] is not None
            else None
        ),
        "recovery_mode": _validate_recovery_mode(row["recovery_mode"]),
        "discovery_status": discovery_status,
        "magnet_status": magnet_status,
        "magnet_count": int(row["magnet_count"] or 0),
        "magnets": list(magnets),
        "web_status": web_status,
        "web_provider_ids": list(providers),
        "web_variant": (
            str(row["web_variant"]) if row["web_variant"] is not None else None
        ),
        "magnet_error_code": (
            _validate_discovery_error_code(row["magnet_error_code"])
            if row["magnet_error_code"] is not None
            else None
        ),
        "web_error_code": (
            _validate_discovery_error_code(row["web_error_code"])
            if row["web_error_code"] is not None
            else None
        ),
        "discovery_started_at": (
            float(row["discovery_started_at"])
            if row["discovery_started_at"] is not None
            else None
        ),
        "discovery_finished_at": (
            float(row["discovery_finished_at"])
            if row["discovery_finished_at"] is not None
            else None
        ),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "expires_at": float(row["expires_at"]),
    }


def _validate_source_kind(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in SOURCE_KINDS:
        raise DownloadReplacementError("download replacement source kind is invalid")
    return clean


def _validate_source_id(kind: str, value: object) -> str:
    clean = str(value or "").strip()
    pattern = _QB_HASH_RE if kind == "qb" else _ENTITY_ID_RE
    if not clean.isascii() or pattern.fullmatch(clean) is None:
        raise DownloadReplacementError(
            "download replacement source identity is invalid"
        )
    return clean.lower() if kind == "qb" else clean


def _validate_target_kind(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in TARGET_KINDS:
        raise DownloadReplacementError("download replacement target kind is invalid")
    return clean


def _validate_recovery_mode(value: object, *, allow_idle: bool = True) -> str:
    clean = str(value or "").strip().lower()
    if clean not in RECOVERY_MODES or (not allow_idle and clean == "idle"):
        raise DownloadReplacementError("download recovery mode is invalid")
    return clean


def _validate_smart_selection_outcome(
    value: object, *, allow_running: bool = True
) -> str:
    clean = str(value or "").strip().lower()
    if clean not in SMART_SELECTION_OUTCOMES or (
        not allow_running and clean == "running"
    ):
        raise DownloadReplacementError("smart selection outcome is invalid")
    return clean


def _validate_smart_selection_cleanup_status(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in SMART_SELECTION_CLEANUP_STATUSES:
        raise DownloadReplacementError("smart selection cleanup status is invalid")
    return clean


def _validate_disposition(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in DISPOSITIONS:
        raise DownloadReplacementError("download replacement disposition is invalid")
    return clean


def _channel_discovery_status(channel_status: str) -> str:
    if channel_status == "available":
        return "available"
    if channel_status == "not_found":
        return "not_found"
    return "inconclusive"


def _validate_target_id(kind: str, value: object) -> str:
    clean = str(value or "").strip()
    pattern = _QB_HASH_RE if kind == "qb" else _ENTITY_ID_RE
    if not clean.isascii() or pattern.fullmatch(clean) is None:
        raise DownloadReplacementError("download replacement target identity is invalid")
    return clean.lower() if kind == "qb" else clean


def _validate_revision(value: object) -> str:
    clean = str(value or "").strip()
    if not clean.isascii() or _REVISION_RE.fullmatch(clean) is None:
        raise DownloadReplacementError(
            "download replacement source revision is invalid"
        )
    return clean


def _normalize_code(value: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(value, max_length=40)
    if normalized is None:
        raise DownloadReplacementError("download replacement catalog code is invalid")
    return normalized


def _validate_replacement_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if _REPLACEMENT_ID_RE.fullmatch(clean) is None:
        raise DownloadReplacementError("download replacement identity is invalid")
    return clean


def _validate_discovery_status(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in DISCOVERY_STATUSES:
        raise DownloadReplacementError("download discovery status is invalid")
    return clean


def _validate_channel_status(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in CHANNEL_STATUSES:
        raise DownloadReplacementError("download discovery channel status is invalid")
    return clean


def _validate_discovery_error_code(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    clean = str(value).strip().lower()
    if (
        not clean.isascii()
        or _DISCOVERY_ERROR_RE.fullmatch(clean) is None
        or len(clean.encode("ascii")) > MAX_DISCOVERY_ERROR_CODE_BYTES
    ):
        return "discovery_failed"
    return clean


def _validate_web_provider_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise DownloadReplacementError("download replacement Web provider state is invalid")
    result: list[str] = []
    for item in value:
        clean = str(item or "").strip().lower()
        if not clean or _WEB_PROVIDER_RE.fullmatch(clean) is None:
            raise DownloadReplacementError("download replacement Web provider is invalid")
        if clean not in result:
            result.append(clean)
        if len(result) >= MAX_WEB_PROVIDERS:
            break
    return tuple(result)


def _validate_magnet_snapshots(value: object) -> tuple[dict[str, object], ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DownloadReplacementError("download replacement magnet state is invalid") from exc
    if not isinstance(value, (list, tuple)):
        raise DownloadReplacementError("download replacement magnet state is invalid")
    result: list[dict[str, object]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        info_hash = str(item.get("info_hash") or "").strip().lower()
        refs = item.get("source_refs")
        if not re.fullmatch(r"[0-9a-f]{40}", info_hash) or not isinstance(refs, list):
            continue
        clean_refs = []
        for ref in refs[:8]:
            if not isinstance(ref, dict):
                continue
            uri = str(ref.get("uri") or "").strip()
            source_id = str(ref.get("source_id") or "").strip().lower()
            if uri.startswith("magnet:?xt=urn:btih:") and source_id:
                clean_refs.append({
                    "source_id": source_id,
                    "uri": uri[:8192],
                    "display_name": str(ref.get("display_name") or "")[:512] or None,
                    "reported_size_text": str(ref.get("reported_size_text") or "")[:128] or None,
                    "reported_size_bytes": ref.get("reported_size_bytes"),
                    "badges": [str(x)[:64] for x in ref.get("badges", [])[:8]] if isinstance(ref.get("badges"), list) else [],
                    "trackers": [str(x)[:512] for x in ref.get("trackers", [])[:32]] if isinstance(ref.get("trackers"), list) else [],
                })
        if clean_refs:
            result.append({
                "info_hash": info_hash,
                "display_name": str(item.get("display_name") or "")[:512] or None,
                "size_bytes": item.get("size_bytes"),
                "size_is_exact": bool(item.get("size_is_exact")),
                "source_refs": clean_refs,
            })
    return tuple(result)


def _validate_variant(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    clean = str(value).strip().lower()
    if clean not in {"original", "chinese_subtitle", "uncensored_leak"}:
        raise DownloadReplacementError("download replacement Web variant is invalid")
    return clean


def _validate_idempotency_key(value: object) -> str:
    clean = str(value or "").strip()
    if not clean.isascii() or _IDEMPOTENCY_KEY_RE.fullmatch(clean) is None:
        raise DownloadReplacementError(
            "download replacement idempotency key is invalid"
        )
    return clean


def _idempotency_hash(value: str) -> str:
    return hashlib.sha256(f"download-replacement\0{value}".encode("utf-8")).hexdigest()


def _validate_cleanup_error(value: object | None) -> str:
    clean = str(value or "cleanup_failed").strip().lower()
    if (
        _CLEANUP_ERROR_RE.fullmatch(clean) is None
        or len(clean.encode("ascii")) > MAX_CLEANUP_ERROR_BYTES
    ):
        return "cleanup_failed"
    return clean


def _validate_ttl(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DownloadReplacementError(
            "download replacement expiry is invalid"
        ) from exc
    if not math.isfinite(clean) or not 60 <= clean <= 30 * 24 * 60 * 60:
        raise DownloadReplacementError("download replacement expiry is invalid")
    return clean


def _validate_lease(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DownloadReplacementError("download replacement lease is invalid") from exc
    if not math.isfinite(clean) or not 1 <= clean <= 300:
        raise DownloadReplacementError("download replacement lease is invalid")
    return clean


def _timestamp(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DownloadReplacementError(
            "download replacement timestamp is invalid"
        ) from exc
    if not math.isfinite(clean) or clean < 0:
        raise DownloadReplacementError("download replacement timestamp is invalid")
    return clean


__all__ = [
    "ACTIVE_STATUSES",
    "CHANNEL_STATUSES",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_DISCOVERY_LEASE_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "DISCOVERY_STATUSES",
    "DownloadReplacementConflictError",
    "DownloadReplacementError",
    "DownloadReplacementNotFoundError",
    "DownloadReplacementStore",
    "SCHEMA_COMPONENT",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "TARGET_KINDS",
    "STATUSES",
]
