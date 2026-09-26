from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any, Iterable, Literal, Mapping

from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..net.http_client import FetchError
from ..indexers.html_parsers import (
    extract_details_with_rules,
    extract_images_with_rules,
    extract_javbus_details_from_html,
    extract_javbus_images_from_html,
    extract_javdb_details_from_html,
    extract_javdb_images_from_html,
    extract_magnets_with_rules,
)
from ..indexers.fc2 import Fc2Indexer
from ..indexers.metadata_catalog import MetadataCatalogIndexer
from ..indexers.javbus import JavBusIndexer
from ..indexers.javdb import (
    JavDbIndexer,
    _looks_like_javdb_detail_content,
    _looks_like_javdb_hard_block,
    _looks_like_javdb_interstitial,
)
from ..core.models import (
    Rating,
    SearchBounds,
    SearchResult,
    SourceDetails,
    SourceImage,
    normalize_source_images,
)
from ..config.settings import load_settings
from ..search.engine import default_indexers
from ..config.source_catalog import METADATA_CATALOG, METADATA_PROFILES


MetadataProfile = Literal["javbus", "javdb", "fc2", "fanza", "mgs", "avbase", "fc2db", "javten"]

_MAX_TEXT_LENGTH = 500
_MAX_RELATED_ITEMS = 100
_DETAIL_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:"
    r"FC2PPV\d{2,9}"
    r"|"
    r"[A-Z0-9]{2,16}(?:[-._ ][A-Z0-9]{2,10})*[-._ ]\d{2,9}"
    r"|[A-Z]{2,12}\d{2,8}"
    r")(?![A-Z0-9])",
    flags=re.IGNORECASE,
)
_JAVBUS_IDENTITY_LABEL_RE = re.compile(r"^\s*(?:識別碼|识别码|番号|品番)\s*[:：]?\s*")
_HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class MediaMetadataSourceError(RuntimeError):
    pass


class MediaMetadataNotFound(MediaMetadataSourceError):
    pass


@dataclass(frozen=True, slots=True)
class MetadataImageCandidate:
    source_id: str
    parser_profile: MetadataProfile
    base_url: str = field(repr=False)
    detail_url: str = field(repr=False)
    kind: str
    url: str = field(repr=False)
    thumbnail_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MediaMetadata:
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
    image_candidates: tuple[MetadataImageCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "title": self.title,
            "original_title": self.original_title,
            "release_date": self.release_date,
            "duration_minutes": self.duration_minutes,
            "rating": self.rating,
            "makers": list(self.makers),
            "publishers": list(self.publishers),
            "series": list(self.series),
            "directors": list(self.directors),
            "actors": list(self.actors),
            "tags": list(self.tags),
            "description": self.description,
            "image_candidates": [
                {
                    "source_id": image.source_id,
                    "parser_profile": image.parser_profile,
                    "kind": image.kind,
                }
                for image in self.image_candidates
            ],
        }


@dataclass(frozen=True, slots=True)
class _SourceMetadata:
    source_id: str
    parser_profile: MetadataProfile
    title: str | None
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
    images: tuple[MetadataImageCandidate, ...]
    detail_verified: bool
    description: str | None = None
    field_sources: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MediaMetadataSourceSnapshot:
    source_id: str
    parser_profile: MetadataProfile
    title: str | None
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
    image_candidates: tuple[MetadataImageCandidate, ...] = field(repr=False)
    description: str | None = None
    field_sources: dict[str, Any] = field(default_factory=dict)

    def review_fields(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for name in (
            "title",
            "original_title",
            "release_date",
            "duration_minutes",
            "rating",
            "makers",
            "publishers",
            "series",
            "directors",
            "actors",
            "tags",
            "description",
        ):
            value = getattr(self, name)
            if value is not None and value != ():
                values[name] = list(value) if isinstance(value, tuple) else value
        return values

    def public_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "parser_profile": self.parser_profile,
            "fields": self.review_fields(),
            "field_sources": self.field_sources,
            "images": [
                {"source_id": item.source_id, "kind": item.kind}
                for item in self.image_candidates
            ],
        }


