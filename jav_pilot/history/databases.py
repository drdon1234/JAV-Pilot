"""Database files, backups and in-progress checks consulted by history maintenance."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .errors import HistoryLifecycleConflictError, HistoryLifecycleValidationError
from .models import DIGEST_RE, GIT_REVISION_RE

def review_in_progress(
    connection: sqlite3.Connection, metadata_row: sqlite3.Row
) -> bool:
    if not table_exists(connection, "media_metadata_reviews"):
        return False
    row = connection.execute(
        "SELECT * FROM media_metadata_reviews "
        "WHERE code_key = ? AND relative_media_path = ?",
        (metadata_row["code_key"], metadata_row["relative_media_path"]),
    ).fetchone()
    if row is None:
        return False
    return _review_row_in_progress(connection, "", row)


def review_in_progress_attached(
    connection: sqlite3.Connection, schema: str, metadata_row: sqlite3.Row
) -> bool:
    table = connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master "
        "WHERE type = 'table' AND name = 'media_metadata_reviews'"
    ).fetchone()
    if table is None:
        return False
    row = connection.execute(
        f"SELECT * FROM {schema}.media_metadata_reviews "
        "WHERE code_key = ? AND relative_media_path = ?",
        (metadata_row["code_key"], metadata_row["relative_media_path"]),
    ).fetchone()
    if row is None:
        return False
    return _review_row_in_progress(connection, f"{schema}.", row)


def _review_row_in_progress(
    connection: sqlite3.Connection, prefix: str, row: sqlite3.Row
) -> bool:
    review_id = str(row["review_id"])
    revision = int(row["revision"])
    active_refetch = connection.execute(
        f"SELECT 1 FROM {prefix}media_metadata_review_refetch "
        "WHERE review_id = ? AND status IN ('queued', 'running') LIMIT 1",
        (review_id,),
    ).fetchone()
    if active_refetch is not None:
        return True
    if "abandoned_revision" in row.keys():
        abandoned_revision = row["abandoned_revision"]
        if abandoned_revision is not None and int(abandoned_revision) == revision:
            return False
    publication = connection.execute(
        f"SELECT MAX(review_revision) FROM {prefix}media_metadata_review_publications "
        "WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    published_revision = (
        int(publication[0])
        if publication is not None and publication[0] is not None
        else 0
    )
    return published_revision < revision


def asset_status_counts(raw: object) -> dict[str, int]:
    try:
        assets = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {"invalid": 1}
    if not isinstance(assets, dict):
        return {"invalid": 1}
    counts: Counter[str] = Counter()
    for value in assets.values():
        status_value = value.get("status") if isinstance(value, dict) else None
        if status_value in {"generated", "existing", "missing", "failed"}:
            counts[str(status_value)] += 1
        else:
            counts["other"] += 1
    return dict(sorted(counts.items()))


def require_backup_database(
    manifest: Mapping[str, object],
    database: Path,
    *,
    expected_revision: str | None,
) -> None:
    if not isinstance(manifest, Mapping):
        raise HistoryLifecycleConflictError("verified backup manifest is invalid")
    if expected_revision is None or manifest.get("revision") != expected_revision:
        raise HistoryLifecycleConflictError(
            "verified backup revision does not match the running release"
        )
    entries = manifest.get("entries")
    sqlite_checks = manifest.get("sqlite")
    if not isinstance(entries, list) or not isinstance(sqlite_checks, Mapping):
        raise HistoryLifecycleConflictError("verified backup manifest is invalid")
    basename = database.name
    expected_names = {basename}
    wal = database.with_name(f"{basename}-wal")
    if wal.exists() or wal.is_symlink():
        expected_names.add(wal.name)
    matching: dict[str, tuple[str, int, str]] = {}
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        normalized = raw_path.replace("\\", "/")
        name = PurePosixPath(normalized).name
        if name not in {basename, f"{basename}-wal"}:
            continue
        if normalized != f"data/{name}":
            continue
        size = item.get("size")
        digest = item.get("sha256")
        if (
            name in matching
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or DIGEST_RE.fullmatch(digest) is None
        ):
            raise HistoryLifecycleConflictError(
                "verified backup database binding is invalid"
            )
        matching[name] = (normalized, size, digest)
    if basename not in matching:
        raise HistoryLifecycleConflictError(
            "verified backup does not contain the vacuum database"
        )
    main_path = matching[basename][0]
    if (
        main_path not in sqlite_checks
        or not isinstance(sqlite_checks[main_path], Mapping)
        or sqlite_checks[main_path].get("integrity_check") != "ok"
    ):
        raise HistoryLifecycleConflictError(
            "verified backup database integrity is unavailable"
        )
    if set(matching) != expected_names:
        raise HistoryLifecycleConflictError(
            "verified backup database does not match the current database"
        )
    for name, (_relative, expected_size, expected_digest) in matching.items():
        current = database if name == basename else wal
        size, digest = _stable_file_checksum(current)
        if size != expected_size or not secrets.compare_digest(digest, expected_digest):
            raise HistoryLifecycleConflictError(
                "verified backup database does not match the current database"
            )


def current_runtime_revision(value: object | None) -> str | None:
    raw = os.environ.get("JAV_PILOT_REVISION", "") if value is None else value
    clean = str(raw or "").strip()
    return clean if GIT_REVISION_RE.fullmatch(clean) is not None else None


def _stable_file_checksum(path: Path) -> tuple[int, str]:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise HistoryLifecycleConflictError(
            "current vacuum database binding is unavailable"
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise HistoryLifecycleConflictError(
            "current vacuum database binding is unavailable"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HistoryLifecycleConflictError(
            "current vacuum database binding is unavailable"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise HistoryLifecycleConflictError(
                "current vacuum database binding changed during verification"
            )
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise HistoryLifecycleConflictError(
            "current vacuum database binding is unavailable"
        ) from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or (os.name != "nt" and after.st_ctime_ns != before.st_ctime_ns)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise HistoryLifecycleConflictError(
            "current vacuum database binding changed during verification"
        )
    return int(after.st_size), digest.hexdigest()


def integrity_check(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise HistoryLifecycleConflictError("SQLite integrity check failed")
    return "ok"


def database_path(value: Path | str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise HistoryLifecycleValidationError(f"{label} database path must be absolute")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise HistoryLifecycleValidationError(
            f"{label} database is unavailable"
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise HistoryLifecycleValidationError(f"{label} database is unsafe")
    return path.resolve(strict=True)


def path_identity(path: Path) -> tuple[int, int, int]:
    info = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise HistoryLifecycleConflictError("history database is unsafe")
    return int(info.st_dev), int(info.st_ino), int(info.st_mode)


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )
