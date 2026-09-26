"""Committing a previewed batch: resolving item intents and creating download jobs."""

from __future__ import annotations

import hmac
from typing import Mapping, Sequence

from ..archives import archived_file_probe_in_root as _archived_file_probe_in_root
from ..jobs import ACTIVE_STATUSES, ARCHIVE_MISSING, ARCHIVE_UNKNOWN, PROVIDER
from ..variant import normalize_web_download_variant
from .errors import (
    WebDownloadBatchConflictError,
    WebDownloadBatchError,
    WebDownloadBatchNotFoundError,
)
from .intents import (
    active_job_matches_batch_intent,
    commit_intent_hash,
    existing_work_height,
    intent_candidate_height,
    resolve_item_intents,
    snapshot_existing_variant,
    snapshot_has_variant,
)
from .library import LibrarySnapshotChanged
from .models import BatchItemIntent, LibraryDeduplicationSnapshot
from .records import (
    batch_from_connection,
    enqueue_batch_terminal_notification,
    fail_bound_rule_preview_locked,
    load_bound_rule_locked,
)
from .store_base import BatchStoreBase
from .validation import (
    batch_idempotency_key,
    batch_item_limit,
    batch_job_id,
    hash_preview_token,
    optional_snapshot_revision,
    validate_batch_existing_policy,
    validate_batch_id,
)