class _DetailIdentityParser(HTMLParser):
    def __init__(self, profile: MetadataProfile) -> None:
        super().__init__(convert_charrefs=True)
        self.profile = profile
        self._structured_identities: list[str] = []
        self._heading_identities: list[str] = []
        self._title_identities: list[str] = []
        self._meta_identities: list[str] = []
        self._capture_depth = 0
        self._capture_kind: str | None = None
        self._capture_parts: list[str] = []
        self._info_depth = 0
        self._has_detail_structure = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_starttag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_starttag(tag, attrs, self_closing=True)

    def _handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        tag_name = tag.casefold()
        values = {
            str(name or "").strip().casefold(): str(value or "")
            for name, value in attrs
        }
        classes = {item.casefold() for item in values.get("class", "").split() if item}
        is_void = self_closing or tag_name in _HTML_VOID_ELEMENTS
        if tag_name == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key in {"og:title", "twitter:title"} and values.get("content"):
                self._meta_identities.append(values["content"])

        if self.profile == "javbus":
            if self._info_depth:
                if not is_void:
                    self._info_depth += 1
            elif tag_name == "div" and "info" in classes and not is_void:
                self._info_depth = 1
                self._has_detail_structure = True
        elif tag_name == "div" and "video-meta-panel" in classes:
            self._has_detail_structure = True

        if self._capture_depth:
            if is_void:
                self._capture_parts.append(" ")
            else:
                self._capture_depth += 1
            return
        capture_kind = "title" if tag_name == "title" else None
        if self.profile == "javdb":
            if tag_name == "div" and "movie-panel-info" in classes:
                capture_kind = "structured"
        else:
            if tag_name in {"h1", "h2", "h3"}:
                capture_kind = "heading"
            elif tag_name == "p" and self._info_depth > 0:
                capture_kind = "info"
        if capture_kind is not None and not is_void:
            self._capture_depth = 1
            self._capture_kind = capture_kind
            self._capture_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.casefold()
        if tag_name in _HTML_VOID_ELEMENTS:
            return
        if self._capture_depth:
            self._capture_depth -= 1
            if not self._capture_depth:
                value = " ".join("".join(self._capture_parts).split())
                self._store_capture(value)
                self._capture_kind = None
                self._capture_parts = []
        if self.profile == "javbus" and self._info_depth:
            self._info_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_depth:
            self._capture_parts.append(data)

    def primary_identity(self) -> str | None:
        groups = (
            (
                self._structured_identities,
                self._heading_identities,
                self._title_identities,
                self._meta_identities,
            )
            if self.profile == "javbus"
            else (
                self._structured_identities,
                self._title_identities,
                self._meta_identities,
            )
        )
        for group in groups:
            for value in group:
                if (
                    group is self._title_identities or group is self._meta_identities
                ) and not self._has_detail_structure:
                    continue
                if _canonical_detail_identity(value) is not None:
                    return value
        return None

    def _store_capture(self, value: str) -> None:
        if not value:
            return
        if self._capture_kind == "structured":
            self._structured_identities.append(value)
        elif self._capture_kind == "heading":
            self._heading_identities.append(value)
        elif self._capture_kind == "title":
            self._title_identities.append(value)
        elif self._capture_kind == "info":
            match = _JAVBUS_IDENTITY_LABEL_RE.match(value)
            if match:
                self._structured_identities.append(value[match.end() :])


def resolve_media_metadata(
    code: object,
    *,
    settings: dict[str, Any] | None = None,
    description: str | None = None,
    indexers: Mapping[str, object] | None = None,
    bounds: SearchBounds | None = None,
) -> MediaMetadata:
    """Resolve exact-code metadata without requesting magnets or magnet AJAX."""

    active_settings = settings if settings is not None else load_settings()
    display_code, snapshots = _resolve_source_snapshots(
        code,
        settings=active_settings,
        indexers=indexers,
        bounds=bounds,
    )
    return merge_media_metadata(
        display_code,
        snapshots,
        description=description,
        source_priority=active_settings.get("metadata_scraper_site_priority"),
    )


