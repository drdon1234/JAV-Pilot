"""Filesystem helpers for scanning library roots without following links."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..web_download.variant import normalize_web_download_variant
from .errors import MediaLibraryError, MediaLibraryUnavailableError
from .models import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    MAX_REFRESH_PATHS,
    PART_SUFFIX_RE,
    QUALITY_RE,
    SAFE_RELATIVE_MAX,
    SCAN_CODE_RE,
    SCAN_LAYOUT_NAME_RE,
    MutableMetrics,
)

def regular_root(
    raw_root: Path | str, metrics: MutableMetrics
) -> tuple[Path, os.stat_result]:
    path = Path(raw_root)
    if not path.is_absolute():
        raise MediaLibraryUnavailableError("media library root must be absolute")
    absolute = Path(os.path.abspath(path))
    try:
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            current_stat = counted_lstat(current, metrics)
            if is_linklike(current_stat):
                raise MediaLibraryUnavailableError(
                    "media library root contains an unsafe link"
                )
        root_stat = counted_lstat(absolute, metrics)
        if is_linklike(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise MediaLibraryUnavailableError("media library root is unavailable")
        resolved = absolute.resolve(strict=True)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
            raise MediaLibraryUnavailableError(
                "media library root resolves outside its configured path"
            )
        return resolved, root_stat
    except MediaLibraryUnavailableError:
        raise
    except OSError as exc:
        raise MediaLibraryUnavailableError("media library root is unavailable") from exc


def scandir_snapshot(
    directory: Path, metrics: MutableMetrics
) -> list[tuple[str, os.stat_result]]:
    metrics.scandir_calls += 1
    result: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                metrics.entries_seen += 1
                metrics.stat_calls += 1
                try:
                    # DirEntry may cache Windows find data without stable device/inode
                    # values. Use the same uncached lstat source as the media probe.
                    entry_stat = Path(entry.path).lstat()
                except OSError as exc:
                    raise MediaLibraryUnavailableError(
                        "media library entry could not be inspected"
                    ) from exc
                result.append((entry.name, entry_stat))
    except MediaLibraryUnavailableError:
        raise
    except OSError as exc:
        raise MediaLibraryUnavailableError(
            "media library directory could not be scanned"
        ) from exc
    result.sort(key=lambda item: os.fsencode(item[0]))
    return result


def counted_lstat(path: Path, metrics: MutableMetrics) -> os.stat_result:
    metrics.stat_calls += 1
    return path.lstat()


def require_within_root(path: Path, root: Path, *, directory: bool) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise MediaLibraryUnavailableError(
            "media library path escaped its root"
        ) from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise MediaLibraryUnavailableError("media library relative path is invalid")
    parent = path if directory else path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise MediaLibraryUnavailableError(
            "media library path escaped its root"
        ) from exc
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise MediaLibraryUnavailableError("media library path is unavailable") from exc
    if is_linklike(parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
        raise MediaLibraryUnavailableError("media library path contains an unsafe link")


def is_linklike(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & FILE_ATTRIBUTE_REPARSE_POINT
    )


def stat_identity(value: os.stat_result) -> tuple[str, str]:
    return identity_scalar(value.st_dev), identity_scalar(value.st_ino)


def stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def regular_file_fingerprint(
    value: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        stat.S_IFMT(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def identity_scalar(value: object) -> str:
    try:
        clean = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaLibraryError("media library file identity is invalid") from exc
    if clean < 0:
        raise MediaLibraryError("media library file identity is invalid")
    return str(clean)


def optional_web_download_variant(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise MediaLibraryError("media library variant is invalid") from exc


def make_root_key(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(os.fsencode(normalized)).hexdigest()


def make_entry_id(
    root_key: str,
    scope_path: str,
    code_key: str | None,
    variant: object | None = None,
) -> str:
    clean_variant = optional_web_download_variant(variant)
    payload = "\0".join(
        (
            root_key,
            scope_path,
            code_key or "<unidentified>",
            clean_variant or "<unknown-variant>",
        )
    )
    return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


def entry_scope(media: Path, root: Path, code_key: str | None) -> str:
    relative = relative_from_root(media, root)
    parent = media.parent
    while parent != root:
        detected = _scan_media_code(parent.name)
        if detected is not None and (code_key is None or detected[1] == code_key):
            return relative_from_root(parent, root)
        parent = parent.parent
    if code_key is not None:
        return "."
    return relative


def media_code_from_path(media: Path, root: Path) -> tuple[str, str] | None:
    stem = PART_SUFFIX_RE.sub("", media.stem)
    file_code = _scan_media_code(stem)
    parent = media.parent
    while parent != root:
        parent_code = _scan_media_code(parent.name)
        if parent_code is not None:
            if file_code is not None and file_code[1] != parent_code[1]:
                if not _starts_with_catalog_code(stem, parent_code[1]):
                    return None
            return parent_code
        parent = parent.parent
    return file_code


def _scan_media_code(value: object) -> tuple[str, str] | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not normalized:
        return None

    def parse(raw: str) -> tuple[str, str] | None:
        candidate = re.sub(r"[-._ ]+", "-", raw.strip().upper())
        normalized_code = normalize_catalog_code(candidate, max_length=40)
        if normalized_code is None:
            return None
        display, code_key = normalized_code
        if sum(
            character.isdigit() for character in display
        ) < 2 or SCAN_LAYOUT_NAME_RE.fullmatch(display):
            return None
        return display, code_key

    without_part = PART_SUFFIX_RE.sub("", normalized)
    exact = parse(without_part) if without_part[-1:].isdigit() else None
    if exact is not None:
        return exact
    matches = [parse(match.group(0)) for match in SCAN_CODE_RE.finditer(normalized)]
    valid = [item for item in matches if item is not None]
    return valid[0] if valid else None


def exact_catalog_query(value: object | None) -> str | None:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    parsed = _scan_media_code(clean)
    canonical = canonical_catalog_code(clean, max_length=80)
    if parsed is None or canonical != parsed[1]:
        return None
    return parsed[1]


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


def quality_height_from_name(value: str) -> int | None:
    matches = list(QUALITY_RE.finditer(unicodedata.normalize("NFKC", value).upper()))
    if not matches:
        return None
    heights: list[int] = []
    for match in matches:
        if match.group(1):
            heights.append(int(match.group(1)))
        elif match.group(2):
            heights.append({"2": 1440, "4": 2160, "8": 4320}[match.group(2)])
    return max(heights) if heights else None


def regular_asset_status(
    siblings: Mapping[str, tuple[Path, os.stat_result]], name: str
) -> str:
    item = siblings.get(name)
    if item is None:
        return "missing"
    return (
        "present"
        if stat.S_ISREG(item[1].st_mode)
        and not is_linklike(item[1])
        and item[1].st_size > 0
        else "invalid"
    )


def relative_from_root(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise MediaLibraryUnavailableError(
            "media library path escaped its root"
        ) from exc
    return safe_relative_path(relative, allow_root=False)


def join_relative(root: Path, relative: str) -> Path:
    clean = safe_relative_path(relative, allow_root=True)
    if clean == ".":
        return root
    target = root.joinpath(*PurePosixPath(clean).parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise MediaLibraryUnavailableError(
            "media library path escaped its root"
        ) from exc
    return target


def safe_relative_path(value: object, *, allow_root: bool) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if allow_root and raw in {"", "."}:
        return "."
    pure = PurePosixPath(raw)
    if (
        not raw
        or len(raw.encode("utf-8")) > SAFE_RELATIVE_MAX
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(ord(character) < 32 for character in raw)
    ):
        raise MediaLibraryError("media library relative path is invalid")
    return pure.as_posix()


def normalize_refresh_directories(paths: object) -> frozenset[str]:
    if isinstance(paths, (str, bytes, bytearray)):
        raise MediaLibraryError("media library refresh paths are invalid")
    try:
        values = tuple(paths)  # type: ignore[arg-type]
    except TypeError as exc:
        raise MediaLibraryError("media library refresh paths are invalid") from exc
    if len(values) > MAX_REFRESH_PATHS:
        raise MediaLibraryError("media library refresh path limit was exceeded")
    directories: set[str] = set()
    for value in values:
        relative = safe_relative_path(value, allow_root=False)
        directories.add(PurePosixPath(relative).parent.as_posix())
    return frozenset(directories)


def minimal_subtrees(paths: Iterable[str]) -> list[str]:
    result: list[str] = []
    for path in sorted(set(paths), key=lambda value: (value.count("/"), value)):
        if any(is_below(path, parent) for parent in result):
            continue
        result.append(path)
    return result


def is_below(path: str, parent: str) -> bool:
    return parent == "." or path == parent or path.startswith(parent + "/")
