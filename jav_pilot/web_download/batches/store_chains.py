"""Batch chains: continuation summaries, retries, cancellation and export."""

from __future__ import annotations

import hmac
import sqlite3
from typing import Sequence

from ..variant import normalize_web_download_variant
from .errors import WebDownloadBatchConflictError, WebDownloadBatchNotFoundError
from .models import (
    ABSOLUTE_MAX_BATCH_PAGES,
    DEFAULT_BATCH_PAGE_BUDGET,
    RESOURCE_SEARCH_SELECTION_PROVENANCE,
)
from .records import batch_from_connection, load_bound_rule_locked
from .store_base import BatchStoreBase
from .validation import (
    bounded_batch_int,
    hash_preview_token,
    validate_batch_id,
    variant_priority_from_json,
    verify_selected_batch_provenance,
)


class BatchChainStoreMixin(BatchStoreBase):
    def _validate_chain_locked(
        self,
        connection: sqlite3.Connection,
        root_chain_id: str,
    ) -> list[sqlite3.Row]:
        root = connection.execute(
            "SELECT * FROM web_download_batches WHERE batch_id = ?",
            (root_chain_id,),
        ).fetchone()
        if root is None:
            raise WebDownloadBatchNotFoundError(
                "web download batch chain was not found"
            )
        rows = connection.execute(
            "SELECT * FROM web_download_batches WHERE root_chain_id = ? "
            "ORDER BY page_number, created_at, batch_id",
            (root_chain_id,),
        ).fetchall()
        if not rows or len(rows) > ABSOLUTE_MAX_BATCH_PAGES:
            raise WebDownloadBatchConflictError(
                "web download batch chain is inconsistent"
            )
        invariant_fields = (
            "token_hash",
            "code_or_prefix",
            "prefix",
            "suffix_width",
            "end_suffix",
            "max_height",
            "existing_policy",
            "page_budget",
            "rule_id",
            "rule_revision",
        )
        previous: sqlite3.Row | None = None
        seen: set[str] = set()
        for page, row in enumerate(rows, start=1):
            batch_id = str(row["batch_id"])
            if (
                batch_id in seen
                or str(row["root_chain_id"]) != root_chain_id
                or int(row["page_number"]) != page
                or any(row[field] != root[field] for field in invariant_fields)
            ):
                raise WebDownloadBatchConflictError(
                    "web download batch chain is inconsistent"
                )
            seen.add(batch_id)
            parent_id = (
                str(row["parent_batch_id"])
                if row["parent_batch_id"] is not None
                else None
            )
            if previous is None:
                if (
                    batch_id != root_chain_id
                    or parent_id is not None
                    or int(row["page_number"]) != 1
                ):
                    raise WebDownloadBatchConflictError(
                        "web download batch chain is inconsistent"
                    )
            else:
                previous_id = str(previous["batch_id"])
                previous_continuation = (
                    str(previous["continuation_batch_id"])
                    if previous["continuation_batch_id"] is not None
                    else None
                )
                expected_start = (
                    str(previous["next_start_suffix"])
                    if previous["next_start_suffix"] is not None
                    else None
                )
                actual_start = (
                    str(row["start_suffix"])
                    if row["start_suffix"] is not None
                    else None
                )
                if (
                    parent_id != previous_id
                    or previous_continuation != batch_id
                    or expected_start is None
                    or actual_start != expected_start
                ):
                    raise WebDownloadBatchConflictError(
                        "web download batch chain is inconsistent"
                    )
            previous = row

        last = rows[-1]
        continuation = (
            str(last["continuation_batch_id"])
            if last["continuation_batch_id"] is not None
            else None
        )
        if continuation is not None:
            linked = connection.execute(
                "SELECT root_chain_id, parent_batch_id FROM web_download_batches "
                "WHERE batch_id = ?",
                (continuation,),
            ).fetchone()
            if (
                linked is None
                or str(linked["root_chain_id"]) != root_chain_id
                or str(linked["parent_batch_id"]) != str(last["batch_id"])
            ):
                raise WebDownloadBatchConflictError(
                    "web download batch chain is inconsistent"
                )
            raise WebDownloadBatchConflictError(
                "web download batch chain is inconsistent"
            )
        return list(rows)

    def _chain_summary_locked(
        self,
        connection: sqlite3.Connection,
        root_chain_id: str,
        *,
        rows: Sequence[sqlite3.Row] | None = None,
    ) -> dict[str, object]:
        chain_rows = (
            list(rows)
            if rows is not None
            else self._validate_chain_locked(connection, root_chain_id)
        )
        if not chain_rows:
            raise WebDownloadBatchNotFoundError(
                "web download batch chain was not found"
            )
        item_counts = connection.execute(
            "SELECT COUNT(*) AS discovered_count, "
            "SUM(CASE WHEN i.selected = 1 THEN 1 ELSE 0 END) AS selected_count, "
            "SUM(CASE WHEN i.status = 'created' THEN 1 ELSE 0 END) AS created_count, "
            "SUM(CASE WHEN i.status = 'reused' THEN 1 ELSE 0 END) AS reused_count, "
            "SUM(CASE WHEN i.status = 'skipped_completed' THEN 1 ELSE 0 END) "
            "AS skipped_count FROM web_download_batch_items i "
            "JOIN web_download_batches b ON b.batch_id = i.batch_id "
            "WHERE b.root_chain_id = ?",
            (root_chain_id,),
        ).fetchone()
        statuses = tuple(str(row["status"]) for row in chain_rows)
        if any(status == "discovering" for status in statuses):
            aggregate_status = "discovering"
        elif any(status == "queued" for status in statuses):
            aggregate_status = "queued"
        elif any(status == "failed" for status in statuses):
            aggregate_status = "failed"
        elif any(status == "incomplete" for status in statuses):
            aggregate_status = "incomplete"
        elif any(status == "too_many" for status in statuses):
            aggregate_status = "too_many"
        elif all(status == "committed" for status in statuses):
            aggregate_status = "committed"
        elif all(status == "cancelled" for status in statuses):
            aggregate_status = "cancelled"
        else:
            aggregate_status = statuses[-1]
        root = chain_rows[0]
        last = chain_rows[-1]
        resume_start = next(
            (
                str(row["next_start_suffix"])
                for row in reversed(chain_rows)
                if bool(row["limit_reached"]) and row["next_start_suffix"] is not None
            ),
            None,
        )
        failed_pages = [
            int(row["page_number"])
            for row in chain_rows
            if str(row["status"]) in {"failed", "incomplete", "too_many"}
        ]
        failed_cursors = [
            {
                "batch_id": str(row["batch_id"]),
                "page": int(row["page_number"]),
                "start": (
                    str(row["start_suffix"])
                    if row["start_suffix"] is not None
                    else None
                ),
                "status": str(row["status"]),
            }
            for row in chain_rows
            if str(row["status"]) in {"failed", "incomplete", "too_many"}
        ]
        return {
            "root_chain_id": root_chain_id,
            "status": aggregate_status,
            "code_or_prefix": str(root["code_or_prefix"]),
            "prefix": str(root["prefix"]),
            "start": (
                str(root["start_suffix"]) if root["start_suffix"] is not None else None
            ),
            "end": (
                str(root["end_suffix"]) if root["end_suffix"] is not None else None
            ),
            "max_height": int(root["max_height"]),
            "existing_policy": str(root["existing_policy"]),
            "page_budget": int(root["page_budget"]),
            "pages_scanned": len(chain_rows),
            "last_page": int(last["page_number"]),
            "discovered_count": int(item_counts["discovered_count"] or 0),
            "selected_count": int(item_counts["selected_count"] or 0),
            "created_count": int(item_counts["created_count"] or 0),
            "reused_count": int(item_counts["reused_count"] or 0),
            "skipped_count": int(item_counts["skipped_count"] or 0),
            "failed_count": len(failed_cursors),
            "failed_pages": failed_pages,
            "failed_cursors": failed_cursors,
            "limit_reached": any(bool(row["limit_reached"]) for row in chain_rows),
            "resume_start": resume_start,
            "can_continue": (
                str(last["status"]) == "committed"
                and last["next_start_suffix"] is not None
                and not bool(last["limit_reached"])
                and int(last["page_number"]) < int(last["page_budget"])
            ),
            "created_at": float(root["created_at"]),
            "updated_at": max(float(row["updated_at"]) for row in chain_rows),
            "rule_id": str(root["rule_id"]) if root["rule_id"] is not None else None,
            "rule_revision": (
                int(root["rule_revision"])
                if root["rule_revision"] is not None
                else None
            ),
        }

    def list_chains(self, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        clean_limit = bounded_batch_int(limit, "limit", 1, 200)
        clean_offset = bounded_batch_int(offset, "offset", 0, 10_000_000)
        self.expire_ready()
        with self._connect() as connection:
            count_row = connection.execute(
                "SELECT COUNT(*) FROM web_download_batches "
                "WHERE batch_id = root_chain_id AND parent_batch_id IS NULL"
            ).fetchone()
            roots = connection.execute(
                "SELECT root_chain_id FROM web_download_batches "
                "WHERE batch_id = root_chain_id AND parent_batch_id IS NULL "
                "ORDER BY created_at DESC, batch_id DESC LIMIT ? OFFSET ?",
                (clean_limit, clean_offset),
            ).fetchall()
            chains = [
                self._chain_summary_locked(connection, str(row["root_chain_id"]))
                for row in roots
            ]
        count = int(count_row[0] if count_row is not None else 0)
        return {
            "chains": chains,
            "count": count,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": clean_offset + len(chains) < count,
        }

    def get_chain(
        self,
        root_chain_id: str,
        *,
        page_limit: int = DEFAULT_BATCH_PAGE_BUDGET,
        page_offset: int = 0,
    ) -> dict[str, object]:
        clean_root = validate_batch_id(root_chain_id)
        clean_limit = bounded_batch_int(
            page_limit, "page_limit", 1, ABSOLUTE_MAX_BATCH_PAGES
        )
        clean_offset = bounded_batch_int(
            page_offset, "page_offset", 0, ABSOLUTE_MAX_BATCH_PAGES
        )
        self.expire_ready()
        with self._connect() as connection:
            rows = self._validate_chain_locked(connection, clean_root)
            selected = rows[clean_offset : clean_offset + clean_limit]
            pages = [
                batch_from_connection(connection, str(row["batch_id"]), self._clock())
                for row in selected
            ]
            summary = self._chain_summary_locked(connection, clean_root, rows=rows)
        return {
            **summary,
            "pages": pages,
            "page_count": len(rows),
            "page_limit": clean_limit,
            "page_offset": clean_offset,
            "has_more_pages": clean_offset + len(pages) < len(rows),
        }

    def retry_failed(
        self, batch_id: str, preview_token: object
    ) -> tuple[dict[str, object], bool]:
        clean_id = validate_batch_id(batch_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_download_batches WHERE batch_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WebDownloadBatchNotFoundError("web download batch was not found")
            if not bool(row["auto_commit"]) and not hmac.compare_digest(
                str(row["token_hash"]),
                hash_preview_token(preview_token),
            ):
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "preview token is invalid or expired"
                )
            status = str(row["status"])
            if status in {"queued", "discovering"}:
                batch = batch_from_connection(connection, clean_id, self._clock())
                connection.commit()
                return batch, False
            if status not in {"failed", "incomplete", "too_many", "cancelled"}:
                connection.rollback()
                raise WebDownloadBatchConflictError(
                    "web download batch is not retryable"
                )
            load_bound_rule_locked(
                connection,
                row["rule_id"],
                row["rule_revision"],
            )
            if str(row["provenance_type"]) == RESOURCE_SEARCH_SELECTION_PROVENANCE:
                items = connection.execute(
                    "SELECT code, code_key, available_variants_json, variant, "
                    "quality_status FROM web_download_batch_items "
                    "WHERE batch_id = ? ORDER BY position",
                    (clean_id,),
                ).fetchall()
                verify_selected_batch_provenance(row, items)
                quality_pending = any(
                    str(item["quality_status"]) == "pending" for item in items
                )
                changed = connection.execute(
                    "UPDATE web_download_batches SET status = 'queued', "
                    "quality_complete = ?, updated_at = ?, expires_at = NULL, "
                    "error = NULL WHERE batch_id = ? AND status = ?",
                    (int(not quality_pending), self._clock(), clean_id, status),
                ).rowcount
            else:
                connection.execute(
                    "DELETE FROM web_download_batch_items WHERE batch_id = ?",
                    (clean_id,),
                )
                changed = connection.execute(
                    "UPDATE web_download_batches SET status = 'queued', "
                    "discovered_count = 0, discovery_complete = NULL, "
                    "quality_complete = 1, limit_reached = 0, "
                    "next_start_suffix = NULL, updated_at = ?, expires_at = NULL, "
                    "error = NULL WHERE batch_id = ? AND status = ?",
                    (self._clock(), clean_id, status),
                ).rowcount
            batch = batch_from_connection(connection, clean_id, self._clock())
            connection.commit()
        return batch, changed == 1

    def cancel_chain(
        self, root_chain_id: str
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        clean_root = validate_batch_id(root_chain_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._validate_chain_locked(connection, clean_root)
            cancellable = tuple(
                str(row["batch_id"])
                for row in rows
                if str(row["status"]) not in {"committed", "cancelled"}
            )
            now = self._clock()
            for batch_id in cancellable:
                connection.execute(
                    "UPDATE web_download_batches SET status = 'cancelled', "
                    "updated_at = ?, expires_at = NULL, error = NULL "
                    "WHERE batch_id = ? AND status != 'committed'",
                    (now, batch_id),
                )
            summary = self._chain_summary_locked(connection, clean_root)
            connection.commit()
        return summary, cancellable

    def export_chain(self, root_chain_id: str) -> dict[str, object]:
        clean_root = validate_batch_id(root_chain_id)
        with self._connect() as connection:
            rows = self._validate_chain_locked(connection, clean_root)
            pages: list[dict[str, object]] = []
            for row in rows:
                items = connection.execute(
                    "SELECT code, selected, quality_strategy, requested_height, "
                    "variant "
                    "FROM web_download_batch_items WHERE batch_id = ? "
                    "ORDER BY position",
                    (str(row["batch_id"]),),
                ).fetchall()
                pages.append(
                    {
                        "batch_id": str(row["batch_id"]),
                        "page": int(row["page_number"]),
                        "status": str(row["status"]),
                        "start": (
                            str(row["start_suffix"])
                            if row["start_suffix"] is not None
                            else None
                        ),
                        "next_start": (
                            str(row["next_start_suffix"])
                            if row["next_start_suffix"] is not None
                            else None
                        ),
                        "items": [
                            {
                                "code": str(item["code"]),
                                "variant": normalize_web_download_variant(
                                    item["variant"]
                                ),
                                "selected": bool(item["selected"]),
                                "quality_strategy": str(item["quality_strategy"]),
                                "requested_height": (
                                    int(item["requested_height"])
                                    if item["requested_height"] is not None
                                    else None
                                ),
                            }
                            for item in items
                        ],
                    }
                )
            root = rows[0]
        return {
            "schema": "jav-pilot-web-batch-chain/v1",
            "root_chain_id": clean_root,
            "prefix": str(root["prefix"]),
            "suffix_width": (
                int(root["suffix_width"]) if root["suffix_width"] is not None else None
            ),
            "start": (
                str(root["start_suffix"]) if root["start_suffix"] is not None else None
            ),
            "end": (
                str(root["end_suffix"]) if root["end_suffix"] is not None else None
            ),
            "page_budget": int(root["page_budget"]),
            "variant_priority": list(
                variant_priority_from_json(root["variant_priority_json"])
            ),
            "rule_id": str(root["rule_id"]) if root["rule_id"] is not None else None,
            "rule_revision": (
                int(root["rule_revision"])
                if root["rule_revision"] is not None
                else None
            ),
            "resume_start": next(
                (
                    str(row["next_start_suffix"])
                    for row in reversed(rows)
                    if bool(row["limit_reached"])
                    and row["next_start_suffix"] is not None
                ),
                None,
            ),
            "pages": pages,
        }