def resolve_media_metadata_snapshots(
    code: object,
    *,
    settings: dict[str, Any] | None = None,
    indexers: Mapping[str, object] | None = None,
    bounds: SearchBounds | None = None,
) -> tuple[MediaMetadataSourceSnapshot, ...]:
    _display_code, snapshots = _resolve_source_snapshots(
        code,
        settings=settings,
        indexers=indexers,
        bounds=bounds,
    )
    return tuple(
        MediaMetadataSourceSnapshot(
            source_id=snapshot.source_id,
            parser_profile=snapshot.parser_profile,
            title=snapshot.title,
            original_title=snapshot.original_title,
            release_date=snapshot.release_date,
            duration_minutes=snapshot.duration_minutes,
            rating=snapshot.rating,
            makers=snapshot.makers,
            publishers=snapshot.publishers,
            series=snapshot.series,
            directors=snapshot.directors,
            actors=snapshot.actors,
            tags=snapshot.tags,
            image_candidates=snapshot.images,
            description=snapshot.description,
            field_sources=snapshot.field_sources,
        )
        for snapshot in snapshots
    )


def _resolve_source_snapshots(
    code: object,
    *,
    settings: dict[str, Any] | None,
    indexers: Mapping[str, object] | None,
    bounds: SearchBounds | None,
) -> tuple[str, tuple[_SourceMetadata, ...]]:
    display_code, canonical_code = _validated_code(code)
    active_settings = settings if settings is not None else load_settings()
    registry = (
        dict(indexers) if indexers is not None else default_indexers(active_settings)
    )
    source_bounds = (
        bounds
        or SearchBounds(
            limit=20,
            page=1,
            max_pages=1,
            fetch_magnets=False,
            detail_limit=0,
            match="exact",
            search_kind="code",
        )
    ).normalized()
    source_bounds = SearchBounds(
        limit=source_bounds.limit,
        page=1,
        max_pages=1,
        fetch_magnets=False,
        detail_limit=0,
        timeout_seconds=source_bounds.timeout_seconds,
        max_response_bytes=source_bounds.max_response_bytes,
        filters=source_bounds.filters,
        sort="relevance",
        match="exact",
        search_kind="code",
        semantic_refs={},
    ).normalized()

    snapshots: list[_SourceMetadata] = []
    source_failed = False
    ordered = _ordered_metadata_indexers(active_settings, registry)
    fallback = _fallback_metadata_indexers(
        active_settings, {source_id for source_id, _, _ in ordered}, canonical_code
    ) if indexers is None else ()
    for position, (source_id, profile, indexer) in enumerate((*ordered, *fallback)):
        if position >= len(ordered) and _snapshots_sufficient(snapshots):
            # Built-in sources that are not enabled for search are only
            # consulted when the configured sources failed to verify the work
            # or supplied no cover art.
            break
        active_bounds = _metadata_source_bounds(source_bounds, profile)
        try:
            result = _find_exact_result(
                indexer, display_code, canonical_code, active_bounds
            )
        except Exception:  # noqa: BLE001 - one source must not prevent the configured fallback.
            source_failed = True
            continue
        if result is None:
            continue
        if isinstance(indexer, MetadataCatalogIndexer):
            try:
                if not result.metadata.get("detail_identity_verified"):
                    result = indexer.resolve(result, active_bounds)
                snapshot = _snapshot_from_native_result(source_id, profile, indexer, result)
            except (FetchError, NotImplementedError):
                source_failed = True
                continue
            if not snapshot.detail_verified:
                source_failed = True
                continue
            snapshots.append(snapshot)
            continue
        if isinstance(indexer, Fc2Indexer):
            snapshot = _snapshot_from_fc2_result(source_id, indexer, result)
            if not snapshot.detail_verified:
                source_failed = True
                continue
            snapshots.append(snapshot)
            continue
        detail_html = ""
        if result.url:
            try:
                detail_html = _fetch_detail_html(indexer, result.url, active_bounds)
            except Exception:  # noqa: BLE001 - retain exact search-card metadata.
                source_failed = True
            if detail_html and not _detail_page_matches_code(
                detail_html,
                profile,
                canonical_code,
                indexer=indexer,
            ):
                source_failed = True
                detail_html = ""
        snapshots.append(
            _snapshot_from_html(
                source_id,
                profile,
                indexer,
                result,
                detail_html,
            )
        )

    if not snapshots:
        if source_failed:
            raise MediaMetadataSourceError(
                "metadata sources are temporarily unavailable"
            )
        raise MediaMetadataNotFound("no exact metadata source result was found")
    if not any(snapshot.detail_verified for snapshot in snapshots):
        raise MediaMetadataSourceError("metadata details are temporarily unavailable")
    return display_code, tuple(snapshots)


