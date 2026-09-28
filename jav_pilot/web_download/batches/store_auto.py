"""Automatic batch intents created by rules and resource searches."""

from __future__ import annotations

import hmac
import json

from ..errors import WebDownloadError
from ..jobs import ACTIVE_STATUSES, normalize_web_download_code
from .errors import (
    WebDownloadBatchConflictError,
    WebDownloadBatchError,
    WebDownloadBatchNotFoundError,
)
from .models import (
    BATCH_ID_RE,
    SERIES_DISCOVERY_PROVENANCE,
    SHA256_RE,
    BatchRequest,
    LibraryDeduplicationSnapshot,
)
from .records import batch_from_connection, replacement_revision_timestamp, row_to_batch
from .store_base import BatchStoreBase
from .validation import (
    hash_direct_queue_key,
    prefix_key,
    split_full_code,
    validate_batch_id,
    validate_direct_queue_hash,
)


class AutoBatchStoreMixin(BatchStoreBase):
    def create_or_reuse_auto(
        self,
        request: BatchRequest,
        *,
        batch_id: str,
        token_hash: str,
        idempotency_key: object,
        request_hash: object,
    ) -> tuple[dict[str, object], bool]:
        if (
            not BATCH_ID_RE.fullmatch(batch_id)
            or not SHA256_RE.fullmatch(token_hash)
            or request.mode != "exact"
            or not request.auto_commit
        ):
            raise WebDownloadBatchError("automatic download identity is invalid")
        try:
            display_code, code_key = normalize_web_download_code(request.code_or_prefix)
        except WebDownloadError as exc:
            raise WebDownloadBatchError(
                "automatic download catalog code is invalid"
            ) from exc
        prefix, suffix = split_full_code(display_code)
        if (
            prefix_key(prefix) != prefix_key(request.prefix)
            or request.suffix_width != len(suffix)
            or request.start != suffix
            or request.end != suffix
        ):
            raise WebDownloadBatchError("automatic download request is invalid")
        key_hash = hash_direct_queue_key(idempotency_key)
        clean_request_hash = validate_direct_queue_hash(
            request_hash,
            "automatic download request",
        )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT batch_id, request_hash FROM web_download_auto_requests "
                "WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            if replay is not None:
                persisted_hash = str(replay["request_hash"])
                if not hmac.compare_digest(persisted_hash, clean_request_hash):
                    connection.rollback()
                    raise WebDownloadBatchConflictError(
                        "idempotency key was already used for a different request"
                    )
                batch = batch_from_connection(
                    connection,
                    str(replay["batch_id"]),
                    now,
                )
                connection.commit()
                return batch, False

            active_status_slots = ", ".join("?" for _ in ACTIVE_STATUSES)
            active = connection.execute(
                "SELECT b.batch_id FROM web_download_batches b "
                "WHERE b.auto_commit = 1 AND b.direct_code_key = ? AND ("
                "b.status IN ('queued', 'discovering', 'ready') OR ("
                "b.status = 'committed' AND EXISTS ("
                "SELECT 1 FROM web_download_batch_items i "
                "JOIN web_download_jobs j ON j.job_id = i.job_id "
                "WHERE i.batch_id = b.batch_id "
                f"AND j.status IN ({active_status_slots})"
                "))) ORDER BY b.created_at, b.batch_id LIMIT 1",
                (code_key, *ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                active_id = str(active["batch_id"])
                connection.execute(
                    "INSERT INTO web_download_auto_requests "
                    "(key_hash, request_hash, batch_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key_hash, clean_request_hash, active_id, now),
                )
                batch = batch_from_connection(connection, active_id, now)
                connection.commit()
                return batch, False

            connection.execute(
                """
                INSERT INTO web_download_batches (
                    batch_id, token_hash, status, mode, code_or_prefix, prefix,
                    suffix_width, start_suffix, end_suffix, max_height,
                    existing_policy, discovered_count, discovery_complete,
                    created_at, updated_at, expires_at, error, root_chain_id,
                    page_budget, limit_reached, library_revision,
                    quality_complete, rule_id, rule_revision,
                    variant_priority_json, provenance_type, source_session_id,
                    source_revision, source_items_hash, direct_queue_key_hash,
                    direct_queue_request_hash, auto_commit, direct_code_key
                ) VALUES (?, ?, 'queued', 'exact', ?, ?, ?, ?, ?, ?, ?, 0, NULL,
                          ?, ?, NULL, NULL, ?, 1, 0, NULL, 1, NULL, NULL, ?, ?,
                          NULL, NULL, NULL, NULL, NULL, 1, ?)
                """,
                (
                    batch_id,
                    token_hash,
                    display_code,
                    prefix,
                    len(suffix),
                    suffix,
                    suffix,
                    request.max_height,
                    request.existing_policy,
                    now,
                    now,
                    batch_id,
                    json.dumps(request.variant_priority, separators=(",", ":")),
                    SERIES_DISCOVERY_PROVENANCE,
                    code_key,
                ),
            )
            connection.execute(
                "INSERT INTO web_download_auto_requests "
                "(key_hash, request_hash, batch_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key_hash, clean_request_hash, batch_id, now),
            )
            batch = batch_from_connection(connection, batch_id, now)
            connection.commit()
        return batch, True

    def latest_auto_intent(self, code: object) -> dict[str, object] | None:
        _, code_key = normalize_web_download_code(code)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT batch_id FROM web_download_batches "
                "WHERE auto_commit = 1 AND direct_code_key = ? "
                "ORDER BY created_at DESC, batch_id DESC LIMIT 1",
                (code_key,),
            ).fetchone()
            if row is None:
                return None
            return batch_from_connection(
                connection,
                str(row["batch_id"]),
                self._clock(),
            )

    def list_auto_intents(
        self,
        *,
        status_filter: str = "all",
        query: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        clauses, parameters = self._auto_intent_filter(status_filter, query)
        clean_limit = max(1, min(int(limit), 500))
        parameters.append(clean_limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT b.batch_id FROM web_download_batches b WHERE "
                + " AND ".join(clauses)
                + " ORDER BY b.created_at DESC, b.batch_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
            now = self._clock()
            return [
                batch_from_connection(connection, str(row["batch_id"]), now)
                for row in rows
            ]

    @staticmethod
    def _auto_intent_filter(
        status_filter: str,
        query: str | None,
    ) -> tuple[list[str], list[object]]:
        clean_filter = str(status_filter or "all").strip().lower()
        status_groups = {
            "all": (),
            "queued": ("queued", "discovering", "ready"),
            "completed": ("committed",),
            "failed": ("failed", "incomplete", "too_many"),
            "cancelled": ("cancelled", "expired"),
            "retry_wait": ("__none__",),
            "downloading": ("__none__",),
        }
        if clean_filter not in status_groups:
            raise WebDownloadBatchError("web download intent filter is invalid")
        clean_query = str(query or "").strip()
        if len(clean_query) > 40:
            raise WebDownloadBatchError("web download intent query is invalid")
        clauses = [
            "b.auto_commit = 1",
            "NOT EXISTS (SELECT 1 FROM web_download_batch_items i "
            "WHERE i.batch_id = b.batch_id AND i.job_id IS NOT NULL)",
        ]
        parameters: list[object] = []
        statuses = status_groups[clean_filter]
        if statuses:
            clauses.append("b.status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        if clean_query:
            clauses.append("instr(lower(b.code_or_prefix), lower(?)) > 0")
            parameters.append(clean_query)
        return clauses, parameters

    def count_auto_intents(
        self,
        *,
        status_filter: str = "all",
        query: str | None = None,
    ) -> int:
        clauses, parameters = self._auto_intent_filter(status_filter, query)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM web_download_batches b WHERE "
                + " AND ".join(clauses),
                tuple(parameters),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def pending_auto_commit_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT batch_id FROM web_download_batches "
                "WHERE auto_commit = 1 AND status = 'queued' "
                "ORDER BY created_at, batch_id"
            ).fetchall()
        return tuple(str(row["batch_id"]) for row in rows)

    def remove_failed_auto_if_unchanged(
        self,
        batch_id: str,
        *,
        expected_updated_at: object,
    ) -> dict[str, object]:
        clean_id = validate_batch_id(batch_id)
        expected = replacement_revision_timestamp(expected_updated_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            attached_job = connection.execute(
                "SELECT 1 FROM web_download_batch_items "
                "WHERE batch_id = ? AND job_id IS NOT NULL LIMIT 1",
                (clean_id,),
            ).fetchone()
            if (
                not bool(row["auto_commit"])
                or str(row["status"])
                not in {"failed", "incomplete", "too_many", "cancelled"}
                or float(row["updated_at"]) != expected
                or attached_job is not None
            ):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "failed automatic download changed before replacement cleanup"
                )
            items = connection.execute(
                "SELECT code, code_key, status, job_id, selected, quality_status, "
                "available_heights_json, default_height, quality_strategy, "
                "requested_height, quality_error_code, available_variants_json, "
                "variant FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
            removed = row_to_batch(row, items, self._clock())
            changed = connection.execute(
                "DELETE FROM web_download_batches WHERE batch_id = ? "
                "AND auto_commit = 1 "
                "AND status IN ('failed', 'incomplete', 'too_many', 'cancelled') "
                "AND updated_at = ?",
                (clean_id, expected),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "failed automatic download changed before replacement cleanup"
                )
            connection.commit()
        return {**removed, "removed": True}

    def commit_auto(
        self,
        batch_id: str,
        *,
        library_snapshot: LibraryDeduplicationSnapshot,
    ) -> tuple[dict[str, object], bool]:
        return self.commit(
            batch_id,
            "",
            library_snapshot=library_snapshot,
            _automatic=True,
        )
