"""HTTP server bootstrap, background service startup and graceful shutdown."""

from __future__ import annotations

import signal
import sqlite3
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config.runtime_config import RuntimeConfigError, runtime_config_path
from ..config.settings import load_settings
from ..core.observability import configure_structured_logging, emit_json_log
from ..core.service_lifecycle import stop_services
from ..downloads.replacements import DownloadReplacementError
from ..history.errors import HistoryLifecycleError
from ..library.errors import MediaLibraryError
from ..library.worker import MediaLibraryConfig
from ..media_metadata.manager import MediaMetadataConfig, MediaMetadataError
from ..media_metadata.review.errors import MediaMetadataReviewError
from ..media_metadata.store import MediaMetadataStoreError
from ..missav.browser_runtime import shutdown_missav_browser_runtime
from ..notifications.errors import NotificationError
from ..search.detail_prefetch import DetailPrefetchError
from ..search.resources.errors import ResourceSearchError
from ..search.session_store import MetadataSearchStoreError
from ..security.auth import AuthConfig
from ..security.posture import enforce_startup_security
from ..sites.diagnostic_codes import load_site_diagnostic_codes
from ..sites.diagnostic_store import SiteDiagnosticStoreError
from ..sites.diagnostic_worker import SiteDiagnosticScheduler
from ..sites.diagnostics import SiteDiagnosticService
from ..sites.probe_adapters import build_site_probe_adapters
from ..torrent.completed import run_completed_download_organizer
from ..web_download.config import WebDownloadConfig
from ..web_download.errors import WebDownloadError
from . import state
from .handler import JavPilotHandler
from .services.detail_prefetch import (
    detail_prefetch_manager,
    shutdown_detail_prefetch_manager,
)
from .services.environment import app_revision
from .services.history import (
    history_lifecycle,
    history_maintenance_mode,
    recover_history_before_workers,
    reset_history_lifecycle,
    start_history_retention_scheduler,
    stop_history_retention_scheduler,
)
from .services.media import (
    media_library_manager,
    media_metadata_manager,
    media_metadata_review_manager,
    shutdown_media_library_manager,
    shutdown_media_metadata_manager,
    synchronize_qb_media_library,
)
from .services.metadata_search import metadata_search_store
from .services.notifications import (
    ensure_notification_worker,
    refresh_notification_runtime,
    stop_notification_worker,
)
from .services.readiness import invalidate_readiness_probes
from .services.replacements import (
    download_replacement_store,
    download_resource_recovery_manager,
)
from .services.resource_search import (
    resource_search_manager,
    shutdown_resource_search_manager,
)
from .services.site_diagnostics import (
    site_diagnostic_delay,
    site_diagnostic_store,
    stop_site_diagnostic_scheduler,
)
from .services.web_downloads import (
    shutdown_download_resource_recovery,
    shutdown_web_download_batch_manager,
    shutdown_web_download_manager,
    web_download_batch_manager,
    web_download_manager,
)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        *,
        max_active_requests: int = 64,
    ) -> None:
        if not 8 <= max_active_requests <= 256:
            raise ValueError("HTTP request concurrency bound is invalid")
        self._request_slots = threading.BoundedSemaphore(max_active_requests)
        super().__init__(server_address, request_handler)

    def process_request(self, request: object, client_address: object) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(  # type: ignore[attr-defined]
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"Retry-After: 1\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)  # type: ignore[arg-type]
            return
        try:
            super().process_request(request, client_address)  # type: ignore[arg-type]
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(  # type: ignore[arg-type]
                request,
                client_address,
            )
        finally:
            self._request_slots.release()


def run_server(host: str, port: int) -> None:
    configure_structured_logging()
    state.SERVER_STOPPING.clear()
    state.LOGIN_RATE_LIMITER.reset()
    invalidate_readiness_probes()
    auth_config = AuthConfig.from_env()
    posture = enforce_startup_security(
        host=host,
        auth_enabled=auth_config.enabled,
        auth_configured=auth_config.configured,
        secret_persistent=auth_config.secret_persistent,
        plain_password=auth_config.password,
        config_path=runtime_config_path(),
        listen_port=port,
    )
    if posture.override_active and not posture.safe:
        emit_json_log(
            "security",
            "insecure_remote_override_active",
            level="warning",
            count=len(posture.findings),
        )
    maintenance_mode = history_maintenance_mode()
    if not maintenance_mode:
        recover_history_before_workers()
    server = BoundedThreadingHTTPServer((host, port), JavPilotHandler)
    if not maintenance_mode:
        try:
            replacement_store = download_replacement_store()
            replacement_store.recover_interrupted_dispositions()
            replacement_store.recover_interrupted_smart_selections()
        except Exception:
            server.server_close()
            raise
    shutdown_requested = threading.Event()
    previous_signal_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, frame: object) -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        threading.Thread(
            target=server.shutdown,
            name="jav-server-shutdown",
            daemon=True,
        ).start()

    for signal_value in (
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGTERM", None),
    ):
        if signal_value is None:
            continue
        try:
            previous_signal_handlers[int(signal_value)] = signal.getsignal(signal_value)
            signal.signal(signal_value, request_shutdown)
        except ValueError:
            for signal_number, previous_handler in previous_signal_handlers.items():
                try:
                    signal.signal(signal_number, previous_handler)
                except ValueError:
                    pass
            previous_signal_handlers.clear()
            break
    organizer_stop = threading.Event()
    organizer_worker = (
        None if maintenance_mode else _start_background_services(organizer_stop)
    )
    if maintenance_mode:
        try:
            history_lifecycle()
        except (
            HistoryLifecycleError,
            MediaLibraryError,
            MediaMetadataError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
        ):
            emit_json_log(
                "history",
                "maintenance_startup_unavailable",
                level="error",
                error_code="storage_unavailable",
            )
    invalidate_readiness_probes()
    emit_json_log(
        "server",
        "started",
        revision=app_revision(),
        outcome="ready",
    )
    try:
        server.serve_forever()
    finally:
        state.SERVER_STOPPING.set()
        invalidate_readiness_probes()
        organizer_stop.set()
        services: dict[str, Callable[..., object]] = {
            "site_diagnostics": stop_site_diagnostic_scheduler,
            "history_retention": stop_history_retention_scheduler,
            "notifications": stop_notification_worker,
            "detail_prefetch": shutdown_detail_prefetch_manager,
            "resource_search": shutdown_resource_search_manager,
            "download_resource_recovery": shutdown_download_resource_recovery,
            "web_download_batches": shutdown_web_download_batch_manager,
            "web_downloads": shutdown_web_download_manager,
            "missav_browser": shutdown_missav_browser_runtime,
            "media_library": shutdown_media_library_manager,
            "media_metadata": shutdown_media_metadata_manager,
            "magnet_selections": state.MAGNET_SELECTIONS.shutdown,
            "magnet_probes": state.MAGNET_PROBES.shutdown,
        }
        if organizer_worker is not None:

            def stop_organizer(*, timeout: float) -> bool:
                organizer_worker.join(timeout=timeout)
                return not organizer_worker.is_alive()

            services["completed_organizer"] = stop_organizer
        try:
            if stop_services(services):
                reset_history_lifecycle()
            if (
                organizer_worker is not None
                and not organizer_worker.is_alive()
                and state.COMPLETED_ORGANIZER_WORKER is organizer_worker
            ):
                state.COMPLETED_ORGANIZER_WORKER = None
        finally:
            server.server_close()
            for signal_number, previous_handler in previous_signal_handlers.items():
                try:
                    signal.signal(signal_number, previous_handler)
                except ValueError:
                    pass


