"""Web download and batch manager lifecycle."""

from __future__ import annotations

import os
import re
import sqlite3
import time

from ...config.settings import (
    BUILTIN_WEB_DOWNLOAD_SITE_IDS,
    WEB_DOWNLOAD_CAPABILITY,
    SettingsError,
    load_settings,
    site_by_id,
    site_has_capability,
)
from ...core.observability import emit_json_log
from ...media_metadata.manager import MediaMetadataError
from ...media_metadata.store import MediaMetadataStoreError
from ...missav.browser_runtime import get_missav_browser_runtime
from ...web_download.batches.discovery import BrokerMissavSeriesDiscoverer
from ...web_download.batches.errors import WebDownloadBatchError
from ...web_download.batches.manager import WebDownloadBatchManager
from ...web_download.batches.models import (
    ABSOLUTE_MAX_BATCH_PAGES,
    DEFAULT_BATCH_PAGE_BUDGET,
)
from ...web_download.config import WebDownloadConfig
from ...web_download.errors import (
    WebDownloadConfigError,
    WebDownloadDisabledError,
    WebDownloadError,
)
from ...web_download.manager import WebDownloadManager
from ...web_download.providers import PriorityWebDownloadProvider
from .. import state
from .history import require_operational_mode
from .media import media_metadata_manager, observe_web_archive
from .notifications import ensure_notification_outbox_registered


def web_download_manager(
    config: WebDownloadConfig | None = None,
    *,
    _register_notifications: bool = True,
) -> WebDownloadManager:
    manager: WebDownloadManager | None = None
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            WebDownloadConfigError("Web downloads are unavailable in maintenance mode")
        )
        with state.WEB_DOWNLOADS_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise WebDownloadError("server is shutting down")
            if state.WEB_DOWNLOADS is not None:
                manager = state.WEB_DOWNLOADS

    if manager is None:
        active_config = config or WebDownloadConfig.from_env()
        if not active_config.enabled:
            raise WebDownloadDisabledError("Web downloads are disabled")
        metadata_manager = None
        try:
            metadata_manager = media_metadata_manager(
                _register_notifications=False,
            )
        except (
            MediaMetadataError,
            MediaMetadataStoreError,
            OSError,
            sqlite3.Error,
        ):
            pass
        with state.OPERATIONAL_MANAGER_LOCK:
            require_operational_mode(
                WebDownloadConfigError(
                    "Web downloads are unavailable in maintenance mode"
                )
            )
            with state.WEB_DOWNLOADS_LOCK:
                if state.SERVER_STOPPING.is_set():
                    raise WebDownloadError("server is shutting down")
                if state.WEB_DOWNLOADS is not None:
                    manager = state.WEB_DOWNLOADS
                else:
                    if state.WEB_DOWNLOADS_PENDING_SHUTDOWN:
                        state.WEB_DOWNLOADS_PENDING_SHUTDOWN = [
                            pending
                            for pending in state.WEB_DOWNLOADS_PENDING_SHUTDOWN
                            if not _stop_web_download_manager(pending, timeout=0.0)
                        ]
                        if state.WEB_DOWNLOADS_PENDING_SHUTDOWN:
                            raise WebDownloadConfigError(
                                "Web download worker cleanup is still pending"
                            )
                    try:
                        manager = WebDownloadManager(
                            active_config,
                            manifest_provider=PriorityWebDownloadProvider(
                                get_missav_browser_runtime()
                            ),
                            on_completed=lambda job: observe_web_archive(
                                job, metadata_manager
                            ),
                            start_workers=False,
                        )
                        if metadata_manager is not None:
                            metadata_manager.reconcile_web(
                                manager.completed_metadata_candidates()
                            )
                        manager.start_workers()
                    except Exception as exc:
                        if manager is not None and not _stop_web_download_manager(
                            manager,
                            timeout=15.0,
                        ):
                            emit_json_log(
                                "web_downloads",
                                "cleanup_deadline_exceeded",
                                level="warning",
                                outcome="pending",
                            )
                            state.WEB_DOWNLOADS_PENDING_SHUTDOWN.append(manager)
                        raise WebDownloadConfigError(
                            "Web download storage is unavailable"
                        ) from exc
                    state.WEB_DOWNLOADS = manager
    if manager is None:
        raise WebDownloadConfigError("Web download storage is unavailable")
    if _register_notifications:
        ensure_notification_outbox_registered(
            manager.store.path,
            component="web_downloads",
        )
    return manager


