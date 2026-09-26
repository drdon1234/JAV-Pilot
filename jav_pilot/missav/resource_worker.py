from __future__ import annotations

import json
import sys
import threading

from ..search.resources.models import (
    MAX_RESOURCE_SEARCH_RESULTS,
    MAX_WORKER_EVENTS,
    MAX_WORKER_INPUT_BYTES,
    MAX_WORKER_LINE_BYTES,
    MAX_WORKER_OUTPUT_BYTES,
    RESOURCE_SEARCH_PROTOCOL_VERSION,
    ResourceSearchItem,
    ResourceSearchPageEvent,
    ResourceSearchState,
)
from ..search.resources.protocol import (
    reject_duplicate_keys as _reject_duplicate_keys,
    reject_non_json_constant as _reject_non_json_constant,
    state_from_payload as _state_from_payload,
    state_payload as _state_payload,
)
from ..search.resources.validation import (
    bounded_int as _bounded_int,
    validate_query as _validate_query,
    validate_range as _validate_range,
    validate_range_query as _validate_range_query,
    validate_source_id as _validate_source_id,
)


_output_lock = threading.Lock()
_output_bytes = 0
_output_events = 0
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "challenge_active",
        "navigation_timeout",
        "rate_limited",
        "upstream_unavailable",
    }
)
_PERMANENT_ERROR_CODES = frozenset(
    {
        "dependency_unavailable",
        "discovery_unavailable",
        "not_found",
        "parse_drift",
        "route_drift",
        "safety_rejected",
    }
)


def _write_event(payload: dict[str, object]) -> None:
    global _output_bytes, _output_events
    encoded = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    if len(encoded) > MAX_WORKER_LINE_BYTES:
        raise ValueError("resource search worker event is too large")
    with _output_lock:
        if (
            _output_bytes + len(encoded) > MAX_WORKER_OUTPUT_BYTES
            or _output_events + 1 > MAX_WORKER_EVENTS
        ):
            raise ValueError("resource search worker output is too large")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        _output_bytes += len(encoded)
        _output_events += 1


def _write_error(code: str, *, retryable: bool) -> None:
    _write_event(
        {
            "protocol": RESOURCE_SEARCH_PROTOCOL_VERSION,
            "type": "error",
            "code": code,
            "retryable": retryable,
        }
    )


def _worker_error_code(error: BaseException, *, retryable: bool) -> str:
    allowed = _RETRYABLE_ERROR_CODES if retryable else _PERMANENT_ERROR_CODES
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in allowed:
        return code
    return "upstream_unavailable" if retryable else "discovery_unavailable"


