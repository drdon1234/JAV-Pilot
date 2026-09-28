from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import unicodedata

from ..core.catalog_code import normalize_catalog_code
from ..web_download.variant import (
    MissavVariant,
    normalize_web_download_variant,
    web_download_variant_label,
)


MAX_ARCHIVE_FILENAME_BYTES = 240

_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,16}$")
_YEAR_RE = re.compile(r"^(\d{4})(?=$|\D)")
_NUMERIC_SERIAL_RE = re.compile(
    r"^(?P<category>[A-Z0-9]+(?:[-._][A-Z0-9]+)*?)(?:[-._]\d{2,9})+$"
)
_PREFIXED_SERIAL_RE = re.compile(
    r"^(?P<category>[A-Z0-9]+(?:[-._][A-Z0-9]+)*)[-._][A-Z]{1,4}\d{2,9}$"
)
_CATEGORY_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_COMPONENT_SEPARATOR_RE = re.compile(r"[\s_]+")
_INVALID_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
_TITLE_PREFIX_SEPARATORS = frozenset("-._ ")
_TITLE_REMAINDER_SEPARATORS = " \t\r\n:：-_.|/\\—–"
_TITLE_WRAPPERS = {"[": "]", "(": ")", "【": "】"}


@dataclass(frozen=True, slots=True)
class ArchiveLayout:
    display_code: str
    code_key: str
    category: str
    clean_title: str | None
    year: str | None
    suffix: str
    variant: MissavVariant | None
    variant_label: str | None
    filename: str
    relative_media_path: PurePosixPath
    ready: bool
    missing_fields: tuple[str, ...]

    @property
    def relative_directory(self) -> PurePosixPath:
        return self.relative_media_path.parent

    @property
    def provisional(self) -> bool:
        return not self.ready


def plan_archive_layout(
    *,
    code: object,
    title: object,
    release_date: object,
    suffix: object,
    variant: object | None = None,
    max_filename_bytes: int = MAX_ARCHIVE_FILENAME_BYTES,
) -> ArchiveLayout:
    """Plan a portable ``category/code/code_title_year_variant.ext`` media path.

    A missing title or year produces a deterministic provisional path and is
    reported through ``ready``/``missing_fields``. This function only plans a
    path; callers remain responsible for collision detection and filesystem
    changes.
    """

    normalized_code = normalize_catalog_code(code, max_length=40)
    if normalized_code is None:
        raise ValueError("code must be a valid catalog code")
    display_code, code_key = normalized_code
    category = archive_category(display_code)
    normalized_suffix = _normalize_suffix(suffix)
    normalized_variant = _normalize_variant(variant)
    variant_label = (
        web_download_variant_label(normalized_variant)
        if normalized_variant is not None
        else None
    )
    _validate_filename_limit(max_filename_bytes)

    clean_title = _clean_title(title, code_key, variant_label)
    year = _release_year(release_date)
    clean_title = _fit_title_to_filename(
        display_code=display_code,
        clean_title=clean_title,
        year=year,
        suffix=normalized_suffix,
        variant_label=variant_label,
        max_filename_bytes=max_filename_bytes,
    )

    missing_fields = tuple(
        field
        for field, value in (("title", clean_title), ("year", year))
        if value is None
    )
    filename = _archive_filename(
        display_code=display_code,
        clean_title=clean_title,
        year=year,
        suffix=normalized_suffix,
        variant_label=variant_label,
    )
    if len(filename.encode("utf-8")) > max_filename_bytes:
        raise ValueError("catalog code and suffix exceed the filename byte limit")

    relative_media_path = PurePosixPath(category, display_code, filename)
    return ArchiveLayout(
        display_code=display_code,
        code_key=code_key,
        category=category,
        clean_title=clean_title,
        year=year,
        suffix=normalized_suffix,
        variant=normalized_variant,
        variant_label=variant_label,
        filename=filename,
        relative_media_path=relative_media_path,
        ready=not missing_fields,
        missing_fields=missing_fields,
    )


def archive_category(code: object) -> str:
    """Return the stable series directory for a normalized catalog code."""

    normalized = normalize_catalog_code(code, max_length=40)
    if normalized is None:
        raise ValueError("code must be a valid catalog code")
    display_code = normalized[0]
    match = _NUMERIC_SERIAL_RE.fullmatch(display_code)
    if match is None:
        match = _PREFIXED_SERIAL_RE.fullmatch(display_code)
    if match is None:
        raise ValueError("catalog code has no safe archive category")
    category = re.sub(r"[-._]+", "-", match.group("category")).strip("-")
    if not category or not _CATEGORY_RE.fullmatch(category):
        raise ValueError("catalog code has no safe archive category")
    return category


