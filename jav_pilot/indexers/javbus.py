from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import quote, urlsplit, urlunsplit

from jav_pilot.core.guards import normalize_query
from jav_pilot.net.http_client import FetchError, fetch_text
from jav_pilot.core.models import (
    MagnetInfo,
    SearchBounds,
    SearchResult,
    SourceDetails,
    normalize_source_images,
)

from .base import Indexer
from .html_parsers import (
    JavBusSearchParser,
    extract_details_with_rules,
    extract_images_with_rules,
    extract_javbus_details_from_html,
    extract_javbus_images_from_html,
    extract_javbus_magnet_endpoint,
    extract_magnets_from_html,
    extract_magnets_with_rules,
    has_configured_empty_search_state,
    looks_like_javbus_age_verification,
    merge_magnet_info,
    parse_search_results_with_rules,
    resolve_semantic_ref_url,
)


class JavBusIndexer(Indexer):
    name = "javbus"

    def __init__(
        self,
        base_url: str = "https://www.javbus.com",
        *,
        search_template: str = "",
        default_filters: dict[str, str] | None = None,
        source_id: str = "",
        parser_rules: dict | None = None,
    ) -> None:
        self.base_url = (base_url or "https://www.javbus.com").rstrip("/")
        self.search_template = search_template.strip()
        self.default_filters = default_filters or {}
        self.source_id = source_id.strip() or self.name
        self.parser_rules = parser_rules

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query = normalize_query(query)
        bounds = bounds.normalized()
        url = self._search_url(query, bounds)
        try:
            html = self._fetch(url, bounds)
        except FetchError as exc:
            # JavBus answers a search without matches (or past the last
            # page) with HTTP 404; that is an empty result, not an outage.
            if _is_missing_page(exc):
                return ()
            raise
        if looks_like_javbus_age_verification(html):
            raise FetchError(
                "JavBus returned age verification page; set JAV_PILOT_JAVBUS_COOKIE from a verified browser session"
            )
        if self.parser_rules:
            results = parse_search_results_with_rules(
                html,
                self.base_url,
                source_id=self.source_id,
                profile="javbus",
                rules=self.parser_rules["search"],
                limit=bounds.limit,
            )
        else:
            results = ()
        if self.parser_rules and not results:
            empty_selector = str(
                self.parser_rules["search"].get("empty_selector") or ""
            )
            if has_configured_empty_search_state(html, empty_selector):
                return ()
        if not results:
            parser = JavBusSearchParser(self.base_url, bounds.limit)
            parser.feed(html)
            results = tuple(
                replace(result, source=self.source_id) for result in parser.results()
            )
        if not bounds.fetch_magnets:
            return results
        return self._enrich_magnets(results, bounds, include_images=False)

    def diagnostic_detail_search(
        self,
        query: str,
        bounds: SearchBounds,
    ) -> tuple[SearchResult, ...]:
        bounds = bounds.normalized()
        results = self.search(query, replace(bounds, fetch_magnets=False))
        return self._enrich_magnets(
            results,
            replace(bounds, fetch_magnets=True),
            include_images=True,
        )

    def _enrich_magnets(
        self,
        results: tuple[SearchResult, ...],
        bounds: SearchBounds,
        *,
        include_images: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[SearchResult, ...]:
        enriched: list[SearchResult] = []
        detail_budget = bounds.detail_limit

        for result in results:
            if cancelled is not None and cancelled():
                enriched.append(result)
                continue
            if not include_images and result.magnet_hint == "unavailable":
                metadata = dict(result.metadata)
                metadata["magnet_skipped"] = "search_card_unavailable"
                enriched.append(replace(result, metadata=metadata))
                continue
            if detail_budget <= 0 or not result.url:
                enriched.append(result)
                continue

            detail_budget -= 1
            try:
                detail_html = self._fetch(result.url, bounds)
                if looks_like_javbus_age_verification(detail_html):
                    raise FetchError("detail page returned age verification")
                if not self.parser_rules and not _looks_like_javbus_detail_content(
                    detail_html
                ):
                    raise FetchError("detail page did not contain expected content")
            except Exception as exc:  # noqa: BLE001 - per-result enrichment should not fail whole search.
                if cancelled is not None and cancelled():
                    enriched.append(result)
                    continue
                metadata = dict(result.metadata)
                metadata.update(
                    {
                        "source_error": str(exc),
                        "magnet_checked": True,
                        "magnet_error": str(exc),
                        "details_resolved": True,
                        "details_error": str(exc),
                    }
                )
                if include_images:
                    metadata.update({"images_resolved": True, "images_error": str(exc)})
                enriched.append(replace(result, metadata=metadata))
                continue

            if cancelled is not None and cancelled():
                enriched.append(result)
                continue

            metadata = dict(result.metadata)
            metadata.pop("source_error", None)
            magnets: list[MagnetInfo] = []
            magnet_error: str | None = None
            image_error: str | None = None
            details_error: str | None = None

            details = result.details
            try:
                parsed_details = extract_javbus_details_from_html(
                    detail_html, self.base_url
                )
                if self.parser_rules:
                    parsed_details = _resolved_details(
                        parsed_details,
                        extract_details_with_rules(
                            detail_html,
                            self.base_url,
                            rules=self.parser_rules["detail"],
                        ),
                    )
                if parsed_details == SourceDetails():
                    details_error = (
                        "JavBus detail page did not contain expected details"
                    )
                else:
                    details = _resolved_details(
                        result.details,
                        parsed_details,
                    )
            except Exception as exc:  # noqa: BLE001 - related metadata must not discard media data.
                details_error = str(exc)

            images = ()
            if cancelled is not None and cancelled():
                enriched.append(result)
                continue
            if include_images:
                try:
                    images = extract_javbus_images_from_html(detail_html, self.base_url)
                    if self.parser_rules:
                        configured_images = extract_images_with_rules(
                            detail_html,
                            self.base_url,
                            profile="javbus",
                            rules=self.parser_rules["detail"]["images"],
                        )
                        images = normalize_source_images((*configured_images, *images))
                except Exception as exc:  # noqa: BLE001 - image metadata must not discard magnets.
                    image_error = str(exc)
                if not images and not image_error:
                    image_error = "JavBus detail page did not contain expected images"

            if result.magnet_hint != "unavailable":
                if cancelled is not None and cancelled():
                    enriched.append(result)
                    continue
                try:
                    configured_magnets: list[MagnetInfo] = []
                    inferred_magnets: list[MagnetInfo] = []
                    if self.parser_rules:
                        configured_magnets.extend(
                            extract_magnets_with_rules(
                                detail_html,
                                rules=self.parser_rules["detail"]["magnets"],
                            )
                        )
                    inferred_magnets.extend(extract_magnets_from_html(detail_html))
                    endpoint = extract_javbus_magnet_endpoint(detail_html)
                    if endpoint:
                        if cancelled is not None and cancelled():
                            enriched.append(result)
                            continue
                        ajax_html = self._fetch(endpoint.to_url(self.base_url), bounds)
                        if cancelled is not None and cancelled():
                            enriched.append(result)
                            continue
                        if self.parser_rules:
                            configured_magnets.extend(
                                extract_magnets_with_rules(
                                    ajax_html,
                                    rules=self.parser_rules["detail"]["magnets"],
                                )
                            )
                        inferred_magnets.extend(extract_magnets_from_html(ajax_html))
                        metadata["magnet_endpoint"] = "javbus_ajax"
                    magnets.extend(configured_magnets)
                    magnets.extend(inferred_magnets)
                except Exception as exc:  # noqa: BLE001 - keep images and any detail-page magnets.
                    magnets.extend(configured_magnets)
                    magnets.extend(inferred_magnets)
                    magnet_error = str(exc)
                if (
                    not magnets
                    and not magnet_error
                    and result.magnets
                ):
                    magnet_error = (
                        "JavBus detail page did not contain previously expected magnets"
                    )

            if cancelled is not None and cancelled():
                enriched.append(result)
                continue

            deduped = _dedupe_magnets(
                [*result.magnets, *magnets] if magnet_error else magnets
            )
            metadata.update(
                {
                    "magnet_checked": True,
                    "magnet_count": len(deduped),
                    "details_resolved": True,
                }
            )
            if include_images:
                metadata["images_resolved"] = True
                if not image_error:
                    metadata["images"] = [image.to_dict() for image in images]
                    cover = next(
                        (image for image in images if image.kind == "cover"), None
                    )
                    if cover is not None:
                        metadata["cover"] = cover.url
            if magnet_error:
                metadata["magnet_error"] = magnet_error
            else:
                metadata.pop("magnet_error", None)
            if details_error:
                metadata["details_error"] = details_error
            else:
                metadata.pop("details_error", None)
            if include_images:
                if image_error:
                    metadata["images_error"] = image_error
                else:
                    metadata.pop("images_error", None)
            magnet_hint = result.magnet_hint
            if not magnet_error:
                magnet_hint = "available" if deduped else "unavailable"
            enriched.append(
                replace(
                    result,
                    actors=_merge_labels(result.actors, details.actors),
                    tags=_merge_labels(result.tags, details.tags),
                    details=details,
                    magnet_hint=magnet_hint,
                    magnets=deduped,
                    metadata=metadata,
                )
            )

        return tuple(enriched)

    def _fetch(self, url: str, bounds: SearchBounds) -> str:
        headers: dict[str, str] = {"Referer": self.base_url + "/"}
        cookie = os.environ.get("JAV_PILOT_JAVBUS_COOKIE")
        if cookie:
            headers["Cookie"] = cookie
        return fetch_text(
            url,
            timeout=bounds.timeout_seconds,
            max_bytes=bounds.max_response_bytes,
            headers=headers,
            allowed_origin=self.base_url,
        )

    def _search_url(self, query: str, bounds: SearchBounds) -> str:
        semantic_ref = bounds.semantic_refs.get(self.source_id, "")
        semantic_url = resolve_semantic_ref_url(
            self.base_url,
            semantic_ref,
            profile="javbus",
            kind=bounds.search_kind,
        )
        if semantic_url:
            return _semantic_page_url(semantic_url, bounds.page)
        page_path = f"/{bounds.page}" if bounds.page > 1 else ""
        filters = {"parent": "ce", **self.default_filters, **bounds.filters}
        encoded_query = quote(query, safe="")
        values = _TemplateValues(
            {
                "base_url": self.base_url,
                "query": encoded_query,
                "query_raw": encoded_query,
                "page": str(bounds.page),
                "page_path": page_path,
                **{key: quote(str(value), safe="") for key, value in filters.items()},
            }
        )
        template = (
            self.search_template
            or "{base_url}/search/{query}{page_path}?type=&parent={parent}"
        )
        try:
            return template.format_map(values)
        except (KeyError, ValueError):
            return (
                f"{self.base_url}/search/{encoded_query}{page_path}?type=&parent="
                f"{quote(filters.get('parent', 'ce'), safe='')}"
            )


def _dedupe_magnets(magnets: list) -> tuple:
    deduped: dict[str, MagnetInfo] = {}
    for magnet in magnets:
        deduped[magnet.info_hash] = merge_magnet_info(
            deduped.get(magnet.info_hash), magnet
        )
    return tuple(deduped.values())


def _is_missing_page(exc: FetchError) -> bool:
    return str(exc).strip().lower() == "http 404"


def _looks_like_javbus_detail_content(page: str) -> bool:
    lowered = (page or "").lower()
    return any(
        marker in lowered
        for marker in (
            'class="bigimage',
            "class='bigimage",
            'class="info',
            "class='info",
            'class="sample-box',
            "class='sample-box",
            'class="video-cover',
            "class='video-cover",
            'class="star-name',
            "class='star-name",
            "uncledatoolsbyajax",
            "var gid",
        )
    )


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _semantic_page_url(url: str, page: int) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    if page > 1:
        path = f"{path}/{page}"
    return urlunsplit(parsed._replace(path=path))


def _merge_labels(existing: tuple[str, ...], related: tuple) -> tuple[str, ...]:
    output = list(existing)
    seen = {value.casefold() for value in output}
    for item in related:
        label = str(item.label or "").strip()
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            output.append(label)
    return tuple(output)


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
