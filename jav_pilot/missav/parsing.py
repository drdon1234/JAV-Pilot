"""Parsing of MissAV search, series and description pages."""

from __future__ import annotations

import unicodedata
from html import unescape
from html.parser import HTMLParser
from typing import Mapping

from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
    normalize_web_download_variant,
)
from .errors import MissavError
from .models import MissavSeriesDiscovery
from .site import (
    CODE_RE,
    DESCRIPTION_SENSITIVE_RE,
    MAX_SERIES_CANDIDATES,
    SERIES_PAGE_QUERY_RE,
    SERIES_PREFIX_RE,
    uses_configured_missav_site,
)
from .urls import (
    detail_href_parts,
    exact_detail_url,
    same_origin_href,
    split_detail_variant_slug,
)

__all__ = [
    "extract_description_from_html",
    "find_exact_detail_url",
]


class _MissavSearchParser(HTMLParser):
    def __init__(
        self,
        canonical_code: str,
        variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_code = canonical_code
        self.variant = normalize_web_download_variant(variant)
        self.matches: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next(
            (value for name, value in attrs if name.lower() == "href" and value), None
        )
        if not href:
            return
        candidate = exact_detail_url(
            href,
            self.canonical_code,
            variant=self.variant,
        )
        if candidate and candidate not in self.matches:
            self.matches.append(candidate)


class MissavSeriesParser(HTMLParser):
    def __init__(
        self,
        *,
        display_prefix: str,
        canonical_prefix: str,
        suffix_width: int | None,
        start: int | None,
        end: int | None,
        search_path: str,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.display_prefix = display_prefix
        self.canonical_prefix = canonical_prefix
        self.suffix_width = suffix_width
        self.start = start
        self.end = end
        self.search_path = search_path
        self.candidates: list[tuple[str, str, int, MissavVariant]] = []
        self._candidate_keys: set[tuple[str, MissavVariant]] = set()
        self._candidate_codes: set[str] = set()
        self.candidate_count = 0
        self.candidate_limit_hit = False
        self.max_page = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = next(
            (value for name, value in attrs if name.casefold() == "href" and value),
            None,
        )
        if not href:
            return
        page_number = _search_page_number(href, self.search_path)
        if page_number is not None:
            self.max_page = max(self.max_page, page_number)
        candidate = _series_candidate_from_href(
            href,
            display_prefix=self.display_prefix,
            canonical_prefix=self.canonical_prefix,
            suffix_width=self.suffix_width,
            start=self.start,
            end=self.end,
        )
        if candidate is None:
            return
        _, canonical_code, _, variant = candidate
        candidate_key = (canonical_code, variant)
        if candidate_key in self._candidate_keys:
            return
        if canonical_code not in self._candidate_codes:
            self.candidate_count += 1
            if self.candidate_count > MAX_SERIES_CANDIDATES:
                self.candidate_limit_hit = True
                return
            self._candidate_codes.add(canonical_code)
        self._candidate_keys.add(candidate_key)
        self.candidates.append(candidate)


class _MissavDescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.descriptions: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "meta":
            return
        values = {
            str(name or "").strip().casefold(): str(value or "")
            for name, value in attrs
        }
        key = (values.get("property") or values.get("name") or "").strip().casefold()
        if key not in {"og:description", "description", "twitter:description"}:
            return
        content = _normalized_description(values.get("content"))
        if content and key not in self.descriptions:
            self.descriptions[key] = content


@uses_configured_missav_site
def find_exact_detail_url(
    html: str,
    code: object,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str | None:
    """Return the first same-origin detail link for the exact code and variant."""

    _, canonical_code = validated_code(code)
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise MissavError("MissAV variant is invalid") from exc
    parser = _MissavSearchParser(canonical_code, clean_variant)
    try:
        parser.feed(str(html or ""))
        parser.close()
    except (TypeError, ValueError):
        return None
    return parser.matches[0] if parser.matches else None


def extract_description_from_html(html: object) -> str | None:
    """Extract a bounded description from a validated MissAV detail page."""

    parser = _MissavDescriptionParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except (TypeError, ValueError):
        return None
    for key in ("og:description", "description", "twitter:description"):
        value = parser.descriptions.get(key)
        if value:
            return value
    return None


def validated_code(code: object) -> tuple[str, str]:
    if not isinstance(code, str):
        raise MissavError("A valid catalog code is required")
    normalized = unicodedata.normalize("NFKC", code).strip().upper()
    if (
        not normalized
        or len(normalized) > 32
        or not normalized.isascii()
        or not CODE_RE.fullmatch(normalized)
    ):
        raise MissavError("A valid catalog code is required")
    canonical = canonical_catalog_code(normalized, max_length=32)
    if canonical is None:
        raise MissavError("A valid catalog code is required")
    return normalized, canonical


def _normalized_description(value: object) -> str | None:
    raw = unescape(str(value or ""))
    normalized = unicodedata.normalize("NFKC", raw).replace("\x00", " ")
    clean = " ".join(normalized.split())
    if len(clean) < 12 or DESCRIPTION_SENSITIVE_RE.search(clean):
        return None
    return clean[:8192]


def validated_series_prefix(prefix: object) -> tuple[str, str]:
    if not isinstance(prefix, str):
        raise MissavError("A valid MissAV series prefix is required")
    display = unicodedata.normalize("NFKC", prefix).strip().upper()
    if (
        not display
        or len(display) > 24
        or not display.isascii()
        or not SERIES_PREFIX_RE.fullmatch(display)
        or sum(character.isalpha() for character in display) < 2
    ):
        raise MissavError("A valid MissAV series prefix is required")
    canonical = "".join(character for character in display if character.isalnum())
    if not canonical or len(canonical) > 24:
        raise MissavError("A valid MissAV series prefix is required")
    return display, canonical


def required_bounded_integer(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MissavError(f"MissAV {name} is invalid")
    if not minimum <= value <= maximum:
        raise MissavError(f"MissAV {name} is invalid")
    return value


def optional_bounded_integer(
    value: object | None,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    return required_bounded_integer(
        value,
        name=name,
        minimum=minimum,
        maximum=maximum,
    )


def series_discovery_result(
    found: Mapping[str, tuple[int, str, set[MissavVariant]]],
    *,
    complete: bool,
) -> MissavSeriesDiscovery:
    ordered = sorted(found.items(), key=lambda item: (item[1][0], item[1][1], item[0]))
    variants_by_code = tuple(
        (
            value[1],
            tuple(variant for variant in WEB_DOWNLOAD_VARIANTS if variant in value[2]),
        )
        for _, value in ordered
    )
    return MissavSeriesDiscovery(
        codes=tuple(value[1] for _, value in ordered),
        complete=complete,
        variants_by_code=variants_by_code,
    )


def exact_series_code(
    display_prefix: str,
    *,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
) -> tuple[str, str] | None:
    if suffix_width is None or start is None or end is None or start != end:
        return None
    suffix = str(start).zfill(suffix_width)
    if len(suffix) != suffix_width:
        return None
    return normalize_catalog_code(f"{display_prefix}-{suffix}", max_length=32)


def _series_candidate_from_href(
    href: object,
    *,
    display_prefix: str,
    canonical_prefix: str,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
) -> tuple[str, str, int, MissavVariant] | None:
    detail_parts = detail_href_parts(href)
    if detail_parts is None:
        return None
    _, slug = detail_parts
    split_slug = split_detail_variant_slug(slug)
    if split_slug is None:
        return None
    base_slug, variant = split_slug
    normalized_slug = base_slug.upper()
    normalized_code = normalize_catalog_code(normalized_slug, max_length=32)
    if normalized_code is None:
        return None
    display_code, canonical_code = normalized_code
    if not canonical_code.startswith(canonical_prefix):
        return None
    suffix_text = canonical_code[len(canonical_prefix) :]
    if not suffix_text or not suffix_text.isascii() or not suffix_text.isdigit():
        return None
    raw_suffix = _series_raw_suffix(
        normalized_slug,
        display_prefix=display_prefix,
        canonical_prefix=canonical_prefix,
    )
    if raw_suffix != suffix_text:
        return None
    if suffix_width is not None and len(suffix_text) != suffix_width:
        return None
    suffix = int(suffix_text, 10)
    if start is not None and suffix < start:
        return None
    if end is not None and suffix > end:
        return None
    expected = normalize_catalog_code(
        f"{display_prefix}-{suffix_text}",
        max_length=32,
    )
    if expected is None or expected[1] != canonical_code:
        return None
    return display_code, canonical_code, suffix, variant


def _series_raw_suffix(
    normalized_slug: str,
    *,
    display_prefix: str,
    canonical_prefix: str,
) -> str | None:
    prefixes = sorted({display_prefix, canonical_prefix}, key=len, reverse=True)
    for prefix in prefixes:
        for separator in ("-", "_", ".", ""):
            candidate_prefix = f"{prefix}{separator}"
            if not normalized_slug.startswith(candidate_prefix):
                continue
            suffix = normalized_slug[len(candidate_prefix) :]
            if suffix and suffix.isascii() and suffix.isdigit():
                return suffix
    return None


def _search_page_number(href: object, search_path: str) -> int | None:
    parsed = same_origin_href(href)
    if parsed is None or parsed.path != search_path or parsed.fragment:
        return None
    match = SERIES_PAGE_QUERY_RE.fullmatch(parsed.query)
    return int(match.group(1), 10) if match is not None else None
