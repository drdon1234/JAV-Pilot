"""Resolving batch item intents against the media library and active jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Mapping, Sequence

from ..errors import WebDownloadError
from ..jobs import normalize_web_download_code
from ..quality import validate_selected_height
from ..variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
)
from .errors import WebDownloadBatchConflictError, WebDownloadBatchError
from .models import (
    ITEM_QUALITY_STRATEGIES,
    MAX_BATCH_ITEMS,
    BatchItemIntent,
    ExistingWork,
    LibraryDeduplicationSnapshot,
    SelectedBatchItem,
)
from .validation import (
    decode_quality_heights,
    hash_preview_token,
    validate_intent_height,
    validate_item_quality_strategy,
    validate_rule_selection,
)

def commit_intent_hash(
    preview_token: object,
    batch_id: str,
    existing_policy: str,
    item_intents: Sequence[BatchItemIntent],
) -> str:
    hash_preview_token(preview_token)
    token = str(preview_token)
    payload = json.dumps(
        {
            "batch_id": batch_id,
            "existing_policy": existing_policy,
            "item_intents": [
                {
                    "code_key": intent.code_key,
                    "variant": intent.variant,
                    "quality_strategy": intent.quality_strategy,
                    "requested_height": intent.requested_height,
                }
                for intent in item_intents
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hmac.new(token.encode("ascii"), payload, hashlib.sha256).hexdigest()


def resolve_item_intents(
    selected_codes: Sequence[object] | None,
    item_intents: Sequence[Mapping[str, object]] | None,
    items: Sequence[sqlite3.Row],
    *,
    max_height: int,
    quality_complete: bool,
    max_items: int = MAX_BATCH_ITEMS,
) -> tuple[BatchItemIntent, ...]:
    if selected_codes is not None and item_intents is not None:
        raise WebDownloadBatchConflictError(
            "selected_codes and item_intents cannot be combined"
        )
    by_key = {str(item["code_key"]): item for item in items}
    if item_intents is None:
        selected = _resolve_selected_code_keys(
            selected_codes,
            items,
            max_items=max_items,
        )
        intents = tuple(
            BatchItemIntent(
                key,
                normalize_web_download_variant(by_key[key]["variant"]),
                validate_item_quality_strategy(by_key[key]["quality_strategy"]),
                (
                    max_height
                    if by_key[key]["requested_height"] is None
                    else _validate_intent_height_for_commit(
                        by_key[key]["requested_height"],
                        max_height=max_height,
                    )
                ),
            )
            for key in selected
        )
    else:
        if isinstance(item_intents, (str, bytes, bytearray)) or not isinstance(
            item_intents, Sequence
        ):
            raise WebDownloadBatchConflictError("item_intents must be an array")
        if len(item_intents) > max_items:
            raise WebDownloadBatchConflictError("too many item intents")
        resolved: dict[str, BatchItemIntent] = {}
        for value in item_intents:
            if not isinstance(value, Mapping):
                raise WebDownloadBatchConflictError("batch item intent is invalid")
            if not {"code", "variant", "quality_strategy"}.issubset(value) or not set(
                value
            ).issubset({"code", "variant", "quality_strategy", "requested_height"}):
                raise WebDownloadBatchConflictError("batch item intent is invalid")
            try:
                _, code_key = normalize_web_download_code(value["code"])
            except WebDownloadError as exc:
                raise WebDownloadBatchConflictError(
                    "batch item intent catalog code is invalid"
                ) from exc
            if code_key not in by_key or code_key in resolved:
                raise WebDownloadBatchConflictError("batch item intent is invalid")
            try:
                variant = normalize_web_download_variant(value["variant"])
            except ValueError as exc:
                raise WebDownloadBatchConflictError(str(exc)) from exc
            if variant != normalize_web_download_variant(by_key[code_key]["variant"]):
                raise WebDownloadBatchConflictError("batch item variant is invalid")
            strategy = str(value["quality_strategy"] or "").strip().lower()
            if strategy not in ITEM_QUALITY_STRATEGIES:
                raise WebDownloadBatchConflictError(
                    "batch item quality strategy is invalid"
                )
            raw_height = value.get("requested_height")
            if strategy == "selected" and raw_height is None:
                raise WebDownloadBatchConflictError(
                    "selected batch quality requires a height"
                )
            requested_height = (
                max_height
                if raw_height is None
                else _validate_intent_height_for_commit(
                    raw_height,
                    max_height=max_height,
                )
            )
            resolved[code_key] = BatchItemIntent(
                code_key,
                variant,
                strategy,
                requested_height,
            )
        intents = tuple(resolved[key] for key in by_key if key in resolved)
    if intents and not quality_complete:
        raise WebDownloadBatchConflictError("batch qualities are still being resolved")
    for intent in intents:
        item = by_key[intent.code_key]
        quality_status = str(item["quality_status"])
        if quality_status not in {"ready", "legacy"}:
            raise WebDownloadBatchConflictError(
                f"{item['code']} has no confirmed download quality"
            )
        if quality_status == "legacy":
            # Resource-search confirmations intentionally have no pre-fetched
            # quality list. The WebDownload worker validates the requested
            # height against MissAV before it downloads any media.
            continue
        heights = decode_quality_heights(item["available_heights_json"])
        if intent.quality_strategy == "selected":
            if intent.requested_height not in heights:
                raise WebDownloadBatchConflictError(
                    f"{item['code']} does not provide the selected quality"
                )
        elif not any(height <= intent.requested_height for height in heights):
            raise WebDownloadBatchConflictError(
                f"{item['code']} has no quality within the selected ceiling"
            )
    return intents


def _validate_intent_height_for_commit(value: object, *, max_height: int) -> int:
    try:
        return validate_intent_height(value, max_height=max_height)
    except WebDownloadBatchError as exc:
        raise WebDownloadBatchConflictError(str(exc)) from exc


def intent_candidate_height(
    item: sqlite3.Row,
    intent: BatchItemIntent,
) -> int:
    if intent.quality_strategy == "selected":
        return intent.requested_height
    if str(item["quality_status"]) == "legacy":
        return intent.requested_height
    heights = decode_quality_heights(item["available_heights_json"])
    eligible = tuple(height for height in heights if height <= intent.requested_height)
    if not eligible:
        raise WebDownloadBatchConflictError(
            f"{item['code']} has no quality within the selected ceiling"
        )
    return max(eligible)


def _resolve_selected_code_keys(
    selected_codes: Sequence[object] | None,
    items: Sequence[sqlite3.Row],
    *,
    max_items: int = MAX_BATCH_ITEMS,
) -> tuple[str, ...]:
    available = tuple(str(item["code_key"]) for item in items)
    selected_by_default = tuple(
        str(item["code_key"]) for item in items if bool(item["selected"])
    )
    if selected_codes is None:
        return selected_by_default
    if isinstance(selected_codes, (str, bytes, bytearray)) or not isinstance(
        selected_codes, Sequence
    ):
        raise WebDownloadBatchConflictError("selected_codes must be an array")
    if len(selected_codes) > max_items:
        raise WebDownloadBatchConflictError("too many selected catalog codes")
    requested: set[str] = set()
    for value in selected_codes:
        if not isinstance(value, str):
            raise WebDownloadBatchConflictError("selected catalog code is invalid")
        try:
            _, code_key = normalize_web_download_code(value)
        except WebDownloadError as exc:
            raise WebDownloadBatchConflictError(
                "selected catalog code is invalid"
            ) from exc
        if code_key in requested:
            raise WebDownloadBatchConflictError("selected catalog codes must be unique")
        requested.add(code_key)
    if not requested.issubset(available):
        raise WebDownloadBatchConflictError(
            "selected catalog code is not part of this batch"
        )
    return tuple(code_key for code_key in available if code_key in requested)


def existing_work_height(existing: ExistingWork | None) -> int | None:
    if existing is None:
        return None
    for value in (existing.verified_height, existing.selected_height):
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        try:
            return validate_selected_height(value)
        except ValueError:
            continue
    return None


def rule_selected_code_keys(
    items: Sequence[SelectedBatchItem],
    *,
    selection_mode: object,
    quality_strategy: object,
    requested_height: int,
    snapshot: LibraryDeduplicationSnapshot,
) -> frozenset[str]:
    mode = validate_rule_selection(selection_mode)
    strategy = validate_item_quality_strategy(quality_strategy)
    if mode == "all":
        return frozenset(item.code_key for item in items)
    selected: set[str] = set()
    for item in items:
        present = snapshot_has_variant(snapshot, item.code_key, item.variant)
        if mode == "missing":
            if not present:
                selected.add(item.code_key)
            continue
        existing_height = existing_work_height(
            snapshot_existing_variant(snapshot, item.code_key, item.variant)
        )
        if (
            strategy in {"selected", "highest"}
            and present
            and existing_height is not None
            and requested_height > existing_height
        ):
            selected.add(item.code_key)
    return frozenset(selected)


def snapshot_has_variant(
    snapshot: LibraryDeduplicationSnapshot,
    code_key: str,
    variant: MissavVariant,
) -> bool:
    if (code_key, variant) in snapshot.variant_keys:
        return True
    return (
        variant == DEFAULT_WEB_DOWNLOAD_VARIANT
        and not snapshot.variant_keys
        and not snapshot.unknown_code_keys
        and code_key in snapshot.existing_by_code
    )


def snapshot_existing_variant(
    snapshot: LibraryDeduplicationSnapshot,
    code_key: str,
    variant: MissavVariant,
) -> ExistingWork | None:
    existing = snapshot.existing_by_variant.get((code_key, variant))
    if existing is not None:
        return existing
    if (
        variant == DEFAULT_WEB_DOWNLOAD_VARIANT
        and not snapshot.variant_keys
        and not snapshot.unknown_code_keys
    ):
        return snapshot.existing_by_code.get(code_key)
    return None


def active_job_matches_batch_intent(
    active: sqlite3.Row,
    *,
    intent_height: int,
    quality_strategy: str,
    existing_policy: str,
    existing_work: ExistingWork | None,
) -> bool:
    active_height = active["requested_height"]
    expected_output_path = (
        existing_work.output_path if existing_work is not None else None
    )
    expected_replaces_job_id = (
        existing_work.job_id if existing_work is not None else None
    )
    return (
        active_height is not None
        and int(active_height) == intent_height
        and str(active["quality_strategy"]) == quality_strategy
        and str(active["existing_policy"]) == existing_policy
        and (
            str(active["incumbent_output_path"])
            if active["incumbent_output_path"] is not None
            else None
        )
        == expected_output_path
        and (
            str(active["replaces_job_id"])
            if active["replaces_job_id"] is not None
            else None
        )
        == expected_replaces_job_id
    )


def optional_existing_height(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return validate_selected_height(value)
    except ValueError:
        return None
