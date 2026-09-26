from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable

from ..config.app_config import AppConfig, QbittorrentConfig
from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from .qbittorrent import DownloaderError, DownloaderHttpError, QbittorrentClient
from ..library.archive import archive_category
from ..media_metadata.publish import VIDEO_SUFFIXES
from ..config.qb_paths import (
    QbPathError,
    is_at_or_below,
    normalize_qb_path,
    validate_qb_library_mapping,
    validate_qb_roots,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 30.0
PAGE_SIZE = 200
MIN_MAIN_BYTES = 128 * 1024 * 1024
MIN_MAIN_DURATION_SECONDS = 20 * 60.0
MAX_CLEANUP_BYTES = 128 * 1024 * 1024
MAX_CLEANUP_DURATION_SECONDS = 10 * 60.0
MAX_ORGANIZER_FILES = 512
MAX_ORGANIZER_VIDEOS = 64
MAX_ADVERTISEMENT_PROBES = 4
MAX_FFPROBE_OUTPUT_BYTES = 64 * 1024
FFPROBE_TIMEOUT_SECONDS = 4.0
PROBE_BUDGET_SECONDS = 10.0
MAX_SIDECAR_SCAN_ENTRIES = 10_000
REVIEW_CACHE_TTL_SECONDS = 10 * 60.0
ADVERTISEMENT_SIDECAR_SUFFIXES = frozenset({".htm", ".html", ".txt", ".url"})
_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:"
    r"FC2[-_. ]?(?:PPV[-_. ]?)?\d{2,9}"
    r"|[A-Z]{2,12}[-_. ]?\d{2,6}"
    r")(?![A-Z0-9])",
    flags=re.IGNORECASE,
)
_EXPLICIT_ADVERTISEMENT_RE = re.compile(
    r"(?:\u5e7f\u544a|\u5ee3\u544a|(?<![a-z])(?:advertisement|commercial)(?![a-z]))",
    flags=re.IGNORECASE,
)
_MARKETING_DOMAIN_RE = re.compile(
    r"(?<![a-z0-9-])(?:[a-z0-9-]+\.)+"
    r"(?:app|cc|cn|co|com|io|live|me|net|org|top|to|tv|vip|xyz)(?![a-z0-9-])",
    flags=re.IGNORECASE,
)
_MARKETING_TERM_RE = re.compile(
    r"(?:\u6e38\u620f|\u904a\u6232|\u535a\u5f69|\u8d4c\u573a|\u8ced\u5834|\u68cb\u724c|"
    r"(?<![a-z])(?:bet(?:ting)?|casino|games?|poker|slots?)(?![a-z]))",
    flags=re.IGNORECASE,
)
_STREAMING_MARKETING_RE = re.compile(
    r"(?:在线\s*视频|線上\s*影片|即\s*点\s*即\s*播|即\s*點\s*即\s*播|"
    r"更多\s*(?:中文\s*)?影片|中文\s*影片|影片\s*(?:访问|訪問)|"
    r"(?:访问|訪問)\s*(?:网站|網站|影片))",
    flags=re.IGNORECASE,
)


class CompletedDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelocationResult:
    moved: tuple[str, ...]
    failed: dict[str, str]
    skipped: int
    renamed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _VideoCandidate:
    index: int
    name: str
    size: int
    priority: int
    path: Path
    identity: _FileIdentity
    duration: float | None = None


@dataclass(frozen=True)
class _TrackedFile:
    index: int
    name: str
    size: int
    progress: float
    priority: int


@dataclass(frozen=True)
class _TaskGuard:
    category: str
    save_path: str
    content_path: str
    complete: bool
    stage: str
    files: tuple[_TrackedFile, ...]


@dataclass(frozen=True)
class _PreparationResult:
    renamed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    reviewed: bool = False
    ready_to_move: bool = False
    guard: _TaskGuard | None = None
    archive_code: str | None = None
    content_is_file: bool = False
    not_ready_reason: str | None = None


PathResolver = Callable[[PurePosixPath], Path]
DurationProbe = Callable[[Path, Path], float]
FileRemover = Callable[[Path, Path, _FileIdentity], bool]
LibraryChangeObserver = Callable[[], None]


def organize_completed_downloads(
    client: QbittorrentClient,
    config: QbittorrentConfig,
    *,
    expected_app_library_root: str | Path | PurePosixPath | None = None,
    path_resolver: PathResolver | None = None,
    duration_probe: DurationProbe | None = None,
    file_remover: FileRemover | None = None,
    reviewed_hashes: set[str] | None = None,
) -> RelocationResult:
    category = str(config.category or "").strip()
    if not category:
        raise CompletedDownloadError(
            "qBittorrent category is required for completed download organization"
        )
    staging, library, app_library, expected_app_root = _validated_roots(
        config.save_path,
        config.library_path,
        config.app_library_path,
        expected_app_library_root,
    )
    raw_path_resolver = path_resolver or _container_path
    if expected_app_root is not None:
        _validate_resolved_library_mapping(
            expected_app_root,
            app_library,
            raw_path_resolver,
        )
    resolve_path = _mapped_path_resolver(
        library,
        app_library,
        raw_path_resolver,
    )
    probe_duration = duration_probe or _probe_video_duration
    remove_file = file_remover or _safe_unlink_regular
    tasks = _completed_snapshot(client, category)
    moved: list[str] = []
    failed: dict[str, str] = {}
    renamed: list[str] = []
    removed: list[str] = []
    skipped = 0

    for task in tasks:
        info_hash = str(task.get("hash") or "").strip().lower()
        if (
            str(task.get("category") or "") != category
            or not bool(task.get("complete"))
            or str(task.get("stage") or "") == "error"
        ):
            skipped += 1
            continue
        try:
            listed_save_path = PurePosixPath(
                normalize_qb_path(str(task.get("save_path") or ""))
            )
        except QbPathError:
            skipped += 1
            continue
        if not (
            is_at_or_below(listed_save_path, staging)
            or is_at_or_below(listed_save_path, library)
        ):
            skipped += 1
            continue
        try:
            snapshot = client.torrent_snapshot(info_hash)
            if snapshot is None:
                raise CompletedDownloadError(
                    "qBittorrent task is temporarily unavailable"
                )
            if (
                str(snapshot.get("category") or "") != category
                or not bool(snapshot.get("complete"))
                or str(snapshot.get("stage") or "") == "error"
            ):
                skipped += 1
                continue
            current = PurePosixPath(
                normalize_qb_path(str(snapshot.get("save_path") or ""))
            )
            in_staging = is_at_or_below(current, staging)
            in_library = is_at_or_below(current, library)
            if not (in_staging or in_library):
                skipped += 1
                continue
            if (
                in_library
                and reviewed_hashes is not None
                and info_hash in reviewed_hashes
                and _snapshot_has_canonical_archive_location(snapshot, library)
            ):
                skipped += 1
                continue
            prepared = _prepare_completed_task(
                client,
                info_hash=info_hash,
                snapshot=snapshot,
                controlled_root=staging if in_staging else library,
                path_resolver=resolve_path,
                duration_probe=probe_duration,
                file_remover=remove_file,
            )
            renamed.extend(prepared.renamed)
            removed.extend(prepared.removed)
            if not prepared.ready_to_move or prepared.guard is None:
                raise CompletedDownloadError(
                    prepared.not_ready_reason
                    or "qBittorrent file organization state has not settled"
                )
            target_location = _archive_qb_location(library, prepared)
            if in_library and current == target_location:
                if reviewed_hashes is not None and prepared.reviewed:
                    reviewed_hashes.add(info_hash)
                skipped += 1
                continue
            _refresh_task_guard(client, info_hash, prepared.guard)
            client.set_location((info_hash,), target_location.as_posix())
            moved.append(info_hash)
        except (DownloaderError, CompletedDownloadError, QbPathError, OSError) as exc:
            failed[info_hash or "<missing-hash>"] = _safe_organizer_error(exc)

    return RelocationResult(
        moved=tuple(moved),
        failed=failed,
        skipped=skipped,
        renamed=tuple(renamed),
        removed=tuple(removed),
    )


