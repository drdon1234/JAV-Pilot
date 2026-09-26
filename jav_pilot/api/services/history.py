"""History lifecycle, retention scheduling and backup status."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

from ...config.app_config import AppConfig
from ...config.paths import default_history_backup_root
from ...config.runtime_config import (
    RuntimeConfigError,
    history_retention_config,
    runtime_config_path,
)
from ...config.settings import settings_path
from ...core.observability import emit_json_log
from ...downloads.replacements import (
    SCHEMA_COMPONENT as DOWNLOAD_REPLACEMENT_SCHEMA_COMPONENT,
)
from ...downloads.replacements import (
    SCHEMA_VERSION as DOWNLOAD_REPLACEMENT_SCHEMA_VERSION,
)
from ...downloads.replacements import (
    DownloadReplacementStore,
)
from ...history.errors import HistoryLifecycleConflictError
from ...history.lifecycle import HistoryLifecycle
from ...history.models import (
    DEFAULT_PREVIEW_SECONDS,
    MAX_CLEANUP_RECORDS,
    MAX_EXPORT_RECORDS,
)
from ...history.retention import (
    DEFAULT_AUTO_BATCH_SIZE,
    DEFAULT_AUTO_MAX_BATCHES,
    DEFAULT_AUTO_MAX_RECORDS,
    HistoryRetentionSchedule,
    HistoryRetentionScheduleError,
    HistoryRetentionScheduler,
    HistoryRetentionSchedulerError,
    HistoryRetentionStateError,
    recover_history_retention_state,
)
from ...library.schema import initialize_media_library_schema
from ...library.worker import MediaLibraryConfig
from ...maintenance.backup import BackupError, verify_backup
from ...media_metadata.manager import MediaMetadataConfig
from ...media_metadata.store import (
    CURRENT_SCHEMA_VERSION as MEDIA_METADATA_SCHEMA_VERSION,
)
from ...media_metadata.store import (
    MediaMetadataStore,
)
from ...missav.browser_gate import MISSAV_BROWSER_GATE
from ...notifications.outbox import NOTIFICATION_SCHEMA_VERSION
from ...web_download.batches.schema import BATCH_SCHEMA_VERSION
from ...web_download.batches.store import WebDownloadBatchStore
from ...web_download.config import WebDownloadConfig
from ...web_download.job_store import WebDownloadStore
from ...web_download.jobs import WEB_DOWNLOAD_SCHEMA_VERSION
from .. import state


def recover_history_before_workers() -> bool:
    web_config = WebDownloadConfig.from_env()
    metadata_config = MediaMetadataConfig.from_env()
    library_config = MediaLibraryConfig.from_env()
    database_paths = (
        Path(web_config.database_path),
        Path(metadata_config.database_path),
        Path(library_config.database_path),
    )
    database_identities: list[tuple[int, int] | None] = []
    for path in database_paths:
        main_identity = _regular_database_artifact_identity(path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = path.with_name(f"{path.name}{suffix}")
            sidecar_identity = _regular_database_artifact_identity(sidecar)
            if sidecar_identity is not None and main_identity is None:
                raise HistoryLifecycleConflictError(
                    "history cleanup recovery database set is incomplete"
                )
        database_identities.append(main_identity)
    database_files = [identity is not None for identity in database_identities]
    if not any(database_files):
        return False
    if all(database_files):
        history_lifecycle()
        return True
    if database_files != [True, True, False]:
        raise HistoryLifecycleConflictError(
            "history cleanup recovery database set is incomplete"
        )

    if not _history_database_matches(
        database_paths[0],
        expected_versions={"web_downloads": 2, "web_download_batches": 2},
        required_columns={
            "schema_migrations": ("component", "version", "applied_at"),
            "web_download_jobs": (
                "job_id",
                "status",
                "requested_height",
                "selected_height",
                "verified_height",
                "quality_strategy",
                "existing_policy",
                "output_path",
            ),
            "web_download_batches": (
                "batch_id",
                "status",
                "parent_batch_id",
                "page_number",
                "continuation_batch_id",
                "existing_policy",
                "commit_intent_hash",
            ),
            "web_download_batch_items": (
                "batch_id",
                "position",
                "code_key",
                "status",
                "selected",
            ),
        },
    ) or not _history_database_matches(
        database_paths[1],
        expected_versions={"media_metadata": 1},
        required_columns={
            "schema_migrations": ("component", "version", "applied_at"),
            "jobs": (
                "job_id",
                "kind",
                "download_key",
                "code_key",
                "status",
                "assets_json",
            ),
        },
    ):
        raise HistoryLifecycleConflictError(
            "history cleanup recovery database set is incomplete"
        )
    for path, expected_identity in zip(database_paths[:2], database_identities[:2]):
        if _regular_database_artifact_identity(path) != expected_identity:
            raise HistoryLifecycleConflictError(
                "history cleanup recovery database set is incomplete"
            )

    WebDownloadStore(database_paths[0])
    WebDownloadBatchStore(database_paths[0])
    DownloadReplacementStore(database_paths[0])
    MediaMetadataStore(database_paths[1])

    notification_tables = {
        "notification_events": (),
        "notification_adapter_registry": (),
        "notification_deliveries": (),
        "notification_delivery_history": (),
    }
    if not _history_database_matches(
        database_paths[0],
        expected_versions={
            "web_downloads": WEB_DOWNLOAD_SCHEMA_VERSION,
            "web_download_batches": BATCH_SCHEMA_VERSION,
            DOWNLOAD_REPLACEMENT_SCHEMA_COMPONENT: DOWNLOAD_REPLACEMENT_SCHEMA_VERSION,
            "notifications": NOTIFICATION_SCHEMA_VERSION,
        },
        required_columns={
            "schema_migrations": (),
            "web_download_jobs": (),
            "web_download_control": (),
            "web_download_batches": (),
            "web_download_batch_items": (),
            "web_download_batch_rules": (),
            "download_replacements": (),
            "failed_download_archives": (),
            **notification_tables,
        },
    ) or not _history_database_matches(
        database_paths[1],
        expected_versions={
            "media_metadata": MEDIA_METADATA_SCHEMA_VERSION,
            "notifications": NOTIFICATION_SCHEMA_VERSION,
        },
        required_columns={
            "schema_migrations": (),
            "jobs": (),
            **notification_tables,
        },
    ):
        raise HistoryLifecycleConflictError(
            "history cleanup recovery database migration is incomplete"
        )
    for suffix in ("", "-wal", "-shm", "-journal"):
        library_artifact = database_paths[2].with_name(
            f"{database_paths[2].name}{suffix}"
        )
        if _regular_database_artifact_identity(library_artifact) is not None:
            raise HistoryLifecycleConflictError(
                "history cleanup recovery database set is incomplete"
            )

    initialize_media_library_schema(database_paths[2])
    history_lifecycle()
    return True


def _regular_database_artifact_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HistoryLifecycleConflictError(
            "history cleanup recovery database set is incomplete"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise HistoryLifecycleConflictError(
            "history cleanup recovery database set is incomplete"
        )
    return int(metadata.st_dev), int(metadata.st_ino)


def _history_database_matches(
    path: Path,
    *,
    expected_versions: dict[str, int],
    required_columns: dict[str, tuple[str, ...]],
) -> bool:
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                return False
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return False
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not set(required_columns).issubset(tables):
                return False
            for table, expected_columns in required_columns.items():
                columns = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM pragma_table_info(?)", (table,)
                    ).fetchall()
                }
                if not set(expected_columns).issubset(columns):
                    return False
            rows = connection.execute(
                "SELECT component, MAX(version) FROM schema_migrations "
                "GROUP BY component"
            ).fetchall()
            versions = {str(component): int(version) for component, version in rows}
            return versions == expected_versions
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def history_lifecycle() -> HistoryLifecycle:
    with state.HISTORY_LIFECYCLE_LOCK:
        if state.SERVER_STOPPING.is_set():
            raise HistoryLifecycleConflictError("server is shutting down")
        if state.HISTORY_LIFECYCLE is not None:
            return state.HISTORY_LIFECYCLE
        web_config = WebDownloadConfig.from_env()
        metadata_config = MediaMetadataConfig.from_env()
        library_config = MediaLibraryConfig.from_env()
        state.HISTORY_LIFECYCLE = HistoryLifecycle(
            web_database_path=web_config.database_path,
            metadata_database_path=metadata_config.database_path,
            media_library_database_path=library_config.database_path,
            review_database_path=metadata_config.database_path,
            maintenance_guard=history_maintenance_mode,
            workers_stopped_guard=_history_operational_workers_stopped,
        )
        return state.HISTORY_LIFECYCLE


def reset_history_lifecycle() -> None:
    if not state.HISTORY_LIFECYCLE_LOCK.acquire(blocking=False):
        return
    try:
        state.HISTORY_LIFECYCLE = None
    finally:
        state.HISTORY_LIFECYCLE_LOCK.release()
    state.HISTORY_BACKUP_STATUS_CACHE.clear()


def start_history_retention_scheduler() -> None:
    with state.HISTORY_RETENTION_SCHEDULER_LOCK:
        current = state.HISTORY_RETENTION_SCHEDULER
        if current is not None and current.is_alive:
            return
        try:
            scheduler = HistoryRetentionScheduler(history_lifecycle)
            scheduler.start()
        except (
            HistoryRetentionSchedulerError,
            RuntimeConfigError,
            OSError,
            ValueError,
        ) as exc:
            state.HISTORY_RETENTION_SCHEDULER = None
            state.HISTORY_RETENTION_STARTUP_ERROR_CODE = (
                "state_unavailable"
                if isinstance(exc, HistoryRetentionStateError)
                else "configuration_invalid"
                if isinstance(exc, (HistoryRetentionScheduleError, RuntimeConfigError))
                else "scheduler_unavailable"
            )
            emit_json_log(
                "history_retention",
                "startup_unavailable",
                level="error",
                error_code=state.HISTORY_RETENTION_STARTUP_ERROR_CODE,
                outcome="failed",
            )
            return
        state.HISTORY_RETENTION_SCHEDULER = scheduler
        state.HISTORY_RETENTION_STARTUP_ERROR_CODE = None


def stop_history_retention_scheduler(*, timeout: float) -> bool:
    with state.HISTORY_RETENTION_SCHEDULER_LOCK:
        scheduler = state.HISTORY_RETENTION_SCHEDULER
    if scheduler is None:
        return True
    stopped = scheduler.stop(timeout=timeout)
    if stopped:
        with state.HISTORY_RETENTION_SCHEDULER_LOCK:
            if state.HISTORY_RETENTION_SCHEDULER is scheduler:
                state.HISTORY_RETENTION_SCHEDULER = None
        return True
    emit_json_log(
        "history_retention",
        "shutdown_deadline_exceeded",
        level="warning",
        outcome="pending",
    )
    return False


def wake_history_retention_scheduler() -> None:
    with state.HISTORY_RETENTION_SCHEDULER_LOCK:
        scheduler = state.HISTORY_RETENTION_SCHEDULER
    if scheduler is not None and scheduler.is_alive:
        scheduler.wake()
        return
    start_history_retention_scheduler()


def recover_history_retention_scheduler_state() -> str | None:
    with state.HISTORY_RETENTION_SCHEDULER_LOCK:
        scheduler = state.HISTORY_RETENTION_SCHEDULER
    if scheduler is not None:
        return scheduler.recover_state()
    return recover_history_retention_state()


def _history_retention_scheduler_status() -> dict[str, object]:
    with state.HISTORY_RETENTION_SCHEDULER_LOCK:
        scheduler = state.HISTORY_RETENTION_SCHEDULER
        startup_error = state.HISTORY_RETENTION_STARTUP_ERROR_CODE
    if scheduler is not None:
        return scheduler.status()
    schedule = HistoryRetentionSchedule.from_mapping(history_retention_config())
    if startup_error is not None:
        return {
            "auto_enabled": schedule.auto_enabled,
            "active": schedule.active,
            "timezone": schedule.timezone,
            "hour": schedule.hour,
            "worker_alive": False,
            "ready": False,
            "last_started_at": None,
            "last_succeeded_at": None,
            "last_error_code": startup_error,
            "outcome": "unavailable",
            "next_retry_at": None,
            "next_run_at": None,
            "last_removed_records": 0,
            "last_batches": 0,
            "batch_size": DEFAULT_AUTO_BATCH_SIZE,
            "max_batches_per_run": DEFAULT_AUTO_MAX_BATCHES,
            "max_records_per_run": DEFAULT_AUTO_MAX_RECORDS,
            "state_recovery_required": startup_error == "state_unavailable",
        }
    inactive = HistoryRetentionScheduler(history_lifecycle)
    status = inactive.status()
    status["worker_alive"] = False
    status["ready"] = not schedule.active
    return status


def history_retention_scheduler_ready() -> bool:
    try:
        schedule = HistoryRetentionSchedule.from_mapping(history_retention_config())
    except (HistoryRetentionSchedulerError, RuntimeConfigError, OSError, ValueError):
        return False
    with state.HISTORY_RETENTION_SCHEDULER_LOCK:
        scheduler = state.HISTORY_RETENTION_SCHEDULER
        startup_error = state.HISTORY_RETENTION_STARTUP_ERROR_CODE
    if not schedule.active:
        return True
    if startup_error is not None:
        return False
    return bool(scheduler and scheduler.ready)


def history_status_payload(_lifecycle: HistoryLifecycle) -> dict[str, object]:
    backup_verified, backup_created_at = _history_backup_status()
    retention = history_retention_config()
    return {
        "ok": True,
        "retention": {
            task_type: retention[task_type]
            for task_type in ("web", "batch", "metadata")
        },
        "retention_schedule": _history_retention_scheduler_status(),
        "maintenance_mode": history_maintenance_mode(),
        "backup_verified": backup_verified,
        "backup_created_at": backup_created_at,
        "preview_ttl_seconds": int(DEFAULT_PREVIEW_SECONDS),
        "limits": {
            "cleanup_records": MAX_CLEANUP_RECORDS,
            "export_records": MAX_EXPORT_RECORDS,
        },
    }


def history_maintenance_mode() -> bool:
    value = os.environ.get("JAV_PILOT_HISTORY_MAINTENANCE_MODE", "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise RuntimeConfigError("history maintenance mode is invalid")


def require_operational_mode(error: BaseException) -> None:
    try:
        maintenance_mode = history_maintenance_mode()
    except RuntimeConfigError as exc:
        raise error from exc
    if maintenance_mode:
        raise error


def _history_operational_workers_stopped() -> bool:
    with state.OPERATIONAL_MANAGER_LOCK:
        organizer = state.COMPLETED_ORGANIZER_WORKER
        diagnostics = state.SITE_DIAGNOSTIC_SCHEDULER
        retention = state.HISTORY_RETENTION_SCHEDULER
        notification = state.NOTIFICATION_DISPATCHER_WORKER
        return bool(
            state.WEB_DOWNLOADS is None
            and not state.WEB_DOWNLOADS_PENDING_SHUTDOWN
            and state.WEB_DOWNLOAD_BATCHES is None
            and state.MEDIA_METADATA is None
            and state.MEDIA_METADATA_REVIEW is None
            and state.MEDIA_LIBRARY is None
            and (organizer is None or not organizer.is_alive())
            and (diagnostics is None or not diagnostics.is_alive)
            and (retention is None or not retention.is_alive)
            and (notification is None or not notification.is_alive())
            and MISSAV_BROWSER_GATE.active_count == 0
        )


def history_backup_path() -> Path | None:
    raw = os.environ.get("JAV_PILOT_HISTORY_BACKUP_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError("history backup path must be absolute")
    root = _history_backup_root()
    resolved = _resolved_history_path(path, "history backup path")
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeConfigError(
            "history backup path must be below the read-only backup root"
        )
    return resolved


def _history_backup_root() -> Path:
    configured = os.environ.get("JAV_PILOT_HISTORY_BACKUP_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else default_history_backup_root()
    if not root.is_absolute():
        raise RuntimeConfigError("history backup root must be absolute")
    resolved = _resolved_history_path(root, "history backup root")
    if resolved == Path(resolved.anchor):
        raise RuntimeConfigError("history backup root is unsafe")
    for unsafe in _history_unsafe_roots():
        if resolved.is_relative_to(unsafe) or unsafe.is_relative_to(resolved):
            raise RuntimeConfigError(
                "history backup root must be isolated from data, media, and downloads"
            )
    if not _history_backup_root_is_read_only(resolved):
        raise RuntimeConfigError("history backup root must be mounted read-only")
    return resolved


def _history_unsafe_roots() -> tuple[Path, ...]:
    web = WebDownloadConfig.from_env()
    metadata = MediaMetadataConfig.from_env()
    library = MediaLibraryConfig.from_env()
    qb = AppConfig.from_env().qbittorrent
    candidates = (
        runtime_config_path().parent,
        settings_path().parent,
        Path(web.database_path).parent,
        Path(web.staging_path),
        Path(web.library_path),
        metadata.database_path.parent,
        metadata.library_path,
        metadata.backup_path,
        library.database_path.parent,
        library.library_path,
        Path(qb.save_path),
        Path(qb.library_path),
        Path(qb.app_library_path),
    )
    return tuple(
        dict.fromkeys(
            _resolved_history_path(path, "operational storage root")
            for path in candidates
        )
    )


def _resolved_history_path(path: Path, label: str) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RuntimeConfigError(f"{label} is invalid") from exc


def _history_backup_root_is_read_only(root: Path) -> bool:
    if os.name != "posix":
        return True
    try:
        flags = os.statvfs(root).f_flag
    except OSError:
        return False
    return bool(flags & getattr(os, "ST_RDONLY", 1))


def _history_backup_status() -> tuple[bool, float | None]:
    path = history_backup_path()
    if path is None:
        return False, None
    manifest_path = path / "manifest.json"
    try:
        info = manifest_path.stat(follow_symlinks=False)
    except OSError:
        return False, None
    cache_key = (str(path), int(info.st_dev), int(info.st_ino), int(info.st_mtime_ns))
    cached = state.HISTORY_BACKUP_STATUS_CACHE.get(cache_key)
    if cached is not None:
        return bool(cached["verified"]), _optional_float(cached.get("created_at"))
    try:
        manifest = verify_backup(path)
        created_at = _history_backup_created_at(manifest.get("created_at"))
    except (BackupError, OSError, ValueError):
        result: dict[str, object] = {"verified": False, "created_at": None}
    else:
        result = {"verified": True, "created_at": created_at}
    state.HISTORY_BACKUP_STATUS_CACHE.set(cache_key, result)
    return bool(result["verified"]), _optional_float(result.get("created_at"))


def _history_backup_created_at(value: object) -> float | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    parsed = datetime.strptime(clean, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return parsed.timestamp()


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
