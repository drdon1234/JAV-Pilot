from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from xml.parsers import expat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Protocol

from ..core.catalog_code import normalize_catalog_code
from ..web_download.variant import (
    normalize_web_download_variant,
    web_download_variant_label,
)


VIDEO_SUFFIXES = frozenset(
    {
        ".avi",
        ".flv",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".rm",
        ".rmvb",
        ".ts",
        ".webm",
        ".wmv",
    }
)
MAX_LIBRARY_ENTRIES = 10_000
MAX_LIBRARY_DEPTH = 5
_LANDSCAPE_ASSET_STEMS = ("fanart", "backdrop", "landscape", "thumb")
_REQUIRED_LANDSCAPE_ASSET_COUNT = 2
MAX_NFO_BYTES = 2 * 1024 * 1024
_BACKUP_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_RENAME_EXCHANGE = 2
_INVALID_XML_TEXT_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)


class MetadataPublishError(RuntimeError):
    pass


class MetadataPublishConflict(MetadataPublishError):
    pass


class MetadataMigrationError(MetadataPublishError):
    def __init__(self, message: str, *, backup_path: str | None = None) -> None:
        self.backup_path = backup_path
        super().__init__(message)


class MetadataRecord(Protocol):
    code: str
    title: str
    original_title: str | None
    release_date: str | None
    duration_minutes: int | None
    rating: float | None
    makers: tuple[str, ...]
    publishers: tuple[str, ...]
    series: tuple[str, ...]
    directors: tuple[str, ...]
    actors: tuple[str, ...]
    tags: tuple[str, ...]
    description: str | None


@dataclass(frozen=True, slots=True)
class Artwork:
    body: bytes
    width: int
    height: int
    source_id: str


@dataclass(frozen=True, slots=True)
class PublishResult:
    media_path: str
    assets: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class MetadataAssetPlan:
    media_path: str
    assets: dict[str, dict[str, object]]
    needs_nfo: bool
    needs_portrait: bool
    needs_landscape: bool

    @property
    def complete(self) -> bool:
        return not (self.needs_nfo or self.needs_portrait or self.needs_landscape)

    @property
    def has_conflict(self) -> bool:
        return any(asset.get("status") == "conflict" for asset in self.assets.values())


@dataclass(frozen=True, slots=True)
class NfoTitleMigrationResult:
    status: str
    nfo_path: str
    backup_path: str | None = None


@dataclass(frozen=True, slots=True)
class NfoTitleInspectionResult:
    status: str
    nfo_path: str
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedNfoTitle:
    title: str
    content_start: int
    content_end: int


@dataclass(frozen=True, slots=True)
class _RegularFileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int
    uid: int
    gid: int
    xattrs: tuple[tuple[str, bytes], ...]


def build_movie_nfo(
    metadata: MetadataRecord,
    *,
    variant: object | None = None,
) -> bytes:
    normalized_code = normalize_catalog_code(metadata.code, max_length=40)
    display_code = normalized_code[0] if normalized_code else metadata.code
    root = ET.Element("movie")
    _text_node(root, "uniqueid", display_code, {"type": "jav", "default": "true"})
    _text_node(root, "id", display_code)
    _text_node(root, "plot", metadata.description)
    _text_node(root, "outline", metadata.description)
    for value in metadata.directors:
        _text_node(root, "director", value)
    for value in metadata.actors:
        actor = ET.SubElement(root, "actor")
        _text_node(actor, "name", value)
    for value in metadata.tags:
        _text_node(root, "genre", value)
    if metadata.rating is not None and 0 <= metadata.rating <= 10:
        _text_node(root, "rating", f"{metadata.rating:g}")
    for value in _dedupe_metadata_values((*metadata.makers, *metadata.publishers)):
        _text_node(root, "studio", value)
    for value in metadata.series:
        _text_node(root, "set", value)
    if metadata.duration_minutes is not None and metadata.duration_minutes > 0:
        _text_node(root, "runtime", str(metadata.duration_minutes))
    _text_node(
        root,
        "title",
        _title_with_catalog_code(display_code, metadata.title, variant=variant),
    )
    _text_node(root, "originaltitle", metadata.original_title)
    if metadata.release_date:
        _text_node(root, "premiered", metadata.release_date)
        _text_node(root, "releasedate", metadata.release_date)
        if len(metadata.release_date) >= 4 and metadata.release_date[:4].isdigit():
            _text_node(root, "year", metadata.release_date[:4])
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _title_with_catalog_code(
    code: object,
    title: object,
    *,
    variant: object | None = None,
) -> str:
    clean_title = " ".join(str(title or "").split())
    normalized_code = normalize_catalog_code(code, max_length=40)
    if normalized_code is None:
        return clean_title
    display_code, code_key = normalized_code
    remainder = _without_catalog_code_prefix(clean_title, code_key)
    visible_title = clean_title if remainder is None else remainder
    variant_prefix = ""
    if variant is not None:
        try:
            clean_variant = normalize_web_download_variant(variant)
        except ValueError as exc:
            raise MetadataPublishError("metadata Web variant is invalid") from exc
        variant_label = web_download_variant_label(clean_variant)
        visible_title = _without_variant_title_prefix(visible_title, variant_label)
        variant_prefix = f" [{variant_label}]"
    return f"[{display_code}]{variant_prefix} {visible_title}".strip()


