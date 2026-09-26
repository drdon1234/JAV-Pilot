from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SearchSort = Literal[
    "relevance",
    "release_date_desc",
    "release_date_asc",
    "code_asc",
    "code_desc",
]
SearchMatch = Literal["auto", "exact", "fuzzy"]
SearchKind = Literal[
    "keyword",
    "code",
    "actor",
    "tag",
    "series",
    "maker",
    "publisher",
    "director",
]
MagnetHint = Literal["available", "unavailable", "unknown"]
SourceImageKind = Literal["cover", "backdrop", "sample"]
SEARCH_SORTS: frozenset[str] = frozenset(
    {"relevance", "release_date_desc", "release_date_asc", "code_asc", "code_desc"}
)
SEARCH_MATCHES: frozenset[str] = frozenset({"auto", "exact", "fuzzy"})
SEARCH_KINDS: frozenset[str] = frozenset(
    {"keyword", "code", "actor", "tag", "series", "maker", "publisher", "director"}
)


@dataclass(frozen=True)
class SearchBounds:
    limit: int = 20
    page: int = 1
    max_pages: int = 1
    result_limit: int | None = None
    fetch_magnets: bool = True
    detail_limit: int = 5
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2 * 1024 * 1024
    filters: dict[str, str] = field(default_factory=dict)
    sort: SearchSort = "relevance"
    match: SearchMatch = "auto"
    search_kind: SearchKind = "keyword"
    semantic_refs: dict[str, str] = field(default_factory=dict)

    def normalized(self) -> "SearchBounds":
        return SearchBounds(
            limit=max(1, min(int(self.limit), 50)),
            page=max(1, min(int(self.page), 100)),
            max_pages=max(1, min(int(self.max_pages), 3)),
            result_limit=_normalized_result_limit(self.result_limit),
            fetch_magnets=bool(self.fetch_magnets),
            detail_limit=max(0, min(int(self.detail_limit), 50)),
            timeout_seconds=max(1.0, min(float(self.timeout_seconds), 30.0)),
            max_response_bytes=max(
                16 * 1024, min(int(self.max_response_bytes), 4 * 1024 * 1024)
            ),
            filters=_normalized_filters(self.filters),
            sort=_normalized_sort(self.sort),
            match=_normalized_match(self.match),
            search_kind=_normalized_search_kind(self.search_kind),
            semantic_refs=_normalized_semantic_refs(self.semantic_refs),
        )


def _normalized_result_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999:
        raise ValueError("result_limit must be an integer between 1 and 999")
    return value


def _normalized_filters(filters: dict[str, str] | None) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in (filters or {}).items():
        safe_key = str(key).strip()[:64]
        safe_value = str(value).strip()[:256]
        if safe_key:
            clean[safe_key] = safe_value
        if len(clean) >= 24:
            break
    return clean


def _normalized_sort(value: object) -> SearchSort:
    clean = str(value or "").strip().lower()
    if clean not in SEARCH_SORTS:
        clean = "relevance"
    return cast(SearchSort, clean)


def _normalized_match(value: object) -> SearchMatch:
    clean = str(value or "").strip().lower()
    if clean not in SEARCH_MATCHES:
        clean = "auto"
    return cast(SearchMatch, clean)


def _normalized_search_kind(value: object) -> SearchKind:
    clean = str(value or "").strip().lower()
    if clean not in SEARCH_KINDS:
        clean = "keyword"
    return cast(SearchKind, clean)


def _normalized_semantic_refs(values: dict[str, str] | None) -> dict[str, str]:
    clean: dict[str, str] = {}
    for source_id, value in (values or {}).items():
        safe_source_id = str(source_id or "").strip()[:64]
        safe_value = str(value or "").strip()[:2048]
        if safe_source_id and safe_value:
            clean[safe_source_id] = safe_value
        if len(clean) >= 16:
            break
    return clean