class BatchCommitStoreMixin(BatchStoreBase):
    def commit(
        self,
        batch_id: str,
        preview_token: object,
        *,
        selected_codes: Sequence[object] | None = None,
        item_intents: Sequence[Mapping[str, object]] | None = None,
        library_snapshot: LibraryDeduplicationSnapshot | None = None,
        _automatic: bool = False,
    ) -> tuple[dict[str, object], bool]:
        clean_id = validate_batch_id(batch_id)
        if not isinstance(_automatic, bool):
            raise WebDownloadBatchError("automatic commit state is invalid")
        token_hash = None if _automatic else hash_preview_token(preview_token)
        now = self._clock()
        active_slots = ", ".join("?" for _ in ACTIVE_STATUSES)
        snapshot = library_snapshot or LibraryDeduplicationSnapshot(
            frozenset(), frozenset()
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            if _automatic:
                if not bool(row["auto_commit"]):
                    connection.rollback()
                    raise WebDownloadBatchConflictError(
                        "web download batch is not an automatic download"
                    )
            elif token_hash is None or not hmac.compare_digest(
                str(row["token_hash"]), token_hash
            ):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "preview token is invalid or expired"
                )
            status = str(row["status"])
            if status == "ready":
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
                    raise
            expires_at = row["expires_at"]
            if (
                not _automatic
                and status == "ready"
                and (expires_at is None or float(expires_at) <= now)
            ):
                connection.execute(
                    "UPDATE web_download_batches SET status = 'expired', "
                    "updated_at = ?, error = 'Batch preview expired' WHERE batch_id = ?",
                    (now, clean_id),
                )
                connection.commit()
                raise WebDownloadBatchConflictError(
                    "preview token is invalid or expired"
                )
            if status not in {"ready", "committed"}:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch is not ready to commit"
                )
            count = int(row["discovered_count"])
            if count < 1 or count > batch_item_limit(row):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch has no committable works"
                )
            items = connection.execute(
                "SELECT position, code, code_key, status, selected, quality_status, "
                "available_heights_json, default_height, quality_strategy, "
                "requested_height, quality_error_code, available_variants_json, "
                "variant "
                "FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
            if len(items) != count:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch contents are inconsistent"
                )
            intents = resolve_item_intents(
                selected_codes,
                item_intents,
                items,
                max_height=int(row["max_height"]),
                quality_complete=bool(row["quality_complete"]),
                max_items=batch_item_limit(row),
            )
            selected_code_keys = tuple(intent.code_key for intent in intents)
            intents_by_key = {intent.code_key: intent for intent in intents}
            existing_policy = validate_batch_existing_policy(row["existing_policy"])
            intent_hash = commit_intent_hash(
                f"automatic:{clean_id}" if _automatic else preview_token,
                clean_id,
                existing_policy,
                intents,
            )
            if status == "committed":
                persisted_keys = tuple(
                    str(item["code_key"]) for item in items if bool(item["selected"])
                )
                persisted_intent = row["commit_intent_hash"]
                persisted_item_intents = tuple(
                    BatchItemIntent(
                        str(item["code_key"]),
                        normalize_web_download_variant(item["variant"]),
                        str(item["quality_strategy"]),
                        int(item["requested_height"]),
                    )
                    for item in items
                    if bool(item["selected"]) and item["requested_height"] is not None
                )
                if (
                    persisted_keys != selected_code_keys
                    or persisted_item_intents != intents
                    or (
                        persisted_intent is not None
                        and not hmac.compare_digest(str(persisted_intent), intent_hash)
                    )
                ):
                    connection.rollback()
                    raise WebDownloadBatchConflictError(
                        "web download batch was committed with different selections"
                    )
                if persisted_intent is None:
                    connection.execute(
                        "UPDATE web_download_batches SET commit_intent_hash = ? "
                        "WHERE batch_id = ? AND commit_intent_hash IS NULL",
                        (intent_hash, clean_id),
                    )
                committed = batch_from_connection(connection, clean_id, now)
                connection.commit()
                return committed, False
            if any(str(item["status"]) != "discovered" for item in items):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch contents are inconsistent"
                )
            persisted_revision = optional_snapshot_revision(row["library_revision"])
            if (
                selected_code_keys
                and persisted_revision is not None
                and persisted_revision != snapshot.index_revision
            ):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "media library changed since this preview was created"
                )
            if snapshot.observed_completed_job_ids is not None and selected_code_keys:
                selected_identities = tuple(
                    (intent.code_key, intent.variant) for intent in intents
                )
                selected_identity_filter = ", ".join(
                    "(?, ?)" for _ in selected_identities
                )
                selected_identity_values = tuple(
                    value for identity in selected_identities for value in identity
                )
                current_completed = connection.execute(
                    "SELECT job_id FROM web_download_jobs "
                    "WHERE provider = ? AND status = 'completed' "
                    "AND superseded_by_job_id IS NULL "
                    f"AND (code_key, variant) IN ({selected_identity_filter})",
                    (PROVIDER, *selected_identity_values),
                ).fetchall()
                if any(
                    str(completed["job_id"]) not in snapshot.observed_completed_job_ids
                    for completed in current_completed
                ):
                    connection.rollback()
                    raise LibrarySnapshotChanged(
                        "completed downloads changed while the media library was inspected"
                    )

            selected_set = frozenset(selected_code_keys)
            for item in items:
                code = str(item["code"])
                code_key = str(item["code_key"])
                variant = normalize_web_download_variant(item["variant"])
                if code_key not in selected_set:
                    connection.execute(
                        "UPDATE web_download_batch_items SET selected = 0, "
                        "job_id = NULL WHERE batch_id = ? AND position = ?",
                        (clean_id, int(item["position"])),
                    )
                    continue
                item_intent = intents_by_key[code_key]
                if item_intent.variant != variant:
                    connection.rollback()
                    raise WebDownloadBatchConflictError(
                        "batch item variant changed before commit"
                    )
                existing = connection.execute(
                    "SELECT job_id FROM web_download_jobs "
                    "WHERE provider = ? AND code_key = ? AND variant = ? "
                    "AND status = 'completed' "
                    "AND superseded_by_job_id IS NULL "
                    "ORDER BY created_at DESC, job_id DESC LIMIT 1",
                    (PROVIDER, code_key, variant),
                ).fetchone()
                existing_work = snapshot_existing_variant(
                    snapshot,
                    code_key,
                    variant,
                )
                library_hit = snapshot_has_variant(snapshot, code_key, variant)
                completed_hit = (
                    existing is not None
                    and str(existing["job_id"]) in snapshot.completed_job_ids
                )
                if (
                    (library_hit or completed_hit)
                    and existing_work is not None
                    and snapshot.resolved_library_root is not None
                ):
                    archive_status = _archived_file_probe_in_root(
                        snapshot.resolved_library_root,
                        existing_work.output_path,
                    )[0]
                    if archive_status == ARCHIVE_UNKNOWN:
                        connection.rollback()
                        raise LibrarySnapshotChanged(
                            "media library could not be verified before commit"
                        )
                    if archive_status == ARCHIVE_MISSING:
                        library_hit = False
                        completed_hit = False
                        existing_work = None
                known_height = existing_work_height(existing_work)
                intended_height = intent_candidate_height(item, item_intent)
                skip_existing = (
                    existing_policy == "skip" and (completed_hit or library_hit)
                ) or (
                    existing_policy == "higher_quality"
                    and (completed_hit or library_hit)
                    and known_height is not None
                    and known_height >= intended_height
                )
                if skip_existing:
                    item_status = "skipped_completed"
                    job_id = (
                        existing_work.job_id
                        if existing_work is not None
                        and existing_work.job_id is not None
                        else (
                            str(existing["job_id"])
                            if completed_hit and existing is not None
                            else None
                        )
                    )
                else:
                    requested_height = item_intent.requested_height
                    quality_strategy = item_intent.quality_strategy
                    existing = connection.execute(
                        "SELECT job_id, requested_height, quality_strategy, "
                        "existing_policy, incumbent_output_path, replaces_job_id "
                        "FROM web_download_jobs WHERE provider = ? "
                        f"AND code_key = ? AND variant = ? "
                        f"AND status IN ({active_slots}) "
                        "ORDER BY created_at, job_id LIMIT 1",
                        (PROVIDER, code_key, variant, *ACTIVE_STATUSES),
                    ).fetchone()
                    if existing is not None:
                        if not _automatic and not active_job_matches_batch_intent(
                            existing,
                            intent_height=requested_height,
                            quality_strategy=quality_strategy,
                            existing_policy=existing_policy,
                            existing_work=existing_work,
                        ):
                            connection.rollback()
                            raise WebDownloadBatchConflictError(
                                f"{code} already has an active download with a different intent"
                            )
                        item_status = "reused"
                        job_id = str(existing["job_id"])
                    else:
                        job_id = batch_job_id(
                            clean_id,
                            code_key,
                            variant,
                            requested_height,
                            quality_strategy,
                        )
                        idempotency_key = batch_idempotency_key(
                            clean_id,
                            code_key,
                            variant,
                            requested_height,
                            quality_strategy,
                        )
                        connection.execute(
                            """
                            INSERT INTO web_download_jobs (
                                job_id, provider, code, code_key, idempotency_key,
                                variant,
                                requested_height, selected_height, quality_strategy,
                                existing_policy, incumbent_output_path,
                                replaces_job_id,
                                status, progress, downloaded_bytes, total_bytes,
                                speed, eta, created_at, updated_at, error, output_path
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?,
                                      'queued', 0, 0, NULL, 0, NULL, ?, ?, NULL,
                                      NULL)
                            """,
                            (
                                job_id,
                                PROVIDER,
                                code,
                                code_key,
                                idempotency_key,
                                variant,
                                requested_height,
                                quality_strategy,
                                existing_policy,
                                (
                                    existing_work.output_path
                                    if existing_work is not None
                                    else None
                                ),
                                (
                                    existing_work.job_id
                                    if existing_work is not None
                                    else None
                                ),
                                now,
                                now,
                            ),
                        )
                        item_status = "created"
                connection.execute(
                    "UPDATE web_download_batch_items SET status = ?, job_id = ?, "
                    "selected = 1, quality_strategy = ?, requested_height = ? "
                    "WHERE batch_id = ? AND position = ?",
                    (
                        item_status,
                        job_id,
                        item_intent.quality_strategy,
                        item_intent.requested_height,
                        clean_id,
                        int(item["position"]),
                    ),
                )
            changed = connection.execute(
                "UPDATE web_download_batches SET status = 'committed', updated_at = ?, "
                "error = NULL, commit_intent_hash = ? "
                "WHERE batch_id = ? AND status = 'ready'",
                (now, intent_hash, clean_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch could not be committed"
                )
            enqueue_batch_terminal_notification(
                connection,
                clean_id,
                status="completed",
                code=(str(row["code_or_prefix"]) if _automatic else None),
                occurred_at=now,
                clock=self._clock,
            )
            committed = batch_from_connection(connection, clean_id, now)
            connection.commit()
        return committed, True

    def commit_requires_library_snapshot(
        self,
        batch_id: str,
        preview_token: object,
        selected_codes: Sequence[object] | None = None,
        item_intents: Sequence[Mapping[str, object]] | None = None,
    ) -> bool:
        clean_id = validate_batch_id(batch_id)
        token_hash = hash_preview_token(preview_token)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token_hash, status, expires_at, max_height, "
                "quality_complete, provenance_type FROM web_download_batches "
                "WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            items = connection.execute(
                "SELECT code, code_key, selected, quality_status, available_heights_json, "
                "quality_strategy, requested_height, variant "
                "FROM web_download_batch_items "
                "WHERE batch_id = ? ORDER BY position",
                (clean_id,),
            ).fetchall()
        if row is None:
            raise WebDownloadBatchNotFoundError("web download batch was not found")
        if not hmac.compare_digest(str(row["token_hash"]), token_hash):
            raise WebDownloadBatchConflictError("preview token is invalid or expired")
        intents = resolve_item_intents(
            selected_codes,
            item_intents,
            items,
            max_height=int(row["max_height"]),
            quality_complete=bool(row["quality_complete"]),
            max_items=batch_item_limit(row),
        )
        expires_at = row["expires_at"]
        return (
            str(row["status"]) == "ready"
            and expires_at is not None
            and float(expires_at) > self._clock()
            and bool(intents)
        )
