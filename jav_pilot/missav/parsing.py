"""Parsing of MissAV search, series, resource and description pages."""

from __future__ import annotations

import unicodedata
from html import unescape
from html.parser import HTMLParser
from typing import Mapping, Sequence
from urllib.parse import unquote, urljoin

from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..core.guards import contains_sensitive_transport_text
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
    normalize_web_download_variant,
)
from .errors import MissavError
from .models import MissavResourceItem, MissavSeriesDiscovery
from .site import (
    CODE_RE,
    DESCRIPTION_SENSITIVE_RE,
    HTML_VOID_ELEMENTS,
    MAX_RESOURCE_RESULTS,
    MAX_RESOURCE_TITLE_LENGTH,
    MAX_SERIES_CANDIDATES,
    RESOURCE_CARD_CLASSES,
    RESOURCE_RESULT_GRID_CLASSES,
    RESOURCE_TITLE_LINK_CLASSES,
    SERIES_PAGE_QUERY_RE,
    SERIES_PREFIX_RE,
    current_missav_site,
    uses_configured_missav_site,
)
from .urls import (
    detail_path_parts,
    exact_detail_url,
    split_detail_variant_slug,
    validated_https_url,
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
        page_number = _series_page_number(href, self.search_path)
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


class MissavResourceParser(HTMLParser):
    def __init__(
        self,
        *,
        display_query: str,
        canonical_query: str | None,
        suffix_width: int | None,
        start: int | None,
        end: int | None,
        search_path: str,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.display_query = display_query
        self.canonical_query = canonical_query
        self.suffix_width = suffix_width
        self.start = start
        self.end = end
        self.search_path = search_path
        self.max_page = 1
        self.candidate_limit_hit = False
        self._items: dict[
            str,
            tuple[str, set[MissavVariant], str | None, int],
        ] = {}
        self._elements: list[tuple[str, frozenset[str]]] = []
        self._anchors: list[
            tuple[
                tuple[str, str, MissavVariant] | None,
                bool,
                list[str],
                list[str],
            ]
        ] = []

    @property
    def items(self) -> tuple[MissavResourceItem, ...]:
        return tuple(
            MissavResourceItem(
                code=display_code,
                available_variants=tuple(
                    variant for variant in WEB_DOWNLOAD_VARIANTS if variant in variants
                ),
                title=title,
            )
            for display_code, variants, title, _priority in self._items.values()
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        clean_tag = tag.casefold()
        values = {str(name or "").casefold(): str(value or "") for name, value in attrs}
        classes = frozenset(values.get("class", "").split())
        if clean_tag == "a":
            href = values.get("href", "")
            page_number = _resource_page_number(href, self.search_path)
            if page_number is not None:
                self.max_page = max(self.max_page, page_number)
            candidate = None
            if href and self._inside_native_result_card():
                candidate = _resource_candidate_from_href(
                    href,
                    display_query=self.display_query,
                    canonical_query=self.canonical_query,
                    suffix_width=self.suffix_width,
                    start=self.start,
                    end=self.end,
                )
            title_link = RESOURCE_TITLE_LINK_CLASSES.issubset(classes)
            self._anchors.append((candidate, title_link, [], []))
            if candidate is not None:
                self._remember(candidate, None, priority=0)
        elif clean_tag == "img" and self._anchors:
            active = self._anchors[-1]
            if active[0] is not None:
                active[3].append(values.get("alt", ""))
        if clean_tag not in HTML_VOID_ELEMENTS:
            self._elements.append((clean_tag, classes))

    def handle_data(self, data: str) -> None:
        if self._anchors and self._anchors[-1][0] is not None:
            self._anchors[-1][2].append(data)

    def handle_endtag(self, tag: str) -> None:
        clean_tag = tag.casefold()
        if clean_tag == "a" and self._anchors:
            candidate, title_link, text_parts, image_alts = self._anchors.pop()
            if candidate is not None:
                title = None
                priority = 0
                if title_link:
                    title = _normalized_resource_title("".join(text_parts))
                    priority = 2
                if title is None:
                    for image_alt in image_alts:
                        title = _normalized_resource_title(image_alt)
                        if title is not None:
                            priority = 1
                            break
                self._remember(candidate, title, priority=priority)
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == clean_tag:
                del self._elements[index:]
                break

    def _inside_native_result_card(self) -> bool:
        grid_index: int | None = None
        for index, (tag, classes) in enumerate(self._elements):
            if tag == "div" and RESOURCE_RESULT_GRID_CLASSES.issubset(classes):
                grid_index = index
        if grid_index is None:
            return False
        return any(
            index > grid_index
            and tag == "div"
            and RESOURCE_CARD_CLASSES.issubset(classes)
            for index, (tag, classes) in enumerate(self._elements)
        )

    def _remember(
        self,
        candidate: tuple[str, str, MissavVariant],
        title: str | None,
        *,
        priority: int,
    ) -> None:
        display_code, canonical_code, variant = candidate
        variant_priority = len(WEB_DOWNLOAD_VARIANTS) - WEB_DOWNLOAD_VARIANTS.index(
            variant
        )
        effective_priority = priority * 10 + variant_priority
        existing = self._items.get(canonical_code)
        if existing is not None:
            existing[1].add(variant)
            current_title = existing[2]
            current_priority = existing[3]
            if title is not None and (
                effective_priority > current_priority
                or (
                    effective_priority == current_priority
                    and len(title) > len(current_title or "")
                )
            ):
                self._items[canonical_code] = (
                    existing[0],
                    existing[1],
                    title,
                    effective_priority,
                )
            return
        if len(self._items) >= MAX_RESOURCE_RESULTS:
            self.candidate_limit_hit = True
            return
        self._items[canonical_code] = (
            display_code,
            {variant},
            title,
            effective_priority,
        )


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


def _normalized_resource_title(value: object) -> str | None:
    raw = unescape(str(value or ""))
    normalized = unicodedata.normalize("NFKC", raw).replace("\x00", " ")
    clean = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    clean = " ".join(clean.split())
    if not clean or contains_sensitive_transport_text(clean):
        return None
    return clean[:MAX_RESOURCE_TITLE_LENGTH]


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
    missav_site = current_missav_site()
    raw_href = str(href or "").strip()
    if not raw_href or any(character in raw_href for character in "\r\n\t\\"):
        return None
    if not (raw_href.startswith("/") or raw_href.lower().startswith("https://")):
        return None
    try:
        absolute = urljoin(f"{missav_site.origin}/", raw_href)
    except ValueError:
        return None
    parsed = validated_https_url(
        absolute,
        allowed_hosts=frozenset({missav_site.host}),
    )
    if parsed is None or parsed.query or parsed.fragment or "%" in parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if "//" in decoded_path:
        return None
    path_segments = decoded_path.split("/")
    if (
        not path_segments
        or path_segments[0]
        or any(not segment for segment in path_segments[1:])
    ):
        return None
    detail_parts = detail_path_parts(path_segments[1:])
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


def validated_resource_pending(
    values: Sequence[MissavResourceItem],
) -> tuple[MissavResourceItem, ...]:
    if (
        isinstance(values, (str, bytes, bytearray))
        or len(values) > MAX_RESOURCE_RESULTS
    ):
        raise MissavError("MissAV resource pending items are invalid")
    clean: list[MissavResourceItem] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, MissavResourceItem):
            raise MissavError("MissAV resource pending items are invalid")
        normalized = normalize_catalog_code(value.code, max_length=32)
        if normalized is None or normalized[0] != value.code:
            raise MissavError("MissAV resource pending items are invalid")
        try:
            variants = tuple(
                normalize_web_download_variant(variant)
                for variant in value.available_variants
            )
        except ValueError as exc:
            raise MissavError("MissAV resource pending items are invalid") from exc
        ordered = tuple(
            variant for variant in WEB_DOWNLOAD_VARIANTS if variant in variants
        )
        if not ordered or variants != ordered or normalized[1] in seen:
            raise MissavError("MissAV resource pending items are invalid")
        seen.add(normalized[1])
        title = _normalized_resource_title(value.title)
        if value.title is not None and title != value.title:
            raise MissavError("MissAV resource pending items are invalid")
        clean.append(MissavResourceItem(normalized[0], ordered, title))
    return tuple(clean)


def _resource_candidate_from_href(
    href: object,
    *,
    display_query: str,
    canonical_query: str | None,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
) -> tuple[str, str, MissavVariant] | None:
    missav_site = current_missav_site()
    raw_href = str(href or "").strip()
    if not raw_href or any(character in raw_href for character in "\r\n\t\\"):
        return None
    if not (raw_href.startswith("/") or raw_href.lower().startswith("https://")):
        return None
    try:
        absolute = urljoin(f"{missav_site.origin}/", raw_href)
    except ValueError:
        return None
    parsed = validated_https_url(
        absolute,
        allowed_hosts=frozenset({missav_site.host}),
    )
    if parsed is None or parsed.query or parsed.fragment or "%" in parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if "//" in decoded_path:
        return None
    path_segments = decoded_path.split("/")
    if (
        not path_segments
        or path_segments[0]
        or any(not segment for segment in path_segments[1:])
    ):
        return None
    detail_parts = detail_path_parts(path_segments[1:])
    if detail_parts is None:
        return None
    _, slug = detail_parts
    split_slug = split_detail_variant_slug(slug)
    if split_slug is None:
        return None
    base_slug, variant = split_slug
    normalized = normalize_catalog_code(base_slug.upper(), max_length=32)
    if normalized is None:
        return None
    display_code, canonical_code = normalized
    if suffix_width is None and start is None and end is None:
        if canonical_query is not None and canonical_query != canonical_code:
            return None
        return display_code, canonical_code, variant
    if canonical_query is None:
        return None
    if not canonical_code.startswith(canonical_query):
        return None
    suffix_text = canonical_code[len(canonical_query) :]
    raw_suffix = _series_raw_suffix(
        base_slug.upper(),
        display_prefix=display_query,
        canonical_prefix=canonical_query,
    )
    if (
        not suffix_text
        or not suffix_text.isascii()
        or not suffix_text.isdigit()
        or raw_suffix != suffix_text
        or (suffix_width is not None and len(suffix_text) != suffix_width)
    ):
        return None
    suffix = int(suffix_text, 10)
    if start is not None and suffix < start:
        return None
    if end is not None and suffix > end:
        return None
    return display_code, canonical_code, variant


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


def _series_page_number(href: object, search_path: str) -> int | None:
    missav_site = current_missav_site()
    raw_href = str(href or "").strip()
    if not raw_href or any(character in raw_href for character in "\r\n\t\\"):
        return None
    if not (raw_href.startswith("/") or raw_href.lower().startswith("https://")):
        return None
    try:
        absolute = urljoin(f"{missav_site.origin}/", raw_href)
    except ValueError:
        return None
    parsed = validated_https_url(
        absolute,
        allowed_hosts=frozenset({missav_site.host}),
    )
    if parsed is None or parsed.path != search_path or parsed.fragment:
        return None
    match = SERIES_PAGE_QUERY_RE.fullmatch(parsed.query)
    return int(match.group(1), 10) if match is not None else None


def _resource_page_number(href: object, search_path: str) -> int | None:
    return _series_page_number(href, search_path)