def _prepare_completed_task(
    client: QbittorrentClient,
    *,
    info_hash: str,
    snapshot: dict[str, object],
    controlled_root: PurePosixPath,
    path_resolver: PathResolver,
    duration_probe: DurationProbe,
    file_remover: FileRemover,
) -> _PreparationResult:
    save_path = PurePosixPath(normalize_qb_path(str(snapshot.get("save_path") or "")))
    content_path = PurePosixPath(
        normalize_qb_path(str(snapshot.get("content_path") or ""))
    )
    if not is_at_or_below(save_path, controlled_root):
        raise CompletedDownloadError(
            "qBittorrent save path is outside the controlled root"
        )
    if not is_at_or_below(content_path, save_path):
        raise CompletedDownloadError("qBittorrent content location has not settled")
    if content_path == save_path:
        return _PreparationResult(
            not_ready_reason="qBittorrent content location has not settled"
        )

    files = client.torrent_files(info_hash)
    guard = _task_guard(snapshot, files)
    if not files:
        return _PreparationResult(not_ready_reason="qBittorrent file list is not ready")
    if len(files) > MAX_ORGANIZER_FILES:
        return _PreparationResult(
            reviewed=True,
            guard=guard,
            not_ready_reason="qBittorrent file count exceeds the safe organization budget",
        )
    video_rows = [
        item
        for item in files
        if PurePosixPath(str(item.get("name") or "")).suffix.lower() in VIDEO_SUFFIXES
    ]
    if not video_rows:
        return _PreparationResult(
            reviewed=True,
            guard=guard,
            not_ready_reason="qBittorrent task has no tracked video",
        )
    if len(video_rows) > MAX_ORGANIZER_VIDEOS:
        return _PreparationResult(
            reviewed=True,
            guard=guard,
            not_ready_reason="qBittorrent video count exceeds the safe organization budget",
        )

    local_root = _regular_directory_root(path_resolver(controlled_root))
    local_content = path_resolver(content_path)
    if local_content.is_symlink() or not (
        local_content.is_dir() or local_content.is_file()
    ):
        raise CompletedDownloadError("qBittorrent content directory is unavailable")
    content_is_file = local_content.is_file()
    if content_is_file and (
        len(video_rows) != 1
        or save_path.joinpath(PurePosixPath(str(video_rows[0].get("name") or "")))
        != content_path
    ):
        raise CompletedDownloadError("qBittorrent single-file content is inconsistent")

    videos: list[_VideoCandidate] = []
    for item in video_rows:
        name = str(item.get("name") or "")
        relative = PurePosixPath(name)
        absolute = save_path.joinpath(relative)
        if not is_at_or_below(absolute, content_path):
            raise CompletedDownloadError(
                "qBittorrent video path is outside its content directory"
            )
        index = _nonnegative_int(item.get("index"), "torrent file index")
        size = _positive_int(item.get("size"), "torrent file size")
        priority = _nonnegative_int(item.get("priority"), "torrent file priority")
        progress = _bounded_progress(item.get("progress"))
        local_path = path_resolver(absolute)
        try:
            identity = _regular_file_identity(local_path, local_root)
        except FileNotFoundError:
            if priority == 0:
                continue
            raise CompletedDownloadError(
                "a tracked video file is unavailable"
            ) from None
        if identity.size != size:
            raise CompletedDownloadError("a tracked video file changed unexpectedly")
        if priority > 0 and progress < 1.0:
            return _PreparationResult(
                not_ready_reason="qBittorrent tracked video is not fully downloaded"
            )
        videos.append(
            _VideoCandidate(
                index=index,
                name=name,
                size=size,
                priority=priority,
                path=local_path,
                identity=identity,
            )
        )

    active = sorted(
        (video for video in videos if video.priority > 0),
        key=lambda video: (-video.size, video.name),
    )
    if not active:
        return _PreparationResult(
            not_ready_reason="qBittorrent task has no active completed video"
        )
    main = active[0]
    code = _trusted_catalog_code(
        str(snapshot.get("name") or ""),
        content_path.name,
        PurePosixPath(main.name).stem,
    )
    if code is None:
        return _PreparationResult(
            reviewed=True,
            guard=guard,
            not_ready_reason="qBittorrent task has no trusted catalog code",
        )
    code_key = canonical_catalog_code(code, max_length=40)
    if code_key is None:
        return _PreparationResult(
            reviewed=True,
            guard=guard,
            not_ready_reason="qBittorrent task has no trusted catalog code",
        )

    if not content_is_file:
        guard, videos, content_path = _canonicalize_content_root(
            client,
            info_hash=info_hash,
            guard=guard,
            videos=videos,
            save_path=save_path,
            content_path=content_path,
            code=code,
            local_root=local_root,
            path_resolver=path_resolver,
        )
        active = sorted(
            (video for video in videos if video.priority > 0),
            key=lambda video: (-video.size, video.name),
        )
        main = active[0]

    if main.size < MIN_MAIN_BYTES or (
        len(active) > 1 and active[1].size * 4 > main.size
    ):
        return _PreparationResult(
            reviewed=True,
            ready_to_move=True,
            guard=guard,
            archive_code=code,
            content_is_file=content_is_file,
        )

    target_name = (
        PurePosixPath(main.name).parent
        / f"{code}{PurePosixPath(main.name).suffix.lower()}"
    ).as_posix()
    preserve_main_name = main.name != target_name and _has_old_stem_sidecar(
        main, files
    )

    advertisement_sidecars: list[_VideoCandidate] = []
    if not content_is_file:
        for tracked in guard.files:
            suffix = PurePosixPath(tracked.name).suffix.lower()
            if (
                tracked.index == main.index
                or suffix not in ADVERTISEMENT_SIDECAR_SUFFIXES
                or tracked.priority <= 0
                or tracked.progress < 1.0
                or tracked.size > MAX_CLEANUP_BYTES
                or _catalog_codes(PurePosixPath(tracked.name).stem)
                or not _looks_like_advertisement(
                    PurePosixPath(tracked.name).name
                )
            ):
                continue
            sidecar_path = path_resolver(save_path / PurePosixPath(tracked.name))
            if not is_at_or_below(save_path / PurePosixPath(tracked.name), content_path):
                continue
            try:
                sidecar_identity = _regular_file_identity(sidecar_path, local_root)
            except FileNotFoundError:
                raise CompletedDownloadError(
                    "a tracked advertisement file is unavailable"
                ) from None
            if sidecar_identity.size != tracked.size:
                raise CompletedDownloadError(
                    "a tracked advertisement file changed unexpectedly"
                )
            advertisement_sidecars.append(
                _VideoCandidate(
                    index=tracked.index,
                    name=tracked.name,
                    size=tracked.size,
                    priority=tracked.priority,
                    path=sidecar_path,
                    identity=sidecar_identity,
                )
            )

    all_possible_advertisements = tuple(
        sorted(
            (
                video
                for video in videos
                if video.index != main.index
                and video.size <= MAX_CLEANUP_BYTES
                and video.size * 16 <= main.size
                and not _catalog_codes(PurePosixPath(video.name).stem)
                and _looks_like_advertisement(PurePosixPath(video.name).name)
            ),
            key=lambda video: (video.size, video.name),
        )
    )
    possible_advertisements = all_possible_advertisements[:MAX_ADVERTISEMENT_PROBES]
    probe_deadline = time.monotonic() + PROBE_BUDGET_SECONDS
    if not _probe_slot_available(probe_deadline):
        return _PreparationResult(
            reviewed=False,
            ready_to_move=True,
            guard=guard,
            archive_code=code,
            content_is_file=content_is_file,
        )
    try:
        main_duration = duration_probe(main.path, local_root)
        if not math.isfinite(main_duration) or main_duration <= 0:
            raise CompletedDownloadError("local video inspection was inconclusive")
    except Exception:
        _verify_video_identities(videos, local_root)
        return _PreparationResult(
            reviewed=False,
            ready_to_move=True,
            guard=guard,
            archive_code=code,
            content_is_file=content_is_file,
        )
    main = replace(main, duration=main_duration)
    _verify_video_identities(videos, local_root)
    if main_duration < MIN_MAIN_DURATION_SECONDS:
        return _PreparationResult(
            reviewed=True,
            ready_to_move=True,
            guard=guard,
            archive_code=code,
            content_is_file=content_is_file,
        )

    advertisement_probe_incomplete = (
        len(all_possible_advertisements) > MAX_ADVERTISEMENT_PROBES
    )
    cleanup_candidates: list[_VideoCandidate] = list(advertisement_sidecars)
    for candidate in possible_advertisements:
        if not _probe_slot_available(probe_deadline):
            advertisement_probe_incomplete = True
            break
        try:
            duration = duration_probe(candidate.path, local_root)
            if not math.isfinite(duration) or duration <= 0:
                raise CompletedDownloadError("local video inspection was inconclusive")
        except Exception:
            advertisement_probe_incomplete = True
            continue
        if duration <= MAX_CLEANUP_DURATION_SECONDS and duration * 8 <= main_duration:
            cleanup_candidates.append(replace(candidate, duration=duration))
    _verify_video_identities(videos, local_root)
    preserve_main_name = preserve_main_name or (
        main.name != target_name and _has_old_stem_sidecar(main, files)
    )

    renamed: list[str] = []
    if main.name != target_name and not preserve_main_name:
        if any(
            str(item.get("name") or "") == target_name
            and _nonnegative_int(item.get("index"), "torrent file index") != main.index
            for item in files
        ):
            raise CompletedDownloadError("canonical video target is already occupied")
        target_path = path_resolver(save_path / PurePosixPath(target_name))
        try:
            _regular_file_identity(target_path, local_root)
        except FileNotFoundError:
            pass
        else:
            raise CompletedDownloadError("canonical video target is already occupied")

        guard = _refresh_task_guard(client, info_hash, guard)
        _require_video_identity(main, local_root)
        client.rename_torrent_file(info_hash, main.name, target_name)
        guard = _guard_after_rename(
            client,
            info_hash,
            guard,
            index=main.index,
            old_name=main.name,
            new_name=target_name,
            single_file_content=content_is_file,
        )
        target_identity = _regular_file_identity(target_path, local_root)
        if not _same_file_object(
            target_identity, main.identity
        ) or _path_exists_without_following(main.path):
            raise CompletedDownloadError("qBittorrent file rename was not confirmed")
        main = replace(
            main,
            name=target_name,
            path=target_path,
            identity=target_identity,
        )
        renamed.append(target_name)

    # A confirmed qB rename is the gate for destructive cleanup. If the request
    # timed out after succeeding, the next cycle observes the canonical path and
    # resumes here without repeating the rename.
    _regular_file_identity(main.path, local_root)
    removed: list[str] = []
    for candidate in cleanup_candidates:
        guard = _refresh_task_guard(client, info_hash, guard)
        current = _guard_file(guard, candidate.index, candidate.name, candidate.size)
        priority = current.priority
        before_priority = guard
        if priority != 0:
            _require_video_identity(candidate, local_root)
            try:
                client.set_torrent_file_priority(info_hash, (candidate.index,), 0)
                guard = _guard_after_priority(
                    client,
                    info_hash,
                    guard,
                    index=candidate.index,
                    priority=0,
                )
            except (CompletedDownloadError, DownloaderError, OSError):
                _best_effort_restore_uncertain_priority(
                    client,
                    info_hash,
                    before_priority,
                    candidate=candidate,
                    priority=priority,
                )
                raise
        try:
            guard = _refresh_task_guard(client, info_hash, guard)
            removed_now = file_remover(
                candidate.path,
                local_root,
                candidate.identity,
            )
        except (CompletedDownloadError, OSError):
            if _path_exists_without_following(candidate.path) and priority != 0:
                _best_effort_restore_uncertain_priority(
                    client,
                    info_hash,
                    before_priority,
                    candidate=candidate,
                    priority=priority,
                )
            raise
        if removed_now or not _path_exists_without_following(candidate.path):
            removed.append(candidate.name)
        else:
            if priority != 0:
                _best_effort_restore_uncertain_priority(
                    client,
                    info_hash,
                    before_priority,
                    candidate=candidate,
                    priority=priority,
                )
            raise CompletedDownloadError("extra video cleanup was not confirmed")

    return _PreparationResult(
        renamed=tuple(renamed),
        removed=tuple(removed),
        reviewed=not advertisement_probe_incomplete,
        ready_to_move=True,
        guard=guard,
        archive_code=code,
        content_is_file=content_is_file,
    )


