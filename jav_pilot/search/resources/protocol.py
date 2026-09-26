"""The line-delimited JSON protocol spoken with resource search workers."""

from __future__ import annotations

import json

from ...web_download.variant import MissavVariant
from .errors import ResourceSearchError
from .models import (
    MAX_WORKER_DELTA_ITEMS,
    MAX_WORKER_LINE_BYTES,
    RESOURCE_SEARCH_PROTOCOL_VERSION,
    ResourceSearchHeartbeatEvent,
    ResourceSearchItem,
    ResourceSearchPageEvent,
    ResourceSearchStartedEvent,
    ResourceSearchState,
    ResourceSearchWorkerEvent,
    ResourceSearchWorkerResult,
)
from .validation import (
    validate_error_code,
    validate_items,
    validate_page_event,
    validate_state,
    validate_variants,
    validate_worker_result,
)

__all__ = [
    "ResourceSearchWorkerError",
]


class ResourceSearchWorkerError(ResourceSearchError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        state: ResourceSearchState | None = None,
    ) -> None:
        super().__init__("resource search worker failed")
        self.code = validate_error_code(code, retryable=retryable)
        self.retryable = retryable
        self.state = state

    def attach_state(self, state: ResourceSearchState | None) -> None:
        """Keep the last validated stream cursor with a worker failure."""

        if self.state is None and state is not None:
            self.state = state


def state_payload(
    state: ResourceSearchState,
    *,
    include_pending: bool = True,
) -> dict[str, object]:
    clean = validate_state(state)
    payload: dict[str, object] = {
        "next_page": clean.next_page,
        "cursor": clean.cursor,
        "total_pages": clean.total_pages,
        "scanned_pages": clean.scanned_pages,
    }
    if include_pending:
        payload["pending"] = [_protocol_item_payload(item) for item in clean.pending]
    return payload


def state_from_payload(
    value: object,
    *,
    pending_fallback: tuple[ResourceSearchItem, ...] | None = None,
) -> ResourceSearchState:
    required_keys = {
        "next_page",
        "cursor",
        "total_pages",
        "scanned_pages",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(required_keys),
        frozenset(required_keys | {"pending"}),
    }:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True)
    pending_value = value.get("pending")
    if "pending" in value:
        if not isinstance(pending_value, list):
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        pending: list[ResourceSearchItem] = []
        for item in pending_value:
            if not _protocol_item_keys_are_valid(item):
                raise ResourceSearchWorkerError("protocol_failure", retryable=True)
            assert isinstance(item, dict)
            pending.append(
                ResourceSearchItem(
                    code=str(item.get("code") or ""),
                    available_variants=_protocol_variants(
                        item.get("available_variants")
                    ),
                    title=item.get("title"),  # type: ignore[arg-type]
                )
            )
    elif pending_fallback is not None:
        pending = list(pending_fallback)
    else:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True)
    try:
        return validate_state(
            ResourceSearchState(
                next_page=value.get("next_page"),  # type: ignore[arg-type]
                pending=tuple(pending),
                cursor=value.get("cursor"),  # type: ignore[arg-type]
                total_pages=value.get("total_pages"),  # type: ignore[arg-type]
                scanned_pages=value.get("scanned_pages"),  # type: ignore[arg-type]
            )
        )
    except ResourceSearchError:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True) from None


def _items_from_payload(
    value: object, *, maximum: int
) -> tuple[ResourceSearchItem, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True)
    items: list[ResourceSearchItem] = []
    for raw_item in value:
        if not _protocol_item_keys_are_valid(raw_item):
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        assert isinstance(raw_item, dict)
        items.append(
            ResourceSearchItem(
                code=str(raw_item.get("code") or ""),
                available_variants=_protocol_variants(
                    raw_item.get("available_variants")
                ),
                title=raw_item.get("title"),  # type: ignore[arg-type]
            )
        )
    try:
        return validate_items(items, maximum=maximum)
    except ResourceSearchError:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True) from None


