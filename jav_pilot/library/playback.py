"""Read indexed local videos without accepting client-provided filesystem paths."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from ..config.paths import default_database_path
from .filesystem import is_linklike, join_relative, regular_root, require_within_root
from .models import MutableMetrics, VIDEO_SUFFIXES


@contextmanager
def open_video(root: Path, relative: str):
    if Path(relative).suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError("Not an indexed video")
    with open_library_file(root, relative) as opened:
        yield opened


@contextmanager
def open_library_file(root: Path, relative: str):
    root, _ = regular_root(root, MutableMetrics())
    target = join_relative(root, relative)
    # On NAS, walk from an open directory descriptor: no component can be
    # replaced by a symlink between validation and opening the file.
    if os.open in os.supports_dir_fd:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = target.relative_to(root).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            media_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        stream = os.fdopen(media_fd, "rb")
    else:
        regular_root(target.parent, MutableMetrics())
        require_within_root(target, root, directory=False)
        before = target.lstat()
        if is_linklike(before) or not stat.S_ISREG(before.st_mode):
            raise ValueError("Unsafe media file")
        stream = target.open("rb")
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            stream.close()
            raise ValueError("Media file changed")
    with stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError("Media file is unavailable")
        identity = hashlib.sha256(f"{relative}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}".encode()).hexdigest()
        yield stream, info.st_size, identity


def byte_range(value: str | None, size: int) -> tuple[int, int]:
    if not value:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        raise ValueError("Invalid byte range")
    first, last = match.groups()
    if not first:
        length = int(last)
        if length <= 0:
            raise ValueError("Invalid suffix range")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, end


@contextmanager
def progress_db():
    path = default_database_path("playback.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS progress (entry_id TEXT NOT NULL, identity TEXT NOT NULL, position REAL NOT NULL, PRIMARY KEY(entry_id,identity))")
            yield db
    finally:
        db.close()


def read_progress(entry_id: str, identity: str) -> float:
    with progress_db() as db:
        row = db.execute("SELECT position FROM progress WHERE entry_id=? AND identity=?", (entry_id, identity)).fetchone()
    return float(row[0]) if row else 0.0


def save_progress(entry_id: str, identity: str, position: float) -> None:
    with progress_db() as db:
        db.execute("INSERT INTO progress VALUES(?,?,?) ON CONFLICT(entry_id,identity) DO UPDATE SET position=excluded.position", (entry_id, identity, position))
