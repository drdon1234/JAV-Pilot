"""Explicit read-only mounts. Browsing never hands a mount to the library indexer."""

import json
import os
import re
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

from .filesystem import regular_root, safe_relative_path
from .models import MutableMetrics, VIDEO_SUFFIXES


def mounts():
    value = json.loads(os.getenv("JAV_PILOT_BROWSE_MOUNTS", "{}"))
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("挂载配置无效")
    result = {}
    for name, path in value.items():
        if (
            not re.fullmatch(r"[\w .-]{1,80}", name)
            or not isinstance(path, str)
            or not Path(path).is_absolute()
        ):
            raise ValueError("挂载名称或路径无效")
        result[name] = Path(path)
    return result


def mount_root(name):
    root = mounts().get(name)
    if root is None:
        raise ValueError("存储来源不存在")
    return root


@contextmanager
def directory(root, relative):
    root, _ = regular_root(root, MutableMetrics())
    parts = Path(safe_relative_path(relative, allow_root=True)).parts
    if os.open in os.supports_dir_fd and os.scandir in os.supports_fd:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(root, flags)
        try:
            for part in parts:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)
    else:
        target, _ = regular_root(root.joinpath(*parts), MutableMetrics())
        yield target


def browse(name, path, offset):
    if not 0 <= offset <= 100000:
        raise ValueError("目录页码无效")
    with directory(mount_root(name), path) as target, os.scandir(target) as entries:
        rows = []
        consumed = 0
        for entry in islice(entries, offset, offset + 101):
            consumed += 1
            if consumed == 101:
                break
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            folder = entry.is_dir(follow_symlinks=False)
            if (
                folder
                or Path(entry.name).suffix.lower() in VIDEO_SUFFIXES
                and entry.is_file(follow_symlinks=False)
            ):
                rows.append({"name": entry.name, "folder": folder})
    return {"items": rows, "next": offset + 100 if consumed > 100 else None}