def _canonicalize_content_root(
    client: QbittorrentClient,
    *,
    info_hash: str,
    guard: _TaskGuard,
    videos: list[_VideoCandidate],
    save_path: PurePosixPath,
    content_path: PurePosixPath,
    code: str,
    local_root: Path,
    path_resolver: PathResolver,
) -> tuple[_TaskGuard, list[_VideoCandidate], PurePosixPath]:
    try:
        relative_content = content_path.relative_to(save_path)
    except ValueError as exc:
        raise CompletedDownloadError(
            "qBittorrent content directory is outside its save path"
        ) from exc
    if len(relative_content.parts) != 1:
        raise CompletedDownloadError(
            "qBittorrent content root is not a single directory"
        )
    old_root = relative_content.as_posix()
    if old_root == code:
        _require_guard_under_root(guard, PurePosixPath(code))
        return guard, videos, content_path

    expected = _guard_with_folder_rename(
        guard,
        old_name=old_root,
        new_name=code,
    )
    _refresh_task_guard(client, info_hash, guard)
    try:
        client.rename_torrent_folder(info_hash, old_root, code)
    except DownloaderError as rename_error:
        try:
            current = _read_task_guard(client, info_hash)
        except (CompletedDownloadError, DownloaderError, OSError):
            raise rename_error
        if current != expected:
            raise rename_error
        guard = current
    else:
        guard = _refresh_task_guard(client, info_hash, expected)

    remapped: list[_VideoCandidate] = []
    files_by_index = {item.index: item for item in guard.files}
    for video in videos:
        tracked = files_by_index.get(video.index)
        if tracked is None or tracked.size != video.size:
            raise CompletedDownloadError(
                "qBittorrent folder rename changed its tracked files"
            )
        new_path = path_resolver(save_path / PurePosixPath(tracked.name))
        identity = _regular_file_identity(new_path, local_root)
        if identity != video.identity:
            raise CompletedDownloadError(
                "qBittorrent folder rename changed a video unexpectedly"
            )
        remapped.append(
            replace(video, name=tracked.name, path=new_path, identity=identity)
        )
    return guard, remapped, save_path / code


def _archive_qb_location(
    library: PurePosixPath,
    prepared: _PreparationResult,
) -> PurePosixPath:
    code = prepared.archive_code
    guard = prepared.guard
    if code is None or guard is None:
        raise CompletedDownloadError(
            "qBittorrent task has no trusted catalog code for archival"
        )
    try:
        category = archive_category(code)
    except ValueError as exc:
        raise CompletedDownloadError("archive category is invalid") from exc
    category_root = library / category
    if prepared.content_is_file:
        return category_root / code
    if PurePosixPath(guard.content_path).name != code:
        raise CompletedDownloadError(
            "qBittorrent content directory does not match the catalog code"
        )
    return category_root