def _without_variant_title_prefix(title: str, variant_label: str) -> str:
    for opening, closing in (("[", "]"), ("【", "】"), ("(", ")")):
        prefix = f"{opening}{variant_label}{closing}"
        if title.startswith(prefix):
            return title[len(prefix) :].lstrip(" :：-")
    return title


def _without_catalog_code_prefix(title: str, code_key: str) -> str | None:
    wrappers = {"[": "]", "(": ")", "【": "】"}
    closing_character = wrappers.get(title[:1])
    if closing_character:
        closing = title.find(closing_character, 1, 43)
        if closing < 0:
            return None
        bracketed = normalize_catalog_code(title[1:closing], max_length=40)
        if bracketed is None or bracketed[1] != code_key:
            return None
        return _clean_title_remainder(title[closing + 1 :])

    candidate: list[str] = []
    for index, character in enumerate(title):
        folded = unicodedata.normalize("NFKC", character).upper()
        if folded and all(item.isalnum() for item in folded):
            candidate.extend(folded)
            if len(candidate) == len(code_key):
                if "".join(candidate) != code_key:
                    return None
                if index + 1 < len(title) and unicodedata.normalize(
                    "NFKC", title[index + 1]
                ).isalnum():
                    return None
                return _clean_title_remainder(title[index + 1 :])
            if len(candidate) > len(code_key):
                return None
        elif not candidate:
            return None
    return None


def _clean_title_remainder(value: str) -> str:
    return value.lstrip(" :：-")


def _leading_catalog_code_key(title: str) -> str | None:
    wrappers = {"[": "]", "(": ")", "【": "】"}
    closing_character = wrappers.get(title[:1])
    if closing_character:
        closing = title.find(closing_character, 1, 43)
        candidate = title[1:closing] if closing > 0 else ""
    else:
        candidate = title.split(maxsplit=1)[0].rstrip(" :：-") if title else ""
    normalized = normalize_catalog_code(candidate, max_length=40)
    return normalized[1] if normalized is not None else None


def publish_movie_metadata(
    *,
    library_root: Path,
    media_file: Path,
    metadata: MetadataRecord,
    portrait: Artwork | None,
    landscape: Artwork | None,
    variant: object | None = None,
) -> PublishResult:
    root = _regular_root(library_root)
    media = _regular_media_file(media_file, root)
    directory = media.parent
    assets: dict[str, dict[str, object]] = {}

    with _open_publish_directory(directory, root) as directory_fd:
        nfo_name, portrait_name, landscape_names = _movie_asset_names(media, root)
        image_payloads: list[tuple[str, Artwork | None]] = [
            (portrait_name, portrait),
            *((name, landscape) for name in landscape_names),
        ]
        for name, artwork in image_payloads:
            target = directory / name
            if artwork is None:
                assets[name] = _existing_or_missing(
                    target,
                    directory_fd=directory_fd,
                )
                continue
            status_value = _publish_no_replace(
                target,
                artwork.body,
                directory,
                directory_fd=directory_fd,
            )
            assets[name] = {
                "status": status_value,
                "source_id": artwork.source_id,
                "width": artwork.width,
                "height": artwork.height,
            }

        nfo_target = directory / nfo_name
        nfo_status = _publish_no_replace(
            nfo_target,
            build_movie_nfo(metadata, variant=variant),
            directory,
            directory_fd=directory_fd,
        )
        assets[nfo_target.name] = {"status": nfo_status}
        _fsync_directory(directory, descriptor=directory_fd)
    return PublishResult(
        media_path=media.relative_to(root).as_posix(),
        assets=assets,
    )


def inspect_movie_metadata_assets(
    *,
    library_root: Path,
    media_file: Path,
) -> MetadataAssetPlan:
    """Inspect local targets before any remote metadata work is started."""

    root = _regular_root(library_root)
    media = _regular_media_file(media_file, root)
    directory = media.parent
    nfo_name, portrait_name, landscape_names = _movie_asset_names(media, root)
    names = (nfo_name, portrait_name, *landscape_names)
    with _open_publish_directory(directory, root) as directory_fd:
        assets = {
            name: _existing_or_missing(
                directory / name,
                directory_fd=directory_fd,
            )
            for name in names
        }
    return MetadataAssetPlan(
        media_path=media.relative_to(root).as_posix(),
        assets=assets,
        needs_nfo=assets[nfo_name]["status"] != "existing",
        needs_portrait=assets[portrait_name]["status"] != "existing",
        needs_landscape=any(
            assets[name]["status"] != "existing"
            for name in landscape_names[:_REQUIRED_LANDSCAPE_ASSET_COUNT]
        ),
    )


