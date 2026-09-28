"""Recovery facts kept for removed history and their digests."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence

from .errors import HistoryLifecycleConflictError, HistoryLifecycleValidationError
from .fields import canonical_digest, optional_int, optional_text, timestamp
from .models import (
    HISTORY_CLEANUP_OPERATION_REVISION,
    SAFE_FACT_NAME_RE,
    SENSITIVE_KEY_RE,
    SENSITIVE_VALUE_RE,
    Candidate,
)

def metadata_nfo_provenance(row: sqlite3.Row) -> dict[str, str]:
    try:
        assets = json.loads(str(row["assets_json"] or "{}"))
    except (TypeError, ValueError) as exc:
        raise HistoryLifecycleConflictError("metadata provenance is invalid") from exc
    if not isinstance(assets, dict):
        raise HistoryLifecycleConflictError("metadata provenance is invalid")
    result: dict[str, str] = {}
    for raw_name, raw_value in assets.items():
        if not isinstance(raw_name, str) or not raw_name.lower().endswith(".nfo"):
            continue
        if not SAFE_FACT_NAME_RE.fullmatch(raw_name) or SENSITIVE_KEY_RE.search(
            raw_name
        ):
            raise HistoryLifecycleConflictError("metadata provenance is unsafe")
        status_value = raw_value.get("status") if isinstance(raw_value, dict) else None
        if status_value == "generated":
            result[raw_name] = "generated"
        elif status_value == "existing":
            result[raw_name] = "tracked_existing"
    return dict(sorted(result.items()))


def make_fact(
    source_type: str,
    source_id: str,
    *,
    code: str | None,
    code_key: str | None,
    relative_media_path: str | None,
    quality_height: int | None,
    nfo_provenance: Mapping[str, str] | None,
    replaces_source_id: str | None,
    superseded_by_source_id: str | None,
    publication_outcome: str | None,
    details: Mapping[str, object],
    source_created_at: float,
    archived_at: float,
) -> dict[str, object]:
    payload = {
        "source_type": source_type,
        "source_id": source_id,
        "code": code,
        "code_key": code_key,
        "relative_media_path": relative_media_path,
        "quality_height": quality_height,
        "nfo_provenance": [
            {"name": name, "provenance": provenance}
            for name, provenance in sorted((nfo_provenance or {}).items())
        ],
        "replaces_source_id": replaces_source_id,
        "superseded_by_source_id": superseded_by_source_id,
        "publication_outcome": publication_outcome,
        "details": dict(details),
        "source_created_at": source_created_at,
    }
    _assert_fact_safe(payload)
    digest = canonical_digest(payload)
    return {
        "fact_id": hashlib.sha256(
            f"{source_type}\0{source_id}".encode("utf-8")
        ).hexdigest(),
        "source_type": source_type,
        "source_id": source_id,
        "code": code,
        "code_key": code_key,
        "relative_media_path": relative_media_path,
        "quality_height": quality_height,
        "nfo_provenance_json": json.dumps(
            dict(nfo_provenance or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "replaces_source_id": replaces_source_id,
        "superseded_by_source_id": superseded_by_source_id,
        "publication_outcome": publication_outcome,
        "details_json": json.dumps(
            dict(details),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "source_created_at": source_created_at,
        "archived_at": archived_at,
        "fact_digest": digest,
    }


def fact_mapping_from_row(row: Mapping[str, object]) -> dict[str, object]:
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
    return {column: row[column] for column in columns}


def fact_digest_from_mapping(fact: Mapping[str, object]) -> str:
    try:
        raw_provenance = json.loads(str(fact["nfo_provenance_json"] or "{}"))
        raw_details = json.loads(str(fact["details_json"] or "{}"))
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoryLifecycleConflictError(
            "history cleanup recovery fact is invalid"
        ) from exc
    if not isinstance(raw_provenance, dict) or not isinstance(raw_details, dict):
        raise HistoryLifecycleConflictError("history cleanup recovery fact is invalid")
    provenance: dict[str, str] = {}
    for raw_name, raw_value in raw_provenance.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise HistoryLifecycleConflictError(
                "history cleanup recovery fact is invalid"
            )
        provenance[raw_name] = raw_value
    payload = {
        "source_type": str(fact["source_type"]),
        "source_id": str(fact["source_id"]),
        "code": optional_text(fact["code"]),
        "code_key": optional_text(fact["code_key"]),
        "relative_media_path": optional_text(fact["relative_media_path"]),
        "quality_height": optional_int(fact["quality_height"]),
        "nfo_provenance": [
            {"name": name, "provenance": value}
            for name, value in sorted(provenance.items())
        ],
        "replaces_source_id": optional_text(fact["replaces_source_id"]),
        "superseded_by_source_id": optional_text(fact["superseded_by_source_id"]),
        "publication_outcome": optional_text(fact["publication_outcome"]),
        "details": raw_details,
        "source_created_at": float(fact["source_created_at"]),
    }
    _assert_fact_safe(payload)
    return canonical_digest(payload)


def recovery_operation_digest(
    candidates: Sequence[Candidate],
    fact_summaries: Sequence[tuple[int, str, str, float]],
    *,
    payload_digest: str,
) -> str:
    return canonical_digest(
        {
            "revision": HISTORY_CLEANUP_OPERATION_REVISION,
            "payload_digest": payload_digest,
            "candidates": [
                {
                    "position": position,
                    "task_type": candidate.task_type,
                    "source_id": candidate.identity,
                    "fingerprint": candidate.fingerprint,
                    "status": candidate.status,
                    "record_count": candidate.record_count,
                }
                for position, candidate in enumerate(candidates)
            ],
            "facts": [
                {
                    "candidate_position": position,
                    "fact_id": fact_id,
                    "fact_digest": fact_digest,
                    "archived_at": archived_at,
                }
                for position, fact_id, fact_digest, archived_at in fact_summaries
            ],
        }
    )


def legacy_recovery_operation_digest(
    candidates: Sequence[Candidate],
    fact_digests: Sequence[tuple[int, str, str]],
) -> str:
    return canonical_digest(
        {
            "revision": HISTORY_CLEANUP_OPERATION_REVISION,
            "candidates": [
                {
                    "position": position,
                    "task_type": candidate.task_type,
                    "source_id": candidate.identity,
                    "fingerprint": candidate.fingerprint,
                    "status": candidate.status,
                    "record_count": candidate.record_count,
                }
                for position, candidate in enumerate(candidates)
            ],
            "facts": [
                {
                    "candidate_position": position,
                    "fact_id": fact_id,
                    "fact_digest": fact_digest,
                }
                for position, fact_id, fact_digest in fact_digests
            ],
        }
    )


def recovery_payload(
    candidates: Sequence[Candidate],
    mapped_facts: Sequence[tuple[int, Mapping[str, object]]],
) -> dict[str, object]:
    return {
        "revision": HISTORY_CLEANUP_OPERATION_REVISION,
        "candidates": [
            {
                "position": position,
                "task_type": candidate.task_type,
                "source_id": candidate.identity,
                "fingerprint": candidate.fingerprint,
                "status": candidate.status,
                "record_count": candidate.record_count,
            }
            for position, candidate in enumerate(candidates)
        ],
        "facts": [
            {"candidate_position": position, **dict(fact)}
            for position, fact in mapped_facts
        ],
    }


def recovery_archived_at(fact: Mapping[str, object]) -> float:
    try:
        return timestamp(fact["archived_at"])
    except (KeyError, HistoryLifecycleValidationError) as exc:
        raise HistoryLifecycleConflictError(
            "history cleanup recovery archive timestamp is invalid"
        ) from exc


def _assert_fact_safe(value: object, *, key: str | None = None) -> None:
    if key is not None and SENSITIVE_KEY_RE.search(key):
        raise HistoryLifecycleConflictError("history fact contains a sensitive field")
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise HistoryLifecycleConflictError("history fact is invalid")
            _assert_fact_safe(item, key=raw_key)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_fact_safe(item)
        return
    if isinstance(value, str) and SENSITIVE_VALUE_RE.search(value):
        raise HistoryLifecycleConflictError("history fact contains sensitive data")
    if isinstance(value, float) and not math.isfinite(value):
        raise HistoryLifecycleConflictError("history fact is invalid")


def assert_export_safe(record: Mapping[str, object]) -> None:
    _assert_fact_safe(record)
