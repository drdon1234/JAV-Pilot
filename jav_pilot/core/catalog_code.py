from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_ALLOWED_CODE_RE = re.compile(r"^[A-Z0-9._\-\s]+$")
_DISPLAY_CODE_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")
_FC2_ALIAS_KEY_RE = re.compile(r"^FC2(?:PPV)?(\d{2,9})$")
_FC2_PPV_KEY_RE = re.compile(r"^FC2PPV(\d{2,9})$")
_SIMPLE_DISPLAY_CODE_RE = re.compile(r"^([A-Z]{2,12})[-._]?(\d{2,8})$")
_QUALITY_TOKEN_RE = re.compile(
    r"^(?:(?:[248]K|720P|1080P|2160P|4320P|SD|HD|FHD|UHD|VR|HDR|HEVC|H26[45]|X26[45]|AV1|\d{2,3}FPS))+$"
)


def canonical_catalog_code(raw_code: object, *, max_length: int = 64) -> str | None:
    if not raw_code:
        return None
    normalized = unicodedata.normalize("NFKC", str(raw_code)).strip().upper()
    if (
        not normalized
        or len(normalized) > max_length
        or not _ALLOWED_CODE_RE.fullmatch(normalized)
    ):
        return None
    if not any(character.isalpha() for character in normalized):
        return None
    if not any(character.isdigit() for character in normalized):
        return None

    canonical = "".join(character for character in normalized if character.isalnum())
    fc2_alias = _FC2_ALIAS_KEY_RE.fullmatch(canonical)
    if fc2_alias:
        canonical = f"FC2PPV{fc2_alias.group(1)}"
    if len(canonical) < 3 or _QUALITY_TOKEN_RE.fullmatch(canonical):
        return None
    has_separator = any(character in "-_. " for character in normalized)
    if not has_separator and sum(character.isalpha() for character in canonical) < 2:
        return None
    return canonical


def normalize_catalog_code(
    raw_code: object,
    *,
    max_length: int = 64,
) -> tuple[str, str] | None:
    """Return a safe display code and its separator-insensitive identity."""

    canonical = canonical_catalog_code(raw_code, max_length=max_length)
    if canonical is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(raw_code)).strip().upper()
    if (
        not normalized.isascii()
        or len(normalized) > max_length
        or not _DISPLAY_CODE_RE.fullmatch(normalized)
    ):
        return None

    fc2_match = _FC2_PPV_KEY_RE.fullmatch(canonical)
    if fc2_match:
        display = f"FC2-PPV-{fc2_match.group(1)}"
    else:
        simple_match = _SIMPLE_DISPLAY_CODE_RE.fullmatch(normalized)
        display = (
            f"{simple_match.group(1)}-{simple_match.group(2)}"
            if simple_match
            else normalized
        )
    if len(display) > max_length:
        return None
    return display, canonical


def looks_like_catalog_code(value: object) -> bool:
    return canonical_catalog_code(value, max_length=20) is not None


_PATTERN_TEXT_RE = re.compile(r"^[A-Z0-9][A-Z0-9._\-\s]*$")
_PATTERN_FC2_RE = re.compile(r"^FC2(?:[-._\s]*PPV)?(?:[-._\s]*(\d{2,9}))?$")
_PATTERN_SPLIT_RE = re.compile(r"^(.*[A-Z].*?)[-._\s]+(\d{1,9})$")
_PATTERN_JOINED_RE = re.compile(r"^([A-Z0-9]*?[A-Z])(\d{1,9})$")
_PATTERN_PREFIX_RE = re.compile(r"^[A-Z0-9]*[A-Z][A-Z0-9]*$")


@dataclass(frozen=True, slots=True)
class CodePattern:
    """The letter prefix of a catalog code and, when given, its serial number.

    Exact matching compares the prefix literally, so ``ABP`` never matches
    ``ABPA`` or ``SABP`` even though source sites return them for the query.
    """

    prefix: str
    number: int | None = None


def parse_code_pattern(value: object) -> CodePattern | None:
    """Parse ``ABP``, ``ABP-123``, ``abp123``, ``300MIUM-12`` or ``FC2-PPV-1``."""

    clean = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not clean or len(clean) > 40 or not _PATTERN_TEXT_RE.fullmatch(clean):
        return None
    fc2 = _PATTERN_FC2_RE.fullmatch(clean)
    if fc2:
        return CodePattern("FC2PPV", int(fc2.group(1)) if fc2.group(1) else None)
    split = _PATTERN_SPLIT_RE.fullmatch(clean)
    if split:
        prefix = "".join(character for character in split.group(1) if character.isalnum())
        return CodePattern(prefix, int(split.group(2)))
    joined = _PATTERN_JOINED_RE.fullmatch(clean)
    if joined:
        return CodePattern(joined.group(1), int(joined.group(2)))
    if _PATTERN_PREFIX_RE.fullmatch(clean):
        return CodePattern(clean)
    return None


def query_code_pattern(query: object) -> CodePattern | None:
    """Return the single code-like term of a search query, if there is one."""

    whole = parse_code_pattern(query)
    if whole is not None:
        return whole
    terms = [
        pattern
        for term in unicodedata.normalize("NFKC", str(query or "")).split()
        if any(character.isalpha() for character in term)
        and (pattern := parse_code_pattern(term)) is not None
    ]
    return terms[0] if len(terms) == 1 else None


def code_matches_pattern(code: object, pattern: CodePattern) -> bool:
    candidate = parse_code_pattern(code)
    if candidate is None or candidate.prefix != pattern.prefix:
        return False
    return pattern.number is None or candidate.number == pattern.number
