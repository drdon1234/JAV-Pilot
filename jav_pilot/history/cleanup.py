"""Crash-safe cleanup: prepared operations, recovery facts and candidate removal."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence

from ..web_download.jobs import ACTIVE_STATUSES as WEB_ACTIVE_STATUSES
from .candidates import (
    batch_candidate,
    batch_matches,
    complete_batch_chain,
    load_batch_chain,
    metadata_candidate,
    row_matches,
    web_candidate,
)
from .databases import review_in_progress_attached
from .errors import (
    CoordinatedApplyError,
    HistoryLifecycleConflictError,
    HistoryLifecycleError,
    HistoryLifecycleValidationError,
)
from .facts import (
    fact_digest_from_mapping,
    fact_mapping_from_row,
    legacy_recovery_operation_digest,
    make_fact,
    metadata_nfo_provenance,
    recovery_archived_at,
    recovery_operation_digest,
    recovery_payload,
)
from .fields import (
    TOKEN_RE,
    canonical_bytes,
    cleanup_operation_id,
    code_query_key,
    first_int,
    history_optional_web_variant,
    history_variant_priority,
    history_web_variant,
    optional_text,
    sql_slots,
    timestamp,
    validated_preview_token,
)
from .lifecycle_base import HistoryLifecycleBase
from .models import (
    BATCH_ACTIVE_STATUSES,
    HISTORY_CLEANUP_OPERATION_REVISION,
    MAX_CLEANUP_RECORDS,
    MAX_COMPLETED_OPERATIONS,
    MAX_RECOVERY_FACTS,
    MAX_RECOVERY_PAYLOAD_BYTES,
    MAX_SKIP_DETAILS,
    TASK_TERMINAL_VALUES,
    Candidate,
    Criteria,
    PreparedOperation,
    Preview,
    Skipped,
)


class HistoryCleanupMixin(HistoryLifecycleBase):
    def execute_cleanup(self, preview_token: object) -> dict[str, object]:
        token = validated_preview_token(preview_token)
        now = timestamp(self._clock())
        with self._lock:
            self._recover_prepared_operations_locked()
            preview = self._previews.pop(token, None)
            self._expire_previews(now)
            if preview is None:
                raise HistoryLifecycleConflictError(
                    "history cleanup preview is missing or already consumed"
                )
            if now >= preview.expires_at:
                raise HistoryLifecycleConflictError("history cleanup preview expired")
            self._verify_database_identities()
            skipped = Skipped()
            prepared_items = self._collect_prepared_items(preview, skipped, now)
            removed = [candidate for candidate, _facts in prepared_items]
            if prepared_items:
                operation_id = cleanup_operation_id(token)
                self._prepare_operation(operation_id, prepared_items, now)
                try:
                    self._fault_hook("after_prepare")
                except Exception as exc:
                    self._abort_prepared_operation(operation_id)
                    raise HistoryLifecycleError(
                        "history cleanup transaction failed"
                    ) from exc
                try:
                    _changed, operation_digest = self._apply_prepared_operation(
                        operation_id, allow_missing=False
                    )
                except CoordinatedApplyError as exc:
                    safe_to_abort = exc.rollback_confirmed and (
                        not exc.commit_attempted
                        or self._prepared_operation_is_intact(operation_id)
                    )
                    if safe_to_abort:
                        self._abort_prepared_operation(operation_id)
                    self._raise_cleanup_failure(exc.cause)
                try:
                    self._fault_hook("after_delete_commit")
                except Exception as exc:
                    raise HistoryLifecycleError(
                        "history cleanup committed but completion is pending recovery"
                    ) from exc
                self._complete_prepared_operation(
                    operation_id,
                    expected_digest=operation_digest,
                )

            return {
                "removed": {
                    "records": sum(item.record_count for item in removed),
                    "groups": len(removed),
                    "by_type": dict(
                        sorted(Counter(item.task_type for item in removed).items())
                    ),
                    "ids": [item.identity for item in removed[:MAX_SKIP_DETAILS]],
                    "ids_truncated": len(removed) > MAX_SKIP_DETAILS,
                },
                "skipped": skipped.public(),
                "vacuum_required": bool(removed),
            }

    def recover_prepared_operations(self) -> dict[str, int]:
        with self._lock:
            return self._recover_prepared_operations_locked()

    def _collect_prepared_items(
        self,
        preview: Preview,
        skipped: Skipped,
        archived_at: float,
    ) -> list[tuple[Candidate, list[dict[str, object]]]]:
        try:
            with self._coordinated_connection() as (connection, review_schema):
                connection.execute("BEGIN IMMEDIATE")
                current: list[Candidate] = []
                for candidate in preview.candidates:
                    refreshed, reason = self._refresh_candidate(
                        connection,
                        review_schema,
                        candidate,
                        preview.criteria,
                    )
                    if refreshed is None:
                        skipped.add(candidate.task_type, candidate.identity, reason)
                    else:
                        current.append(refreshed)
                current = self._remove_dangling_dependencies(
                    connection, current, skipped
                )
                prepared = [
                    (
                        candidate,
                        self._facts_for_candidate(connection, candidate, archived_at),
                    )
                    for candidate in current
                ]
                connection.commit()
                return prepared
        except HistoryLifecycleError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryLifecycleError("history cleanup preparation failed") from exc

    def _prepare_operation(
        self,
        operation_id: str,
        items: Sequence[tuple[Candidate, Sequence[Mapping[str, object]]]],
        now: float,
    ) -> None:
        if len(items) > MAX_CLEANUP_RECORDS:
            raise HistoryLifecycleValidationError(
                "history cleanup recovery candidate limit exceeded"
            )
        candidates = tuple(candidate for candidate, _facts in items)
        mapped_facts: list[tuple[int, Mapping[str, object]]] = []
        for position, (_candidate, facts) in enumerate(items):
            mapped_facts.extend((position, fact) for fact in facts)
        mapped_facts.sort(key=lambda item: (item[0], str(item[1]["fact_id"])))
        if len(mapped_facts) > MAX_RECOVERY_FACTS:
            raise HistoryLifecycleValidationError(
                "history cleanup recovery fact limit exceeded"
            )
        preflight_payload_bytes = len(
            canonical_bytes(recovery_payload(candidates, mapped_facts))
        )
        if preflight_payload_bytes > MAX_RECOVERY_PAYLOAD_BYTES:
            raise HistoryLifecycleValidationError(
                "history cleanup recovery payload limit exceeded"
            )
        try:
            with self._library_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                resolved_facts: list[tuple[int, Mapping[str, object]]] = []
                mapping_rows: list[tuple[int, str, str, int]] = []
                for position, fact in mapped_facts:
                    existing = connection.execute(
                        "SELECT fact_id FROM media_library_history_facts "
                        "WHERE fact_id = ?",
                        (fact["fact_id"],),
                    ).fetchone()
                    self._insert_fact(connection, fact)
                    stored = connection.execute(
                        "SELECT * FROM media_library_history_facts WHERE fact_id = ?",
                        (fact["fact_id"],),
                    ).fetchone()
                    if stored is None:
                        raise HistoryLifecycleConflictError(
                            "history cleanup recovery fact is missing"
                        )
                    stored_fact = fact_mapping_from_row(stored)
                    actual_digest = fact_digest_from_mapping(stored_fact)
                    if actual_digest != str(stored_fact["fact_digest"]):
                        raise HistoryLifecycleConflictError(
                            "history cleanup recovery fact digest changed"
                        )
                    resolved_facts.append((position, stored_fact))
                    mapping_rows.append(
                        (
                            position,
                            str(stored_fact["fact_id"]),
                            actual_digest,
                            int(existing is None),
                        )
                    )

                payload = canonical_bytes(
                    recovery_payload(candidates, resolved_facts)
                )
                payload_bytes = len(payload)
                if payload_bytes > MAX_RECOVERY_PAYLOAD_BYTES:
                    raise HistoryLifecycleValidationError(
                        "history cleanup recovery payload limit exceeded"
                    )
                payload_digest = hashlib.sha256(payload).hexdigest()
                fact_summaries = tuple(
                    (
                        position,
                        str(fact["fact_id"]),
                        str(fact["fact_digest"]),
                        recovery_archived_at(fact),
                    )
                    for position, fact in resolved_facts
                )
                operation_digest = recovery_operation_digest(
                    candidates,
                    fact_summaries,
                    payload_digest=payload_digest,
                )
                connection.execute(
                    "INSERT INTO media_library_history_cleanup_operations "
                    "(operation_id, revision, status, operation_digest, "
                    "candidate_count, fact_count, payload_bytes, payload_digest, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        HISTORY_CLEANUP_OPERATION_REVISION,
                        operation_digest,
                        len(candidates),
                        len(resolved_facts),
                        payload_bytes,
                        payload_digest,
                        now,
                        now,
                    ),
                )
                for position, candidate in enumerate(candidates):
                    connection.execute(
                        "INSERT INTO media_library_history_cleanup_candidates "
                        "(operation_id, position, task_type, source_id, "
                        "fingerprint, status, record_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            operation_id,
                            position,
                            candidate.task_type,
                            candidate.identity,
                            candidate.fingerprint,
                            candidate.status,
                            candidate.record_count,
                        ),
                    )
                for position, fact_id, fact_digest, created in mapping_rows:
                    connection.execute(
                        "INSERT INTO media_library_history_cleanup_facts "
                        "(operation_id, candidate_position, fact_id, fact_digest, "
                        "created_by_operation) VALUES (?, ?, ?, ?, ?)",
                        (
                            operation_id,
                            position,
                            fact_id,
                            fact_digest,
                            created,
                        ),
                    )
                connection.commit()
        except HistoryLifecycleError:
            raise
        except sqlite3.IntegrityError as exc:
            raise HistoryLifecycleConflictError(
                "another history cleanup recovery operation is pending"
            ) from exc
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryLifecycleError(
                "history cleanup recovery preparation failed"
            ) from exc

    def _recover_prepared_operations_locked(self) -> dict[str, int]:
        self._verify_database_identities()
        try:
            with self._library_connection() as connection:
                rows = connection.execute(
                    "SELECT operation_id FROM "
                    "media_library_history_cleanup_operations "
                    "WHERE status = 'prepared' ORDER BY created_at, operation_id"
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise HistoryLifecycleError(
                "history cleanup recovery ledger is unavailable"
            ) from exc
        recovered_operations = 0
        recovered_candidates = 0
        for row in rows:
            operation_id = str(row[0])
            try:
                changed, operation_digest = self._apply_prepared_operation(
                    operation_id, allow_missing=True
                )
                recovered_candidates += changed
            except CoordinatedApplyError as exc:
                if (
                    isinstance(exc.cause, HistoryLifecycleConflictError)
                    and exc.rollback_confirmed
                    and not exc.commit_attempted
                ):
                    self._abort_prepared_operation(operation_id)
                    continue
                self._raise_cleanup_failure(exc.cause)
            self._complete_prepared_operation(
                operation_id,
                expected_digest=operation_digest,
            )
            recovered_operations += 1
        return {
            "operations": recovered_operations,
            "candidates": recovered_candidates,
        }

    def _apply_prepared_operation(
        self, operation_id: str, *, allow_missing: bool
    ) -> tuple[int, str]:
        commit_attempted = False
        try:
            with self._coordinated_connection() as (
                connection,
                review_schema,
            ):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    operation = self._load_prepared_operation(connection, operation_id)
                    pending: list[Candidate] = []
                    for candidate in operation.candidates:
                        current = self._refresh_prepared_candidate(
                            connection, review_schema, candidate
                        )
                        if current is None:
                            if not allow_missing:
                                raise HistoryLifecycleConflictError(
                                    "prepared history cleanup state is missing"
                                )
                            continue
                        pending.append(current)
                    rejected = Skipped()
                    safe_pending = self._remove_dangling_dependencies(
                        connection, pending, rejected
                    )
                    if len(safe_pending) != len(pending):
                        raise HistoryLifecycleConflictError(
                            "prepared history cleanup dependencies changed"
                        )
                    for candidate in safe_pending:
                        changed = self._delete_candidate(connection, candidate)
                        if changed != candidate.record_count:
                            raise HistoryLifecycleConflictError(
                                "prepared history cleanup changed during recovery"
                            )
                        self._fault_hook("after_candidate_delete")
                    commit_attempted = True
                    connection.commit()
                    return len(safe_pending), operation.operation_digest
                except BaseException as exc:
                    rollback_confirmed = False
                    if connection.in_transaction:
                        try:
                            connection.rollback()
                        except sqlite3.Error:
                            rollback_confirmed = False
                        else:
                            rollback_confirmed = True
                    if not isinstance(exc, Exception):
                        raise
                    raise CoordinatedApplyError(
                        exc,
                        rollback_confirmed=rollback_confirmed,
                        commit_attempted=commit_attempted,
                    ) from exc
        except CoordinatedApplyError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise CoordinatedApplyError(
                exc,
                rollback_confirmed=False,
                commit_attempted=commit_attempted,
            ) from exc

    def _refresh_prepared_candidate(
        self,
        connection: sqlite3.Connection,
        review_schema: str | None,
        candidate: Candidate,
    ) -> Candidate | None:
        if candidate.task_type == "web":
            row = connection.execute(
                "SELECT * FROM webdb.web_download_jobs WHERE job_id = ?",
                (candidate.identity,),
            ).fetchone()
            if row is None:
                return None
            current = web_candidate(row)
            if str(row["status"]) not in TASK_TERMINAL_VALUES["web"]:
                raise HistoryLifecycleConflictError(
                    "prepared web history is no longer terminal"
                )
        elif candidate.task_type == "metadata":
            row = connection.execute(
                "SELECT * FROM metadb.jobs WHERE job_id = ?",
                (candidate.identity,),
            ).fetchone()
            if row is None:
                return None
            current = metadata_candidate(row)
            if str(row["status"]) not in TASK_TERMINAL_VALUES["metadata"]:
                raise HistoryLifecycleConflictError(
                    "prepared metadata history is no longer terminal"
                )
            metadata_nfo_provenance(row)
            if review_schema is not None and review_in_progress_attached(
                connection, review_schema, row
            ):
                raise HistoryLifecycleConflictError(
                    "prepared metadata history now has an active review"
                )
        else:
            rows, items = load_batch_chain(connection, "webdb", candidate.identity)
            if not rows:
                return None
            current = batch_candidate(candidate.identity, rows, items)
            if not complete_batch_chain(candidate.identity, rows):
                raise HistoryLifecycleConflictError(
                    "prepared batch history chain changed"
                )
            if any(str(row["status"]) in BATCH_ACTIVE_STATUSES for row in rows):
                raise HistoryLifecycleConflictError(
                    "prepared batch history is no longer terminal"
                )
            active = connection.execute(
                "SELECT 1 FROM webdb.web_download_batch_items i "
                "JOIN webdb.web_download_jobs j ON j.job_id = i.job_id "
                "WHERE i.batch_id IN (SELECT batch_id FROM "
                "webdb.web_download_batches WHERE root_chain_id = ?) "
                f"AND j.status IN ({sql_slots(WEB_ACTIVE_STATUSES)}) LIMIT 1",
                (candidate.identity, *WEB_ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                raise HistoryLifecycleConflictError(
                    "prepared batch history has active work"
                )
        if (
            current.fingerprint != candidate.fingerprint
            or current.status != candidate.status
            or current.record_count != candidate.record_count
        ):
            raise HistoryLifecycleConflictError(
                "prepared history cleanup fingerprint changed"
            )
        return current

    def _prepared_operation_is_intact(self, operation_id: str) -> bool:
        try:
            with self._coordinated_connection() as (connection, review_schema):
                connection.execute("BEGIN IMMEDIATE")
                operation = self._load_prepared_operation(connection, operation_id)
                current: list[Candidate] = []
                for candidate in operation.candidates:
                    refreshed = self._refresh_prepared_candidate(
                        connection, review_schema, candidate
                    )
                    if refreshed is None:
                        connection.rollback()
                        return False
                    current.append(refreshed)
                rejected = Skipped()
                safe = self._remove_dangling_dependencies(connection, current, rejected)
                connection.rollback()
                return len(safe) == len(current)
        except (HistoryLifecycleError, OSError, sqlite3.Error, ValueError):
            return False

    def _load_prepared_operation(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> PreparedOperation:
        row = connection.execute(
            "SELECT * FROM media_library_history_cleanup_operations "
            "WHERE operation_id = ? AND status = 'prepared'",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise HistoryLifecycleConflictError(
                "history cleanup recovery operation is unavailable"
            )
        if int(row["revision"]) != HISTORY_CLEANUP_OPERATION_REVISION:
            raise HistoryLifecycleConflictError(
                "history cleanup recovery revision is unsupported"
            )
        candidate_rows = connection.execute(
            "SELECT * FROM media_library_history_cleanup_candidates "
            "WHERE operation_id = ? ORDER BY position",
            (operation_id,),
        ).fetchall()
        candidates = tuple(
            Candidate(
                str(item["task_type"]),
                str(item["source_id"]),
                str(item["fingerprint"]),
                str(item["status"]),
                None,
                None,
                int(item["record_count"]),
                0,
            )
            for item in candidate_rows
        )
        if (
            len(candidates) != int(row["candidate_count"])
            or len(candidates) > MAX_CLEANUP_RECORDS
            or tuple(int(item["position"]) for item in candidate_rows)
            != tuple(range(len(candidates)))
        ):
            raise HistoryLifecycleConflictError(
                "history cleanup recovery candidates are incomplete"
            )
        fact_rows = connection.execute(
            "SELECT mapping.candidate_position, mapping.fact_id, "
            "mapping.fact_digest AS expected_digest, facts.* "
            "FROM media_library_history_cleanup_facts mapping "
            "LEFT JOIN media_library_history_facts facts "
            "ON facts.fact_id = mapping.fact_id "
            "WHERE mapping.operation_id = ? "
            "ORDER BY mapping.candidate_position, mapping.fact_id",
            (operation_id,),
        ).fetchall()
        if (
            len(fact_rows) != int(row["fact_count"])
            or len(fact_rows) > MAX_RECOVERY_FACTS
        ):
            raise HistoryLifecycleConflictError(
                "history cleanup recovery facts are incomplete"
            )
        fact_digests: list[tuple[int, str, str]] = []
        fact_summaries: list[tuple[int, str, str, float]] = []
        mapped_facts: list[tuple[int, Mapping[str, object]]] = []
        for fact_row in fact_rows:
            position = int(fact_row["candidate_position"])
            if position < 0 or position >= len(candidates):
                raise HistoryLifecycleConflictError(
                    "history cleanup recovery fact mapping is invalid"
                )
            fact_id = str(fact_row["fact_id"] or "")
            expected = str(fact_row["expected_digest"] or "")
            if not fact_id or fact_row["source_type"] is None:
                raise HistoryLifecycleConflictError(
                    "history cleanup recovery fact is missing"
                )
            fact = fact_mapping_from_row(fact_row)
            actual = fact_digest_from_mapping(fact)
            if str(fact["fact_digest"]) != expected or actual != expected:
                raise HistoryLifecycleConflictError(
                    "history cleanup recovery fact digest changed"
                )
            fact_digests.append((position, fact_id, expected))
            fact_summaries.append(
                (position, fact_id, expected, recovery_archived_at(fact))
            )
            mapped_facts.append((position, fact))
        payload = canonical_bytes(recovery_payload(candidates, mapped_facts))
        payload_bytes = len(payload)
        if (
            payload_bytes != int(row["payload_bytes"])
            or payload_bytes > MAX_RECOVERY_PAYLOAD_BYTES
        ):
            raise HistoryLifecycleConflictError(
                "history cleanup recovery payload changed"
            )
        stored_payload_digest = optional_text(row["payload_digest"])
        if stored_payload_digest is None:
            operation_digest = legacy_recovery_operation_digest(
                candidates, tuple(fact_digests)
            )
        else:
            if TOKEN_RE.fullmatch(stored_payload_digest) is None:
                raise HistoryLifecycleConflictError(
                    "history cleanup recovery payload digest is invalid"
                )
            payload_digest = hashlib.sha256(payload).hexdigest()
            if payload_digest != stored_payload_digest:
                raise HistoryLifecycleConflictError(
                    "history cleanup recovery payload digest changed"
                )
            operation_digest = recovery_operation_digest(
                candidates,
                tuple(fact_summaries),
                payload_digest=payload_digest,
            )
        if operation_digest != str(row["operation_digest"]):
            raise HistoryLifecycleConflictError(
                "history cleanup recovery operation digest changed"
            )
        return PreparedOperation(
            operation_id,
            operation_digest,
            candidates,
            tuple(fact_digests),
        )

    def _complete_prepared_operation(
        self,
        operation_id: str,
        *,
        expected_digest: str,
    ) -> None:
        now = timestamp(self._clock())
        try:
            with self._library_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT revision, status, operation_digest FROM "
                    "media_library_history_cleanup_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise HistoryLifecycleConflictError(
                        "history cleanup recovery operation is unavailable"
                    )
                if (
                    int(row["revision"]) != HISTORY_CLEANUP_OPERATION_REVISION
                    or str(row["operation_digest"]) != expected_digest
                ):
                    raise HistoryLifecycleConflictError(
                        "history cleanup recovery completion conflicts"
                    )
                status_value = str(row["status"])
                if status_value == "completed":
                    connection.commit()
                    return
                if status_value != "prepared":
                    raise HistoryLifecycleConflictError(
                        "history cleanup recovery completion changed"
                    )
                operation = self._load_prepared_operation(connection, operation_id)
                if operation.operation_digest != expected_digest:
                    raise HistoryLifecycleConflictError(
                        "history cleanup recovery completion conflicts"
                    )
                changed = connection.execute(
                    "UPDATE media_library_history_cleanup_operations "
                    "SET status = 'completed', updated_at = ? "
                    "WHERE operation_id = ? AND status = 'prepared'",
                    (now, operation_id),
                ).rowcount
                if changed != 1:
                    raise HistoryLifecycleConflictError(
                        "history cleanup recovery completion changed"
                    )
                stale = connection.execute(
                    "SELECT operation_id FROM "
                    "media_library_history_cleanup_operations "
                    "WHERE status = 'completed' AND operation_id <> ? "
                    "ORDER BY updated_at DESC, operation_id DESC "
                    "LIMIT -1 OFFSET ?",
                    (operation_id, MAX_COMPLETED_OPERATIONS - 1),
                ).fetchall()
                if stale:
                    connection.executemany(
                        "DELETE FROM media_library_history_cleanup_operations "
                        "WHERE operation_id = ? AND status = 'completed'",
                        [(str(item[0]),) for item in stale],
                    )
                connection.commit()
        except HistoryLifecycleError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryLifecycleError(
                "history cleanup recovery completion failed"
            ) from exc

    def _abort_prepared_operation(self, operation_id: str) -> None:
        try:
            with self._library_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                created = connection.execute(
                    "SELECT fact_id FROM media_library_history_cleanup_facts "
                    "WHERE operation_id = ? AND created_by_operation = 1",
                    (operation_id,),
                ).fetchall()
                connection.execute(
                    "DELETE FROM media_library_history_cleanup_operations "
                    "WHERE operation_id = ? AND status = 'prepared'",
                    (operation_id,),
                )
                for row in created:
                    connection.execute(
                        "DELETE FROM media_library_history_facts WHERE fact_id = ? "
                        "AND NOT EXISTS (SELECT 1 FROM "
                        "media_library_history_cleanup_facts WHERE fact_id = ?)",
                        (str(row[0]), str(row[0])),
                    )
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise HistoryLifecycleError(
                "history cleanup recovery rollback failed"
            ) from exc

    @staticmethod
    def _raise_cleanup_failure(cause: BaseException) -> None:
        if isinstance(cause, HistoryLifecycleError):
            raise cause
        raise HistoryLifecycleError("history cleanup transaction failed") from cause

    def _refresh_candidate(
        self,
        connection: sqlite3.Connection,
        review_schema: str | None,
        candidate: Candidate,
        criteria: Criteria,
    ) -> tuple[Candidate | None, str]:
        if candidate.task_type == "web":
            row = connection.execute(
                "SELECT * FROM webdb.web_download_jobs WHERE job_id = ?",
                (candidate.identity,),
            ).fetchone()
            if row is None:
                return None, "missing"
            current = web_candidate(row)
            if str(row["status"]) != candidate.status:
                return None, "status_changed"
            if current.fingerprint != candidate.fingerprint:
                return None, "record_changed"
            if str(row["status"]) not in TASK_TERMINAL_VALUES["web"]:
                return None, "active_task"
            if not row_matches(criteria, "web", row):
                return None, "no_longer_matches"
            return current, ""
        if candidate.task_type == "metadata":
            row = connection.execute(
                "SELECT * FROM metadb.jobs WHERE job_id = ?", (candidate.identity,)
            ).fetchone()
            if row is None:
                return None, "missing"
            current = metadata_candidate(row)
            if str(row["status"]) != candidate.status:
                return None, "status_changed"
            if current.fingerprint != candidate.fingerprint:
                return None, "record_changed"
            if str(row["status"]) not in TASK_TERMINAL_VALUES["metadata"]:
                return None, "active_task"
            if not row_matches(criteria, "metadata", row):
                return None, "no_longer_matches"
            try:
                metadata_nfo_provenance(row)
            except HistoryLifecycleError:
                return None, "provenance_invalid"
            if review_schema is not None and review_in_progress_attached(
                connection, review_schema, row
            ):
                return None, "review_in_progress"
            return current, ""

        chain_rows, items = load_batch_chain(connection, "webdb", candidate.identity)
        if not chain_rows:
            return None, "missing"
        current = batch_candidate(candidate.identity, chain_rows, items)
        if current.status != candidate.status:
            return None, "status_changed"
        if current.fingerprint != candidate.fingerprint:
            return None, "record_changed"
        if not complete_batch_chain(candidate.identity, chain_rows):
            return None, "incomplete_batch_chain"
        if any(str(row["status"]) in BATCH_ACTIVE_STATUSES for row in chain_rows):
            return None, "active_task"
        if not batch_matches(criteria, chain_rows):
            return None, "no_longer_matches"
        active = connection.execute(
            "SELECT 1 FROM webdb.web_download_batch_items i "
            "JOIN webdb.web_download_jobs j ON j.job_id = i.job_id "
            "WHERE i.batch_id IN (SELECT batch_id FROM webdb.web_download_batches "
            "WHERE root_chain_id = ?) "
            f"AND j.status IN ({sql_slots(WEB_ACTIVE_STATUSES)}) LIMIT 1",
            (candidate.identity, *WEB_ACTIVE_STATUSES),
        ).fetchone()
        if active is not None:
            return None, "active_task"
        return current, ""

    def _remove_dangling_dependencies(
        self,
        connection: sqlite3.Connection,
        candidates: list[Candidate],
        skipped: Skipped,
    ) -> list[Candidate]:
        current = list(candidates)
        while True:
            roots = {item.identity for item in current if item.task_type == "batch"}
            metadata_ids = {
                item.identity for item in current if item.task_type == "metadata"
            }
            rejected: dict[tuple[str, str], str] = {}
            for item in current:
                if item.task_type != "web":
                    continue
                linked_roots = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT b.root_chain_id "
                        "FROM webdb.web_download_batch_items i "
                        "JOIN webdb.web_download_batches b ON b.batch_id = i.batch_id "
                        "WHERE i.job_id = ?",
                        (item.identity,),
                    ).fetchall()
                }
                if linked_roots - roots:
                    rejected[(item.task_type, item.identity)] = "linked_batch_history"
                    continue
                linked_metadata = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT job_id FROM metadb.jobs "
                        "WHERE kind = 'web' AND download_key = ?",
                        (item.identity,),
                    ).fetchall()
                }
                if linked_metadata - metadata_ids:
                    rejected[(item.task_type, item.identity)] = (
                        "linked_metadata_history"
                    )
            if not rejected:
                return current
            next_items: list[Candidate] = []
            for item in current:
                reason = rejected.get((item.task_type, item.identity))
                if reason is None:
                    next_items.append(item)
                else:
                    skipped.add(item.task_type, item.identity, reason)
            current = next_items

    def _facts_for_candidate(
        self,
        connection: sqlite3.Connection,
        candidate: Candidate,
        archived_at: float,
    ) -> list[dict[str, object]]:
        if candidate.task_type == "web":
            row = connection.execute(
                "SELECT * FROM webdb.web_download_jobs WHERE job_id = ?",
                (candidate.identity,),
            ).fetchone()
            if row is None:
                raise HistoryLifecycleConflictError("web history changed")
            details = {
                "variant": history_web_variant(row["variant"]),
                "quality_strategy": str(row["quality_strategy"]),
                "existing_policy": str(row["existing_policy"]),
                "status": str(row["status"]),
            }
            return [
                make_fact(
                    "web",
                    str(row["job_id"]),
                    code=str(row["code"]),
                    code_key=str(row["code_key"]),
                    relative_media_path=(
                        str(row["output_path"])
                        if row["output_path"] is not None
                        else None
                    ),
                    quality_height=first_int(
                        row["verified_height"],
                        row["selected_height"],
                        row["requested_height"],
                    ),
                    nfo_provenance=None,
                    replaces_source_id=optional_text(row["replaces_job_id"]),
                    superseded_by_source_id=optional_text(row["superseded_by_job_id"]),
                    publication_outcome=optional_text(row["publication_outcome"]),
                    details=details,
                    source_created_at=float(row["created_at"]),
                    archived_at=archived_at,
                )
            ]
        if candidate.task_type == "metadata":
            row = connection.execute(
                "SELECT * FROM metadb.jobs WHERE job_id = ?", (candidate.identity,)
            ).fetchone()
            if row is None:
                raise HistoryLifecycleConflictError("metadata history changed")
            provenance = metadata_nfo_provenance(row)
            metadata_variant = history_optional_web_variant(row["variant"])
            return [
                make_fact(
                    "metadata",
                    str(row["job_id"]),
                    code=str(row["code"]),
                    code_key=str(row["code_key"]),
                    relative_media_path=optional_text(row["relative_media_path"]),
                    quality_height=None,
                    nfo_provenance=provenance,
                    replaces_source_id=None,
                    superseded_by_source_id=None,
                    publication_outcome=None,
                    details={
                        "kind": str(row["kind"]),
                        "status": str(row["status"]),
                        "variant": metadata_variant,
                    },
                    source_created_at=float(row["created_at"]),
                    archived_at=archived_at,
                )
            ]

        rows, items = load_batch_chain(connection, "webdb", candidate.identity)
        if not rows:
            raise HistoryLifecycleConflictError("batch history changed")
        result: list[dict[str, object]] = []
        for row in rows:
            batch_id = str(row["batch_id"])
            result.append(
                make_fact(
                    "batch",
                    batch_id,
                    code=str(row["code_or_prefix"]),
                    code_key=code_query_key(str(row["code_or_prefix"])),
                    relative_media_path=None,
                    quality_height=int(row["max_height"]),
                    nfo_provenance=None,
                    replaces_source_id=None,
                    superseded_by_source_id=None,
                    publication_outcome=None,
                    details={
                        "root_chain_id": candidate.identity,
                        "page_number": int(row["page_number"]),
                        "status": str(row["status"]),
                        "existing_policy": str(row["existing_policy"]),
                        "variant_priority": list(
                            history_variant_priority(row["variant_priority_json"])
                        ),
                    },
                    source_created_at=float(row["created_at"]),
                    archived_at=archived_at,
                )
            )
        for item in items:
            height = first_int(item["requested_height"], item["default_height"])
            result.append(
                make_fact(
                    "batch",
                    f"{item['batch_id']}:{int(item['position'])}",
                    code=str(item["code"]),
                    code_key=str(item["code_key"]),
                    relative_media_path=None,
                    quality_height=height,
                    nfo_provenance=None,
                    replaces_source_id=None,
                    superseded_by_source_id=None,
                    publication_outcome=None,
                    details={
                        "root_chain_id": candidate.identity,
                        "variant": history_web_variant(item["variant"]),
                        "selected": bool(item["selected"]),
                        "status": str(item["status"]),
                        "job_id": optional_text(item["job_id"]),
                        "quality_status": str(item["quality_status"]),
                        "quality_strategy": str(item["quality_strategy"]),
                    },
                    source_created_at=min(float(row["created_at"]) for row in rows),
                    archived_at=archived_at,
                )
            )
        return result

    def _insert_fact(
        self, connection: sqlite3.Connection, fact: Mapping[str, object]
    ) -> None:
        columns = (
            "fact_id",
            "source_type",
            "source_id",
            "code",
            "code_key",
            "relative_media_path",
            "quality_height",
            "nfo_provenance_json",
            "replaces_source_id",
            "superseded_by_source_id",
            "publication_outcome",
            "details_json",
            "source_created_at",
            "archived_at",
            "fact_digest",
        )
        connection.execute(
            "INSERT INTO media_library_history_facts "
            f"({', '.join(columns)}) VALUES ({sql_slots(columns)}) "
            "ON CONFLICT(source_type, source_id) DO NOTHING",
            tuple(fact[column] for column in columns),
        )
        stored = connection.execute(
            "SELECT fact_digest FROM media_library_history_facts "
            "WHERE source_type = ? AND source_id = ?",
            (fact["source_type"], fact["source_id"]),
        ).fetchone()
        if stored is None or str(stored[0]) != str(fact["fact_digest"]):
            raise HistoryLifecycleConflictError(
                "archived media fact conflicts with task history"
            )

    def _delete_candidate(
        self, connection: sqlite3.Connection, candidate: Candidate
    ) -> int:
        if candidate.task_type == "batch":
            return int(
                connection.execute(
                    "DELETE FROM webdb.web_download_batches WHERE root_chain_id = ?",
                    (candidate.identity,),
                ).rowcount
            )
        schema = "webdb" if candidate.task_type == "web" else "metadb"
        table = "web_download_jobs" if candidate.task_type == "web" else "jobs"
        return int(
            connection.execute(
                f"DELETE FROM {schema}.{table} WHERE job_id = ?",
                (candidate.identity,),
            ).rowcount
        )
