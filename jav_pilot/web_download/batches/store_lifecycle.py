"""Batch lifecycle in the store: creation, claims, discovery, quality resolution and cancellation."""

from __future__ import annotations

import hmac
import json
import re
from typing import Mapping, Sequence

from ..jobs import normalize_web_download_code
from ..policy import DEFAULT_EXISTING_POLICY
from ..quality import normalize_quality_heights
from ..variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
    MissavVariant,
    normalize_variant_priority,
    normalize_web_download_variant,
)
from .errors import (
    AUTO_DISCOVERY_FAILURE_MESSAGES,
    WebDownloadBatchConflictError,
    WebDownloadBatchError,
    WebDownloadBatchNotFoundError,
)
from .intents import (
    existing_work_height,
    snapshot_existing_variant,
    snapshot_has_variant,
)
from .library import require_current_library_snapshot
from .models import (
    BATCH_ID_RE,
    DEFAULT_BATCH_PAGE_BUDGET,
    DISCOVERY_LIMIT,
    MAX_BATCH_ITEMS,
    RESOURCE_SEARCH_SELECTION_PROVENANCE,
    SHA256_RE,
    BatchRequest,
    LibraryDeduplicationSnapshot,
    SelectedBatchItem,
)
from .records import (
    PREVIEW_TTL_SECONDS,
    batch_from_connection,
    enqueue_batch_terminal_notification,
    fail_bound_rule_preview_locked,
    load_bound_rule_locked,
    row_to_batch,
    row_to_rule,
)
from .store_base import BatchStoreBase
from .validation import (
    batch_item_limit,
    hash_direct_queue_key,
    hash_preview_token,
    normalize_discovered_variants,
    optional_rule_revision,
    optional_snapshot_revision,
    selected_batch_items_hash,
    split_full_code,
    validate_batch_existing_policy,
    validate_batch_id,
    validate_direct_queue_hash,
    validate_intent_height,
    validate_item_quality_strategy,
    validate_max_height,
    validate_page_budget,
    validate_rule_id,
    validate_rule_selection,
    validate_selected_batch_items,
    validate_source_revision,
    validate_source_session_id,
    variant_priority_from_json,
    verify_selected_batch_provenance,
)