@dataclass(frozen=True)
class MagnetInfo:
    uri: str
    info_hash: str
    display_name: str | None = None
    trackers: tuple[str, ...] = ()
    exact_length: int | None = None
    params: dict[str, list[str]] = field(default_factory=dict)
    reported_size_text: str | None = None
    reported_size_bytes: int | None = None
    badges: tuple[str, ...] = ()
    reported_seeders: int | None = None
    reported_leechers: int | None = None
    reported_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "info_hash": self.info_hash,
            "display_name": self.display_name,
            "trackers": list(self.trackers),
            "exact_length": self.exact_length,
            "params": self.params,
            "reported_size_text": self.reported_size_text,
            "reported_size_bytes": self.reported_size_bytes,
            "badges": list(self.badges),
            "reported_seeders": self.reported_seeders,
            "reported_leechers": self.reported_leechers,
            "reported_at": self.reported_at,
        }


@dataclass(frozen=True)
class RelatedRef:
    kind: SearchKind
    label: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "url": self.url,
        }


@dataclass(frozen=True)
class Rating:
    value: float | None = None
    votes: int | None = None
    text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "votes": self.votes,
            "text": self.text,
        }


@dataclass(frozen=True)
class SourceDetails:
    title: str | None = None
    original_title: str | None = None
    release_date: str | None = None
    duration_minutes: int | None = None
    duration_text: str | None = None
    rating: Rating | None = None
    makers: tuple[RelatedRef, ...] = ()
    publishers: tuple[RelatedRef, ...] = ()
    series: tuple[RelatedRef, ...] = ()
    directors: tuple[RelatedRef, ...] = ()
    actors: tuple[RelatedRef, ...] = ()
    tags: tuple[RelatedRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "original_title": self.original_title,
            "release_date": self.release_date,
            "duration_minutes": self.duration_minutes,
            "duration_text": self.duration_text,
            "rating": self.rating.to_dict() if self.rating else None,
            "makers": [item.to_dict() for item in self.makers],
            "publishers": [item.to_dict() for item in self.publishers],
            "series": [item.to_dict() for item in self.series],
            "directors": [item.to_dict() for item in self.directors],
            "actors": [item.to_dict() for item in self.actors],
            "tags": [item.to_dict() for item in self.tags],
        }


@dataclass(frozen=True)
class SearchResult:
    source: str
    title: str
    url: str | None = None
    code: str | None = None
    date: str | None = None
    actors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    details: SourceDetails = field(default_factory=SourceDetails)
    magnet_hint: MagnetHint = "unknown"
    magnets: tuple[MagnetInfo, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "code": self.code,
            "date": self.date,
            "actors": list(self.actors),
            "tags": list(self.tags),
            "details": self.details.to_dict(),
            "magnet_hint": self.magnet_hint,
            "magnets": [magnet.to_dict() for magnet in self.magnets],
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SourceImage:
    kind: SourceImageKind
    url: str
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "thumbnail_url": self.thumbnail_url,
            "width": self.width,
            "height": self.height,
        }


_IMAGE_TRANSFORM_QUERY_KEYS = frozenset(
    {
        "dpr",
        "fm",
        "format",
        "h",
        "height",
        "q",
        "quality",
        "w",
        "width",
    }
)
_IMAGE_RESIZE_QUERY_KEYS = frozenset({"dpr", "h", "height", "w", "width"})
_IMAGE_KIND_PRIORITY: dict[str, int] = {"sample": 0, "backdrop": 1, "cover": 2}
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")


