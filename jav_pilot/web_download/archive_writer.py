"""Moving verified videos into the library archive, including replacements."""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..library.archive import ArchiveLayout, plan_archive_layout
from .artifacts import replacement_artifact_name
from .variant import DEFAULT_WEB_DOWNLOAD_VARIANT, normalize_web_download_variant
from .worker_common import COPY_CHUNK_BYTES, VIDEO_SUFFIXES, raise_if_cancelled
from .worker_errors import WebDownloadWorkerError
from .worker_files import (
    create_child_directory,
    fsync_directory,
    path_is_linklike,
    require_safe_child,
)
from .worker_task import JsonEventEmitter

@dataclass(frozen=True, slots=True)
class _ArchiveTarget:
    path: Path
    identity: tuple[int, int, int, int] | None


def resolve_policy_archive_target(
    library_root: Path,
    code: str,
    job_id: str,
    incumbent_output_path: str | None,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> _ArchiveTarget:
    if incumbent_output_path is not None:
        target = library_root.joinpath(*PurePosixPath(incumbent_output_path).parts)
    else:
        layout = _provisional_archive_layout(code, ".mp4", variant=variant)
        target = library_root.joinpath(*layout.relative_media_path.parts)
    require_safe_child(target, library_root)
    if target.suffix.casefold() != ".mp4":
        raise WebDownloadWorkerError("existing media format cannot be replaced safely")
    _recover_archive_replacement(target, library_root, job_id)
    try:
        identity = _regular_archive_identity(target, library_root)
    except FileNotFoundError:
        identity = None
    return _ArchiveTarget(path=target, identity=identity)


def _replacement_artifact_path(
    target: Path,
    library_root: Path,
    job_id: str,
    suffix: str,
) -> Path:
    artifact = target.parent / replacement_artifact_name(
        target.name,
        job_id,
        suffix,
    )
    require_safe_child(artifact, library_root)
    return artifact


def _recover_archive_replacement(
    target: Path,
    library_root: Path,
    job_id: str,
) -> None:
    backup = _replacement_artifact_path(target, library_root, job_id, "backup")
    failed = _replacement_artifact_path(target, library_root, job_id, "failed")
    backup_present = backup.exists() or backup.is_symlink()
    failed_present = failed.exists() or failed.is_symlink()
    if not backup_present and not failed_present:
        return
    if backup.is_symlink() or (backup_present and not backup.is_file()):
        raise WebDownloadWorkerError("unsafe archive replacement backup")
    if failed.is_symlink() or (failed_present and not failed.is_file()):
        raise WebDownloadWorkerError("unsafe failed archive replacement")

    target_present = target.exists() or target.is_symlink()
    if target_present:
        target_identity = _regular_archive_identity(target, library_root)
        if target_identity[2] <= 0:
            if not backup_present:
                raise WebDownloadWorkerError(
                    "unfinished archive replacement is invalid"
                )
            if failed_present:
                failed.unlink()
            os.replace(target, failed)
            fsync_directory(target.parent)
            os.replace(backup, target)
            fsync_directory(target.parent)
            failed.unlink()
            fsync_directory(target.parent)
            return
        if backup_present:
            backup.unlink()
        if failed_present:
            failed.unlink()
        fsync_directory(target.parent)
        return

    if not backup_present:
        raise WebDownloadWorkerError("unfinished archive replacement requires recovery")
    os.replace(backup, target)
    fsync_directory(target.parent)
    if failed_present:
        failed.unlink()
        fsync_directory(target.parent)


def _regular_archive_identity(
    path: Path,
    library_root: Path,
) -> tuple[int, int, int, int]:
    require_safe_child(path, library_root)
    file_stat = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise WebDownloadWorkerError("existing archive is not a regular file")
    resolved = path.resolve(strict=True)
    resolved.relative_to(library_root)
    if resolved != path.absolute():
        raise WebDownloadWorkerError("existing archive path is unsafe")
    verified_stat = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(verified_stat.st_mode)
        or (verified_stat.st_dev, verified_stat.st_ino)
        != (file_stat.st_dev, file_stat.st_ino)
    ):
        raise WebDownloadWorkerError("existing archive changed during inspection")
    return (
        verified_stat.st_dev,
        verified_stat.st_ino,
        verified_stat.st_size,
        verified_stat.st_mtime_ns,
    )