def _read_instruction() -> dict[str, object]:
    raw = sys.stdin.buffer.readline(MAX_WORKER_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_WORKER_INPUT_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid resource search instruction")
    if b"\r" in raw or sys.stdin.buffer.read(1):
        raise ValueError("invalid resource search instruction")
    payload = json.loads(
        raw[:-1].decode("ascii"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )
    if not isinstance(payload, dict) or set(payload) != {
        "protocol",
        "source_id",
        "query",
        "result_limit",
        "suffix_width",
        "start",
        "end",
        "state",
        "timeout_seconds",
    }:
        raise ValueError("invalid resource search instruction")
    if (
        type(payload.get("protocol")) is not int
        or payload.get("protocol") != RESOURCE_SEARCH_PROTOCOL_VERSION
    ):
        raise ValueError("invalid resource search instruction")
    source_id = _validate_source_id(payload.get("source_id"))
    query = _validate_query(payload.get("query"))
    result_limit = _bounded_int(
        payload.get("result_limit"),
        "result_limit",
        1,
        MAX_RESOURCE_SEARCH_RESULTS,
    )
    suffix_width, start, end = _validate_range(
        suffix_width=payload.get("suffix_width"),
        start=payload.get("start"),
        end=payload.get("end"),
    )
    _validate_range_query(
        query,
        suffix_width=suffix_width,
        start=start,
        end=end,
    )
    state = _state_from_payload(payload.get("state"))
    timeout_value = payload.get("timeout_seconds")
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise ValueError("invalid resource search instruction")
    timeout_seconds = float(timeout_value)
    if not 1.0 <= timeout_seconds <= 180.0:
        raise ValueError("invalid resource search instruction")
    return {
        "source_id": source_id,
        "query": query,
        "result_limit": result_limit,
        "suffix_width": suffix_width,
        "start": start,
        "end": end,
        "state": state,
        "timeout_seconds": timeout_seconds,
    }


def _worker_item_payload(item: ResourceSearchItem) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": item.code,
        "available_variants": list(item.available_variants),
    }
    if item.title is not None:
        payload["title"] = item.title
    return payload


def main() -> int:
    try:
        instruction = _read_instruction()
    except Exception:
        _write_error("invalid_instruction", retryable=False)
        return 0

    try:
        from .client import discover_resource_items
        from .errors import MissavError, MissavTransientError
        from .models import MissavResourceItem
    except Exception:
        _write_error("dependency_unavailable", retryable=False)
        return 0

    state_lock = threading.Lock()
    current_state = instruction["state"]
    if not isinstance(current_state, ResourceSearchState):
        _write_error("invalid_instruction", retryable=False)
        return 0
    heartbeat_stop = threading.Event()
    heartbeat_failed = threading.Event()
    heartbeat_thread: threading.Thread | None = None

    def heartbeat() -> None:
        while not heartbeat_stop.wait(2.0):
            try:
                with state_lock:
                    _write_event(
                        {
                            "protocol": RESOURCE_SEARCH_PROTOCOL_VERSION,
                            "type": "heartbeat",
                            "state": _state_payload(
                                current_state,
                                include_pending=False,
                            ),
                        }
                    )
            except Exception:
                heartbeat_failed.set()
                return

    def stop_heartbeat() -> None:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join()

    try:
        _write_event(
            {
                "protocol": RESOURCE_SEARCH_PROTOCOL_VERSION,
                "type": "started",
                "state": _state_payload(current_state),
            }
        )
        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name="jav-missav-resource-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

        def on_page(page: object) -> None:
            nonlocal current_state
            if heartbeat_failed.is_set():
                raise MissavTransientError("MissAV resource event stream failed")
            converted_state = ResourceSearchState(
                next_page=page.state.next_page,
                pending=tuple(
                    ResourceSearchItem(
                        item.code,
                        item.available_variants,
                        item.title,
                    )
                    for item in page.state.pending
                ),
                cursor=page.state.cursor,
                total_pages=page.state.total_pages,
                scanned_pages=page.state.scanned_pages,
            )
            converted_items = tuple(
                ResourceSearchItem(
                    item.code,
                    item.available_variants,
                    item.title,
                )
                for item in page.items
            )
            event = ResourceSearchPageEvent(
                page=page.page,
                fetched=page.fetched,
                items=converted_items,
                state=converted_state,
            )
            with state_lock:
                pending_changed = current_state.pending != converted_state.pending
                _write_event(
                    {
                        "protocol": RESOURCE_SEARCH_PROTOCOL_VERSION,
                        "type": "page",
                        "page": event.page,
                        "fetched": event.fetched,
                        "items": [_worker_item_payload(item) for item in event.items],
                        "state": _state_payload(
                            event.state,
                            include_pending=pending_changed,
                        ),
                    }
                )
                current_state = converted_state

        pending = tuple(
            MissavResourceItem(
                item.code,
                item.available_variants,
                item.title,
            )
            for item in current_state.pending
        )
        result = discover_resource_items(
            instruction["query"],
            result_limit=instruction["result_limit"],
            suffix_width=instruction["suffix_width"],
            start=instruction["start"],
            end=instruction["end"],
            next_page=current_state.next_page,
            pending=pending,
            cursor=current_state.cursor,
            total_pages=current_state.total_pages,
            scanned_pages=current_state.scanned_pages,
            timeout_seconds=instruction["timeout_seconds"],
            on_page=on_page,
        )
        final_state = ResourceSearchState(
            next_page=result.state.next_page,
            pending=tuple(
                ResourceSearchItem(
                    item.code,
                    item.available_variants,
                    item.title,
                )
                for item in result.state.pending
            ),
            cursor=result.state.cursor,
            total_pages=result.state.total_pages,
            scanned_pages=result.state.scanned_pages,
        )
        stop_heartbeat()
        if heartbeat_failed.is_set():
            raise MissavTransientError("MissAV resource event stream failed")
        with state_lock:
            pending_changed = current_state.pending != final_state.pending
            _write_event(
                {
                    "protocol": RESOURCE_SEARCH_PROTOCOL_VERSION,
                    "type": "done",
                    "complete": result.complete,
                    "state": _state_payload(
                        final_state,
                        include_pending=pending_changed,
                    ),
                }
            )
            current_state = final_state
        return 0
    except MissavTransientError as exc:
        stop_heartbeat()
        _write_error(_worker_error_code(exc, retryable=True), retryable=True)
        return 0
    except MissavError as exc:
        stop_heartbeat()
        _write_error(_worker_error_code(exc, retryable=False), retryable=False)
        return 0
    except Exception:
        stop_heartbeat()
        _write_error("internal_failure", retryable=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