def _protocol_item_payload(item: ResourceSearchItem) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": item.code,
        "available_variants": list(item.available_variants),
    }
    if item.title is not None:
        payload["title"] = item.title
    return payload


def _protocol_item_keys_are_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    keys = set(value)
    required = {"code", "available_variants"}
    return keys == required or keys == required | {"title"}


def _protocol_variants(value: object) -> tuple[MissavVariant, ...]:
    try:
        return validate_variants(value)
    except ResourceSearchError:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True) from None


def decode_worker_message(
    raw: bytes,
    *,
    previous_state: ResourceSearchState | None = None,
) -> ResourceSearchWorkerEvent | ResourceSearchWorkerResult | ResourceSearchWorkerError:
    if not raw.endswith(b"\n") or b"\r" in raw or len(raw) > MAX_WORKER_LINE_BYTES:
        raise ResourceSearchWorkerError("protocol_failure", retryable=True)
    try:
        payload = json.loads(
            raw[:-1].decode("ascii"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ResourceSearchWorkerError("protocol_failure", retryable=True) from None
    if not isinstance(payload, dict):
        raise ResourceSearchWorkerError("protocol_failure", retryable=True)
    if (
        payload.get("protocol") != RESOURCE_SEARCH_PROTOCOL_VERSION
        or type(payload.get("protocol")) is not int
    ):
        raise ResourceSearchWorkerError("protocol_failure", retryable=True)
    event_type = payload.get("type")
    if event_type == "started":
        if set(payload) != {"protocol", "type", "state"}:
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        return ResourceSearchStartedEvent(state_from_payload(payload.get("state")))
    if event_type == "heartbeat":
        if set(payload) != {"protocol", "type", "state"}:
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        return ResourceSearchHeartbeatEvent(
            state_from_payload(
                payload.get("state"),
                pending_fallback=(
                    previous_state.pending if previous_state is not None else None
                ),
            )
        )
    if event_type == "page":
        if set(payload) != {
            "protocol",
            "type",
            "page",
            "fetched",
            "items",
            "state",
        }:
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        try:
            event = ResourceSearchPageEvent(
                page=payload.get("page"),  # type: ignore[arg-type]
                fetched=payload.get("fetched"),  # type: ignore[arg-type]
                items=_items_from_payload(
                    payload.get("items"),
                    maximum=MAX_WORKER_DELTA_ITEMS,
                ),
                state=state_from_payload(
                    payload.get("state"),
                    pending_fallback=(
                        previous_state.pending if previous_state is not None else None
                    ),
                ),
            )
            return validate_page_event(event)
        except ResourceSearchError:
            raise ResourceSearchWorkerError(
                "protocol_failure", retryable=True
            ) from None
    if event_type == "done":
        if set(payload) != {"protocol", "type", "complete", "state"} or not isinstance(
            payload.get("complete"), bool
        ):
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        try:
            return validate_worker_result(
                ResourceSearchWorkerResult(
                    complete=bool(payload["complete"]),
                    state=state_from_payload(
                        payload.get("state"),
                        pending_fallback=(
                            previous_state.pending
                            if previous_state is not None
                            else None
                        ),
                    ),
                )
            )
        except ResourceSearchError:
            raise ResourceSearchWorkerError(
                "protocol_failure", retryable=True
            ) from None
    if event_type == "error":
        if set(payload) != {"protocol", "type", "code", "retryable"} or not isinstance(
            payload.get("retryable"), bool
        ):
            raise ResourceSearchWorkerError("protocol_failure", retryable=True)
        try:
            return ResourceSearchWorkerError(
                str(payload.get("code") or ""),
                retryable=bool(payload["retryable"]),
            )
        except ResourceSearchError:
            raise ResourceSearchWorkerError(
                "protocol_failure", retryable=True
            ) from None
    raise ResourceSearchWorkerError("protocol_failure", retryable=True)


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protocol key")
        result[key] = value
    return result


def reject_non_json_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")
