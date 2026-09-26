"""Cleanup of job artifacts, staging files, browser profiles and archive directories."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Sequence

from ..library.archive import plan_archive_layout
from .artifacts import replacement_artifact_name
from .config import WebDownloadConfig
from .errors import WebDownloadConfigError, WebDownloadError, WebDownloadRunnerError
from .jobs import (
    JOB_ID_RE,
    MISSAV_BROWSER_PROFILE_RE,
    normalize_web_download_code,
    validate_relative_output_path,
)

def retained_staging_bytes(
    staging_root: Path,
    job_id: str,
    *,
    max_entries: int,
    max_bytes: int,
) -> int:
    clean_job_id = str(job_id or "").strip().lower()
    if not JOB_ID_RE.fullmatch(clean_job_id):
        raise WebDownloadRunnerError("web download checkpoint path is invalid")
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
    ):
        raise WebDownloadRunnerError("web download checkpoint limit is invalid")

    try:
        if not staging_root.exists():
            return 0
        if path_is_linklike(staging_root) or not staging_root.is_dir():
            raise WebDownloadRunnerError("web download checkpoint root is unsafe")
        root = staging_root.resolve(strict=True)
        job_path = root / clean_job_id
        try:
            job_stat = job_path.lstat()
        except FileNotFoundError:
            return 0
        if path_is_linklike(job_path) or not stat.S_ISDIR(job_stat.st_mode):
            raise WebDownloadRunnerError("web download checkpoint path is unsafe")

        entry_count = 0
        total_bytes = 0
        pending = [job_path]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > max_entries:
                        raise WebDownloadRunnerError(
                            "web download checkpoint exceeds its safe entry limit"
                        )
                    if entry.is_symlink() or _entry_is_junction(entry):
                        raise WebDownloadRunnerError(
                            "web download checkpoint contains an unsafe link"
                        )
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(Path(entry.path))
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode):
                        raise WebDownloadRunnerError(
                            "web download checkpoint contains an unsafe entry"
                        )
                    if entry_stat.st_size < 0:
                        raise WebDownloadRunnerError(
                            "web download checkpoint contains an invalid file"
                        )
                    total_bytes += entry_stat.st_size
                    if total_bytes > max_bytes:
                        raise WebDownloadRunnerError(
                            "web download checkpoint exceeds its safe size limit"
                        )
        return total_bytes
    except WebDownloadRunnerError:
        raise
    except OSError:
        raise WebDownloadRunnerError(
            "web download checkpoint could not be inspected safely"
        ) from None


def path_is_linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _entry_is_junction(entry: os.DirEntry[str]) -> bool:
    is_junction = getattr(entry, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def cleanup_orphan_browser_profiles(
    staging_root: Path,
    managed_job_ids: Sequence[str],
) -> int:
    """Remove profiles left by interrupted managed workers before recovery."""

    try:
        job_ids = frozenset(
            job_id for job_id in managed_job_ids if JOB_ID_RE.fullmatch(job_id)
        )
        if not job_ids:
            return 0
        if path_is_linklike(staging_root):
            raise WebDownloadConfigError("web download staging path is unsafe")
        if not staging_root.exists():
            return 0
        root_stat = staging_root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise WebDownloadConfigError("web download staging path is unsafe")
        resolved_root = staging_root.resolve(strict=True)
        if resolved_root != staging_root.absolute():
            raise WebDownloadConfigError("web download staging path is unsafe")

        cleaned = 0
        with os.scandir(resolved_root) as jobs:
            for job in jobs:
                if job.name not in job_ids:
                    continue
                if job.is_symlink() or _entry_is_junction(job):
                    continue
                job_path = Path(job.path)
                job_stat = job_path.lstat()
                if not stat.S_ISDIR(job_stat.st_mode):
                    continue
                if job_path.resolve(strict=True) != job_path.absolute():
                    continue

                with os.scandir(job_path) as entries:
                    profiles = tuple(
                        entry
                        for entry in entries
                        if MISSAV_BROWSER_PROFILE_RE.fullmatch(entry.name)
                    )
                for profile in profiles:
                    _remove_orphan_browser_profile(
                        Path(profile.path),
                        profile,
                        job_path,
                        (job_stat.st_dev, job_stat.st_ino),
                    )
                    cleaned += 1
        return cleaned
    except WebDownloadConfigError:
        raise
    except OSError:
        raise WebDownloadConfigError(
            "orphaned MissAV browser profiles could not be cleaned safely"
        ) from None


def _remove_orphan_browser_profile(
    profile_path: Path,
    profile: os.DirEntry[str],
    job_path: Path,
    job_identity: tuple[int, int],
) -> None:
    current_job_stat = job_path.lstat()
    if (
        path_is_linklike(job_path)
        or not stat.S_ISDIR(current_job_stat.st_mode)
        or (current_job_stat.st_dev, current_job_stat.st_ino) != job_identity
    ):
        raise WebDownloadConfigError(
            "orphaned MissAV browser profiles could not be cleaned safely"
        )

    if profile.is_symlink():
        profile_path.unlink()
    elif _entry_is_junction(profile):
        os.rmdir(profile_path)
    else:
        profile_stat = profile.stat(follow_symlinks=False)
        if not stat.S_ISDIR(profile_stat.st_mode):
            return
        if profile_path.resolve(strict=True) != profile_path.absolute():
            raise WebDownloadConfigError(
                "orphaned MissAV browser profiles could not be cleaned safely"
            )
        shutil.rmtree(profile_path)

    if os.path.lexists(profile_path):
        raise WebDownloadConfigError(
            "orphaned MissAV browser profiles could not be cleaned safely"
        )


def cleanup_job_artifacts(
    config: WebDownloadConfig,
    job_id: str,
    code: str,
    *archive_paths: object,
) -> bool:
    return _cleanup_job_paths(
        Path(config.staging_path),
        Path(config.library_path),
        job_id,
        code,
        archive_paths,
    )


def _cleanup_job_paths(
    staging_root: Path,
    library_root: Path,
    job_id: str,
    code: str,
    archive_paths: Sequence[object] = (),
) -> bool:
    clean_job_id = str(job_id or "").strip().lower()
    if not JOB_ID_RE.fullmatch(clean_job_id):
        return False
    try:
        display_code, _ = normalize_web_download_code(code)
    except WebDownloadError:
        return False

    staging = _regular_directory(staging_root)
    library = _regular_directory(library_root)
    if staging is None or library is None:
        return False

    try:
        layout = plan_archive_layout(
            code=display_code,
            title=None,
            release_date=None,
            suffix=".mp4",
        )
    except ValueError:
        return False
    planned_path = library.joinpath(*layout.relative_directory.parts)
    cleanup_targets: dict[str, tuple[Path, set[str]]] = {}

    def add_cleanup_target(directory: Path, target_name: str) -> None:
        key = os.path.normcase(str(directory.absolute()))
        current = cleanup_targets.get(key)
        if current is None:
            cleanup_targets[key] = (directory, {target_name})
        else:
            current[1].add(target_name)

    add_cleanup_target(planned_path, layout.filename)
    add_cleanup_target(library / display_code, f"{display_code}.mp4")
    for value in archive_paths:
        if value in (None, ""):
            continue
        try:
            relative_path = PurePosixPath(validate_relative_output_path(value))
            relative = relative_path.parent
        except WebDownloadError:
            return False
        add_cleanup_target(
            (
                library
                if relative == PurePosixPath(".")
                else library.joinpath(*relative.parts)
            ),
            relative_path.name,
        )
    for path, _target_names in cleanup_targets.values():
        try:
            path.absolute().relative_to(library.absolute())
        except ValueError:
            return False
    if not all(
        _archive_cleanup_directory_is_safe(path, library)
        for path, _target_names in cleanup_targets.values()
    ):
        return False
    if not all(
        _cleanup_archive_directory(
            path,
            clean_job_id,
            target_names=target_names,
            apply=False,
        )
        for path, target_names in cleanup_targets.values()
    ):
        return False

    job_path = staging / clean_job_id
    try:
        if path_is_linklike(job_path):
            return False
        if job_path.is_file():
            return False
        if job_path.is_dir():
            if job_path.resolve(strict=True) != job_path.absolute():
                return False
            shutil.rmtree(job_path)
        elif job_path.exists():
            return False
    except OSError:
        return False
    if job_path.exists() or path_is_linklike(job_path):
        return False

    return all(
        _cleanup_archive_directory(
            path,
            clean_job_id,
            target_names=target_names,
        )
        for path, target_names in cleanup_targets.values()
    )


def _archive_cleanup_directory_is_safe(path: Path, library_root: Path) -> bool:
    try:
        relative = path.absolute().relative_to(library_root.absolute())
    except ValueError:
        return False
    current = library_root
    for part in relative.parts:
        current /= part
        if path_is_linklike(current):
            return False
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISDIR(current_stat.st_mode):
            return False
    try:
        return path.resolve(strict=True) == path.absolute()
    except OSError:
        return False


def _cleanup_archive_directory(
    code_path: Path,
    clean_job_id: str,
    *,
    target_names: set[str],
    apply: bool = True,
) -> bool:
    if path_is_linklike(code_path):
        return False
    if not code_path.exists():
        return True
    if not code_path.is_dir():
        return False
    try:
        if code_path.resolve(strict=True) != code_path.absolute():
            return False
    except OSError:
        return False
    try:
        entries = tuple(code_path.iterdir())
    except OSError:
        return False
    entries_by_name = {entry.name: entry for entry in entries}
    replacements: dict[str, dict[str, Path]] = {}
    expected_names: set[str] = set()
    for target_name in target_names:
        for artifact_kind in ("part", "backup", "failed"):
            artifact_name = replacement_artifact_name(
                target_name,
                clean_job_id,
                artifact_kind,
            )
            expected_names.add(artifact_name)
            entry = entries_by_name.get(artifact_name)
            if entry is None:
                continue
            try:
                if path_is_linklike(entry) or not entry.is_file():
                    return False
                if artifact_kind == "part":
                    if apply:
                        entry.unlink()
                    continue
            except OSError:
                return False
            replacements.setdefault(target_name, {})[artifact_kind] = entry
    for entry in entries:
        if entry.name.startswith(f".jav-pilot-{clean_job_id}.") and (
            entry.name not in expected_names
        ):
            return False

    for target_name, artifacts in replacements.items():
        target = code_path / target_name
        backup = artifacts.get("backup")
        failed = artifacts.get("failed")
        try:
            for artifact in artifacts.values():
                if path_is_linklike(artifact) or not artifact.is_file():
                    return False
            if path_is_linklike(target) or (target.exists() and not target.is_file()):
                return False
            if not apply:
                if backup is None and failed is not None and not target.exists():
                    return False
                continue
            if backup is not None:
                if not target.exists():
                    os.replace(backup, target)
                elif target.stat().st_size <= 0:
                    if failed is not None:
                        failed.unlink()
                        failed = None
                    failed_target = code_path / replacement_artifact_name(
                        target.name,
                        clean_job_id,
                        "failed",
                    )
                    os.replace(target, failed_target)
                    os.replace(backup, target)
                    failed = failed_target
                else:
                    backup.unlink()
            if failed is not None:
                if not target.exists():
                    return False
                failed.unlink()
            _fsync_directory(code_path)
        except OSError:
            return False
    return True


def _fsync_directory(path: Path) -> None:
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


def _regular_directory(path: Path) -> Path | None:
    try:
        if path_is_linklike(path) or not path.is_dir():
            return None
        resolved = path.resolve(strict=True)
        return resolved if resolved == path.absolute() else None
    except OSError:
        return None
