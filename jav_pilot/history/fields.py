"""Validation and normalization of history lifecycle values."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence

from ..web_download.variant import (
    normalize_variant_priority,
    normalize_web_download_variant,
)
from .errors import HistoryLifecycleConflictError, HistoryLifecycleValidationError

TOKEN_RE = re.compile(r"^[a-f0-9]{64}$")


_SAFE_CODE_QUERY_RE = re.compile(r"^[A-Z0-9._ -]{1,40}$")


def validated_preview_token(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not TOKEN_RE.fullmatch(clean):
        raise HistoryLifecycleValidationError("history preview token is invalid")
    return clean


def cleanup_operation_id(preview_token: str) -> str:
    return hashlib.sha256(
        f"history-cleanup\0{preview_token}".encode("ascii")
    ).hexdigest()


def retention_days(value: object) -> int | None:
    if value is None or value is False or value == 0 or value == "0":
        return None
    return bounded_int(value, "retention days", 1, 36_500)


def finite_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise HistoryLifecycleValidationError("preview lifetime is invalid")
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryLifecycleValidationError("preview lifetime is invalid") from exc
    if not math.isfinite(clean) or clean <= 0 or clean > 3600:
        raise HistoryLifecycleValidationError("preview lifetime is invalid")
    return clean


def timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise HistoryLifecycleValidationError("history timestamp is invalid")
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryLifecycleValidationError("history timestamp is invalid") from exc
    if not math.isfinite(clean) or clean < 0:
        raise HistoryLifecycleValidationError("history timestamp is invalid")
    return clean


def optional_timestamp(value: object) -> float | None:
    return None if value is None else timestamp(value)


def bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise HistoryLifecycleValidationError(f"{label} is invalid")
    try:
        clean = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryLifecycleValidationError(f"{label} is invalid") from exc
    if clean < minimum or clean > maximum:
        raise HistoryLifecycleValidationError(f"{label} is invalid")
    return clean


def optional_code_query(value: object) -> str | None:
    if value is None:
        return None
    clean = unicodedata.normalize("NFKC", str(value)).strip().upper()
    if not clean:
        return None
    if not clean.isascii() or _SAFE_CODE_QUERY_RE.fullmatch(clean) is None:
        raise HistoryLifecycleValidationError("history catalog code filter is invalid")
    result = code_query_key(clean)
    if not result:
        raise HistoryLifecycleValidationError("history catalog code filter is invalid")
    return result


def code_query_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def history_web_variant(value: object) -> str:
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise HistoryLifecycleConflictError(
            "history web download variant is invalid"
        ) from exc


def history_optional_web_variant(value: object) -> str | None:
    return None if value is None else history_web_variant(value)


def history_variant_priority(value: object) -> tuple[str, ...]:
    try:
        raw = json.loads(str(value))
        return normalize_variant_priority(raw)
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise HistoryLifecycleConflictError(
            "history batch variant priority is invalid"
        ) from exc


def optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def first_int(*values: object) -> int | None:
    for value in values:
        if value is not None:
            return int(value)
    return None


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sql_slots(values: Sequence[object] | Mapping[object, object]) -> str:
    return ", ".join("?" for _ in values)
