"""Validation and normalization of stored media library values."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence

from .errors import (
    MediaLibraryConflictError,
    MediaLibraryError,
    MediaLibraryRootChangedError,
    MediaLibraryUnavailableError,
)
from .models import ENTRY_ID_RE, GENERATION_ID_RE, ROOT_KEY_RE

def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def optional_string(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def first(values: Sequence[str]) -> str | None:
    return values[0] if values else None


def dedupe(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = term_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def term_key(value: object) -> str:
    clean = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    if not clean or len(clean.encode("utf-8")) > 1024:
        raise MediaLibraryError("media library term is invalid")
    return clean


def optional_term(value: object) -> str | None:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    if not clean:
        return None
    if len(clean.encode("utf-8")) > 256 or any(ord(char) < 32 for char in clean):
        raise MediaLibraryError("media library filter is invalid")
    return clean


def optional_query(value: object) -> str | None:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    if not clean:
        return None
    if len(clean.encode("utf-8")) > 256 or any(ord(char) < 32 for char in clean):
        raise MediaLibraryError("media library query is invalid")
    return clean


def optional_enum(value: object, allowed: Iterable[str], name: str) -> str | None:
    if value is None or str(value).strip() in {"", "all"}:
        return None
    clean = str(value).strip().lower()
    if clean not in set(allowed):
        raise MediaLibraryError(f"media library {name} filter is invalid")
    return clean


def optional_height(value: object, name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return bounded_int(value, name, minimum=144, maximum=4320)


def bounded_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MediaLibraryError(f"media library {name} is invalid")
    try:
        clean = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError(f"media library {name} is invalid") from exc
    if clean < minimum or clean > maximum:
        raise MediaLibraryError(f"media library {name} is invalid")
    return clean


def finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise MediaLibraryError(f"media library {name} is invalid")
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError(f"media library {name} is invalid") from exc
    if not math.isfinite(clean) or clean < 0:
        raise MediaLibraryError(f"media library {name} is invalid")
    return clean


def finite_positive(value: object, name: str, *, maximum: float) -> float:
    clean = finite_nonnegative(value, name)
    if clean <= 0 or clean > maximum:
        raise MediaLibraryError(f"media library {name} is invalid")
    return clean


def stored_timestamp(value: object) -> float:
    clean = finite_nonnegative(value, "timestamp")
    return clean


def validate_root_key(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not ROOT_KEY_RE.fullmatch(clean):
        raise MediaLibraryError("media library root key is invalid")
    return clean


def validate_generation_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not GENERATION_ID_RE.fullmatch(clean):
        raise MediaLibraryError("media library generation id is invalid")
    return clean


def validate_entry_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not ENTRY_ID_RE.fullmatch(clean):
        raise MediaLibraryError("media library entry id is invalid")
    return clean


def normalize_error_code(value: object) -> str:
    clean = re.sub(r"[^a-z0-9_]+", "_", str(value or "failed").strip().lower()).strip(
        "_"
    )
    return clean[:64] or "failed"


def failure_code(error: BaseException) -> str:
    if isinstance(error, MediaLibraryRootChangedError):
        return "root_changed"
    if isinstance(error, MediaLibraryUnavailableError):
        return "storage_unavailable"
    if isinstance(error, MediaLibraryConflictError):
        return "conflict"
    return "scan_failed"


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