def _snapshots_sufficient(snapshots: Iterable[_SourceMetadata]) -> bool:
    items = tuple(snapshots)
    return any(snapshot.detail_verified for snapshot in items) and any(
        image.kind in {"cover", "backdrop"}
        for snapshot in items
        for image in snapshot.images
    )


def _metadata_fallback_enabled(settings: dict[str, Any]) -> bool:
    defaults = settings.get("workflow_defaults") if isinstance(settings, dict) else None
    value = defaults.get("metadata_auto_fallback") if isinstance(defaults, dict) else None
    return value is not False


def _fallback_metadata_indexers(
    settings: dict[str, Any],
    already_used: set[str],
    canonical_code: str,
) -> tuple[tuple[str, MetadataProfile, object], ...]:
    """Built-in catalog adapters to try when the configured sources fall short."""

    if not _metadata_fallback_enabled(settings):
        return ()
    fc2 = canonical_code.startswith("FC2PPV")
    raw_sites = settings.get("sites", []) if isinstance(settings, dict) else []
    output: list[tuple[str, MetadataProfile, object]] = []
    for site in raw_sites if isinstance(raw_sites, list) else ():
        if not isinstance(site, dict):
            continue
        source_id = str(site.get("id") or "").strip()
        profile = str(site.get("parser_profile") or "")
        if source_id in already_used or profile not in METADATA_CATALOG:
            continue
        if (profile in _FC2_CATALOG_PROFILES) != fc2:
            continue
        registry = default_indexers(
            {**settings, "sites": [{**site, "enabled": True}]}
        )
        indexer = registry.get(source_id)
        if isinstance(indexer, MetadataCatalogIndexer) and indexer.name == profile:
            output.append((source_id, profile, indexer))  # type: ignore[arg-type]
    return tuple(output)


_FC2_CATALOG_PROFILES = frozenset({"fc2db", "javten"})


def _metadata_source_bounds(
    bounds: SearchBounds,
    profile: MetadataProfile,
) -> SearchBounds:
    filters = dict(bounds.filters)
    search_kind = "code"
    if profile == "javdb":
        # JavDB's dedicated code route is more aggressively challenged. The
        # all-fields route is still exact-safe because _find_exact_result applies
        # canonical catalog-code matching locally before any detail fetch.
        filters["f"] = "all"
        search_kind = "keyword"
    return SearchBounds(
        limit=bounds.limit,
        page=1,
        max_pages=1,
        fetch_magnets=False,
        detail_limit=0,
        timeout_seconds=bounds.timeout_seconds,
        max_response_bytes=bounds.max_response_bytes,
        filters=filters,
        sort="relevance",
        match="exact",
        search_kind=search_kind,
        semantic_refs={},
    ).normalized()


def merge_media_metadata(
    code: object,
    sources: Iterable[_SourceMetadata],
    *,
    description: str | None = None,
    source_priority: object = None,
) -> MediaMetadata:
    display_code, _ = _validated_code(code)
    raw_priority = source_priority if isinstance(source_priority, list) else []
    priority = {
        str(source_id): index
        for index, source_id in enumerate(raw_priority)
        if isinstance(source_id, str)
    }
    ordered = sorted(
        tuple(sources),
        key=lambda source: (
            priority.get(source.source_id, len(priority) + 1),
            0 if source.parser_profile == "javbus" else 1,
        ),
    )
    if not ordered:
        raise MediaMetadataNotFound("no metadata source result was provided")

    title = _first_text(source.title for source in ordered) or display_code
    original_title = next(
        (
            value
            for value in (source.original_title for source in ordered)
            if value and not _same_title(value, title)
        ),
        None,
    )
    release_date = _first_value(source.release_date for source in ordered)
    duration_minutes = _first_value(source.duration_minutes for source in ordered)
    rating = _first_value(source.rating for source in ordered)
    image_candidates = _dedupe_images(
        image for source in ordered for image in source.images
    )
    return MediaMetadata(
        code=display_code,
        title=title,
        original_title=original_title,
        release_date=release_date,
        duration_minutes=duration_minutes,
        rating=rating,
        makers=_first_sequence(source.makers for source in ordered),
        publishers=_first_sequence(source.publishers for source in ordered),
        series=_first_sequence(source.series for source in ordered),
        directors=_first_sequence(source.directors for source in ordered),
        actors=_first_sequence(source.actors for source in ordered),
        tags=_first_sequence(source.tags for source in ordered),
        description=_clean_description(description),
        image_candidates=image_candidates,
    )