def _snapshot_has_canonical_archive_location(
    snapshot: dict[str, object],
    library: PurePosixPath,
) -> bool:
    try:
        save_path = PurePosixPath(
            normalize_qb_path(str(snapshot.get("save_path") or ""))
        )
        content_path = PurePosixPath(
            normalize_qb_path(str(snapshot.get("content_path") or ""))
        )
    except QbPathError:
        return False
    code = _trusted_catalog_code(
        str(snapshot.get("name") or ""),
        content_path.stem if content_path.suffix else content_path.name,
    )
    if code is None:
        return False
    try:
        category_root = library / archive_category(code)
    except ValueError:
        return False
    if save_path == category_root:
        return content_path == save_path / code
    return (
        save_path == category_root / code
        and content_path.parent == save_path
        and code in _catalog_codes(content_path.stem)
    )


def _completed_snapshot(
    client: QbittorrentClient, category: str
) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    seen_hashes: set[str] = set()
    offset = 0

    while True:
        page = client.list_torrents(
            filter_name="completed",
            category=category,
            limit=PAGE_SIZE,
            offset=offset,
        )
        new_hashes = 0
        for task in page:
            info_hash = str(task.get("hash") or "").strip().lower()
            if info_hash and info_hash in seen_hashes:
                continue
            if info_hash:
                seen_hashes.add(info_hash)
                new_hashes += 1
            tasks.append(task)
        if len(page) < PAGE_SIZE or not new_hashes:
            break
        offset += PAGE_SIZE

    return tasks


def run_completed_download_organizer(
    stop_event: threading.Event,
    on_library_changed: LibraryChangeObserver | None = None,
) -> None:
    interval = _interval_seconds()
    reviewed_hashes: set[str] = set()
    reviewed_cleared_at = time.monotonic()
    while not stop_event.is_set():
        try:
            reviewed_cleared_at = _expire_reviewed_hashes(
                reviewed_hashes,
                last_cleared_at=reviewed_cleared_at,
                now=time.monotonic(),
            )
            config = AppConfig.from_env().qbittorrent
            if (
                config.configured
                and config.category
                and config.save_path
                and config.library_path
            ):
                from ..media_metadata.manager import MediaMetadataConfig

                metadata_config = MediaMetadataConfig.from_env()
                result = organize_completed_downloads(
                    QbittorrentClient(config),
                    config,
                    expected_app_library_root=metadata_config.library_path,
                    reviewed_hashes=reviewed_hashes,
                )
                if result.moved:
                    LOGGER.info(
                        "moved %d completed JAV download(s) to the library",
                        len(result.moved),
                    )
                if result.renamed:
                    LOGGER.info(
                        "renamed %d completed JAV video(s)",
                        len(result.renamed),
                    )
                if result.removed:
                    LOGGER.info(
                        "removed %d confirmed extra JAV video(s)",
                        len(result.removed),
                    )
                if on_library_changed is not None and (
                    result.moved or result.renamed or result.removed
                ):
                    try:
                        on_library_changed()
                    except Exception:
                        LOGGER.warning("media library change observer failed")
                for info_hash, error in result.failed.items():
                    LOGGER.warning(
                        "could not organize completed JAV download %s: %s",
                        info_hash,
                        error,
                    )
        except (CompletedDownloadError, DownloaderError) as exc:
            LOGGER.warning("completed download organizer cycle failed: %s", exc)
        except Exception:
            LOGGER.exception("unexpected completed download organizer failure")
        stop_event.wait(interval)


def _expire_reviewed_hashes(
    reviewed_hashes: set[str],
    *,
    last_cleared_at: float,
    now: float,
) -> float:
    if now - last_cleared_at < REVIEW_CACHE_TTL_SECONDS:
        return last_cleared_at
    reviewed_hashes.clear()
    return now


def _trusted_catalog_code(*values: str) -> str | None:
    route_codes = [_catalog_codes(value) for value in values]
    scores = Counter(code for codes in route_codes for code in codes)
    if not scores:
        return None
    ranked = scores.most_common()
    best, score = ranked[0]
    if score < 2 or (len(ranked) > 1 and ranked[1][1] == score):
        return None
    return best


def _catalog_codes(value: object) -> set[str]:
    normalized = str(value or "").upper()
    output: set[str] = set()
    for match in _CODE_RE.finditer(normalized):
        parsed = normalize_catalog_code(
            re.sub(r"\s+", "-", match.group(0)),
            max_length=40,
        )
        if parsed is not None:
            output.add(parsed[0])
    return output


def _task_guard(
    snapshot: dict[str, object],
    files: tuple[dict[str, object], ...],
) -> _TaskGuard:
    tracked = tuple(
        sorted(
            (
                _TrackedFile(
                    index=_nonnegative_int(item.get("index"), "torrent file index"),
                    name=str(item.get("name") or ""),
                    size=_nonnegative_int(item.get("size"), "torrent file size"),
                    progress=_bounded_progress(item.get("progress")),
                    priority=_nonnegative_int(
                        item.get("priority"), "torrent file priority"
                    ),
                )
                for item in files
            ),
            key=lambda item: item.index,
        )
    )
    if len({item.index for item in tracked}) != len(tracked) or any(
        not item.name for item in tracked
    ):
        raise CompletedDownloadError("qBittorrent file state is invalid")
    return _TaskGuard(
        category=str(snapshot.get("category") or ""),
        save_path=normalize_qb_path(str(snapshot.get("save_path") or "")),
        content_path=normalize_qb_path(str(snapshot.get("content_path") or "")),
        complete=bool(snapshot.get("complete")),
        stage=str(snapshot.get("stage") or ""),
        files=tracked,
    )


def _read_task_guard(client: QbittorrentClient, info_hash: str) -> _TaskGuard:
    snapshot = client.torrent_snapshot(info_hash)
    if snapshot is None:
        raise CompletedDownloadError("qBittorrent task is temporarily unavailable")
    return _task_guard(snapshot, client.torrent_files(info_hash))


