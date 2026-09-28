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
    # The copy holds the same secrets as the original: create it private
    # instead of tightening it after the content is already written.
    with path.open("rb") as source, _open_new_file(backup, "wb") as target:
        shutil.copyfileobj(source, target)
    shutil.copystat(path, backup)
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
        # Created with its final mode, so secrets are never readable by others,
        # not even between the write and a later chmod.
        with _open_new_file(
            temporary,
            "w",
            mode=0o600 if restrict_permissions else 0o666,
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _open_new_file(path: Path, open_mode: str, *, mode: int = 0o600, **kwargs):
    """Create and open a file that must not exist yet, with permission ``mode``."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        return os.fdopen(descriptor, open_mode, **kwargs)
    except BaseException:
        os.close(descriptor)
        raise


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def fsync_directory(path: Path) -> None:
    """Make renames and new entries in ``path`` durable (no-op on Windows)."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)