def migrate_movie_nfo_title(
    *,
    library_root: Path,
    media_file: Path,
    code: object,
    backup_root: Path,
    backup_run_id: str,
    expected_sha256: str,
) -> NfoTitleMigrationResult:
    """Prefix a verified legacy NFO title after preserving its original bytes."""

    normalized_code = normalize_catalog_code(code, max_length=40)
    if normalized_code is None:
        raise MetadataPublishError("metadata catalog code is invalid")
    display_code, code_key = normalized_code
    if not _BACKUP_RUN_ID_RE.fullmatch(str(backup_run_id or "")):
        raise MetadataPublishError("metadata backup run id is invalid")
    clean_expected_sha256 = str(expected_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean_expected_sha256):
        raise MetadataPublishError("metadata NFO preview digest is invalid")

    root = _regular_root(library_root)
    media = _regular_media_file(media_file, root)
    directory = media.parent
    nfo_name, _, _ = _movie_asset_names(media, root)
    nfo_target = directory / nfo_name
    relative_nfo = nfo_target.relative_to(root).as_posix()

    with _open_publish_directory(directory, root) as directory_fd:
        state = _existing_or_missing(
            nfo_target,
            directory_fd=directory_fd,
        )["status"]
        if state == "missing":
            return NfoTitleMigrationResult("missing", relative_nfo)
        if state != "existing":
            return NfoTitleMigrationResult("invalid", relative_nfo)

        original, identity = _read_regular_nfo(
            nfo_target,
            directory_fd=directory_fd,
        )
        if hashlib.sha256(original).hexdigest() != clean_expected_sha256:
            raise MetadataPublishConflict("metadata NFO changed after preview")
        parsed = _verified_legacy_movie_nfo(original, code_key)
        if parsed is None:
            return NfoTitleMigrationResult("invalid", relative_nfo)
        current_title = " ".join(parsed.title.split())
        if _without_catalog_code_prefix(current_title, code_key) is not None:
            return NfoTitleMigrationResult("current", relative_nfo)
        existing_prefix = _leading_catalog_code_key(current_title)
        if existing_prefix is not None and existing_prefix != code_key:
            return NfoTitleMigrationResult("invalid", relative_nfo)

        prefix = f"[{display_code}]" + (" " if parsed.title else "")
        migrated = (
            original[: parsed.content_start]
            + prefix.encode("ascii")
            + original[parsed.content_start :]
        )
        if migrated == original:
            return NfoTitleMigrationResult("current", relative_nfo)
        verified_migration = _verified_legacy_movie_nfo(migrated, code_key)
        if verified_migration is None or _without_catalog_code_prefix(
            " ".join(verified_migration.title.split()),
            code_key,
        ) is None:
            raise MetadataPublishError("metadata NFO migration could not be verified")

        with _open_nfo_backup_target(
            backup_root=backup_root,
            backup_run_id=backup_run_id,
            relative_nfo=relative_nfo,
        ) as (backup_target, backup_directory_fd):
            backup_status = _publish_no_replace(
                backup_target,
                original,
                backup_target.parent,
                directory_fd=backup_directory_fd,
            )
            if backup_status not in {"generated", "existing"}:
                raise MetadataPublishError("metadata backup could not be published")
            backup_body, _ = _read_regular_nfo(
                backup_target,
                directory_fd=backup_directory_fd,
            )
            if backup_body != original:
                raise MetadataPublishConflict("metadata backup does not match the source")

        try:
            _replace_regular_file_if_unchanged(
                nfo_target,
                migrated,
                original,
                identity,
                directory,
                directory_fd=directory_fd,
            )
            _fsync_directory(directory, descriptor=directory_fd)
        except (MetadataPublishError, OSError) as exc:
            raise MetadataMigrationError(
                "metadata NFO replacement failed",
                backup_path=backup_target.as_posix(),
            ) from exc
    return NfoTitleMigrationResult(
        "migrated",
        relative_nfo,
        backup_target.as_posix(),
    )


