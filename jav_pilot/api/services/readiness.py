"""Readiness probes for the service and its dependencies."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from ...config.app_config import AppConfig
from ...config.runtime_config import RuntimeConfigError, runtime_config_path
from ...config.settings import settings_path
from ...core.readiness import (
    CachedReadinessProbe,
    disk_space_ready,
    sqlite_database_ready,
    writable_directory,
)
from ...library.errors import MediaLibraryError
from ...library.worker import MediaLibraryConfig
from ...maintenance.lock import (
    MaintenanceLockError,
    validate_configured_maintenance_lock,
)
from ...media_metadata.manager import MediaMetadataConfig, MediaMetadataError
from ...search.detail_prefetch import DetailPrefetchError
from ...search.resources.errors import ResourceSearchError
from ...search.session_store import (
    MetadataSearchStoreError,
    metadata_search_schema_ready,
)
from ...sites.diagnostic_store import SiteDiagnosticStoreError
from ...torrent.qbittorrent import DownloaderError, QbittorrentClient
from ...web_download.config import WebDownloadConfig
from ...web_download.errors import WebDownloadError
from .. import state
from .environment import (
    app_revision,
    detail_prefetch_database_path,
    metadata_search_database_path,
    resource_search_database_path,
    site_diagnostic_database_path,
)
from .history import history_retention_scheduler_ready

DEFAULT_READINESS_MIN_FREE_BYTES = 64 * 1024**2
MAX_READINESS_MIN_FREE_BYTES = 1024**5
LOCAL_READINESS_CACHE_TTL_SECONDS = 5.0
QB_READINESS_CACHE_TTL_SECONDS = 15.0
QB_READINESS_TIMEOUT_SECONDS = 1.0
SQLITE_READINESS_TIMEOUT_SECONDS = 0.5


def readiness_status() -> dict[str, object]:
    if state.SERVER_STOPPING.is_set():
        return {
            "ok": False,
            "checks": {"server": False},
            "revision": app_revision(),
        }
    result = READINESS_PROBE.check()
    checks = dict(result.get("checks", {}))
    checks.update(QB_READINESS_PROBE.check().get("checks", {}))
    checks.update(_manager_readiness_checks())
    checks["server"] = True
    result["checks"] = checks
    result["ok"] = bool(checks) and all(checks.values())
    result["revision"] = app_revision()
    return result


def _readiness_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    try:
        validate_configured_maintenance_lock()
        checks["maintenance_lock"] = True
    except (MaintenanceLockError, OSError, ValueError):
        checks["maintenance_lock"] = False
    data_paths = {runtime_config_path().parent, settings_path().parent}
    media_paths: set[Path] = set()
    metadata_config: MediaMetadataConfig | None = None
    try:
        min_free_bytes = _readiness_min_free_bytes()
    except ValueError:
        checks["disk_space_config"] = False
        min_free_bytes = DEFAULT_READINESS_MIN_FREE_BYTES

    try:
        library_config = MediaLibraryConfig.from_env()
        if library_config.enabled:
            data_paths.add(library_config.database_path.parent)
            media_paths.add(library_config.library_path)
            checks["media_library_database"] = sqlite_database_ready(
                library_config.database_path,
                required_tables=(
                    "schema_migrations",
                    "media_library_roots",
                    "media_library_generations",
                    "media_library_entries",
                ),
                timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
            )
            checks["media_library_index_root"] = writable_directory(
                library_config.library_path
            )
    except (MediaLibraryError, OSError, ValueError):
        checks["media_library_config"] = False

    try:
        diagnostics_database = site_diagnostic_database_path()
        data_paths.add(diagnostics_database.parent)
        checks["site_diagnostics_database"] = sqlite_database_ready(
            diagnostics_database,
            required_tables=("schema_migrations", "site_diagnostic_status"),
            timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
        )
    except (OSError, SiteDiagnosticStoreError, ValueError):
        checks["site_diagnostics_config"] = False

    try:
        search_database = metadata_search_database_path()
        data_paths.add(search_database.parent)
        checks["metadata_search_database"] = sqlite_database_ready(
            search_database,
            required_tables=(
                "schema_migrations",
                "metadata_search_sessions",
                "metadata_search_events",
            ),
            timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
        ) and metadata_search_schema_ready(
            search_database,
            timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
        )
    except (OSError, MetadataSearchStoreError, ValueError):
        checks["metadata_search_config"] = False

    try:
        metadata_config = MediaMetadataConfig.from_env()
        if metadata_config.enabled:
            data_paths.add(metadata_config.database_path.parent)
            checks["metadata_database"] = sqlite_database_ready(
                metadata_config.database_path,
                required_tables=("jobs", "schema_migrations"),
                timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
            )
            checks["metadata_library"] = writable_directory(
                metadata_config.library_path
            )
            media_paths.add(metadata_config.library_path)
    except (MediaMetadataError, OSError, ValueError):
        checks["metadata_config"] = False

    try:
        resource_database = resource_search_database_path()
        if resource_database.is_absolute():
            data_paths.add(resource_database.parent)
        checks["resource_search_database"] = sqlite_database_ready(
            resource_database,
            required_tables=(
                "schema_migrations",
                "resource_search_sessions",
                "resource_search_items",
                "resource_search_variants",
                "resource_search_pending",
                "resource_search_members",
            ),
            timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
        )
    except (ResourceSearchError, OSError, ValueError):
        checks["resource_search_config"] = False

    try:
        detail_prefetch_database = detail_prefetch_database_path()
        data_paths.add(detail_prefetch_database.parent)
        checks["detail_prefetch_database"] = sqlite_database_ready(
            detail_prefetch_database,
            required_tables=(
                "schema_migrations",
                "detail_prefetch_batches",
                "detail_prefetch_items",
                "detail_prefetch_cache",
                "detail_prefetch_cache_usage",
            ),
            timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
        )
    except (DetailPrefetchError, OSError, ValueError):
        checks["detail_prefetch_config"] = False

    try:
        web_config = WebDownloadConfig.from_env()
        if web_config.enabled:
            data_paths.add(Path(web_config.database_path).parent)
            web_min_free_bytes = max(min_free_bytes, web_config.min_free_bytes)
            checks["web_database"] = sqlite_database_ready(
                Path(web_config.database_path),
                required_tables=(
                    "schema_migrations",
                    "web_download_jobs",
                    "web_download_batches",
                    "download_replacements",
                ),
                timeout_seconds=SQLITE_READINESS_TIMEOUT_SECONDS,
            )
            web_staging = Path(web_config.staging_path)
            web_library = Path(web_config.library_path)
            checks["web_staging"] = writable_directory(web_staging)
            checks["web_staging_free_space"] = disk_space_ready(
                web_staging,
                min_free_bytes=web_min_free_bytes,
            )
            checks["web_library"] = writable_directory(web_library)
            checks["web_library_free_space"] = disk_space_ready(
                web_library,
                min_free_bytes=web_min_free_bytes,
            )
            media_paths.add(web_library)
    except (WebDownloadError, OSError, ValueError):
        checks["web_download_config"] = False

    try:
        qb_config = AppConfig.from_env().qbittorrent
        if qb_config.configured:
            qb_staging_text = qb_config.save_path.strip()
            checks["qb_destination_config"] = bool(
                qb_config.category.strip() and qb_staging_text
            )
            qb_staging = Path(qb_staging_text)
            checks["qb_staging"] = bool(qb_staging_text) and writable_directory(
                qb_staging
            )
            checks["qb_staging_free_space"] = bool(
                qb_staging_text
            ) and disk_space_ready(
                qb_staging,
                min_free_bytes=min_free_bytes,
            )
            if qb_config.library_path:
                app_library_text = qb_config.app_library_path.strip()
                app_library = Path(app_library_text)
                checks["qb_library_mapping"] = bool(app_library_text) and (
                    _mapped_library_ready(
                        app_library,
                        metadata_config.library_path,
                    )
                    if metadata_config is not None and metadata_config.enabled
                    else writable_directory(app_library)
                )
                if app_library_text:
                    media_paths.add(app_library)
    except (OSError, RuntimeConfigError, ValueError):
        checks["qb_config"] = False

    checks["data"] = _paths_ready(data_paths)
    checks["data_free_space"] = _paths_have_space(
        data_paths,
        min_free_bytes=min_free_bytes,
    )
    if media_paths:
        checks["media_library"] = _paths_ready(media_paths)
        checks["media_library_free_space"] = _paths_have_space(
            media_paths,
            min_free_bytes=min_free_bytes,
        )
    return checks


def _qbittorrent_readiness_checks() -> dict[str, bool]:
    try:
        config = AppConfig.from_env().qbittorrent
    except (OSError, RuntimeConfigError, ValueError):
        return {"qbittorrent_config": False}
    if not config.configured:
        return {}
    try:
        status = QbittorrentClient(config).status(
            timeout_seconds=QB_READINESS_TIMEOUT_SECONDS
        )
    except (DownloaderError, OSError, ValueError):
        return {"qbittorrent": False}
    return {"qbittorrent": status.get("ok") is True}


def _manager_readiness_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    checks["resource_search_manager"] = bool(
        state.RESOURCE_SEARCHES and state.RESOURCE_SEARCHES.is_alive()
    )
    checks["detail_prefetch_manager"] = bool(
        state.DETAIL_PREFETCH and state.DETAIL_PREFETCH.is_alive()
    )
    checks["site_diagnostic_scheduler"] = bool(
        state.SITE_DIAGNOSTIC_SCHEDULER and state.SITE_DIAGNOSTIC_SCHEDULER.is_alive
    )
    checks["history_retention_scheduler"] = history_retention_scheduler_ready()
    try:
        if MediaLibraryConfig.from_env().enabled:
            checks["media_library_manager"] = bool(
                state.MEDIA_LIBRARY and state.MEDIA_LIBRARY.is_alive
            )
    except (MediaLibraryError, OSError, ValueError):
        checks["media_library_manager"] = False
    try:
        if MediaMetadataConfig.from_env().enabled:
            checks["metadata_manager"] = _thread_ready(state.MEDIA_METADATA, "_worker")
    except (MediaMetadataError, OSError, ValueError):
        checks["metadata_manager"] = False
    try:
        if WebDownloadConfig.from_env().enabled:
            checks["web_download_manager"] = _workers_ready(state.WEB_DOWNLOADS)
            checks["web_batch_manager"] = _thread_ready(state.WEB_DOWNLOAD_BATCHES, "_worker")
    except (WebDownloadError, OSError, ValueError):
        checks["web_download_manager"] = False
        checks["web_batch_manager"] = False
    try:
        qb_config = AppConfig.from_env().qbittorrent
        if qb_config.configured and qb_config.library_path:
            checks["organizer_worker"] = _worker_ready(state.COMPLETED_ORGANIZER_WORKER)
    except (OSError, RuntimeConfigError, ValueError):
        checks["organizer_worker"] = False
    checks["notification_dispatcher"] = _worker_ready(state.NOTIFICATION_DISPATCHER_WORKER)
    return checks


def _shared_data_paths_ready() -> bool:
    return _paths_ready({runtime_config_path().parent, settings_path().parent})


def _paths_ready(paths: Iterable[Path]) -> bool:
    unique_paths = set(paths)
    return bool(unique_paths) and all(writable_directory(path) for path in unique_paths)


def _paths_have_space(
    paths: Iterable[Path],
    *,
    min_free_bytes: int,
) -> bool:
    unique_paths = set(paths)
    return bool(unique_paths) and all(
        disk_space_ready(path, min_free_bytes=min_free_bytes) for path in unique_paths
    )


def _mapped_library_ready(path: Path, root: Path) -> bool:
    if not writable_directory(path):
        return False
    try:
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except OSError:
        return False


def _thread_ready(manager: object | None, attribute: str) -> bool:
    if manager is None:
        return False
    worker = getattr(manager, attribute, None)
    return _worker_ready(worker)


def _worker_ready(worker: object | None) -> bool:
    is_alive = getattr(worker, "is_alive", None)
    return callable(is_alive) and bool(is_alive())


def _workers_ready(manager: object | None) -> bool:
    if manager is None or not bool(getattr(manager, "_workers_started", False)):
        return False
    workers = tuple(getattr(manager, "_workers", ()))
    return bool(workers) and all(
        callable(getattr(worker, "is_alive", None)) and worker.is_alive()
        for worker in workers
    )


def _readiness_min_free_bytes() -> int:
    raw = os.environ.get("JAV_PILOT_READINESS_MIN_FREE_BYTES", "").strip()
    if not raw:
        return DEFAULT_READINESS_MIN_FREE_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("readiness minimum free bytes must be an integer") from exc
    if not 0 <= value <= MAX_READINESS_MIN_FREE_BYTES:
        raise ValueError("readiness minimum free bytes is out of range")
    return value


def invalidate_readiness_probes() -> None:
    READINESS_PROBE.invalidate()
    QB_READINESS_PROBE.invalidate()


READINESS_PROBE = CachedReadinessProbe(
    _readiness_checks,
    ttl_seconds=LOCAL_READINESS_CACHE_TTL_SECONDS,
)
QB_READINESS_PROBE = CachedReadinessProbe(
    _qbittorrent_readiness_checks,
    ttl_seconds=QB_READINESS_CACHE_TTL_SECONDS,
)
