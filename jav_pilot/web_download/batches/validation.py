"""Validation, normalization and hashing of batch inputs, rules and identifiers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from typing import Mapping, Sequence

from ..errors import WebDownloadError
from ..jobs import normalize_web_download_code
from ..policy import DEFAULT_EXISTING_POLICY, validate_existing_policy
from ..quality import normalize_quality_heights, validate_selected_height
from ..variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
    normalize_variant_priority,
    normalize_web_download_variant,
)
from .errors import (
    WebDownloadBatchConflictError,
    WebDownloadBatchError,
    WebDownloadBatchNotFoundError,
)
from .models import (
    ABSOLUTE_MAX_BATCH_PAGES,
    BATCH_ID_RE,
    ITEM_QUALITY_STRATEGIES,
    MAX_BATCH_ITEMS,
    RESOURCE_SEARCH_SELECTION_PROVENANCE,
    SERIES_DISCOVERY_PROVENANCE,
    SHA256_RE,
    BatchRequest,
    SelectedBatchItem,
)

MAX_SELECTED_BATCH_ITEMS = 999


DEFAULT_BATCH_START = "001"
DEFAULT_BATCH_END = "999"


RULE_SELECTION_MODES = ("all", "missing", "upgrades")


_SOURCE_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RULE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_INPUT_RE = re.compile(r"^[A-Za-z0-9]+(?:[-._][A-Za-z0-9]+)*$")
_DIGITS_RE = re.compile(r"^[0-9]{1,9}$")
_TERMINAL_SUFFIX_RE = re.compile(r"^(.*?)[-._]?([0-9]{1,9})$")
_FORBIDDEN_INPUT_RE = re.compile(r"[\x00-\x20\x7f:/\\*?\[\]{}<>]")


def parse_batch_request(
    code_or_prefix: object,
    start: object | None = None,
    end: object | None = None,
    max_height: object | None = 2160,
    existing_policy: object = DEFAULT_EXISTING_POLICY,
    variant_priority: object = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
) -> BatchRequest:
    raw = _strict_ascii_input(code_or_prefix, "code_or_prefix")
    clean_max_height = validate_max_height(max_height)
    clean_existing_policy = validate_batch_existing_policy(existing_policy)
    try:
        clean_variant_priority = normalize_variant_priority(variant_priority)
    except ValueError as exc:
        raise WebDownloadBatchError(str(exc)) from exc
    has_start = start is not None
    has_end = end is not None
    if has_start != has_end:
        raise WebDownloadBatchError("start and end must be provided together")

    try:
        display, _ = normalize_web_download_code(raw)
        prefix, suffix = split_full_code(display)
    except WebDownloadError:
        pass
    else:
        return BatchRequest(
            "exact",
            raw.upper(),
            prefix,
            len(suffix),
            suffix,
            suffix,
            clean_max_height,
            clean_existing_policy,
            clean_variant_priority,
        )

    prefix = _normalize_prefix(raw)
    if not has_start:
        return BatchRequest(
            "all",
            raw.upper(),
            prefix,
            None,
            None,
            None,
            clean_max_height,
            clean_existing_policy,
            clean_variant_priority,
        )

    clean_start = _strict_ascii_input(start, "start")
    clean_end = _strict_ascii_input(end, "end")
    digit_bounds = _DIGITS_RE.fullmatch(clean_start) and _DIGITS_RE.fullmatch(clean_end)
    if digit_bounds:
        start_suffix = clean_start
        end_suffix = clean_end
    else:
        if bool(_DIGITS_RE.fullmatch(clean_start)) != bool(
            _DIGITS_RE.fullmatch(clean_end)
        ):
            raise WebDownloadBatchError("start and end must use the same range format")
        start_code = _normalize_full_code(clean_start, "start")
        end_code = _normalize_full_code(clean_end, "end")
        start_prefix, start_suffix = split_full_code(start_code)
        end_prefix, end_suffix = split_full_code(end_code)
        if prefix_key(start_prefix) != prefix_key(end_prefix):
            raise WebDownloadBatchError("range codes must use the same prefix")
        if prefix_key(prefix) != prefix_key(start_prefix):
            raise WebDownloadBatchError("range codes do not match code_or_prefix")

    if len(start_suffix) != len(end_suffix):
        raise WebDownloadBatchError("range suffixes must use the same width")
    if int(start_suffix) > int(end_suffix):
        raise WebDownloadBatchError("start must not be greater than end")
    mode = (
        "all"
        if (start_suffix, end_suffix) == (DEFAULT_BATCH_START, DEFAULT_BATCH_END)
        else "range"
    )
    return BatchRequest(
        mode,
        raw.upper(),
        prefix,
        len(start_suffix),
        start_suffix,
        end_suffix,
        clean_max_height,
        clean_existing_policy,
        clean_variant_priority,
    )


def _strict_ascii_input(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise WebDownloadBatchError(f"{field} is invalid")
    if not value.isascii() or _FORBIDDEN_INPUT_RE.search(value):
        raise WebDownloadBatchError(f"{field} is invalid")
    if not _SAFE_INPUT_RE.fullmatch(value):
        raise WebDownloadBatchError(f"{field} is invalid")
    return value.upper()


def bounded_batch_int(
    value: object,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebDownloadBatchError(f"{field} is invalid")
    if value < minimum or value > maximum:
        raise WebDownloadBatchError(f"{field} is invalid")
    return value


def validate_page_budget(value: object) -> int:
    return bounded_batch_int(
        value,
        "page_budget",
        1,
        ABSOLUTE_MAX_BATCH_PAGES,
    )


def optional_snapshot_revision(value: object | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x21 for character in value)
    ):
        raise WebDownloadBatchError("media library revision is invalid")
    return value


def validate_rule_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _RULE_ID_RE.fullmatch(clean):
        raise WebDownloadBatchError("web download batch rule identity is invalid")
    return clean


def validate_rule_name(value: object) -> str:
    if not isinstance(value, str):
        raise WebDownloadBatchError("batch rule name is invalid")
    clean = value.strip()
    if (
        not clean
        or len(clean) > 120
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in clean)
    ):
        raise WebDownloadBatchError("batch rule name is invalid")
    return clean


def validate_item_quality_strategy(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in ITEM_QUALITY_STRATEGIES:
        raise WebDownloadBatchError("batch item quality strategy is invalid")
    return clean


def validate_intent_height(value: object, *, max_height: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebDownloadBatchError("batch item quality height is invalid")
    try:
        clean = validate_selected_height(value)
    except ValueError as exc:
        raise WebDownloadBatchError("batch item quality height is invalid") from exc
    if clean > max_height:
        raise WebDownloadBatchError("batch item quality height exceeds the batch limit")
    return clean


def validate_rule_selection(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in RULE_SELECTION_MODES:
        raise WebDownloadBatchError("batch rule selection mode is invalid")
    return clean


def optional_rule_revision(value: object | None) -> int | None:
    if value is None:
        return None
    return bounded_batch_int(value, "rule revision", 1, 2_147_483_647)


def decode_quality_heights(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or len(value) > 512:
        raise WebDownloadBatchError("stored batch qualities are invalid")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WebDownloadBatchError("stored batch qualities are invalid") from exc
    if not isinstance(decoded, list) or any(
        isinstance(height, bool) or not isinstance(height, int) for height in decoded
    ):
        raise WebDownloadBatchError("stored batch qualities are invalid")
    try:
        normalized = normalize_quality_heights(decoded)
    except ValueError as exc:
        raise WebDownloadBatchError("stored batch qualities are invalid") from exc
    if list(normalized) != decoded:
        raise WebDownloadBatchError("stored batch qualities are invalid")
    return normalized


def _normalize_prefix(value: str) -> str:
    prefix = value.upper()
    if (
        not _SAFE_INPUT_RE.fullmatch(prefix)
        or len(prefix) > 32
        or sum(character.isalpha() for character in prefix) < 2
    ):
        raise WebDownloadBatchError("code_or_prefix is invalid")
    return prefix


def _normalize_full_code(value: str, field: str) -> str:
    try:
        return normalize_web_download_code(value)[0]
    except WebDownloadError as exc:
        raise WebDownloadBatchError(f"{field} catalog code is invalid") from exc


def split_full_code(value: str) -> tuple[str, str]:
    match = _TERMINAL_SUFFIX_RE.fullmatch(value)
    if match is None:
        raise WebDownloadBatchError("catalog code suffix is invalid")
    prefix = match.group(1).rstrip("-._")
    suffix = match.group(2)
    if not prefix or sum(character.isalpha() for character in prefix) < 2:
        raise WebDownloadBatchError("catalog code prefix is invalid")
    return prefix, suffix


def prefix_key(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def normalize_discovered_codes(
    raw_codes: object, request: BatchRequest
) -> tuple[str, ...]:
    if isinstance(raw_codes, (str, bytes)) or not isinstance(raw_codes, Sequence):
        raise WebDownloadBatchError("batch discovery result is invalid")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_code in raw_codes:
        display, code_key = normalize_web_download_code(raw_code)
        prefix, suffix = split_full_code(display)
        if prefix_key(prefix) != prefix_key(request.prefix):
            raise WebDownloadBatchError("batch discovery returned an unexpected code")
        if request.suffix_width is not None and len(suffix) != request.suffix_width:
            raise WebDownloadBatchError("batch discovery returned an unexpected code")
        suffix_number = int(suffix)
        if request.start is not None and suffix_number < int(request.start):
            raise WebDownloadBatchError("batch discovery returned an unexpected code")
        if request.end is not None and suffix_number > int(request.end):
            raise WebDownloadBatchError("batch discovery returned an unexpected code")
        if code_key not in seen:
            seen.add(code_key)
            normalized.append(display)
    return tuple(
        sorted(
            normalized,
            key=lambda code: (
                int(split_full_code(code)[1]),
                len(split_full_code(code)[1]),
                code,
            ),
        )
    )


def normalize_discovered_variants(
    codes: Sequence[str],
    raw_variants: Mapping[str, Sequence[object]] | None,
) -> dict[str, tuple[MissavVariant, ...]]:
    expected = {
        normalize_web_download_code(code)[1]: normalize_web_download_code(code)[0]
        for code in codes
    }
    if raw_variants is None:
        return {code_key: (DEFAULT_WEB_DOWNLOAD_VARIANT,) for code_key in expected}
    normalized: dict[str, tuple[MissavVariant, ...]] = {}
    for raw_code, raw_values in raw_variants.items():
        try:
            _display, code_key = normalize_web_download_code(raw_code)
        except WebDownloadError as exc:
            raise WebDownloadBatchError("batch discovery variants are invalid") from exc
        if code_key not in expected or code_key in normalized:
            raise WebDownloadBatchError("batch discovery variants are invalid")
        normalized[code_key] = normalize_available_variants(raw_values)
    if set(normalized) != set(expected):
        raise WebDownloadBatchError("batch discovery variants are incomplete")
    return normalized


def normalize_selected_batch_items(
    values: object,
    variant_priority: Sequence[MissavVariant],
) -> tuple[SelectedBatchItem, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise WebDownloadBatchError("selected resources are invalid")
    if not 1 <= len(values) <= MAX_SELECTED_BATCH_ITEMS:
        raise WebDownloadBatchError("selected resources are invalid")
    normalized: list[SelectedBatchItem] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping) or set(value) != {
            "code",
            "available_variants",
        }:
            raise WebDownloadBatchError("selected resource is invalid")
        try:
            display, code_key = normalize_web_download_code(value["code"])
        except WebDownloadError as exc:
            raise WebDownloadBatchError("selected resource code is invalid") from exc
        if code_key in seen:
            raise WebDownloadBatchError("selected resource codes must be unique")
        available_variants = normalize_available_variants(value["available_variants"])
        variant = next(
            candidate
            for candidate in variant_priority
            if candidate in available_variants
        )
        normalized.append(
            SelectedBatchItem(
                code=display,
                code_key=code_key,
                available_variants=available_variants,
                variant=variant,
            )
        )
        seen.add(code_key)
    return tuple(normalized)


def validate_selected_batch_items(
    values: object,
) -> tuple[SelectedBatchItem, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise WebDownloadBatchError("selected resources are invalid")
    if not 1 <= len(values) <= MAX_SELECTED_BATCH_ITEMS:
        raise WebDownloadBatchError("selected resources are invalid")
    clean: list[SelectedBatchItem] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, SelectedBatchItem):
            raise WebDownloadBatchError("selected resource is invalid")
        try:
            display, code_key = normalize_web_download_code(value.code)
            available_variants = normalize_available_variants(value.available_variants)
            variant = normalize_web_download_variant(value.variant)
        except (ValueError, WebDownloadError) as exc:
            raise WebDownloadBatchError("selected resource is invalid") from exc
        if (
            display != value.code
            or code_key != value.code_key
            or code_key in seen
            or variant not in available_variants
        ):
            raise WebDownloadBatchError("selected resource is invalid")
        clean.append(SelectedBatchItem(display, code_key, available_variants, variant))
        seen.add(code_key)
    return tuple(clean)


def validate_source_session_id(value: object) -> str:
    if not isinstance(value, str) or not _SOURCE_SESSION_ID_RE.fullmatch(value):
        raise WebDownloadBatchError("resource search session identity is invalid")
    return value


def validate_source_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebDownloadBatchError("resource search revision is invalid")
    if not 1 <= value <= 2_147_483_647:
        raise WebDownloadBatchError("resource search revision is invalid")
    return value


def validate_direct_queue_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise WebDownloadBatchError(f"{name} is invalid")
    return value


def hash_direct_queue_key(value: object) -> str:
    if not isinstance(value, str):
        raise WebDownloadBatchError("direct queue idempotency key is invalid")
    clean = value.strip()
    if not _IDEMPOTENCY_RE.fullmatch(clean) or "://" in clean:
        raise WebDownloadBatchError("direct queue idempotency key is invalid")
    return hashlib.sha256(clean.encode("ascii")).hexdigest()


def auto_download_request_hash(request: BatchRequest, code_key: str) -> str:
    if not request.auto_commit or request.mode != "exact":
        raise WebDownloadBatchError("automatic download request is invalid")
    payload = json.dumps(
        {
            "code_key": code_key,
            "existing_policy": request.existing_policy,
            "max_height": request.max_height,
            "quality_strategy": "highest",
            "variant_priority": list(request.variant_priority),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def batch_item_limit(row: Mapping[str, object]) -> int:
    provenance_type = str(row["provenance_type"])
    if provenance_type == RESOURCE_SEARCH_SELECTION_PROVENANCE:
        return MAX_SELECTED_BATCH_ITEMS
    if provenance_type == SERIES_DISCOVERY_PROVENANCE:
        return MAX_BATCH_ITEMS
    raise WebDownloadBatchError("batch provenance is invalid")


def selected_batch_items_hash(
    source_session_id: str,
    source_revision: int,
    items: Sequence[SelectedBatchItem],
) -> str:
    payload = json.dumps(
        {
            "source_session_id": source_session_id,
            "source_revision": source_revision,
            "items": [
                {
                    "code_key": item.code_key,
                    "available_variants": list(item.available_variants),
                }
                for item in items
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def verify_selected_batch_provenance(
    row: sqlite3.Row,
    item_rows: Sequence[sqlite3.Row],
) -> None:
    source_session_id = validate_source_session_id(row["source_session_id"])
    source_revision = validate_source_revision(row["source_revision"])
    source_items_hash = row["source_items_hash"]
    if not isinstance(source_items_hash, str) or not SHA256_RE.fullmatch(
        source_items_hash
    ):
        raise WebDownloadBatchError("resource search selection hash is invalid")
    if (
        not 1 <= len(item_rows) <= MAX_SELECTED_BATCH_ITEMS
        or int(row["discovered_count"]) != len(item_rows)
        or not bool(row["discovery_complete"])
    ):
        raise WebDownloadBatchError("resource search selection is invalid")
    priority = variant_priority_from_json(row["variant_priority_json"])
    items: list[SelectedBatchItem] = []
    for item in item_rows:
        try:
            display, code_key = normalize_web_download_code(item["code"])
            available_variants = variants_from_json(item["available_variants_json"])
            variant = normalize_web_download_variant(item["variant"])
        except (ValueError, WebDownloadError) as exc:
            raise WebDownloadBatchError("resource search selection is invalid") from exc
        expected_variant = next(
            candidate for candidate in priority if candidate in available_variants
        )
        if (
            display != str(item["code"])
            or code_key != str(item["code_key"])
            or variant != expected_variant
        ):
            raise WebDownloadBatchError("resource search selection is invalid")
        items.append(SelectedBatchItem(display, code_key, available_variants, variant))
    clean_items = validate_selected_batch_items(items)
    expected_hash = selected_batch_items_hash(
        source_session_id,
        source_revision,
        clean_items,
    )
    if not hmac.compare_digest(source_items_hash, expected_hash):
        raise WebDownloadBatchError("resource search selection hash is invalid")


def normalize_available_variants(values: object) -> tuple[MissavVariant, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise WebDownloadBatchError("web download variants are invalid")
    try:
        requested = tuple(normalize_web_download_variant(value) for value in values)
    except ValueError as exc:
        raise WebDownloadBatchError(str(exc)) from exc
    if not requested or len(set(requested)) != len(requested):
        raise WebDownloadBatchError("web download variants are invalid")
    requested_set = frozenset(requested)
    return tuple(
        variant for variant in WEB_DOWNLOAD_VARIANTS if variant in requested_set
    )


def variants_from_json(value: object) -> tuple[MissavVariant, ...]:
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise WebDownloadBatchError("web download variants are invalid") from exc
    return normalize_available_variants(raw)


def variant_priority_from_json(value: object) -> tuple[MissavVariant, ...]:
    try:
        raw = json.loads(str(value))
        return normalize_variant_priority(raw)
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise WebDownloadBatchError("web download variant priority is invalid") from exc


def hash_preview_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isascii()
    ):
        raise WebDownloadBatchConflictError("preview token is invalid or expired")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def validate_batch_existing_policy(value: object) -> str:
    try:
        return str(
            validate_existing_policy(
                value,
                default=DEFAULT_EXISTING_POLICY,
                allow_legacy=False,
            )
        )
    except ValueError as exc:
        raise WebDownloadBatchError("existing_policy is invalid") from exc


def validate_max_height(value: object | None) -> int:
    if value is None:
        value = 2160
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebDownloadBatchError("max_height is invalid")
    try:
        return validate_selected_height(value)
    except ValueError as exc:
        raise WebDownloadBatchError("max_height is invalid") from exc


def validate_batch_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not BATCH_ID_RE.fullmatch(clean):
        raise WebDownloadBatchNotFoundError("web download batch was not found")
    return clean


def batch_job_id(
    batch_id: str,
    code_key: str,
    variant: MissavVariant,
    requested_height: int,
    quality_strategy: str,
) -> str:
    return hashlib.sha256(
        (
            "missav-batch-job:"
            f"{batch_id}:{code_key}:{variant}:{quality_strategy}:{requested_height}"
        ).encode("ascii")
    ).hexdigest()[:32]


def batch_idempotency_key(
    batch_id: str,
    code_key: str,
    variant: MissavVariant,
    requested_height: int,
    quality_strategy: str,
) -> str:
    suffix = hashlib.sha256(f"{code_key}:{variant}".encode("ascii")).hexdigest()[:24]
    return f"batch:{batch_id}:{variant}:{quality_strategy}:{requested_height}:{suffix}"
