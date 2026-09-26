"""Keeps the media library index in step with the library roots on disk."""

from __future__ import annotations

import math
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from ..web_download.variant import web_download_variant_from_stem
from .media_probe import (
    LocalFileIdentity,
    LocalMediaProbeSafetyError,
    probe_local_video_height,
)
from .errors import (
    MediaLibraryConflictError,
    MediaLibraryError,
    MediaLibraryRootChangedError,
    MediaLibraryUnavailableError,
)
from .fields import bounded_int, failure_code, finite_nonnegative, finite_positive
from .filesystem import (
    counted_lstat,
    entry_scope,
    identity_scalar,
    is_below,
    is_linklike,
    join_relative,
    make_entry_id,
    make_root_key,
    media_code_from_path,
    minimal_subtrees,
    normalize_refresh_directories,
    quality_height_from_name,
    regular_asset_status,
    regular_root,
    relative_from_root,
    require_within_root,
    safe_relative_path,
    scandir_snapshot,
    stat_fingerprint,
    stat_identity,
)
from .models import (
    DEFAULT_AUDIT_STEP_SECONDS,
    DEFAULT_FULL_SCAN_SECONDS,
    IN_PLACE_AUDIT_FRACTION,
    MAX_AUDIT_DIRECTORIES,
    MAX_SCAN_DEPTH,
    MAX_SCAN_ENTRIES,
    PART_SUFFIX_RE,
    VIDEO_SUFFIXES,
    DirectoryIdentity,
    FileRecord,
    MutableMetrics,
    ReconcileReport,
)
from .nfo import NfoMetadata, read_movie_nfo
from .store import MediaLibraryStore

__all__ = [
    "MediaLibraryIndex",
]


