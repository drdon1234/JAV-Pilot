from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Sequence

from ..config.app_config import AppConfig
from ..library.archive import plan_archive_layout
from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..torrent.qbittorrent import DownloaderError, QbittorrentClient
from .images import MetadataArtworkSelection, select_metadata_artwork
from .publish import (
    Artwork,
    MetadataMigrationError,
    MetadataPublishConflict,
    MetadataPublishError,
    VIDEO_SUFFIXES,
    inspect_movie_nfo_title,
    inspect_movie_metadata_assets,
    migrate_movie_nfo_title,
    publish_movie_metadata,
    safe_relative_media_path,
)
from .sources import (
    MediaMetadata,
    MediaMetadataNotFound,
    MediaMetadataSourceError,
    resolve_media_metadata,
)
from .store import (
    MediaMetadataConflictError,
    MediaMetadataNotFoundError,
    MediaMetadataStore,
    MediaMetadataStoreError,
)
from ..library.nfo import read_movie_nfo
from ..config.qb_paths import QbPathError, is_at_or_below, normalize_qb_path
from ..config.paths import (
    default_database_path,
    default_library_path,
    default_nfo_backup_path,
)
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
    web_download_variant_from_stem,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_DESCRIPTION_TIMEOUT_SECONDS = 30.0
MEDIA_WAIT_MIN_SECONDS = 30.0
MEDIA_WAIT_JITTER_SECONDS = 30
QB_MISSING_GRACE_SECONDS = 60.0 * 60.0
WEB_PATH_MISSING_GRACE_SECONDS = 15.0 * 60.0
QB_TASK_MISSING_ERROR = "qBittorrent task was not found"
MAX_SCAN_ENUM_ENTRIES = 100_000
MAX_SCAN_CANDIDATES = 10_000
MAX_SCAN_DEPTH = 8
MAX_ATTEMPTS = 10
# Retry delays grow from one minute to one day (about 3.5 days in total), so a
# source outage such as a JavDB challenge or a work that is listed a few days
# after release is retried instead of failing within minutes.
RETRY_BASE_SECONDS = 60.0
RETRY_MAX_SECONDS = 86_400.0
NFO_MIGRATION_PREVIEW_SECONDS = 10.0 * 60.0
_DESCRIPTION_OUTPUT_BYTES = 64 * 1024
_SAFE_ERROR_RE = re.compile(r"(?:https?|wss?|ftp)://\S+", flags=re.IGNORECASE)
_SCAN_LAYOUT_NAME_RE = re.compile(
    r"^(?:CD|DISC|DISK|PART|PT|VOL|VOLUME|SEASON)[-._ ]?\d{1,3}$",
    flags=re.IGNORECASE,
)
_SCAN_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:"
    r"FC2PPV\d{2,9}"
    r"|"
    r"[A-Z0-9]{2,16}(?:[-._ ][A-Z0-9]{2,10})*[-._ ]\d{2,9}"
    r"|[A-Z]{2,12}\d{2,8}"
    r")(?![A-Z0-9])",
    flags=re.IGNORECASE,
)


class MediaMetadataError(RuntimeError):
    pass


class MediaMetadataDisabledError(MediaMetadataError):
    pass


class MediaMetadataUnavailableError(MediaMetadataError):
    pass


