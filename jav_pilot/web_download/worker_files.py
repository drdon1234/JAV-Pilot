"""Staging and job directories, safe paths, locks and free-space checks for the worker."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

from ..core.catalog_code import canonical_catalog_code
from .worker_common import SAFE_CODE_RE, raise_if_cancelled
from .worker_errors import WebDownloadWorkerDiskLowError, WebDownloadWorkerError

def prepare_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if path_is_linklike(path) or not path.is_dir():
        raise WebDownloadWorkerError("download root must be a regular directory")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise WebDownloadWorkerError("download root is unsafe")
    return resolved


def prepare_job_dir(root: Path, job_id: str) -> Path:
    job_dir = root / job_id
    require_safe_child(job_dir, root)
    if job_dir.exists():
        if path_is_linklike(job_dir) or not job_dir.is_dir():
            raise WebDownloadWorkerError("unsafe worker staging path")
        os.chmod(job_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return job_dir
    job_dir.mkdir(mode=0o700)
    return job_dir


def create_child_directory(path: Path, root: Path) -> None:
    require_safe_child(path, root)
    if path.exists():
        if path_is_linklike(path) or not path.is_dir():
            raise WebDownloadWorkerError("archive destination must be a directory")
        if path.resolve(strict=True) != path.absolute():
            raise WebDownloadWorkerError("archive destination is unsafe")
        return
    try:
        path.mkdir(mode=0o755)
    except FileExistsError:
        if path_is_linklike(path) or not path.is_dir():
            raise WebDownloadWorkerError("archive destination must be a directory")
    if path_is_linklike(path) or path.resolve(strict=True) != path.absolute():
        raise WebDownloadWorkerError("archive destination is unsafe")


def path_is_linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def remove_job_dir(path: Path, root: Path) -> None:
    require_safe_child(path, root)
    if path_is_linklike(path):
        raise WebDownloadWorkerError("refusing to remove a linked staging path")
    if path.exists():
        shutil.rmtree(path)


def require_safe_child(path: Path, root: Path) -> None:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise WebDownloadWorkerError("path escapes configured download root") from exc
    if path.absolute() == root.absolute():
        raise WebDownloadWorkerError("path must be below configured download root")
    parent = path.parent
    if parent.exists() and parent.resolve(strict=True) != parent.absolute():
        raise WebDownloadWorkerError("symlinked download path is not allowed")


def absolute_root(value: object) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise WebDownloadWorkerError("download root is not configured")
    path = Path(raw)
    if not path.is_absolute():
        raise WebDownloadWorkerError("download root must be absolute")
    return path


def absolute_file_path(value: object, name: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise WebDownloadWorkerError(f"{name} path is not configured")
    path = Path(raw)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise WebDownloadWorkerError(f"{name} path must be absolute")
    return path


def optional_relative_output_path(value: object | None) -> str | None:
    if value in (None, ""):
        return None
    if (
        not isinstance(value, str)
        or len(value) > 512
        or "\\" in value
        or "://" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise WebDownloadWorkerError("incumbent output path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise WebDownloadWorkerError("incumbent output path is invalid")
    return value


@contextmanager
def exclusive_finalize_lock(
    path: Path | None, cancel_event: threading.Event
) -> Iterator[None]:
    if path is None:
        yield
        return
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise WebDownloadWorkerError("finalize lock parent is invalid")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise WebDownloadWorkerError("finalize lock path is invalid")

    try:
        lock_file = path.open("a+b")
    except OSError as exc:
        raise WebDownloadWorkerError("finalize lock is unavailable") from exc
    with lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            while True:
                raise_if_cancelled(cancel_event)
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise WebDownloadWorkerError(
                            "finalize lock is unavailable"
                        ) from exc
                    if cancel_event.wait(0.1):
                        raise_if_cancelled(cancel_event)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        while True:
            raise_if_cancelled(cancel_event)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise WebDownloadWorkerError(
                        "finalize lock is unavailable"
                    ) from exc
                if cancel_event.wait(0.1):
                    raise_if_cancelled(cancel_event)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def require_free_space(
    path: Path,
    required_bytes: int,
    *,
    volume_key: str = "staging",
) -> None:
    try:
        free_bytes = shutil.disk_usage(path).free
    except OSError as exc:
        raise WebDownloadWorkerError(
            "download storage capacity could not be checked"
        ) from exc
    if free_bytes < required_bytes:
        raise WebDownloadWorkerDiskLowError(volume_key)


def safe_display_code(value: object) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    canonical = canonical_catalog_code(raw, max_length=64)
    if canonical is None:
        raise WebDownloadWorkerError("invalid catalog code")
    if not raw.isascii() or not SAFE_CODE_RE.fullmatch(raw):
        raise WebDownloadWorkerError("invalid catalog code")
    return raw


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)
