"""Selecting cleanup candidates: filters, matching and batch chains."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from .errors import HistoryLifecycleValidationError
from .fields import (
    bounded_int,
    canonical_bytes,
    canonical_digest,
    code_query_key,
    history_optional_web_variant,
    history_variant_priority,
    history_web_variant,
    optional_code_query,
    optional_int,
    optional_text,
    optional_timestamp,
    sql_slots,
)
from .models import HEX_ID_RE, TASK_STATUS_VALUES, TASK_TYPES, Candidate, Criteria

def parse_criteria(value: Mapping[str, object] | None, *, maximum: int) -> Criteria:
    raw = {} if value is None else value
    if not isinstance(raw, Mapping):
        raise HistoryLifecycleValidationError("history filter is invalid")
    allowed = {
        "task_types",
        "statuses",
        "created_after",
        "created_before",
        "updated_after",
        "updated_before",
        "code",
        "limit",
    }
    if set(raw) - allowed:
        raise HistoryLifecycleValidationError("history filter field is invalid")
    task_types = _task_types(raw.get("task_types"))
    statuses = _status_filters(raw.get("statuses"), task_types)
    created_after = optional_timestamp(raw.get("created_after"))
    created_before = optional_timestamp(raw.get("created_before"))
    updated_after = optional_timestamp(raw.get("updated_after"))
    updated_before = optional_timestamp(raw.get("updated_before"))
    if (
        created_after is not None
        and created_before is not None
        and created_after >= created_before
    ):
        raise HistoryLifecycleValidationError("history created time range is invalid")
    if (
        updated_after is not None
        and updated_before is not None
        and updated_after >= updated_before
    ):
        raise HistoryLifecycleValidationError("history updated time range is invalid")
    return Criteria(
        task_types,
        statuses,
        created_after,
        created_before,
        updated_after,
        updated_before,
        optional_code_query(raw.get("code")),
        bounded_int(raw.get("limit", min(1000, maximum)), "history limit", 1, maximum),
    )


def _task_types(value: object) -> tuple[str, ...]:
    if value is None or value == "all":
        return TASK_TYPES
    raw = (value,) if isinstance(value, str) else value
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        raise HistoryLifecycleValidationError("history task type is invalid")
    result = tuple(dict.fromkeys(str(item).strip().lower() for item in raw))
    if not result or any(item not in TASK_TYPES for item in result):
        raise HistoryLifecycleValidationError("history task type is invalid")
    return tuple(item for item in TASK_TYPES if item in result)


def _status_filters(
    value: object, task_types: Sequence[str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise HistoryLifecycleValidationError("history status filter is invalid")
    if set(value) - set(task_types):
        raise HistoryLifecycleValidationError("history status task type is invalid")
    result: list[tuple[str, tuple[str, ...]]] = []
    for task_type in TASK_TYPES:
        if task_type not in value:
            continue
        raw = value[task_type]
        raw_values = (raw,) if isinstance(raw, str) else raw
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (bytes, bytearray)
        ):
            raise HistoryLifecycleValidationError("history status filter is invalid")
        statuses = tuple(
            dict.fromkeys(str(item).strip().lower() for item in raw_values)
        )
        if not statuses or any(
            item not in TASK_STATUS_VALUES[task_type] for item in statuses
        ):
            raise HistoryLifecycleValidationError("history status filter is invalid")
        result.append((task_type, tuple(sorted(statuses))))
    return tuple(result)


def sql_filter(
    criteria: Criteria,
    task_type: str,
    *,
    column_prefix: str = "",
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    values: list[object] = []
    statuses = criteria.statuses_for(task_type)
    if statuses is not None:
        clauses.append(f"{column_prefix}status IN ({sql_slots(statuses)})")
        values.extend(sorted(statuses))
    for column, value, operator in (
        ("created_at", criteria.created_after, ">="),
        ("created_at", criteria.created_before, "<"),
        ("updated_at", criteria.updated_after, ">="),
        ("updated_at", criteria.updated_before_for(task_type), "<"),
    ):
        if value is not None:
            clauses.append(f"{column_prefix}{column} {operator} ?")
            values.append(value)
    if criteria.code_query is not None:
        column = (
            f"{column_prefix}code_key"
            if task_type != "batch"
            else f"{column_prefix}code_or_prefix"
        )
        clauses.append(
            "REPLACE(REPLACE(REPLACE(REPLACE(UPPER("
            f"{column}), '-', ''), '_', ''), '.', ''), ' ', '') LIKE ?"
        )
        values.append(f"%{criteria.code_query}%")
    return clauses, values


def row_matches(criteria: Criteria, task_type: str, row: sqlite3.Row) -> bool:
    statuses = criteria.statuses_for(task_type)
    if statuses is not None and str(row["status"]) not in statuses:
        return False
    created = float(row["created_at"])
    updated = float(row["updated_at"])
    if criteria.created_after is not None and created < criteria.created_after:
        return False
    if criteria.created_before is not None and created >= criteria.created_before:
        return False
    if criteria.updated_after is not None and updated < criteria.updated_after:
        return False
    before = criteria.updated_before_for(task_type)
    if before is not None and updated >= before:
        return False
    if criteria.code_query is not None:
        value = row["code_key"] if task_type != "batch" else row["code_or_prefix"]
        if criteria.code_query not in code_query_key(str(value)):
            return False
    return True


def batch_matches(criteria: Criteria, rows: Sequence[sqlite3.Row]) -> bool:
    if not rows:
        return False
    statuses = criteria.statuses_for("batch")
    if statuses is not None and any(str(row["status"]) not in statuses for row in rows):
        return False
    created_values = [float(row["created_at"]) for row in rows]
    updated_values = [float(row["updated_at"]) for row in rows]
    if (
        criteria.created_after is not None
        and min(created_values) < criteria.created_after
    ):
        return False
    if (
        criteria.created_before is not None
        and max(created_values) >= criteria.created_before
    ):
        return False
    if (
        criteria.updated_after is not None
        and min(updated_values) < criteria.updated_after
    ):
        return False
    before = criteria.updated_before_for("batch")
    if before is not None and max(updated_values) >= before:
        return False
    if criteria.code_query is not None and not any(
        criteria.code_query in code_query_key(str(row["code_or_prefix"]))
        for row in rows
    ):
        return False
    return True


def web_candidate(row: sqlite3.Row) -> Candidate:
    variant = history_web_variant(row["variant"])
    payload = {
        "job_id": str(row["job_id"]),
        "status": str(row["status"]),
        "code": str(row["code"]),
        "code_key": str(row["code_key"]),
        "variant": variant,
        "requested_height": optional_int(row["requested_height"]),
        "selected_height": optional_int(row["selected_height"]),
        "verified_height": optional_int(row["verified_height"]),
        "quality_strategy": str(row["quality_strategy"]),
        "existing_policy": str(row["existing_policy"]),
        "replaces_job_id": optional_text(row["replaces_job_id"]),
        "superseded_by_job_id": optional_text(row["superseded_by_job_id"]),
        "publication_outcome": optional_text(row["publication_outcome"]),
        "output_path": optional_text(row["output_path"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    return Candidate(
        "web",
        str(row["job_id"]),
        canonical_digest(payload),
        str(row["status"]),
        str(row["code"]),
        variant,
        1,
        len(canonical_bytes(payload)) + 256,
    )


def metadata_candidate(row: sqlite3.Row) -> Candidate:
    variant = history_optional_web_variant(row["variant"])
    payload = {
        "job_id": str(row["job_id"]),
        "kind": str(row["kind"]),
        "download_key": str(row["download_key"]),
        "code": str(row["code"]),
        "code_key": str(row["code_key"]),
        "variant": variant,
        "status": str(row["status"]),
        "relative_media_path": optional_text(row["relative_media_path"]),
        "attempts": int(row["attempts"]),
        "assets_json": str(row["assets_json"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    return Candidate(
        "metadata",
        str(row["job_id"]),
        canonical_digest(payload),
        str(row["status"]),
        str(row["code"]),
        None,
        1,
        len(canonical_bytes(payload)) + 192,
    )


def batch_candidate(
    root_id: str, rows: Sequence[sqlite3.Row], items: Sequence[sqlite3.Row]
) -> Candidate:
    ordered_rows = sorted(rows, key=lambda row: int(row["page_number"]))
    payload = {
        "root_chain_id": root_id,
        "batches": [
            {
                "batch_id": str(row["batch_id"]),
                "status": str(row["status"]),
                "code_or_prefix": str(row["code_or_prefix"]),
                "page_number": int(row["page_number"]),
                "parent_batch_id": optional_text(row["parent_batch_id"]),
                "continuation_batch_id": optional_text(row["continuation_batch_id"]),
                "max_height": int(row["max_height"]),
                "existing_policy": str(row["existing_policy"]),
                "variant_priority": list(
                    history_variant_priority(row["variant_priority_json"])
                ),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in ordered_rows
        ],
        "items": [
            {
                "batch_id": str(row["batch_id"]),
                "position": int(row["position"]),
                "code_key": str(row["code_key"]),
                "variant": history_web_variant(row["variant"]),
                "status": str(row["status"]),
                "job_id": optional_text(row["job_id"]),
                "selected": bool(row["selected"]),
                "quality_status": str(row["quality_status"]),
                "quality_strategy": str(row["quality_strategy"]),
                "requested_height": optional_int(row["requested_height"]),
                "default_height": optional_int(row["default_height"]),
            }
            for row in items
        ],
    }
    statuses = sorted({str(row["status"]) for row in rows})
    code = str(ordered_rows[0]["code_or_prefix"]) if ordered_rows else None
    return Candidate(
        "batch",
        root_id,
        canonical_digest(payload),
        statuses[0] if len(statuses) == 1 else "mixed",
        code,
        None,
        len(rows),
        len(canonical_bytes(payload)) + 256 * (len(rows) + len(items)),
    )


def complete_batch_chain(root_id: str, rows: Sequence[sqlite3.Row]) -> bool:
    if not rows or not HEX_ID_RE.fullmatch(root_id):
        return False
    ordered = sorted(rows, key=lambda row: int(row["page_number"]))
    ids = {str(row["batch_id"]) for row in ordered}
    if len(ids) != len(ordered):
        return False
    for index, row in enumerate(ordered, start=1):
        batch_id = str(row["batch_id"])
        if str(row["root_chain_id"] or "") != root_id:
            return False
        if int(row["page_number"]) != index:
            return False
        expected_parent = None if index == 1 else str(ordered[index - 2]["batch_id"])
        expected_next = (
            None if index == len(ordered) else str(ordered[index]["batch_id"])
        )
        if optional_text(row["parent_batch_id"]) != expected_parent:
            return False
        if optional_text(row["continuation_batch_id"]) != expected_next:
            return False
        if index == 1 and batch_id != root_id:
            return False
        if expected_parent is not None and expected_parent not in ids:
            return False
    return True


def load_batch_chain(
    connection: sqlite3.Connection, schema: str, root_id: str
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    rows = connection.execute(
        f"SELECT * FROM {schema}.web_download_batches "
        "WHERE root_chain_id = ? ORDER BY page_number",
        (root_id,),
    ).fetchall()
    if not rows:
        return [], []
    ids = tuple(str(row["batch_id"]) for row in rows)
    items = connection.execute(
        f"SELECT * FROM {schema}.web_download_batch_items "
        f"WHERE batch_id IN ({sql_slots(ids)}) ORDER BY batch_id, position",
        ids,
    ).fetchall()
    return list(rows), list(items)
