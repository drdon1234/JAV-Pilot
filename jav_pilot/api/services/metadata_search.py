"""Persistent metadata search sessions and their continuation envelopes."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from urllib.parse import urlencode

from ...config.settings import SettingsError, load_settings, source_list
from ...config.source_catalog import MAX_SEARCH_SOURCES
from ...core.catalog_code import canonical_catalog_code
from ...core.guards import (
    QueryError,
    contains_sensitive_transport_text,
    normalize_query,
)
from ...core.models import (
    MagnetInfo,
    Rating,
    RelatedRef,
    SearchBounds,
    SearchContinuation,
    SearchContinuationSource,
    SearchResult,
    SourceDetails,
)
from ...search.engine import aggregate_results
from ...search.session_store import (
    MetadataSearchStoreError,
    SQLiteMetadataSearchStore,
)
from .. import state
from ..registries import SearchContinuationEnvelope
from ..request import (
    filters_param,
    query_params,
    semantic_refs_param,
    valid_request_id,
)
from .enrichment import source_record
from .environment import metadata_search_database_path


def metadata_search_store() -> SQLiteMetadataSearchStore:
    path = metadata_search_database_path()
    with state.METADATA_SEARCH_STORE_LOCK:
        if state.METADATA_SEARCH_STORE is not None and state.METADATA_SEARCH_STORE_PATH == path:
            return state.METADATA_SEARCH_STORE
        store = SQLiteMetadataSearchStore(path)
        store.fail_interrupted()
        state.METADATA_SEARCH_STORE = store
        state.METADATA_SEARCH_STORE_PATH = path
        return store


def metadata_search_query_params(
    query_string: str, *, allowed: set[str]
) -> dict[str, list[str]]:
    params = query_params(query_string)
    if any(key not in allowed or len(values) != 1 for key, values in params.items()):
        raise QueryError("invalid metadata search query")
    return params


_METADATA_SEARCH_REQUEST_KEYS = frozenset(
    {
        "request_id",
        "query",
        "sources",
        "result_limit",
        "fetch_magnets",
        "filters",
        "sort",
        "match",
        "search_kind",
        "semantic_refs",
        "continuation_token",
    }
)
_METADATA_SEARCH_FIELD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def prepare_metadata_search_session(
    payload: dict[str, object],
) -> dict[str, object]:
    if set(payload) != _METADATA_SEARCH_REQUEST_KEYS:
        raise QueryError("invalid metadata search request fields")
    raw_request_id = payload.get("request_id")
    if not isinstance(raw_request_id, str):
        raise QueryError("invalid request_id")
    request_id = raw_request_id.strip()
    if not valid_request_id(request_id):
        raise QueryError("invalid request_id")
    query = normalize_query(payload.get("query"))
    if contains_sensitive_transport_text(query):
        raise QueryError("query contains sensitive transport data")
    raw_sources = payload.get("sources")
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or len(raw_sources) > MAX_SEARCH_SOURCES
        or any(
            not isinstance(source, str)
            or _METADATA_SEARCH_FIELD_ID_RE.fullmatch(source.strip()) is None
            for source in raw_sources
        )
    ):
        raise QueryError("invalid search sources")
    clean_sources = [str(source).strip() for source in raw_sources]
    if len(set(clean_sources)) != len(clean_sources):
        raise QueryError("duplicate search sources")
    try:
        sources = source_list(
            ",".join(clean_sources),
            load_settings(),
        )
    except (SettingsError, ValueError) as exc:
        raise QueryError(str(exc)) from exc
    raw_result_limit = payload.get("result_limit")
    if (
        isinstance(raw_result_limit, bool)
        or not isinstance(raw_result_limit, int)
        or not 1 <= raw_result_limit <= 999
    ):
        raise QueryError("result_limit must be between 1 and 999")
    result_limit = raw_result_limit
    fetch_magnets = payload.get("fetch_magnets")
    if not isinstance(fetch_magnets, bool):
        raise QueryError("fetch_magnets must be a boolean")
    filters = payload.get("filters")
    semantic_refs = payload.get("semantic_refs", {})
    if not isinstance(filters, dict) or not isinstance(semantic_refs, dict):
        raise QueryError("invalid search filters")
    if len(filters) > 24 or len(semantic_refs) > 16:
        raise QueryError("too many search filters")
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or _METADATA_SEARCH_FIELD_ID_RE.fullmatch(key) is None
        or len(value) > 256
        or contains_sensitive_transport_text(key)
        or contains_sensitive_transport_text(value)
        for values in (filters, semantic_refs)
        for key, value in values.items()
    ):
        raise QueryError("invalid search filters")
    for field in ("sort", "match", "search_kind"):
        if not isinstance(payload.get(field), str):
            raise QueryError("invalid search options")
    try:
        bounds = SearchBounds(
            result_limit=result_limit,
            sort=str(payload.get("sort") or "relevance"),
            match=str(payload.get("match") or "auto"),
            search_kind=str(payload.get("search_kind") or "keyword"),
            filters=filters,
            semantic_refs=semantic_refs,
        ).normalized()
    except (TypeError, ValueError) as exc:
        raise QueryError(str(exc)) from exc
    if (
        bounds.sort != payload["sort"]
        or bounds.match != payload["match"]
        or bounds.search_kind != payload["search_kind"]
    ):
        raise QueryError("invalid search options")
    raw_continuation_token = payload.get("continuation_token")
    if raw_continuation_token is not None and not isinstance(
        raw_continuation_token, str
    ):
        raise QueryError("invalid search continuation")
    continuation_token = str(raw_continuation_token or "").strip()
    if continuation_token and not re.fullmatch(
        r"[A-Za-z0-9_-]{24,64}", continuation_token
    ):
        raise QueryError("invalid search continuation")
    request: dict[str, object] = {
        "query": query,
        "sources": list(sources),
        "result_limit": result_limit,
        "fetch_magnets": fetch_magnets,
        "filters": dict(sorted(bounds.filters.items())),
        "sort": bounds.sort,
        "match": bounds.match,
        "search_kind": bounds.search_kind,
        "semantic_refs": dict(sorted(bounds.semantic_refs.items())),
        # Continuation capabilities remain memory-only and are never persisted.
        "continuation_token": None,
    }
    parameters: list[tuple[str, str]] = [
        ("request_id", request_id),
        ("q", query),
        ("source", ",".join(sources)),
        ("result_limit", str(result_limit)),
        ("magnets", "1" if fetch_magnets else "0"),
        ("sort", bounds.sort),
        ("match", bounds.match),
        ("kind", bounds.search_kind),
    ]
    parameters.extend((f"filter.{key}", value) for key, value in bounds.filters.items())
    parameters.extend(
        (f"ref.{key}", value) for key, value in bounds.semantic_refs.items()
    )
    query_string = urlencode(parameters)
    parsed = query_params(query_string)
    filters_param(parsed)
    semantic_refs_param(parsed)
    fingerprint = hashlib.sha256(
        json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "request_id": request_id,
        "fingerprint": fingerprint,
        "request": request,
        "query_string": query_string,
        "continuation_token": continuation_token,
    }


METADATA_SEARCH_ERROR_REDACTION = "搜索错误详情已隐藏"


def sanitize_metadata_search_event_value(
    value: object, *, field_name: str = ""
) -> object:
    normalized_name = field_name.strip().lower()
    if normalized_name in {"error", "errors"} or normalized_name.endswith("_error"):
        if isinstance(value, dict):
            return {
                str(key): sanitize_metadata_search_event_value(
                    item, field_name="error"
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                sanitize_metadata_search_event_value(item, field_name="error")
                for item in value
            ]
        return METADATA_SEARCH_ERROR_REDACTION
    if isinstance(value, dict):
        return {
            str(key): sanitize_metadata_search_event_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_metadata_search_event_value(item) for item in value]
    return value


def metadata_search_continuation_payload(
    envelope: SearchContinuationEnvelope,
) -> dict[str, object]:
    persisted_records = _metadata_search_persisted_records(envelope)
    payload = {
        "binding": envelope.binding,
        "works": [work.work_id for work in envelope.works],
        "continuation": {
            "sources": [
                {
                    "source_id": source.source_id,
                    "records": [
                        record.to_dict()
                        for record in persisted_records.get(
                            source.source_id,
                            source.records,
                        )
                    ],
                    "next_page": source.next_page,
                }
                for source in envelope.state.sources
            ],
            "pages_scanned": envelope.state.pages_scanned,
            "result_limit": envelope.state.result_limit,
            "errors": envelope.state.errors,
        },
    }
    sanitized = sanitize_metadata_search_event_value(payload)
    if not isinstance(sanitized, dict):
        raise ValueError("metadata search continuation is invalid")
    return sanitized


def _metadata_search_persisted_records(
    envelope: SearchContinuationEnvelope,
) -> dict[str, tuple[SearchResult, ...]]:
    """Fold completed detail enrichment back into continuation source records."""
    enriched_by_source: dict[str, list[SearchResult]] = {}
    for work in envelope.works:
        for source in work.sources:
            enriched_by_source.setdefault(source.source_id, []).append(
                source_record(work, source)
            )

    persisted: dict[str, tuple[SearchResult, ...]] = {}
    for source in envelope.state.sources:
        candidates = enriched_by_source.get(source.source_id, [])
        indexes: dict[tuple[str, str], list[int]] = {}
        for index, candidate in enumerate(candidates):
            indexes.setdefault(_metadata_search_record_identity(candidate), []).append(
                index
            )
        used: set[int] = set()
        records: list[SearchResult] = []
        for record in source.records:
            match = next(
                (
                    index
                    for index in indexes.get(
                        _metadata_search_record_identity(record), []
                    )
                    if index not in used
                ),
                None,
            )
            if match is None:
                records.append(record)
                continue
            used.add(match)
            records.append(candidates[match])
        persisted[source.source_id] = tuple(records)
    return persisted


def _metadata_search_record_identity(record: SearchResult) -> tuple[str, str]:
    canonical_code = canonical_catalog_code(record.code)
    if canonical_code:
        return "code", canonical_code
    detail_url = str(record.url or "").strip()
    if detail_url:
        return "url", detail_url
    return "title", " ".join(record.title.split()).casefold()


def restore_metadata_search_continuation(
    store: SQLiteMetadataSearchStore,
    request_id: str,
) -> None:
    if state.METADATA_SEARCH_CONTINUATIONS.contains_valid(request_id):
        return
    with state.METADATA_SEARCH_CONTINUATION_RESTORE_LOCK:
        if state.METADATA_SEARCH_CONTINUATIONS.contains_valid(request_id):
            return
        stored = store.continuation(request_id)
        if stored is None:
            return
        try:
            envelope = _metadata_search_continuation_envelope(stored["state"])
            mode = str(stored["mode"])
        except (KeyError, TypeError, ValueError, MetadataSearchStoreError):
            return
        for _attempt in range(8):
            token = secrets.token_urlsafe(24)
            if state.SEARCH_CONTINUATIONS.remember(token, envelope):
                state.METADATA_SEARCH_CONTINUATIONS.remember(
                    request_id,
                    {
                        "can_continue": True,
                        "continuation_token": token,
                        "continuation_mode": mode,
                    },
                )
                return


def _metadata_search_continuation_envelope(
    value: object,
) -> SearchContinuationEnvelope:
    if not isinstance(value, dict) or set(value) != {
        "binding",
        "continuation",
        "works",
    }:
        raise ValueError("metadata search continuation is invalid")
    binding = _metadata_search_nested_tuple(value["binding"])
    if (
        not isinstance(binding, tuple)
        or len(binding) != 13
        or binding[0] != "search-continuation"
        or not isinstance(binding[1], str)
        or not isinstance(binding[2], tuple)
        or not all(isinstance(source, str) for source in binding[2])
        or not isinstance(binding[7], str)
    ):
        raise ValueError("metadata search continuation is invalid")
    raw_state = value["continuation"]
    raw_work_ids = value["works"]
    if (
        not isinstance(raw_work_ids, list)
        or len(raw_work_ids) > 999
        or any(not isinstance(work_id, str) or not work_id for work_id in raw_work_ids)
        or len(set(raw_work_ids)) != len(raw_work_ids)
    ):
        raise ValueError("metadata search continuation is invalid")
    if not isinstance(raw_state, dict) or set(raw_state) != {
        "sources",
        "pages_scanned",
        "result_limit",
        "errors",
    }:
        raise ValueError("metadata search continuation is invalid")
    raw_sources = raw_state["sources"]
    raw_errors = raw_state["errors"]
    if (
        not isinstance(raw_sources, list)
        or not 1 <= len(raw_sources) <= MAX_SEARCH_SOURCES
        or not isinstance(raw_errors, dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in raw_errors.items()
        )
    ):
        raise ValueError("metadata search continuation is invalid")
    sources: list[SearchContinuationSource] = []
    records: list[SearchResult] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict) or set(raw_source) != {
            "source_id",
            "records",
            "next_page",
        }:
            raise ValueError("metadata search continuation is invalid")
        source_id = raw_source["source_id"]
        raw_records = raw_source["records"]
        next_page = raw_source["next_page"]
        if (
            not isinstance(source_id, str)
            or not isinstance(raw_records, list)
            or len(raw_records) > 10_000
            or (
                next_page is not None
                and (isinstance(next_page, bool) or not isinstance(next_page, int))
            )
        ):
            raise ValueError("metadata search continuation is invalid")
        decoded = tuple(_metadata_search_result(item) for item in raw_records)
        records.extend(decoded)
        sources.append(
            SearchContinuationSource(
                source_id=source_id,
                records=decoded,
                next_page=next_page,
            )
        )
    pages_scanned = raw_state["pages_scanned"]
    result_limit = raw_state["result_limit"]
    if (
        isinstance(pages_scanned, bool)
        or not isinstance(pages_scanned, int)
        or isinstance(result_limit, bool)
        or not isinstance(result_limit, int)
        or not 0 <= pages_scanned <= 10_000
        or not 1 <= result_limit <= 999
    ):
        raise ValueError("metadata search continuation is invalid")
    continuation = SearchContinuation(
        sources=tuple(sources),
        pages_scanned=pages_scanned,
        result_limit=result_limit,
        errors=dict(raw_errors),
    )
    aggregated = {
        work.work_id: work
        for work in aggregate_results(records, sort=binding[7])
    }
    if any(work_id not in aggregated for work_id in raw_work_ids):
        raise ValueError("metadata search continuation is invalid")
    works = tuple(aggregated[work_id] for work_id in raw_work_ids)
    return SearchContinuationEnvelope(binding=binding, state=continuation, works=works)


def _metadata_search_nested_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(_metadata_search_nested_tuple(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("metadata search continuation is invalid")


def _metadata_search_result(value: object) -> SearchResult:
    expected = {
        "source",
        "title",
        "url",
        "code",
        "date",
        "actors",
        "tags",
        "details",
        "magnet_hint",
        "magnets",
        "metadata",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("metadata search continuation is invalid")
    source = _metadata_required_string(value["source"])
    title = _metadata_required_string(value["title"])
    actors = _metadata_string_tuple(value["actors"], maximum=500)
    tags = _metadata_string_tuple(value["tags"], maximum=500)
    raw_magnets = value["magnets"]
    if not isinstance(raw_magnets, list) or len(raw_magnets) > 2_000:
        raise ValueError("metadata search continuation is invalid")
    if value["magnet_hint"] not in {"available", "unavailable", "unknown"}:
        raise ValueError("metadata search continuation is invalid")
    if not isinstance(value["metadata"], dict):
        raise ValueError("metadata search continuation is invalid")
    return SearchResult(
        source=source,
        title=title,
        url=_metadata_optional_string(value["url"]),
        code=_metadata_optional_string(value["code"]),
        date=_metadata_optional_string(value["date"]),
        actors=actors,
        tags=tags,
        details=_metadata_source_details(value["details"]),
        magnet_hint=value["magnet_hint"],
        magnets=tuple(_metadata_magnet(item) for item in raw_magnets),
        metadata=dict(value["metadata"]),
    )


def _metadata_source_details(value: object) -> SourceDetails:
    expected = {
        "title",
        "original_title",
        "release_date",
        "duration_minutes",
        "duration_text",
        "rating",
        "makers",
        "publishers",
        "series",
        "directors",
        "actors",
        "tags",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("metadata search continuation is invalid")
    duration = value["duration_minutes"]
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int)
    ):
        raise ValueError("metadata search continuation is invalid")
    rating_value = value["rating"]
    rating = None
    if rating_value is not None:
        if not isinstance(rating_value, dict) or set(rating_value) != {
            "value",
            "votes",
            "text",
        }:
            raise ValueError("metadata search continuation is invalid")
        score = rating_value["value"]
        votes = rating_value["votes"]
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, (int, float))
        ):
            raise ValueError("metadata search continuation is invalid")
        if votes is not None and (
            isinstance(votes, bool) or not isinstance(votes, int)
        ):
            raise ValueError("metadata search continuation is invalid")
        rating = Rating(
            value=float(score) if score is not None else None,
            votes=votes,
            text=_metadata_optional_string(rating_value["text"]),
        )
    return SourceDetails(
        title=_metadata_optional_string(value["title"]),
        original_title=_metadata_optional_string(value["original_title"]),
        release_date=_metadata_optional_string(value["release_date"]),
        duration_minutes=duration,
        duration_text=_metadata_optional_string(value["duration_text"]),
        rating=rating,
        makers=_metadata_related_refs(value["makers"]),
        publishers=_metadata_related_refs(value["publishers"]),
        series=_metadata_related_refs(value["series"]),
        directors=_metadata_related_refs(value["directors"]),
        actors=_metadata_related_refs(value["actors"]),
        tags=_metadata_related_refs(value["tags"]),
    )


def _metadata_related_refs(value: object) -> tuple[RelatedRef, ...]:
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError("metadata search continuation is invalid")
    output: list[RelatedRef] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "label", "url"}
            or item["kind"]
            not in {
                "keyword",
                "code",
                "actor",
                "tag",
                "series",
                "maker",
                "publisher",
                "director",
            }
        ):
            raise ValueError("metadata search continuation is invalid")
        output.append(
            RelatedRef(
                kind=item["kind"],
                label=_metadata_required_string(item["label"]),
                url=_metadata_optional_string(item["url"]),
            )
        )
    return tuple(output)


def _metadata_magnet(value: object) -> MagnetInfo:
    expected = {
        "uri",
        "info_hash",
        "display_name",
        "trackers",
        "exact_length",
        "params",
        "reported_size_text",
        "reported_size_bytes",
        "badges",
    }
    activity_fields = {"reported_seeders", "reported_leechers", "reported_at"}
    if not isinstance(value, dict) or not expected <= set(value) or set(value) - expected - activity_fields:
        raise ValueError("metadata search continuation is invalid")
    exact_length = value["exact_length"]
    reported_size = value["reported_size_bytes"]
    if any(
        item is not None and (isinstance(item, bool) or not isinstance(item, int))
        for item in (exact_length, reported_size, value.get("reported_seeders"), value.get("reported_leechers"))
    ):
        raise ValueError("metadata search continuation is invalid")
    params = value["params"]
    if not isinstance(params, dict) or any(
        not isinstance(key, str)
        or not isinstance(items, list)
        or any(not isinstance(item, str) for item in items)
        for key, items in params.items()
    ):
        raise ValueError("metadata search continuation is invalid")
    return MagnetInfo(
        uri=_metadata_required_string(value["uri"]),
        info_hash=_metadata_required_string(value["info_hash"]),
        display_name=_metadata_optional_string(value["display_name"]),
        trackers=_metadata_string_tuple(value["trackers"], maximum=500),
        exact_length=exact_length,
        params={str(key): list(items) for key, items in params.items()},
        reported_size_text=_metadata_optional_string(value["reported_size_text"]),
        reported_size_bytes=reported_size,
        badges=_metadata_string_tuple(value["badges"], maximum=500),
        reported_seeders=value.get("reported_seeders"),
        reported_leechers=value.get("reported_leechers"),
        reported_at=_metadata_optional_string(value.get("reported_at")),
    )


def _metadata_required_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("metadata search continuation is invalid")
    return value


def _metadata_optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _metadata_required_string(value)


def _metadata_string_tuple(value: object, *, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError("metadata search continuation is invalid")
    return tuple(value)
