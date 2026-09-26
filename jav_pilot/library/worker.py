from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    MediaLibraryConflictError,
    MediaLibraryError,
    MediaLibraryRootChangedError,
)
from .index import MediaLibraryIndex
from .models import DEFAULT_FULL_SCAN_SECONDS, ReconcileReport
from ..config.paths import default_database_path, default_library_path


@dataclass(frozen=True, slots=True)
class MediaLibraryConfig:
    enabled: bool
    library_path: Path
    database_path: Path
    reconcile_seconds: float
    full_scan_seconds: float

    @classmethod
    def from_env(cls) -> "MediaLibraryConfig":
        enabled = _env_bool("JAV_PILOT_MEDIA_LIBRARY_ENABLED", True)
        library_path = Path(
            os.environ.get("JAV_PILOT_MEDIA_LIBRARY_PATH", "").strip()
            or default_library_path()
        ).expanduser()
        configured_database = os.environ.get(
            "JAV_PILOT_MEDIA_LIBRARY_DATABASE_PATH", ""
        ).strip()
        database_path = (
            Path(configured_database).expanduser()
            if configured_database
            else default_database_path("media_library.sqlite3")
        )
        if not library_path.is_absolute() or not database_path.is_absolute():
            raise MediaLibraryError("media library paths must be absolute")
        return cls(
            enabled=enabled,
            library_path=library_path,
            database_path=database_path,
            reconcile_seconds=_env_seconds(
                "JAV_PILOT_MEDIA_LIBRARY_RECONCILE_SECONDS",
                300.0,
                minimum=30.0,
            ),
            full_scan_seconds=_env_seconds(
                "JAV_PILOT_MEDIA_LIBRARY_FULL_SCAN_SECONDS",
                DEFAULT_FULL_SCAN_SECONDS,
                minimum=300.0,
            ),
        )


class MediaLibraryManager:
    def __init__(
        self,
        config: MediaLibraryConfig,
        *,
        on_changed: Callable[[ReconcileReport], None] | None = None,
    ) -> None:
        if not config.enabled:
            raise MediaLibraryError("media library index is disabled")
        self.config = config
        self._on_changed = on_changed
        self.index = MediaLibraryIndex(
            config.library_path,
            config.database_path,
            full_scan_seconds=config.full_scan_seconds,
            audit_step_seconds=config.reconcile_seconds,
        )
        self._stop = threading.Event()
        self._wake = threading.Condition()
        self._wake_generation = 0
        self._pending_paths: set[str] = set()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.RLock()
        self._reconcile_lock = threading.Lock()
        self._indexing = False
        self._last_completed_at: float | None = None
        self._last_error_code: str | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="jav-media-library-index",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 30.0) -> bool:
        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError("media library shutdown timeout is invalid")
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return not self.is_alive

    def reconcile_now(
        self,
        *,
        force_full: bool = False,
        accept_root_change: bool = False,
        refresh_paths: tuple[str, ...] = (),
        expected_revision: int | None = None,
    ) -> ReconcileReport:
        if not self._reconcile_lock.acquire(blocking=False):
            raise MediaLibraryConflictError(
                "media library reconciliation is already running"
            )
        indexing = False
        try:
            root = self.index.store.root(self.index.root_key)
            current_revision = int(root["revision"]) if root is not None else 0
            if expected_revision is not None and expected_revision != current_revision:
                raise MediaLibraryConflictError("media library revision changed")
            with self._status_lock:
                self._indexing = True
            indexing = True
            report = self.index.reconcile(
                force_full=force_full,
                accept_root_change=accept_root_change,
                refresh_paths=refresh_paths,
            )
        except MediaLibraryConflictError:
            raise
        except Exception as exc:
            with self._status_lock:
                self._last_error_code = _error_code(exc)
            raise
        else:
            with self._status_lock:
                self._last_completed_at = time.time()
                self._last_error_code = None
            return report
        finally:
            if indexing:
                with self._status_lock:
                    self._indexing = False
            self._reconcile_lock.release()

    def request_reconcile(self, refresh_paths: tuple[str, ...] = ()) -> None:
        if isinstance(refresh_paths, (str, bytes, bytearray)):
            raise MediaLibraryError("media library refresh paths are invalid")
        with self._wake:
            self._pending_paths.update(str(path) for path in refresh_paths)
            self._wake_generation += 1
            self._wake.notify()

    def status(self) -> dict[str, object]:
        root = self.index.store.root(self.index.root_key)
        with self._status_lock:
            indexing = self._indexing
            completed = self._last_completed_at
            error_code = self._last_error_code
        state = "indexing" if indexing else "initializing"
        revision = 0
        if root is not None:
            state = "unknown" if root["state"] == "unknown" else "ready"
            if indexing:
                state = "indexing"
            revision = int(root["revision"])
        return {
            "state": state,
            "revision": revision,
            "last_completed_at": completed,
            "last_error_code": error_code,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                observed_generation = self._wake_generation
                refresh_paths = tuple(sorted(self._pending_paths))
                self._pending_paths.clear()
            try:
                try:
                    report = self.reconcile_now(refresh_paths=refresh_paths)
                except MediaLibraryRootChangedError:
                    if not self._same_library_after_remount():
                        raise
                    # NAS reboots and container remounts give the same folder
                    # a new device/inode. When the indexed works are still
                    # there, rebuild instead of staying "unknown" forever.
                    report = self.reconcile_now(force_full=True, accept_root_change=True)
                if report.changed and self._on_changed is not None:
                    try:
                        self._on_changed(report)
                    except Exception:  # noqa: BLE001 - an observer never stops indexing.
                        pass
            except Exception:  # noqa: BLE001 - status exposes only a fixed error code.
                # A failed incremental reconcile must not lose the precise
                # refresh hints that triggered it; preserve them for retry.
                # Requeuing is not an external wake request: retry on the
                # normal interval instead of spinning while the root is down.
                with self._wake:
                    self._pending_paths.update(refresh_paths)
            with self._wake:
                self._wake.wait_for(
                    lambda: self._stop.is_set()
                    or self._wake_generation != observed_generation,
                    timeout=self.config.reconcile_seconds,
                )


    def _same_library_after_remount(self) -> bool:
        """True when most previously indexed media still exist under the root."""

        try:
            sample = self.index.store.list_entries(
                root_key=self.index.root_key, limit=50, presence="present"
            ).get("items") or []
        except Exception:  # noqa: BLE001 - an unreadable index is not evidence.
            return False
        if not sample:
            return True
        root = Path(self.config.library_path)
        present = 0
        for item in sample:
            relative = str(item.get("primary_media_path") or "")
            if relative and (root / relative).is_file():
                present += 1
        return present >= max(1, int(len(sample) * 0.8))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise MediaLibraryError(f"{name} is invalid")


def _env_seconds(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise MediaLibraryError(f"{name} is invalid") from None
    if not math.isfinite(value) or not minimum <= value <= 7 * 24 * 60 * 60:
        raise MediaLibraryError(f"{name} is invalid")
    return value


def _error_code(error: BaseException) -> str:
    name = type(error).__name__.casefold()
    if "rootchanged" in name:
        return "root_changed"
    if isinstance(error, OSError) or "unavailable" in name:
        return "root_unavailable"
    if "conflict" in name:
        return "busy"
    return "index_failed"


__all__ = ["MediaLibraryConfig", "MediaLibraryManager"]