class _MediaNotReady(MediaMetadataError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("media file is not ready")


_WAIT_QB_MISSING = "qb_missing"
_WAIT_QB_INCOMPLETE = "qb_incomplete"
_WAIT_QB_NOT_ARCHIVED = "qb_not_archived"
_WAIT_QB_FILES_PENDING = "qb_files_pending"
_WAIT_QB_UNAVAILABLE = "qb_unavailable"
_WAIT_WEB_PATH_MISSING = "web_path_missing"
_WAIT_KNOWN_PATH_MISSING = "known_path_missing"
_ARCHIVE_RELOCATION_DIRECTORY = "archive_relocations"
_ARCHIVE_RELOCATION_REVISION = 1
_MAX_ARCHIVE_RELOCATION_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _ArchiveRelocationIntent:
    job_id: str
    kind: str
    download_key: str
    old_relative_path: str
    new_relative_path: str
    media_identity: tuple[int, int, int, int]
    nfo_identity: tuple[int, int, int, int]
    phase: str = "prepared"


@dataclass(frozen=True, slots=True)
class MediaMetadataConfig:
    enabled: bool
    database_path: Path
    library_path: Path
    poll_seconds: float = DEFAULT_POLL_SECONDS
    max_attempts: int = MAX_ATTEMPTS
    backup_path: Path = field(default_factory=default_nfo_backup_path)

    @classmethod
    def from_env(cls) -> "MediaMetadataConfig":
        enabled = _env_bool("JAV_PILOT_MEDIA_METADATA_ENABLED", True)
        database_path = Path(
            os.environ.get("JAV_PILOT_MEDIA_METADATA_DATABASE_PATH", "").strip()
            or default_database_path("media_metadata.sqlite3")
        ).expanduser()
        backup_path = Path(
            os.environ.get("JAV_PILOT_MEDIA_METADATA_BACKUP_PATH", "").strip()
            or default_nfo_backup_path()
        ).expanduser()
        library_value = (
            os.environ.get("JAV_PILOT_MEDIA_METADATA_LIBRARY_PATH", "").strip()
            or os.environ.get("JAV_PILOT_WEB_DOWNLOAD_LIBRARY_PATH", "").strip()
            or os.environ.get("JAV_PILOT_QB_APP_LIBRARY_PATH", "").strip()
            or default_library_path()
        )
        library_path = Path(library_value).expanduser()
        if (
            not database_path.is_absolute()
            or not library_path.is_absolute()
            or not backup_path.is_absolute()
        ):
            raise MediaMetadataUnavailableError("metadata paths must be absolute")
        poll_seconds = _bounded_float(
            os.environ.get("JAV_PILOT_MEDIA_METADATA_POLL_SECONDS", DEFAULT_POLL_SECONDS),
            minimum=0.2,
            maximum=60.0,
            default=DEFAULT_POLL_SECONDS,
        )
        return cls(
            enabled=enabled,
            database_path=database_path,
            library_path=library_path,
            poll_seconds=poll_seconds,
            backup_path=backup_path,
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "library_path": self.library_path.as_posix(),
        }


MetadataResolver = Callable[[str], MediaMetadata]
ArtworkSelector = Callable[..., MetadataArtworkSelection]
DescriptionResolver = Callable[..., str | None]
PublicationObserver = Callable[[str], None]
ArchiveRelocationObserver = Callable[[str, str, str, str], None]


class MediaMetadataManager:
    def __init__(
        self,
        config: MediaMetadataConfig | None = None,
        *,
        store: MediaMetadataStore | None = None,
        metadata_resolver: MetadataResolver | None = None,
        artwork_selector: ArtworkSelector | None = None,
        description_resolver: DescriptionResolver | None = None,
        on_published: PublicationObserver | None = None,
        on_archive_relocated: ArchiveRelocationObserver | None = None,
        qb_client_factory: Callable[[], QbittorrentClient] | None = None,
        clock: Callable[[], float] = time.time,
        start_worker: bool = True,
    ) -> None:
        self.config = config or MediaMetadataConfig.from_env()
        self.store = store or MediaMetadataStore(self.config.database_path)
        self._metadata_resolver = metadata_resolver
        self._artwork_selector = artwork_selector or select_metadata_artwork
        self._description_resolver = (
            description_resolver or discover_optional_description
        )
        self._on_published = on_published
        self._on_archive_relocated = on_archive_relocated
        self._qb_client_factory = qb_client_factory or (
            lambda: QbittorrentClient(AppConfig.from_env().qbittorrent)
        )
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._lifecycle_lock = threading.RLock()
        self._nfo_migration_lock = threading.Lock()
        self._nfo_migration_previews: dict[str, dict[str, object]] = {}
        self._stopping = False
        self._recover_archive_relocations()
        self.store.recover_running()
        self._worker = threading.Thread(
            target=self._dispatch,
            name="jav-media-metadata",
            daemon=True,
        )
        if start_worker:
            self._worker.start()

    def enqueue_qb(self, info_hash: str, code: object) -> dict[str, object]:
        self._require_enabled()
        with self._lifecycle_lock:
            job = self.store.enqueue("qb", info_hash, code)
            if not job.get("relative_media_path") and str(job["status"]) != "completed":
                job = self.store.refresh_qb_registration(job["job_id"])
        self.notify()
        return self._public_job(job)

    def discard_qb(
        self,
        info_hashes: Sequence[str],
        *,
        delete_files: bool = False,
    ) -> int:
        self._require_enabled()
        with self._lifecycle_lock:
            return len(
                self.store.delete_incomplete_qb(
                    info_hashes,
                    include_bound=delete_files,
                )
            )

    def delete_qb(
        self,
        client: QbittorrentClient,
        info_hashes: Sequence[str],
        *,
        delete_files: bool = False,
    ) -> tuple[dict[str, object], int | None]:
        self._require_enabled()
        with self._lifecycle_lock:
            result = client.torrent_action(
                "delete",
                tuple(info_hashes),
                delete_files=delete_files,
            )
            try:
                removed = len(
                    self.store.delete_incomplete_qb(
                        info_hashes,
                        include_bound=delete_files,
                    )
                )
            except (MediaMetadataStoreError, OSError, sqlite3.Error):
                removed = None
        return result, removed

    def enqueue_web(
        self,
        job_id: str,
        code: object,
        relative_media_path: object | None = None,
        *,
        variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    ) -> dict[str, object]:
        self._require_enabled()
        job = self.store.enqueue(
            "web",
            job_id,
            code,
            relative_media_path,
            variant=variant,
        )
        self.notify()
        return self._public_job(job)

    def web_completed(self, job: dict[str, object]) -> None:
        try:
            if str(job.get("status") or "") != "completed":
                return
            output_path = safe_relative_media_path(job.get("output_path"))
            self.enqueue_web(
                str(job.get("job_id") or ""),
                job.get("code"),
                output_path,
                variant=job.get("variant", DEFAULT_WEB_DOWNLOAD_VARIANT),
            )
        except (MediaMetadataError, MediaMetadataStoreError, MetadataPublishError):
            LOGGER.warning("could not queue completed Web metadata")

    def reconcile_web(self, jobs: list[dict[str, object]]) -> None:
        for job in jobs:
            self.web_completed(job)

    def complete_all(self) -> dict[str, object]:
        """一键补全: queue every incomplete library item and retry stalled jobs.

        Manually created folders are included: any playable video is found by
        the library scan. Videos whose names carry no catalog code are only
        reported, because metadata cannot be looked up without one.
        """

        self._require_enabled()
        unidentified: list[str] = []
        queued = self.scan(unidentified=unidentified)
        queued_ids = {str(job["job_id"]) for job in queued}
        retried = 0
        skipped = 0
        for status in ("failed", "retry"):
            for job in self.store.list(500, status_filter=status):
                if str(job["job_id"]) in queued_ids:
                    continue
                try:
                    self.retry(job["job_id"])
                except (MediaMetadataConflictError, MediaMetadataUnavailableError):
                    skipped += 1
                    continue
                retried += 1
        self.notify()
        return {
            "queued": len(queued),
            "retried": retried,
            "skipped": skipped,
            "unidentified": len(unidentified),
            "unidentified_examples": unidentified[:10],
        }

    def scan(
        self,
        code: object | None = None,
        *,
        unidentified: list[str] | None = None,
        retry_existing: bool = True,
    ) -> list[dict[str, object]]:
        self._require_enabled()
        root = _regular_library_root(self.config.library_path)
        requested_key = canonical_catalog_code(code, max_length=40) if code else None
        if code is not None and requested_key is None:
            raise MediaMetadataError("invalid catalog code")
        queued: list[dict[str, object]] = []
        candidates = _collect_library_media_candidates(
            root,
            requested_key=requested_key,
            unidentified=unidentified,
        )

        matched_media = len(candidates)
        queued_candidates: list[tuple[str, str, str, MissavVariant | None]] = []
        for (entry_key, _, _), (
            _,
            relative,
            display_code,
            variant,
        ) in sorted(candidates.items()):
            media_file = root.joinpath(*PurePosixPath(relative).parts)
            try:
                plan = inspect_movie_metadata_assets(
                    library_root=root,
                    media_file=media_file,
                )
            except MetadataPublishError:
                plan = None
            if plan is not None and plan.complete:
                continue
            queued_candidates.append((entry_key, relative, display_code, variant))

        for entry_key, relative, display_code, variant in queued_candidates:
            path_key = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
            job = self.store.enqueue(
                "manual",
                f"{entry_key}:{path_key}",
                display_code,
                relative,
                variant=variant,
            )
            if str(job["status"]) in {"completed", "failed", "retry"}:
                if not retry_existing:
                    # Automatic scans only add new media; they never restart
                    # a job that already ran, which would loop forever on a
                    # work no source can identify.
                    continue
                try:
                    job = self.store.retry(job["job_id"])
                except MediaMetadataConflictError:
                    pass
            queued.append(self._public_job(job))
        if requested_key and not matched_media:
            raise MediaMetadataNotFoundError(
                "no media file was found for this catalog code"
            )
        self.notify()
        return queued

    def preview_nfo_title_migration(
        self,
        code: object | None = None,
    ) -> dict[str, object]:
        self._require_enabled()
        root = _regular_library_root(self.config.library_path)
        requested_key = canonical_catalog_code(code, max_length=40) if code else None
        if code is not None and requested_key is None:
            raise MediaMetadataError("invalid catalog code")

        with self._nfo_migration_lock:
            candidates = _collect_library_media_candidates(
                root,
                requested_key=requested_key,
            )
            if requested_key and not candidates:
                raise MediaMetadataNotFoundError(
                    "no media file was found for this catalog code"
                )
            provenance_by_nfo = self.store.nfo_provenance_map()
            results: list[dict[str, object]] = []
            processable: list[dict[str, object]] = []
            for (entry_key, _, _), (_, relative, display_code, _variant) in sorted(
                candidates.items()
            ):
                media_file = root.joinpath(*PurePosixPath(relative).parts)
                nfo_name = media_file.with_suffix(".nfo").name
                provenance = provenance_by_nfo.get((entry_key, relative, nfo_name))
                base_result: dict[str, object] = {
                    "code": display_code,
                    "relative_media_path": relative,
                    "nfo_path": media_file.with_suffix(".nfo")
                    .relative_to(root)
                    .as_posix(),
                    "provenance": provenance,
                }
                if provenance is None:
                    results.append({**base_result, "status": "unverified"})
                    continue
                try:
                    inspected = inspect_movie_nfo_title(
                        library_root=root,
                        media_file=media_file,
                        code=display_code,
                    )
                except (MetadataPublishError, OSError):
                    results.append(
                        {
                            **base_result,
                            "status": "failed",
                            "error": "NFO inspection failed",
                        }
                    )
                    continue
                item = {
                    **base_result,
                    "nfo_path": inspected.nfo_path,
                    "status": inspected.status,
                }
                results.append(item)
                if inspected.status in {"ready", "current"} and inspected.sha256:
                    processable.append(
                        {
                            **base_result,
                            "expected_sha256": inspected.sha256,
                        }
                    )

            ready_count = sum(item["status"] == "ready" for item in results)
            current_count = sum(item["status"] == "current" for item in results)
            failed_count = sum(item["status"] == "failed" for item in results)
            skipped_count = len(results) - ready_count - current_count - failed_count
            preview_id: str | None = None
            self._nfo_migration_previews.clear()
            if ready_count:
                now = float(self._clock())
                preview_id = uuid.uuid4().hex
                self._nfo_migration_previews[preview_id] = {
                    "expires_at": now + NFO_MIGRATION_PREVIEW_SECONDS,
                    "scanned": len(results),
                    "processable": processable,
                    "fixed_results": [
                        item
                        for item in results
                        if item["status"] not in {"ready", "current"}
                    ],
                }

        return {
            "preview_id": preview_id,
            "scanned": len(results),
            "ready": ready_count,
            "current": current_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "tracked_existing": sum(
                item["status"] == "ready"
                and item.get("provenance") == "tracked_existing"
                for item in results
            ),
            "results": results,
        }

    def migrate_nfo_titles(self, preview_id: object) -> dict[str, object]:
        self._require_enabled()
        clean_preview_id = str(preview_id or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", clean_preview_id):
            raise MediaMetadataError("invalid NFO migration preview")
        root = _regular_library_root(self.config.library_path)
        with self._nfo_migration_lock:
            now = float(self._clock())
            preview = self._nfo_migration_previews.pop(clean_preview_id, None)
            if preview is None or float(preview["expires_at"]) <= now:
                raise MediaMetadataConflictError(
                    "NFO migration preview expired; inspect the library again"
                )
            processable = [dict(item) for item in list(preview["processable"])]
            for item in processable:
                try:
                    relative = safe_relative_media_path(item["relative_media_path"])
                    media_file = root.joinpath(*PurePosixPath(relative).parts)
                    inspected = inspect_movie_nfo_title(
                        library_root=root,
                        media_file=media_file,
                        code=item["code"],
                    )
                except (MetadataPublishError, OSError) as exc:
                    raise MediaMetadataConflictError(
                        "NFO migration preview changed; inspect the library again"
                    ) from exc
                if inspected.sha256 != str(item["expected_sha256"]):
                    raise MediaMetadataConflictError(
                        "NFO migration preview changed; inspect the library again"
                    )
            run_id = (
                time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
                + f"-{uuid.uuid4().hex[:8]}"
            )
            backup_run_path = self.config.backup_path / run_id
            results = [dict(item) for item in list(preview["fixed_results"])]
            for item in processable:
                relative = safe_relative_media_path(item["relative_media_path"])
                media_file = root.joinpath(*PurePosixPath(relative).parts)
                base_result = {
                    key: value
                    for key, value in item.items()
                    if key != "expected_sha256"
                }
                try:
                    migrated = migrate_movie_nfo_title(
                        library_root=root,
                        media_file=media_file,
                        code=item["code"],
                        backup_root=self.config.backup_path,
                        backup_run_id=run_id,
                        expected_sha256=str(item["expected_sha256"]),
                    )
                    migrated_result = {
                        **base_result,
                        "nfo_path": migrated.nfo_path,
                        "status": migrated.status,
                    }
                    if migrated.backup_path is not None:
                        migrated_result["backup_path"] = migrated.backup_path
                    results.append(migrated_result)
                except MetadataMigrationError as exc:
                    failed_result = {
                        **base_result,
                        "status": "failed",
                        "error": "NFO migration failed",
                    }
                    if exc.backup_path is not None:
                        failed_result["backup_path"] = exc.backup_path
                    results.append(failed_result)
                except (MetadataPublishError, OSError):
                    results.append(
                        {
                            **base_result,
                            "status": "failed",
                            "error": "NFO changed after preview or migration failed",
                        }
                    )

        migrated_count = sum(item["status"] == "migrated" for item in results)
        current_count = sum(item["status"] == "current" for item in results)
        failed_count = sum(item["status"] == "failed" for item in results)
        skipped_count = len(results) - migrated_count - current_count - failed_count
        has_backup = any(item.get("backup_path") for item in results)
        migrated_path = next(
            (
                str(item["relative_media_path"])
                for item in results
                if item["status"] == "migrated"
            ),
            None,
        )
        if migrated_path is not None:
            self._notify_published(migrated_path)
        return {
            "scanned": int(preview["scanned"]),
            "migrated": migrated_count,
            "current": current_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "backup_path": backup_run_path.as_posix() if has_backup else None,
            "results": results,
        }

    def list(
        self,
        limit: int = 200,
        *,
        offset: int = 0,
        status_filter: str = "all",
        query: object | None = None,
    ) -> list[dict[str, object]]:
        return [
            self._public_job(job)
            for job in self.store.list(
                limit,
                offset=offset,
                status_filter=status_filter,
                query=query,
            )
        ]

    def count(
        self,
        *,
        status_filter: str = "all",
        query: object | None = None,
    ) -> int:
        return self.store.count(status_filter=status_filter, query=query)

    def summary(self) -> dict[str, int]:
        return self.store.summary()

    def retry(self, job_id: object) -> dict[str, object]:
        self._require_enabled()
        current = self.store.get(job_id)
        if (
            str(current["status"]) in {"completed", "failed", "retry"}
            and str(current["kind"]) == "qb"
            and not current.get("relative_media_path")
        ):
            try:
                snapshot = self._qb_client_factory().torrent_snapshot(
                    str(current["download_key"])
                )
            except DownloaderError as exc:
                raise MediaMetadataUnavailableError(
                    "qBittorrent is unavailable; metadata retry was not started"
                ) from exc
            if snapshot is None:
                raise MediaMetadataConflictError(
                    "qBittorrent task no longer exists; add the download again "
                    "before retrying metadata"
                )
        job = self.store.retry(job_id)
        self.notify()
        return self._public_job(job)

    def notify(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def shutdown(self, *, timeout: float = 45.0) -> bool:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._worker.ident is not None:
            self._worker.join(timeout=max(0.0, timeout))
        return not self._worker.is_alive()

    def process_once(self) -> bool:
        job = self.store.claim_ready()
        if job is None:
            return False
        self._process_job(job)
        return True

    def _dispatch(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                if not self.process_once():
                    with self._condition:
                        if self._stopping:
                            return
                        self._condition.wait(timeout=self.config.poll_seconds)
            except (OSError, sqlite3.Error, MediaMetadataStoreError):
                with self._condition:
                    if self._stopping:
                        return
                    self._condition.wait(timeout=self.config.poll_seconds)
            except Exception:
                LOGGER.exception("unexpected media metadata worker failure")
                with self._condition:
                    if self._stopping:
                        return
                    self._condition.wait(timeout=self.config.poll_seconds)

    def _process_job(self, job: dict[str, object]) -> None:
        job_id = str(job["job_id"])
        try:
            with self._lifecycle_lock:
                try:
                    media_file = self._resolve_media_file(job)
                except _MediaNotReady as exc:
                    self._wait_for_media(job, exc)
                    return
                root = _regular_library_root(self.config.library_path)
                resolved_relative = media_file.relative_to(root).as_posix()
                current_relative = job.get("relative_media_path")
                if current_relative and str(current_relative) != resolved_relative:
                    old_relative = safe_relative_media_path(current_relative)
                    observer_applied = False
                    try:
                        if self._on_archive_relocated is not None:
                            self._on_archive_relocated(
                                str(job["kind"]),
                                str(job["download_key"]),
                                old_relative,
                                resolved_relative,
                            )
                            observer_applied = True
                        job = self.store.relocate_media_path(
                            job_id,
                            old_relative,
                            resolved_relative,
                        )
                    except BaseException:
                        if observer_applied and self._on_archive_relocated is not None:
                            try:
                                self._on_archive_relocated(
                                    str(job["kind"]),
                                    str(job["download_key"]),
                                    resolved_relative,
                                    old_relative,
                                )
                            except Exception:
                                LOGGER.error(
                                    "archive reference recovery rollback failed"
                                )
                        raise
                else:
                    job = self.store.bind_media_path(job_id, resolved_relative)
        except (
            DownloaderError,
            MediaMetadataError,
            MediaMetadataStoreError,
            MetadataPublishError,
            QbPathError,
            OSError,
        ):
            self._retry_or_fail(job, "media file is not ready")
            return

        try:
            plan = inspect_movie_metadata_assets(
                library_root=self.config.library_path,
                media_file=media_file,
            )
        except MetadataPublishError:
            self._retry_or_fail(job, "metadata targets are temporarily unavailable")
            return
        if plan.complete:
            try:
                nfo = read_movie_nfo(media_file.with_suffix(".nfo"), root)
                with self._lifecycle_lock:
                    if nfo is not None:
                        media_file, assets = self._relocate_published_archive(
                            job,
                            media_file,
                            title=nfo.title,
                            release_date=nfo.release_date,
                            assets=plan.assets,
                        )
                    else:
                        assets = plan.assets
                    relative = media_file.relative_to(root).as_posix()
                    self.store.set_completed(
                        job_id,
                        relative_media_path=relative,
                        assets=assets,
                    )
                self._notify_published(relative)
            except (MetadataPublishError, MediaMetadataStoreError, OSError):
                self._retry_or_fail(job, "archive naming is temporarily unavailable")
            return
        if plan.has_conflict:
            self.store.set_failed(job_id, "metadata target is protected")
            return

        try:
            if self._metadata_resolver is None:
                metadata = resolve_media_metadata(str(job["code"]))
            else:
                metadata = self._metadata_resolver(str(job["code"]))
            if plan.needs_nfo and not metadata.description:
                try:
                    description = self._description_resolver(
                        str(job["code"]),
                        variant=job.get("variant")
                        or DEFAULT_WEB_DOWNLOAD_VARIANT,
                    )
                except Exception:
                    description = None
                if description:
                    metadata = _with_description(metadata, description)
            selection = (
                self._artwork_selector(
                    metadata.image_candidates,
                    need_portrait=plan.needs_portrait,
                    need_landscape=plan.needs_landscape,
                )
                if plan.needs_portrait or plan.needs_landscape
                else MetadataArtworkSelection(portrait=None, landscape=None)
            )
            artwork_missing = (
                (plan.needs_portrait and selection.portrait is None)
                or (plan.needs_landscape and selection.landscape is None)
            )
            portrait = _artwork(selection.portrait)
            landscape = _artwork(selection.landscape)
            with self._lifecycle_lock:
                self.store.get(job_id)
                published = publish_movie_metadata(
                    library_root=self.config.library_path,
                    media_file=media_file,
                    metadata=metadata,
                    portrait=portrait,
                    landscape=landscape,
                    variant=job.get("variant"),
                )
                media_file, assets = self._relocate_published_archive(
                    job,
                    media_file,
                    title=metadata.title,
                    release_date=metadata.release_date,
                    assets=published.assets,
                )
                relative = media_file.relative_to(root).as_posix()
                if artwork_missing:
                    # NFO/title publication is independent from remote artwork.
                    # Keep the job retryable so a later pass can fill missing
                    # images without leaving the media under a provisional name.
                    self._retry_or_fail(
                        job,
                        "metadata artwork is temporarily unavailable",
                        assets=assets,
                    )
                else:
                    self.store.set_completed(
                        job_id,
                        relative_media_path=relative,
                        assets=assets,
                    )
            self._notify_published(relative)
        except MediaMetadataNotFoundError:
            return
        except MediaMetadataNotFound:
            self._retry_or_fail(job, "metadata was not found")
        except MetadataPublishConflict:
            self.store.set_failed(job_id, "metadata target is protected")
        except (MediaMetadataSourceError, MetadataPublishError, OSError):
            self._retry_or_fail(job, "metadata is temporarily unavailable")
        except Exception:
            self._retry_or_fail(job, "metadata processing failed")

    def _notify_published(self, relative_media_path: str) -> None:
        if self._on_published is None:
            return
        try:
            self._on_published(relative_media_path)
        except Exception:
            LOGGER.warning("media metadata publication observer failed")

    def _relocate_published_archive(
        self,
        job: dict[str, object],
        media_file: Path,
        *,
        title: object | None,
        release_date: object | None,
        assets: dict[str, dict[str, object]],
    ) -> tuple[Path, dict[str, dict[str, object]]]:
        root = _regular_library_root(self.config.library_path)
        media = media_file.resolve(strict=True)
        try:
            layout = plan_archive_layout(
                code=job["code"],
                title=title,
                release_date=release_date,
                suffix=media.suffix,
                variant=job.get("variant"),
            )
        except ValueError as exc:
            raise MetadataPublishError("archive layout is invalid") from exc
        if not layout.ready:
            return media, assets
        target = root.joinpath(*layout.relative_media_path.parts)
        if target == media:
            return media, assets
        if media.parent != target.parent:
            # Cross-directory moves need to coordinate all sidecars and any
            # qBittorrent save-path mapping. The explicit library migration
            # handles those bundles while workers are paused.
            return media, assets

        old_relative = media.relative_to(root).as_posix()
        new_relative = target.relative_to(root).as_posix()
        old_nfo = media.with_suffix(".nfo")
        target_nfo = target.with_suffix(".nfo")
        media_identity = _archive_file_identity(media, root)
        nfo_identity = _archive_file_identity(old_nfo, root)
        _require_archive_target_absent(target, root)
        _require_archive_target_absent(target_nfo, root)
        intent = _ArchiveRelocationIntent(
            job_id=str(job["job_id"]),
            kind=str(job["kind"]),
            download_key=str(job["download_key"]),
            old_relative_path=old_relative,
            new_relative_path=new_relative,
            media_identity=media_identity,
            nfo_identity=nfo_identity,
        )
        _write_archive_relocation_intent(self.config.database_path.parent, intent)
        try:
            self._complete_archive_relocation(job, intent, root)
        except BaseException:
            try:
                self._rollback_archive_relocation(job, intent, root)
            except Exception:
                LOGGER.error("archive relocation rollback requires recovery")
            raise

        updated_assets = dict(assets)
        if old_nfo.name in updated_assets:
            updated_assets[target_nfo.name] = updated_assets.pop(old_nfo.name)
        return target, updated_assets

    def _recover_archive_relocations(self) -> None:
        root = _regular_library_root(self.config.library_path)
        for intent in _read_archive_relocation_intents(
            self.config.database_path.parent
        ):
            try:
                job = self.store.get(intent.job_id)
                if (
                    str(job["kind"]) != intent.kind
                    or str(job["download_key"]) != intent.download_key
                ):
                    raise MetadataPublishConflict(
                        "archive relocation ownership changed"
                    )
                self._complete_archive_relocation(job, intent, root)
            except Exception as exc:
                try:
                    job = self.store.get(intent.job_id)
                    self._rollback_archive_relocation(job, intent, root)
                except Exception as rollback_exc:
                    raise MediaMetadataUnavailableError(
                        "archive relocation recovery is incomplete"
                    ) from rollback_exc
                LOGGER.warning(
                    "rolled back interrupted archive relocation for %s",
                    intent.job_id,
                )
                if isinstance(exc, MediaMetadataStoreError):
                    continue

    def _complete_archive_relocation(
        self,
        job: dict[str, object],
        intent: _ArchiveRelocationIntent,
        root: Path,
    ) -> None:
        source = root.joinpath(*PurePosixPath(intent.old_relative_path).parts)
        target = root.joinpath(*PurePosixPath(intent.new_relative_path).parts)
        if source.parent != target.parent:
            raise MetadataPublishError("archive relocation directory changed")
        if intent.kind == "qb":
            self._ensure_qb_archive_target(job, source, target, intent, root)
        else:
            _ensure_archive_target(source, target, intent.media_identity, root)
        _ensure_archive_target(
            source.with_suffix(".nfo"),
            target.with_suffix(".nfo"),
            intent.nfo_identity,
            root,
        )
        intent = replace(intent, phase="files_published")
        _write_archive_relocation_intent(self.config.database_path.parent, intent)

        if self._on_archive_relocated is not None:
            self._on_archive_relocated(
                intent.kind,
                intent.download_key,
                intent.old_relative_path,
                intent.new_relative_path,
            )
        intent = replace(intent, phase="references_published")
        _write_archive_relocation_intent(self.config.database_path.parent, intent)
        self.store.relocate_media_path(
            intent.job_id,
            intent.old_relative_path,
            intent.new_relative_path,
        )
        _remove_archive_relocation_intent(
            self.config.database_path.parent,
            intent.job_id,
        )

    def _rollback_archive_relocation(
        self,
        job: dict[str, object],
        intent: _ArchiveRelocationIntent,
        root: Path,
    ) -> None:
        source = root.joinpath(*PurePosixPath(intent.old_relative_path).parts)
        target = root.joinpath(*PurePosixPath(intent.new_relative_path).parts)
        rollback_errors: list[Exception] = []
        if self._on_archive_relocated is not None:
            try:
                self._on_archive_relocated(
                    intent.kind,
                    intent.download_key,
                    intent.new_relative_path,
                    intent.old_relative_path,
                )
            except Exception as exc:
                rollback_errors.append(exc)
        try:
            self.store.relocate_media_path(
                intent.job_id,
                intent.new_relative_path,
                intent.old_relative_path,
            )
        except MediaMetadataStoreError as exc:
            rollback_errors.append(exc)
        try:
            _restore_archive_source(
                source.with_suffix(".nfo"),
                target.with_suffix(".nfo"),
                intent.nfo_identity,
                root,
            )
        except (MetadataPublishError, OSError) as exc:
            rollback_errors.append(exc)
        try:
            if intent.kind == "qb":
                self._ensure_qb_archive_target(job, target, source, intent, root)
            else:
                _restore_archive_source(
                    source,
                    target,
                    intent.media_identity,
                    root,
                )
        except (DownloaderError, MetadataPublishError, OSError) as exc:
            rollback_errors.append(exc)
        if rollback_errors:
            raise MetadataPublishError(
                "archive relocation rollback is incomplete"
            ) from (rollback_errors[0])
        _remove_archive_relocation_intent(
            self.config.database_path.parent,
            intent.job_id,
        )

    def _ensure_qb_archive_target(
        self,
        job: dict[str, object],
        source: Path,
        target: Path,
        intent: _ArchiveRelocationIntent,
        root: Path,
    ) -> None:
        expected = intent.media_identity
        source_identity = _optional_archive_file_identity(source, root)
        target_identity = _optional_archive_file_identity(target, root)
        if target_identity == expected and source_identity is None:
            return
        if source_identity != expected or target_identity is not None:
            raise MetadataPublishConflict("qBittorrent archive relocation is ambiguous")
        self._rename_qb_archive_file(job, source, target, root)
        _verify_archive_move(source, target, expected, root)

    def _rename_qb_archive_file(
        self,
        job: dict[str, object],
        source: Path,
        target: Path,
        root: Path,
    ) -> None:
        client = self._qb_client_factory()
        info_hash = str(job["download_key"])
        snapshot = client.torrent_snapshot(info_hash)
        if snapshot is None or not bool(snapshot.get("complete")):
            raise MetadataPublishError("qBittorrent archive is unavailable")
        config = client.config
        if not config.library_path or not config.app_library_path:
            raise MetadataPublishError("qBittorrent archive mapping is unavailable")
        save_path = PurePosixPath(
            normalize_qb_path(str(snapshot.get("save_path") or ""))
        )
        qb_library = PurePosixPath(normalize_qb_path(config.library_path))
        if not is_at_or_below(save_path, qb_library):
            raise MetadataPublishError("qBittorrent archive has not settled")
        app_library = _regular_library_root(Path(config.app_library_path))
        if not app_library.is_relative_to(root):
            raise MetadataPublishError("qBittorrent archive mapping is unsafe")
        mapped_save = app_library.joinpath(*save_path.relative_to(qb_library).parts)
        try:
            old_name = source.relative_to(mapped_save).as_posix()
            new_name = target.relative_to(mapped_save).as_posix()
        except ValueError as exc:
            raise MetadataPublishError(
                "qBittorrent archive target is outside the mapped save path"
            ) from exc
        names = {
            str(item.get("name") or "") for item in client.torrent_files(info_hash)
        }
        if old_name not in names:
            if new_name in names and target.exists() and not source.exists():
                return
            raise MetadataPublishError("qBittorrent archive file was not found")
        if new_name in names:
            raise MetadataPublishError("qBittorrent archive target is occupied")
        try:
            client.rename_torrent_file(info_hash, old_name, new_name)
        except DownloaderError as exc:
            refreshed = {
                str(item.get("name") or "") for item in client.torrent_files(info_hash)
            }
            if (
                new_name in refreshed
                and old_name not in refreshed
                and target.exists()
                and not source.exists()
            ):
                return
            if old_name in refreshed and new_name not in refreshed:
                raise
            raise MetadataPublishError(
                "qBittorrent archive rename outcome is ambiguous"
            ) from exc

    def _resolve_media_file(self, job: dict[str, object]) -> Path:
        root = _regular_library_root(self.config.library_path)
        relative = job.get("relative_media_path")
        if relative:
            clean = safe_relative_media_path(relative)
            target = root.joinpath(*PurePosixPath(clean).parts)
            if not target.exists():
                relocated = _recover_canonical_media_path(
                    root,
                    clean,
                    job["code"],
                    variant=job.get("variant"),
                )
                if relocated is not None:
                    return relocated
                reason = (
                    _WAIT_WEB_PATH_MISSING
                    if str(job["kind"]) == "web"
                    else _WAIT_KNOWN_PATH_MISSING
                )
                raise _MediaNotReady(reason)
            return target
        if str(job["kind"]) != "qb":
            raise _MediaNotReady(_WAIT_KNOWN_PATH_MISSING)
        client = self._qb_client_factory()
        try:
            snapshot = client.torrent_snapshot(str(job["download_key"]))
        except DownloaderError as exc:
            raise _MediaNotReady(_WAIT_QB_UNAVAILABLE) from exc
        if snapshot is None:
            raise _MediaNotReady(_WAIT_QB_MISSING)
        config = client.config
        if str(snapshot.get("category") or "") != str(config.category or "").strip():
            raise _MediaNotReady(_WAIT_QB_INCOMPLETE)
        if (
            not bool(snapshot.get("complete"))
            or str(snapshot.get("stage") or "") == "error"
        ):
            raise _MediaNotReady(_WAIT_QB_INCOMPLETE)
        save_path = PurePosixPath(
            normalize_qb_path(str(snapshot.get("save_path") or ""))
        )
        if not config.library_path:
            raise _MediaNotReady(_WAIT_QB_NOT_ARCHIVED)
        library = PurePosixPath(normalize_qb_path(config.library_path))
        if not is_at_or_below(save_path, library):
            raise _MediaNotReady(_WAIT_QB_NOT_ARCHIVED)
        app_library = _regular_library_root(Path(config.app_library_path))
        if not app_library.is_relative_to(root):
            raise QbPathError(
                "qBittorrent app library path is outside the metadata library root"
            )
        relative_save_path = save_path.relative_to(library)
        mapped_save_path = app_library.joinpath(*relative_save_path.parts)
        candidates: list[tuple[int, str, Path]] = []
        try:
            torrent_files = client.torrent_files(str(job["download_key"]))
        except DownloaderError as exc:
            raise _MediaNotReady(_WAIT_QB_UNAVAILABLE) from exc
        for item in torrent_files:
            name = str(item.get("name") or "")
            size = int(item.get("size") or 0)
            progress = float(item.get("progress") or 0)
            if (
                progress < 1.0
                or size <= 0
                or PurePosixPath(name).suffix.lower() not in VIDEO_SUFFIXES
            ):
                continue
            path = mapped_save_path.joinpath(*PurePosixPath(name).parts)
            try:
                path_stat = path.stat(follow_symlinks=False)
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if (
                path.is_symlink()
                or not resolved.is_relative_to(root)
                or not path_stat.st_size
                or path_stat.st_size != size
            ):
                continue
            candidates.append((size, resolved.relative_to(root).as_posix(), resolved))
        if not candidates:
            raise _MediaNotReady(_WAIT_QB_FILES_PENDING)
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    def _wait_for_media(
        self,
        job: dict[str, object],
        pending: _MediaNotReady,
    ) -> None:
        now = float(self._clock())
        job_id = str(job["job_id"])
        try:
            job = self.store.get(job_id)
        except MediaMetadataNotFoundError:
            return
        if pending.reason == _WAIT_QB_MISSING:
            created_at = float(job.get("created_at") or now)
            if now - created_at >= QB_MISSING_GRACE_SECONDS:
                self.store.delete_incomplete_qb((job["download_key"],))
                return
        failure = self._media_wait_failure(job, pending.reason, now)
        if failure is not None:
            self.store.set_failed(job_id, failure)
            return
        self.store.set_waiting(
            job_id,
            next_attempt_at=now + self._media_wait_delay(job_id),
        )

    def _media_wait_failure(
        self,
        job: dict[str, object],
        reason: str,
        now: float,
    ) -> str | None:
        if reason == _WAIT_WEB_PATH_MISSING:
            created_at = float(job.get("created_at") or now)
            if now - created_at >= WEB_PATH_MISSING_GRACE_SECONDS:
                return "Web media file was not found"
        return None

    def _media_wait_delay(self, job_id: str) -> float:
        base = max(MEDIA_WAIT_MIN_SECONDS, self.config.poll_seconds)
        digest = hashlib.sha256(job_id.encode("ascii", errors="ignore")).digest()
        return base + digest[0] % (MEDIA_WAIT_JITTER_SECONDS + 1)

    def _retry_or_fail(
        self,
        job: dict[str, object],
        message: str,
        *,
        assets: dict[str, dict[str, object]] | None = None,
    ) -> None:
        attempts = int(job.get("attempts") or 0)
        clean = _safe_error(message)
        try:
            if attempts >= self.config.max_attempts:
                self.store.set_failed(job["job_id"], clean, assets=assets)
                return
            delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (3 ** max(0, attempts - 1)))
            self.store.set_retry(
                job["job_id"],
                clean,
                next_attempt_at=time.time() + delay,
                assets=assets,
            )
        except MediaMetadataNotFoundError:
            return

    def _public_job(self, job: dict[str, object]) -> dict[str, object]:
        status = str(job["status"])
        orphaned_qb = (
            str(job["kind"]) == "qb"
            and not job.get("relative_media_path")
            and str(job.get("error") or "") == QB_TASK_MISSING_ERROR
        )
        return {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "code": job["code"],
            "variant": job.get("variant"),
            "status": status,
            "relative_media_path": job["relative_media_path"],
            "attempts": job["attempts"],
            "max_attempts": self.config.max_attempts,
            "next_attempt_at": job.get("next_attempt_at") if status == "retry" else None,
            "error": job["error"],
            "assets": job["assets"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "can_retry": status in {"completed", "failed", "retry"} and not orphaned_qb,
        }

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise MediaMetadataDisabledError("media metadata is disabled")


def _archive_file_identity(path: Path, root: Path) -> tuple[int, int, int, int]:
    try:
        file_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MetadataPublishError("archive file is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(file_stat.st_mode)
        or not resolved.is_relative_to(root)
    ):
        raise MetadataPublishError("archive file is unsafe")
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
    )


def _optional_archive_file_identity(
    path: Path,
    root: Path,
) -> tuple[int, int, int, int] | None:
    try:
        return _archive_file_identity(path, root)
    except MetadataPublishError:
        if not path.exists() and not path.is_symlink():
            return None
        raise


def _require_archive_target_absent(path: Path, root: Path) -> None:
    if not path.parent.resolve(strict=True).is_relative_to(root):
        raise MetadataPublishError("archive target is unsafe")
    if path.exists() or path.is_symlink():
        raise MetadataPublishConflict("archive target already exists")


def _move_archive_no_replace(
    source: Path,
    target: Path,
    expected: tuple[int, int, int, int],
    root: Path,
) -> None:
    if _archive_file_identity(source, root) != expected:
        raise MetadataPublishConflict("archive source changed before relocation")
    _require_archive_target_absent(target, root)
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise MetadataPublishConflict("archive target already exists") from exc
    except OSError as exc:
        raise MetadataPublishError("archive target could not be created") from exc
    try:
        if (
            _archive_file_identity(source, root) != expected
            or _archive_file_identity(target, root) != expected
        ):
            raise MetadataPublishConflict(
                "archive file changed while its target was created"
            )
        _fsync_archive_directory(source.parent)
        source.unlink()
        _fsync_archive_directory(source.parent)
        _verify_archive_move(source, target, expected, root)
    except BaseException:
        if (
            source.exists()
            and target.exists()
            and _optional_archive_file_identity(source, root) == expected
            and _optional_archive_file_identity(target, root) == expected
        ):
            try:
                target.unlink()
                _fsync_archive_directory(source.parent)
            except OSError:
                pass
        raise


def _ensure_archive_target(
    source: Path,
    target: Path,
    expected: tuple[int, int, int, int],
    root: Path,
) -> None:
    source_identity = _optional_archive_file_identity(source, root)
    target_identity = _optional_archive_file_identity(target, root)
    if target_identity == expected:
        if source_identity is None:
            return
        if source_identity == expected:
            source.unlink()
            _fsync_archive_directory(source.parent)
            _verify_archive_move(source, target, expected, root)
            return
        raise MetadataPublishConflict("archive source was replaced during relocation")
    if target_identity is not None or source_identity != expected:
        raise MetadataPublishConflict("archive relocation state is ambiguous")
    _move_archive_no_replace(source, target, expected, root)


def _restore_archive_source(
    source: Path,
    target: Path,
    expected: tuple[int, int, int, int],
    root: Path,
) -> None:
    source_identity = _optional_archive_file_identity(source, root)
    target_identity = _optional_archive_file_identity(target, root)
    if source_identity == expected:
        if target_identity == expected:
            target.unlink()
            _fsync_archive_directory(source.parent)
        return
    if source_identity is None and target_identity == expected:
        _move_archive_no_replace(target, source, expected, root)
        return
    if source_identity is None and target_identity is None:
        raise MetadataPublishConflict("archive relocation files are missing")
    raise MetadataPublishConflict("archive rollback would overwrite another file")


def _verify_archive_move(
    source: Path,
    target: Path,
    expected: tuple[int, int, int, int],
    root: Path,
) -> None:
    if source.exists() or source.is_symlink():
        raise MetadataPublishError("archive source remained after relocation")
    if _archive_file_identity(target, root) != expected:
        raise MetadataPublishError("archive identity changed during relocation")


def _fsync_archive_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _archive_relocation_directory(data_dir: Path, *, create: bool) -> Path:
    root = Path(data_dir)
    directory = root / _ARCHIVE_RELOCATION_DIRECTORY
    if not root.is_absolute():
        raise MediaMetadataUnavailableError("archive relocation storage is unavailable")
    if not root.exists() and not create:
        return directory
    if not root.exists() and create:
        root.mkdir(parents=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise MediaMetadataUnavailableError("archive relocation storage is unavailable")
    if not directory.exists() and create:
        directory.mkdir(mode=0o700)
        _fsync_archive_directory(root)
    if not directory.exists():
        return directory
    if directory.is_symlink() or not directory.is_dir():
        raise MediaMetadataUnavailableError("archive relocation storage is unsafe")
    return directory


def _archive_relocation_path(data_dir: Path, job_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{15,79}", job_id) is None:
        raise MediaMetadataUnavailableError("archive relocation identity is invalid")
    return _archive_relocation_directory(data_dir, create=True) / f"{job_id}.json"


def _write_archive_relocation_intent(
    data_dir: Path,
    intent: _ArchiveRelocationIntent,
) -> None:
    target = _archive_relocation_path(data_dir, intent.job_id)
    payload = {
        "revision": _ARCHIVE_RELOCATION_REVISION,
        "job_id": intent.job_id,
        "kind": intent.kind,
        "download_key": intent.download_key,
        "old_relative_path": intent.old_relative_path,
        "new_relative_path": intent.new_relative_path,
        "media_identity": list(intent.media_identity),
        "nfo_identity": list(intent.nfo_identity),
        "phase": intent.phase,
    }
    body = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(body) > _MAX_ARCHIVE_RELOCATION_BYTES:
        raise MediaMetadataUnavailableError("archive relocation journal is too large")
    temporary = target.parent / f".{intent.job_id}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as writer:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            writer.write(body)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
        _fsync_archive_directory(target.parent)
    finally:
        if temporary.exists() and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()


def _read_archive_relocation_intents(
    data_dir: Path,
) -> tuple[_ArchiveRelocationIntent, ...]:
    directory = _archive_relocation_directory(data_dir, create=False)
    if not directory.exists():
        return ()
    intents: list[_ArchiveRelocationIntent] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            entry_stat = entry.stat(follow_symlinks=False)
            if (
                not entry.name.endswith(".json")
                or stat.S_ISLNK(entry_stat.st_mode)
                or not stat.S_ISREG(entry_stat.st_mode)
                or not 0 < entry_stat.st_size <= _MAX_ARCHIVE_RELOCATION_BYTES
            ):
                raise MediaMetadataUnavailableError(
                    "archive relocation journal is unsafe"
                )
            try:
                payload = json.loads(Path(entry.path).read_text(encoding="ascii"))
                intent = _decode_archive_relocation_intent(payload)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise MediaMetadataUnavailableError(
                    "archive relocation journal is invalid"
                ) from exc
            if entry.name != f"{intent.job_id}.json":
                raise MediaMetadataUnavailableError(
                    "archive relocation journal identity is invalid"
                )
            intents.append(intent)
    return tuple(sorted(intents, key=lambda item: item.job_id))


def _decode_archive_relocation_intent(
    payload: object,
) -> _ArchiveRelocationIntent:
    expected_keys = {
        "revision",
        "job_id",
        "kind",
        "download_key",
        "old_relative_path",
        "new_relative_path",
        "media_identity",
        "nfo_identity",
        "phase",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError("archive relocation journal shape is invalid")
    if payload["revision"] != _ARCHIVE_RELOCATION_REVISION:
        raise ValueError("archive relocation journal revision is invalid")
    kind = str(payload["kind"])
    phase = str(payload["phase"])
    if kind not in {"qb", "web", "manual"} or phase not in {
        "prepared",
        "files_published",
        "references_published",
    }:
        raise ValueError("archive relocation journal state is invalid")
    old_path = safe_relative_media_path(payload["old_relative_path"])
    new_path = safe_relative_media_path(payload["new_relative_path"])
    if (
        old_path == new_path
        or PurePosixPath(old_path).parent != PurePosixPath(new_path).parent
    ):
        raise ValueError("archive relocation journal paths are invalid")
    return _ArchiveRelocationIntent(
        job_id=str(payload["job_id"]),
        kind=kind,
        download_key=str(payload["download_key"]),
        old_relative_path=old_path,
        new_relative_path=new_path,
        media_identity=_decode_archive_identity(payload["media_identity"]),
        nfo_identity=_decode_archive_identity(payload["nfo_identity"]),
        phase=phase,
    )


def _decode_archive_identity(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("archive relocation identity is invalid")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError("archive relocation identity is invalid")
    identity = tuple(value)
    if any(item < 0 for item in identity) or identity[2] <= 0:
        raise ValueError("archive relocation identity is invalid")
    return identity  # type: ignore[return-value]


def _remove_archive_relocation_intent(data_dir: Path, job_id: str) -> None:
    target = _archive_relocation_path(data_dir, job_id)
    if not target.exists():
        return
    if target.is_symlink() or not target.is_file():
        raise MediaMetadataUnavailableError("archive relocation journal is unsafe")
    target.unlink()
    _fsync_archive_directory(target.parent)


def _recover_canonical_media_path(
    root: Path,
    old_relative_path: str,
    code: object,
    *,
    variant: object | None = None,
) -> Path | None:
    old_relative = PurePosixPath(safe_relative_media_path(old_relative_path))
    expected_code_key = canonical_catalog_code(code, max_length=40)
    if expected_code_key is None:
        return None
    directory = root.joinpath(*old_relative.parent.parts)
    try:
        directory_stat = directory.lstat()
        resolved_directory = directory.resolve(strict=True)
    except OSError:
        return None
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(directory_stat.st_mode)
        or not resolved_directory.is_relative_to(root)
    ):
        raise MetadataPublishError("archive recovery directory is unsafe")
    candidates: list[Path] = []
    with os.scandir(resolved_directory) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            entry_stat = entry.stat(follow_symlinks=False)
            candidate = Path(entry.path)
            if (
                stat.S_ISLNK(entry_stat.st_mode)
                or not stat.S_ISREG(entry_stat.st_mode)
                or entry_stat.st_size <= 0
                or candidate.suffix.lower() not in VIDEO_SUFFIXES
            ):
                continue
            nfo = read_movie_nfo(candidate.with_suffix(".nfo"), root)
            if nfo is None or nfo.title is None or nfo.code_key != expected_code_key:
                continue
            try:
                layout = plan_archive_layout(
                    code=code,
                    title=nfo.title,
                    release_date=nfo.release_date,
                    suffix=candidate.suffix,
                    variant=variant,
                )
            except ValueError:
                continue
            if layout.ready and candidate.relative_to(root) == Path(
                *layout.relative_media_path.parts
            ):
                candidates.append(candidate.resolve(strict=True))
    return candidates[0] if len(candidates) == 1 else None


def discover_optional_description(
    code: str,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    command: Sequence[str] | None = None,
    timeout_seconds: float = DEFAULT_DESCRIPTION_TIMEOUT_SECONDS,
) -> str | None:
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError:
        return None
    task = json.dumps(
        {
            "code": str(code),
            "variant": clean_variant,
            "timeout_seconds": timeout_seconds,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    worker = tuple(command) if command is not None else _description_worker_command()
    if not worker:
        return None
    process: subprocess.Popen[bytes] | None = None
    try:
        kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(list(worker), **kwargs)
        stdout, _ = process.communicate(input=task, timeout=timeout_seconds + 12.0)
    except subprocess.TimeoutExpired:
        if process is not None:
            _terminate_process_group(process)
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        if process is not None:
            _terminate_process_group(process)
        return None
    if process.returncode not in {0, 1, 3} or len(stdout) > _DESCRIPTION_OUTPUT_BYTES:
        return None
    try:
        response = json.loads(
            stdout.decode("ascii"),
            object_pairs_hook=_reject_duplicate_description_keys,
            parse_constant=_reject_description_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(response, dict) or set(response) != {
        "code",
        "variant",
        "description",
        "error",
    }:
        return None
    if canonical_catalog_code(
        response.get("code"), max_length=32
    ) != canonical_catalog_code(code, max_length=32):
        return None
    if response.get("variant") != clean_variant:
        return None
    description = response.get("description")
    if response.get("error") is not None or description is None:
        return None
    clean = " ".join(str(description).split())
    return clean[:8192] if clean else None


def _reject_duplicate_description_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_description_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _description_worker_command() -> tuple[str, ...] | None:
    worker = (sys.executable, "-m", "jav_pilot.media_metadata.description_worker")
    if os.name == "nt" or sys.platform == "darwin" or os.environ.get("DISPLAY"):
        return worker
    xvfb_run = shutil.which("xvfb-run")
    return (xvfb_run, "-a", *worker) if xvfb_run else None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, ValueError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _with_description(metadata: MediaMetadata, description: str) -> MediaMetadata:
    return MediaMetadata(
        code=metadata.code,
        title=metadata.title,
        original_title=metadata.original_title,
        release_date=metadata.release_date,
        duration_minutes=metadata.duration_minutes,
        rating=metadata.rating,
        makers=metadata.makers,
        publishers=metadata.publishers,
        series=metadata.series,
        directors=metadata.directors,
        actors=metadata.actors,
        tags=metadata.tags,
        description=description,
        image_candidates=metadata.image_candidates,
    )


def _artwork(value: object | None) -> Artwork | None:
    if value is None:
        return None
    return Artwork(
        body=bytes(getattr(value, "body")),
        width=int(getattr(value, "width")),
        height=int(getattr(value, "height")),
        source_id=str(getattr(value, "source_id")),
    )


def _regular_library_root(path: Path) -> Path:
    try:
        if path.is_symlink() or not path.is_dir():
            raise MediaMetadataUnavailableError("metadata library is unavailable")
        return path.resolve(strict=True)
    except OSError as exc:
        raise MediaMetadataUnavailableError("metadata library is unavailable") from exc


def _safe_error(value: object) -> str:
    clean = " ".join(str(value or "metadata processing failed").split())
    clean = _SAFE_ERROR_RE.sub("<redacted>", clean)
    lowered = clean.casefold()
    if any(
        marker in lowered
        for marker in (
            "cookie",
            "header",
            "manifest",
            "password",
            "secret",
            "token",
            "url",
        )
    ):
        return "metadata processing failed"
    return clean[:500] or "metadata processing failed"


def _collect_library_media_candidates(
    root: Path,
    *,
    requested_key: str | None,
    unidentified: list[str] | None = None,
) -> dict[
    tuple[str, str, str],
    tuple[int, str, str, MissavVariant | None],
]:
    candidates: dict[
        tuple[str, str, str],
        tuple[int, str, str, MissavVariant | None],
    ] = {}
    for media_file, size in _scan_library_media_files(root):
        detected = _scan_media_identity(
            media_file,
            root,
            requested_key=requested_key,
        )
        if detected is None:
            if unidentified is not None and requested_key is None:
                try:
                    unidentified.append(media_file.relative_to(root).as_posix())
                except ValueError:
                    pass
            continue
        display_code, entry_key, scope = detected
        try:
            relative = media_file.relative_to(root).as_posix()
        except ValueError:
            continue
        variant = web_download_variant_from_stem(media_file.name)
        group_key = (entry_key, scope, variant or "")
        current = candidates.get(group_key)
        if current is None or (-size, relative) < (-current[0], current[1]):
            candidates[group_key] = (size, relative, display_code, variant)
        if len(candidates) > MAX_SCAN_CANDIDATES:
            raise MediaMetadataUnavailableError(
                "metadata library contains too many media candidates"
            )
    return candidates


def _scan_library_media_files(root: Path) -> Iterator[tuple[Path, int]]:
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_SCAN_ENUM_ENTRIES:
                        raise MediaMetadataUnavailableError(
                            "metadata library contains too many entries"
                        )
                    if entry.name.startswith("."):
                        continue
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode):
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if depth < MAX_SCAN_DEPTH:
                            pending.append((path, depth + 1))
                        continue
                    if (
                        stat.S_ISREG(entry_stat.st_mode)
                        and path.suffix.lower() in VIDEO_SUFFIXES
                        and entry_stat.st_size > 0
                    ):
                        yield path, entry_stat.st_size
        except MediaMetadataUnavailableError:
            raise
        except OSError as exc:
            if directory == root:
                raise MediaMetadataUnavailableError(
                    "metadata library is unavailable"
                ) from exc


def _scan_media_identity(
    media_file: Path,
    root: Path,
    *,
    requested_key: str | None,
) -> tuple[str, str, str] | None:
    file_identity = _scan_media_code(media_file.stem)
    parent = media_file.parent
    while parent != root:
        detected = _scan_media_code(parent.name)
        if detected is not None:
            if file_identity is not None and file_identity[1] != detected[1]:
                if not _starts_with_catalog_code(media_file.stem, detected[1]):
                    return None
            if requested_key is not None and detected[1] != requested_key:
                return None
            try:
                scope = parent.relative_to(root).as_posix()
            except ValueError:
                return None
            return detected[0], detected[1], scope
        parent = parent.parent
    if file_identity is None or (
        requested_key is not None and file_identity[1] != requested_key
    ):
        return None
    try:
        scope = media_file.relative_to(root).as_posix()
    except ValueError:
        return None
    return file_identity[0], file_identity[1], scope


def _scan_media_code(
    value: object,
    *,
    requested_key: str | None = None,
) -> tuple[str, str] | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not normalized:
        return None

    def candidate(raw: str) -> tuple[str, str] | None:
        normalized_code = normalize_catalog_code(
            re.sub(r"\s+", "-", raw.strip().upper()),
            max_length=40,
        )
        if normalized_code is None:
            return None
        display, code_key = normalized_code
        alpha_count = sum(character.isalpha() for character in display)
        if (
            sum(character.isdigit() for character in display) < 2
            or _SCAN_LAYOUT_NAME_RE.fullmatch(display)
            or (
                not any(separator in display for separator in "-._") and alpha_count < 2
            )
        ):
            return None
        return display, code_key

    exact = candidate(normalized) if normalized[-1:].isdigit() else None
    if exact is not None and (requested_key is None or exact[1] == requested_key):
        return exact

    matches = [
        (
            parsed,
            sum(character in "-._" for character in match.group(0)),
            match.start(),
        )
        for match in _SCAN_CODE_RE.finditer(normalized)
        if (parsed := candidate(match.group(0))) is not None
    ]
    if requested_key is not None:
        return next(
            (item for item, _, _ in matches if item[1] == requested_key),
            None,
        )
    if not matches:
        return None
    return max(matches, key=lambda item: (item[1], -item[2]))[0]


def _starts_with_catalog_code(value: object, code_key: str) -> bool:
    candidate: list[str] = []
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
    for index, character in enumerate(normalized):
        if character.isascii() and character.isalnum():
            candidate.append(character)
            joined = "".join(candidate)
            if not code_key.startswith(joined):
                return False
            if joined == code_key:
                return (
                    index + 1 == len(normalized) or not normalized[index + 1].isalnum()
                )
        elif not candidate:
            return False
    return False


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _bounded_float(
    value: object,
    *,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


__all__ = [
    "MediaMetadataConfig",
    "MediaMetadataDisabledError",
    "MediaMetadataError",
    "MediaMetadataManager",
    "MediaMetadataUnavailableError",
    "discover_optional_description",
]