def _ordered_metadata_indexers(
    settings: dict[str, Any], registry: Mapping[str, object]
) -> tuple[tuple[str, MetadataProfile, object], ...]:
    raw_sites = settings.get("sites", []) if isinstance(settings, dict) else []
    sites = raw_sites if isinstance(raw_sites, list) else []
    raw_priority = settings.get("metadata_scraper_site_priority", [])
    priority = {
        str(source_id): index
        for index, source_id in enumerate(raw_priority)
        if isinstance(raw_priority, list) and isinstance(source_id, str)
    }
    indexed_candidates = [
        (index, site) for index, site in enumerate(sites) if isinstance(site, dict)
    ]
    indexed_candidates.sort(
        key=lambda item: (
            priority.get(str(item[1].get("id") or ""), len(priority) + item[0]),
            item[0],
        )
    )
    candidates = [site for _index, site in indexed_candidates]
    ordered: list[tuple[str, MetadataProfile, object]] = []
    for site in candidates:
        if not site.get("enabled", True):
            continue
        profile = str(site.get("parser_profile") or site.get("id") or "")
        if profile not in METADATA_PROFILES:
            continue
        source_id = str(site.get("id") or "").strip()
        indexer = registry.get(source_id)
        if indexer is None:
            continue
        if profile == "javbus" and not isinstance(indexer, JavBusIndexer):
            continue
        if profile == "javdb" and not isinstance(indexer, JavDbIndexer):
            continue
        if profile == "fc2" and not isinstance(indexer, Fc2Indexer):
            continue
        if profile in METADATA_CATALOG and (
            not isinstance(indexer, MetadataCatalogIndexer) or indexer.name != profile
        ):
            continue
        ordered.append((source_id, profile, indexer))  # type: ignore[arg-type]
    return tuple(ordered)


def _find_exact_result(
    indexer: object,
    display_code: str,
    canonical_code: str,
    bounds: SearchBounds,
) -> SearchResult | None:
    search = getattr(indexer, "search", None)
    if not callable(search):
        return None
    results = search(display_code, bounds)
    return next(
        (
            result
            for result in results
            if isinstance(result, SearchResult)
            and canonical_catalog_code(result.code) == canonical_code
        ),
        None,
    )


def _fetch_detail_html(indexer: object, url: str, bounds: SearchBounds) -> str:
    if isinstance(indexer, JavBusIndexer):
        return indexer._fetch(url, bounds)
    if not isinstance(indexer, JavDbIndexer):
        raise MediaMetadataSourceError("unsupported metadata source")

    mode = os.environ.get("JAV_PILOT_JAVDB_FETCHER", "auto").strip().lower()
    if mode not in {"auto", "browser", "http"}:
        mode = "auto"
    if mode != "browser":
        try:
            page = indexer._fetch_http(url, bounds)
            _require_javdb_detail_page(page, indexer=indexer)
            return page
        except Exception:
            if mode == "http":
                raise
    with indexer._make_browser_fetcher(bounds) as browser:
        page = browser.fetch(url)
    _require_javdb_detail_page(page, indexer=indexer)
    return page