def canonical_image_url_key(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return raw

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname:
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = (scheme == "http" and port == 80) or (
            scheme == "https" and port == 443
        )
        netloc = host if port is None or default_port else f"{host}:{port}"
    else:
        netloc = parsed.netloc.lower()

    query_items = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.strip().casefold()
        if normalized_key in _IMAGE_TRANSFORM_QUERY_KEYS:
            continue
        query_items.append((key, item_value))
    query_items.sort(key=lambda item: (item[0].casefold(), item[1]))
    path = _PERCENT_ESCAPE_RE.sub(lambda match: match.group(0).upper(), parsed.path)
    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


def normalize_source_images(
    values: list[SourceImage] | tuple[SourceImage, ...],
    *,
    limit: int = 120,
) -> tuple[SourceImage, ...]:
    deduped: list[SourceImage] = []
    positions: dict[str, int] = {}
    for candidate in values:
        key = canonical_image_url_key(candidate.url)
        if not key:
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(deduped)
            deduped.append(candidate)
            continue
        deduped[position] = _merge_source_image(deduped[position], candidate)

    output: list[SourceImage] = []
    featured_kinds: set[str] = set()
    safe_limit = max(1, min(int(limit), 500))
    for image in deduped:
        if image.kind in {"cover", "backdrop"}:
            if image.kind in featured_kinds:
                continue
            featured_kinds.add(image.kind)
        output.append(image)
        if len(output) >= safe_limit:
            break
    return tuple(output)


def _merge_source_image(current: SourceImage, candidate: SourceImage) -> SourceImage:
    current_kind = _IMAGE_KIND_PRIORITY.get(current.kind, -1)
    candidate_kind = _IMAGE_KIND_PRIORITY.get(candidate.kind, -1)
    if candidate_kind > current_kind or (
        candidate_kind == current_kind
        and _source_image_quality(candidate) >= _source_image_quality(current)
    ):
        preferred, fallback = candidate, current
    else:
        preferred, fallback = current, candidate
    return SourceImage(
        kind=preferred.kind,
        url=preferred.url,
        thumbnail_url=preferred.thumbnail_url or fallback.thumbnail_url,
        width=preferred.width or fallback.width,
        height=preferred.height or fallback.height,
    )


def _source_image_quality(image: SourceImage) -> tuple[bool, int, int, float, int]:
    try:
        query_items = parse_qsl(urlsplit(image.url).query, keep_blank_values=True)
    except ValueError:
        query_items = []
    query = {key.strip().casefold(): value for key, value in query_items}
    has_resize = any(key in query for key in _IMAGE_RESIZE_QUERY_KEYS)
    width = image.width or _query_dimension(query.get("width") or query.get("w"))
    height = image.height or _query_dimension(query.get("height") or query.get("h"))
    dpr = _query_number(query.get("dpr")) or 1.0
    if image.width is None and width:
        width = int(width * dpr)
    if image.height is None and height:
        height = int(height * dpr)
    area = width * height if width and height else 0
    edge = max(width or 0, height or 0)
    quality = _query_number(query.get("quality") or query.get("q"))
    return (
        not has_resize,
        area,
        edge,
        quality if quality is not None else 101.0,
        int(bool(width)) + int(bool(height)),
    )


def _query_dimension(value: str | None) -> int | None:
    number = _query_number(value)
    if number is None:
        return None
    dimension = int(number)
    return dimension if 0 < dimension <= 100_000 else None


def _query_number(value: str | None) -> float | None:
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class FieldSourceOrigin(TypedDict, total=False):
    source_id: str
    provider: str
    url: str
    upstream_source: str
    upstream_url: str


class FieldSource(FieldSourceOrigin, total=False):
    contributors: list[FieldSourceOrigin]


@dataclass(frozen=True)
class WorkSource:
    source_id: str
    raw_code: str | None
    title: str
    detail_url: str | None
    release_date: str | None
    images: tuple[SourceImage, ...] = ()
    details: SourceDetails = field(default_factory=SourceDetails)
    magnet_hint: MagnetHint = "unknown"
    parse_status: Literal["summary", "resolved", "error"] = "summary"
    error: str | None = None
    details_error: str | None = None
    image_error: str | None = None
    magnet_error: str | None = None
    field_sources: dict[str, FieldSource] = field(default_factory=dict)
    detail_provider: str | None = None
    detail_identity_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "raw_code": self.raw_code,
            "title": self.title,
            "detail_url": self.detail_url,
            "release_date": self.release_date,
            "images": [image.to_dict() for image in self.images],
            "details": self.details.to_dict(),
            "magnet_hint": self.magnet_hint,
            "parse_status": self.parse_status,
            "error": self.error,
            "details_error": self.details_error,
            "image_error": self.image_error,
            "magnet_error": self.magnet_error,
            "field_sources": self.field_sources,
            "detail_provider": self.detail_provider,
            "detail_identity_verified": self.detail_identity_verified,
        }


