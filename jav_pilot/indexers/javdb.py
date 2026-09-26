from __future__ import annotations

import html as html_lib
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from jav_pilot.net.browser_fetcher import BrowserPageFetcher, BrowserWaitTimeout
from jav_pilot.core.guards import normalize_query
from jav_pilot.net.http_client import FetchError, fetch_text
from jav_pilot.core.models import (
    SearchBounds,
    SearchResult,
    SourceDetails,
    normalize_source_images,
)

from .base import Indexer
from .html_parsers import (
    JavDbSearchParser,
    extract_details_with_rules,
    extract_images_with_rules,
    extract_javdb_details_from_html,
    extract_javdb_images_from_html,
    extract_magnets_from_html,
    extract_magnets_with_rules,
    has_configured_empty_search_state,
    has_javdb_empty_search_state,
    merge_magnet_info,
    parse_search_results_with_rules,
    resolve_semantic_ref_url,
)


JAVDB_SEARCH_READY_SELECTOR = 'a.box[href*="/v/"]:visible, .empty-message:visible'
JAVDB_DETAIL_READY_SELECTOR = (
    ".column-video-cover, .movie-panel-info, .preview-images, "
    "[data-fancybox], .magnets-content, .video-detail"
)
JAVDB_CHALLENGE_WAIT_SECONDS = 12.0
JAVDB_BROWSER_SEARCH_ATTEMPTS = 2
_JAVDB_REGION_UNAVAILABLE_MESSAGE = (
    "JavDB is unavailable from the current network region"
)
_JAVDB_TEMPORARILY_BLOCKED_MESSAGE = (
    "JavDB temporarily blocked this client; retry after the upstream cooldown period"
)


class _Fetcher(Protocol):
    def fetch(self, url: str) -> str:
        raise NotImplementedError


class _JavDbDetailLoader:
    def __init__(
        self,
        indexer: JavDbIndexer,
        bounds: SearchBounds,
        browser_fetcher: _Fetcher | None,
    ) -> None:
        self._indexer = indexer
        self._bounds = bounds
        self._browser_fetcher = browser_fetcher
        self._owned_browser_manager: BrowserPageFetcher | None = None

    def fetch(self, url: str) -> tuple[str, bool]:
        if self._browser_fetcher is not None:
            return self._fetch_with_browser(url), True

        mode = _javdb_fetch_mode()
        if mode == "browser":
            return self._fetch_with_browser(url), True

        try:
            html = self._indexer._fetch_http(url, self._bounds)
        except FetchError as exc:
            if mode != "auto" or not _should_fallback_to_browser(exc):
                raise
            return self._fetch_with_browser(url), True

        has_detail_content = _looks_like_javdb_detail_content(html)
        if _looks_like_javdb_hard_block(html):
            raise FetchError(_javdb_block_message(html))
        if _looks_like_javdb_interstitial(html) and not has_detail_content:
            if mode != "auto":
                raise FetchError(_javdb_block_message(html))
            return self._fetch_with_browser(url), True
        return html, False

    def close(self) -> None:
        manager = self._owned_browser_manager
        if manager is None:
            return
        self._owned_browser_manager = None
        self._browser_fetcher = None
        manager.__exit__(None, None, None)

    def _fetch_with_browser(self, url: str) -> str:
        if self._browser_fetcher is None:
            manager = self._indexer._make_browser_fetcher(self._bounds)
            self._browser_fetcher = manager.__enter__()
            self._owned_browser_manager = manager

        html = self._browser_fetcher.fetch(url)
        has_detail_content = _looks_like_javdb_detail_content(html)
        if (
            not has_detail_content
            and not _looks_like_javdb_hard_block(html)
            and _looks_like_javdb_interstitial(html)
        ):
            html = self._indexer._wait_for_browser_detail(
                self._browser_fetcher,
                html,
                self._bounds,
            )
            has_detail_content = _looks_like_javdb_detail_content(html)

        if _looks_like_javdb_hard_block(html):
            raise FetchError(_javdb_block_message(html))
        if _looks_like_javdb_interstitial(html) and not has_detail_content:
            raise FetchError(_javdb_block_message(html))
        return html


