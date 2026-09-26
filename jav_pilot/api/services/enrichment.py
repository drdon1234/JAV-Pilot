"""Detail enrichment and merging of per-source search results."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import replace

from ...core.models import (
    MagnetInfo,
    SearchBounds,
    SearchResult,
    SourceDetails,
    WorkResult,
    WorkSource,
)
from ...indexers import Fc2Indexer, JavBusIndexer, JavDbIndexer
from ...indexers.html_parsers import merge_magnet_info
from ...indexers.metadata_catalog import MetadataCatalogIndexer
from ...indexers.torznab import TorznabIndexer
from ...search.engine import merge_work_records, sanitize_source_error
from .. import state
from .search_stream import work_cache_key

MAX_STREAM_ENRICHMENTS_PER_SOURCE = 5


class JavDbFetcherPool:
    def __init__(self, bounds: SearchBounds) -> None:
        self._bounds = bounds
        self._fetcher: object | None = None
        self._failed = False

    def get(self, indexer: JavDbIndexer) -> object | None:
        if self._fetcher is not None:
            return self._fetcher
        if self._failed:
            return None
        try:
            self._fetcher = indexer._make_browser_fetcher(self._bounds).__enter__()
        except Exception:  # noqa: BLE001 - HTTP error metadata remains available as fallback.
            self._failed = True
            return None
        return self._fetcher

    def close(self) -> None:
        if self._fetcher is None:
            return
        try:
            self._fetcher.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            self._fetcher = None


def _work_has_source_errors(work: WorkResult) -> bool:
    return any(
        source.parse_status == "error" or bool(source.error) for source in work.sources
    )


def work_has_enrichment_errors(work: WorkResult) -> bool:
    return _work_has_source_errors(work) or any(
        bool(source.details_error or source.image_error or source.magnet_error)
        for source in work.sources
    )


def with_source_lookup_errors(
    work: WorkResult,
    errors: dict[str, str],
) -> WorkResult:
    sources: list[WorkSource] = []
    present: set[str] = set()
    for source in work.sources:
        present.add(source.source_id)
        error = errors.get(source.source_id)
        sources.append(
            replace(
                source,
                parse_status="error",
                error=source.error or error,
            )
            if error
            else source
        )
    for source_id, error in errors.items():
        if source_id in present:
            continue
        sources.append(
            WorkSource(
                source_id=source_id,
                raw_code=work.code,
                title=work.title,
                detail_url=None,
                release_date=None,
                parse_status="error",
                error=error,
            )
        )
    return replace(work, sources=tuple(sources))


def work_has_requested_sources(work: WorkResult, sources: Iterable[str]) -> bool:
    expected = set(sources)
    actual = {source.source_id for source in work.sources}
    return expected.issubset(actual)


def remember_works(
    works: Iterable[WorkResult],
    sources: Iterable[str],
    *,
    errors: dict[str, str] | None = None,
) -> None:
    if errors:
        return
    cache_sources = tuple(sources)
    for work in works:
        if work_has_requested_sources(work, cache_sources):
            state.WORK_CACHE.set(work_cache_key(work.work_id, cache_sources), work)


def _enrich_stream_result(
    result: SearchResult,
    registry: dict[str, object],
    bounds: SearchBounds,
    javdb_fetcher: object | None,
    *,
    include_images: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> SearchResult:
    if cancelled is not None and cancelled():
        return result
    indexer = registry.get(result.source)
    if isinstance(indexer, (Fc2Indexer, MetadataCatalogIndexer, TorznabIndexer)):
        enriched = indexer.enrich_results(
            (result,),
            bounds,
            include_images=include_images,
            detail_limit=1,
            cancelled=cancelled,
        )
        if cancelled is not None and cancelled():
            return result
        return enriched[0] if enriched else result
    if isinstance(indexer, JavBusIndexer):
        enrichment_options: dict[str, object] = {"include_images": include_images}
        if cancelled is not None:
            enrichment_options["cancelled"] = cancelled
        enriched = indexer._enrich_magnets((result,), bounds, **enrichment_options)
        if cancelled is not None and cancelled():
            return result
        return enriched[0] if enriched else result
    if isinstance(indexer, JavDbIndexer):
        enrichment_options = {"include_images": include_images}
        if cancelled is not None:
            enrichment_options["cancelled"] = cancelled
        mode = os.environ.get("JAV_PILOT_JAVDB_FETCHER", "auto").strip().lower()
        if mode not in {"auto", "browser", "http"}:
            mode = "auto"
        if mode == "browser":
            if cancelled is not None and cancelled():
                return result
            browser = _pooled_javdb_fetcher(javdb_fetcher, indexer)
            if browser is None:
                metadata = dict(result.metadata)
                metadata.update(
                    {
                        "magnet_checked": True,
                        "magnet_error": "JavDB browser fetcher is unavailable",
                    }
                )
                if include_images:
                    metadata.update(
                        {
                            "images_resolved": True,
                            "images_error": "JavDB browser fetcher is unavailable",
                        }
                    )
                return replace(result, metadata=metadata)
            enriched = indexer._enrich_magnets(
                (result,),
                bounds,
                browser,
                **enrichment_options,
            )
            if cancelled is not None and cancelled():
                return result
            return enriched[0] if enriched else result

        enriched = indexer._enrich_magnets(
            (result,),
            bounds,
            None,
            **enrichment_options,
        )
        if cancelled is not None and cancelled():
            return result
        candidate = enriched[0] if enriched else result
        if mode == "auto" and _detail_enrichment_failed(candidate):
            if cancelled is not None and cancelled():
                return result
            browser = _pooled_javdb_fetcher(javdb_fetcher, indexer)
            if browser is not None:
                try:
                    retried = indexer._enrich_magnets(
                        (result,),
                        bounds,
                        browser,
                        **enrichment_options,
                    )
                except Exception:  # noqa: BLE001 - HTTP detail data remains the fallback.
                    return candidate
                if cancelled is not None and cancelled():
                    return result
                return (
                    _merge_detail_candidates(candidate, retried[0])
                    if retried
                    else candidate
                )
        return candidate
    return result


def enrich_stream_work(
    work: WorkResult,
    registry: dict[str, object],
    bounds: SearchBounds,
    javdb_fetcher: object | None,
    *,
    include_images: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> WorkResult:
    enriched_records: list[SearchResult] = []
    for source in work.sources:
        if cancelled is not None and cancelled():
            return work
        try:
            enriched_records.append(
                _enrich_stream_result(
                    source_record(work, source),
                    registry,
                    bounds,
                    javdb_fetcher,
                    include_images=include_images,
                    cancelled=cancelled,
                )
            )
        except Exception as exc:  # noqa: BLE001 - isolate one source from the detail response.
            failed_source = replace(
                source,
                parse_status="error",
                error=sanitize_source_error(exc) or "source enrichment failed",
            )
            enriched_records.append(source_record(work, failed_source))
    if cancelled is not None and cancelled():
        return work
    if not enriched_records:
        return work
    return merge_work_records(
        work, ((record.source, record) for record in enriched_records)
    )


def stream_source_work(work: WorkResult, source_id: str) -> WorkResult | None:
    source = next(
        (candidate for candidate in work.sources if candidate.source_id == source_id),
        None,
    )
    if source is None:
        return None
    return merge_work_records(work, ((source_id, source_record(work, source)),))


def stream_work_needs_enrichment(work: WorkResult) -> bool:
    return any(
        bool(source.detail_url)
        and source.magnet_hint != "unavailable"
        and source.parse_status != "resolved"
        for source in work.sources
    )


def merge_continued_search_results(
    current: tuple[WorkResult, ...],
    previous: tuple[WorkResult, ...],
) -> tuple[WorkResult, ...]:
    previous_by_id = {work.work_id: work for work in previous}
    merged: list[WorkResult] = []
    for work in current:
        prior = previous_by_id.get(work.work_id)
        if prior is None:
            merged.append(work)
            continue
        enriched_sources: dict[str, WorkResult] = {}
        for source in prior.sources:
            source_work = stream_source_work(prior, source.source_id)
            if source_work is not None:
                enriched_sources[source.source_id] = source_work
        merged.append(merge_stream_work(work, enriched_sources))
    return tuple(merged)


def merge_stream_work(
    work: WorkResult,
    enriched_sources: dict[str, WorkResult],
) -> WorkResult:
    if not enriched_sources:
        return work
    records: list[SearchResult] = []
    for source in work.sources:
        enriched = enriched_sources.get(source.source_id)
        if enriched is None:
            records.append(source_record(work, source))
            continue
        enriched_source = next(
            (
                candidate
                for candidate in enriched.sources
                if candidate.source_id == source.source_id
            ),
            None,
        )
        records.append(
            source_record(enriched, enriched_source)
            if enriched_source
            else source_record(work, source)
        )
    if not records:
        return work
    return merge_work_records(
        work, ((record.source, record) for record in records)
    )


def source_record(work: WorkResult, source: WorkSource) -> SearchResult:
    metadata: dict[str, object] = {
        "images": [image.to_dict() for image in source.images],
        "field_sources": source.field_sources,
        "detail_provider": source.detail_provider,
        "detail_identity_verified": source.detail_identity_verified,
    }
    cover = next((image.url for image in source.images if image.kind == "cover"), None)
    if cover:
        metadata["cover"] = cover
    if source.parse_status == "resolved":
        metadata["images_resolved"] = True
        metadata["details_resolved"] = True
        metadata["magnet_checked"] = True
    if source.error:
        metadata["source_error"] = source.error
    if source.details_error:
        metadata["details_error"] = source.details_error
    if source.image_error:
        metadata["images_error"] = source.image_error
    if source.magnet_error:
        metadata["magnet_error"] = source.magnet_error
    magnets = tuple(
        MagnetInfo(
            uri=reference.uri,
            info_hash=magnet.info_hash,
            display_name=reference.display_name or magnet.display_name,
            trackers=reference.trackers,
            exact_length=magnet.size_bytes if magnet.size_is_exact else None,
            reported_size_text=reference.reported_size_text,
            reported_size_bytes=reference.reported_size_bytes,
            badges=reference.badges,
            reported_seeders=reference.reported_seeders,
            reported_leechers=reference.reported_leechers,
            reported_at=reference.reported_at,
        )
        for magnet in work.magnets
        for reference in magnet.source_refs
        if reference.source_id == source.source_id
    )
    return SearchResult(
        source=source.source_id,
        title=source.title,
        url=source.detail_url,
        code=source.raw_code or work.code,
        date=source.release_date,
        actors=work.actors,
        tags=work.tags,
        details=source.details,
        magnet_hint=source.magnet_hint,
        magnets=magnets,
        metadata=metadata,
    )


def _pooled_javdb_fetcher(
    holder: object | None, indexer: JavDbIndexer
) -> object | None:
    get = getattr(holder, "get", None)
    if callable(get):
        return get(indexer)
    return holder


def _detail_enrichment_failed(result: SearchResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return bool(
        metadata.get("source_error")
        or metadata.get("details_error")
        or metadata.get("magnet_error")
        or metadata.get("images_error")
    )


def _merge_detail_candidates(
    primary: SearchResult,
    fallback: SearchResult,
) -> SearchResult:
    primary_metadata = dict(primary.metadata)
    fallback_metadata = dict(fallback.metadata)
    metadata = dict(primary_metadata)
    if fallback_metadata.get("fetcher"):
        metadata["fetcher"] = fallback_metadata["fetcher"]

    details_source = _preferred_detail_stage(
        primary,
        fallback,
        resolved_key="details_resolved",
        error_key="details_error",
    )
    image_source = _preferred_detail_stage(
        primary,
        fallback,
        resolved_key="images_resolved",
        error_key="images_error",
    )
    magnet_source = _preferred_detail_stage(
        primary,
        fallback,
        resolved_key="magnet_checked",
        error_key="magnet_error",
    )
    if any(
        source is fallback for source in (details_source, image_source, magnet_source)
    ) and not fallback_metadata.get("source_error"):
        metadata.pop("source_error", None)
    _replace_metadata_stage(
        metadata,
        details_source.metadata,
        ("details_resolved", "details_error"),
    )
    _replace_metadata_stage(
        metadata,
        image_source.metadata,
        ("images", "cover", "images_resolved", "images_error"),
    )
    _replace_metadata_stage(
        metadata,
        magnet_source.metadata,
        ("magnet_checked", "magnet_count", "magnet_error", "magnet_endpoint"),
    )

    magnets = magnet_source.magnets
    if magnet_source is fallback and primary.magnets:
        merged_magnets: dict[str, MagnetInfo] = {}
        for magnet in (*primary.magnets, *magnets):
            merged_magnets[magnet.info_hash] = merge_magnet_info(
                merged_magnets.get(magnet.info_hash), magnet
            )
        magnets = tuple(merged_magnets.values())
    if image_source is fallback:
        images = _merged_stage_images(fallback_metadata, primary_metadata)
        metadata["images"] = images
        cover = next(
            (
                str(image.get("url"))
                for image in images
                if image.get("kind") == "cover" and image.get("url")
            ),
            None,
        )
        if cover:
            metadata["cover"] = cover
    return replace(
        primary,
        actors=tuple(dict.fromkeys((*primary.actors, *details_source.actors))),
        tags=tuple(dict.fromkeys((*primary.tags, *details_source.tags))),
        details=(
            _merge_source_details(primary.details, details_source.details)
            if details_source is fallback
            else primary.details
        ),
        magnet_hint="available" if magnets else magnet_source.magnet_hint,
        magnets=tuple(magnets),
        metadata=metadata,
    )


def _preferred_detail_stage(
    primary: SearchResult,
    fallback: SearchResult,
    *,
    resolved_key: str,
    error_key: str,
) -> SearchResult:
    primary_metadata = primary.metadata if isinstance(primary.metadata, dict) else {}
    fallback_metadata = fallback.metadata if isinstance(fallback.metadata, dict) else {}
    primary_ok = primary_metadata.get(
        resolved_key
    ) is True and not primary_metadata.get(error_key)
    fallback_ok = fallback_metadata.get(
        resolved_key
    ) is True and not fallback_metadata.get(error_key)
    if primary_ok or not fallback_ok:
        return primary
    return fallback


def _replace_metadata_stage(
    target: dict[str, object],
    source: dict[str, object],
    keys: tuple[str, ...],
) -> None:
    for key in keys:
        target.pop(key, None)
        if key in source:
            target[key] = source[key]


def _merged_stage_images(
    primary: dict[str, object],
    fallback: dict[str, object],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for metadata in (primary, fallback):
        candidates: list[object] = []
        cover = metadata.get("cover")
        if isinstance(cover, str) and cover.strip():
            candidates.append({"kind": "cover", "url": cover.strip()})
        raw_images = metadata.get("images")
        if isinstance(raw_images, (list, tuple)):
            candidates.extend(raw_images)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            kind = candidate.get("kind")
            url = candidate.get("url")
            if kind not in {"cover", "backdrop", "sample"} or not isinstance(url, str):
                continue
            clean_url = url.strip()
            if not clean_url or (kind, clean_url) in seen:
                continue
            seen.add((kind, clean_url))
            merged.append({**candidate, "kind": kind, "url": clean_url})
    return merged


def _merge_source_details(
    primary: SourceDetails,
    fallback: SourceDetails,
) -> SourceDetails:
    return SourceDetails(
        title=fallback.title or primary.title,
        original_title=fallback.original_title or primary.original_title,
        release_date=fallback.release_date or primary.release_date,
        duration_minutes=fallback.duration_minutes or primary.duration_minutes,
        duration_text=fallback.duration_text or primary.duration_text,
        rating=fallback.rating or primary.rating,
        makers=fallback.makers or primary.makers,
        publishers=fallback.publishers or primary.publishers,
        series=fallback.series or primary.series,
        directors=fallback.directors or primary.directors,
        actors=fallback.actors or primary.actors,
        tags=fallback.tags or primary.tags,
    )


def code_key(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())
