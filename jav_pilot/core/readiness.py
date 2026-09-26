from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class DiskUsage(Protocol):
    free: int


class CachedReadinessProbe:
    def __init__(
        self,
        probe: Callable[[], dict[str, bool]],
        *,
        ttl_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("readiness cache TTL must be positive")
        self._probe = probe
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._cached_at: float | None = None
        self._cached_checks: dict[str, bool] | None = None

    def check(self) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            if (
                self._cached_at is None
                or self._cached_checks is None
                or now - self._cached_at >= self._ttl_seconds
            ):
                try:
                    checks = self._probe()
                except Exception:
                    checks = {"probe": False}
                self._cached_checks = {
                    str(name): value is True for name, value in checks.items()
                }
                self._cached_at = now
            result = dict(self._cached_checks)
        return {"ok": bool(result) and all(result.values()), "checks": result}

    def invalidate(self) -> None:
        with self._lock:
            self._cached_at = None
            self._cached_checks = None


def writable_directory(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError:
        return False
    return True


def disk_space_ready(
    path: Path,
    *,
    min_free_bytes: int,
    disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
) -> bool:
    try:
        required = int(min_free_bytes)
    except (TypeError, ValueError, OverflowError):
        return False
    if isinstance(min_free_bytes, bool) or required < 0:
        return False
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        free = int(disk_usage(path).free)
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    return free >= required


def sqlite_database_ready(
    path: Path,
    *,
    required_tables: tuple[str, ...] = (),
    timeout_seconds: float = 1.0,
) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        timeout = float(timeout_seconds)
        if not 0.05 <= timeout <= 5.0:
            return False
        connection = sqlite3.connect(path, timeout=timeout)
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA schema_version").fetchone() is None:
                return False
            return all(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                is not None
                for table in required_tables
            )
        finally:
            connection.close()
    except (OSError, TypeError, ValueError, OverflowError, sqlite3.Error):
        return False
