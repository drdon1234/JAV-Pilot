"""Media library snapshots used to deduplicate batch items."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Callable

from ...core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..archives import (
    archive_root_matches as _archive_root_matches,
    archive_root_snapshot as _archive_root_snapshot,
    archived_file_probe_in_root as _archived_file_probe_in_root,
)
from ..jobs import ARCHIVE_AVAILABLE, PROVIDER
from ..variant import (
    MissavVariant,
    normalize_web_download_variant,
    web_download_variant_from_stem,
)
from .errors import WebDownloadBatchError, WebDownloadBatchUnavailableError
from .intents import existing_work_height, optional_existing_height
from .models import ExistingWork, LibraryDeduplicationSnapshot

MAX_LIBRARY_DEDUP_ROWS = 100_000


_NFO_ID_RE = re.compile(rb"<id>\s*([^<&]{3,64})\s*</id>", re.IGNORECASE)


class LibrarySnapshotChanged(WebDownloadBatchError):
    pass


def _media_library_database_path(web_download_database: Path) -> Path:
    configured = os.environ.get("JAV_PILOT_MEDIA_LIBRARY_DATABASE_PATH", "").strip()
    database = (
        Path(configured).expanduser()
        if configured
        else web_download_database.with_name("media_library.sqlite3")
    )
    if not database.is_absolute():
        raise WebDownloadBatchError("media library index database path is invalid")
    return database


def _media_library_root_key(library_root: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(library_root))
    return hashlib.sha256(os.fsencode(normalized)).hexdigest()


def _read_media_library_revision(database: Path, root_key: str) -> str:
    try:
        with closing(
            sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro",
                timeout=5.0,
                isolation_level=None,
                uri=True,
            )
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            row = connection.execute(
                "SELECT state, published_generation_id, revision "
                "FROM media_library_roots WHERE root_key = ?",
                (root_key,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise WebDownloadBatchUnavailableError(
            "media library index is unavailable"
        ) from exc
    if (
        row is None
        or str(row[0]) != "available"
        or row[1] is None
        or not isinstance(row[2], int)
    ):
        raise WebDownloadBatchUnavailableError("media library index is unavailable")
    return f"{root_key}:{int(row[2])}:{str(row[1])}"


def current_media_library_revision(
    library_root: Path,
    web_download_database: Path,
) -> str | None:
    database = _media_library_database_path(web_download_database)
    if not database.is_file():
        return None
    try:
        return _read_media_library_revision(
            database,
            _media_library_root_key(library_root),
        )
    except WebDownloadBatchUnavailableError:
        # The index is still building its first generation, or its root is
        # awaiting a rebuild after a remount. Deduplication then reads the
        # library directly, exactly as before the index existed, instead of
        # refusing every Web download until the index recovers.
        return None


def _indexed_library_works(
    library_root: Path,
    web_download_database: Path,
) -> (
    tuple[
        set[str],
        dict[str, ExistingWork],
        set[tuple[str, MissavVariant]],
        dict[tuple[str, MissavVariant], ExistingWork],
        set[str],
        str,
        str,
        Path,
        str,
    ]
    | None
):
    database = _media_library_database_path(web_download_database)
    if not database.is_file():
        return None
    root_key = _media_library_root_key(library_root)
    try:
        from ...library.dedup import read_media_library_dedup_snapshot
        from ...library.errors import (
            MediaLibraryCapacityError,
            MediaLibraryError,
            MediaLibraryUnavailableError,
        )

        snapshot = read_media_library_dedup_snapshot(
            database,
            root_key,
            max_rows=MAX_LIBRARY_DEDUP_ROWS,
        )
        revision = snapshot.revision_token
        generation_id = snapshot.generation_id
        items = snapshot.records
    except WebDownloadBatchError:
        raise
    except MediaLibraryUnavailableError:
        # Unpublished or root-changed index: use the direct library scan.
        return None
    except MediaLibraryCapacityError as exc:
        raise WebDownloadBatchError(str(exc)) from exc
    except (
        OSError,
        sqlite3.Error,
        MediaLibraryError,
    ) as exc:
        raise WebDownloadBatchUnavailableError(
            "media library index is unavailable"
        ) from exc

    code_keys: set[str] = set()
    existing_by_code: dict[str, ExistingWork] = {}
    variant_keys: set[tuple[str, MissavVariant]] = set()
    existing_by_variant: dict[tuple[str, MissavVariant], ExistingWork] = {}
    unknown_code_keys: set[str] = set()
    for item in items:
        code_key = canonical_catalog_code(item.code_key, max_length=40)
        if code_key is None:
            continue
        relative_path = str(item.primary_media_path or "")
        pure_path = PurePosixPath(relative_path)
        if (
            not relative_path
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or "\\" in relative_path
        ):
            raise WebDownloadBatchError("media library index is inconsistent")
        raw_variant = item.variant
        try:
            variant = (
                None
                if raw_variant is None
                else normalize_web_download_variant(raw_variant)
            )
        except ValueError as exc:
            raise WebDownloadBatchError("media library index is inconsistent") from exc
        height = optional_existing_height(item.quality_height)
        candidate = ExistingWork(relative_path, None, height, height, variant)
        current = existing_by_code.get(code_key)
        if current is None or (height or -1) > (existing_work_height(current) or -1):
            existing_by_code[code_key] = candidate
        if variant is None:
            unknown_code_keys.add(code_key)
        else:
            work_key = (code_key, variant)
            current_variant = existing_by_variant.get(work_key)
            if current_variant is None or (height or -1) > (
                existing_work_height(current_variant) or -1
            ):
                existing_by_variant[work_key] = candidate
            variant_keys.add(work_key)
        code_keys.add(code_key)
    if revision != _read_media_library_revision(database, root_key):
        raise WebDownloadBatchError(
            "media library changed while existing works were inspected"
        )
    return (
        code_keys,
        existing_by_code,
        variant_keys,
        existing_by_variant,
        unknown_code_keys,
        revision,
        generation_id,
        database,
        root_key,
    )


def _legacy_library_works(
    resolved_root: Path,
    web_download_database: Path,
) -> tuple[
    set[str],
    dict[str, ExistingWork],
    set[tuple[str, MissavVariant]],
    dict[tuple[str, MissavVariant], ExistingWork],
    set[str],
    str,
]:
    try:
        from ...media_metadata.manager import (
            MAX_SCAN_DEPTH,
            MAX_SCAN_ENUM_ENTRIES,
            MediaMetadataUnavailableError,
            _scan_media_identity,
        )
        from ...media_metadata.publish import (
            MAX_NFO_BYTES,
            VIDEO_SUFFIXES,
            _verified_legacy_movie_nfo,
        )

        code_keys: set[str] = set()
        existing_by_code: dict[str, ExistingWork] = {}
        variant_keys: set[tuple[str, MissavVariant]] = set()
        existing_by_variant: dict[tuple[str, MissavVariant], ExistingWork] = {}
        unknown_code_keys: set[str] = set()
        tree_revision, media_files = _library_tree_state(
            resolved_root,
            max_depth=MAX_SCAN_DEPTH,
            max_entries=MAX_SCAN_ENUM_ENTRIES,
            video_suffixes=VIDEO_SUFFIXES,
            collect_media=True,
        )
        for media_file, _size in media_files:
            try:
                relative_path = media_file.relative_to(resolved_root).as_posix()
            except ValueError:
                continue
            detected = _scan_media_identity(
                media_file,
                resolved_root,
                requested_key=None,
            )
            if detected is not None:
                code_key = detected[1]
                variant = web_download_variant_from_stem(media_file.name)
                candidate = ExistingWork(
                    relative_path,
                    None,
                    None,
                    None,
                    variant,
                )
                code_keys.add(code_key)
                existing_by_code.setdefault(
                    code_key,
                    candidate,
                )
                if variant is None:
                    unknown_code_keys.add(code_key)
                else:
                    work_key = (code_key, variant)
                    variant_keys.add(work_key)
                    existing_by_variant.setdefault(work_key, candidate)
                continue
            nfo_code_key = _verified_nfo_code_key(
                media_file.with_suffix(".nfo"),
                max_bytes=MAX_NFO_BYTES,
                verify=_verified_legacy_movie_nfo,
            )
            if nfo_code_key is not None:
                variant = web_download_variant_from_stem(media_file.name)
                candidate = ExistingWork(
                    relative_path,
                    None,
                    None,
                    None,
                    variant,
                )
                code_keys.add(nfo_code_key)
                existing_by_code.setdefault(
                    nfo_code_key,
                    candidate,
                )
                if variant is None:
                    unknown_code_keys.add(nfo_code_key)
                else:
                    work_key = (nfo_code_key, variant)
                    variant_keys.add(work_key)
                    existing_by_variant.setdefault(work_key, candidate)
    except (OSError, MediaMetadataUnavailableError) as exc:
        raise WebDownloadBatchUnavailableError(
            "media library could not be inspected for existing works"
        ) from exc

    metadata_database = Path(
        os.environ.get(
            "JAV_PILOT_MEDIA_METADATA_DATABASE_PATH",
            str(web_download_database.with_name("media_metadata.sqlite3")),
        )
    )
    if not metadata_database.is_absolute():
        raise WebDownloadBatchError("media metadata database path is invalid")
    try:
        if metadata_database.is_file():
            with closing(sqlite3.connect(metadata_database, timeout=5.0)) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
                ).fetchone()
                rows = (
                    connection.execute(
                        "SELECT code_key, relative_media_path FROM jobs "
                        "WHERE relative_media_path IS NOT NULL LIMIT ?",
                        (MAX_LIBRARY_DEDUP_ROWS + 1,),
                    ).fetchall()
                    if table is not None
                    else ()
                )
            if len(rows) > MAX_LIBRARY_DEDUP_ROWS:
                raise WebDownloadBatchError(
                    "media library metadata contains too many records"
                )
            for raw_code_key, relative_path in rows:
                code_key = canonical_catalog_code(raw_code_key, max_length=40)
                if code_key is None:
                    continue
                if (
                    PurePosixPath(str(relative_path or "")).suffix.lower()
                    in VIDEO_SUFFIXES
                    and _archived_file_probe_in_root(resolved_root, relative_path)[0]
                    == ARCHIVE_AVAILABLE
                ):
                    normalized_path = str(relative_path)
                    variant = web_download_variant_from_stem(
                        PurePosixPath(normalized_path).name
                    )
                    candidate = ExistingWork(
                        normalized_path,
                        None,
                        None,
                        None,
                        variant,
                    )
                    code_keys.add(code_key)
                    existing_by_code.setdefault(
                        code_key,
                        candidate,
                    )
                    if variant is None:
                        unknown_code_keys.add(code_key)
                    else:
                        work_key = (code_key, variant)
                        variant_keys.add(work_key)
                        existing_by_variant.setdefault(work_key, candidate)
    except (OSError, sqlite3.Error) as exc:
        raise WebDownloadBatchUnavailableError(
            "media library metadata could not be inspected"
        ) from exc
    return (
        code_keys,
        existing_by_code,
        variant_keys,
        existing_by_variant,
        unknown_code_keys,
        tree_revision,
    )


def library_deduplication_snapshot(
    library_root: Path,
    web_download_database: Path,
) -> LibraryDeduplicationSnapshot:
    root_snapshot = _archive_root_snapshot(library_root)
    if root_snapshot is None:
        raise WebDownloadBatchUnavailableError(
            "media library could not be inspected for existing works"
        )
    resolved_root = root_snapshot[0]
    indexed = _indexed_library_works(library_root, web_download_database)
    if indexed is None:
        (
            code_keys,
            existing_by_code,
            variant_keys,
            existing_by_variant,
            unknown_code_keys,
            tree_revision,
        ) = _legacy_library_works(resolved_root, web_download_database)
        index_revision = None
        index_generation_id = None
        index_database_path = None
        index_root_key = None
    else:
        (
            code_keys,
            existing_by_code,
            variant_keys,
            existing_by_variant,
            unknown_code_keys,
            index_revision,
            index_generation_id,
            index_database_path,
            index_root_key,
        ) = indexed
        tree_revision = None

    completed_job_ids: set[str] = set()
    observed_completed_job_ids: set[str] = set()
    try:
        with closing(sqlite3.connect(web_download_database, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT job_id, code_key, variant, output_path, verified_height, "
                "selected_height FROM web_download_jobs "
                "WHERE provider = ? AND status = 'completed' "
                "AND superseded_by_job_id IS NULL "
                "ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (PROVIDER, MAX_LIBRARY_DEDUP_ROWS + 1),
            ).fetchall()
        if len(rows) > MAX_LIBRARY_DEDUP_ROWS:
            raise WebDownloadBatchError(
                "completed download history contains too many records"
            )
        for row in rows:
            observed_completed_job_ids.add(str(row["job_id"]))
            if (
                _archived_file_probe_in_root(resolved_root, row["output_path"])[0]
                != ARCHIVE_AVAILABLE
            ):
                continue
            completed_job_ids.add(str(row["job_id"]))
            code_key = canonical_catalog_code(row["code_key"], max_length=40)
            if code_key is not None:
                try:
                    variant = normalize_web_download_variant(row["variant"])
                except ValueError as exc:
                    raise WebDownloadBatchError(
                        "completed download variant is invalid"
                    ) from exc
                work_key = (code_key, variant)
                code_keys.add(code_key)
                current = existing_by_variant.get(work_key)
                completed_work = ExistingWork(
                    output_path=str(row["output_path"]),
                    job_id=str(row["job_id"]),
                    verified_height=optional_existing_height(row["verified_height"]),
                    selected_height=optional_existing_height(row["selected_height"]),
                    variant=variant,
                )
                current_height = existing_work_height(current)
                completed_height = existing_work_height(completed_work)
                if (
                    current is None
                    or current_height is None
                    or (
                        completed_height is not None
                        and completed_height >= current_height
                    )
                ):
                    existing_by_variant[work_key] = completed_work
                current_code = existing_by_code.get(code_key)
                current_code_height = existing_work_height(current_code)
                if (
                    current_code is None
                    or current_code_height is None
                    or (
                        completed_height is not None
                        and completed_height >= current_code_height
                    )
                ):
                    existing_by_code[code_key] = completed_work
                variant_keys.add(work_key)
    except (OSError, sqlite3.Error) as exc:
        raise WebDownloadBatchUnavailableError(
            "completed downloads could not be inspected"
        ) from exc
    if not _archive_root_matches(library_root, root_snapshot):
        raise WebDownloadBatchError(
            "media library changed while existing works were inspected"
        )
    return LibraryDeduplicationSnapshot(
        code_keys=frozenset(code_keys),
        completed_job_ids=frozenset(completed_job_ids),
        existing_by_code=existing_by_code,
        variant_keys=frozenset(variant_keys),
        existing_by_variant=existing_by_variant,
        unknown_code_keys=frozenset(unknown_code_keys),
        observed_completed_job_ids=frozenset(observed_completed_job_ids),
        resolved_library_root=resolved_root,
        library_root_identity=root_snapshot[1],
        tree_revision=tree_revision,
        index_revision=index_revision,
        index_generation_id=index_generation_id,
        index_database_path=index_database_path,
        index_root_key=index_root_key,
    )


def require_current_library_snapshot(snapshot: LibraryDeduplicationSnapshot) -> None:
    root = snapshot.resolved_library_root
    root_identity = snapshot.library_root_identity
    revision = snapshot.tree_revision
    index_revision = snapshot.index_revision
    if (
        root is None
        and root_identity is None
        and revision is None
        and index_revision is None
    ):
        return
    if root is None or root_identity is None:
        raise LibrarySnapshotChanged("media library snapshot is invalid")
    if index_revision is not None:
        database = snapshot.index_database_path
        root_key = snapshot.index_root_key
        if database is None or root_key is None or revision is not None:
            raise LibrarySnapshotChanged("media library snapshot is invalid")
        current_root = _archive_root_snapshot(root)
        if current_root != (root, root_identity):
            raise LibrarySnapshotChanged(
                "media library root changed while existing works were inspected"
            )
        try:
            current_revision = _read_media_library_revision(database, root_key)
        except WebDownloadBatchError as exc:
            raise LibrarySnapshotChanged("media library index is unavailable") from exc
        if not hmac.compare_digest(current_revision, index_revision):
            raise LibrarySnapshotChanged(
                "media library changed while existing works were inspected"
            )
        return
    if revision is None:
        raise LibrarySnapshotChanged("media library snapshot is invalid")
    try:
        from ...media_metadata.manager import MAX_SCAN_DEPTH, MAX_SCAN_ENUM_ENTRIES
        from ...media_metadata.publish import VIDEO_SUFFIXES

        current_root = _archive_root_snapshot(root)
        if current_root != (root, root_identity):
            raise LibrarySnapshotChanged(
                "media library root changed while existing works were inspected"
            )
        current_revision, _media_files = _library_tree_state(
            root,
            max_depth=MAX_SCAN_DEPTH,
            max_entries=MAX_SCAN_ENUM_ENTRIES,
            video_suffixes=VIDEO_SUFFIXES,
            collect_media=False,
        )
    except LibrarySnapshotChanged:
        raise
    except (OSError, WebDownloadBatchError) as exc:
        raise LibrarySnapshotChanged("media library could not be revalidated") from exc
    if not hmac.compare_digest(current_revision, revision):
        raise LibrarySnapshotChanged(
            "media library changed while existing works were inspected"
        )


def _library_tree_state(
    root: Path,
    *,
    max_depth: int,
    max_entries: int,
    video_suffixes: frozenset[str],
    collect_media: bool,
) -> tuple[str, tuple[tuple[Path, int], ...]]:
    digest = hashlib.sha256()
    media_files: list[tuple[Path, int]] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = 0

    def update_digest(*parts: object) -> None:
        for part in parts:
            encoded = os.fsencode(str(part))
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)

    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as raw_entries:
                entries = []
                for entry in raw_entries:
                    visited += 1
                    if visited > max_entries:
                        raise WebDownloadBatchError(
                            "media library contains too many entries"
                        )
                    entries.append(entry)
            entries.sort(key=lambda entry: os.fsencode(entry.name))
            child_directories: list[Path] = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(entry_stat.st_mode):
                    continue
                path = Path(entry.path)
                relative_path = path.relative_to(root).as_posix()
                if stat.S_ISDIR(entry_stat.st_mode):
                    update_digest(
                        "directory",
                        relative_path,
                        entry_stat.st_dev,
                        entry_stat.st_ino,
                    )
                    if depth < max_depth:
                        child_directories.append(path)
                    continue
                suffix = path.suffix.lower()
                if not stat.S_ISREG(entry_stat.st_mode) or (
                    suffix not in video_suffixes and suffix != ".nfo"
                ):
                    continue
                update_digest(
                    "file",
                    relative_path,
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                    entry_stat.st_ctime_ns,
                )
                if (
                    collect_media
                    and suffix in video_suffixes
                    and entry_stat.st_size > 0
                ):
                    media_files.append((path, entry_stat.st_size))
            try:
                directory_stat = directory.stat(follow_symlinks=False)
            except OSError:
                directory_stat = None
            if directory_stat is not None:
                update_digest(
                    "directory-state",
                    directory.relative_to(root).as_posix(),
                    directory_stat.st_dev,
                    directory_stat.st_ino,
                    directory_stat.st_mtime_ns,
                    directory_stat.st_ctime_ns,
                )
            pending.extend(
                (child_directory, depth + 1)
                for child_directory in reversed(child_directories)
            )
        except WebDownloadBatchError:
            raise
        except OSError as exc:
            if directory == root:
                raise WebDownloadBatchUnavailableError(
                    "media library is unavailable"
                ) from exc
    return digest.hexdigest(), tuple(media_files)


def _verified_nfo_code_key(
    nfo_path: Path,
    *,
    max_bytes: int,
    verify: Callable[[bytes, str], object | None],
) -> str | None:
    try:
        if nfo_path.is_symlink():
            return None
        file_stat = nfo_path.stat()
        if not 0 < file_stat.st_size <= max_bytes:
            return None
        body = nfo_path.read_bytes()
        if len(body) != file_stat.st_size:
            return None
    except OSError:
        return None
    match = _NFO_ID_RE.search(body)
    if match is None:
        return None
    try:
        raw_code = match.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = normalize_catalog_code(raw_code, max_length=40)
    if normalized is None or verify(body, normalized[1]) is None:
        return None
    return normalized[1]