def _require_javdb_detail_page(page: str, *, indexer: object | None = None) -> None:
    if _looks_like_javdb_hard_block(page):
        raise FetchError("JavDB metadata source is unavailable")
    if _looks_like_javdb_interstitial(page) and not _looks_like_javdb_detail_content(
        page
    ):
        raise FetchError("JavDB metadata source is unavailable")
    if not _looks_like_javdb_detail_content(
        page
    ) and not _configured_detail_has_content(page, indexer):
        raise FetchError("JavDB metadata detail page is incomplete")


def _configured_detail_has_content(page: str, indexer: object | None) -> bool:
    rules = _indexer_detail_rules(indexer)
    if rules is None:
        return False
    base_url = str(getattr(indexer, "base_url", "") or "").rstrip("/")
    details = extract_details_with_rules(page, base_url, rules=rules)
    if details != SourceDetails():
        return True
    if extract_images_with_rules(
        page,
        base_url,
        profile="javdb",
        rules=rules["images"],
    ):
        return True
    return bool(extract_magnets_with_rules(page, rules=rules["magnets"]))


def _indexer_detail_rules(indexer: object | None) -> dict[str, Any] | None:
    parser_rules = getattr(indexer, "parser_rules", None)
    if not isinstance(parser_rules, dict):
        return None
    detail_rules = parser_rules.get("detail")
    return detail_rules if isinstance(detail_rules, dict) else None


def _configured_details(
    page: str,
    indexer: object | None,
    *,
    base_url: str = "",
) -> SourceDetails:
    rules = _indexer_detail_rules(indexer)
    if rules is None:
        return SourceDetails()
    effective_base_url = base_url or str(getattr(indexer, "base_url", "") or "").rstrip(
        "/"
    )
    return extract_details_with_rules(page, effective_base_url, rules=rules)


def _resolved_details(
    existing: SourceDetails, candidate: SourceDetails
) -> SourceDetails:
    return SourceDetails(
        title=candidate.title or existing.title,
        original_title=candidate.original_title or existing.original_title,
        release_date=candidate.release_date or existing.release_date,
        duration_minutes=candidate.duration_minutes or existing.duration_minutes,
        duration_text=candidate.duration_text or existing.duration_text,
        rating=candidate.rating or existing.rating,
        makers=candidate.makers or existing.makers,
        publishers=candidate.publishers or existing.publishers,
        series=candidate.series or existing.series,
        directors=candidate.directors or existing.directors,
        actors=candidate.actors or existing.actors,
        tags=candidate.tags or existing.tags,
    )


def _detail_page_matches_code(
    page: str,
    profile: MetadataProfile,
    canonical_code: str,
    *,
    indexer: object | None = None,
) -> bool:
    parser = _DetailIdentityParser(profile)
    try:
        parser.feed(page)
        parser.close()
    except (TypeError, ValueError):
        return False
    identity = parser.primary_identity()
    if _canonical_detail_identity(identity) == canonical_code:
        return True
    configured = _configured_details(page, indexer)
    return any(
        _canonical_detail_identity(value) == canonical_code
        for value in (configured.title, configured.original_title)
        if value
    )


def _canonical_detail_identity(value: object) -> str | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
    match = _DETAIL_CODE_RE.search(normalized)
    if match is None:
        return None
    return canonical_catalog_code(match.group(0), max_length=40)


