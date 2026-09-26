"""Validation of resource search queries, ranges, identifiers and items."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
from collections.abc import Sequence

from ...core.catalog_code import normalize_catalog_code
from ...core.guards import (
    QueryError,
    contains_sensitive_transport_text,
    normalize_query,
)
from ...web_download.variant import (
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
    normalize_web_download_variant,
)
from .errors import (
    ResourceSearchConflictError,
    ResourceSearchError,
    ResourceSearchNotFoundError,
)
from .models import (
    ERROR_CODES,
    MAX_PENDING_ITEMS,
    MAX_RESOURCE_SEARCH_RESULTS,
    MAX_RESOURCE_TITLE_LENGTH,
    MAX_WORKER_DELTA_ITEMS,
    RANGE_BOUND_RE,
    RANGE_QUERY_RE,
    RESOURCE_SEARCH_SOURCE_IDS,
    RETRYABLE_ERROR_CODES,
    SESSION_ID_RE,
    TERMINAL_STATUSES,
    ResourceSearchItem,
    ResourceSearchPageEvent,
    ResourceSearchState,
    ResourceSearchWork,
    ResourceSearchWorkerResult,
)

def validate_state_transition(
    row: sqlite3.Row,
    state: ResourceSearchState,
) -> None:
    clean = validate_state(state)
    old_next = int(row["next_page"]) if row["next_page"] is not None else None
    if old_next is None:
        if clean.next_page is not None:
            raise ResourceSearchError("resource search cursor moved backwards")
    elif clean.next_page is not None and clean.next_page < old_next:
        raise ResourceSearchError("resource search cursor moved backwards")
    if (
        old_next is not None
        and clean.next_page == old_next
        and clean.cursor < int(row["pending_cursor"])
    ):
        raise ResourceSearchError("resource search cursor moved backwards")
    if clean.scanned_pages < int(row["scanned_pages"]):
        raise ResourceSearchError("resource search progress moved backwards")
    old_total = int(row["total_pages"]) if row["total_pages"] is not None else None
    if old_total is not None and (
        clean.total_pages is None or clean.total_pages < old_total
    ):
        raise ResourceSearchError("resource search page count moved backwards")


def validate_page_event(event: object) -> ResourceSearchPageEvent:
    if not isinstance(event, ResourceSearchPageEvent):
        raise ResourceSearchError("resource search page event is invalid")
    page = bounded_int(event.page, "page", 1, 999)
    if not isinstance(event.fetched, bool):
        raise ResourceSearchError("resource search page event is invalid")
    items = validate_items(event.items, maximum=MAX_WORKER_DELTA_ITEMS)
    state = validate_state(event.state)
    return ResourceSearchPageEvent(page, event.fetched, items, state)


def validate_worker_result(result: object) -> ResourceSearchWorkerResult:
    if not isinstance(result, ResourceSearchWorkerResult) or not isinstance(
        result.complete, bool
    ):
        raise ResourceSearchError("resource search worker result is invalid")
    state = validate_state(result.state)
    if result.complete != (state.next_page is None):
        raise ResourceSearchError("resource search worker result is inconsistent")
    return ResourceSearchWorkerResult(result.complete, state)


def validate_state(state: object) -> ResourceSearchState:
    if not isinstance(state, ResourceSearchState):
        raise ResourceSearchError("resource search cursor is invalid")
    next_page = _optional_bounded_int(state.next_page, "next_page", 1, 999)
    pending = validate_items(state.pending, maximum=MAX_PENDING_ITEMS)
    cursor = bounded_int(state.cursor, "cursor", 0, len(pending))
    total_pages = _optional_bounded_int(state.total_pages, "total_pages", 1, 999)
    scanned_pages = bounded_int(state.scanned_pages, "scanned_pages", 0, 999)
    if pending and next_page is None:
        raise ResourceSearchError("resource search cursor is invalid")
    if not pending and cursor:
        raise ResourceSearchError("resource search cursor is invalid")
    if next_page is not None and total_pages is not None and next_page > total_pages:
        raise ResourceSearchError("resource search cursor is invalid")
    return ResourceSearchState(
        next_page=next_page,
        pending=pending,
        cursor=cursor,
        total_pages=total_pages,
        scanned_pages=scanned_pages,
    )


def validate_items(
    values: object,
    *,
    maximum: int,
) -> tuple[ResourceSearchItem, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise ResourceSearchError("resource search items are invalid")
    if len(values) > maximum:
        raise ResourceSearchError("resource search items are invalid")
    clean: list[ResourceSearchItem] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, ResourceSearchItem):
            raise ResourceSearchError("resource search item is invalid")
        normalized = normalize_catalog_code(value.code, max_length=32)
        if normalized is None or normalized[0] != value.code:
            raise ResourceSearchError("resource search item is invalid")
        variants = validate_variants(value.available_variants)
        title = _validate_resource_title(value.title)
        if normalized[1] in seen:
            raise ResourceSearchError("resource search items contain duplicates")
        seen.add(normalized[1])
        clean.append(ResourceSearchItem(normalized[0], variants, title))
    return tuple(clean)


def validate_variants(values: object) -> tuple[MissavVariant, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise ResourceSearchError("resource search categories are invalid")
    try:
        variants = tuple(normalize_web_download_variant(item) for item in values)
    except ValueError as exc:
        raise ResourceSearchError("resource search categories are invalid") from exc
    ordered = tuple(variant for variant in WEB_DOWNLOAD_VARIANTS if variant in variants)
    if not ordered or ordered != variants:
        raise ResourceSearchError("resource search categories are invalid")
    return ordered


def variants_from_json(value: object) -> tuple[MissavVariant, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResourceSearchError("resource search categories are invalid") from exc
    return validate_variants(decoded)


def _validate_resource_title(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResourceSearchError("resource search item title is invalid")
    normalized = unicodedata.normalize("NFKC", value).replace("\x00", " ")
    clean = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    clean = " ".join(clean.split())
    if (
        not clean
        or len(clean) > MAX_RESOURCE_TITLE_LENGTH
        or clean != value
        or contains_sensitive_transport_text(clean)
    ):
        raise ResourceSearchError("resource search item title is invalid")
    return clean


def public_item(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, object]:
    variant_rows = connection.execute(
        "SELECT variant FROM resource_search_variants WHERE item_id = ?",
        (str(row["item_id"]),),
    ).fetchall()
    found = {str(variant_row[0]) for variant_row in variant_rows}
    variants = [variant for variant in WEB_DOWNLOAD_VARIANTS if variant in found]
    if not variants:
        raise ResourceSearchError("resource search item categories are missing")
    source = connection.execute(
        "SELECT s.source_id FROM resource_search_items i JOIN resource_search_sessions s "
        "ON s.session_id = i.session_id WHERE i.item_id = ?",
        (str(row["item_id"]),),
    ).fetchone()
    return {
        "item_id": str(row["item_id"]),
        "code": str(row["code"]),
        "title": str(row["title"]) if row["title"] is not None else None,
        "available_variants": variants,
        "source_ids": [str(source[0])] if source else [],
    }


def public_session(
    row: sqlite3.Row,
    *,
    items: list[dict[str, object]],
    filtered_total: int,
    limit: int,
    offset: int,
    keyword: str | None,
    variant: MissavVariant | None,
    pending_count: int,
) -> dict[str, object]:
    status = str(row["status"])
    item_count = int(row["item_count"])
    result_limit = int(row["result_limit"])
    scanned_pages = int(row["scanned_pages"])
    total_pages = int(row["total_pages"]) if row["total_pages"] is not None else None
    cursor = int(row["pending_cursor"])
    pending_remaining = max(0, pending_count - cursor)
    if status in {"completed", "limit_reached"}:
        percent = 100
    elif total_pages is not None and total_pages > 0:
        completed_pages = max(0, scanned_pages - int(pending_remaining > 0))
        page_fraction = completed_pages / total_pages
        if pending_count:
            page_fraction += min(cursor, pending_count) / pending_count / total_pages
        result_fraction = item_count / result_limit
        percent = min(99, max(0, int(max(page_fraction, result_fraction) * 100)))
    else:
        percent = min(99, int(item_count / result_limit * 100))
    next_page = int(row["next_page"]) if row["next_page"] is not None else None
    return {
        "session_id": str(row["session_id"]),
        "source_id": str(row["source_id"]),
        "query": str(row["query"]),
        "exact_match": bool(row["exact_match"]),
        "suffix_width": (
            int(row["suffix_width"]) if row["suffix_width"] is not None else None
        ),
        "start": int(row["range_start"]) if row["range_start"] is not None else None,
        "end": int(row["range_end"]) if row["range_end"] is not None else None,
        "status": status,
        "revision": int(row["revision"]),
        "result_limit": result_limit,
        "item_count": item_count,
        "error_code": str(row["error_code"]) if row["error_code"] else None,
        "retryable": bool(row["retryable"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "started_at": (
            float(row["started_at"]) if row["started_at"] is not None else None
        ),
        "heartbeat_at": (
            float(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None
        ),
        "finished_at": (
            float(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        "items": items,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": filtered_total,
            "has_more": offset + len(items) < filtered_total,
            "keyword": keyword,
            "variant": variant,
        },
        "progress": {
            "percent": percent,
            "items_found": item_count,
            "result_limit": result_limit,
            "scanned_pages": scanned_pages,
            "total_pages": total_pages,
            "next_page": next_page,
            "pending_total": pending_count,
            "pending_cursor": cursor,
            "pending_remaining": pending_remaining,
            "determinate": total_pages is not None,
        },
        "can_continue": status in {"limit_reached", "cancelled"}
        and result_limit < MAX_RESOURCE_SEARCH_RESULTS
        and next_page is not None,
        "can_retry": status == "failed" and bool(row["retryable"]),
        "can_cancel": status in {"queued", "running"},
        "can_remove": status in TERMINAL_STATUSES,
    }


def validate_source_id(value: object) -> str:
    if not isinstance(value, str) or value not in RESOURCE_SEARCH_SOURCE_IDS:
        raise ResourceSearchError("resource search source is invalid")
    return value


def validate_query(value: object) -> str:
    try:
        return normalize_query(value)
    except QueryError as exc:
        raise ResourceSearchError("resource search query is invalid") from exc


def _coerce_range_bound(value: object | None) -> object | None:
    # The web UI submits range bounds as ASCII digit strings to preserve
    # leading zeros (the batch rule parser accepts the same shape); anything
    # else falls through to the strict integer validation unchanged.
    if isinstance(value, str) and RANGE_BOUND_RE.fullmatch(value):
        return int(value)
    return value


def validate_range(
    *,
    suffix_width: object | None,
    start: object | None,
    end: object | None,
) -> tuple[int | None, int | None, int | None]:
    clean_width = _optional_bounded_int(
        _coerce_range_bound(suffix_width), "suffix_width", 1, 9
    )
    clean_start = _optional_bounded_int(
        _coerce_range_bound(start), "start", 0, 999_999_999
    )
    clean_end = _optional_bounded_int(_coerce_range_bound(end), "end", 0, 999_999_999)
    if clean_start is not None and clean_end is not None and clean_start > clean_end:
        raise ResourceSearchError("resource search range is invalid")
    return clean_width, clean_start, clean_end


def validate_range_query(
    query: str,
    *,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
) -> None:
    if suffix_width is None and start is None and end is None:
        return
    candidate = query.upper()
    if (
        len(candidate) > 24
        or not candidate.isascii()
        or not RANGE_QUERY_RE.fullmatch(candidate)
        or sum(character.isalpha() for character in candidate) < 2
    ):
        raise ResourceSearchError("resource search range query is invalid")


def validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not SESSION_ID_RE.fullmatch(value):
        raise ResourceSearchNotFoundError("resource search session was not found")
    return value


def validate_revision(value: object) -> int:
    return bounded_int(value, "expected_revision", 1, 2_147_483_647)


def require_revision(row: sqlite3.Row, expected: int) -> None:
    if int(row["revision"]) != expected:
        raise ResourceSearchConflictError("resource search revision changed")


def validate_optional_variant(value: object | None) -> MissavVariant | None:
    if value is None or value == "":
        return None
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise ResourceSearchError("resource search category filter is invalid") from exc


def validate_filter_keyword(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResourceSearchError("resource search keyword filter is invalid")
    if not value.strip():
        return None
    try:
        clean = normalize_query(value)
    except QueryError as exc:
        raise ResourceSearchError("resource search keyword filter is invalid") from exc
    return clean


def validate_item_ids(values: Sequence[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise ResourceSearchError("selected resources are invalid")
    if not 1 <= len(values) <= MAX_RESOURCE_SEARCH_RESULTS:
        raise ResourceSearchError("selected resources are invalid")
    clean: list[str] = []
    for value in values:
        if not isinstance(value, str) or not SESSION_ID_RE.fullmatch(value):
            raise ResourceSearchError("selected resources are invalid")
        if value in clean:
            raise ResourceSearchError("selected resources are duplicated")
        clean.append(value)
    return tuple(clean)


def validate_error_code(value: object, *, retryable: bool) -> str:
    if not isinstance(value, str) or value not in ERROR_CODES:
        raise ResourceSearchError("resource search error code is invalid")
    if (value in RETRYABLE_ERROR_CODES) != retryable:
        raise ResourceSearchError("resource search retry flag is invalid")
    return value


def bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResourceSearchError(f"resource search {name} is invalid")
    if not minimum <= value <= maximum:
        raise ResourceSearchError(f"resource search {name} is invalid")
    return value


def _optional_bounded_int(
    value: object | None,
    name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    return bounded_int(value, name, minimum, maximum)


def safe_timestamp(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ResourceSearchError("resource search clock is invalid") from exc
    if not math.isfinite(clean) or clean < 0:
        raise ResourceSearchError("resource search clock is invalid")
    return clean


def make_item_id(session_id: str, code_key: str) -> str:
    return hashlib.sha256(f"{session_id}:{code_key}".encode("ascii")).hexdigest()[:32]


def validate_work(work: object) -> ResourceSearchWork:
    if not isinstance(work, ResourceSearchWork):
        raise ResourceSearchError("resource search work is invalid")
    session_id = validate_session_id(work.session_id)
    source_id = validate_source_id(work.source_id)
    query = validate_query(work.query)
    result_limit = bounded_int(
        work.result_limit,
        "result_limit",
        0,
        MAX_RESOURCE_SEARCH_RESULTS,
    )
    width, start, end = validate_range(
        suffix_width=work.suffix_width,
        start=work.start,
        end=work.end,
    )
    validate_range_query(
        query,
        suffix_width=width,
        start=start,
        end=end,
    )
    return ResourceSearchWork(
        session_id=session_id,
        source_id=source_id,
        query=query,
        result_limit=result_limit,
        suffix_width=width,
        start=start,
        end=end,
        state=validate_state(work.state),
    )