class JavDbIndexer(Indexer):
    name = "javdb"

    def __init__(
        self,
        base_url: str = "https://javdb.com",
        *,
        search_template: str = "",
        default_filters: dict[str, str] | None = None,
        source_id: str = "",
        parser_rules: dict | None = None,
    ) -> None:
        self.base_url = (base_url or "https://javdb.com").rstrip("/")
        self.search_template = search_template.strip()
        self.default_filters = default_filters or {}
        self.source_id = source_id.strip() or self.name
        self.parser_rules = parser_rules

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        return self._search_with_detail_options(query, bounds, include_images=False)

    def diagnostic_detail_search(
        self,
        query: str,
        bounds: SearchBounds,
    ) -> tuple[SearchResult, ...]:
        return self._search_with_detail_options(
            query,
            replace(bounds, fetch_magnets=True),
            include_images=True,
        )

    def _search_with_detail_options(
        self,
        query: str,
        bounds: SearchBounds,
        *,
        include_images: bool,
    ) -> tuple[SearchResult, ...]:
        query = normalize_query(query)
        bounds = bounds.normalized()
        url = self._search_url(query, bounds)
        browser_fetcher = None
        try:
            html = self._fetch_search_html(url, bounds)
            if html is None:
                browser_fetcher = self._make_browser_fetcher(bounds).__enter__()
                html = browser_fetcher.fetch(url)
                html, results, has_empty_state = self._resolve_browser_search(
                    browser_fetcher,
                    url,
                    html,
                    bounds,
                )
            else:
                results, has_empty_state = self._parse_search_page(html, bounds)
            if (
                not results
                and not has_empty_state
                and browser_fetcher is None
                and _should_retry_empty_page_with_browser(html)
            ):
                browser_fetcher = self._make_browser_fetcher(bounds).__enter__()
                html = browser_fetcher.fetch(url)
                html, results, has_empty_state = self._resolve_browser_search(
                    browser_fetcher,
                    url,
                    html,
                    bounds,
                )

            if _looks_like_javdb_hard_block(html):
                raise FetchError(_javdb_block_message(html))
            if (
                not results
                and not has_empty_state
                and _looks_like_javdb_interstitial(html)
            ):
                raise FetchError(_javdb_block_message(html))

            if not bounds.fetch_magnets:
                return results
            return self._enrich_magnets(
                results,
                bounds,
                browser_fetcher,
                include_images=include_images,
            )
        finally:
            if browser_fetcher:
                browser_fetcher.__exit__(None, None, None)

    def _enrich_magnets(
        self,
        results: tuple[SearchResult, ...],
        bounds: SearchBounds,
        browser_fetcher: _Fetcher | None = None,
        *,
        include_images: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[SearchResult, ...]:
        detail_loader = _JavDbDetailLoader(self, bounds, browser_fetcher)
        try:
            return self._enrich_magnets_with_loader(
                results,
                bounds,
                detail_loader,
                include_images=include_images,
                cancelled=cancelled,
            )
        finally:
            detail_loader.close()

    def _enrich_magnets_with_loader(
        self,
        results: tuple[SearchResult, ...],
        bounds: SearchBounds,
        detail_loader: _JavDbDetailLoader,
        *,
        include_images: bool,
        cancelled: Callable[[], bool] | None,
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
                detail_html, detail_used_browser = detail_loader.fetch(result.url)
                has_detail_content = _looks_like_javdb_detail_content(detail_html)
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
            details = result.details
            magnets = result.magnets
            images = ()
            details_error: str | None = None
            magnet_error: str | None = None
            image_error: str | None = None

            if cancelled is not None and cancelled():
                enriched.append(result)
                continue

            try:
                parsed_details = extract_javdb_details_from_html(
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
                    details_error = "JavDB detail page did not contain expected details"
                else:
                    details = _resolved_details(
                        result.details,
                        parsed_details,
                    )
            except Exception as exc:  # noqa: BLE001 - one parser must not discard other detail data.
                details_error = str(exc)

            if cancelled is not None and cancelled():
                enriched.append(result)
                continue
            if result.magnet_hint != "unavailable":
                try:
                    parsed_magnets = extract_magnets_from_html(detail_html)
                    if self.parser_rules:
                        parsed_magnets = _dedupe_magnets(
                            [
                                *extract_magnets_with_rules(
                                    detail_html,
                                    rules=self.parser_rules["detail"]["magnets"],
                                ),
                                *parsed_magnets,
                            ]
                        )
                    if not parsed_magnets and (
                        result.magnets
                    ):
                        magnet_error = "JavDB detail page did not contain previously expected magnets"
                    else:
                        magnets = parsed_magnets
                except Exception as exc:  # noqa: BLE001 - preserve metadata and images on magnet parse failure.
                    magnet_error = str(exc)
            else:
                magnets = ()

            if include_images:
                if cancelled is not None and cancelled():
                    enriched.append(result)
                    continue
                try:
                    images = extract_javdb_images_from_html(detail_html, self.base_url)
                    if self.parser_rules:
                        configured_images = extract_images_with_rules(
                            detail_html,
                            self.base_url,
                            profile="javdb",
                            rules=self.parser_rules["detail"]["images"],
                        )
                        images = normalize_source_images((*configured_images, *images))
                except Exception as exc:  # noqa: BLE001 - preserve metadata and magnets on image parse failure.
                    image_error = str(exc)
                if not images and not image_error:
                    image_error = "JavDB detail page did not contain expected images"

            if cancelled is not None and cancelled():
                enriched.append(result)
                continue

            if (
                not magnets
                and not images
                and not has_detail_content
                and details == SourceDetails()
            ):
                message = "JavDB detail page did not contain expected content"
                details_error = details_error or message
                magnet_error = magnet_error or message
                if include_images:
                    image_error = image_error or message

            metadata.update(
                {
                    "magnet_checked": True,
                    "magnet_count": len(magnets),
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
            _set_optional_error(metadata, "details_error", details_error)
            _set_optional_error(metadata, "magnet_error", magnet_error)
            if include_images:
                _set_optional_error(metadata, "images_error", image_error)
            if detail_used_browser:
                metadata["fetcher"] = "browser"

            magnet_hint = result.magnet_hint
            if not magnet_error:
                magnet_hint = "available" if magnets else "unavailable"
            enriched.append(
                replace(
                    result,
                    actors=_merge_labels(result.actors, details.actors),
                    tags=_merge_labels(result.tags, details.tags),
                    details=details,
                    magnet_hint=magnet_hint,
                    magnets=magnets,
                    metadata=metadata,
                )
            )

        return tuple(enriched)

    def _fetch_search_html(self, url: str, bounds: SearchBounds) -> str | None:
        mode = _javdb_fetch_mode()
        if mode == "browser":
            return None

        try:
            return self._fetch_http(url, bounds)
        except FetchError as exc:
            if mode == "http" or not _should_fallback_to_browser(exc):
                raise
            return None

    def _fetch_http(self, url: str, bounds: SearchBounds) -> str:
        headers: dict[str, str] = {"Referer": self.base_url + "/"}
        cookie = os.environ.get("JAV_PILOT_JAVDB_COOKIE")
        if cookie:
            headers["Cookie"] = cookie
        return fetch_text(
            url,
            timeout=bounds.timeout_seconds,
            max_bytes=bounds.max_response_bytes,
            headers=headers,
            allowed_origin=self.base_url,
        )

    def _make_browser_fetcher(self, bounds: SearchBounds) -> BrowserPageFetcher:
        return BrowserPageFetcher(bounds)

    def _wait_for_browser_search(
        self,
        browser_fetcher: _Fetcher,
        current_html: str,
        bounds: SearchBounds,
    ) -> str:
        wait_for_selector = getattr(browser_fetcher, "wait_for_selector", None)
        if not callable(wait_for_selector):
            return current_html
        try:
            return wait_for_selector(
                (
                    str(self.parser_rules["search"].get("ready_selector") or "")
                    if self.parser_rules
                    else JAVDB_SEARCH_READY_SELECTOR
                )
                or JAVDB_SEARCH_READY_SELECTOR,
                timeout_seconds=min(
                    JAVDB_CHALLENGE_WAIT_SECONDS, bounds.timeout_seconds
                ),
            )
        except BrowserWaitTimeout:
            return current_html

    def _resolve_browser_search(
        self,
        browser_fetcher: _Fetcher,
        url: str,
        current_html: str,
        bounds: SearchBounds,
    ) -> tuple[str, tuple[SearchResult, ...], bool]:
        html = current_html
        for attempt in range(JAVDB_BROWSER_SEARCH_ATTEMPTS):
            if _looks_like_javdb_hard_block(html):
                results, has_empty_state = self._parse_search_page(html, bounds)
                return html, results, has_empty_state

            html, custom_ready_waited = self._wait_for_custom_browser_search(
                browser_fetcher,
                html,
                bounds,
            )
            results, has_empty_state = self._parse_search_page(html, bounds)
            if results or has_empty_state or _looks_like_javdb_hard_block(html):
                return html, results, has_empty_state

            if not custom_ready_waited and _looks_like_javdb_challenge(html):
                html = self._wait_for_browser_search(browser_fetcher, html, bounds)
                results, has_empty_state = self._parse_search_page(html, bounds)
                if results or has_empty_state or _looks_like_javdb_hard_block(html):
                    return html, results, has_empty_state

            if (
                attempt + 1 >= JAVDB_BROWSER_SEARCH_ATTEMPTS
                or not _looks_like_javdb_challenge(html)
            ):
                return html, results, has_empty_state
            html = browser_fetcher.fetch(url)

        return html, results, has_empty_state

    def _wait_for_browser_detail(
        self,
        browser_fetcher: _Fetcher,
        current_html: str,
        bounds: SearchBounds,
    ) -> str:
        wait_for_selector = getattr(browser_fetcher, "wait_for_selector", None)
        if not callable(wait_for_selector):
            return current_html
        try:
            return wait_for_selector(
                JAVDB_DETAIL_READY_SELECTOR,
                timeout_seconds=min(
                    JAVDB_CHALLENGE_WAIT_SECONDS, bounds.timeout_seconds
                ),
            )
        except BrowserWaitTimeout:
            return current_html

    def _wait_for_custom_browser_search(
        self,
        browser_fetcher: _Fetcher,
        current_html: str,
        bounds: SearchBounds,
    ) -> tuple[str, bool]:
        if not self._has_custom_browser_ready_selector():
            return current_html, False
        return (
            self._wait_for_browser_search(browser_fetcher, current_html, bounds),
            True,
        )

    def _has_custom_browser_ready_selector(self) -> bool:
        return bool(
            self.parser_rules
            and str(self.parser_rules["search"].get("ready_selector") or "").strip()
        )

    def _parse_search_page(
        self,
        html: str,
        bounds: SearchBounds,
    ) -> tuple[tuple[SearchResult, ...], bool]:
        if self.parser_rules:
            results = parse_search_results_with_rules(
                html,
                self.base_url,
                source_id=self.source_id,
                profile="javdb",
                rules=self.parser_rules["search"],
                limit=bounds.limit,
            )
            empty_selector = str(
                self.parser_rules["search"].get("empty_selector") or ""
            )
            has_configured_empty = has_configured_empty_search_state(
                html, empty_selector
            )
            if results or has_configured_empty:
                return results, has_configured_empty
        parser = JavDbSearchParser(self.base_url, bounds.limit)
        parser.feed(html)
        results = tuple(
            replace(result, source=self.source_id) for result in parser.results()
        )
        return results, has_javdb_empty_search_state(html)

    def _search_url(self, query: str, bounds: SearchBounds) -> str:
        semantic_ref = bounds.semantic_refs.get(self.source_id, "")
        semantic_url = resolve_semantic_ref_url(
            self.base_url,
            semantic_ref,
            profile="javdb",
            kind=bounds.search_kind,
        )
        if semantic_url:
            return _semantic_page_url(semantic_url, bounds.page)
        filters = {"f": "all", **self.default_filters, **bounds.filters}
        semantic_filter = {
            "actor": "actor",
            "series": "series",
            "maker": "maker",
            "director": "director",
            "tag": "all",
            "publisher": "all",
        }.get(bounds.search_kind)
        if semantic_filter:
            filters["f"] = semantic_filter
        sb = str(filters.get("sb", "")).strip()
        sb_part = f"&sb={quote(sb, safe='')}" if sb else ""
        encoded_query = quote(query, safe="")
        values = _TemplateValues(
            {
                "base_url": self.base_url,
                "query": encoded_query,
                "query_raw": encoded_query,
                "page": str(bounds.page),
                "sb_part": sb_part,
                **{key: quote(str(value), safe="") for key, value in filters.items()},
            }
        )
        template = (
            self.search_template
            or "{base_url}/search?q={query}&f={f}&page={page}{sb_part}"
        )
        try:
            return template.format_map(values)
        except (KeyError, ValueError):
            return (
                f"{self.base_url}/search?q={encoded_query}"
                f"&f={quote(str(filters.get('f', 'all')), safe='')}"
                f"&page={bounds.page}{sb_part}"
            )


def _should_fallback_to_browser(exc: FetchError) -> bool:
    text = str(exc).lower()
    return "403" in text or "cloudflare" in text or "forbidden" in text


def _javdb_fetch_mode() -> str:
    mode = os.environ.get("JAV_PILOT_JAVDB_FETCHER", "auto").strip().lower()
    return mode if mode in {"auto", "browser", "http"} else "auto"


def _should_retry_empty_page_with_browser(page: str) -> bool:
    mode = _javdb_fetch_mode()
    return (
        mode == "auto"
        and not _looks_like_javdb_hard_block(page)
        and _looks_like_javdb_interstitial(page)
    )


def _looks_like_javdb_block_page(page: str) -> bool:
    return _looks_like_javdb_hard_block(page) or _looks_like_javdb_challenge(page)


def _looks_like_javdb_hard_block(page: str) -> bool:
    return _looks_like_javdb_region_block(page) or _looks_like_javdb_temporary_block(
        page
    )


def _looks_like_javdb_region_block(page: str) -> bool:
    lowered = html_lib.unescape(page or "").casefold()
    return any(
        marker in lowered
        for marker in (
            "due to copyright restrictions",
            "禁止了你的網路所在",
            "禁止了你的网络所在",
        )
    )


def _looks_like_javdb_temporary_block(page: str) -> bool:
    lowered = html_lib.unescape(page or "").casefold()
    english_ip_ban = (
        "the owner of this website has banned your access "
        "based on your browser's behaving ip"
    ) in lowered
    chinese_temporary_ban = (
        "管理員禁止了你的訪問" in lowered and "3-7日後解除" in lowered
    )
    return english_ip_ban or chinese_temporary_ban


def _looks_like_javdb_challenge(page: str) -> bool:
    lowered = (page or "").lower()
    return any(
        marker in lowered
        for marker in (
            "cf-chl",
            "checking your browser",
            "just a moment",
        )
    )


def _looks_like_javdb_interstitial(page: str) -> bool:
    lowered = (page or "").lower()
    return _looks_like_javdb_block_page(page) or any(
        marker in lowered
        for marker in (
            "age verification",
            "年齡為18歲以上",
            "年龄为18岁以上",
            "over 18",
        )
    )


def _looks_like_javdb_detail_content(page: str) -> bool:
    lowered = (page or "").lower()
    return any(
        marker in lowered
        for marker in (
            "column-video-cover",
            "magnets-content",
            "movie-panel-info",
            "preview-images",
            "video-detail",
            "data-fancybox",
            "magnet:?",
        )
    )


def _javdb_block_message(page: str) -> str:
    if _looks_like_javdb_region_block(page):
        return _JAVDB_REGION_UNAVAILABLE_MESSAGE
    if _looks_like_javdb_temporary_block(page):
        return _JAVDB_TEMPORARILY_BLOCKED_MESSAGE
    return "JavDB returned an anti-bot or verification page"


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _semantic_page_url(url: str, page: int) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "page"
    ]
    query.append(("page", str(page)))
    return urlunsplit(parsed._replace(query=urlencode(query)))


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


def _set_optional_error(
    metadata: dict[str, object], key: str, value: str | None
) -> None:
    if value:
        metadata[key] = value
    else:
        metadata.pop(key, None)


def _dedupe_magnets(magnets: list) -> tuple:
    deduped = {}
    for magnet in magnets:
        deduped[magnet.info_hash] = merge_magnet_info(
            deduped.get(magnet.info_hash), magnet
        )
    return tuple(deduped.values())


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