def _snapshot_from_html(
    source_id: str,
    profile: MetadataProfile,
    indexer: object,
    result: SearchResult,
    page: str,
) -> _SourceMetadata:
    base_url = str(getattr(indexer, "base_url", "") or "").rstrip("/")
    detail_url = str(result.url or "")
    if profile == "javbus":
        details = extract_javbus_details_from_html(page, base_url)
        detail_images = extract_javbus_images_from_html(page, base_url)
    else:
        details = extract_javdb_details_from_html(page, base_url)
        detail_images = extract_javdb_images_from_html(page, base_url)
    configured_details = _configured_details(page, indexer, base_url=base_url)
    details = _resolved_details(details, configured_details)
    parser_rules = _indexer_detail_rules(indexer)
    if parser_rules is not None:
        configured_images = extract_images_with_rules(
            page,
            base_url,
            profile=profile,
            rules=parser_rules["images"],
        )
        detail_images = normalize_source_images((*configured_images, *detail_images))

    images: list[SourceImage] = []
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    cover = _clean_text(metadata.get("cover"), 4096)
    if cover:
        images.append(SourceImage(kind="cover", url=cover))
    images.extend(detail_images)
    candidates = tuple(
        MetadataImageCandidate(
            source_id=source_id,
            parser_profile=profile,
            base_url=base_url,
            detail_url=detail_url,
            kind=image.kind,
            url=image.url,
            thumbnail_url=image.thumbnail_url,
        )
        for image in images
        if image.url
    )
    canonical_code = canonical_catalog_code(result.code, max_length=40)
    detail_title = _without_exact_code_prefix(details.title, canonical_code)
    search_title = _without_exact_code_prefix(result.title, canonical_code)
    title = detail_title or search_title
    original_title = _without_exact_code_prefix(
        details.original_title,
        canonical_code,
    )
    if original_title and title and _same_title(original_title, title):
        original_title = None
    return _SourceMetadata(
        source_id=source_id,
        parser_profile=profile,
        title=title,
        original_title=original_title,
        release_date=_release_date(details.release_date) or _release_date(result.date),
        duration_minutes=_duration(details),
        rating=_rating(details.rating, profile=profile),
        makers=_related_labels(details, "makers"),
        publishers=_related_labels(details, "publishers"),
        series=_related_labels(details, "series"),
        directors=_related_labels(details, "directors"),
        actors=_related_labels(details, "actors") or _labels(result.actors),
        tags=_related_labels(details, "tags") or _labels(result.tags),
        images=candidates,
        detail_verified=bool(page),
    )


def _snapshot_from_fc2_result(
    source_id: str,
    indexer: Fc2Indexer,
    result: SearchResult,
) -> _SourceMetadata:
    profile: MetadataProfile = "fc2"
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    base_url = str(indexer.base_url or "").rstrip("/")
    detail_url = str(result.url or "")
    details = result.details
    images = _result_metadata_images(metadata)
    candidates = tuple(
        MetadataImageCandidate(
            source_id=source_id,
            parser_profile=profile,
            base_url=base_url,
            detail_url=detail_url,
            kind=image.kind,
            url=image.url,
            thumbnail_url=image.thumbnail_url,
        )
        for image in images
        if image.url
    )
    canonical_code = canonical_catalog_code(result.code, max_length=40)
    title = _without_exact_code_prefix(details.title or result.title, canonical_code)
    original_title = _without_exact_code_prefix(details.original_title, canonical_code)
    if original_title and title and _same_title(original_title, title):
        original_title = None
    return _SourceMetadata(
        source_id=source_id,
        parser_profile=profile,
        title=title,
        original_title=original_title,
        release_date=_release_date(details.release_date) or _release_date(result.date),
        duration_minutes=_duration(details),
        rating=_rating(details.rating, profile=profile),
        makers=_related_labels(details, "makers"),
        publishers=_related_labels(details, "publishers"),
        series=(),
        directors=(),
        actors=(),
        tags=_related_labels(details, "tags") or _labels(result.tags),
        images=candidates,
        detail_verified=metadata.get("detail_identity_verified") is True,
        field_sources=dict(metadata.get("field_sources") or {}),
    )


def _snapshot_from_native_result(
    source_id: str, profile: MetadataProfile, indexer: MetadataCatalogIndexer,
    result: SearchResult,
) -> _SourceMetadata:
    metadata = result.metadata
    details = result.details
    canonical_code = canonical_catalog_code(result.code, max_length=40)
    images = tuple(
        MetadataImageCandidate(source_id=source_id, parser_profile=profile,
                               base_url=indexer.base_url, detail_url=result.url or "",
                               kind=image.kind, url=image.url, thumbnail_url=image.thumbnail_url)
        for image in _result_metadata_images(metadata)
    )
    return _SourceMetadata(
        source_id=source_id, parser_profile=profile,
        title=_without_exact_code_prefix(details.title or result.title, canonical_code),
        original_title=_without_exact_code_prefix(details.original_title, canonical_code),
        release_date=_release_date(details.release_date), duration_minutes=_duration(details),
        rating=_rating(details.rating, profile=profile), makers=_related_labels(details, "makers"),
        publishers=_related_labels(details, "publishers"), series=_related_labels(details, "series"),
        directors=_related_labels(details, "directors"), actors=_related_labels(details, "actors"),
        tags=_related_labels(details, "tags"), images=images,
        detail_verified=metadata.get("detail_identity_verified") is True,
        description=_clean_description(metadata.get("description")),
        field_sources=dict(metadata.get("field_sources") or {}),
    )


