from __future__ import annotations

import os
import stat
import threading
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import Iterable


MAINTENANCE_LOCK_NAME = ".jav-pilot-maintenance.lock"
MAINTENANCE_LOCK_ROOT_ENV = "JAV_PILOT_MAINTENANCE_LOCK_ROOT"


class MaintenanceLockError(RuntimeError):
    pass


class MaintenanceBusyError(MaintenanceLockError):
    pass


_WINDOWS_LOCKS_GUARD = threading.Lock()
_WINDOWS_LOCKS: dict[str, threading.Lock] = {}


class MaintenanceFileLock(AbstractContextManager["MaintenanceFileLock"]):
    def __init__(self, root: Path | str, *, shared: bool = False) -> None:
        self.root = _canonical_directory(root)
        self.shared = shared
        self.path = self.root / MAINTENANCE_LOCK_NAME
        self._descriptor = -1
        self._windows_lock: threading.Lock | None = None

    def __enter__(self) -> "MaintenanceFileLock":
        root_before = self.root.lstat()
        if _is_linklike(self.root, root_before) or not stat.S_ISDIR(
            root_before.st_mode
        ):
            raise MaintenanceLockError("maintenance root is unsafe")
        if self.shared and os.name != "nt" and stat.S_IMODE(root_before.st_mode) & 0o007:
            raise MaintenanceLockError("shared maintenance root must exclude other users")
        flags = (
            os.O_RDWR
            | (0 if self.shared else os.O_CREAT)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o660 if self.shared else 0o600)
        except OSError as exc:
            raise MaintenanceLockError(
                "maintenance lock cannot be opened safely"
            ) from exc
        self._descriptor = descriptor
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise MaintenanceLockError("maintenance lock is not a regular file")
            if self.shared:
                # A pre-provisioned group-writable inode is shared by the NAS
                # operator and service UID. Neither reader replaces/chowns it.
                self._verify_identity(opened, root_before)
            elif os.name != "nt" and hasattr(os, "fchmod"):
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    os.fchown(
                        descriptor, int(root_before.st_uid), int(root_before.st_gid)
                    )
                elif int(opened.st_uid) == int(root_before.st_uid) and int(
                    opened.st_gid
                ) != int(root_before.st_gid):
                    os.fchown(descriptor, -1, int(root_before.st_gid))
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(self.path, 0o600)
            self._verify_identity(os.fstat(descriptor), root_before)
            self._acquire()
            self._verify_identity(os.fstat(descriptor), root_before)
        except BaseException:
            if self._windows_lock is not None:
                self._windows_lock.release()
                self._windows_lock = None
            os.close(descriptor)
            self._descriptor = -1
            raise
        return self

    def __exit__(self, *_args: object) -> bool:
        descriptor = self._descriptor
        if descriptor >= 0:
            try:
                if os.name == "nt":
                    if self._windows_lock is not None:
                        self._windows_lock.release()
                        self._windows_lock = None
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                self._descriptor = -1
        return False

    def _verify_identity(
        self,
        opened: os.stat_result,
        root_before: os.stat_result,
    ) -> None:
        try:
            current = self.path.lstat()
            root_after = self.root.lstat()
        except OSError as exc:
            raise MaintenanceLockError(
                "maintenance lock identity is unavailable"
            ) from exc
        if (
            _is_linklike(self.root, root_after)
            or not stat.S_ISDIR(root_after.st_mode)
            or (root_after.st_dev, root_after.st_ino)
            != (root_before.st_dev, root_before.st_ino)
            or _is_linklike(self.path, current)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or (
                os.name != "nt"
                and (
                    stat.S_IMODE(current.st_mode) != (0o660 if self.shared else 0o600)
                    or stat.S_IMODE(opened.st_mode) != (0o660 if self.shared else 0o600)
                )
            )
            or (
                os.name != "nt"
                and (
                    (int(current.st_uid), int(current.st_gid))
                    != (int(root_before.st_uid), int(root_before.st_gid))
                    or (int(opened.st_uid), int(opened.st_gid))
                    != (int(root_before.st_uid), int(root_before.st_gid))
                )
            )
        ):
            raise MaintenanceLockError("maintenance lock identity is unsafe")

    def _acquire(self) -> None:
        if os.name == "nt":
            key = os.path.normcase(str(self.path))
            with _WINDOWS_LOCKS_GUARD:
                lock = _WINDOWS_LOCKS.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=False):
                raise MaintenanceBusyError("another maintenance operation is active")
            self._windows_lock = lock
            return
        try:
            import fcntl

            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusyError(
                "another maintenance operation is active"
            ) from exc
        except ImportError as exc:  # pragma: no cover - production is Linux.
            raise MaintenanceLockError(
                "POSIX maintenance locking is unavailable"
            ) from exc


class MaintenanceLocks(AbstractContextManager["MaintenanceLocks"]):
    def __init__(self, roots: Iterable[Path | str]) -> None:
        configured_root = _configured_root()
        self.shared = configured_root is not None
        if configured_root is not None:
            roots = (configured_root,)
        canonical: dict[str, Path] = {}
        for root in roots:
            path = _canonical_directory(root)
            canonical.setdefault(os.path.normcase(str(path)), path)
        self.roots = tuple(canonical[key] for key in sorted(canonical))
        if not self.roots:
            raise MaintenanceLockError("at least one maintenance root is required")
        self._stack: ExitStack | None = None

    def __enter__(self) -> "MaintenanceLocks":
        stack = ExitStack()
        try:
            for root in self.roots:
                stack.enter_context(MaintenanceFileLock(root, shared=self.shared))
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(self, *args: object) -> bool:
        if self._stack is not None:
            stack = self._stack
            self._stack = None
            return bool(stack.__exit__(*args))
        return False


def validate_configured_maintenance_lock() -> None:
    """Validate shared access without taking the lock or changing its inode.

    Readiness runs while the release tool holds the lock, so successful access
    must not depend on an exclusive flock. Unconfigured standalone use keeps
    the ordinary per-root locking contract.
    """
    root = _configured_root()
    if root is None:
        return
    lock = MaintenanceFileLock(root, shared=True)
    details = lock.root.lstat()
    if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o007:
        raise MaintenanceLockError("shared maintenance root must exclude other users")
    if not os.access(lock.root, os.R_OK | os.W_OK | os.X_OK):
        raise MaintenanceLockError("shared maintenance root is inaccessible")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock.path, flags)
    except OSError as exc:
        raise MaintenanceLockError("maintenance lock cannot be opened safely") from exc
    try:
        lock._verify_identity(os.fstat(descriptor), details)
    finally:
        os.close(descriptor)


def _configured_root() -> Path | None:
    configured = os.environ.get(MAINTENANCE_LOCK_ROOT_ENV, "").strip()
    if not configured:
        return None
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise MaintenanceLockError("shared maintenance root must be absolute")
    return root


def _canonical_directory(value: Path | str) -> Path:
    path = Path(value).expanduser()
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MaintenanceLockError("maintenance root is unavailable") from exc
    if _is_linklike(path, details) or not stat.S_ISDIR(details.st_mode):
        raise MaintenanceLockError("maintenance root is unsafe")
    return resolved


def _is_linklike(path: Path, details: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & reparse_flag
    )


__all__ = [
    "MAINTENANCE_LOCK_NAME",
    "MAINTENANCE_LOCK_ROOT_ENV",
    "MaintenanceBusyError",
    "MaintenanceFileLock",
    "MaintenanceLockError",
    "MaintenanceLocks",
    "validate_configured_maintenance_lock",
]
