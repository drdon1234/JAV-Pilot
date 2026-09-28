"""Presence and size checks for archived web download files."""

from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath
from typing import Sequence

from .cleanup import path_is_linklike
from .errors import WebDownloadRunnerError
from .job_store import WebDownloadStore
from .jobs import (
    ARCHIVE_AVAILABLE,
    ARCHIVE_MISSING,
    ARCHIVE_REPLACED,
    ARCHIVE_UNKNOWN,
    validate_relative_output_path,
)

def with_archive_statuses(
    jobs: Sequence[dict[str, object]], library_root: Path
) -> list[dict[str, object]]:
    public_jobs = [dict(job) for job in jobs]
    completed = [
        job
        for job in public_jobs
        if job.get("status") == "completed" and job.get("superseded_by_job_id") is None
    ]
    root_snapshot = archive_root_snapshot(library_root) if completed else None

    for job in public_jobs:
        archive_status = ARCHIVE_UNKNOWN
        if job.get("superseded_by_job_id") is not None:
            archive_status = ARCHIVE_REPLACED
        elif job.get("status") == "completed" and root_snapshot is not None:
            archive_status = archived_file_probe_in_root(
                root_snapshot[0], job.get("output_path")
            )[0]
        job["archive_status"] = archive_status

    if (
        completed
        and root_snapshot is not None
        and not archive_root_matches(library_root, root_snapshot)
    ):
        for job in completed:
            job["archive_status"] = ARCHIVE_UNKNOWN
    return public_jobs


def reconcile_completed_archives(store: WebDownloadStore, library_root: Path) -> None:
    for candidate in store.completed_archive_candidates():
        output_path = candidate["output_path"]
        size = _archived_file_size(library_root, output_path)
        if size is None:
            continue
        store.reconcile_completed_size(candidate["job_id"], output_path, size)


def _archived_file_size(library_root: Path, output_path: object) -> int | None:
    return _archived_file_probe(library_root, output_path)[1]


def _archived_file_probe(
    library_root: Path, output_path: object
) -> tuple[str, int | None]:
    root_snapshot = archive_root_snapshot(library_root)
    if root_snapshot is None:
        return ARCHIVE_UNKNOWN, None
    result = archived_file_probe_in_root(root_snapshot[0], output_path)
    if not archive_root_matches(library_root, root_snapshot):
        return ARCHIVE_UNKNOWN, None
    return result


def archived_file_probe_in_root(
    resolved_root: Path, output_path: object
) -> tuple[str, int | None]:
    try:
        clean_path = validate_relative_output_path(output_path)
    except WebDownloadRunnerError:
        return ARCHIVE_UNKNOWN, None

    parts = PurePosixPath(clean_path).parts
    parent = resolved_root
    try:
        for part in parts[:-1]:
            parent = parent / part
            try:
                parent_stat = parent.lstat()
            except FileNotFoundError:
                return ARCHIVE_MISSING, None
            if path_is_linklike(parent) or not stat.S_ISDIR(parent_stat.st_mode):
                return ARCHIVE_UNKNOWN, None
        archived = parent / parts[-1]
        try:
            archived_stat = archived.lstat()
        except FileNotFoundError:
            return ARCHIVE_MISSING, None
        if path_is_linklike(archived) or not stat.S_ISREG(archived_stat.st_mode):
            return ARCHIVE_UNKNOWN, None
        resolved = archived.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if resolved != archived.absolute():
            return ARCHIVE_UNKNOWN, None
        verified_stat = archived.lstat()
        if (
            path_is_linklike(archived)
            or not stat.S_ISREG(verified_stat.st_mode)
            or (verified_stat.st_dev, verified_stat.st_ino)
            != (archived_stat.st_dev, archived_stat.st_ino)
        ):
            return ARCHIVE_UNKNOWN, None
        return ARCHIVE_AVAILABLE, verified_stat.st_size
    except FileNotFoundError:
        return ARCHIVE_MISSING, None
    except (OSError, ValueError):
        return ARCHIVE_UNKNOWN, None


def archive_root_snapshot(library_root: Path) -> tuple[Path, tuple[int, int]] | None:
    try:
        root_stat = library_root.lstat()
        if path_is_linklike(library_root) or not stat.S_ISDIR(root_stat.st_mode):
            return None
        root = library_root.resolve(strict=True)
        if root != library_root.absolute():
            return None
        verified_stat = library_root.lstat()
    except OSError:
        return None
    if (
        path_is_linklike(library_root)
        or not stat.S_ISDIR(verified_stat.st_mode)
        or (verified_stat.st_dev, verified_stat.st_ino)
        != (root_stat.st_dev, root_stat.st_ino)
    ):
        return None
    return root, (verified_stat.st_dev, verified_stat.st_ino)


def archive_root_matches(
    library_root: Path,
    snapshot: tuple[Path, tuple[int, int]],
) -> bool:
    current = archive_root_snapshot(library_root)
    return current is not None and current == snapshot


def stable_archived_file_status(
    library_root: Path,
    root_snapshot: tuple[Path, tuple[int, int]],
    output_path: object,
) -> str:
    if not archive_root_matches(library_root, root_snapshot):
        return ARCHIVE_UNKNOWN
    status_value = archived_file_probe_in_root(root_snapshot[0], output_path)[0]
    if not archive_root_matches(library_root, root_snapshot):
        return ARCHIVE_UNKNOWN
    return status_value