def _refresh_task_guard(
    client: QbittorrentClient,
    info_hash: str,
    expected: _TaskGuard,
) -> _TaskGuard:
    current = _read_task_guard(client, info_hash)
    if current != expected:
        raise CompletedDownloadError("qBittorrent task changed during organization")
    return current


def _guard_file(
    guard: _TaskGuard,
    index: int,
    name: str,
    size: int,
) -> _TrackedFile:
    matches = [item for item in guard.files if item.index == index]
    if len(matches) != 1:
        raise CompletedDownloadError("qBittorrent file state was not confirmed")
    item = matches[0]
    if item.name != name or item.size != size:
        raise CompletedDownloadError("qBittorrent file state changed unexpectedly")
    return item


def _guard_after_rename(
    client: QbittorrentClient,
    info_hash: str,
    previous: _TaskGuard,
    *,
    index: int,
    old_name: str,
    new_name: str,
    single_file_content: bool,
) -> _TaskGuard:
    files = tuple(
        replace(item, name=new_name)
        if item.index == index and item.name == old_name
        else item
        for item in previous.files
    )
    if files == previous.files:
        raise CompletedDownloadError("qBittorrent rename target was not tracked")
    content_path = previous.content_path
    if single_file_content:
        content_path = (
            PurePosixPath(previous.save_path) / PurePosixPath(new_name)
        ).as_posix()
    expected = replace(previous, content_path=content_path, files=files)
    return _refresh_task_guard(client, info_hash, expected)


def _guard_with_folder_rename(
    previous: _TaskGuard,
    *,
    old_name: str,
    new_name: str,
) -> _TaskGuard:
    old_root = PurePosixPath(old_name)
    new_root = PurePosixPath(new_name)
    if (
        old_root.is_absolute()
        or new_root.is_absolute()
        or len(old_root.parts) != 1
        or len(new_root.parts) != 1
        or old_root == new_root
    ):
        raise CompletedDownloadError("qBittorrent folder rename path is invalid")
    expected_content = PurePosixPath(previous.save_path) / old_root
    if PurePosixPath(previous.content_path) != expected_content:
        raise CompletedDownloadError("qBittorrent content root changed unexpectedly")
    _require_guard_under_root(previous, old_root)
    files = tuple(
        replace(
            item,
            name=(new_root / PurePosixPath(item.name).relative_to(old_root)).as_posix(),
        )
        for item in previous.files
    )
    if len({item.name for item in files}) != len(files):
        raise CompletedDownloadError("qBittorrent folder rename target is ambiguous")
    return replace(
        previous,
        content_path=(PurePosixPath(previous.save_path) / new_root).as_posix(),
        files=files,
    )


def _require_guard_under_root(guard: _TaskGuard, root: PurePosixPath) -> None:
    if not guard.files or any(
        root not in PurePosixPath(item.name).parents for item in guard.files
    ):
        raise CompletedDownloadError(
            "qBittorrent tracked files do not share the content root"
        )


def _guard_after_priority(
    client: QbittorrentClient,
    info_hash: str,
    previous: _TaskGuard,
    *,
    index: int,
    priority: int,
) -> _TaskGuard:
    original = next((item for item in previous.files if item.index == index), None)
    if original is None:
        raise CompletedDownloadError("qBittorrent priority target was not tracked")
    expected = _guard_with_priority(previous, index=index, priority=priority)
    return _refresh_task_guard(client, info_hash, expected)


def _guard_with_priority(
    guard: _TaskGuard,
    *,
    index: int,
    priority: int,
) -> _TaskGuard:
    if not any(item.index == index for item in guard.files):
        raise CompletedDownloadError("qBittorrent priority target was not tracked")
    return replace(
        guard,
        files=tuple(
            replace(item, priority=priority) if item.index == index else item
            for item in guard.files
        ),
    )


def _best_effort_restore_priority(
    client: QbittorrentClient,
    info_hash: str,
    guard: _TaskGuard,
    *,
    index: int,
    priority: int,
) -> bool:
    expected = _guard_with_priority(guard, index=index, priority=priority)
    try:
        _refresh_task_guard(client, info_hash, guard)
        client.set_torrent_file_priority(info_hash, (index,), priority)
    except (CompletedDownloadError, OSError):
        return False
    except DownloaderError:
        try:
            return _read_task_guard(client, info_hash) == expected
        except (CompletedDownloadError, DownloaderError, OSError):
            return False
    try:
        return _refresh_task_guard(client, info_hash, expected) == expected
    except (CompletedDownloadError, DownloaderError, OSError):
        return False


def _best_effort_restore_uncertain_priority(
    client: QbittorrentClient,
    info_hash: str,
    previous: _TaskGuard,
    *,
    candidate: _VideoCandidate,
    priority: int,
) -> bool:
    try:
        current = _read_task_guard(client, info_hash)
    except (CompletedDownloadError, DownloaderError, OSError):
        return False
    if current == previous:
        return True
    deselected = _guard_with_priority(previous, index=candidate.index, priority=0)
    if current != deselected or not _path_exists_without_following(candidate.path):
        return False
    return _best_effort_restore_priority(
        client,
        info_hash,
        current,
        index=candidate.index,
        priority=priority,
    )


def _looks_like_advertisement(value: object) -> bool:
    name = str(value or "")
    if _EXPLICIT_ADVERTISEMENT_RE.search(name):
        return True
    if _MARKETING_DOMAIN_RE.search(name) and _MARKETING_TERM_RE.search(name):
        return True
    return bool(
        _MARKETING_DOMAIN_RE.search(name)
        and _STREAMING_MARKETING_RE.search(name)
    )


