"""Saved batch rules."""

from __future__ import annotations

import json

from .errors import (
    WebDownloadBatchConflictError,
    WebDownloadBatchError,
    WebDownloadBatchNotFoundError,
)
from .models import BatchRequest
from .records import row_to_rule
from .store_base import BatchStoreBase
from .validation import (
    optional_rule_revision,
    validate_intent_height,
    validate_item_quality_strategy,
    validate_rule_id,
    validate_rule_name,
    validate_rule_selection,
)


class BatchRuleStoreMixin(BatchStoreBase):
    def save_rule(
        self,
        request: BatchRequest,
        *,
        rule_id: str,
        name: object,
        default_quality_strategy: object = "highest",
        default_height: object | None = None,
        selection_mode: object = "all",
        expected_revision: object | None = None,
    ) -> dict[str, object]:
        clean_id = validate_rule_id(rule_id)
        clean_name = validate_rule_name(name)
        strategy = validate_item_quality_strategy(default_quality_strategy)
        height = (
            request.max_height
            if default_height is None
            else validate_intent_height(default_height, max_height=request.max_height)
        )
        selection = validate_rule_selection(selection_mode)
        expected = optional_rule_revision(expected_revision)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, deleted_at FROM web_download_batch_rules "
                "WHERE rule_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                if expected is not None:
                    connection.rollback()
                    raise WebDownloadBatchConflictError("batch rule revision is stale")
                connection.execute(
                    """
                    INSERT INTO web_download_batch_rules (
                        rule_id, name, mode, code_or_prefix, prefix, suffix_width,
                        start_suffix, end_suffix, max_height, existing_policy,
                        default_quality_strategy, default_height, selection_mode,
                        revision, created_at, updated_at, variant_priority_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        clean_id,
                        clean_name,
                        request.mode,
                        request.code_or_prefix,
                        request.prefix,
                        request.suffix_width,
                        request.start,
                        request.end,
                        request.max_height,
                        request.existing_policy,
                        strategy,
                        height,
                        selection,
                        now,
                        now,
                        json.dumps(request.variant_priority, separators=(",", ":")),
                    ),
                )
            else:
                revision = int(row["revision"])
                if row["deleted_at"] is not None:
                    connection.rollback()
                    raise WebDownloadBatchConflictError(
                        "deleted batch rule identity cannot be reused"
                    )
                if expected is None or expected != revision:
                    connection.rollback()
                    raise WebDownloadBatchConflictError("batch rule revision is stale")
                connection.execute(
                    """
                    UPDATE web_download_batch_rules SET
                        name = ?, mode = ?, code_or_prefix = ?, prefix = ?,
                        suffix_width = ?, start_suffix = ?, end_suffix = ?,
                        max_height = ?, existing_policy = ?,
                        default_quality_strategy = ?, default_height = ?,
                        selection_mode = ?, variant_priority_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE rule_id = ?
                    """,
                    (
                        clean_name,
                        request.mode,
                        request.code_or_prefix,
                        request.prefix,
                        request.suffix_width,
                        request.start,
                        request.end,
                        request.max_height,
                        request.existing_policy,
                        strategy,
                        height,
                        selection,
                        json.dumps(request.variant_priority, separators=(",", ":")),
                        now,
                        clean_id,
                    ),
                )
            saved = connection.execute(
                "SELECT * FROM web_download_batch_rules WHERE rule_id = ?",
                (clean_id,),
            ).fetchone()
            connection.commit()
        if saved is None:
            raise WebDownloadBatchError("batch rule could not be persisted")
        return row_to_rule(saved)

    def get_rule(self, rule_id: str) -> dict[str, object]:
        clean_id = validate_rule_id(rule_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_download_batch_rules "
                "WHERE rule_id = ? AND deleted_at IS NULL",
                (clean_id,),
            ).fetchone()
        if row is None:
            raise WebDownloadBatchNotFoundError("web download batch rule was not found")
        return row_to_rule(row)

    def list_rules(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM web_download_batch_rules "
                "WHERE deleted_at IS NULL "
                "ORDER BY updated_at DESC, rule_id"
            ).fetchall()
        return [row_to_rule(row) for row in rows]

    def remove_rule(
        self,
        rule_id: str,
        *,
        expected_revision: object,
    ) -> dict[str, object]:
        clean_id = validate_rule_id(rule_id)
        expected = optional_rule_revision(expected_revision)
        if expected is None:
            raise WebDownloadBatchConflictError("batch rule revision is required")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM web_download_batch_rules "
                "WHERE rule_id = ? AND deleted_at IS NULL",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError(
                    "web download batch rule was not found"
                )
            if int(row["revision"]) != expected:
                connection.rollback()
                raise WebDownloadBatchConflictError("batch rule revision is stale")
            changed = connection.execute(
                "UPDATE web_download_batch_rules SET deleted_at = ?, updated_at = ?, "
                "revision = revision + 1 WHERE rule_id = ? AND deleted_at IS NULL "
                "AND revision = ?",
                (now, now, clean_id, expected),
            ).rowcount
            connection.commit()
        if changed != 1:
            raise WebDownloadBatchConflictError("batch rule revision is stale")
        return {"rule_id": clean_id, "removed": True, "revision": expected + 1}
