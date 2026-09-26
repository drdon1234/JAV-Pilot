"""Validation and JSON helpers for stored review values."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence

from ...core.catalog_code import normalize_catalog_code
from .errors import MediaMetadataReviewError, MediaMetadataReviewValidationError
from .models import (
    ERROR_CODE_RE,
    HEX_ID_RE,
    SENSITIVE_KEY_RE,
    SENSITIVE_VALUE_RE,
    SHA256_RE,
)

def reject_sensitive_text(value: str, label: str) -> None:
    if SENSITIVE_VALUE_RE.search(value):
        raise MediaMetadataReviewValidationError(f"{label} contains sensitive data")


def validated_code(value: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(value, max_length=40)
    if normalized is None:
        raise MediaMetadataReviewValidationError(
            "metadata review catalog code is invalid"
        )
    return normalized


def hex_id(value: object, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not HEX_ID_RE.fullmatch(clean):
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    return clean


def validated_sha256(value: object, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(clean):
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    return clean


def validated_source_id(value: object, allowed: Iterable[str]) -> str:
    return enum(value, set(allowed), "source")


def enum(value: object, allowed: Iterable[str], label: str) -> str:
    clean = str(value or "").strip().lower()
    if clean not in set(allowed):
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    return clean


def strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    return value


def bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MediaMetadataReviewValidationError(
            f"metadata review {label} is invalid"
        ) from exc
    if result < minimum or result > maximum or str(result) != str(value).strip():
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    return result


def optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def optional_float(value: object) -> float | None:
    return float(value) if value is not None else None


def optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaMetadataReviewValidationError(
            "metadata review list field is invalid"
        )
    return tuple(str(item) for item in value)


def validated_error_code(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not ERROR_CODE_RE.fullmatch(clean):
        raise MediaMetadataReviewValidationError(
            "metadata review error code is invalid"
        )
    return clean


def timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise MediaMetadataReviewValidationError("metadata review timestamp is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaMetadataReviewValidationError(
            "metadata review timestamp is invalid"
        ) from exc
    if not math.isfinite(result) or result < 0:
        raise MediaMetadataReviewValidationError("metadata review timestamp is invalid")
    return result


def finite_positive(value: object, label: str, *, maximum: float) -> float:
    result = timestamp(value)
    if result <= 0 or result > maximum:
        raise MediaMetadataReviewValidationError(f"metadata review {label} is invalid")
    return result


def json_text(value: object) -> str:
    _validate_json_value(value)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def json_digest(value: object) -> str:
    return hashlib.sha256(json_text(value).encode("ascii")).hexdigest()


def json_load(value: str) -> object:
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise MediaMetadataReviewError(
            "metadata review stored JSON is invalid"
        ) from exc
    _validate_json_value(result)
    return result


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise MediaMetadataReviewValidationError(
                "metadata review JSON number is invalid"
            )
        return
    if isinstance(value, str):
        reject_sensitive_text(value, "metadata JSON")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            clean_key = str(key)
            if SENSITIVE_KEY_RE.search(clean_key):
                raise MediaMetadataReviewValidationError(
                    "metadata review JSON key is sensitive"
                )
            _validate_json_value(item)
        return
    raise MediaMetadataReviewValidationError("metadata review JSON value is invalid")