def _has_old_stem_sidecar(
    main: _VideoCandidate,
    files: tuple[dict[str, object], ...],
) -> bool:
    main_name = PurePosixPath(main.name)
    for item in files:
        candidate = PurePosixPath(str(item.get("name") or ""))
        if candidate != main_name and _is_old_stem_sidecar(
            candidate.name, main_name.stem
        ):
            return True
    pending = [main.path.parent]
    visited = 0
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_SIDECAR_SCAN_ENTRIES:
                        return True
                    entry_path = Path(entry.path)
                    if entry_path == main.path:
                        continue
                    if _is_old_stem_sidecar(entry.name, main.path.stem):
                        return True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry_path)
                    except OSError as exc:
                        raise CompletedDownloadError(
                            "video sidecars could not be inspected"
                        ) from exc
    except OSError as exc:
        raise CompletedDownloadError("video sidecars could not be inspected") from exc
    return False


def _is_old_stem_sidecar(name: str, old_stem: str) -> bool:
    suffix = Path(name).suffix.lower()
    if not suffix or suffix in VIDEO_SUFFIXES:
        return False
    base = name[: -len(suffix)].casefold()
    stem = old_stem.casefold()
    return base == stem or any(
        base.startswith(f"{stem}{separator}") for separator in (".", "-", "_")
    )