@dataclass(frozen=True)
class MagnetSourceRef:
    source_id: str
    uri: str
    display_name: str | None = None
    reported_size_text: str | None = None
    reported_size_bytes: int | None = None
    badges: tuple[str, ...] = ()
    trackers: tuple[str, ...] = ()
    reported_seeders: int | None = None
    reported_leechers: int | None = None
    reported_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "uri": self.uri,
            "display_name": self.display_name,
            "reported_size_text": self.reported_size_text,
            "reported_size_bytes": self.reported_size_bytes,
            "badges": list(self.badges),
            "trackers": list(self.trackers),
            "reported_seeders": self.reported_seeders,
            "reported_leechers": self.reported_leechers,
            "reported_at": self.reported_at,
        }


@dataclass(frozen=True)
class MagnetGroup:
    info_hash: str
    display_name: str | None
    size_bytes: int | None
    source_refs: tuple[MagnetSourceRef, ...]
    size_is_exact: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "info_hash": self.info_hash,
            "display_name": self.display_name,
            "size_bytes": self.size_bytes,
            "size_is_exact": self.size_is_exact,
            "source_refs": [source.to_dict() for source in self.source_refs],
        }


@dataclass(frozen=True)
class WorkResult:
    work_id: str
    canonical_code: str | None
    code: str | None
    title: str
    release_date: str | None
    release_date_conflict: bool = False
    actors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    sources: tuple[WorkSource, ...] = ()
    magnets: tuple[MagnetGroup, ...] = ()

    @property
    def magnet_hint(self) -> MagnetHint:
        if self.magnets or any(
            source.magnet_hint == "available" for source in self.sources
        ):
            return "available"
        if self.sources and all(
            source.magnet_hint == "unavailable" for source in self.sources
        ):
            return "unavailable"
        return "unknown"

    @property
    def url(self) -> str | None:
        return next(
            (source.detail_url for source in self.sources if source.detail_url), None
        )

    @property
    def cover(self) -> SourceImage | None:
        preferred = self._preferred_cover()
        return preferred[1] if preferred else None

    def _preferred_cover(self) -> tuple[WorkSource, SourceImage] | None:
        for source in self.sources:
            for image in source.images:
                if image.kind == "cover":
                    return source, image
        return None

    def to_dict(self) -> dict[str, Any]:
        preferred_cover = self._preferred_cover()
        cover = None
        if preferred_cover:
            source, image = preferred_cover
            cover = {"source_id": source.source_id, **image.to_dict()}
        return {
            "work_id": self.work_id,
            "canonical_code": self.canonical_code,
            "code": self.code,
            "title": self.title,
            "release_date": self.release_date,
            "release_date_conflict": self.release_date_conflict,
            "cover": cover,
            "actors": list(self.actors),
            "tags": list(self.tags),
            "magnet_hint": self.magnet_hint,
            "sources": [source.to_dict() for source in self.sources],
            "magnets": [magnet.to_dict() for magnet in self.magnets],
        }


@dataclass(frozen=True)
class SearchContinuationSource:
    source_id: str
    records: tuple[SearchResult, ...]
    next_page: int | None


@dataclass(frozen=True)
class SearchContinuation:
    sources: tuple[SearchContinuationSource, ...]
    pages_scanned: int
    result_limit: int
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: tuple[WorkResult, ...]
    errors: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    sort: SearchSort = "relevance"
    match: SearchMatch = "auto"
    result_limit: int | None = None
    found_count: int | None = None
    pages_scanned: int = 0
    pages_total: int | None = None
    continuation: SearchContinuation | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [result.to_dict() for result in self.results],
            "errors": self.errors,
            "skipped": self.skipped,
            "sort": self.sort,
            "match": self.match,
            "result_limit": self.result_limit,
            "found_count": (
                self.found_count if self.found_count is not None else len(self.results)
            ),
            "pages_scanned": self.pages_scanned,
            "pages_total": self.pages_total,
            "can_continue": self.continuation is not None,
        }


@dataclass(frozen=True)
class SearchPageUpdate:
    source_id: str
    upstream_page: int
    delta: tuple[WorkResult, ...]
    pages_scanned: int
    pages_total: int | None
    found_count: int
    result_limit: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "upstream_page": self.upstream_page,
            "delta": [result.to_dict() for result in self.delta],
            "pages_scanned": self.pages_scanned,
            "pages_total": self.pages_total,
            "found_count": self.found_count,
            "result_limit": self.result_limit,
        }
