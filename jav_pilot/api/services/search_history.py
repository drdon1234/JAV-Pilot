"""Search history store accessors and recording."""

from __future__ import annotations

import sqlite3

from ...config.settings import SettingsError, load_settings, search_sites
from ...core.observability import emit_json_log
from ...search.history import SearchHistoryError, SearchHistoryStore
from .. import state


def search_history_store() -> SearchHistoryStore:
    with state.LAZY_SERVICES_LOCK:
        if state.SEARCH_HISTORY_STORE is None:
            state.SEARCH_HISTORY_STORE = SearchHistoryStore()
        return state.SEARCH_HISTORY_STORE


def search_history_limit() -> int:
    defaults = load_settings().get("workflow_defaults") or {}
    value = defaults.get("search_history_limit") if isinstance(defaults, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 100


def record_search_history(kind: str, query: str, params: dict[str, object]) -> None:
    """History is a convenience; recording must never fail the search itself."""

    try:
        search_history_store().record(kind, query, params, limit=search_history_limit())
    except (SearchHistoryError, OSError, sqlite3.Error, SettingsError):
        emit_json_log("search_history", "record_failed", level="warning", error_code="storage")


def record_metadata_search_history(request: object) -> None:
    if not isinstance(request, dict):
        return
    sources = [str(item) for item in request.get("sources") or []]
    try:
        all_sources = {str(site.get("id")) for site in search_sites(load_settings())}
    except SettingsError:
        all_sources = set()
    params = {
        "query": request.get("query"),
        "sources": sorted(sources),
        "site_mode": "all" if all_sources and set(sources) == all_sources else "custom",
        "result_limit": request.get("result_limit"),
        "fetch_magnets": request.get("fetch_magnets"),
        "filters": request.get("filters") or {},
        "sort": request.get("sort"),
        "match": request.get("match"),
        "search_kind": request.get("search_kind"),
        "semantic_refs": request.get("semantic_refs") or {},
    }
    record_search_history("metadata", str(request.get("query") or ""), params)
