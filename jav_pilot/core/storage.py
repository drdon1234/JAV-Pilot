from __future__ import annotations

import os
import shutil
import time
from contextlib import suppress
from pathlib import Path


_BACKUP_RETENTION = 10


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{timestamp}-{time.time_ns() % 1_000_000_000:09d}")
    shutil.copy2(path, backup)
    _restrict_permissions(backup)
    _prune_backups(path, keep=_BACKUP_RETENTION)
    return backup


def _prune_backups(path: Path, *, keep: int) -> None:
    prefix = f"{path.name}.bak."
    backups = sorted(
        (
            candidate
            for candidate in path.parent.iterdir()
            if candidate.name.startswith(prefix)
            and candidate.is_file()
            and not candidate.is_symlink()
        ),
        key=lambda candidate: candidate.name,
        reverse=True,
    )
    for stale in backups[max(1, keep) :]:
        with suppress(OSError):
            stale.unlink()


def atomic_write_text(path: Path, text: str, *, restrict_permissions: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if restrict_permissions:
            _restrict_permissions(temporary)
        _fsync_file(temporary)
        os.replace(temporary, path)
        if restrict_permissions:
            _restrict_permissions(path)
        _fsync_file(path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)