def _normalize_suffix(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    if not _SUFFIX_RE.fullmatch(normalized):
        raise ValueError("suffix must be a simple file extension")
    return normalized


def _normalize_variant(value: object | None) -> MissavVariant | None:
    if value is None:
        return None
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise ValueError("variant must be a supported Web download variant") from exc


def _validate_filename_limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise ValueError("max_filename_bytes must be between 1 and 255")


def _clean_title(
    value: object,
    code_key: str,
    variant_label: str | None,
) -> str | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not normalized:
        return None
    without_prefix = _without_catalog_code_prefix(normalized, code_key)
    without_variant = _without_variant_label_prefix(without_prefix, variant_label)
    clean = _safe_filename_component(without_variant)
    return clean or None


def _without_catalog_code_prefix(title: str, code_key: str) -> str:
    opening = title[:1]
    closing_character = _TITLE_WRAPPERS.get(opening)
    if closing_character:
        closing = title.find(closing_character, 1, 82)
        if closing > 0:
            normalized = normalize_catalog_code(title[1:closing], max_length=40)
            if normalized is not None and normalized[1] == code_key:
                return title[closing + 1 :].lstrip(_TITLE_REMAINDER_SEPARATORS)
        return title

    candidate: list[str] = []
    for index, character in enumerate(title):
        folded = unicodedata.normalize("NFKC", character).upper()
        if folded and all(item.isascii() and item.isalnum() for item in folded):
            candidate.extend(folded)
            joined = "".join(candidate)
            if not code_key.startswith(joined):
                return title
            if len(joined) == len(code_key):
                remainder = title[index + 1 :]
                if remainder:
                    boundary = unicodedata.normalize("NFKC", remainder[0])
                    if boundary and boundary[0].isalnum():
                        return title
                return remainder.lstrip(_TITLE_REMAINDER_SEPARATORS)
        elif character not in _TITLE_PREFIX_SEPARATORS:
            return title
    return title


def _without_variant_label_prefix(title: str, variant_label: str | None) -> str:
    if not variant_label:
        return title
    normalized = unicodedata.normalize("NFKC", title).lstrip()
    for opening, closing in (("[", "]"), ("【", "】"), ("(", ")")):
        prefix = f"{opening}{variant_label}{closing}"
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].lstrip(_TITLE_REMAINDER_SEPARATORS)
    return normalized


def _safe_filename_component(value: str) -> str:
    characters: list[str] = []
    for character in unicodedata.normalize("NFKC", value):
        if unicodedata.category(character).startswith("C"):
            characters.append(" ")
        elif character in _INVALID_FILENAME_CHARACTERS:
            characters.append(" ")
        else:
            characters.append(character)
    collapsed = _COMPONENT_SEPARATOR_RE.sub("_", "".join(characters))
    return collapsed.strip(" ._")


def _release_year(value: object) -> str | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    match = _YEAR_RE.match(normalized)
    return match.group(1) if match else None


def _fit_title_to_filename(
    *,
    display_code: str,
    clean_title: str | None,
    year: str | None,
    suffix: str,
    variant_label: str | None,
    max_filename_bytes: int,
) -> str | None:
    if clean_title is None:
        return None
    tail_parts = [part for part in (year, variant_label) if part]
    tail = f"_{'_'.join(tail_parts)}{suffix}" if tail_parts else suffix
    title_byte_limit = (
        max_filename_bytes
        - len(f"{display_code}_".encode("utf-8"))
        - len(tail.encode("utf-8"))
    )
    if title_byte_limit <= 0:
        raise ValueError("catalog code, year, and suffix leave no room for a title")
    fitted = _truncate_utf8(clean_title, title_byte_limit).rstrip(" ._")
    return fitted or None


def _truncate_utf8(value: str, byte_limit: int) -> str:
    if len(value.encode("utf-8")) <= byte_limit:
        return value
    size = 0
    characters: list[str] = []
    for character in value:
        encoded_size = len(character.encode("utf-8"))
        if size + encoded_size > byte_limit:
            break
        size += encoded_size
        characters.append(character)
    return "".join(characters)


def _archive_filename(
    *,
    display_code: str,
    clean_title: str | None,
    year: str | None,
    suffix: str,
    variant_label: str | None,
) -> str:
    parts = [display_code]
    if clean_title:
        parts.append(clean_title)
    if year:
        parts.append(year)
    if variant_label:
        parts.append(variant_label)
    return f"{'_'.join(parts)}{suffix}"
