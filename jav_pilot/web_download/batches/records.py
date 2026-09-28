"""Stored batch rows: decoding, bound rule previews, expiry and terminal notifications."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, Sequence

from ..jobs import normalize_web_download_code
from ...notifications.events import completed_event, failed_event
from ...notifications.outbox import enqueue_notification_event
from ..variant import normalize_web_download_variant
from .errors import (
    AUTO_DISCOVERY_FAILURE_MESSAGES,
    WebDownloadBatchConflictError,
    WebDownloadBatchError,
    WebDownloadBatchNotFoundError,
)
from .validation import (
    decode_quality_heights,
    optional_rule_revision,
    validate_rule_id,
    variant_priority_from_json,
    variants_from_json,
)

PREVIEW_TTL_SECONDS = 15 * 60


def load_bound_rule_locked(
    connection: sqlite3.Connection,
    rule_id: object | None,
    rule_revision: object | None,
) -> sqlite3.Row | None:
    if rule_id is None and rule_revision is None:
        return None
    if rule_id is None or rule_revision is None:
        raise WebDownloadBatchConflictError(
            "batch rule binding is missing; recreate the preview"
        )
    clean_id = validate_rule_id(rule_id)
    clean_revision = optional_rule_revision(rule_revision)
    if clean_revision is None:
        raise WebDownloadBatchConflictError(
            "batch rule binding is missing; recreate the preview"
        )
    row = connection.execute(
        "SELECT * FROM web_download_batch_rules WHERE rule_id = ?",
        (clean_id,),
    ).fetchone()
    if (
        row is None
        or row["deleted_at"] is not None
        or int(row["revision"]) != clean_revision
    ):
        raise WebDownloadBatchConflictError(
            "batch rule changed before preview completion"
        )
    return row


def fail_bound_rule_preview_locked(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    now: float,
    clock: Callable[[], float],
) -> bool:
    changed = connection.execute(
        "UPDATE web_download_batches SET status = 'failed', "
        "quality_complete = 1, updated_at = ?, expires_at = NULL, "
        "error = 'Batch rule changed before preview completion' "
        "WHERE batch_id = ? AND status IN ('queued', 'discovering', 'ready')",
        (now, batch_id),
    ).rowcount
    if changed == 1:
        enqueue_batch_terminal_notification(
            connection,
            batch_id,
            status="failed",
            occurred_at=now,
            clock=clock,
        )
    return changed == 1


def materialize_expired_web_download_batches(
    database_path: Path | str,
    *,
    clock: Callable[[], float] = time.time,
) -> tuple[str, ...]:
    path = Path(database_path)
    if not path.is_absolute() or not path.is_file():
        return ()
    with closing(
        sqlite3.connect(path, timeout=30.0, isolation_level=None)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        expired = expire_ready_rows(connection, now=clock())
        connection.commit()
    return expired


def expire_ready_rows(
    connection: sqlite3.Connection,
    *,
    now: float,
    batch_id: str | None = None,
) -> tuple[str, ...]:
    clauses = [
        "status = 'ready'",
        "quality_complete = 1",
        "expires_at IS NOT NULL",
        "expires_at <= ?",
    ]
    values: list[object] = [now]
    if batch_id is not None:
        clauses.append("batch_id = ?")
        values.append(batch_id)
    where = " AND ".join(clauses)
    rows = connection.execute(
        f"SELECT batch_id FROM web_download_batches WHERE {where} ORDER BY batch_id",
        tuple(values),
    ).fetchall()
    changed = connection.execute(
        "UPDATE web_download_batches SET status = 'expired', updated_at = ?, "
        f"error = 'Batch preview expired' WHERE {where}",
        (now, *values),
    ).rowcount
    if changed != len(rows):
        raise WebDownloadBatchConflictError(
            "expiring web download batches changed concurrently"
        )
    return tuple(str(row["batch_id"]) for row in rows)


def enqueue_batch_terminal_notification(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    status: str,
    code: str | None = None,
    error_code: str = "batch_failed",
    occurred_at: float,
    clock: Callable[[], float],
) -> bool:
    notification_code = None if code is None else normalize_web_download_code(code)[0]
    if status == "completed":
        event = completed_event(
            source="web_download_batch",
            entity_id=batch_id,
            code=notification_code,
            occurred_at=occurred_at,
        )
    elif status == "failed":
        if error_code not in {
            "batch_failed",
            "discovery_failed",
            "not_found",
            "queue_failed",
        } and error_code not in AUTO_DISCOVERY_FAILURE_MESSAGES:
            raise WebDownloadBatchError(
                "batch terminal notification error code is invalid"
            )
        event = failed_event(
            source="web_download_batch",
            entity_id=batch_id,
            code=notification_code,
            error_code=error_code,
            occurred_at=occurred_at,
        )
    else:
        raise WebDownloadBatchError("batch terminal notification status is invalid")
    return enqueue_notification_event(connection, event, clock=clock)


def row_to_rule(row: sqlite3.Row) -> dict[str, object]:
    return {
        "rule_id": str(row["rule_id"]),
        "name": str(row["name"]),
        "mode": str(row["mode"]),
        "code_or_prefix": str(row["code_or_prefix"]),
        "prefix": str(row["prefix"]),
        "suffix_width": (
            int(row["suffix_width"]) if row["suffix_width"] is not None else None
        ),
        "start": (
            str(row["start_suffix"]) if row["start_suffix"] is not None else None
        ),
        "end": str(row["end_suffix"]) if row["end_suffix"] is not None else None,
        "max_height": int(row["max_height"]),
        "existing_policy": str(row["existing_policy"]),
        "variant_priority": list(
            variant_priority_from_json(row["variant_priority_json"])
        ),
        "default_quality_strategy": str(row["default_quality_strategy"]),
        "default_height": (
            int(row["default_height"]) if row["default_height"] is not None else None
        ),
        "selection_mode": str(row["selection_mode"]),
        "revision": int(row["revision"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def row_to_batch(
    row: sqlite3.Row,
    item_rows: Sequence[sqlite3.Row],
    now: float,
) -> dict[str, object]:
    status = str(row["status"])
    auto_commit = bool(row["auto_commit"])
    items = [
        {
            "code": str(item["code"]),
            "variant": normalize_web_download_variant(item["variant"]),
            "available_variants": list(
                variants_from_json(item["available_variants_json"])
            ),
            "status": str(item["status"]),
            "job_id": str(item["job_id"]) if item["job_id"] is not None else None,
            "selected": bool(item["selected"]),
            "quality_status": str(item["quality_status"]),
            "available_heights": list(
                decode_quality_heights(item["available_heights_json"])
            ),
            "default_height": (
                int(item["default_height"])
                if item["default_height"] is not None
                else None
            ),
            "quality_strategy": str(item["quality_strategy"]),
            "requested_height": (
                int(item["requested_height"])
                if item["requested_height"] is not None
                else None
            ),
            "quality_error_code": (
                str(item["quality_error_code"])
                if item["quality_error_code"] is not None
                else None
            ),
        }
        for item in item_rows
    ]
    created_count = sum(item["status"] == "created" for item in items)
    reused_count = sum(item["status"] == "reused" for item in items)
    skipped_count = sum(item["status"] == "skipped_completed" for item in items)
    selected_count = sum(bool(item["selected"]) for item in items)
    committable_count = sum(
        item["quality_status"] in {"ready", "legacy"} for item in items
    )
    expires_at = float(row["expires_at"]) if row["expires_at"] is not None else None
    count = int(row["discovered_count"])
    next_start = (
        str(row["next_start_suffix"]) if row["next_start_suffix"] is not None else None
    )
    continuation_batch_id = (
        str(row["continuation_batch_id"])
        if row["continuation_batch_id"] is not None
        else None
    )
    return {
        "batch_id": str(row["batch_id"]),
        "root_chain_id": str(row["root_chain_id"]),
        "status": status,
        "mode": str(row["mode"]),
        "code_or_prefix": str(row["code_or_prefix"]),
        "prefix": str(row["prefix"]),
        "suffix_width": (
            int(row["suffix_width"]) if row["suffix_width"] is not None else None
        ),
        "start": str(row["start_suffix"]) if row["start_suffix"] is not None else None,
        "end": str(row["end_suffix"]) if row["end_suffix"] is not None else None,
        "max_height": int(row["max_height"]),
        "existing_policy": str(row["existing_policy"]),
        "variant_priority": list(
            variant_priority_from_json(row["variant_priority_json"])
        ),
        "provenance_type": str(row["provenance_type"]),
        "auto_commit": auto_commit,
        "source_session_id": (
            str(row["source_session_id"])
            if row["source_session_id"] is not None
            else None
        ),
        "source_revision": (
            int(row["source_revision"]) if row["source_revision"] is not None else None
        ),
        "source_items_hash": (
            str(row["source_items_hash"])
            if row["source_items_hash"] is not None
            else None
        ),
        "page": int(row["page_number"]),
        "page_budget": int(row["page_budget"]),
        "limit_reached": bool(row["limit_reached"]),
        "resume_start": next_start if bool(row["limit_reached"]) else None,
        "quality_complete": bool(row["quality_complete"]),
        "rule_id": str(row["rule_id"]) if row["rule_id"] is not None else None,
        "rule_revision": (
            int(row["rule_revision"]) if row["rule_revision"] is not None else None
        ),
        "count": count,
        "created_count": created_count,
        "reused_count": reused_count,
        "skipped_count": skipped_count,
        "selected_count": selected_count,
        "excluded_count": count - selected_count,
        "complete": (
            bool(row["discovery_complete"])
            if row["discovery_complete"] is not None
            else None
        ),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "expires_at": expires_at,
        "expires_in": (
            max(0, int(expires_at - now)) if expires_at is not None else None
        ),
        "error": str(row["error"]) if row["error"] is not None else None,
        "has_more": next_start is not None,
        "continuation_batch_id": continuation_batch_id,
        "can_cancel": status not in {"committed", "cancelled"},
        "can_retry": status in {"failed", "incomplete", "too_many"}
        or (status == "cancelled" and auto_commit),
        "can_commit": (
            status == "ready"
            and not auto_commit
            and committable_count > 0
            and bool(row["quality_complete"])
        ),
        "can_continue": (
            status == "committed"
            and next_start is not None
            and continuation_batch_id is None
            and not bool(row["limit_reached"])
            and int(row["page_number"]) < int(row["page_budget"])
        ),
        "can_remove": (
            status not in {"queued", "discovering"}
            and row["parent_batch_id"] is None
            and continuation_batch_id is None
        ),
        "items": items,
    }


def batch_from_connection(
    connection: sqlite3.Connection,
    batch_id: str,
    now: float,
) -> dict[str, object]:
    row = connection.execute(
        "SELECT * FROM web_download_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise WebDownloadBatchNotFoundError("web download batch was not found")
    items = connection.execute(
        "SELECT code, code_key, status, job_id, selected, quality_status, "
        "available_heights_json, default_height, quality_strategy, "
        "requested_height, quality_error_code, available_variants_json, variant "
        "FROM web_download_batch_items "
        "WHERE batch_id = ? ORDER BY position",
        (batch_id,),
    ).fetchall()
    return row_to_batch(row, items, now)


def replacement_revision_timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise WebDownloadBatchError("web download replacement revision is invalid")
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebDownloadBatchError(
            "web download replacement revision is invalid"
        ) from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise WebDownloadBatchError("web download replacement revision is invalid")
    return timestamp