def inspect_movie_nfo_title(
    *,
    library_root: Path,
    media_file: Path,
    code: object,
) -> NfoTitleInspectionResult:
    normalized_code = normalize_catalog_code(code, max_length=40)
    if normalized_code is None:
        raise MetadataPublishError("metadata catalog code is invalid")
    _, code_key = normalized_code
    root = _regular_root(library_root)
    media = _regular_media_file(media_file, root)
    directory = media.parent
    nfo_name, _, _ = _movie_asset_names(media, root)
    nfo_target = directory / nfo_name
    relative_nfo = nfo_target.relative_to(root).as_posix()
    with _open_publish_directory(directory, root) as directory_fd:
        state = _existing_or_missing(
            nfo_target,
            directory_fd=directory_fd,
        )["status"]
        if state == "missing":
            return NfoTitleInspectionResult("missing", relative_nfo)
        if state != "existing":
            return NfoTitleInspectionResult("invalid", relative_nfo)
        original, _ = _read_regular_nfo(
            nfo_target,
            directory_fd=directory_fd,
        )
    digest = hashlib.sha256(original).hexdigest()
    parsed = _verified_legacy_movie_nfo(original, code_key)
    if parsed is None:
        return NfoTitleInspectionResult("invalid", relative_nfo, digest)
    current_title = " ".join(parsed.title.split())
    if _without_catalog_code_prefix(current_title, code_key) is not None:
        return NfoTitleInspectionResult("current", relative_nfo, digest)
    existing_prefix = _leading_catalog_code_key(current_title)
    if existing_prefix is not None and existing_prefix != code_key:
        return NfoTitleInspectionResult("invalid", relative_nfo, digest)
    return NfoTitleInspectionResult("ready", relative_nfo, digest)


def _movie_asset_names(
    media: Path,
    root: Path,
) -> tuple[str, str, tuple[str, ...]]:
    image_prefix = f"{media.stem}-" if media.parent == root else ""
    return (
        f"{media.stem}.nfo",
        f"{image_prefix}poster.jpg",
        tuple(f"{image_prefix}{name}.jpg" for name in _LANDSCAPE_ASSET_STEMS),
    )


def select_primary_video(directory: Path, library_root: Path) -> Path:
    root = _regular_root(library_root)
    target = _regular_directory_within(directory, root)
    candidates: list[tuple[int, str, Path]] = []
    pending: list[tuple[Path, int]] = [(target, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        try:
            entries = tuple(os.scandir(current))
        except OSError as exc:
            raise MetadataPublishError("media directory could not be read") from exc
        visited += len(entries)
        if visited > MAX_LIBRARY_ENTRIES:
            raise MetadataPublishError("media directory contains too many entries")
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            if stat.S_ISLNK(entry_stat.st_mode):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                if depth < MAX_LIBRARY_DEPTH and not path.name.startswith("."):
                    pending.append((path, depth + 1))
                continue
            if (
                stat.S_ISREG(entry_stat.st_mode)
                and not path.name.startswith(".")
                and path.suffix.lower() in VIDEO_SUFFIXES
                and entry_stat.st_size > 0
            ):
                relative = path.relative_to(root).as_posix()
                candidates.append((entry_stat.st_size, relative, path))
    if not candidates:
        raise MetadataPublishError("no completed video file was found")
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return _regular_media_file(candidates[0][2], root)


def safe_relative_media_path(value: object) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 1024
        or raw.startswith(("/", "\\"))
        or "\\" in raw
        or "\x00" in raw
        or any(ord(character) < 32 for character in raw)
    ):
        raise MetadataPublishError("media path is invalid")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MetadataPublishError("media path is invalid")
    normalized = Path(*parts).as_posix()
    if normalized != raw or Path(normalized).suffix.lower() not in VIDEO_SUFFIXES:
        raise MetadataPublishError("media path is invalid")
    return normalized


def _text_node(
    parent: ET.Element,
    tag: str,
    value: object,
    attributes: dict[str, str] | None = None,
) -> None:
    clean = " ".join(_INVALID_XML_TEXT_RE.sub(" ", str(value or "")).split())
    if not clean:
        return
    node = ET.SubElement(parent, tag, attributes or {})
    node.text = clean


def _dedupe_metadata_values(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = " ".join(str(value or "").split())
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return tuple(result)


def _regular_root(path: Path) -> Path:
    try:
        path_stat = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MetadataPublishError("metadata library is unavailable") from exc
    if not stat.S_ISDIR(path_stat.st_mode) or path.is_symlink():
        raise MetadataPublishError("metadata library must be a regular directory")
    return resolved


def _regular_directory_within(path: Path, root: Path) -> Path:
    try:
        path_stat = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MetadataPublishError("media directory is unavailable") from exc
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or path.is_symlink()
        or not resolved.is_relative_to(root)
    ):
        raise MetadataPublishError("media directory is unsafe")
    _reject_linked_parents(resolved, root)
    return resolved


def _regular_media_file(path: Path, root: Path) -> Path:
    try:
        path_stat = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MetadataPublishError("media file is unavailable") from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path.is_symlink()
        or path.suffix.lower() not in VIDEO_SUFFIXES
        or path_stat.st_size <= 0
        or not resolved.is_relative_to(root)
    ):
        raise MetadataPublishError("media file is unsafe")
    _reject_linked_parents(resolved.parent, root)
    return resolved


def _reject_linked_parents(path: Path, root: Path) -> None:
    current = path
    while current != root:
        try:
            current_stat = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise MetadataPublishError("media path is unavailable") from exc
        if not stat.S_ISDIR(current_stat.st_mode) or current.is_symlink():
            raise MetadataPublishError("media path contains a link")
        current = current.parent