def _result_metadata_images(metadata: dict[str, Any]) -> tuple[SourceImage, ...]:
    images: list[SourceImage] = []
    cover = _clean_text(metadata.get("cover"), 8192)
    if cover:
        images.append(SourceImage(kind="cover", url=cover))
    raw_images = metadata.get("images")
    if isinstance(raw_images, (list, tuple)):
        for value in raw_images:
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            url = _clean_text(value.get("url"), 8192)
            if kind not in {"cover", "backdrop", "sample"} or not url:
                continue
            images.append(
                SourceImage(
                    kind=kind,
                    url=url,
                    thumbnail_url=_clean_text(value.get("thumbnail_url"), 8192),
                )
            )
    return normalize_source_images(images)


def _validated_code(value: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(value, max_length=40)
    if normalized is None:
        raise MediaMetadataSourceError("a valid catalog code is required")
    return normalized


def _clean_text(value: object, limit: int = _MAX_TEXT_LENGTH) -> str | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    clean = " ".join(text.replace("\x00", " ").split())
    return clean[:limit] if clean else None


def _clean_description(value: object) -> str | None:
    return _clean_text(value, 8192)


def _without_exact_code_prefix(
    value: object,
    canonical_code: str | None,
) -> str | None:
    clean = _clean_text(value)
    if not clean or not canonical_code:
        return clean
    match = _DETAIL_CODE_RE.search(clean)
    if match is None or clean[: match.start()].strip(" \t[【(（"):
        return clean
    if canonical_catalog_code(match.group(0), max_length=40) != canonical_code:
        return clean
    remainder = re.sub(
        r"^[\s\]】)）\-–—:：|｜/／]+",
        "",
        clean[match.end() :],
    )
    return remainder or None


def _same_title(left: object, right: object) -> bool:
    left_text = _clean_text(left)
    right_text = _clean_text(right)
    return bool(
        left_text and right_text and left_text.casefold() == right_text.casefold()
    )


def _release_date(value: object) -> str | None:
    clean = _clean_text(value, 32)
    if not clean:
        return None
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError:
        return None


def _duration(details: SourceDetails) -> int | None:
    value = details.duration_minutes
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 1440
        else None
    )


def _rating(value: Rating | None, *, profile: MetadataProfile) -> float | None:
    if value is None:
        return None
    rating_value = value.value if isinstance(value.value, (int, float)) else None
    if rating_value is not None and not 0 <= float(rating_value) <= 10:
        rating_value = None
    if rating_value is None:
        return None
    normalized = float(rating_value)
    if profile in {"javdb", "fc2"}:
        normalized *= 2.0
    return min(normalized, 10.0)


def _related_labels(details: SourceDetails, name: str) -> tuple[str, ...]:
    return _labels(item.label for item in getattr(details, name, ()))


def _labels(values: Iterable[object]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean_text(value)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= _MAX_RELATED_ITEMS:
            break
    return tuple(output)


def _first_text(values: Iterable[str | None]) -> str | None:
    return next((value for value in values if value), None)


def _first_value(values: Iterable[Any]) -> Any:
    return next((value for value in values if value is not None), None)


def _first_sequence(values: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    return next((value for value in values if value), ())


def _dedupe_images(
    values: Iterable[MetadataImageCandidate],
) -> tuple[MetadataImageCandidate, ...]:
    output: list[MetadataImageCandidate] = []
    seen: set[str] = set()
    for image in values:
        key = image.url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(image)
    return tuple(output)


__all__ = [
    "MediaMetadata",
    "MediaMetadataNotFound",
    "MediaMetadataSourceError",
    "MediaMetadataSourceSnapshot",
    "MetadataImageCandidate",
    "merge_media_metadata",
    "resolve_media_metadata",
    "resolve_media_metadata_snapshots",
]
