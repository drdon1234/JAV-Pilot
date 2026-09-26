"""Detail prefetch manager lifecycle and snapshot resolution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from ...config.settings import load_settings, search_sites
from ...core.guards import QueryError
from ...core.models import SearchBounds
from ...core.observability import emit_json_log
from ...indexers.torznab import TorznabIndexer
from ...search.detail_prefetch import (
    DetailPrefetchManager,
    DetailPrefetchResolveError,
    DetailPrefetchUnavailableError,
    DetailPrefetchValidationError,
    select_snapshot_sources,
    work_result_from_snapshot,
)
from ...search.engine import default_indexers, search
from ...search.session_store import MetadataSearchStoreError
from .. import state
from ..request import valid_request_id, valid_work_id
from .enrichment import JavDbFetcherPool, code_key, enrich_stream_work
from .environment import app_revision, detail_prefetch_database_path
from .history import require_operational_mode
from .metadata_search import metadata_search_store
from .search_capacity import acquire_search_capacity, release_search_capacity


def _detail_prefetch_config_fingerprint() -> str:
    settings = load_settings()
    sites: list[dict[str, object]] = []
    for site in search_sites(settings):
        sites.append(
            {
                "id": site.get("id"),
                "parser_profile": site.get("parser_profile"),
                "base_url": site.get("base_url"),
                "search": site.get("search"),
                "filters": site.get("filters"),
                "parser_rules_mode": site.get("parser_rules_mode"),
                "parser_rules": site.get("parser_rules"),
            }
        )
    payload = {
        "sites": sites,
        "revision": app_revision(),
        "javdb_fetcher": os.environ.get("JAV_PILOT_JAVDB_FETCHER", "auto")
        .strip()
        .lower(),
    }
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def detail_prefetch_manager() -> DetailPrefetchManager:
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            DetailPrefetchUnavailableError(
                "detail prefetch is unavailable in maintenance mode"
            )
        )
        with state.DETAIL_PREFETCH_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise DetailPrefetchUnavailableError("server is shutting down")
            if state.DETAIL_PREFETCH is None:
                state.DETAIL_PREFETCH = DetailPrefetchManager(
                    detail_prefetch_database_path(),
                    _resolve_detail_prefetch,
                    _detail_prefetch_config_fingerprint,
                    worker_count=1,
                )
            return state.DETAIL_PREFETCH


def detail_prefetch_cached_result(
    work_id: str, sources: tuple[str, ...]
) -> dict[str, object] | None:
    with state.DETAIL_PREFETCH_LOCK:
        manager = state.DETAIL_PREFETCH
    if manager is None:
        return None
    return manager.cached_result(work_id, sources)


def shutdown_detail_prefetch_manager(*, timeout: float) -> bool:
    with state.DETAIL_PREFETCH_LOCK:
        manager = state.DETAIL_PREFETCH
    if manager is None:
        return True
    try:
        stopped = manager.shutdown(timeout=timeout)
    except Exception:
        stopped = False
    if stopped:
        with state.DETAIL_PREFETCH_LOCK:
            if state.DETAIL_PREFETCH is manager:
                state.DETAIL_PREFETCH = None
    else:
        emit_json_log(
            "detail_prefetch",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    return bool(stopped)


def _prepare_detail_prefetch_snapshot(
    value: object,
    source_scope: str,
    settings: dict[str, object],
) -> dict[str, object]:
    snapshot = select_snapshot_sources(value, source_scope)
    registry = default_indexers(settings)
    configured_sites = {
        str(site.get("id") or ""): site for site in search_sites(settings)
    }
    has_lookup_identity = bool(snapshot.get("canonical_code") or snapshot.get("code"))
    sources = snapshot.get("sources")
    if not isinstance(sources, list):
        raise DetailPrefetchValidationError("detail prefetch sources are invalid")
    for source in sources:
        if not isinstance(source, dict):
            raise DetailPrefetchValidationError("detail prefetch source is invalid")
        source_id = str(source.get("source_id") or "")
        site = configured_sites.get(source_id)
        if source_id not in registry or site is None:
            raise DetailPrefetchValidationError("detail prefetch source is unavailable")
        detail_url = str(source.get("detail_url") or "").strip()
        if detail_url:
            indexer = registry[source_id]
            source_policy = getattr(indexer, "detail_url_allowed", None)
            allowed = (
                bool(source_policy(detail_url))
                if callable(source_policy)
                else _detail_prefetch_url_allowed(
                    detail_url, str(site.get("base_url") or "")
                )
            )
            if not allowed:
                raise DetailPrefetchValidationError(
                    "detail prefetch source URL is invalid"
                )
        if not detail_url and not (
            has_lookup_identity or str(source.get("raw_code") or "").strip()
        ):
            raise DetailPrefetchValidationError(
                "detail prefetch source cannot be recovered"
            )
    return snapshot


def detail_prefetch_snapshots_from_search_session(
    request_id: str,
    work_ids: object,
    source_scope: str,
    settings: dict[str, object],
) -> list[dict[str, object]]:
    if not valid_request_id(request_id):
        raise DetailPrefetchValidationError("detail prefetch search request is invalid")
    if not isinstance(work_ids, list) or not 1 <= len(work_ids) <= 999:
        raise DetailPrefetchValidationError(
            "detail prefetch work_ids must contain between 1 and 999 works"
        )
    selected_ids: list[str] = []
    seen_ids: set[str] = set()
    for value in work_ids:
        if not isinstance(value, str):
            raise DetailPrefetchValidationError("detail prefetch work_id is invalid")
        work_id = value.strip()
        if not valid_work_id(work_id):
            raise DetailPrefetchValidationError("detail prefetch work_id is invalid")
        if work_id not in seen_ids:
            seen_ids.add(work_id)
            selected_ids.append(work_id)

    stored = metadata_search_store().snapshot(request_id)
    request = stored.get("request")
    if not isinstance(request, Mapping):
        raise MetadataSearchStoreError("metadata search request is corrupted")
    raw_sources = request.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise MetadataSearchStoreError("metadata search request is corrupted")
    search_sources: list[str] = []
    for value in raw_sources:
        source_id = str(value or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source_id):
            raise MetadataSearchStoreError("metadata search request is corrupted")
        if source_id not in search_sources:
            search_sources.append(source_id)
    clean_scope = str(source_scope or "").strip().lower()
    if clean_scope == "all":
        target_sources = search_sources
    elif clean_scope in search_sources:
        target_sources = [clean_scope]
    else:
        raise DetailPrefetchValidationError(
            "detail prefetch source is not present in the search session"
        )

    available = {
        str(work["work_id"]): work for work in _metadata_search_snapshot_works(stored)
    }
    missing = [work_id for work_id in selected_ids if work_id not in available]
    if missing:
        raise DetailPrefetchValidationError(
            "detail prefetch work is not present in the search session"
        )
    return [
        _prepare_detail_prefetch_snapshot(
            _detail_prefetch_expand_sources(available[work_id], target_sources),
            clean_scope,
            settings,
        )
        for work_id in selected_ids
    ]


def _metadata_search_snapshot_works(
    stored: Mapping[str, object],
) -> list[dict[str, object]]:
    events = stored.get("events")
    if not isinstance(events, list):
        raise MetadataSearchStoreError("metadata search snapshot is corrupted")
    works: OrderedDict[str, dict[str, object]] = OrderedDict()

    def upsert(values: object, *, replace_all: bool = False) -> None:
        if not isinstance(values, list):
            raise MetadataSearchStoreError("metadata search snapshot is corrupted")
        next_works: OrderedDict[str, dict[str, object]] = OrderedDict()
        target = next_works if replace_all else works
        for value in values:
            if not isinstance(value, Mapping):
                raise MetadataSearchStoreError("metadata search snapshot is corrupted")
            work_id = str(value.get("work_id") or "").strip()
            if not valid_work_id(work_id):
                raise MetadataSearchStoreError("metadata search snapshot is corrupted")
            target[work_id] = dict(value)
        if replace_all:
            works.clear()
            works.update(next_works)

    for event in events:
        if not isinstance(event, Mapping) or not isinstance(
            event.get("payload"), Mapping
        ):
            raise MetadataSearchStoreError("metadata search snapshot is corrupted")
        event_name = str(event.get("event") or "")
        payload = event["payload"]
        if event_name in {"source", "base"}:
            upsert(payload.get("results"), replace_all=True)
        elif event_name == "delta":
            upsert(payload.get("delta"))
        elif event_name == "result":
            upsert([payload.get("result")])
    return list(works.values())


def _detail_prefetch_expand_sources(
    work: Mapping[str, object], source_ids: Sequence[str]
) -> dict[str, object]:
    raw_sources = work.get("sources")
    if not isinstance(raw_sources, list):
        raise MetadataSearchStoreError("metadata search snapshot is corrupted")
    source_by_id: dict[str, dict[str, object]] = {}
    for value in raw_sources:
        if not isinstance(value, Mapping):
            raise MetadataSearchStoreError("metadata search snapshot is corrupted")
        source_id = str(value.get("source_id") or "").strip().lower()
        if source_id:
            source_by_id[source_id] = dict(value)
    lookup_code = str(
        work.get("canonical_code")
        or work.get("code")
        or next(
            (
                source.get("raw_code")
                for source in source_by_id.values()
                if source.get("raw_code")
            ),
            "",
        )
    ).strip()
    effective_source_ids = list(source_ids)
    if not lookup_code:
        effective_source_ids = [
            source_id for source_id in effective_source_ids if source_id in source_by_id
        ]
        if not effective_source_ids:
            raise DetailPrefetchValidationError(
                "detail prefetch source is not present in the work snapshot"
            )
    title = str(work.get("title") or lookup_code or "Unknown work").strip()
    release_date = work.get("release_date")
    magnet_hint = work.get("magnet_hint")
    expanded: list[dict[str, object]] = []
    for source_id in effective_source_ids:
        source = source_by_id.get(source_id)
        if source is not None:
            expanded.append(source)
            continue
        expanded.append(
            {
                "source_id": source_id,
                "raw_code": lookup_code or None,
                "title": title,
                "detail_url": None,
                "release_date": release_date,
                "images": [],
                "magnet_hint": magnet_hint,
                "parse_status": "summary",
                "error": None,
            }
        )
    return {**dict(work), "sources": expanded}


def _detail_prefetch_url_allowed(detail_url: str, base_url: str) -> bool:
    try:
        detail = urlsplit(detail_url)
        base = urlsplit(base_url)
        if (
            detail.scheme not in {"http", "https"}
            or base.scheme not in {"http", "https"}
            or not detail.hostname
            or not base.hostname
            or detail.username is not None
            or detail.password is not None
            or detail.fragment
            or _url_origin(detail) != _url_origin(base)
        ):
            return False
        query = parse_qs(detail.query, keep_blank_values=True, max_num_fields=64)
    except (TypeError, ValueError):
        return False
    sensitive = re.compile(
        r"(?:auth|authorization|cookie|credential|key|password|secret|session|sign|token)",
        flags=re.IGNORECASE,
    )
    return not any(sensitive.search(key) for key in query)


def _url_origin(parsed: object) -> tuple[str, str, int | None]:
    scheme = str(getattr(parsed, "scheme", "") or "").lower()
    hostname = str(getattr(parsed, "hostname", "") or "").rstrip(".").lower()
    port = getattr(parsed, "port", None)
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, hostname, port


def _resolve_detail_prefetch(
    snapshot: dict[str, object],
    source_scope: str,
    cancel_event: threading.Event,
) -> dict[str, object]:
    settings = load_settings()
    prepared = _prepare_detail_prefetch_snapshot(snapshot, source_scope, settings)
    work = work_result_from_snapshot(prepared)
    if not _wait_for_detail_search_capacity(cancel_event):
        return work.to_dict()
    javdb_pool = None
    try:
        registry = default_indexers(settings)
        if any(source.source_id not in registry for source in work.sources):
            raise DetailPrefetchResolveError("parse_failed")
        bounds = SearchBounds(
            limit=20,
            page=1,
            max_pages=1,
            fetch_magnets=True,
            detail_limit=1,
            sort="relevance",
            match="exact",
            search_kind="code",
        ).normalized()
        missing_sources = tuple(
            source.source_id for source in work.sources
            if not source.detail_url and not isinstance(registry[source.source_id], TorznabIndexer)
        )
        if missing_sources:
            lookup_code = (
                work.canonical_code
                or work.code
                or next(
                    (source.raw_code for source in work.sources if source.raw_code),
                    None,
                )
            )
            if not lookup_code:
                raise DetailPrefetchResolveError("work_not_found")
            try:
                response = search(
                    lookup_code,
                    sources=missing_sources,
                    bounds=replace(bounds, fetch_magnets=False, detail_limit=0),
                    indexers=registry,
                    cancelled=cancel_event.is_set,
                )
            except (QueryError, ValueError):
                raise DetailPrefetchResolveError("work_not_found") from None
            if response.errors:
                raise _detail_prefetch_error_from_text(
                    " ".join(str(error) for error in response.errors.values())
                )
            requested_key = code_key(str(lookup_code))
            recovered = next(
                (
                    candidate
                    for candidate in response.results
                    if candidate.work_id == work.work_id
                    or code_key(candidate.canonical_code or candidate.code or "")
                    == requested_key
                ),
                None,
            )
            if recovered is None:
                raise DetailPrefetchResolveError("work_not_found")
            recovered_sources = {
                source.source_id: source for source in recovered.sources
            }
            work = replace(
                work,
                sources=tuple(
                    recovered_sources.get(source.source_id, source)
                    if not source.detail_url
                    else source
                    for source in work.sources
                ),
            )
            if any(not source.detail_url and not isinstance(registry[source.source_id], TorznabIndexer)
                   for source in work.sources):
                raise DetailPrefetchResolveError("work_not_found")
        if cancel_event.is_set():
            return work.to_dict()
        javdb_pool = JavDbFetcherPool(bounds)
        enriched = enrich_stream_work(
            work,
            registry,
            bounds,
            javdb_pool,
            include_images=True,
            cancelled=cancel_event.is_set,
        )
    finally:
        try:
            if javdb_pool is not None:
                javdb_pool.close()
        finally:
            release_search_capacity(state.DETAIL_SEARCH_SLOTS)
    if cancel_event.is_set():
        return enriched.to_dict()
    errors = [
        error
        for source in enriched.sources
        for error in (
            source.error,
            source.details_error,
            source.image_error,
            source.magnet_error,
        )
        if error
    ]
    if errors:
        raise _detail_prefetch_error_from_text(" ".join(errors))
    if any(source.parse_status != "resolved" for source in enriched.sources):
        raise DetailPrefetchResolveError("parse_failed")
    return enriched.to_dict()


def _wait_for_detail_search_capacity(cancel_event: threading.Event) -> bool:
    while not cancel_event.is_set():
        if acquire_search_capacity(state.DETAIL_SEARCH_SLOTS):
            return True
        cancel_event.wait(0.1)
    return False


def _detail_prefetch_error_from_text(value: object) -> DetailPrefetchResolveError:
    text = str(value or "").lower()
    if re.search(r"(?:timed?\s*out|timeout)", text):
        return DetailPrefetchResolveError("upstream_timeout", retryable=True)
    if re.search(r"(?:\b429\b|rate.?limit|too many requests)", text):
        return DetailPrefetchResolveError("upstream_rate_limited", retryable=True)
    if re.search(r"(?:\b5[0-9]{2}\b|server error|bad gateway)", text):
        return DetailPrefetchResolveError("upstream_server_error", retryable=True)
    return DetailPrefetchResolveError("parse_failed")