def archive_video(
    source: Path,
    library_root: Path,
    code: str,
    job_id: str,
    emitter: JsonEventEmitter,
    cancel_event: threading.Event,
    *,
    replace_target: _ArchiveTarget | None = None,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> Path:
    suffix = (
        source.suffix.lower() if source.suffix.lower() in VIDEO_SUFFIXES else ".mp4"
    )
    if replace_target is not None:
        target = replace_target.path
        if target.suffix.casefold() != suffix.casefold():
            raise WebDownloadWorkerError(
                "existing media format cannot be replaced safely"
            )
        target_dir = target.parent
    else:
        layout = _provisional_archive_layout(code, suffix, variant=variant)
        target = library_root.joinpath(*layout.relative_media_path.parts)
        target_dir = target.parent
    _prepare_archive_directory(target_dir, library_root)
    if replace_target is None and (target.exists() or path_is_linklike(target)):
        target = target_dir / f"{target.stem}-{job_id[:8]}{suffix}"
        if target.exists() or path_is_linklike(target):
            raise WebDownloadWorkerError("archive collision target is occupied")
    require_safe_child(target, library_root)
    temporary = _replacement_artifact_path(target, library_root, job_id, "part")
    backup = _replacement_artifact_path(target, library_root, job_id, "backup")
    require_safe_child(temporary, library_root)
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise WebDownloadWorkerError("unsafe archive temporary path")
        temporary.unlink()
    _recover_archive_replacement(target, library_root, job_id)

    total = source.stat().st_size
    copied = 0
    published = False
    old_backed_up = False
    emitter.progress(
        status="archiving",
        progress=96.0,
        downloaded_bytes=0,
        total_bytes=total,
        force=True,
    )
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while True:
                raise_if_cancelled(cancel_event)
                chunk = reader.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                writer.write(chunk)
                copied += len(chunk)
                fraction = copied / total if total else 1.0
                emitter.progress(
                    status="archiving",
                    progress=96.0 + (3.0 * min(fraction, 1.0)),
                    downloaded_bytes=copied,
                    total_bytes=total,
                )
            writer.flush()
            os.fsync(writer.fileno())
        if copied != total:
            raise WebDownloadWorkerError("archived media size did not match source")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        if replace_target is not None:
            try:
                current_identity = _regular_archive_identity(target, library_root)
            except FileNotFoundError:
                current_identity = None
            if current_identity != replace_target.identity:
                raise WebDownloadWorkerError(
                    "existing archive changed before replacement"
                )
            if current_identity is not None:
                os.replace(target, backup)
                old_backed_up = True
                fsync_directory(target_dir)
        os.replace(temporary, target)
        published = True
        fsync_directory(target_dir)
        if old_backed_up:
            try:
                backup.unlink()
                fsync_directory(target_dir)
            except OSError:
                # The verified new target is durable; retain the old file as a
                # recoverable backup rather than turning success into data loss.
                pass
        return target
    except BaseException:
        if temporary.exists() and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        if (
            old_backed_up
            and backup.exists()
            and backup.is_file()
            and not backup.is_symlink()
        ):
            try:
                if target.exists() and target.is_file() and not target.is_symlink():
                    failed_new = _replacement_artifact_path(
                        target, library_root, job_id, "failed"
                    )
                    os.replace(target, failed_new)
                    os.replace(backup, target)
                    if failed_new.exists() and failed_new.is_file():
                        failed_new.unlink()
                elif not target.exists():
                    os.replace(backup, target)
                fsync_directory(target_dir)
            except OSError:
                pass
        elif (
            published
            and replace_target is None
            and target.exists()
            and target.is_file()
            and not target.is_symlink()
        ):
            target.unlink()
            try:
                fsync_directory(target_dir)
            except OSError:
                pass
        raise


def _provisional_archive_layout(
    code: str,
    suffix: str,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> ArchiveLayout:
    try:
        return plan_archive_layout(
            code=code,
            variant=normalize_web_download_variant(variant),
            title=None,
            release_date=None,
            suffix=suffix,
        )
    except ValueError as exc:
        raise WebDownloadWorkerError("archive layout is invalid") from exc


def _prepare_archive_directory(path: Path, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise WebDownloadWorkerError("archive directory escapes its root") from exc
    if not relative.parts:
        if path_is_linklike(root) or not root.is_dir():
            raise WebDownloadWorkerError("archive root is unsafe")
        return
    current = root
    for part in relative.parts:
        current /= part
        create_child_directory(current, root)
