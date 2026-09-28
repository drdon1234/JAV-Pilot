"""Streaming metadata search: result caching, continuations and replay."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterable

from ...config.settings import load_settings
from ...core.guards import normalize_query
from ...core.models import SearchBounds, SearchResponse
from .. import state
from ..base import BaseHandler
from ..registries import (
    SearchContinuationDecision,
    SearchContinuationEnvelope,
    copy_stream_snapshot,
)


def stream_search_cache_key(
    query: str,
    sources: tuple[str, ...],
    bounds: SearchBounds,
    fetch_magnets: bool,
    *,
    continuation_header: str = "",
) -> tuple[object, ...]:
    semantic_refs = getattr(bounds, "semantic_refs", {})
    return (
        "search-stream",
        query,
        sources,
        bounds.limit,
        bounds.page,
        bounds.max_pages,
        bounds.result_limit,
        bool(fetch_magnets),
        bounds.sort,
        bounds.match,
        getattr(bounds, "search_kind", "keyword"),
        tuple(sorted(semantic_refs.items())) if isinstance(semantic_refs, dict) else (),
        tuple(sorted(bounds.filters.items())),
        continuation_header or None,
    )


def stream_search_continuation_binding(
    query: str,
    sources: tuple[str, ...],
    bounds: SearchBounds,
    fetch_magnets: bool,
    *,
    settings: dict[str, object] | None = None,
) -> tuple[object, ...]:
    semantic_refs = getattr(bounds, "semantic_refs", {})
    active_settings = load_settings() if settings is None else settings
    return (
        "search-continuation",
        normalize_query(query),
        sources,
        bounds.limit,
        bounds.page,
        bounds.max_pages,
        bool(fetch_magnets),
        bounds.sort,
        bounds.match,
        getattr(bounds, "search_kind", "keyword"),
        tuple(sorted(semantic_refs.items())) if isinstance(semantic_refs, dict) else (),
        tuple(sorted(bounds.filters.items())),
        _stream_search_source_configuration(active_settings, sources),
    )


def _stream_search_source_configuration(
    settings: dict[str, object],
    sources: tuple[str, ...],
) -> tuple[tuple[str, str | None], ...]:
    raw_sites = settings.get("sites")
    sites = raw_sites if isinstance(raw_sites, list) else []
    configured = {
        str(site.get("id")): site
        for site in sites
        if isinstance(site, dict) and isinstance(site.get("id"), str)
    }
    return tuple(
        (
            source_id,
            (
                hashlib.sha256(
                    json.dumps(
                        configured[source_id],
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                if source_id in configured
                else None
            ),
        )
        for source_id in sources
    )


def inspect_stream_search_continuation(
    token: str,
    *,
    binding: tuple[object, ...],
    result_limit: int | None,
) -> SearchContinuationDecision:
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token):
        return SearchContinuationDecision("invalid")
    return state.SEARCH_CONTINUATIONS.inspect(
        token,
        binding=binding,
        result_limit=result_limit,
    )


def restore_stream_search_continuation(
    token: str,
    envelope: SearchContinuationEnvelope,
    *,
    lease_id: str,
) -> None:
    state.SEARCH_CONTINUATIONS.abort(
        token,
        lease_id=lease_id,
        envelope=envelope,
    )


def prepare_stream_search_continuation(
    binding: tuple[object, ...],
    response: SearchResponse,
) -> tuple[str, SearchContinuationEnvelope] | None:
    if response.continuation is None:
        return None
    for _attempt in range(8):
        token = secrets.token_urlsafe(24)
        if not state.SEARCH_CONTINUATIONS.contains(token):
            return token, SearchContinuationEnvelope(
                binding=binding,
                state=response.continuation,
                works=response.results,
            )
    raise RuntimeError("could not create search continuation")


def stream_search_continuation_mode(response: SearchResponse) -> str | None:
    continuation = response.continuation
    if continuation is None:
        return None
    found_count = (
        response.found_count
        if response.found_count is not None
        else len(response.results)
    )
    if response.errors and found_count < continuation.result_limit:
        return "retry"
    return "extend"


def cache_stream_snapshot(
    cache_key: tuple[object, ...], snapshot: dict[str, object]
) -> None:
    state.SEARCH_CACHE.set(cache_key, copy_stream_snapshot(snapshot))


def replay_stream_snapshot(
    handler: BaseHandler,
    snapshot: dict[str, object],
    request_id: str,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    def send_cancelled() -> None:
        payload = dict(snapshot.get("done") or {})
        payload.update({"request_id": request_id, "cached": True})
        handler._send_event("cancelled", payload)

    if cancelled is not None and cancelled():
        send_cancelled()
        return
    base = dict(snapshot.get("base") or {})
    base.update({"request_id": request_id, "cached": True})
    if not handler._send_event("base", base):
        return
    for stored in snapshot.get("results") or []:
        if cancelled is not None and cancelled():
            send_cancelled()
            return
        if not isinstance(stored, dict):
            continue
        payload = {**stored, "request_id": request_id, "cached": True}
        if not handler._send_event("result", payload):
            return
    if cancelled is not None and cancelled():
        send_cancelled()
        return
    done = dict(snapshot.get("done") or {})
    done.update({"request_id": request_id, "cached": True})
    terminal = snapshot.get("terminal")
    handler._send_event("cancelled" if terminal == "cancelled" else "done", done)


def work_cache_key(
    work_id: str, sources: Iterable[str]
) -> tuple[str, tuple[str, ...]]:
    return work_id, tuple(sources)