class BatchLifecycleStoreMixin(BatchStoreBase):
    def create(
        self,
        request: BatchRequest,
        *,
        batch_id: str,
        token_hash: str,
        page_budget: object = DEFAULT_BATCH_PAGE_BUDGET,
        library_revision: object | None = None,
        rule_id: object | None = None,
        rule_revision: object | None = None,
    ) -> dict[str, object]:
        if not BATCH_ID_RE.fullmatch(batch_id) or not re.fullmatch(
            r"[0-9a-f]{64}", token_hash
        ):
            raise WebDownloadBatchError("batch identity is invalid")
        clean_budget = validate_page_budget(page_budget)
        clean_library_revision = optional_snapshot_revision(library_revision)
        clean_rule_id = None if rule_id is None else validate_rule_id(rule_id)
        clean_rule_revision = optional_rule_revision(rule_revision)
        if (clean_rule_id is None) != (clean_rule_revision is None):
            raise WebDownloadBatchError(
                "batch rule identity and revision must be provided together"
            )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            load_bound_rule_locked(
                connection,
                clean_rule_id,
                clean_rule_revision,
            )
            connection.execute(
                """
                INSERT INTO web_download_batches (
                    batch_id, token_hash, status, mode, code_or_prefix, prefix,
                    suffix_width, start_suffix, end_suffix, max_height,
                    existing_policy, discovered_count, discovery_complete,
                    created_at, updated_at, expires_at, error, root_chain_id,
                    page_budget, limit_reached, library_revision,
                    quality_complete, rule_id, rule_revision,
                    variant_priority_json
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL,
                          ?, ?, NULL, NULL, ?, ?, 0, ?, 1, ?, ?, ?)
                """,
                (
                    batch_id,
                    token_hash,
                    request.mode,
                    request.code_or_prefix,
                    request.prefix,
                    request.suffix_width,
                    request.start,
                    request.end,
                    request.max_height,
                    request.existing_policy,
                    now,
                    now,
                    batch_id,
                    clean_budget,
                    clean_library_revision,
                    clean_rule_id,
                    clean_rule_revision,
                    json.dumps(request.variant_priority, separators=(",", ":")),
                ),
            )
            connection.commit()
        return self.get(batch_id)

    def create_selected(
        self,
        items: Sequence[SelectedBatchItem],
        *,
        batch_id: str,
        token_hash: str,
        source_session_id: object,
        source_revision: object,
        max_height: object = 2160,
        existing_policy: object = DEFAULT_EXISTING_POLICY,
        variant_priority: object = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        quality_pending: bool,
        default_quality_strategy: object = "highest",
        default_height: object | None = None,
        selected_code_keys: frozenset[str] | None = None,
        library_revision: object | None = None,
        rule_id: object | None = None,
        rule_revision: object | None = None,
        direct_queue_key_hash: object | None = None,
        direct_queue_request_hash: object | None = None,
    ) -> dict[str, object]:
        if not BATCH_ID_RE.fullmatch(batch_id) or not SHA256_RE.fullmatch(token_hash):
            raise WebDownloadBatchError("batch identity is invalid")
        clean_items = validate_selected_batch_items(items)
        clean_session_id = validate_source_session_id(source_session_id)
        clean_revision = validate_source_revision(source_revision)
        clean_max_height = validate_max_height(max_height)
        clean_existing_policy = validate_batch_existing_policy(existing_policy)
        try:
            clean_priority = normalize_variant_priority(variant_priority)
        except ValueError as exc:
            raise WebDownloadBatchError(str(exc)) from exc
        if not isinstance(quality_pending, bool):
            raise WebDownloadBatchError("batch quality state is invalid")
        clean_quality_strategy = validate_item_quality_strategy(
            default_quality_strategy
        )
        clean_default_height = (
            clean_max_height
            if clean_quality_strategy == "highest"
            else validate_intent_height(
                default_height,
                max_height=clean_max_height,
            )
        )
        available_code_keys = frozenset(item.code_key for item in clean_items)
        clean_selected_code_keys = (
            available_code_keys
            if selected_code_keys is None
            else frozenset(selected_code_keys)
        )
        if not clean_selected_code_keys.issubset(available_code_keys):
            raise WebDownloadBatchError("batch default selection is invalid")
        clean_library_revision = optional_snapshot_revision(library_revision)
        clean_rule_id = None if rule_id is None else validate_rule_id(rule_id)
        clean_rule_revision = optional_rule_revision(rule_revision)
        if (clean_rule_id is None) != (clean_rule_revision is None):
            raise WebDownloadBatchError(
                "batch rule identity and revision must be provided together"
            )
        if (direct_queue_key_hash is None) != (direct_queue_request_hash is None):
            raise WebDownloadBatchError("direct queue identity is invalid")
        clean_direct_key_hash = (
            None
            if direct_queue_key_hash is None
            else validate_direct_queue_hash(
                direct_queue_key_hash,
                "direct queue identity",
            )
        )
        clean_direct_request_hash = (
            None
            if direct_queue_request_hash is None
            else validate_direct_queue_hash(
                direct_queue_request_hash,
                "direct queue request",
            )
        )
        source_items_hash = selected_batch_items_hash(
            clean_session_id,
            clean_revision,
            clean_items,
        )
        first = clean_items[0]
        prefix, suffix = split_full_code(first.code)
        now = self._clock()
        initial_status = "ready" if clean_direct_key_hash is not None else "queued"
        expires_at = (
            now + PREVIEW_TTL_SECONDS if clean_direct_key_hash is not None else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            load_bound_rule_locked(
                connection,
                clean_rule_id,
                clean_rule_revision,
            )
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
                    direct_queue_request_hash
                ) VALUES (?, ?, ?, 'exact', ?, ?, ?, ?, ?, ?, ?, ?, 1,
                          ?, ?, ?, NULL, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?)
                """,
                (
                    batch_id,
                    token_hash,
                    initial_status,
                    first.code,
                    prefix,
                    len(suffix),
                    suffix,
                    suffix,
                    clean_max_height,
                    clean_existing_policy,
                    len(clean_items),
                    now,
                    now,
                    expires_at,
                    batch_id,
                    clean_library_revision,
                    int(not quality_pending),
                    clean_rule_id,
                    clean_rule_revision,
                    json.dumps(clean_priority, separators=(",", ":")),
                    RESOURCE_SEARCH_SELECTION_PROVENANCE,
                    clean_session_id,
                    clean_revision,
                    source_items_hash,
                    clean_direct_key_hash,
                    clean_direct_request_hash,
                ),
            )
            for position, item in enumerate(clean_items):
                connection.execute(
                    "INSERT INTO web_download_batch_items "
                    "(batch_id, position, code, code_key, status, job_id, "
                    "selected, quality_status, available_heights_json, default_height, "
                    "quality_strategy, requested_height, "
                    "available_variants_json, variant) "
                    "VALUES (?, ?, ?, ?, 'discovered', NULL, ?, ?, '[]', ?, ?, ?, "
                    "?, ?)",
                    (
                        batch_id,
                        position,
                        item.code,
                        item.code_key,
                        int(item.code_key in clean_selected_code_keys),
                        "pending" if quality_pending else "legacy",
                        clean_default_height,
                        clean_quality_strategy,
                        clean_default_height,
                        json.dumps(item.available_variants, separators=(",", ":")),
                        item.variant,
                    ),
                )
            connection.commit()
        return self.get(batch_id)

    def replay_direct_submission(
        self,
        idempotency_key: object,
        request_hash: object,
    ) -> dict[str, object] | None:
        key_hash = hash_direct_queue_key(idempotency_key)
        clean_request_hash = validate_direct_queue_hash(
            request_hash,
            "direct queue request",
        )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT batch_id, status, direct_queue_request_hash "
                "FROM web_download_batches WHERE direct_queue_key_hash = ?",
                (key_hash,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            persisted_hash = str(row["direct_queue_request_hash"] or "")
            if not hmac.compare_digest(persisted_hash, clean_request_hash):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "idempotency key was already used for a different request"
                )
            batch_id = str(row["batch_id"])
            if str(row["status"]) != "committed":
                connection.execute(
                    "DELETE FROM web_download_batches WHERE batch_id = ? "
                    "AND status != 'committed'",
                    (batch_id,),
                )
                connection.commit()
                return None
            batch = batch_from_connection(connection, batch_id, now)
            connection.commit()
            return batch

    def discard_direct_submission(
        self,
        idempotency_key: object,
        request_hash: object,
        *,
        batch_id: object,
    ) -> bool:
        key_hash = hash_direct_queue_key(idempotency_key)
        clean_request_hash = validate_direct_queue_hash(
            request_hash,
            "direct queue request",
        )
        clean_batch_id = validate_batch_id(batch_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "DELETE FROM web_download_batches WHERE batch_id = ? "
                "AND direct_queue_key_hash = ? "
                "AND direct_queue_request_hash = ? AND status != 'committed'",
                (clean_batch_id, key_hash, clean_request_hash),
            ).rowcount
            connection.commit()
        return changed == 1

    def claim(self, batch_id: str) -> BatchRequest | None:
        clean_id = validate_batch_id(batch_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'discovering', "
                "updated_at = ? WHERE batch_id = ? AND status = 'queued'",
                (now, clean_id),
            ).rowcount
            if changed != 1:
                connection.commit()
                return None
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            return None
        return BatchRequest(
            mode=str(row["mode"]),
            code_or_prefix=str(row["code_or_prefix"]),
            prefix=str(row["prefix"]),
            suffix_width=(
                int(row["suffix_width"]) if row["suffix_width"] is not None else None
            ),
            start=str(row["start_suffix"]) if row["start_suffix"] is not None else None,
            end=str(row["end_suffix"]) if row["end_suffix"] is not None else None,
            max_height=int(row["max_height"]),
            existing_policy=str(row["existing_policy"]),
            variant_priority=variant_priority_from_json(row["variant_priority_json"]),
            provenance_type=str(row["provenance_type"]),
            auto_commit=bool(row["auto_commit"]),
        )

    def prepare_selected(self, batch_id: str) -> bool:
        clean_id = validate_batch_id(batch_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "discovering"
                or str(row["provenance_type"]) != RESOURCE_SEARCH_SELECTION_PROVENANCE
            ):
                connection.commit()
                return False
            items = connection.execute(
                "SELECT code, code_key, available_variants_json, variant "
                "FROM web_download_batch_items WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
            verify_selected_batch_provenance(row, items)
            quality_complete = bool(row["quality_complete"])
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'ready', updated_at = ?, "
                "expires_at = ? WHERE batch_id = ? AND status = 'discovering'",
                (
                    now,
                    now + PREVIEW_TTL_SECONDS if quality_complete else None,
                    clean_id,
                ),
            ).rowcount
            connection.commit()
        return changed == 1

    def finish_discovery(
        self,
        batch_id: str,
        *,
        codes: Sequence[str],
        variants_by_code: Mapping[str, Sequence[object]] | None = None,
        complete: bool,
        quality_pending: bool = False,
    ) -> bool:
        clean_id = validate_batch_id(batch_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, mode, page_number, page_budget, start_suffix, "
                "variant_priority_json, auto_commit, code_or_prefix, "
                "rule_id, rule_revision "
                "FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "discovering":
                connection.commit()
                return False
            try:
                load_bound_rule_locked(
                    connection,
                    row["rule_id"],
                    row["rule_revision"],
                )
            except WebDownloadBatchConflictError:
                fail_bound_rule_preview_locked(
                    connection,
                    clean_id,
                    now=now,
                    clock=self._clock,
                )
                connection.commit()
                return False
            page_codes = tuple(codes)
            auto_commit = bool(row["auto_commit"])
            if auto_commit and quality_pending:
                connection.rollback()
                raise WebDownloadBatchError(
                    "automatic download must defer quality discovery"
                )
            variant_map = normalize_discovered_variants(
                page_codes,
                variants_by_code,
            )
            continuation_start: str | None = None
            limit_reached = False
            if str(row["mode"]) == "all" and len(page_codes) > MAX_BATCH_ITEMS:
                _, continuation_start = split_full_code(page_codes[MAX_BATCH_ITEMS])
                current_start = (
                    int(row["start_suffix"])
                    if row["start_suffix"] is not None
                    else None
                )
                if (
                    current_start is not None
                    and int(continuation_start) <= current_start
                ):
                    connection.rollback()
                    raise WebDownloadBatchError(
                        "batch discovery did not advance its continuation cursor"
                    )
                page_codes = page_codes[:MAX_BATCH_ITEMS]
                status = "ready"
                error = None
                expires_at = (
                    None
                    if quality_pending and page_codes
                    else now + PREVIEW_TTL_SECONDS
                )
                limit_reached = int(row["page_number"]) >= int(row["page_budget"])
            elif len(page_codes) > MAX_BATCH_ITEMS:
                status = "too_many"
                error = (
                    "The automatic batch page limit was reached"
                    if str(row["mode"]) == "all"
                    else "More than 64 works were found; narrow the range"
                )
                expires_at = None
            elif not complete:
                status = "failed" if auto_commit else "incomplete"
                error = (
                    "MissAV automatic download discovery did not finish"
                    if auto_commit
                    else "MissAV discovery did not finish; retry the preview"
                )
                expires_at = None
            else:
                status = "failed" if auto_commit and not page_codes else "ready"
                error = (
                    "No exact MissAV result was found for this catalog code"
                    if auto_commit and not page_codes
                    else None
                    if page_codes
                    else "No matching works were found"
                )
                expires_at = (
                    None
                    if auto_commit or (quality_pending and page_codes)
                    else now + PREVIEW_TTL_SECONDS
                )
            discovered_count = len(page_codes)
            connection.execute(
                "DELETE FROM web_download_batch_items WHERE batch_id = ?",
                (clean_id,),
            )
            variant_priority = variant_priority_from_json(row["variant_priority_json"])
            for position, code in enumerate(page_codes[:DISCOVERY_LIMIT]):
                display, code_key = normalize_web_download_code(code)
                available_variants = variant_map[code_key]
                selected_variant = next(
                    variant
                    for variant in variant_priority
                    if variant in available_variants
                )
                connection.execute(
                    "INSERT INTO web_download_batch_items "
                    "(batch_id, position, code, code_key, status, job_id, "
                    "quality_status, available_heights_json, quality_strategy, "
                    "available_variants_json, variant) "
                    "VALUES (?, ?, ?, ?, 'discovered', NULL, ?, '[]', 'highest', "
                    "?, ?)",
                    (
                        clean_id,
                        position,
                        display,
                        code_key,
                        "pending" if quality_pending else "legacy",
                        json.dumps(available_variants, separators=(",", ":")),
                        selected_variant,
                    ),
                )
            changed = connection.execute(
                "UPDATE web_download_batches SET status = ?, discovered_count = ?, "
                "discovery_complete = ?, updated_at = ?, expires_at = ?, error = ?, "
                "next_start_suffix = ? "
                ", limit_reached = ?, quality_complete = ? "
                "WHERE batch_id = ? AND status = 'discovering'",
                (
                    status,
                    discovered_count,
                    1 if complete else 0,
                    now,
                    expires_at,
                    error,
                    continuation_start,
                    int(limit_reached),
                    int(not quality_pending or not page_codes or status != "ready"),
                    clean_id,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return False
            if status in {"incomplete", "too_many"} or (
                auto_commit and status == "failed"
            ):
                enqueue_batch_terminal_notification(
                    connection,
                    clean_id,
                    status="failed",
                    code=str(row["code_or_prefix"]) if auto_commit else None,
                    error_code=(
                        (
                            "not_found"
                            if complete and not page_codes
                            else "discovery_failed"
                        )
                        if auto_commit
                        else "batch_failed"
                    ),
                    occurred_at=now,
                    clock=self._clock,
                )
            connection.commit()
        return True

    def fail_discovery(
        self,
        batch_id: str,
        *,
        failure_code: str = "queue_failed",
    ) -> bool:
        clean_id = validate_batch_id(batch_id)
        clean_failure_code = (
            failure_code
            if failure_code in AUTO_DISCOVERY_FAILURE_MESSAGES
            else "queue_failed"
        )
        auto_error = AUTO_DISCOVERY_FAILURE_MESSAGES[clean_failure_code]
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT auto_commit, code_or_prefix FROM web_download_batches "
                "WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            auto_commit = bool(row["auto_commit"])
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'failed', updated_at = ?, "
                "quality_complete = CASE WHEN provenance_type = ? OR auto_commit = 1 "
                "THEN 1 ELSE quality_complete END, "
                "expires_at = NULL, error = CASE WHEN auto_commit = 1 "
                "THEN ? "
                "WHEN provenance_type = ? "
                "THEN 'Selected resource preview failed' "
                "ELSE 'MissAV batch discovery failed' END "
                "WHERE batch_id = ? AND (status IN ('queued', 'discovering') "
                "OR (provenance_type = ? AND status = 'ready' "
                "AND quality_complete = 0) "
                "OR (auto_commit = 1 AND status = 'ready'))",
                (
                    now,
                    RESOURCE_SEARCH_SELECTION_PROVENANCE,
                    auto_error,
                    RESOURCE_SEARCH_SELECTION_PROVENANCE,
                    clean_id,
                    RESOURCE_SEARCH_SELECTION_PROVENANCE,
                ),
            ).rowcount
            if changed == 1:
                enqueue_batch_terminal_notification(
                    connection,
                    clean_id,
                    status="failed",
                    code=(str(row["code_or_prefix"]) if auto_commit else None),
                    error_code=(
                        clean_failure_code if auto_commit else "batch_failed"
                    ),
                    occurred_at=now,
                    clock=self._clock,
                )
            connection.commit()
        return changed == 1

    def quality_requests(
        self, batch_id: str
    ) -> tuple[int, tuple[tuple[str, str, MissavVariant], ...]]:
        clean_id = validate_batch_id(batch_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, max_height, quality_complete, rule_id, rule_revision "
                "FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            if str(row["status"]) != "ready" or bool(row["quality_complete"]):
                connection.commit()
                return int(row["max_height"]), ()
            try:
                load_bound_rule_locked(
                    connection,
                    row["rule_id"],
                    row["rule_revision"],
                )
            except WebDownloadBatchConflictError:
                fail_bound_rule_preview_locked(
                    connection,
                    clean_id,
                    now=self._clock(),
                    clock=self._clock,
                )
                connection.commit()
                return int(row["max_height"]), ()
            items = connection.execute(
                "SELECT code, code_key, variant FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
            connection.commit()
        return int(row["max_height"]), tuple(
            (
                str(item["code"]),
                str(item["code_key"]),
                normalize_web_download_variant(item["variant"]),
            )
            for item in items
        )

    def finish_quality_resolution(
        self,
        batch_id: str,
        results: Mapping[str, Sequence[object] | None],
        *,
        rule: Mapping[str, object] | None = None,
        library_snapshot: LibraryDeduplicationSnapshot | None = None,
    ) -> bool:
        clean_id = validate_batch_id(batch_id)
        if not isinstance(results, Mapping):
            raise WebDownloadBatchError("batch quality results are invalid")
        if rule is not None:
            if library_snapshot is None:
                raise WebDownloadBatchError("batch rule media snapshot is missing")
            require_current_library_snapshot(library_snapshot)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, max_height, quality_complete, library_revision, "
                "rule_id, rule_revision, provenance_type "
                "FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "ready"
                or bool(row["quality_complete"])
            ):
                connection.commit()
                return False
            if len(results) > batch_item_limit(row):
                connection.rollback()
                raise WebDownloadBatchError("batch quality results are invalid")
            try:
                bound_rule_row = load_bound_rule_locked(
                    connection,
                    row["rule_id"],
                    row["rule_revision"],
                )
                if (bound_rule_row is None) != (rule is None):
                    raise WebDownloadBatchConflictError(
                        "batch rule changed before preview completion"
                    )
                if bound_rule_row is not None and rule is not None:
                    clean_rule_id = validate_rule_id(rule.get("rule_id"))
                    clean_rule_revision = optional_rule_revision(rule.get("revision"))
                    if clean_rule_id != str(
                        bound_rule_row["rule_id"]
                    ) or clean_rule_revision != int(bound_rule_row["revision"]):
                        raise WebDownloadBatchConflictError(
                            "batch rule changed before preview completion"
                        )
                    rule = row_to_rule(bound_rule_row)
            except WebDownloadBatchConflictError:
                fail_bound_rule_preview_locked(
                    connection,
                    clean_id,
                    now=now,
                    clock=self._clock,
                )
                connection.commit()
                return False
            items = connection.execute(
                "SELECT code_key, variant FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
            expected = tuple(str(item["code_key"]) for item in items)
            variants = {
                str(item["code_key"]): normalize_web_download_variant(item["variant"])
                for item in items
            }
            if set(results) != set(expected):
                connection.rollback()
                raise WebDownloadBatchError("batch quality results are incomplete")
            max_height = int(row["max_height"])
            rule_strategy = "highest"
            rule_height = max_height
            rule_selection = "all"
            if rule is not None:
                persisted_revision = optional_snapshot_revision(
                    row["library_revision"]
                )
                if persisted_revision != library_snapshot.index_revision:
                    connection.rollback()
                    raise WebDownloadBatchConflictError(
                        "media library changed since this preview was created"
                    )
                rule_strategy = validate_item_quality_strategy(
                    rule.get("default_quality_strategy")
                )
                rule_height = validate_intent_height(
                    rule.get("default_height"), max_height=max_height
                )
                rule_selection = validate_rule_selection(rule.get("selection_mode"))
            for code_key in expected:
                raw_heights = results[code_key]
                eligible: tuple[int, ...] = ()
                if raw_heights is None:
                    heights: tuple[int, ...] = ()
                    quality_status = "failed"
                    default_height = None
                    error_code = "quality_unavailable"
                else:
                    try:
                        heights = normalize_quality_heights(raw_heights)
                    except ValueError as exc:
                        connection.rollback()
                        raise WebDownloadBatchError(
                            "batch quality results are invalid"
                        ) from exc
                    eligible = tuple(
                        height for height in heights if height <= max_height
                    )
                    if eligible:
                        quality_status = "ready"
                        default_height = max(eligible)
                        error_code = None
                    else:
                        quality_status = "failed"
                        default_height = None
                        error_code = "no_eligible_quality"
                selected = True
                quality_strategy = "highest"
                requested_height: int | None = None
                if rule is not None:
                    quality_strategy = rule_strategy
                    requested_height = (
                        max_height if rule_strategy == "highest" else rule_height
                    )
                    selected = quality_status == "ready"
                    if rule_strategy == "selected" and rule_height not in eligible:
                        selected = False
                    if selected and rule_selection != "all":
                        variant = variants[code_key]
                        existing = snapshot_existing_variant(
                            library_snapshot,
                            code_key,
                            variant,
                        )
                        present = snapshot_has_variant(
                            library_snapshot,
                            code_key,
                            variant,
                        )
                        if rule_selection == "missing":
                            selected = not present
                        else:
                            existing_height = existing_work_height(existing)
                            candidate_height = (
                                default_height
                                if rule_strategy == "highest"
                                else rule_height
                            )
                            selected = bool(
                                present
                                and existing_height is not None
                                and candidate_height is not None
                                and candidate_height > existing_height
                            )
                connection.execute(
                    "UPDATE web_download_batch_items SET quality_status = ?, "
                    "available_heights_json = ?, default_height = ?, "
                    "selected = ?, quality_strategy = ?, requested_height = ?, "
                    "quality_error_code = ? WHERE batch_id = ? AND code_key = ?",
                    (
                        quality_status,
                        json.dumps(heights, separators=(",", ":")),
                        default_height,
                        int(selected),
                        quality_strategy,
                        requested_height,
                        error_code,
                        clean_id,
                        code_key,
                    ),
                )
            changed = connection.execute(
                "UPDATE web_download_batches SET quality_complete = 1, updated_at = ?, "
                "expires_at = ? "
                "WHERE batch_id = ? AND status = 'ready' AND quality_complete = 0",
                (now, now + PREVIEW_TTL_SECONDS, clean_id),
            ).rowcount
            connection.commit()
        return changed == 1

    def fail_quality_resolution(self, batch_id: str) -> bool:
        clean_id = validate_batch_id(batch_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'failed', "
                "quality_complete = 1, updated_at = ?, expires_at = NULL, "
                "error = CASE WHEN provenance_type = ? "
                "THEN 'Selected resource qualities could not be resolved' "
                "ELSE 'Batch rule preview could not be completed' END "
                "WHERE batch_id = ? AND status = 'ready' AND quality_complete = 0",
                (now, RESOURCE_SEARCH_SELECTION_PROVENANCE, clean_id),
            ).rowcount
            if changed == 1:
                enqueue_batch_terminal_notification(
                    connection,
                    clean_id,
                    status="failed",
                    occurred_at=now,
                    clock=self._clock,
                )
            connection.commit()
        return changed == 1

    def requeue_discovery(self, batch_id: str) -> bool:
        clean_id = validate_batch_id(batch_id)
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'queued', updated_at = ?, "
                "expires_at = NULL, error = NULL "
                "WHERE batch_id = ? AND status = 'discovering'",
                (self._clock(), clean_id),
            ).rowcount
        return changed == 1

    def fail_claim(self, batch_id: str) -> bool:
        clean_id = validate_batch_id(batch_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT auto_commit, code_or_prefix FROM web_download_batches "
                "WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            auto_commit = bool(row["auto_commit"])
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'failed', updated_at = ?, "
                "expires_at = NULL, error = CASE WHEN auto_commit = 1 "
                "THEN 'MissAV automatic download could not be started' "
                "WHEN provenance_type = ? "
                "THEN 'Selected resource preview could not be started' "
                "ELSE 'Batch discovery could not be started' END "
                "WHERE batch_id = ? AND status IN ('queued', 'discovering')",
                (now, RESOURCE_SEARCH_SELECTION_PROVENANCE, clean_id),
            ).rowcount
            if changed == 1:
                enqueue_batch_terminal_notification(
                    connection,
                    clean_id,
                    status="failed",
                    code=(str(row["code_or_prefix"]) if auto_commit else None),
                    error_code=("queue_failed" if auto_commit else "batch_failed"),
                    occurred_at=now,
                    clock=self._clock,
                )
            connection.commit()
        return changed == 1

    def create_continuation(
        self,
        batch_id: str,
        preview_token: object,
        *,
        continuation_id: str,
        library_revision: object | None = None,
    ) -> tuple[dict[str, object], bool]:
        clean_id = validate_batch_id(batch_id)
        clean_continuation_id = validate_batch_id(continuation_id)
        token_hash = hash_preview_token(preview_token)
        clean_library_revision = optional_snapshot_revision(library_revision)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            self._validate_chain_locked(connection, str(row["root_chain_id"]))
            if not hmac.compare_digest(str(row["token_hash"]), token_hash):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "preview token is invalid or expired"
                )
            existing_id = row["continuation_batch_id"]
            if existing_id is not None:
                existing_batch = batch_from_connection(
                    connection,
                    str(existing_id),
                    now,
                )
                connection.commit()
                return existing_batch, False
            next_start = row["next_start_suffix"]
            expires_at = row["expires_at"]
            if (
                str(row["status"]) != "committed"
                or next_start is None
                or bool(row["limit_reached"])
                or int(row["page_number"]) >= int(row["page_budget"])
            ):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch has no available continuation"
                )
            if expires_at is None or float(expires_at) <= now:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "preview token is invalid or expired"
                )
            load_bound_rule_locked(
                connection,
                row["rule_id"],
                row["rule_revision"],
            )
            connection.execute(
                """
                INSERT INTO web_download_batches (
                    batch_id, token_hash, status, mode, code_or_prefix, prefix,
                    suffix_width, start_suffix, end_suffix, max_height,
                    existing_policy, commit_intent_hash, discovered_count,
                    discovery_complete, created_at, updated_at, expires_at,
                    error, parent_batch_id, page_number, next_start_suffix,
                    continuation_batch_id, root_chain_id, page_budget,
                    limit_reached, library_revision, quality_complete, rule_id,
                    rule_revision, variant_priority_json
                ) VALUES (?, ?, 'queued', 'all', ?, ?, ?, ?, ?, ?, ?,
                          NULL, 0, NULL, ?, ?, NULL, NULL, ?, ?, NULL, NULL,
                          ?, ?, 0, ?, 1, ?, ?, ?)
                """,
                (
                    clean_continuation_id,
                    str(row["token_hash"]),
                    str(row["code_or_prefix"]),
                    str(row["prefix"]),
                    (
                        int(row["suffix_width"])
                        if row["suffix_width"] is not None
                        else None
                    ),
                    str(next_start),
                    (str(row["end_suffix"]) if row["end_suffix"] is not None else None),
                    int(row["max_height"]),
                    str(row["existing_policy"]),
                    now,
                    now,
                    clean_id,
                    int(row["page_number"]) + 1,
                    str(row["root_chain_id"]),
                    int(row["page_budget"]),
                    clean_library_revision,
                    str(row["rule_id"]) if row["rule_id"] is not None else None,
                    (
                        int(row["rule_revision"])
                        if row["rule_revision"] is not None
                        else None
                    ),
                    str(row["variant_priority_json"]),
                ),
            )
            changed = connection.execute(
                "UPDATE web_download_batches SET continuation_batch_id = ?, "
                "updated_at = ? WHERE batch_id = ? AND continuation_batch_id IS NULL",
                (clean_continuation_id, now, clean_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch continuation could not be created"
                )
            continuation = batch_from_connection(
                connection,
                clean_continuation_id,
                now,
            )
            connection.commit()
        return continuation, True

    def cancel(self, batch_id: str) -> dict[str, object]:
        clean_id = validate_batch_id(batch_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            status = str(row["status"])
            if status == "cancelled":
                connection.commit()
                return self.get(clean_id)
            if status == "committed":
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "committed web download batches cannot be cancelled"
                )
            connection.execute(
                "UPDATE web_download_batches SET status = 'cancelled', updated_at = ?, "
                "expires_at = NULL, error = NULL WHERE batch_id = ?",
                (self._clock(), clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def remove(self, batch_id: str) -> dict[str, object]:
        clean_id = validate_batch_id(batch_id)
        self._expire_ready(clean_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            if str(row["status"]) in {"queued", "discovering"}:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "cancel the active batch before removing it"
                )
            if (
                row["continuation_batch_id"] is not None
                or row["parent_batch_id"] is not None
            ):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "remove the complete batch chain instead"
                )
            items = connection.execute(
                "SELECT code, code_key, status, job_id, selected, quality_status, "
                "available_heights_json, default_height, quality_strategy, "
                "requested_height, quality_error_code, available_variants_json, "
                "variant "
                "FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
            removed_batch = row_to_batch(row, items, self._clock())
            connection.execute(
                "DELETE FROM web_download_batches WHERE batch_id = ?", (clean_id,)
            )
            connection.commit()
        return {**removed_batch, "removed": True}
