"""Web resource search manager lifecycle and source selection."""

from __future__ import annotations

import sqlite3

from ...config.settings import (
    BUILTIN_WEB_RESOURCE_SITE_IDS,
    RESOURCE_SEARCH_CAPABILITY,
    SettingsError,
    load_settings,
    site_by_id,
    site_has_capability,
)
from ...core.observability import emit_json_log
from ...search.resources.errors import (
    ResourceSearchError,
    ResourceSearchUnavailableError,
)
from ...search.resources.manager import ResourceSearchManager
from ...search.resources.searchers import RoutedResourceSearcher
from ...web_download.providers import WebProviderResourceSearcher
from .. import state
from .environment import resource_search_database_path
from .history import require_operational_mode


def resource_search_manager() -> ResourceSearchManager:
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            ResourceSearchUnavailableError(
                "resource search is unavailable in maintenance mode"
            )
        )
        with state.RESOURCE_SEARCHES_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise ResourceSearchUnavailableError("server is shutting down")
            if state.RESOURCE_SEARCHES is not None:
                return state.RESOURCE_SEARCHES
            try:
                state.RESOURCE_SEARCHES = ResourceSearchManager(
                    resource_search_database_path(),
                    discoverer=RoutedResourceSearcher(
                        {
                            "jable": WebProviderResourceSearcher("jable"),
                            "supjav": WebProviderResourceSearcher("supjav"),
                            "missav": WebProviderResourceSearcher("missav"),
                            "kissjav": WebProviderResourceSearcher("kissjav"),
                            "javnoni": WebProviderResourceSearcher("javnoni"),
                        }
                    ),
                )
            except (
                OSError,
                sqlite3.Error,
                ResourceSearchError,
                TypeError,
                ValueError,
            ) as exc:
                raise ResourceSearchUnavailableError(
                    "resource search storage is unavailable"
                ) from exc
            return state.RESOURCE_SEARCHES


def shutdown_resource_search_manager(*, timeout: float) -> bool:
    with state.RESOURCE_SEARCHES_LOCK:
        manager = state.RESOURCE_SEARCHES
    if manager is None:
        return True
    try:
        stopped = manager.shutdown(timeout=timeout)
    except Exception:
        stopped = False
    if stopped:
        with state.RESOURCE_SEARCHES_LOCK:
            if state.RESOURCE_SEARCHES is manager:
                state.RESOURCE_SEARCHES = None
    else:
        emit_json_log(
            "resource_search",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    return bool(stopped)


def require_resource_search_source(source_id: object) -> str:
    if source_id == "all":
        enabled_resource_search_sources()
        return "all"
    if not isinstance(source_id, str) or source_id not in BUILTIN_WEB_RESOURCE_SITE_IDS:
        raise ValueError("resource search source is invalid")
    try:
        settings = load_settings()
        if settings.get("_config_error"):
            raise ResourceSearchUnavailableError(
                "resource search site configuration is invalid"
            )
        site = site_by_id(source_id, settings)
        if (
            site is None
            or not site.get("enabled")
            or site.get("parser_profile") != source_id
            or not str(site.get("base_url") or "").strip()
            or not site_has_capability(site, RESOURCE_SEARCH_CAPABILITY)
        ):
            raise ResourceSearchUnavailableError(
                "resource search source is unavailable"
            )
    except SettingsError as exc:
        raise ResourceSearchUnavailableError(
            "resource search site configuration is invalid"
        ) from exc
    return source_id


def enabled_resource_search_sources() -> tuple[str, ...]:
    from .runtime_settings import resource_search_sites

    try:
        settings = load_settings()
        if settings.get("_config_error"):
            raise ResourceSearchUnavailableError(
                "resource search site configuration is invalid"
            )
        sources = tuple(
            str(site["id"])
            for site in resource_search_sites(settings)
            if site.get("id") in BUILTIN_WEB_RESOURCE_SITE_IDS
            and site.get("parser_profile") == site.get("id")
            and str(site.get("base_url") or "").strip()
        )
        if not sources:
            raise ResourceSearchUnavailableError("resource search source is unavailable")
        return sources
    except SettingsError as exc:
        raise ResourceSearchUnavailableError(
            "resource search site configuration is invalid"
        ) from exc
