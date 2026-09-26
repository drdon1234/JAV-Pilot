"""Building and validating MissAV search and detail URLs."""

from __future__ import annotations

import unicodedata
from typing import Sequence
from urllib.parse import quote, unquote, urljoin, urlsplit

from ..core.catalog_code import canonical_catalog_code
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
    normalize_web_download_variant,
    web_download_variant_suffix,
)
from .errors import MissavError
from .site import (
    CODE_RE,
    DETAIL_LOCALE_SEGMENTS,
    DETAIL_ROUTE_PREFIX_RE,
    current_missav_site,
)

def series_search_path(display_prefix: str) -> str:
    return f"/cn/search/{quote(display_prefix, safe='-._')}"


def series_search_url(display_prefix: str, *, page: int) -> str:
    url = f"{current_missav_site().origin}{series_search_path(display_prefix)}"
    return url if page == 1 else f"{url}?page={page}"


def require_series_search_page(
    url: object,
    *,
    display_prefix: str,
    page: int,
) -> None:
    missav_site = current_missav_site()
    parsed = validated_https_url(
        str(url or ""),
        allowed_hosts=frozenset({missav_site.host}),
    )
    expected_queries = {"", "page=1"} if page == 1 else {f"page={page}"}
    if (
        parsed is None
        or parsed.path != series_search_path(display_prefix)
        or parsed.query not in expected_queries
        or parsed.fragment
    ):
        raise MissavError(
            "MissAV series search redirected unexpectedly",
            code="route_drift",
        )


def search_url(search_code: str) -> str:
    origin = current_missav_site().origin
    return f"{origin}/cn/search/{quote(search_code, safe='-._')}"


def require_exact_search_page(url: object, search_code: str) -> None:
    missav_site = current_missav_site()
    parsed = validated_https_url(
        str(url or ""),
        allowed_hosts=frozenset({missav_site.host}),
    )
    expected = urlsplit(search_url(search_code))
    if (
        parsed is None
        or parsed.path != expected.path
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
    ):
        raise MissavError(
            "MissAV search redirected unexpectedly",
            code="route_drift",
        )


def build_detail_url(
    search_code: str,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str:
    suffix = web_download_variant_suffix(variant)
    slug = quote(f"{search_code.lower()}{suffix}", safe="-._")
    return f"{current_missav_site().origin}/{slug}"


def localized_detail_url(
    search_code: str,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str:
    suffix = web_download_variant_suffix(variant)
    slug = quote(f"{search_code.lower()}{suffix}", safe="-._")
    return f"{current_missav_site().origin}/cn/{slug}"


def exact_detail_url(
    href: str,
    canonical_code: str,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str | None:
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
    if parsed is None or parsed.query or parsed.fragment:
        return None
    if "%" in parsed.path:
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
    detail_segments, slug = detail_parts
    base_slug = _detail_variant_base_slug(slug, variant)
    if base_slug is None:
        return None
    if canonical_catalog_code(base_slug, max_length=32) != canonical_code:
        return None
    clean_path = "/".join(quote(segment, safe="-._") for segment in detail_segments)
    return f"{missav_site.origin}/{clean_path}"


def routed_challenge_detail_url(
    href: str,
    canonical_code: str,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str | None:
    exact = exact_detail_url(href, canonical_code, variant=variant)
    if exact is None:
        return None
    parsed = urlsplit(exact)
    segments = unquote(parsed.path).split("/")
    detail_parts = detail_path_parts(segments[1:])
    if detail_parts is None or not DETAIL_ROUTE_PREFIX_RE.fullmatch(
        detail_parts[0][0]
    ):
        return None
    return exact


def detail_path_parts(
    segments: Sequence[str],
) -> tuple[tuple[str, ...], str] | None:
    """Validate a same-origin MissAV detail route and return its slug.

    MissAV currently serves routed detail pages as ``/dmNNN/<code>``. Older
    pages used ``/cn/<code>`` and ``/dmNNN/cn/<code>``; language-prefixed
    routes remain valid aliases. Keeping the accepted shapes explicit avoids
    treating arbitrary same-origin paths as work pages.
    """

    clean = tuple(str(segment) for segment in segments)
    if any(not segment for segment in clean):
        return None
    if len(clean) == 1:
        return clean, clean[0]
    if len(clean) == 2:
        prefix, slug = clean
        if prefix in DETAIL_LOCALE_SEGMENTS or DETAIL_ROUTE_PREFIX_RE.fullmatch(
            prefix
        ):
            return clean, slug
        return None
    if len(clean) == 3:
        route, locale, slug = clean
        if (
            DETAIL_ROUTE_PREFIX_RE.fullmatch(route)
            and locale in DETAIL_LOCALE_SEGMENTS
        ):
            return clean, slug
    return None


def validated_https_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allowed_host_suffixes: frozenset[str] = frozenset(),
):
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").rstrip(".").lower()
    host_allowed = hostname in allowed_hosts or any(
        hostname.endswith(f".{suffix.rstrip('.').lower()}")
        and hostname.count(".") == suffix.rstrip(".").count(".") + 1
        for suffix in allowed_host_suffixes
    )
    if (
        parsed.scheme.lower() != "https"
        or not host_allowed
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return parsed


def require_missav_page(url: str) -> None:
    missav_host = current_missav_site().host
    if validated_https_url(url, allowed_hosts=frozenset({missav_host})) is None:
        raise MissavError(
            "MissAV redirected outside its allowed origin",
            code="safety_rejected",
        )


def require_exact_detail_page(
    url: str,
    canonical_code: str,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> None:
    if exact_detail_url(url, canonical_code, variant=variant) is None:
        raise MissavError(
            "MissAV redirected away from the requested work",
            code="route_drift",
        )


def split_detail_variant_slug(
    slug: object,
) -> tuple[str, MissavVariant] | None:
    normalized = unicodedata.normalize("NFKC", str(slug or "")).strip()
    if (
        not normalized
        or len(normalized) > 64
        or not normalized.isascii()
        or not CODE_RE.fullmatch(normalized.upper())
    ):
        return None
    folded = normalized.casefold()
    for variant in WEB_DOWNLOAD_VARIANTS[1:]:
        suffix = web_download_variant_suffix(variant)
        if not folded.endswith(suffix):
            continue
        base_slug = normalized[: -len(suffix)]
        if base_slug and CODE_RE.fullmatch(base_slug.upper()):
            return base_slug, variant
        return None
    if len(normalized) > 32:
        return None
    return normalized, DEFAULT_WEB_DOWNLOAD_VARIANT


def _detail_variant_base_slug(
    slug: object,
    variant: object,
) -> str | None:
    clean_variant = normalize_web_download_variant(variant)
    normalized = unicodedata.normalize("NFKC", str(slug or "")).strip()
    if (
        not normalized
        or len(normalized) > 64
        or not normalized.isascii()
        or not CODE_RE.fullmatch(normalized.upper())
    ):
        return None
    suffix = web_download_variant_suffix(clean_variant)
    if suffix:
        if not normalized.casefold().endswith(suffix):
            return None
        normalized = normalized[: -len(suffix)]
    if (
        not normalized
        or len(normalized) > 32
        or not CODE_RE.fullmatch(normalized.upper())
    ):
        return None
    return normalized