def _start_background_services(organizer_stop: threading.Event) -> threading.Thread:
    organizer_worker = threading.Thread(
        target=run_completed_download_organizer,
        args=(organizer_stop, synchronize_qb_media_library),
        name="jav-completed-organizer",
        daemon=True,
    )
    state.MAGNET_PROBES.reap_stale(max_age_seconds=300)
    state.MAGNET_SELECTIONS.reap_stale(max_age_seconds=3_600)
    try:
        diagnostic_store = site_diagnostic_store()

        state.SITE_DIAGNOSTIC_SCHEDULER = SiteDiagnosticScheduler(
            lambda: SiteDiagnosticService(
                diagnostic_store,
                build_site_probe_adapters(
                    load_settings(), load_site_diagnostic_codes()
                ),
            ),
            base_delay_seconds=site_diagnostic_delay(
                "JAV_PILOT_SITE_DIAGNOSTIC_INTERVAL_SECONDS", 900.0
            ),
            max_delay_seconds=site_diagnostic_delay(
                "JAV_PILOT_SITE_DIAGNOSTIC_MAX_INTERVAL_SECONDS", 21_600.0
            ),
        )
        state.SITE_DIAGNOSTIC_SCHEDULER.start()
    except (OSError, sqlite3.Error, SiteDiagnosticStoreError):
        emit_json_log(
            "site_diagnostics",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    except (ValueError, WebDownloadError):
        emit_json_log(
            "site_diagnostics",
            "startup_unavailable",
            level="error",
            error_code="configuration_invalid",
        )
    try:
        metadata_search_store()
    except (MetadataSearchStoreError, OSError, sqlite3.Error):
        emit_json_log(
            "metadata_search",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    try:
        library_config = MediaLibraryConfig.from_env()
        if library_config.enabled:
            media_library_manager(library_config)
    except (MediaLibraryError, OSError, sqlite3.Error):
        emit_json_log(
            "media_library",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    try:
        metadata_config = MediaMetadataConfig.from_env()
        if metadata_config.enabled:
            media_metadata_manager(metadata_config)
            media_metadata_review_manager()
    except (
        MediaMetadataError,
        MediaMetadataStoreError,
        MediaMetadataReviewError,
        OSError,
        sqlite3.Error,
    ):
        emit_json_log(
            "media_metadata",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    try:
        resource_search_manager()
    except (ResourceSearchError, OSError, sqlite3.Error):
        emit_json_log(
            "resource_search",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    try:
        detail_prefetch_manager()
    except (DetailPrefetchError, OSError, sqlite3.Error):
        emit_json_log(
            "detail_prefetch",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    try:
        web_download_config = WebDownloadConfig.from_env()
        download_replacement_store(web_download_config)
        download_resource_recovery_manager(web_download_config)
        if web_download_config.enabled:
            web_download_manager(web_download_config)
            web_download_batch_manager(web_download_config)
    except (DownloadReplacementError, WebDownloadError, OSError, sqlite3.Error):
        emit_json_log(
            "web_downloads",
            "startup_unavailable",
            level="error",
            error_code="storage_unavailable",
        )
    try:
        refresh_notification_runtime()
    except (
        NotificationError,
        RuntimeConfigError,
        OSError,
        sqlite3.Error,
        RuntimeError,
    ):
        emit_json_log(
            "notifications",
            "startup_unavailable",
            level="error",
            error_code="configuration_invalid",
        )
    try:
        ensure_notification_worker()
    except (NotificationError, RuntimeError):
        emit_json_log(
            "notifications",
            "worker_startup_unavailable",
            level="error",
            error_code="worker_unavailable",
        )
    start_history_retention_scheduler()
    organizer_worker.start()
    state.COMPLETED_ORGANIZER_WORKER = organizer_worker
    return organizer_worker