class MediaLibraryIndex:
    def __init__(
        self,
        library_root: Path | str,
        database_path: Path | str | None = None,
        *,
        store: MediaLibraryStore | None = None,
        clock: Callable[[], float] = time.time,
        full_scan_seconds: float = DEFAULT_FULL_SCAN_SECONDS,
        audit_step_seconds: float = DEFAULT_AUDIT_STEP_SECONDS,
        max_depth: int = MAX_SCAN_DEPTH,
        max_entries: int = MAX_SCAN_ENTRIES,
        video_height_probe: Callable[
            [Path, Path, LocalFileIdentity], int | None
        ] = probe_local_video_height,
    ) -> None:
        self.root_path = Path(library_root)
        if not self.root_path.is_absolute():
            raise MediaLibraryError("media library root must be absolute")
        if store is None and database_path is None:
            raise MediaLibraryError("media library database path is required")
        self.store = store or MediaLibraryStore(Path(database_path), clock=clock)
        self._clock = clock
        self.full_scan_seconds = finite_nonnegative(
            full_scan_seconds, "full scan interval"
        )
        self.audit_step_seconds = finite_positive(
            audit_step_seconds, "in-place audit step", maximum=24 * 60 * 60
        )
        self.max_depth = bounded_int(max_depth, "max depth", minimum=1, maximum=64)
        self.max_entries = bounded_int(
            max_entries, "max entries", minimum=1, maximum=10_000_000
        )
        if not callable(video_height_probe):
            raise MediaLibraryError("media library video probe is invalid")
        self._video_height_probe = video_height_probe
        self.root_key = make_root_key(self.root_path)
        self._lock = threading.Lock()

    def rebuild(
        self,
        *,
        accept_root_change: bool = False,
        fault_injector: Callable[[str, str], None] | None = None,
    ) -> ReconcileReport:
        return self.reconcile(
            force_full=True,
            accept_root_change=accept_root_change,
            fault_injector=fault_injector,
        )

    def reconcile(
        self,
        *,
        force_full: bool = False,
        accept_root_change: bool = False,
        refresh_paths: Sequence[str] = (),
        fault_injector: Callable[[str, str], None] | None = None,
    ) -> ReconcileReport:
        if accept_root_change and not force_full:
            raise MediaLibraryError(
                "accepting a media library root change requires a full rebuild"
            )
        refresh_directories = normalize_refresh_directories(refresh_paths)
        if not self._lock.acquire(blocking=False):
            raise MediaLibraryConflictError(
                "media library reconciliation is already running"
            )
        generation_id: str | None = None
        try:
            metrics = MutableMetrics()
            try:
                root, root_stat = regular_root(self.root_path, metrics)
            except MediaLibraryUnavailableError:
                self.store.mark_unknown(self.root_key)
                raise
            root_device, root_inode = stat_identity(root_stat)
            previous_identity = self.store.root(self.root_key)
            published_identity = self.store.published_root_identity(self.root_key)
            identity_changed = bool(
                (
                    previous_identity is not None
                    and previous_identity["device"]
                    and previous_identity["inode"]
                    and (
                        previous_identity["device"] != root_device
                        or previous_identity["inode"] != root_inode
                    )
                )
                or (
                    published_identity is not None
                    and published_identity != (root_device, root_inode)
                )
            )
            self.store.register_root(
                self.root_key,
                device=root_device,
                inode=root_inode,
                accept_identity_change=accept_root_change,
            )
            previous = self.store.root(self.root_key)
            if previous is None:
                raise MediaLibraryError("media library root state is unavailable")
            base_generation = (
                str(previous["published_generation_id"])
                if previous["published_generation_id"] is not None
                else None
            )
            last_full = (
                float(previous["last_full_scan_at"])
                if previous["last_full_scan_at"] is not None
                else None
            )
            scan_kind = (
                "full"
                if force_full
                or base_generation is None
                or last_full is None
                or self._clock() - last_full >= self.full_scan_seconds
                else "incremental"
            )
            quality_cache = (
                self.store.generation_quality_cache(base_generation)
                if base_generation is not None and not identity_changed
                else {}
            )
            changed_directories: set[str] | None = None
            deleted_directories: set[str] = set()
            if scan_kind == "incremental" and base_generation is not None:
                cached = self.store.generation_directories(base_generation)
                changed_directories, deleted_directories = self._changed_directories(
                    root, cached, metrics
                )
                audit_directories = set(
                    self._scheduled_audit_directories(base_generation)
                )
                audit_directories.update(refresh_directories)
                audit_directories.difference_update(changed_directories)
                audit_directories.difference_update(deleted_directories)
                audited_changed, audited_deleted = self._audit_directories(
                    root,
                    base_generation,
                    cached,
                    audit_directories,
                    metrics,
                    quality_cache,
                )
                changed_directories.update(audited_changed)
                deleted_directories.update(audited_deleted)
                self._verify_root_identity(root, root_device, root_inode, metrics)
                if not changed_directories and not deleted_directories:
                    self.store.mark_available_without_generation(self.root_key)
                    present, missing = self.store.published_counts(self.root_key)
                    return ReconcileReport(
                        root_key=self.root_key,
                        generation_id=base_generation,
                        previous_generation_id=base_generation,
                        scan_kind="incremental",
                        changed=False,
                        published=False,
                        present=present,
                        missing=missing,
                        metrics=metrics.freeze(),
                    )
            generation_id = self.store.begin_generation(
                self.root_key,
                scan_kind=scan_kind,
                base_generation_id=base_generation
                if scan_kind == "incremental"
                else None,
                root_device=root_device,
                root_inode=root_inode,
            )
            if fault_injector is not None:
                fault_injector("generation_started", generation_id)
            affected: set[str] = set()
            if scan_kind == "full":
                affected.update(
                    self._scan_tree(
                        root,
                        generation_id,
                        start_relative=".",
                        metrics=metrics,
                        fault_injector=fault_injector,
                        quality_cache=quality_cache,
                    )
                )
                self.store.rebuild_entries(generation_id)
                self.store.copy_missing_tombstones(
                    None if identity_changed else base_generation,
                    generation_id,
                )
            else:
                for relative in minimal_subtrees(deleted_directories):
                    affected.update(self.store.remove_subtree(generation_id, relative))
                pending = set(changed_directories or ())
                cached = self.store.generation_directories(base_generation or "")
                while pending:
                    relative = min(pending, key=lambda value: (value.count("/"), value))
                    pending.remove(relative)
                    depth = 0 if relative == "." else len(PurePosixPath(relative).parts)
                    if depth > self.max_depth:
                        raise MediaLibraryUnavailableError(
                            "media library scan depth limit was exceeded"
                        )
                    if any(
                        is_below(relative, deleted) for deleted in deleted_directories
                    ):
                        continue
                    scanned, discovered, changed_entries = self._scan_directory(
                        root,
                        generation_id,
                        relative,
                        metrics,
                        quality_cache,
                    )
                    if metrics.entries_seen > self.max_entries:
                        raise MediaLibraryUnavailableError(
                            "media library scan entry limit was exceeded"
                        )
                    affected.update(changed_entries)
                    for child in discovered:
                        cached_child = cached.get(child)
                        if cached_child is None:
                            pending.add(child)
                        elif child in (changed_directories or set()):
                            pending.add(child)
                    if fault_injector is not None:
                        fault_injector("directory_scanned", scanned)
                self.store.rebuild_entries(generation_id, affected)
            self._verify_root_identity(root, root_device, root_inode, metrics)
            if fault_injector is not None:
                fault_injector("before_publish", generation_id)
            present, missing = self.store.publish_generation(
                generation_id,
                root_device=root_device,
                root_inode=root_inode,
            )
            return ReconcileReport(
                root_key=self.root_key,
                generation_id=generation_id,
                previous_generation_id=base_generation,
                scan_kind=scan_kind,
                changed=True,
                published=True,
                present=present,
                missing=missing,
                metrics=metrics.freeze(),
            )
        except BaseException as exc:
            if generation_id is not None:
                try:
                    self.store.fail_generation(generation_id, failure_code(exc))
                except (OSError, sqlite3.Error, MediaLibraryError):
                    pass
            if isinstance(exc, MediaLibraryUnavailableError):
                try:
                    current_root = self.store.root(self.root_key)
                    if current_root is None or current_root["state"] != "unknown":
                        self.store.mark_unknown(self.root_key)
                except (OSError, sqlite3.Error, MediaLibraryError):
                    pass
            if isinstance(exc, (OSError, sqlite3.Error)):
                self.store.mark_unknown(self.root_key)
                raise MediaLibraryUnavailableError(
                    "media library reconciliation failed"
                ) from exc
            raise
        finally:
            self._lock.release()

    def _changed_directories(
        self,
        root: Path,
        cached: Mapping[str, DirectoryIdentity],
        metrics: MutableMetrics,
    ) -> tuple[set[str], set[str]]:
        changed: set[str] = set()
        deleted: set[str] = set()
        for relative in sorted(cached, key=lambda value: (value.count("/"), value)):
            if any(is_below(relative, ancestor) for ancestor in deleted):
                continue
            path = join_relative(root, relative)
            try:
                current = counted_lstat(path, metrics)
            except FileNotFoundError:
                deleted.add(relative)
                continue
            except OSError as exc:
                raise MediaLibraryUnavailableError(
                    "media library directory identity could not be checked"
                ) from exc
            if is_linklike(current) or not stat.S_ISDIR(current.st_mode):
                deleted.add(relative)
                continue
            expected = cached[relative]
            if (
                identity_scalar(current.st_dev) != expected.device
                or identity_scalar(current.st_ino) != expected.inode
                or int(current.st_mtime_ns) != expected.modified_ns
            ):
                changed.add(relative)
        return changed, deleted

    def _scheduled_audit_directories(
        self,
        generation_id: str,
    ) -> tuple[str, ...]:
        directories = self.store.generation_media_directories(generation_id)
        if not directories:
            return ()
        count = min(
            MAX_AUDIT_DIRECTORIES,
            max(1, math.ceil(len(directories) * IN_PLACE_AUDIT_FRACTION)),
        )
        slot = int(self._clock() // self.audit_step_seconds)
        start = (slot * count) % len(directories)
        return tuple(
            directories[(start + offset) % len(directories)] for offset in range(count)
        )

    def _audit_directories(
        self,
        root: Path,
        generation_id: str,
        cached: Mapping[str, DirectoryIdentity],
        directories: Iterable[str],
        metrics: MutableMetrics,
        quality_cache: Mapping[LocalFileIdentity, int | None],
    ) -> tuple[set[str], set[str]]:
        changed: set[str] = set()
        deleted: set[str] = set()
        for relative in sorted(set(directories)):
            expected_identity = cached.get(relative)
            if expected_identity is None:
                changed.add(relative)
                continue
            try:
                _, identity, _, files = self._read_directory(
                    root,
                    relative,
                    metrics,
                    quality_cache,
                )
            except FileNotFoundError:
                deleted.add(relative)
                continue
            expected_files = self.store.generation_directory_files(
                generation_id,
                relative,
            )
            if identity != expected_identity or tuple(files) != expected_files:
                changed.add(relative)
        return changed, deleted

    def _scan_tree(
        self,
        root: Path,
        generation_id: str,
        *,
        start_relative: str,
        metrics: MutableMetrics,
        fault_injector: Callable[[str, str], None] | None,
        quality_cache: Mapping[LocalFileIdentity, int | None],
    ) -> set[str]:
        affected: set[str] = set()
        pending = [start_relative]
        while pending:
            relative = pending.pop()
            depth = 0 if relative == "." else len(PurePosixPath(relative).parts)
            if depth > self.max_depth:
                raise MediaLibraryUnavailableError(
                    "media library scan depth limit was exceeded"
                )
            scanned, children, changed_entries = self._scan_directory(
                root, generation_id, relative, metrics, quality_cache
            )
            affected.update(changed_entries)
            if metrics.entries_seen > self.max_entries:
                raise MediaLibraryUnavailableError(
                    "media library scan entry limit was exceeded"
                )
            pending.extend(reversed(children))
            if fault_injector is not None:
                fault_injector("directory_scanned", scanned)
        return affected

    def _scan_directory(
        self,
        root: Path,
        generation_id: str,
        relative: str,
        metrics: MutableMetrics,
        quality_cache: Mapping[LocalFileIdentity, int | None],
    ) -> tuple[str, list[str], set[str]]:
        clean_relative, identity, children, files = self._read_directory(
            root,
            relative,
            metrics,
            quality_cache,
        )
        affected = self.store.replace_directory(generation_id, identity, files)
        return clean_relative, children, affected

    def _read_directory(
        self,
        root: Path,
        relative: str,
        metrics: MutableMetrics,
        quality_cache: Mapping[LocalFileIdentity, int | None],
    ) -> tuple[str, DirectoryIdentity, list[str], list[FileRecord]]:
        for attempt in range(3):
            try:
                return self._read_directory_once(root, relative, metrics, quality_cache)
            except FileNotFoundError:
                if attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))
        raise AssertionError("directory retry loop exhausted")

    def _read_directory_once(
        self,
        root: Path,
        relative: str,
        metrics: MutableMetrics,
        quality_cache: Mapping[LocalFileIdentity, int | None],
    ) -> tuple[str, DirectoryIdentity, list[str], list[FileRecord]]:
        clean_relative = safe_relative_path(relative, allow_root=True)
        directory = join_relative(root, clean_relative)
        before = counted_lstat(directory, metrics)
        if is_linklike(before) or not stat.S_ISDIR(before.st_mode):
            raise MediaLibraryUnavailableError("media library directory is unsafe")
        require_within_root(directory, root, directory=True)
        entries = scandir_snapshot(directory, metrics)
        after = counted_lstat(directory, metrics)
        if stat_fingerprint(before) != stat_fingerprint(after):
            raise MediaLibraryUnavailableError(
                "media library directory changed while it was scanned"
            )
        children: list[str] = []
        regular: dict[str, tuple[Path, os.stat_result]] = {}
        for name, entry_stat in entries:
            if name.startswith("."):
                continue
            path = directory / name
            if is_linklike(entry_stat):
                continue
            child_relative = relative_from_root(path, root)
            if stat.S_ISDIR(entry_stat.st_mode):
                children.append(child_relative)
            elif stat.S_ISREG(entry_stat.st_mode):
                regular[name] = (path, entry_stat)
        files: list[FileRecord] = []
        for name in sorted(regular, key=os.fsencode):
            path, file_stat = regular[name]
            if path.suffix.lower() not in VIDEO_SUFFIXES or file_stat.st_size <= 0:
                continue
            files.append(
                self._file_record(
                    root,
                    path,
                    file_stat,
                    regular,
                    clean_relative,
                    quality_cache,
                )
            )
            metrics.files_indexed += 1
        identity = DirectoryIdentity(
            relative_path=clean_relative,
            device=identity_scalar(after.st_dev),
            inode=identity_scalar(after.st_ino),
            modified_ns=int(after.st_mtime_ns),
            changed_ns=int(after.st_ctime_ns),
        )
        metrics.directories_scanned += 1
        return clean_relative, identity, sorted(children), files

    def _file_record(
        self,
        root: Path,
        media: Path,
        file_stat: os.stat_result,
        siblings: Mapping[str, tuple[Path, os.stat_result]],
        parent_relative: str,
        quality_cache: Mapping[LocalFileIdentity, int | None],
    ) -> FileRecord:
        relative = relative_from_root(media, root)
        nfo_name = f"{media.stem}.nfo"
        nfo_metadata: NfoMetadata | None = None
        nfo_status = "missing"
        nfo_path: str | None = None
        nfo_item = siblings.get(nfo_name)
        if nfo_item is not None:
            nfo_path = relative_from_root(nfo_item[0], root)
            nfo_metadata = read_movie_nfo(nfo_item[0], root)
            nfo_status = "present" if nfo_metadata is not None else "invalid"
        path_code = media_code_from_path(media, root)
        nfo_code = (
            (nfo_metadata.code, nfo_metadata.code_key)
            if nfo_metadata is not None
            and nfo_metadata.code is not None
            and nfo_metadata.code_key is not None
            else None
        )
        if (
            path_code is not None
            and nfo_code is not None
            and path_code[1] != nfo_code[1]
        ):
            nfo_status = "invalid"
            nfo_metadata = None
            nfo_code = None
        code_pair = path_code or nfo_code
        code = code_pair[0] if code_pair is not None else None
        code_key = code_pair[1] if code_pair is not None else None
        variant = web_download_variant_from_stem(media.name)
        scope_path = entry_scope(media, root, code_key)
        entry_id = make_entry_id(self.root_key, scope_path, code_key, variant)
        image_prefix = f"{media.stem}-" if media.parent == root else ""
        portrait = regular_asset_status(siblings, f"{image_prefix}poster.jpg")
        landscape_names = tuple(
            f"{image_prefix}{name}.jpg"
            for name in ("fanart", "backdrop", "landscape", "thumb")
        )
        landscape_count = sum(
            regular_asset_status(siblings, name) == "present"
            for name in landscape_names
        )
        part_match = PART_SUFFIX_RE.search(media.stem)
        file_identity = LocalFileIdentity(
            device=int(file_stat.st_dev),
            inode=int(file_stat.st_ino),
            size=int(file_stat.st_size),
            modified_ns=int(file_stat.st_mtime_ns),
            changed_ns=int(file_stat.st_ctime_ns),
        )
        cached_quality = file_identity in quality_cache
        quality_height = quality_cache.get(file_identity)
        quality_source = (
            "probe" if cached_quality and quality_height is not None else None
        )
        if not cached_quality:
            try:
                probed_height = self._video_height_probe(media, root, file_identity)
            except LocalMediaProbeSafetyError as exc:
                raise MediaLibraryUnavailableError(
                    "media library video changed while it was inspected"
                ) from exc
            if (
                isinstance(probed_height, int)
                and not isinstance(probed_height, bool)
                and 144 <= probed_height <= 4320
            ):
                quality_height = probed_height
                quality_source = "probe"
        if quality_height is None:
            quality_height = quality_height_from_name(media.name)
            quality_source = "filename"
        return FileRecord(
            relative_path=relative,
            parent_path=parent_relative,
            entry_id=entry_id,
            scope_path=scope_path,
            code=code,
            code_key=code_key,
            variant=variant,
            source="nfo" if nfo_code is not None else "path",
            device=identity_scalar(file_stat.st_dev),
            inode=identity_scalar(file_stat.st_ino),
            size=int(file_stat.st_size),
            modified_ns=int(file_stat.st_mtime_ns),
            changed_ns=int(file_stat.st_ctime_ns),
            suffix=media.suffix.lower(),
            part_key=part_match.group(0).lstrip("-._ ").upper() if part_match else None,
            quality_height=quality_height,
            quality_source=quality_source,
            nfo_status=nfo_status,
            nfo_path=nfo_path,
            nfo_json=nfo_metadata.to_json() if nfo_metadata is not None else None,
            portrait_status=portrait,
            landscape_status="present" if landscape_count >= 2 else "missing",
        )

    def _verify_root_identity(
        self,
        root: Path,
        expected_device: str,
        expected_inode: str,
        metrics: MutableMetrics,
    ) -> None:
        current = counted_lstat(root, metrics)
        if (
            is_linklike(current)
            or not stat.S_ISDIR(current.st_mode)
            or identity_scalar(current.st_dev) != expected_device
            or identity_scalar(current.st_ino) != expected_inode
        ):
            self.store.mark_unknown(self.root_key)
            raise MediaLibraryRootChangedError(
                "media library root changed while it was scanned"
            )