@contextmanager
def _open_publish_directory(directory: Path, root: Path) -> Iterator[int | None]:
    if os.name == "nt":
        yield None
        return
    if not _secure_directory_operations_supported():
        raise MetadataPublishError(
            "secure metadata directory operations are unavailable"
        )

    try:
        relative = directory.relative_to(root)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current_fd = os.open(root, flags)
        try:
            for part in relative.parts:
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            _verify_open_directory(directory, current_fd)
        except BaseException:
            os.close(current_fd)
            raise
    except MetadataPublishError:
        raise
    except (OSError, ValueError) as exc:
        raise MetadataPublishError("metadata directory could not be secured") from exc

    try:
        yield current_fd
    finally:
        os.close(current_fd)


def _existing_or_missing(
    path: Path,
    *,
    directory_fd: int | None = None,
) -> dict[str, object]:
    if directory_fd is None:
        if os.path.lexists(path):
            return {
                "status": "existing"
                if path.is_file() and not path.is_symlink()
                else "conflict"
            }
        return {"status": "missing"}
    try:
        entry_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as exc:
        raise MetadataPublishError("metadata target could not be inspected") from exc
    return {"status": "existing" if stat.S_ISREG(entry_stat.st_mode) else "conflict"}


def _read_regular_nfo(
    target: Path,
    *,
    directory_fd: int | None,
) -> tuple[bytes, _RegularFileIdentity]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target if directory_fd is None else target.name,
            flags,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_NFO_BYTES:
            raise MetadataPublishConflict("metadata NFO is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise MetadataPublishError("metadata NFO changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MetadataPublishError("metadata NFO changed while being read")
        body = b"".join(chunks)
        current = (
            os.stat(target, follow_symlinks=False)
            if directory_fd is None
            else os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        )
        identity = _RegularFileIdentity(
            device=opened.st_dev,
            inode=opened.st_ino,
            size=opened.st_size,
            modified_ns=opened.st_mtime_ns,
            mode=stat.S_IMODE(opened.st_mode),
            uid=opened.st_uid,
            gid=opened.st_gid,
            xattrs=_descriptor_xattrs(descriptor),
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != identity.device
            or current.st_ino != identity.inode
            or current.st_size != identity.size
            or current.st_mtime_ns != identity.modified_ns
            or stat.S_IMODE(current.st_mode) != identity.mode
            or current.st_uid != identity.uid
            or current.st_gid != identity.gid
        ):
            raise MetadataPublishError("metadata NFO identity changed")
        return body, identity
    except (MetadataPublishError, MetadataPublishConflict):
        raise
    except OSError as exc:
        raise MetadataPublishError("metadata NFO could not be read safely") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _descriptor_xattrs(descriptor: int) -> tuple[tuple[str, bytes], ...]:
    if os.name == "nt" or not hasattr(os, "listxattr"):
        return ()
    try:
        return tuple(
            (name, os.getxattr(descriptor, name))
            for name in sorted(os.listxattr(descriptor))
        )
    except OSError as exc:
        raise MetadataPublishError("metadata NFO attributes could not be read") from exc


def _verified_legacy_movie_nfo(
    body: bytes,
    expected_code_key: str,
) -> _VerifiedNfoTitle | None:
    if (
        b"\x00" in body
        or body.startswith((b"\xff\xfe", b"\xfe\xff"))
        or body.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"))
    ):
        return None
    try:
        body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    stack: list[str] = []
    values = {"id": [], "title": [], "uniqueid": []}
    counts = {"id": 0, "title": 0, "uniqueid": 0}
    unique_attributes: list[dict[str, str]] = []
    title_start: int | None = None
    title_end: int | None = None
    declared_encoding: str | None = None
    invalid = False
    parser = expat.ParserCreate()

    def start_element(name: str, attributes: dict[str, str]) -> None:
        nonlocal invalid, title_start
        depth = len(stack)
        if depth == 0:
            if name != "movie":
                invalid = True
        elif depth == 1 and name in values:
            counts[name] += 1
            if name == "uniqueid":
                unique_attributes.append(dict(attributes))
            if name == "title":
                title_start = _xml_element_content_start(
                    body,
                    parser.CurrentByteIndex,
                )
                if title_start is None:
                    invalid = True
        elif len(stack) >= 2 and stack[1] in values:
            invalid = True
        stack.append(name)

    def character_data(value: str) -> None:
        if len(stack) == 2 and stack[-1] in values:
            values[stack[-1]].append(value)

    def end_element(name: str) -> None:
        nonlocal invalid, title_end
        if not stack or stack[-1] != name:
            invalid = True
            return
        if len(stack) == 2 and name == "title":
            title_end = parser.CurrentByteIndex
        stack.pop()

    def xml_declaration(
        _version: str,
        encoding: str | None,
        _standalone: int,
    ) -> None:
        nonlocal declared_encoding
        declared_encoding = encoding

    def reject_doctype(*_args: object) -> None:
        nonlocal invalid
        invalid = True

    parser.StartElementHandler = start_element
    parser.CharacterDataHandler = character_data
    parser.EndElementHandler = end_element
    parser.XmlDeclHandler = xml_declaration
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    try:
        parser.Parse(body, True)
    except (expat.ExpatError, UnicodeError, ValueError):
        return None
    if declared_encoding and declared_encoding.replace("_", "-").casefold() not in {
        "utf-8",
        "utf8",
    }:
        return None
    if (
        invalid
        or stack
        or counts != {"id": 1, "title": 1, "uniqueid": 1}
        or len(unique_attributes) != 1
        or title_start is None
        or title_end is None
        or title_end < title_start
    ):
        return None
    unique_attributes_value = unique_attributes[0]
    if (
        str(unique_attributes_value.get("type") or "").strip().casefold() != "jav"
        or str(unique_attributes_value.get("default") or "").strip().casefold()
        != "true"
    ):
        return None
    identities = (
        normalize_catalog_code("".join(values["id"]), max_length=40),
        normalize_catalog_code("".join(values["uniqueid"]), max_length=40),
    )
    if any(item is None or item[1] != expected_code_key for item in identities):
        return None
    return _VerifiedNfoTitle(
        title="".join(values["title"]),
        content_start=title_start,
        content_end=title_end,
    )


def _xml_element_content_start(body: bytes, start: int) -> int | None:
    quote: int | None = None
    for index in range(start, min(len(body), start + 64 * 1024)):
        value = body[index]
        if quote is not None:
            if value == quote:
                quote = None
            continue
        if value in {ord("'"), ord('"')}:
            quote = value
        elif value == ord(">"):
            if body[start:index].rstrip().endswith(b"/"):
                return None
            return index + 1
    return None


@contextmanager
def _open_nfo_backup_target(
    *,
    backup_root: Path,
    backup_run_id: str,
    relative_nfo: str,
) -> Iterator[tuple[Path, int | None]]:
    root = Path(backup_root)
    if not root.is_absolute():
        raise MetadataPublishError("metadata backup path must be absolute")
    if any(part in {"", ".", ".."} for part in root.parts[1:]):
        raise MetadataPublishError("metadata backup path is invalid")
    relative = PurePosixPath(relative_nfo)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise MetadataPublishError("metadata backup target is invalid")
    directory_parts = (*root.parts[1:], backup_run_id, *relative.parts[:-1])

    if os.name == "nt":
        target = root.joinpath(backup_run_id, *relative.parts)
        try:
            current = Path(target.anchor)
            for part in target.parent.parts[1:]:
                next_path = current / part
                if os.path.lexists(next_path):
                    if _path_is_link_or_reparse(next_path):
                        raise MetadataPublishError(
                            "metadata backup path contains a link"
                        )
                else:
                    next_path.mkdir(mode=0o700)
                if _path_is_link_or_reparse(next_path):
                    raise MetadataPublishError("metadata backup path contains a link")
                current = next_path
        except MetadataPublishError:
            raise
        except OSError as exc:
            raise MetadataPublishError("metadata backup target is unavailable") from exc
        yield target, None
        return

    if not _secure_directory_operations_supported():
        raise MetadataPublishError("secure metadata backup operations are unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd: int | None = None
    current_path = Path(root.anchor)
    try:
        current_fd = os.open(root.anchor, flags)
        for part in directory_parts:
            created = False
            try:
                os.mkdir(part, 0o700, dir_fd=current_fd)
                created = True
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise MetadataPublishError("metadata backup path is unsafe")
            if created:
                os.fsync(current_fd)
            os.close(current_fd)
            current_fd = next_fd
            current_path /= part
        yield current_path / relative.name, current_fd
    except MetadataPublishError:
        raise
    except OSError as exc:
        raise MetadataPublishError("metadata backup target is unavailable") from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _path_is_link_or_reparse(path: Path) -> bool:
    entry = path.stat(follow_symlinks=False)
    attributes = int(getattr(entry, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return (
        not stat.S_ISDIR(entry.st_mode)
        or path.is_symlink()
        or bool(attributes & reparse_flag)
    )


def _replace_regular_file_if_unchanged(
    target: Path,
    body: bytes,
    original: bytes,
    identity: _RegularFileIdentity,
    directory: Path,
    *,
    directory_fd: int | None,
) -> None:
    if not body:
        raise MetadataPublishError("metadata replacement is empty")
    _verify_open_directory(directory, directory_fd)
    temporary_name = f".jav-pilot-nfo-replace-{uuid.uuid4().hex}.tmp"
    temporary_path = directory / temporary_name
    published = False
    preserve_temporary = False
    descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(
            temporary_path if directory_fd is None else temporary_name,
            flags,
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("metadata replacement write stopped")
            view = view[written:]
        _apply_file_metadata(descriptor, identity)
        os.fsync(descriptor)
        replacement_stat = os.fstat(descriptor)
        if not stat.S_ISREG(replacement_stat.st_mode):
            raise MetadataPublishError("metadata replacement is not a regular file")
        replacement_identity = (replacement_stat.st_dev, replacement_stat.st_ino)
        os.close(descriptor)
        descriptor = None

        _verify_open_directory(directory, directory_fd)
        if directory_fd is None:
            current_body, current_identity = _read_regular_nfo(
                target,
                directory_fd=None,
            )
            if current_identity != identity or current_body != original:
                raise MetadataPublishConflict("metadata NFO changed before replacement")
            os.replace(temporary_path, target)
            published = True
        else:
            _rename_exchange(
                temporary_name,
                target.name,
                directory_fd=directory_fd,
            )
            exchanged = True
            try:
                exchanged_body, exchanged_identity = _read_regular_nfo(
                    directory / temporary_name,
                    directory_fd=directory_fd,
                )
                if exchanged_identity != identity or exchanged_body != original:
                    raise MetadataPublishConflict(
                        "metadata NFO changed before atomic exchange"
                    )
                os.unlink(temporary_name, dir_fd=directory_fd)
                exchanged = False
                published = True
            except BaseException:
                if exchanged:
                    preserve_temporary = True
                    if _directory_entry_has_identity(
                        target.name,
                        replacement_identity,
                        directory_fd=directory_fd,
                    ):
                        _rename_exchange(
                            temporary_name,
                            target.name,
                            directory_fd=directory_fd,
                        )
                        preserve_temporary = False
                raise
        published_body, published_identity = _read_regular_nfo(
            target,
            directory_fd=directory_fd,
        )
        if published_body != body or (
            published_identity.mode,
            published_identity.uid,
            published_identity.gid,
            published_identity.xattrs,
        ) != (
            identity.mode,
            identity.uid,
            identity.gid,
            identity.xattrs,
        ):
            raise MetadataPublishError("metadata replacement could not be verified")
    except (MetadataPublishError, MetadataPublishConflict):
        raise
    except OSError as exc:
        raise MetadataPublishError("metadata NFO could not be replaced") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not published and not preserve_temporary:
            try:
                if directory_fd is None:
                    if temporary_path.is_file() and not temporary_path.is_symlink():
                        temporary_path.unlink()
                else:
                    entry = os.stat(
                        temporary_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISREG(entry.st_mode):
                        os.unlink(temporary_name, dir_fd=directory_fd)
            except (FileNotFoundError, OSError):
                pass


def _apply_file_metadata(
    descriptor: int,
    identity: _RegularFileIdentity,
) -> None:
    try:
        if os.name != "nt" and hasattr(os, "fchown"):
            os.fchown(descriptor, identity.uid, identity.gid)
        os.fchmod(descriptor, identity.mode)
        if os.name != "nt":
            listxattr = getattr(os, "listxattr", None)
            removexattr = getattr(os, "removexattr", None)
            setxattr = getattr(os, "setxattr", None)
            getxattr = getattr(os, "getxattr", None)
            if not all(
                callable(function)
                for function in (listxattr, removexattr, setxattr, getxattr)
            ):
                raise MetadataPublishError(
                    "metadata replacement xattr operations are unavailable"
                )
            expected = dict(identity.xattrs)
            for name in tuple(listxattr(descriptor)):
                if name not in expected:
                    removexattr(descriptor, name)
            for name, value in identity.xattrs:
                setxattr(descriptor, name, value)
            actual = tuple(
                (name, getxattr(descriptor, name))
                for name in sorted(listxattr(descriptor))
            )
            if actual != identity.xattrs:
                raise MetadataPublishError(
                    "metadata replacement xattrs could not be preserved"
                )
    except OSError as exc:
        raise MetadataPublishError(
            "metadata replacement attributes could not be preserved"
        ) from exc


def _rename_exchange(
    source_name: str,
    target_name: str,
    *,
    directory_fd: int,
) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise MetadataPublishError("atomic metadata exchange is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        directory_fd,
        os.fsencode(source_name),
        directory_fd,
        os.fsencode(target_name),
        _RENAME_EXCHANGE,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _directory_entry_has_identity(
    name: str,
    identity: tuple[int, int],
    *,
    directory_fd: int,
) -> bool:
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(entry.st_mode) and (entry.st_dev, entry.st_ino) == identity


def _publish_no_replace(
    target: Path,
    body: bytes,
    directory: Path,
    *,
    directory_fd: int | None = None,
) -> str:
    if not isinstance(body, bytes) or not body:
        raise MetadataPublishError("metadata artifact is empty")
    _verify_open_directory(directory, directory_fd)
    existing = _existing_or_missing(target, directory_fd=directory_fd)["status"]
    if existing == "existing":
        return "existing"
    if existing == "conflict":
        raise MetadataPublishConflict(
            f"metadata target {target.name} is not a regular file"
        )
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        if directory_fd is None:
            temporary = directory / (
                f".jav-pilot-metadata-publish-{uuid.uuid4().hex}.tmp"
            )
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
        else:
            descriptor = os.open(
                ".",
                os.O_WRONLY | os.O_TMPFILE,
                0o600,
                dir_fd=directory_fd,
            )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("metadata artifact write stopped")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise MetadataPublishError("metadata artifact is not a regular file")
        _verify_open_directory(directory, directory_fd)
        try:
            expected_identity = _link_descriptor_no_replace(
                descriptor,
                temporary=temporary,
                target=target,
                directory_fd=directory_fd,
            )
        except FileExistsError:
            state = _existing_or_missing(
                target,
                directory_fd=directory_fd,
            )["status"]
            if state == "existing":
                return "existing"
            raise MetadataPublishConflict(f"metadata target {target.name} is unsafe")
        target_stat = (
            os.stat(target, follow_symlinks=False)
            if directory_fd is None
            else os.stat(
                target.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        )
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or (target_stat.st_dev, target_stat.st_ino) != expected_identity
        ):
            raise MetadataPublishError("metadata target identity changed")
        _verify_open_directory(directory, directory_fd)
        _fsync_directory(directory, descriptor=directory_fd)
        return "generated"
    except MetadataPublishError:
        raise
    except OSError as exc:
        raise MetadataPublishError("metadata artifact could not be published") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                if temporary.is_file() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass


def _link_descriptor_no_replace(
    descriptor: int,
    *,
    temporary: Path | None,
    target: Path,
    directory_fd: int | None,
) -> tuple[int, int]:
    if directory_fd is None:
        if temporary is None:
            raise MetadataPublishError("metadata temporary path is unavailable")
        try:
            os.link(temporary, target, follow_symlinks=False)
            source_stat = os.fstat(descriptor)
            return source_stat.st_dev, source_stat.st_ino
        except FileExistsError:
            raise
        except OSError:
            return _copy_descriptor_no_replace(
                descriptor, target=target, directory_fd=None
            )
    descriptor_path = f"/proc/self/fd/{descriptor}"
    descriptor_stat = os.fstat(descriptor)
    try:
        proc_stat = os.stat(descriptor_path, follow_symlinks=True)
    except OSError as exc:
        raise MetadataPublishError(
            "secure metadata descriptor linking is unavailable"
        ) from exc
    if (proc_stat.st_dev, proc_stat.st_ino) != (
        descriptor_stat.st_dev,
        descriptor_stat.st_ino,
    ):
        raise MetadataPublishError("metadata descriptor identity changed")
    try:
        os.link(
            descriptor_path,
            target.name,
            dst_dir_fd=directory_fd,
            follow_symlinks=True,
        )
        return descriptor_stat.st_dev, descriptor_stat.st_ino
    except FileExistsError:
        raise
    except OSError:
        return _copy_descriptor_no_replace(
            descriptor, target=target, directory_fd=directory_fd
        )


def _copy_descriptor_no_replace(
    source: int,
    *,
    target: Path,
    directory_fd: int | None,
) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    destination = os.open(
        target if directory_fd is None else target.name,
        flags,
        0o644,
        dir_fd=directory_fd,
    )
    try:
        os.lseek(source, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written <= 0:
                    raise OSError("metadata artifact copy stopped")
                view = view[written:]
        os.fsync(destination)
        target_stat = os.fstat(destination)
        return target_stat.st_dev, target_stat.st_ino
    except BaseException:
        try:
            os.unlink(target if directory_fd is None else target.name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(destination)


def _secure_directory_operations_supported() -> bool:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_TMPFILE")
    ):
        return False
    dir_fd_functions = getattr(os, "supports_dir_fd", set())
    follow_functions = getattr(os, "supports_follow_symlinks", set())
    return (
        all(
            function in dir_fd_functions
            for function in (os.open, os.stat, os.link, os.unlink)
        )
        and os.stat in follow_functions
        and os.link in follow_functions
    )


def _verify_open_directory(directory: Path, descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        expected = os.stat(directory, follow_symlinks=False)
        actual = os.fstat(descriptor)
    except OSError as exc:
        raise MetadataPublishError("metadata directory identity changed") from exc
    if (
        not stat.S_ISDIR(expected.st_mode)
        or not stat.S_ISDIR(actual.st_mode)
        or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)
    ):
        raise MetadataPublishError("metadata directory identity changed")


def _fsync_directory(directory: Path, *, descriptor: int | None = None) -> None:
    if os.name == "nt":
        return
    if descriptor is not None:
        os.fsync(descriptor)
        return
    opened = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(opened)
    finally:
        os.close(opened)


def assets_json(assets: dict[str, dict[str, object]]) -> str:
    return json.dumps(assets, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
