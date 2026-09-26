from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from ..core.catalog_code import normalize_catalog_code
from ..library.archive import plan_archive_layout
from ..library.models import VIDEO_SUFFIXES
from ..library.nfo import parse_movie_nfo
from .lock import MaintenanceFileLock
from ..web_download.variant import web_download_variant_from_stem


JOURNAL_NAME = "archive_migration_journal.json"
JOURNAL_REVISION = 1
MAX_SCAN_DEPTH = 16
MAX_SCAN_ENTRIES = 1_000_000
MAX_ARCHIVE_IMAGE_BYTES = 64 * 1024 * 1024
ARCHIVE_RELOCATION_DIRECTORY = "archive_relocations"
ARCHIVE_PUBLICATION_ARTIFACT_SUFFIXES = (".part", ".backup", ".failed")
ARCHIVE_IMAGE_NAMES = (
    "poster.jpg",
    "fanart.jpg",
    "backdrop.jpg",
    "landscape.jpg",
    "thumb.jpg",
)


class ArchiveMigrationError(RuntimeError):
    pass


class ArchiveMigrationConflict(ArchiveMigrationError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveMigrationConfig:
    library_root: Path
    data_dir: Path
    web_database: Path
    metadata_database: Path
    media_library_database: Path

    @property
    def journal_path(self) -> Path:
        return self.data_dir / JOURNAL_NAME

    @classmethod
    def from_environment(
        cls,
        *,
        library_root: Path | str | None = None,
        data_dir: Path | str | None = None,
        web_database: Path | str | None = None,
        metadata_database: Path | str | None = None,
        media_library_database: Path | str | None = None,
    ) -> ArchiveMigrationConfig:
        web = Path(
            web_database
            or os.environ.get(
                "JAV_PILOT_WEB_DOWNLOAD_DATABASE_PATH",
                "/app/data/web_downloads.sqlite3",
            )
        )
        data = Path(
            data_dir
            or os.environ.get("JAV_PILOT_ARCHIVE_MIGRATION_DATA_PATH", "")
            or web.parent
        )
        library = Path(
            library_root
            or os.environ.get("JAV_PILOT_MEDIA_LIBRARY_PATH", "")
            or os.environ.get("JAV_PILOT_WEB_DOWNLOAD_LIBRARY_PATH", "/media/JAV")
        )
        return cls(
            library_root=library,
            data_dir=data,
            web_database=web,
            metadata_database=Path(
                metadata_database
                or os.environ.get(
                    "JAV_PILOT_MEDIA_METADATA_DATABASE_PATH",
                    "/app/data/media_metadata.sqlite3",
                )
            ),
            media_library_database=Path(
                media_library_database
                or os.environ.get(
                    "JAV_PILOT_MEDIA_LIBRARY_DATABASE_PATH",
                    "/app/data/media_library.sqlite3",
                )
            ),
        )


def preview_archive_migration(config: ArchiveMigrationConfig) -> dict[str, object]:
    checked = _validated_config(config)
    existing = _read_journal_optional(checked)
    if existing is not None and existing.get("state") in {
        "applying",
        "applied",
        "rolling_back",
    }:
        raise ArchiveMigrationConflict(
            "an applied migration journal must be rolled back before a new preview"
        )
    plan = _build_plan(checked)
    _require_quiescent_archive_state(checked, _plan_entries(plan))
    digest = _plan_digest(plan)
    journal = {
        "revision": JOURNAL_REVISION,
        "state": "previewed",
        "plan_digest": digest,
        "created_at": time.time(),
        "updated_at": time.time(),
        "plan": plan,
    }
    _write_journal(checked, journal)
    return _public_result("preview", digest, plan)


def apply_archive_migration(
    config: ArchiveMigrationConfig,
    *,
    plan_digest: object,
) -> dict[str, object]:
    checked = _validated_config(config)
    with MaintenanceFileLock(checked.data_dir):
        return _apply_archive_migration_locked(
            checked, plan_digest=plan_digest
        )


def _apply_archive_migration_locked(
    config: ArchiveMigrationConfig,
    *,
    plan_digest: object,
) -> dict[str, object]:
    checked = _validated_config(config)
    expected = _validate_digest(plan_digest)
    journal = _read_journal(checked)
    _require_journal_digest(journal, expected)
    journal_plan = _journal_plan(journal)
    _require_quiescent_archive_state(checked, _plan_entries(journal_plan))
    state = str(journal.get("state") or "")
    if state == "applied":
        _verify_applied_files(checked, journal_plan)
        return {
            **_public_result("apply", expected, journal_plan),
            "already_applied": True,
        }
    if state == "applying":
        raise ArchiveMigrationConflict(
            "migration apply was interrupted; run rollback before retrying"
        )
    if state != "previewed":
        raise ArchiveMigrationConflict("migration journal is not ready to apply")

    current_plan = _build_plan(checked)
    current_digest = _plan_digest(current_plan)
    if current_digest != expected or current_plan != _journal_plan(journal):
        raise ArchiveMigrationConflict("migration plan changed after preview")
    _require_quiescent_archive_state(checked, _plan_entries(current_plan))

    applying = {**journal, "state": "applying", "updated_at": time.time()}
    _write_journal(checked, applying)
    files_published = False
    database_committed = False
    try:
        _publish_files(checked, current_plan)
        files_published = True
        _apply_database_changes(checked, current_plan, reverse=False)
        database_committed = True
        applied = {**applying, "state": "applied", "updated_at": time.time()}
        _write_journal(checked, applied)
    except Exception as exc:
        compensation_error: Exception | None = None
        if database_committed:
            try:
                _apply_database_changes(checked, current_plan, reverse=True)
            except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O
                compensation_error = rollback_exc
        if files_published or _has_staged_or_target_files(checked, current_plan):
            try:
                _restore_files(checked, current_plan)
            except Exception as rollback_exc:
                compensation_error = compensation_error or rollback_exc
        recovered = {
            **applying,
            "state": "previewed" if compensation_error is None else "applying",
            "updated_at": time.time(),
            "last_error": type(exc).__name__,
        }
        try:
            _write_journal(checked, recovered)
        except Exception:
            pass
        if compensation_error is not None:
            raise ArchiveMigrationError(
                "migration failed and automatic rollback was incomplete"
            ) from compensation_error
        if isinstance(exc, ArchiveMigrationError):
            raise
        raise ArchiveMigrationError("migration apply failed") from exc

    return {
        **_public_result("apply", expected, current_plan),
        "already_applied": False,
    }


def rollback_archive_migration(
    config: ArchiveMigrationConfig,
    *,
    plan_digest: object,
) -> dict[str, object]:
    checked = _validated_config(config)
    with MaintenanceFileLock(checked.data_dir):
        return _rollback_archive_migration_locked(
            checked, plan_digest=plan_digest
        )


def _rollback_archive_migration_locked(
    config: ArchiveMigrationConfig,
    *,
    plan_digest: object,
) -> dict[str, object]:
    checked = _validated_config(config)
    expected = _validate_digest(plan_digest)
    journal = _read_journal(checked)
    _require_journal_digest(journal, expected)
    state = str(journal.get("state") or "")
    plan = _journal_plan(journal)
    if state == "rolled_back":
        return {
            **_public_result("rollback", expected, plan),
            "already_rolled_back": True,
        }
    if state not in {"applying", "applied", "rolling_back"}:
        raise ArchiveMigrationConflict("migration journal has not been applied")

    rolling_back = {**journal, "state": "rolling_back", "updated_at": time.time()}
    _write_journal(checked, rolling_back)
    database_reversed = False
    try:
        _apply_database_changes(checked, plan, reverse=True)
        database_reversed = True
        _restore_files(checked, plan)
    except Exception as exc:
        if database_reversed:
            try:
                _apply_database_changes(checked, plan, reverse=False)
            except Exception:
                pass
        failed = {**rolling_back, "state": "applying", "updated_at": time.time()}
        try:
            _write_journal(checked, failed)
        except Exception:
            pass
        if isinstance(exc, ArchiveMigrationError):
            raise
        raise ArchiveMigrationError("migration rollback failed") from exc

    rolled_back = {**rolling_back, "state": "rolled_back", "updated_at": time.time()}
    _write_journal(checked, rolled_back)
    return {
        **_public_result("rollback", expected, plan),
        "already_rolled_back": False,
    }


def _validated_config(config: ArchiveMigrationConfig) -> ArchiveMigrationConfig:
    if not isinstance(config, ArchiveMigrationConfig):
        raise ArchiveMigrationError("archive migration configuration is invalid")
    absolute = replace(
        config,
        library_root=_absolute_path(config.library_root, "library root"),
        data_dir=_absolute_path(config.data_dir, "data directory"),
        web_database=_absolute_path(config.web_database, "web database"),
        metadata_database=_absolute_path(config.metadata_database, "metadata database"),
        media_library_database=_absolute_path(
            config.media_library_database, "media library database"
        ),
    )
    _require_regular_directory(absolute.library_root, "library root")
    _require_regular_directory(absolute.data_dir, "data directory")
    for label, path in (
        ("web database", absolute.web_database),
        ("metadata database", absolute.metadata_database),
        ("media library database", absolute.media_library_database),
    ):
        _require_regular_file(path, label)
    return absolute


def _absolute_path(value: Path | str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ArchiveMigrationError(f"{label} must be absolute")
    return path


def _require_regular_directory(path: Path, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ArchiveMigrationError(f"{label} is unavailable") from exc
    if _path_is_linklike(path) or not stat.S_ISDIR(value.st_mode):
        raise ArchiveMigrationError(f"{label} must be a regular directory")
    return value


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ArchiveMigrationError(f"{label} is unavailable") from exc
    if _path_is_linklike(path) or not stat.S_ISREG(value.st_mode):
        raise ArchiveMigrationError(f"{label} must be a regular file")
    return value


def _path_is_linklike(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _build_plan(config: ArchiveMigrationConfig) -> dict[str, object]:
    _require_no_pending_archive_relocations(config)
    root_stat = _require_regular_directory(config.library_root, "library root")
    entries, skipped = _scan_archive(config.library_root, root_stat)
    _require_no_archive_publication_artifacts(config.library_root, entries)
    path_changes = {
        str(entry["source_media_path"]): str(entry["target_media_path"])
        for entry in entries
    }
    nfo_changes = {
        str(entry["source_nfo_path"]): str(entry["target_nfo_path"])
        for entry in entries
    }
    mutations = _database_snapshot(config, path_changes, nfo_changes)
    plan = {
        "revision": JOURNAL_REVISION,
        "root_identity": _directory_identity(root_stat),
        "entries": entries,
        "skipped": skipped,
        "database_mutations": mutations,
    }
    _require_quiescent_archive_state(config, entries)
    return plan


def _require_quiescent_archive_state(
    config: ArchiveMigrationConfig,
    entries: Sequence[Mapping[str, object]],
) -> None:
    _require_no_pending_archive_relocations(config)
    _require_no_archive_publication_artifacts(config.library_root, entries)


def _require_no_pending_archive_relocations(
    config: ArchiveMigrationConfig,
) -> None:
    directory = config.data_dir / ARCHIVE_RELOCATION_DIRECTORY
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArchiveMigrationError(
            "archive relocation recovery state could not be inspected"
        ) from exc
    if _path_is_linklike(directory) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ArchiveMigrationConflict("archive relocation recovery state is unsafe")
    try:
        with os.scandir(directory) as iterator:
            pending = next(iterator, None)
    except OSError as exc:
        raise ArchiveMigrationError(
            "archive relocation recovery state could not be inspected"
        ) from exc
    if pending is not None:
        raise ArchiveMigrationConflict(
            "unfinished archive relocation must be recovered before migration"
        )


def _require_no_archive_publication_artifacts(
    root: Path,
    entries: Sequence[Mapping[str, object]],
) -> None:
    directories: set[PurePosixPath] = set()
    for entry in entries:
        for key in ("source_media_path", "target_media_path"):
            value = str(entry.get(key) or "")
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.as_posix() != value
            ):
                raise ArchiveMigrationError("migration journal path is invalid")
            directories.add(path.parent)

    for relative in sorted(directories, key=lambda item: item.as_posix()):
        directory = _existing_regular_directory_below_root(root, relative)
        if directory is None:
            continue
        try:
            with os.scandir(directory) as iterator:
                for child in iterator:
                    if child.name.casefold().endswith(
                        ARCHIVE_PUBLICATION_ARTIFACT_SUFFIXES
                    ):
                        raise ArchiveMigrationConflict(
                            "archive publication artifacts must be recovered "
                            "before migration"
                        )
        except OSError as exc:
            raise ArchiveMigrationError(
                "archive publication state could not be inspected"
            ) from exc


def _existing_regular_directory_below_root(
    root: Path,
    relative: PurePosixPath,
) -> Path | None:
    current = root
    if relative == PurePosixPath("."):
        return current
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ArchiveMigrationError(
                "archive publication state could not be inspected"
            ) from exc
        if _path_is_linklike(current) or not stat.S_ISDIR(current_stat.st_mode):
            raise ArchiveMigrationConflict("archive publication directory is unsafe")
    return current


def _scan_archive(
    root: Path,
    root_stat: os.stat_result,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    media_files: list[tuple[Path, os.stat_result]] = []
    directories: list[tuple[Path, int]] = [(root, 0)]
    seen = 0
    while directories:
        directory, depth = directories.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ArchiveMigrationError(
                "archive directory could not be scanned"
            ) from exc
        for child in children:
            seen += 1
            if seen > MAX_SCAN_ENTRIES:
                raise ArchiveMigrationError("archive scan entry budget exceeded")
            child_path = Path(child.path)
            try:
                child_stat = child_path.lstat()
            except OSError as exc:
                raise ArchiveMigrationError(
                    "archive entry changed during scan"
                ) from exc
            if _path_is_linklike(child_path):
                continue
            if stat.S_ISDIR(child_stat.st_mode):
                if depth >= MAX_SCAN_DEPTH:
                    raise ArchiveMigrationError("archive scan depth budget exceeded")
                if os.name != "nt" and child_stat.st_dev != root_stat.st_dev:
                    raise ArchiveMigrationConflict(
                        "archive contains a nested filesystem"
                    )
                directories.append((child_path, depth + 1))
            elif (
                stat.S_ISREG(child_stat.st_mode)
                and child_stat.st_size > 0
                and child_path.suffix.casefold() in VIDEO_SUFFIXES
            ):
                if os.name != "nt" and child_stat.st_dev != root_stat.st_dev:
                    raise ArchiveMigrationConflict("archive media crosses filesystems")
                media_files.append((child_path, child_stat))

    entries: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    source_paths: set[str] = set()
    target_paths: dict[str, str] = {}
    for media, media_stat in sorted(media_files, key=lambda item: item[0].as_posix()):
        source_media = media.relative_to(root).as_posix()
        nfo = media.with_suffix(".nfo")
        try:
            nfo_stat = nfo.lstat()
        except FileNotFoundError:
            skipped.append(
                {
                    "source_media_path": source_media,
                    "reason": "nfo_missing",
                    "media_identity": _file_identity(media_stat),
                }
            )
            continue
        except OSError as exc:
            raise ArchiveMigrationError("archive NFO could not be inspected") from exc
        if _path_is_linklike(nfo) or not stat.S_ISREG(nfo_stat.st_mode):
            raise ArchiveMigrationConflict("archive NFO must be a regular file")
        if os.name != "nt" and nfo_stat.st_dev != root_stat.st_dev:
            raise ArchiveMigrationConflict("archive NFO crosses filesystems")
        body, stable_nfo_stat = _read_regular_file(nfo, nfo_stat)
        parsed = parse_movie_nfo(body)
        if parsed is None:
            skipped.append(
                {
                    "source_media_path": source_media,
                    "source_nfo_path": nfo.relative_to(root).as_posix(),
                    "reason": "nfo_invalid",
                    "media_identity": _file_identity(media_stat),
                    "nfo_identity": _file_identity(stable_nfo_stat),
                    "nfo_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            continue
        path_code = _code_from_path(media)
        if (
            parsed.code_key is not None
            and path_code is not None
            and normalize_catalog_code(path_code, max_length=40)[1] != parsed.code_key
        ):
            raise ArchiveMigrationConflict(
                "archive NFO catalog code conflicts with its path"
            )
        code = parsed.code or path_code
        if code is None:
            skipped.append(
                {
                    "source_media_path": source_media,
                    "reason": "code_missing",
                    "media_identity": _file_identity(media_stat),
                }
            )
            continue
        release_date = parsed.release_date or _nfo_year(body)
        variant = web_download_variant_from_stem(media.name)
        try:
            layout = plan_archive_layout(
                code=code,
                title=parsed.title,
                release_date=release_date,
                suffix=media.suffix,
                variant=variant,
            )
        except ValueError as exc:
            raise ArchiveMigrationConflict(
                "archive metadata cannot form a safe path"
            ) from exc
        if not layout.ready:
            skipped.append(
                {
                    "source_media_path": source_media,
                    "source_nfo_path": nfo.relative_to(root).as_posix(),
                    "reason": "metadata_incomplete",
                    "missing_fields": list(layout.missing_fields),
                    "media_identity": _file_identity(media_stat),
                    "nfo_identity": _file_identity(stable_nfo_stat),
                    "nfo_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            continue
        target_media = layout.relative_media_path.as_posix()
        target_nfo = layout.relative_media_path.with_suffix(".nfo").as_posix()
        source_nfo = nfo.relative_to(root).as_posix()
        if source_media == target_media and source_nfo == target_nfo:
            continue
        image_moves = _archive_image_moves(
            root,
            root_stat,
            media,
            layout.relative_media_path.parent,
        )
        entry = {
            "display_code": layout.display_code,
            "source_media_path": source_media,
            "target_media_path": target_media,
            "source_nfo_path": source_nfo,
            "target_nfo_path": target_nfo,
            "staging_media_path": _staging_path(source_media, "media"),
            "staging_nfo_path": _staging_path(source_nfo, "nfo"),
            "media_identity": _file_identity(media_stat),
            "nfo_identity": _file_identity(stable_nfo_stat),
            "nfo_sha256": hashlib.sha256(body).hexdigest(),
            "image_moves": image_moves,
        }
        path_pairs = [
            (source_media, target_media),
            (source_nfo, target_nfo),
        ]
        path_pairs.extend(
            (str(image["source_path"]), str(image["target_path"]))
            for image in image_moves
        )
        for source, target in path_pairs:
            if source in source_paths:
                raise ArchiveMigrationConflict("archive migration source is duplicated")
            source_paths.add(source)
            previous = target_paths.setdefault(target, source)
            if previous != source:
                raise ArchiveMigrationConflict("archive migration target is duplicated")
        entries.append(entry)

    for entry in entries:
        planned_paths = [
            (
                str(entry["source_media_path"]),
                str(entry["target_media_path"]),
                str(entry["staging_media_path"]),
            ),
            (
                str(entry["source_nfo_path"]),
                str(entry["target_nfo_path"]),
                str(entry["staging_nfo_path"]),
            ),
        ]
        for image in _entry_image_moves(entry):
            planned_paths.append(
                (
                    str(image["source_path"]),
                    str(image["target_path"]),
                    str(image["staging_path"]),
                )
            )
        for source, target, stage in planned_paths:
            if stage in source_paths or stage in target_paths:
                raise ArchiveMigrationConflict("archive staging path is not unique")
            if (
                target != source
                and target not in source_paths
                and _path_exists(root, target)
            ):
                raise ArchiveMigrationConflict(
                    "archive migration target already exists"
                )
            if _path_exists(root, stage):
                raise ArchiveMigrationConflict("archive migration staging path exists")
    return entries, skipped


def _archive_image_moves(
    root: Path,
    root_stat: os.stat_result,
    media: Path,
    target_directory: PurePosixPath,
) -> list[dict[str, object]]:
    moves: list[dict[str, object]] = []
    for name in ARCHIVE_IMAGE_NAMES:
        source = media.parent / name
        try:
            source_stat = source.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArchiveMigrationError("archive image could not be inspected") from exc
        if _path_is_linklike(source) or not stat.S_ISREG(source_stat.st_mode):
            raise ArchiveMigrationConflict("archive image must be a regular file")
        if source_stat.st_size <= 0:
            raise ArchiveMigrationConflict("archive image size is invalid")
        if os.name != "nt" and source_stat.st_dev != root_stat.st_dev:
            raise ArchiveMigrationConflict("archive image crosses filesystems")
        digest, stable_stat = _read_regular_digest(source, source_stat)
        source_path = source.relative_to(root).as_posix()
        target_path = (target_directory / name).as_posix()
        if source_path == target_path:
            continue
        moves.append(
            {
                "source_path": source_path,
                "target_path": target_path,
                "staging_path": _staging_path(source_path, "image"),
                "identity": _file_identity(stable_stat),
                "sha256": digest,
            }
        )
    return moves


def _read_regular_file(
    path: Path, expected: os.stat_result
) -> tuple[bytes, os.stat_result]:
    if expected.st_size <= 0 or expected.st_size > 2 * 1024 * 1024:
        raise ArchiveMigrationConflict("archive NFO size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(expected):
            raise ArchiveMigrationConflict("archive NFO changed during preview")
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ArchiveMigrationConflict("archive NFO was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArchiveMigrationConflict("archive NFO grew during preview")
        final = os.fstat(descriptor)
        if _file_identity(final) != _file_identity(opened):
            raise ArchiveMigrationConflict("archive NFO changed during preview")
        return b"".join(chunks), final
    except OSError as exc:
        raise ArchiveMigrationError("archive NFO could not be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_regular_digest(
    path: Path,
    expected: os.stat_result,
) -> tuple[str, os.stat_result]:
    if expected.st_size <= 0 or expected.st_size > MAX_ARCHIVE_IMAGE_BYTES:
        raise ArchiveMigrationConflict("archive image size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(opened.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != _file_identity(expected)
        ):
            raise ArchiveMigrationConflict("archive image changed during preview")
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ArchiveMigrationConflict("archive image was truncated")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArchiveMigrationConflict("archive image grew during preview")
        final = os.fstat(descriptor)
        if _file_identity(final) != _file_identity(opened):
            raise ArchiveMigrationConflict("archive image changed during preview")
        return digest.hexdigest(), final
    except OSError as exc:
        raise ArchiveMigrationError("archive image could not be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _nfo_year(body: bytes) -> str | None:
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        return None
    if root.tag.casefold() != "movie":
        return None
    for child in root:
        if child.tag.casefold() == "year" and child.text:
            value = child.text.strip()
            if len(value) == 4 and value.isdigit():
                return value
    return None


def _code_from_path(media: Path) -> str | None:
    for candidate in (media.parent.name, media.stem):
        normalized = normalize_catalog_code(candidate, max_length=40)
        if normalized is not None:
            return normalized[0]
    return None


def _staging_path(source: str, kind: str) -> str:
    path = PurePosixPath(source)
    token = hashlib.sha256(f"{kind}\0{source}".encode("utf-8")).hexdigest()[:20]
    return (path.parent / f".archive-migration-{token}.stage").as_posix()


def _file_identity(value: os.stat_result) -> dict[str, int]:
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
    }


def _directory_identity(value: os.stat_result) -> dict[str, int]:
    return {"device": int(value.st_dev), "inode": int(value.st_ino)}


def _database_snapshot(
    config: ArchiveMigrationConfig,
    path_changes: Mapping[str, str],
    nfo_changes: Mapping[str, str],
) -> list[dict[str, object]]:
    mutations: list[dict[str, object]] = []
    with _readonly_database(config.web_database) as web:
        columns = _require_table_columns(
            web,
            "web_download_jobs",
            {"job_id", "output_path", "incumbent_output_path"},
        )
        del columns
        rows = web.execute(
            "SELECT job_id, output_path, incumbent_output_path "
            "FROM web_download_jobs ORDER BY job_id"
        ).fetchall()
        for row in rows:
            changes = _path_column_changes(
                row, ("output_path", "incumbent_output_path"), path_changes
            )
            if changes:
                mutations.append(
                    _mutation("web", "web_download_jobs", "job_id", row[0], changes)
                )

    with _readonly_database(config.metadata_database) as metadata:
        _require_table_columns(
            metadata,
            "jobs",
            {"job_id", "relative_media_path", "assets_json"},
        )
        rows = metadata.execute(
            "SELECT job_id, relative_media_path, assets_json FROM jobs ORDER BY job_id"
        ).fetchall()
        for row in rows:
            old_path = str(row[1]) if row[1] is not None else None
            if old_path not in path_changes:
                continue
            changes: dict[str, dict[str, object]] = {
                "relative_media_path": {
                    "old": old_path,
                    "new": path_changes[old_path],
                }
            }
            old_assets = str(row[2] or "{}")
            new_assets = _relocate_nfo_asset_key(
                old_assets,
                PurePosixPath(old_path).with_suffix(".nfo").as_posix(),
                nfo_changes[PurePosixPath(old_path).with_suffix(".nfo").as_posix()],
            )
            if new_assets != old_assets:
                changes["assets_json"] = {"old": old_assets, "new": new_assets}
            mutations.append(_mutation("metadata", "jobs", "job_id", row[0], changes))
        mutations.extend(_review_snapshot(metadata, path_changes, nfo_changes))

    with _readonly_database(config.media_library_database) as library:
        _require_no_library_blockers(library, path_changes)
    return sorted(mutations, key=_mutation_sort_key)


def _review_snapshot(
    connection: sqlite3.Connection,
    path_changes: Mapping[str, str],
    nfo_changes: Mapping[str, str],
) -> list[dict[str, object]]:
    if not _table_exists(connection, "media_metadata_reviews"):
        return []
    columns = _table_columns(connection, "media_metadata_reviews")
    required = {"review_id", "relative_media_path"}
    if not required.issubset(columns):
        raise ArchiveMigrationConflict("metadata review schema is incomplete")
    old_paths = set(path_changes)
    old_references = old_paths | set(nfo_changes)
    review_rows = connection.execute(
        "SELECT * FROM media_metadata_reviews ORDER BY review_id"
    ).fetchall()
    review_columns = [
        item[0]
        for item in connection.execute(
            "SELECT * FROM media_metadata_reviews LIMIT 0"
        ).description
        or ()
    ]
    index = {name: position for position, name in enumerate(review_columns)}

    if _table_exists(connection, "media_metadata_review_publications"):
        publication_columns = _table_columns(
            connection, "media_metadata_review_publications"
        )
        if not {"review_id", "artifacts_json"}.issubset(publication_columns):
            raise ArchiveMigrationConflict("metadata publication schema is incomplete")
        publications = connection.execute(
            "SELECT review_id, artifacts_json FROM media_metadata_review_publications"
        ).fetchall()
        publication_review_ids = {str(row[0]) for row in publications}
        for _, raw in publications:
            if _json_references(str(raw), old_references):
                raise ArchiveMigrationConflict(
                    "metadata review publication references an archive path"
                )
    else:
        publication_review_ids = set()

    active_refetch: set[str] = set()
    if _table_exists(connection, "media_metadata_review_refetch"):
        refetch_columns = _table_columns(connection, "media_metadata_review_refetch")
        if {"review_id", "status"}.issubset(refetch_columns):
            active_refetch = {
                str(row[0])
                for row in connection.execute(
                    "SELECT review_id FROM media_metadata_review_refetch "
                    "WHERE status IN ('queued', 'running')"
                )
            }

    mutations: list[dict[str, object]] = []
    for row in review_rows:
        old_path = str(row[index["relative_media_path"]])
        if old_path not in path_changes:
            continue
        review_id = str(row[index["review_id"]])
        active = "abandoned_at" not in index or row[index["abandoned_at"]] is None
        if active or review_id in active_refetch:
            raise ArchiveMigrationConflict(
                "an active metadata review references an archive path"
            )
        if review_id in publication_review_ids:
            raise ArchiveMigrationConflict(
                "metadata review publication references an archive path"
            )
        mutations.append(
            _mutation(
                "metadata",
                "media_metadata_reviews",
                "review_id",
                review_id,
                {
                    "relative_media_path": {
                        "old": old_path,
                        "new": path_changes[old_path],
                    }
                },
            )
        )
    return mutations


def _require_no_library_blockers(
    connection: sqlite3.Connection,
    path_changes: Mapping[str, str],
) -> None:
    if _table_exists(connection, "media_library_history_cleanup_operations"):
        columns = _table_columns(connection, "media_library_history_cleanup_operations")
        if "status" not in columns:
            raise ArchiveMigrationConflict("media library cleanup schema is incomplete")
        if connection.execute(
            "SELECT 1 FROM media_library_history_cleanup_operations "
            "WHERE status = 'prepared' LIMIT 1"
        ).fetchone():
            raise ArchiveMigrationConflict(
                "a prepared history cleanup blocks migration"
            )
    if _table_exists(connection, "media_library_history_facts"):
        columns = _table_columns(connection, "media_library_history_facts")
        if "relative_media_path" not in columns:
            raise ArchiveMigrationConflict("media library history schema is incomplete")
        old_paths = tuple(sorted(path_changes))
        if old_paths:
            slots = ",".join("?" for _ in old_paths)
            if connection.execute(
                "SELECT 1 FROM media_library_history_facts "
                f"WHERE relative_media_path IN ({slots}) LIMIT 1",
                old_paths,
            ).fetchone():
                raise ArchiveMigrationConflict(
                    "media library history references an archive path"
                )


def _relocate_nfo_asset_key(
    raw_assets: str,
    old_nfo_path: str,
    new_nfo_path: str,
) -> str:
    try:
        assets = json.loads(raw_assets)
    except (TypeError, ValueError) as exc:
        raise ArchiveMigrationConflict("metadata assets JSON is invalid") from exc
    if not isinstance(assets, dict):
        raise ArchiveMigrationConflict("metadata assets JSON is invalid")
    old_name = PurePosixPath(old_nfo_path).name
    new_name = PurePosixPath(new_nfo_path).name
    if old_name == new_name or old_name not in assets:
        return raw_assets
    if new_name in assets and assets[new_name] != assets[old_name]:
        raise ArchiveMigrationConflict("metadata NFO asset target already exists")
    relocated = dict(assets)
    value = relocated.pop(old_name)
    relocated[new_name] = value
    return json.dumps(
        relocated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _json_references(raw: str, references: set[str]) -> bool:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ArchiveMigrationConflict("metadata publication JSON is invalid") from exc
    stack = [value]
    seen = 0
    while stack:
        item = stack.pop()
        seen += 1
        if seen > 10_000:
            raise ArchiveMigrationConflict("metadata publication JSON is too large")
        if isinstance(item, str) and item in references:
            return True
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _path_column_changes(
    row: Sequence[object],
    columns: Sequence[str],
    path_changes: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    changes: dict[str, dict[str, object]] = {}
    for position, column in enumerate(columns, start=1):
        old = str(row[position]) if row[position] is not None else None
        if old in path_changes:
            changes[column] = {"old": old, "new": path_changes[old]}
    return changes


def _mutation(
    database: str,
    table: str,
    key_column: str,
    key: object,
    changes: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    return {
        "database": database,
        "table": table,
        "key_column": key_column,
        "key": str(key),
        "changes": {name: dict(value) for name, value in sorted(changes.items())},
    }


def _mutation_sort_key(value: Mapping[str, object]) -> tuple[str, str, str]:
    return (str(value["database"]), str(value["table"]), str(value["key"]))


@contextmanager
def _readonly_database(path: Path):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30.0)
    try:
        yield connection
    finally:
        connection.close()


def _table_exists(
    connection: sqlite3.Connection, table: str, schema: str = "main"
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(
    connection: sqlite3.Connection, table: str, schema: str = "main"
) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA {schema}.table_info({table})")
    }


def _require_table_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
    schema: str = "main",
) -> set[str]:
    columns = _table_columns(connection, table, schema)
    if not required.issubset(columns):
        raise ArchiveMigrationConflict(f"{table} schema is incomplete")
    return columns


def _apply_database_changes(
    config: ArchiveMigrationConfig,
    plan: Mapping[str, object],
    *,
    reverse: bool,
) -> None:
    connection = sqlite3.connect(
        config.web_database, timeout=30.0, isolation_level=None
    )
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "ATTACH DATABASE ? AS metadata", (str(config.metadata_database),)
        )
        connection.execute(
            "ATTACH DATABASE ? AS media_library", (str(config.media_library_database),)
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            path_changes = {
                str(entry["source_media_path"]): str(entry["target_media_path"])
                for entry in _plan_entries(plan)
            }
            if not reverse:
                _require_no_library_blockers_attached(connection, path_changes)
                expected = list(plan.get("database_mutations") or ())
                current = _database_snapshot_attached(
                    connection, path_changes, _nfo_changes(plan)
                )
                if current != expected:
                    raise ArchiveMigrationConflict(
                        "database references changed after migration preview"
                    )
            _execute_mutations(
                connection,
                list(plan.get("database_mutations") or ()),
                reverse=reverse,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except sqlite3.Error as exc:
        raise ArchiveMigrationError(
            "archive migration database transaction failed"
        ) from exc
    finally:
        connection.close()


def _database_snapshot_attached(
    connection: sqlite3.Connection,
    path_changes: Mapping[str, str],
    nfo_changes: Mapping[str, str],
) -> list[dict[str, object]]:
    mutations: list[dict[str, object]] = []
    _require_table_columns(
        connection,
        "web_download_jobs",
        {"job_id", "output_path", "incumbent_output_path"},
    )
    for row in connection.execute(
        "SELECT job_id, output_path, incumbent_output_path "
        "FROM web_download_jobs ORDER BY job_id"
    ):
        changes = _path_column_changes(
            row, ("output_path", "incumbent_output_path"), path_changes
        )
        if changes:
            mutations.append(
                _mutation("web", "web_download_jobs", "job_id", row[0], changes)
            )

    _require_table_columns(
        connection,
        "jobs",
        {"job_id", "relative_media_path", "assets_json"},
        "metadata",
    )
    for row in connection.execute(
        "SELECT job_id, relative_media_path, assets_json "
        "FROM metadata.jobs ORDER BY job_id"
    ):
        old_path = str(row[1]) if row[1] is not None else None
        if old_path not in path_changes:
            continue
        changes: dict[str, dict[str, object]] = {
            "relative_media_path": {"old": old_path, "new": path_changes[old_path]}
        }
        old_assets = str(row[2] or "{}")
        old_nfo = PurePosixPath(old_path).with_suffix(".nfo").as_posix()
        new_assets = _relocate_nfo_asset_key(old_assets, old_nfo, nfo_changes[old_nfo])
        if new_assets != old_assets:
            changes["assets_json"] = {"old": old_assets, "new": new_assets}
        mutations.append(_mutation("metadata", "jobs", "job_id", row[0], changes))
    mutations.extend(_review_snapshot_attached(connection, path_changes, nfo_changes))
    return sorted(mutations, key=_mutation_sort_key)


def _review_snapshot_attached(
    connection: sqlite3.Connection,
    path_changes: Mapping[str, str],
    nfo_changes: Mapping[str, str],
) -> list[dict[str, object]]:
    # Reuse the standalone checks through a temporary in-transaction adapter.
    return _review_snapshot_for_schema(
        connection, "metadata", path_changes, nfo_changes
    )


def _review_snapshot_for_schema(
    connection: sqlite3.Connection,
    schema: str,
    path_changes: Mapping[str, str],
    nfo_changes: Mapping[str, str],
) -> list[dict[str, object]]:
    if not _table_exists(connection, "media_metadata_reviews", schema):
        return []
    columns = _table_columns(connection, "media_metadata_reviews", schema)
    if not {"review_id", "relative_media_path"}.issubset(columns):
        raise ArchiveMigrationConflict("metadata review schema is incomplete")
    prefix = f"{schema}."
    publications: list[tuple[object, object]] = []
    if _table_exists(connection, "media_metadata_review_publications", schema):
        publications = connection.execute(
            f"SELECT review_id, artifacts_json FROM {prefix}media_metadata_review_publications"
        ).fetchall()
    old_references = set(path_changes) | set(nfo_changes)
    publication_ids = {str(row[0]) for row in publications}
    for _, raw in publications:
        if _json_references(str(raw), old_references):
            raise ArchiveMigrationConflict(
                "metadata review publication references an archive path"
            )
    active_refetch: set[str] = set()
    if _table_exists(connection, "media_metadata_review_refetch", schema):
        refetch_columns = _table_columns(
            connection, "media_metadata_review_refetch", schema
        )
        if {"review_id", "status"}.issubset(refetch_columns):
            active_refetch = {
                str(row[0])
                for row in connection.execute(
                    f"SELECT review_id FROM {prefix}media_metadata_review_refetch "
                    "WHERE status IN ('queued', 'running')"
                )
            }
    selected = ["review_id", "relative_media_path"]
    if "abandoned_at" in columns:
        selected.append("abandoned_at")
    rows = connection.execute(
        f"SELECT {','.join(selected)} FROM {prefix}media_metadata_reviews "
        "ORDER BY review_id"
    ).fetchall()
    mutations: list[dict[str, object]] = []
    for row in rows:
        review_id = str(row[0])
        old_path = str(row[1])
        if old_path not in path_changes:
            continue
        active = len(selected) == 2 or row[2] is None
        if active or review_id in active_refetch:
            raise ArchiveMigrationConflict(
                "an active metadata review references an archive path"
            )
        if review_id in publication_ids:
            raise ArchiveMigrationConflict(
                "metadata review publication references an archive path"
            )
        mutations.append(
            _mutation(
                "metadata",
                "media_metadata_reviews",
                "review_id",
                review_id,
                {
                    "relative_media_path": {
                        "old": old_path,
                        "new": path_changes[old_path],
                    }
                },
            )
        )
    return mutations


def _require_no_library_blockers_attached(
    connection: sqlite3.Connection, path_changes: Mapping[str, str]
) -> None:
    schema = "media_library"
    if _table_exists(connection, "media_library_history_cleanup_operations", schema):
        if connection.execute(
            "SELECT 1 FROM media_library.media_library_history_cleanup_operations "
            "WHERE status = 'prepared' LIMIT 1"
        ).fetchone():
            raise ArchiveMigrationConflict(
                "a prepared history cleanup blocks migration"
            )
    if _table_exists(connection, "media_library_history_facts", schema):
        old_paths = tuple(sorted(path_changes))
        if old_paths:
            slots = ",".join("?" for _ in old_paths)
            if connection.execute(
                "SELECT 1 FROM media_library.media_library_history_facts "
                f"WHERE relative_media_path IN ({slots}) LIMIT 1",
                old_paths,
            ).fetchone():
                raise ArchiveMigrationConflict(
                    "media library history references an archive path"
                )


_ALLOWED_MUTATIONS = {
    ("web", "web_download_jobs", "job_id"): {
        "output_path",
        "incumbent_output_path",
    },
    ("metadata", "jobs", "job_id"): {"relative_media_path", "assets_json"},
    ("metadata", "media_metadata_reviews", "review_id"): {"relative_media_path"},
}


def _execute_mutations(
    connection: sqlite3.Connection,
    mutations: Sequence[Mapping[str, object]],
    *,
    reverse: bool,
) -> None:
    values = reversed(mutations) if reverse else iter(mutations)
    for mutation in values:
        _execute_mutation(connection, mutation, reverse=reverse)


def _execute_mutation(
    connection: sqlite3.Connection,
    mutation: Mapping[str, object],
    *,
    reverse: bool,
) -> None:
    database = str(mutation.get("database") or "")
    table = str(mutation.get("table") or "")
    key_column = str(mutation.get("key_column") or "")
    allowed = _ALLOWED_MUTATIONS.get((database, table, key_column))
    changes = mutation.get("changes")
    if (
        allowed is None
        or not isinstance(changes, dict)
        or not set(changes).issubset(allowed)
    ):
        raise ArchiveMigrationError(
            "migration journal contains an invalid database mutation"
        )
    schema = "main" if database == "web" else "metadata"
    key = str(mutation.get("key") or "")
    row = connection.execute(
        f"SELECT {','.join(sorted(changes))} FROM {schema}.{table} "
        f"WHERE {key_column} = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise ArchiveMigrationConflict("migration database row no longer exists")
    columns = sorted(changes)
    desired: list[object] = []
    expected: list[object] = []
    for index, column in enumerate(columns):
        change = changes[column]
        if not isinstance(change, dict) or set(change) != {"old", "new"}:
            raise ArchiveMigrationError("migration journal database change is invalid")
        before = change["new"] if reverse else change["old"]
        after = change["old"] if reverse else change["new"]
        if row[index] == after:
            desired.append(after)
            expected.append(after)
            continue
        if row[index] != before:
            raise ArchiveMigrationConflict("migration database row changed")
        desired.append(after)
        expected.append(before)
    if all(row[index] == desired[index] for index in range(len(columns))):
        return
    assignments = ",".join(f"{column} = ?" for column in columns)
    changed = connection.execute(
        f"UPDATE {schema}.{table} SET {assignments} WHERE {key_column} = ?",
        (*desired, key),
    ).rowcount
    if changed != 1:
        raise ArchiveMigrationConflict("migration database update was not persisted")


def _publish_files(config: ArchiveMigrationConfig, plan: Mapping[str, object]) -> None:
    entries = _plan_entries(plan)
    _verify_root_identity(config.library_root, plan)
    moves = _file_moves(entries)
    _validate_sources(config.library_root, moves)
    _prepare_target_directories(config.library_root, moves)
    staged: list[dict[str, object]] = []
    published: list[dict[str, object]] = []
    try:
        for move in moves:
            source = _root_path(config.library_root, str(move["source"]))
            stage = _root_path(config.library_root, str(move["stage"]))
            _rename_no_replace(source, stage, root=config.library_root)
            staged.append(move)
        _fsync_move_directories(config.library_root, moves, ("source", "stage"))
        for move in moves:
            stage = _root_path(config.library_root, str(move["stage"]))
            target = _root_path(config.library_root, str(move["target"]))
            _rename_no_replace(stage, target, root=config.library_root)
            published.append(move)
        _fsync_move_directories(config.library_root, moves, ("stage", "target"))
        _verify_applied_files(config, plan)
    except Exception:
        for move in reversed(published):
            target = _root_path(config.library_root, str(move["target"]))
            stage = _root_path(config.library_root, str(move["stage"]))
            if target.exists() and not stage.exists():
                _rename_no_replace(target, stage, root=config.library_root)
        for move in reversed(staged):
            stage = _root_path(config.library_root, str(move["stage"]))
            source = _root_path(config.library_root, str(move["source"]))
            if stage.exists() and not source.exists():
                _rename_no_replace(stage, source, root=config.library_root)
        _fsync_move_directories(config.library_root, moves, ("source", "target"))
        raise


def _restore_files(config: ArchiveMigrationConfig, plan: Mapping[str, object]) -> None:
    _verify_root_identity(config.library_root, plan)
    moves = _file_moves(_plan_entries(plan))
    for move in reversed(moves):
        source = _root_path(config.library_root, str(move["source"]))
        stage = _root_path(config.library_root, str(move["stage"]))
        target = _root_path(config.library_root, str(move["target"]))
        existing = [path for path in (source, stage, target) if _path_present(path)]
        if not existing:
            raise ArchiveMigrationConflict(
                "migration file state cannot be rolled back safely"
            )
        for path in existing:
            _verify_move_file(path, move)
        if source in existing:
            for duplicate in (stage, target):
                if duplicate in existing:
                    _unlink_move_file(config.library_root, duplicate, move)
            continue
        if stage in existing:
            if target in existing:
                _unlink_move_file(config.library_root, target, move)
            _rename_no_replace(stage, source, root=config.library_root)
            continue
        _rename_no_replace(target, source, root=config.library_root)
    _fsync_move_directories(
        config.library_root,
        moves,
        ("source", "stage", "target"),
    )
    _validate_sources(config.library_root, moves)


def _verify_applied_files(
    config: ArchiveMigrationConfig, plan: Mapping[str, object]
) -> None:
    _verify_root_identity(config.library_root, plan)
    for move in _file_moves(_plan_entries(plan)):
        source = _root_path(config.library_root, str(move["source"]))
        stage = _root_path(config.library_root, str(move["stage"]))
        target = _root_path(config.library_root, str(move["target"]))
        if source.exists() or stage.exists() or not target.exists():
            raise ArchiveMigrationConflict("migration target state is incomplete")
        _verify_move_file(target, move)


def _validate_sources(root: Path, moves: Sequence[Mapping[str, object]]) -> None:
    source_names = {str(move["source"]) for move in moves}
    for move in moves:
        source = _root_path(root, str(move["source"]))
        stage = _root_path(root, str(move["stage"]))
        target = _root_path(root, str(move["target"]))
        _verify_move_file(source, move)
        if stage.exists():
            raise ArchiveMigrationConflict("migration staging path already exists")
        if str(move["target"]) not in source_names and target.exists():
            raise ArchiveMigrationConflict("migration target already exists")


def _verify_move_file(path: Path, move: Mapping[str, object]) -> None:
    if _path_identity(path) != move.get("identity"):
        raise ArchiveMigrationConflict("migration file identity changed")
    expected_digest = move.get("sha256")
    if expected_digest is not None and _sha256_regular(path) != expected_digest:
        raise ArchiveMigrationConflict("migration file content changed")


def _unlink_move_file(
    root: Path,
    path: Path,
    move: Mapping[str, object],
) -> None:
    _require_regular_archive_parent(root, path)
    _verify_move_file(path, move)
    try:
        path.unlink()
    except OSError as exc:
        raise ArchiveMigrationError("migration file could not be removed") from exc
    _fsync_directory(path.parent)


def _file_moves(entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    moves: list[dict[str, object]] = []
    for entry in entries:
        moves.extend(
            (
                {
                    "kind": "media",
                    "source": entry["source_media_path"],
                    "target": entry["target_media_path"],
                    "stage": entry["staging_media_path"],
                    "identity": entry["media_identity"],
                    "sha256": None,
                },
                {
                    "kind": "nfo",
                    "source": entry["source_nfo_path"],
                    "target": entry["target_nfo_path"],
                    "stage": entry["staging_nfo_path"],
                    "identity": entry["nfo_identity"],
                    "sha256": entry["nfo_sha256"],
                },
            )
        )
        for image in _entry_image_moves(entry):
            moves.append(
                {
                    "kind": "image",
                    "source": image["source_path"],
                    "target": image["target_path"],
                    "stage": image["staging_path"],
                    "identity": image["identity"],
                    "sha256": image["sha256"],
                }
            )
    return moves


def _entry_image_moves(
    entry: Mapping[str, object],
) -> list[Mapping[str, object]]:
    values = entry.get("image_moves", [])
    required = {
        "source_path",
        "target_path",
        "staging_path",
        "identity",
        "sha256",
    }
    if not isinstance(values, list) or not all(
        isinstance(value, dict) and required.issubset(value) for value in values
    ):
        raise ArchiveMigrationError("migration journal image moves are invalid")
    return values


def _prepare_target_directories(
    root: Path, moves: Sequence[Mapping[str, object]]
) -> None:
    directories = sorted(
        {PurePosixPath(str(move["target"])).parent.as_posix() for move in moves}
    )
    for relative in directories:
        clean = PurePosixPath(relative)
        if len(clean.parts) != 2:
            raise ArchiveMigrationConflict("archive target directory depth is invalid")
        parent = root
        for part in clean.parts:
            target = parent / part
            if _path_present(target):
                _require_regular_directory(target, "archive target directory")
                parent = target
                continue
            try:
                target.mkdir(mode=0o750)
            except FileExistsError:
                _require_regular_directory(target, "archive target directory")
            except OSError as exc:
                raise ArchiveMigrationError(
                    "archive target directory could not be created"
                ) from exc
            _fsync_directory(parent)
            parent = target


def _rename_no_replace(source: Path, target: Path, *, root: Path | None = None) -> None:
    if root is not None:
        _require_regular_archive_parent(root, source)
        _require_regular_archive_parent(root, target)
    if target.exists() or _path_is_linklike(target):
        raise ArchiveMigrationConflict("archive rename target already exists")
    identity = _path_identity(source)
    linked = False
    try:
        os.link(source, target, follow_symlinks=False)
        linked = True
        if _path_identity(source) != identity or _path_identity(target) != identity:
            raise ArchiveMigrationConflict("archive file changed during rename")
        if root is not None:
            _require_regular_archive_parent(root, source)
            _require_regular_archive_parent(root, target)
        _fsync_directory(source.parent)
        if target.parent != source.parent:
            _fsync_directory(target.parent)
        source.unlink()
        _fsync_directory(source.parent)
    except OSError as exc:
        if isinstance(exc, FileExistsError):
            raise ArchiveMigrationConflict(
                "archive rename target already exists"
            ) from exc
        raise ArchiveMigrationError("archive file rename failed") from exc
    except BaseException:
        if linked and _path_present(source) and _path_present(target):
            try:
                if (
                    _path_identity(source) == identity
                    and _path_identity(target) == identity
                ):
                    if root is not None:
                        _require_regular_archive_parent(root, target)
                    target.unlink()
                    _fsync_directory(target.parent)
            except (OSError, ArchiveMigrationError):
                pass
        raise


def _require_regular_archive_parent(root: Path, path: Path) -> None:
    try:
        relative = path.parent.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ArchiveMigrationError("archive path escapes its root") from exc
    clean = PurePosixPath(*relative.parts) if relative.parts else PurePosixPath(".")
    directory = _existing_regular_directory_below_root(root, clean)
    if directory is None:
        raise ArchiveMigrationConflict("archive parent directory is unavailable")


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ArchiveMigrationError("archive path could not be inspected") from exc
    return True


def _fsync_move_directories(
    root: Path,
    moves: Sequence[Mapping[str, object]],
    keys: Sequence[str],
) -> None:
    directories = {
        _root_path(root, str(move[key])).parent for move in moves for key in keys
    }
    for directory in sorted(directories, key=lambda item: item.as_posix()):
        if not _path_present(directory):
            continue
        _require_regular_directory(directory, "archive durability directory")
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as exc:
        raise ArchiveMigrationError("archive directory durability sync failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_root_identity(root: Path, plan: Mapping[str, object]) -> None:
    current = _directory_identity(_require_regular_directory(root, "library root"))
    if current != plan.get("root_identity"):
        raise ArchiveMigrationConflict("archive root identity changed")


def _path_identity(path: Path) -> dict[str, int]:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ArchiveMigrationConflict("migration file is unavailable") from exc
    if _path_is_linklike(path) or not stat.S_ISREG(value.st_mode):
        raise ArchiveMigrationConflict("migration path is not a regular file")
    return _file_identity(value)


def _sha256_regular(path: Path) -> str:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise ArchiveMigrationConflict("migration file is unavailable") from exc
    digest, _ = _read_regular_digest(path, initial)
    return digest


def _root_path(root: Path, relative: str) -> Path:
    clean = PurePosixPath(relative)
    if (
        clean.is_absolute()
        or not clean.parts
        or any(part in {"", ".", ".."} for part in clean.parts)
        or clean.as_posix() != relative
    ):
        raise ArchiveMigrationError("migration journal path is invalid")
    return root.joinpath(*clean.parts)


def _path_exists(root: Path, relative: str) -> bool:
    path = _root_path(root, relative)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ArchiveMigrationError("archive path could not be inspected") from exc
    return True


def _has_staged_or_target_files(
    config: ArchiveMigrationConfig, plan: Mapping[str, object]
) -> bool:
    return any(
        _path_exists(config.library_root, str(move[key]))
        for move in _file_moves(_plan_entries(plan))
        for key in ("stage", "target")
    )


def _nfo_changes(plan: Mapping[str, object]) -> dict[str, str]:
    return {
        str(entry["source_nfo_path"]): str(entry["target_nfo_path"])
        for entry in _plan_entries(plan)
    }


def _plan_entries(plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    entries = plan.get("entries")
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) for item in entries
    ):
        raise ArchiveMigrationError("migration journal entries are invalid")
    return entries


def _plan_digest(plan: Mapping[str, object]) -> str:
    body = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _validate_digest(value: object) -> str:
    clean = str(value or "").strip().lower()
    if len(clean) != 64 or any(
        character not in "0123456789abcdef" for character in clean
    ):
        raise ArchiveMigrationError("migration plan digest is invalid")
    return clean


def _journal_plan(journal: Mapping[str, object]) -> dict[str, object]:
    plan = journal.get("plan")
    if not isinstance(plan, dict) or _plan_digest(plan) != journal.get("plan_digest"):
        raise ArchiveMigrationError("migration journal plan is invalid")
    return plan


def _require_journal_digest(journal: Mapping[str, object], expected: str) -> None:
    if journal.get("revision") != JOURNAL_REVISION:
        raise ArchiveMigrationError("migration journal revision is unsupported")
    if journal.get("plan_digest") != expected:
        raise ArchiveMigrationConflict("migration plan digest does not match journal")
    _journal_plan(journal)


def _read_journal_optional(config: ArchiveMigrationConfig) -> dict[str, object] | None:
    try:
        config.journal_path.lstat()
    except FileNotFoundError:
        return None
    return _read_journal(config)


def _read_journal(config: ArchiveMigrationConfig) -> dict[str, object]:
    path = config.journal_path
    value = _require_regular_file(path, "migration journal")
    if value.st_size <= 0 or value.st_size > 16 * 1024 * 1024:
        raise ArchiveMigrationError("migration journal size is invalid")
    try:
        body = path.read_bytes()
        payload = json.loads(body)
    except (OSError, ValueError) as exc:
        raise ArchiveMigrationError("migration journal could not be read") from exc
    if not isinstance(payload, dict):
        raise ArchiveMigrationError("migration journal is invalid")
    return payload


def _write_journal(
    config: ArchiveMigrationConfig, payload: Mapping[str, object]
) -> None:
    body = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n"
    )
    temporary = config.data_dir / f".{JOURNAL_NAME}.tmp"
    if temporary.exists() or _path_is_linklike(temporary):
        try:
            value = temporary.lstat()
        except OSError as exc:
            raise ArchiveMigrationError(
                "migration journal temporary is unsafe"
            ) from exc
        if _path_is_linklike(temporary) or not stat.S_ISREG(value.st_mode):
            raise ArchiveMigrationError("migration journal temporary is unsafe")
        temporary.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        written = 0
        while written < len(body):
            written += os.write(descriptor, body[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, config.journal_path)
        _fsync_directory(config.data_dir)
    except OSError as exc:
        raise ArchiveMigrationError("migration journal could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _public_result(
    action: str, digest: str, plan: Mapping[str, object]
) -> dict[str, object]:
    entries = _plan_entries(plan)
    skipped = plan.get("skipped")
    return {
        "ok": True,
        "action": action,
        "plan_digest": digest,
        "entry_count": len(entries),
        "skipped_count": len(skipped) if isinstance(skipped, list) else 0,
        "entries": [
            {
                "code": entry["display_code"],
                "source": entry["source_media_path"],
                "target": entry["target_media_path"],
            }
            for entry in entries
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jav_pilot.maintenance.archive_migration")
    parser.add_argument("action", choices=("preview", "apply", "rollback"))
    parser.add_argument("--digest")
    parser.add_argument("--library-root", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--web-database", type=Path)
    parser.add_argument("--metadata-database", type=Path)
    parser.add_argument("--media-library-database", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    config = ArchiveMigrationConfig.from_environment(
        library_root=args.library_root,
        data_dir=args.data_dir,
        web_database=args.web_database,
        metadata_database=args.metadata_database,
        media_library_database=args.media_library_database,
    )
    try:
        if args.action == "preview":
            result = preview_archive_migration(config)
        elif args.action == "apply":
            if not args.digest:
                raise ArchiveMigrationError("apply requires --digest from preview")
            result = apply_archive_migration(config, plan_digest=args.digest)
        else:
            if not args.digest:
                raise ArchiveMigrationError("rollback requires --digest from preview")
            result = rollback_archive_migration(config, plan_digest=args.digest)
    except ArchiveMigrationError as exc:
        print(
            json.dumps(
                {"ok": False, "action": args.action, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