def shutdown_web_download_manager(*, timeout: float) -> bool:
    with state.WEB_DOWNLOADS_LOCK:
        manager = state.WEB_DOWNLOADS
        state.WEB_DOWNLOADS = None
        managers = ([manager] if manager is not None else []) + list(
            state.WEB_DOWNLOADS_PENDING_SHUTDOWN
        )
        state.WEB_DOWNLOADS_PENDING_SHUTDOWN = list(dict.fromkeys(managers))
    # Signal every generation before spending the shared budget waiting on one.
    deadline = time.monotonic() + max(0.0, timeout)
    pending = [
        candidate for candidate in dict.fromkeys(managers)
        if not _stop_web_download_manager(candidate, timeout=0.0)
    ]
    pending = [
        candidate for candidate in pending
        if not _stop_web_download_manager(
            candidate, timeout=max(0.0, deadline - time.monotonic())
        )
    ]
    if pending:
        emit_json_log(
            "web_downloads",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    with state.WEB_DOWNLOADS_LOCK:
        state.WEB_DOWNLOADS_PENDING_SHUTDOWN = pending
    return not pending


def shutdown_download_resource_recovery(*, timeout: float) -> bool:
    with state.DOWNLOAD_RESOURCE_RECOVERY_LOCK:
        manager = state.DOWNLOAD_RESOURCE_RECOVERY
    if manager is None:
        return True
    stopped = manager.shutdown(timeout=timeout)
    if stopped:
        with state.DOWNLOAD_RESOURCE_RECOVERY_LOCK:
            if state.DOWNLOAD_RESOURCE_RECOVERY is manager:
                state.DOWNLOAD_RESOURCE_RECOVERY = None
    else:
        emit_json_log(
            "download_resource_recovery",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    return bool(stopped)


def web_download_batch_manager(
    config: WebDownloadConfig | None = None,
) -> WebDownloadBatchManager:
    download_manager = web_download_manager(
        config,
        _register_notifications=False,
    )
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            WebDownloadConfigError(
                "Web download batches are unavailable in maintenance mode"
            )
        )
        with state.WEB_DOWNLOAD_BATCHES_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise WebDownloadBatchError("server is shutting down")
            if state.WEB_DOWNLOAD_BATCHES is not None:
                manager = state.WEB_DOWNLOAD_BATCHES
            else:
                try:
                    state.WEB_DOWNLOAD_BATCHES = WebDownloadBatchManager(
                        download_manager,
                        discover=BrokerMissavSeriesDiscoverer(
                            get_missav_browser_runtime()
                        ),
                        page_budget=_web_download_batch_page_budget(),
                    )
                except (OSError, sqlite3.Error, WebDownloadError) as exc:
                    raise WebDownloadConfigError(
                        "Web download batch storage is unavailable"
                    ) from exc
                manager = state.WEB_DOWNLOAD_BATCHES
    ensure_notification_outbox_registered(
        download_manager.store.path,
        component="web_downloads",
    )
    return manager


def shutdown_web_download_batch_manager(*, timeout: float) -> bool:
    with state.WEB_DOWNLOAD_BATCHES_LOCK:
        manager = state.WEB_DOWNLOAD_BATCHES
    if manager is None:
        return True
    try:
        stopped = manager.shutdown(timeout=timeout)
    except Exception:
        stopped = False
    if stopped:
        with state.WEB_DOWNLOAD_BATCHES_LOCK:
            if state.WEB_DOWNLOAD_BATCHES is manager:
                state.WEB_DOWNLOAD_BATCHES = None
    else:
        emit_json_log(
            "web_download_batches",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    return bool(stopped)


def _stop_web_download_manager(
    manager: WebDownloadManager,
    *,
    timeout: float,
) -> bool:
    try:
        return bool(manager.shutdown(timeout=timeout))
    except Exception:
        return False


def _web_download_batch_page_budget() -> int:
    raw = os.environ.get(
        "JAV_PILOT_WEB_DOWNLOAD_BATCH_PAGE_BUDGET",
        str(DEFAULT_BATCH_PAGE_BUDGET),
    ).strip()
    if re.fullmatch(r"[0-9]+", raw) is None:
        raise WebDownloadConfigError("Web download batch page budget is invalid")
    value = int(raw)
    if not 1 <= value <= ABSOLUTE_MAX_BATCH_PAGES:
        raise WebDownloadConfigError("Web download batch page budget is invalid")
    return value


def web_download_public_status(
    settings: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        config = WebDownloadConfig.from_env()
    except WebDownloadError as exc:
        return {
            "configured": False,
            "enabled": False,
            "available": False,
            "reason": str(exc),
        }
    status = config.public_dict()
    if not config.enabled:
        status["available"] = False
        status["reason"] = "Web downloads are disabled"
        return status
    available, reason, providers = web_download_site_availability(settings)
    status["enabled"] = available
    status["available"] = available
    status["reason"] = reason
    status["providers"] = providers
    return status


def web_download_site_availability(
    settings: dict[str, object] | None = None,
) -> tuple[bool, str, list[dict[str, object]]]:
    settings = load_settings() if settings is None else settings
    if settings.get("_config_error"):
        return False, "Web download site configuration is invalid", []
    raw_priority = settings.get("web_download_provider_priority", [])
    priority = [str(item) for item in raw_priority] if isinstance(raw_priority, list) else []
    if not priority:
        priority = list(BUILTIN_WEB_DOWNLOAD_SITE_IDS)
    providers: list[dict[str, object]] = []
    for provider_id in priority:
        if provider_id not in BUILTIN_WEB_DOWNLOAD_SITE_IDS:
            continue
        site = site_by_id(provider_id, settings)
        try:
            available = bool(
                site is not None
                and site.get("enabled")
                and site.get("parser_profile") == provider_id
                and site_has_capability(site, WEB_DOWNLOAD_CAPABILITY)
            )
        except SettingsError:
            available = False
        providers.append(
            {
                "id": provider_id,
                "name": str(site.get("name") if site else provider_id),
                "available": available,
            }
        )
    available = any(bool(item["available"]) for item in providers)
    return available, "" if available else "No Web download site is enabled", providers


def require_web_download_site_available() -> None:
    available, reason, _providers = web_download_site_availability()
    if not available:
        raise WebDownloadDisabledError(reason)