def _probe_video_duration(path: Path, allowed_root: Path) -> float:
    _regular_file_identity(path, allowed_root)
    if not path.is_absolute():
        raise CompletedDownloadError("local video path must be absolute")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompletedDownloadError("local video inspection failed") from exc
    if result.returncode != 0 or len(result.stdout) > MAX_FFPROBE_OUTPUT_BYTES:
        raise CompletedDownloadError("local video inspection failed")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        streams = payload.get("streams") if isinstance(payload, dict) else None
        raw_duration = payload.get("format", {}).get("duration")
        duration = float(raw_duration)
    except (
        AttributeError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise CompletedDownloadError("local video inspection failed") from exc
    if (
        not isinstance(streams, list)
        or not any(
            isinstance(stream, dict) and stream.get("codec_type") == "video"
            for stream in streams
        )
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise CompletedDownloadError("local video inspection failed")
    return duration


def _probe_slot_available(deadline: float) -> bool:
    return deadline - time.monotonic() >= FFPROBE_TIMEOUT_SECONDS


def _regular_directory_root(path: Path) -> Path:
    if not path.is_absolute():
        raise CompletedDownloadError("local controlled root must be absolute")
    try:
        root_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CompletedDownloadError("local controlled root is unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise CompletedDownloadError("local controlled root is not a regular directory")
    return resolved


def _regular_file_identity(path: Path, allowed_root: Path) -> _FileIdentity:
    try:
        root = allowed_root.resolve(strict=True)
        lexical = path.absolute()
        relative = lexical.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CompletedDownloadError(
            "video path is outside the controlled root"
        ) from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise CompletedDownloadError("video path contains an unsafe directory")
    file_stat = lexical.lstat()
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise CompletedDownloadError("video path is not a regular file")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CompletedDownloadError(
            "video path is outside the controlled root"
        ) from exc
    return _FileIdentity(
        device=int(file_stat.st_dev),
        inode=int(file_stat.st_ino),
        mode=int(file_stat.st_mode),
        size=int(file_stat.st_size),
        modified_ns=int(file_stat.st_mtime_ns),
        changed_ns=int(file_stat.st_ctime_ns),
    )


def _require_video_identity(video: _VideoCandidate, allowed_root: Path) -> None:
    if _regular_file_identity(video.path, allowed_root) != video.identity:
        raise CompletedDownloadError("a tracked video file changed unexpectedly")


def _verify_video_identities(
    videos: list[_VideoCandidate],
    allowed_root: Path,
) -> None:
    for video in videos:
        _require_video_identity(video, allowed_root)


def _safe_unlink_regular(
    path: Path,
    allowed_root: Path,
    expected: _FileIdentity,
) -> bool:
    try:
        current = _regular_file_identity(path, allowed_root)
    except FileNotFoundError:
        return False
    if current != expected:
        raise CompletedDownloadError("extra video changed before cleanup")
    if os.name == "nt":
        quarantine = path.with_name(f".jav-organizer-{uuid.uuid4().hex}.quarantine")
        try:
            path.rename(quarantine)
            quarantined = _regular_file_identity(quarantine, allowed_root)
            if not _same_file_object(quarantined, expected):
                raise CompletedDownloadError("quarantined video changed unexpectedly")
            descriptor = os.open(quarantine, os.O_RDONLY)
            try:
                opened = _identity(os.fstat(descriptor))
                latest = _regular_file_identity(quarantine, allowed_root)
                if (
                    not _same_file_object(opened, latest)
                    or not _same_file_object(latest, expected)
                    or latest != quarantined
                ):
                    raise CompletedDownloadError(
                        "quarantined video changed unexpectedly"
                    )
            finally:
                os.close(descriptor)
            if _regular_file_identity(quarantine, allowed_root) != latest:
                raise CompletedDownloadError("quarantined video changed unexpectedly")
            quarantine.unlink()
            return True
        except BaseException:
            _best_effort_restore_quarantine_path(quarantine, path)
            raise

    root = allowed_root.resolve(strict=True)
    relative = path.absolute().relative_to(root)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(root, directory_flags)
    file_fd: int | None = None
    quarantine_name = f".jav-organizer-{uuid.uuid4().hex}.quarantine"
    quarantined = False
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        name = relative.name
        path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(path_stat.st_mode) or _identity(path_stat) != expected:
            raise CompletedDownloadError("extra video changed before cleanup")
        file_fd = os.open(name, file_flags, dir_fd=directory_fd)
        if _identity(os.fstat(file_fd)) != expected:
            raise CompletedDownloadError("extra video changed before cleanup")
        latest = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(latest) != expected:
            raise CompletedDownloadError("extra video changed before cleanup")
        os.rename(
            name,
            quarantine_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        quarantined = True
        quarantined_stat = os.stat(
            quarantine_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        opened_quarantine = os.fstat(file_fd)
        quarantined_identity = _identity(quarantined_stat)
        if (
            not stat.S_ISREG(quarantined_stat.st_mode)
            or quarantined_identity != _identity(opened_quarantine)
            or not _same_file_object(quarantined_identity, expected)
        ):
            raise CompletedDownloadError("quarantined video changed unexpectedly")
        os.unlink(quarantine_name, dir_fd=directory_fd)
        quarantined = False
    except FileNotFoundError:
        return False
    except BaseException:
        if quarantined:
            _best_effort_restore_quarantine_fd(
                directory_fd,
                quarantine_name,
                relative.name,
            )
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)
    return True


def _best_effort_restore_quarantine_path(quarantine: Path, original: Path) -> bool:
    if not _path_exists_without_following(quarantine):
        return False
    try:
        os.link(quarantine, original, follow_symlinks=False)
    except OSError:
        return False
    try:
        quarantine.unlink()
    except OSError:
        return False
    return True


def _best_effort_restore_quarantine_fd(
    directory_fd: int,
    quarantine_name: str,
    original_name: str,
) -> bool:
    try:
        os.link(
            quarantine_name,
            original_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError:
        return False
    try:
        os.unlink(quarantine_name, dir_fd=directory_fd)
    except OSError:
        return False
    return True


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        modified_ns=int(value.st_mtime_ns),
        changed_ns=int(value.st_ctime_ns),
    )


def _same_file_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    return (left.device, left.inode, left.mode, left.size, left.modified_ns) == (
        right.device,
        right.inode,
        right.mode,
        right.size,
        right.modified_ns,
    )


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise CompletedDownloadError(f"invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CompletedDownloadError(f"invalid {field}") from exc
    if parsed < 0 or parsed != value:
        raise CompletedDownloadError(f"invalid {field}")
    return parsed


def _positive_int(value: object, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed <= 0:
        raise CompletedDownloadError(f"invalid {field}")
    return parsed


def _bounded_progress(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CompletedDownloadError("invalid torrent file progress") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
        raise CompletedDownloadError("invalid torrent file progress")
    return parsed


def _container_path(path: PurePosixPath) -> Path:
    return Path(path.as_posix())


def _mapped_path_resolver(
    library: PurePosixPath,
    app_library: PurePosixPath,
    resolver: PathResolver,
) -> PathResolver:
    def resolve(path: PurePosixPath) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise CompletedDownloadError(
                "qBittorrent path is outside the controlled root"
            )
        if is_at_or_below(path, library):
            relative = path.relative_to(library)
            mapped = app_library.joinpath(*relative.parts)
            if not is_at_or_below(mapped, app_library):
                raise CompletedDownloadError(
                    "qBittorrent library path is outside the mapped app root"
                )
            return resolver(mapped)
        return resolver(path)

    return resolve


def _validate_resolved_library_mapping(
    expected_root: PurePosixPath,
    app_library: PurePosixPath,
    resolver: PathResolver,
) -> None:
    expected_local = _resolved_non_symlink_directory(
        resolver(expected_root),
        "metadata library root",
    )
    app_local = _resolved_non_symlink_directory(
        resolver(app_library),
        "qBittorrent app library root",
    )
    try:
        app_local.relative_to(expected_local)
    except ValueError as exc:
        raise CompletedDownloadError(
            "qBittorrent app library resolves outside the metadata library root"
        ) from exc


def _resolved_non_symlink_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise CompletedDownloadError(f"{label} must be absolute")
    lexical = path.absolute()
    current = Path(lexical.anchor)
    try:
        for part in lexical.parts[1:]:
            current = current / part
            current_stat = current.lstat()
            is_junction = getattr(current, "is_junction", lambda: False)()
            if stat.S_ISLNK(current_stat.st_mode) or is_junction:
                raise CompletedDownloadError(f"{label} contains a symbolic link")
            if not stat.S_ISDIR(current_stat.st_mode):
                raise CompletedDownloadError(f"{label} is not a directory")
        resolved = lexical.resolve(strict=True)
        with os.scandir(lexical):
            pass
    except CompletedDownloadError:
        raise
    except OSError as exc:
        raise CompletedDownloadError(f"{label} is unavailable") from exc
    return resolved


def _safe_organizer_error(error: BaseException) -> str:
    if isinstance(error, DownloaderHttpError):
        return f"qBittorrent file organization failed with HTTP {error.status_code}"
    if isinstance(error, DownloaderError):
        return "qBittorrent file organization failed"
    return str(error)[:300] or "completed download organization failed"


def _validated_roots(
    staging_value: str,
    library_value: str,
    app_library_value: str,
    expected_app_library_root: str | Path | PurePosixPath | None,
) -> tuple[
    PurePosixPath,
    PurePosixPath,
    PurePosixPath,
    PurePosixPath | None,
]:
    expected_root_value = (
        expected_app_library_root.as_posix()
        if isinstance(expected_app_library_root, (Path, PurePosixPath))
        else expected_app_library_root
    )
    try:
        staging, library = validate_qb_roots(staging_value, library_value)
        mapped_library, app_library = validate_qb_library_mapping(
            library_value,
            app_library_value,
            expected_app_root=expected_root_value,
        )
        expected_root = (
            PurePosixPath(normalize_qb_path(expected_root_value))
            if expected_root_value is not None
            else None
        )
    except QbPathError as exc:
        raise CompletedDownloadError(str(exc)) from exc
    if mapped_library != library:
        raise CompletedDownloadError("qBittorrent library mapping is inconsistent")
    if is_at_or_below(staging, app_library) or is_at_or_below(app_library, staging):
        raise CompletedDownloadError(
            "qBittorrent staging and app library paths must not overlap"
        )
    return staging, library, app_library, expected_root


def _interval_seconds() -> float:
    raw = os.environ.get("JAV_PILOT_QB_ORGANIZER_INTERVAL_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return max(5.0, min(value, 3600.0))
