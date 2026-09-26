"""Prometheus metric samples for the running service."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from ...config.runtime_config import runtime_config_path
from ...core.observability import RUNTIME_METRICS, MetricSample, render_prometheus
from ...library.errors import MediaLibraryError
from ...library.worker import MediaLibraryConfig
from ...media_metadata.manager import MediaMetadataConfig, MediaMetadataError
from ...missav.browser_gate import MISSAV_BROWSER_GATE
from ...missav.browser_runtime import get_missav_browser_runtime
from ...search.session_store import MetadataSearchStoreError
from ...sites.diagnostic_store import SiteDiagnosticStoreError
from ...web_download.config import WebDownloadConfig
from ...web_download.errors import WebDownloadError
from .. import state
from .environment import (
    metadata_search_database_path,
    site_diagnostic_database_path,
)


def render_observability_metrics() -> str:
    cached = state.METRICS_TEXT_CACHE.get("prometheus")
    if cached is not None:
        return cached
    with state.METRICS_RENDER_LOCK:
        cached = state.METRICS_TEXT_CACHE.get("prometheus")
        if cached is not None:
            return cached
        rendered = render_prometheus(_observability_samples())
        state.METRICS_TEXT_CACHE.set("prometheus", rendered)
        return rendered


def _observability_samples() -> list[MetricSample]:
    samples = [
        MetricSample(
            "jav_pilot_up",
            0 if state.SERVER_STOPPING.is_set() else 1,
            help_text="Whether the JAV Pilot server is accepting work.",
        )
    ]
    manager = state.WEB_DOWNLOADS
    if manager is not None:
        try:
            summary = manager.store.summary()
        except (OSError, sqlite3.Error, WebDownloadError):
            summary = {}
        workers = tuple(getattr(manager, "_workers", ()))
        active = len(getattr(manager, "_active_cancels", {}))
        samples.extend(
            (
                MetricSample(
                    "jav_pilot_web_download_queue_depth",
                    int(summary.get("queued", 0)),
                    help_text="Queued Web download jobs.",
                ),
                MetricSample(
                    "jav_pilot_web_download_active_jobs",
                    active,
                    help_text="Web download jobs currently owned by workers.",
                ),
                MetricSample(
                    "jav_pilot_web_download_failed_jobs",
                    int(summary.get("failed", 0)),
                    help_text="Web download jobs currently in failed state.",
                ),
                MetricSample(
                    "jav_pilot_web_download_workers_configured",
                    len(workers),
                    help_text="Configured Web download worker threads.",
                ),
                MetricSample(
                    "jav_pilot_web_download_workers_alive",
                    sum(worker.is_alive() for worker in workers),
                    help_text="Live Web download worker threads.",
                ),
            )
        )
    else:
        for name, help_text in (
            ("jav_pilot_web_download_queue_depth", "Queued Web download jobs."),
            (
                "jav_pilot_web_download_active_jobs",
                "Web download jobs currently owned by workers.",
            ),
            (
                "jav_pilot_web_download_failed_jobs",
                "Web download jobs currently in failed state.",
            ),
            (
                "jav_pilot_web_download_workers_configured",
                "Configured Web download worker threads.",
            ),
            (
                "jav_pilot_web_download_workers_alive",
                "Live Web download worker threads.",
            ),
        ):
            samples.append(MetricSample(name, 0, help_text=help_text))

    process_count, zombie_count = _browser_process_counts()
    samples.extend(
        (
            MetricSample(
                "jav_pilot_browser_gate_active",
                MISSAV_BROWSER_GATE.active_count,
                help_text="Active permits in the shared MissAV browser gate.",
            ),
            MetricSample(
                "jav_pilot_browser_gate_waiters",
                MISSAV_BROWSER_GATE.download_waiter_count,
                help_text="Foreground downloads waiting for a browser permit.",
            ),
            MetricSample(
                "jav_pilot_browser_processes",
                process_count,
                help_text="Observed Chromium-family processes in the container.",
            ),
            MetricSample(
                "jav_pilot_browser_zombie_processes",
                zombie_count,
                help_text="Observed defunct Chromium-family processes.",
            ),
        )
    )
    try:
        browser_service = get_missav_browser_runtime().metrics_snapshot()
    except Exception:
        browser_service = None
    if browser_service is not None:
        samples.extend(
            (
                MetricSample(
                    "jav_pilot_missav_browser_queue_depth",
                    browser_service.queue_depth,
                    help_text="Queued operations in the single MissAV browser.",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_cooldown_seconds",
                    browser_service.cooldown_remaining_seconds,
                    help_text="Remaining global MissAV browser cooldown.",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_restart_attempts",
                    browser_service.restart_attempts,
                    help_text="Bounded MissAV browser restart attempts.",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_active_elapsed_seconds",
                    browser_service.active_elapsed_seconds,
                    help_text="Elapsed time of the active bounded MissAV operation.",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_active_stage_elapsed_seconds",
                    browser_service.active_stage_elapsed_seconds,
                    help_text="Elapsed time of the active bounded MissAV capture stage.",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_upstream_events_total",
                    browser_service.upstream_events,
                    help_text="Sanitized upstream responses seen by the MissAV browser.",
                    metric_type="counter",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_challenge_events_total",
                    browser_service.challenge_events,
                    help_text="Managed challenge responses seen by the MissAV browser.",
                    metric_type="counter",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_probes_started_total",
                    browser_service.probes_started,
                    help_text="MissAV browser cooldown probes started.",
                    metric_type="counter",
                ),
                MetricSample(
                    "jav_pilot_missav_browser_probes_succeeded_total",
                    browser_service.probes_succeeded,
                    help_text="MissAV browser cooldown probes completed successfully.",
                    metric_type="counter",
                ),
            )
        )
        for reason, count in browser_service.restart_by_reason:
            samples.append(
                MetricSample(
                    "jav_pilot_missav_browser_restarts_total",
                    count,
                    (("reason", reason),),
                    "MissAV browser restarts by sanitized bounded reason.",
                    metric_type="counter",
                )
            )
        for stage in (
            "locate_exact_detail",
            "reload_detail",
            "start_player",
            "manifest_wait",
            "quality_resolution",
        ):
            samples.append(
                MetricSample(
                    "jav_pilot_missav_browser_active_stage",
                    int(browser_service.active_stage == stage),
                    (("stage", stage),),
                    "Active sanitized MissAV capture stage.",
                )
            )
        for kind, count in browser_service.queued_by_kind:
            samples.append(
                MetricSample(
                    "jav_pilot_missav_browser_queued_operations",
                    count,
                    (("kind", kind),),
                    "Queued MissAV browser operations by bounded kind.",
                )
            )
            samples.append(
                MetricSample(
                    "jav_pilot_missav_browser_active_operation",
                    int(browser_service.active_kind == kind),
                    (("kind", kind),),
                    "Active MissAV browser operation kind.",
                )
            )

    for component, path in _sqlite_metric_paths().items():
        available, busy, latency = _sqlite_probe(path)
        if busy:
            RUNTIME_METRICS.record_sqlite_busy()
        labels = (("component", component),)
        samples.extend(
            (
                MetricSample(
                    "jav_pilot_sqlite_available",
                    available,
                    labels,
                    "Whether a bounded local SQLite probe succeeded.",
                ),
                MetricSample(
                    "jav_pilot_sqlite_busy",
                    busy,
                    labels,
                    "Whether the latest bounded SQLite probe encountered busy state.",
                ),
                MetricSample(
                    "jav_pilot_sqlite_probe_latency_seconds",
                    latency,
                    labels,
                    "Bounded local SQLite probe duration.",
                ),
            )
        )

    for role, path in _disk_metric_paths().items():
        try:
            free_bytes = shutil.disk_usage(path).free
            available = 1
        except OSError:
            free_bytes = 0
            available = 0
        labels = (("role", role),)
        samples.extend(
            (
                MetricSample(
                    "jav_pilot_disk_free_bytes",
                    free_bytes,
                    labels,
                    "Free bytes on configured data and media filesystems.",
                ),
                MetricSample(
                    "jav_pilot_disk_available",
                    available,
                    labels,
                    "Whether the configured filesystem can be inspected.",
                ),
            )
        )

    store = state.SITE_DIAGNOSTICS
    if store is not None:
        try:
            statuses = store.list()
        except (OSError, sqlite3.Error, SiteDiagnosticStoreError):
            statuses = []
        for status in statuses:
            labels = (("site", status.site), ("stage", status.stage))
            samples.extend(
                (
                    MetricSample(
                        "jav_pilot_site_last_success_timestamp_seconds",
                        status.last_success_at or 0,
                        labels,
                        "Timestamp of the latest successful sanitized site stage probe.",
                    ),
                    MetricSample(
                        "jav_pilot_site_probe_latency_seconds",
                        status.last_latency_ms / 1000,
                        labels,
                        "Latency of the latest sanitized site stage probe.",
                    ),
                    MetricSample(
                        "jav_pilot_site_consecutive_failures",
                        status.consecutive_failures,
                        labels,
                        "Consecutive failures for a sanitized site stage probe.",
                    ),
                )
            )
    samples.extend(RUNTIME_METRICS.samples())
    return samples


def _sqlite_metric_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    try:
        paths["metadata_search"] = metadata_search_database_path()
    except (OSError, ValueError, MetadataSearchStoreError):
        pass
    try:
        paths["site_diagnostics"] = site_diagnostic_database_path()
    except (OSError, ValueError, SiteDiagnosticStoreError):
        pass
    try:
        config = WebDownloadConfig.from_env()
        if config.enabled:
            paths["web_downloads"] = Path(config.database_path)
    except (OSError, ValueError, WebDownloadError):
        pass
    try:
        config = MediaMetadataConfig.from_env()
        if config.enabled:
            paths["media_metadata"] = config.database_path
    except (OSError, ValueError, MediaMetadataError):
        pass
    try:
        config = MediaLibraryConfig.from_env()
        if config.enabled:
            paths["media_library"] = config.database_path
    except (OSError, ValueError, MediaLibraryError):
        pass
    return paths


def _disk_metric_paths() -> dict[str, Path]:
    paths = {"data": runtime_config_path().parent}
    try:
        config = WebDownloadConfig.from_env()
        if config.enabled:
            paths["web_staging"] = Path(config.staging_path)
            paths["media"] = Path(config.library_path)
    except (OSError, ValueError, WebDownloadError):
        pass
    return paths


def _sqlite_probe(path: Path) -> tuple[int, int, float]:
    started = time.monotonic()
    if not path.is_file():
        return 0, 0, max(0.0, time.monotonic() - started)
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=0.05,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA busy_timeout = 50")
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        message = str(exc).casefold()
        busy = int("locked" in message or "busy" in message)
        return 0, busy, max(0.0, time.monotonic() - started)
    except (OSError, sqlite3.Error, ValueError):
        return 0, 0, max(0.0, time.monotonic() - started)
    return 1, 0, max(0.0, time.monotonic() - started)


def _browser_process_counts() -> tuple[int, int]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0, 0
    count = 0
    zombies = 0
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return 0, 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace")
            clean_name = name.strip().casefold()
            if not any(
                token in clean_name for token in ("chromium", "chrome", "msedge")
            ):
                continue
            count += 1
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            state = stat.rsplit(")", 1)[-1].strip().split(maxsplit=1)[0]
            zombies += int(state == "Z")
        except (OSError, IndexError):
            continue
    return count, zombies
