from __future__ import annotations

import html
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Sequence
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..core.guards import contains_sensitive_transport_text
from ..net.network_guard import PublicHostResolver
from ..search.resources.errors import (
    ResourceSearchCancelledError,
    ResourceSearchUnavailableError,
)
from ..search.resources.models import (
    ResourceSearchHeartbeatEvent,
    ResourceSearchItem,
    ResourceSearchPageEvent,
    ResourceSearchStartedEvent,
    ResourceSearchState,
    ResourceSearchWork,
    ResourceSearchWorkerEvent,
    ResourceSearchWorkerResult,
)
from ..search.resources.protocol import ResourceSearchWorkerError
from ..config.settings import (
    BUILTIN_WEB_DOWNLOAD_SITE_IDS,
    JABLE_SITE_ID,
    MISSAV_SITE_ID,
    RESOURCE_SEARCH_CAPABILITY,
    WEB_DOWNLOAD_CAPABILITY,
    load_settings,
    site_by_id,
    site_has_capability,
)
from .media import MAX_MANIFEST_BYTES
from ..net.http_client import FetchError
from ..net.site_fetch import fetch_site_text
from .variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
    normalize_variant_priority,
    normalize_web_download_variant,
    web_download_variant_suffix,
)


_MAX_PAGE_BYTES = 4 * 1024 * 1024
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RESOURCE_DELTA_ITEMS = 64
_MAX_RESOURCE_PAGE_ITEMS = 128
_RESOURCE_SEARCH_CARD_SELECTORS = {
    JABLE_SITE_ID: "div.video-img-box",
    MISSAV_SITE_ID: "div.thumbnail",
}
_RESOURCE_SEARCH_EMPTY_SELECTOR = (
    ".no-results, .no-result, .nothing-found, .empty-results, "
    ".search-empty, body.search-no-results"
)
_RESOURCE_SEARCH_EMPTY_TEXT_RE = re.compile(
    r"(?:\bno\s+(?:matching\s+|videos?\s+)?results?\b|"
    r"\bnothing\s+found\b|\b0\s+results?\b|"
    r"(?:没有|沒有|未|找不到)(?:匹配的?)?(?:影片|视频|視頻|结果|結果))",
    re.IGNORECASE,
)
_RESOURCE_SEARCH_CHALLENGE_RE = re.compile(
    r"(?:cf-chl-|challenge-platform|cf-browser-verification|"
    r"just\s+a\s+moment|verify\s+(?:that\s+)?you\s+are\s+human|"
    r"attention\s+required|captcha)",
    re.IGNORECASE,
)


class WebDownloadProviderError(RuntimeError):
    retryable = True

    def __init__(self, message: str, *, code: str = "upstream_unavailable") -> None:
        self.code = code
        super().__init__(message)


class WebDownloadProviderNotFound(WebDownloadProviderError):
    retryable = False

    def __init__(self, message: str = "No exact Web source was found") -> None:
        super().__init__(message, code="not_found")


class _ResourceSearchPageError(ValueError):
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__("Web resource search page is not a valid result document")


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    provider: str
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    page_url: str = field(repr=False)
    selected_height: int | None = None
    media_kind: str = "hls"
    allowed_media_hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderSearchPage:
    provider: str
    origin: str
    query: str
    document: str = field(repr=False)




class PriorityWebDownloadProvider:
    """Resolve one task through enabled providers in the configured order."""

    def __init__(
        self,
        missav_provider: object,
        *,
        settings_loader: Callable[[], dict[str, object]] = load_settings,
    ) -> None:
        self._missav_provider = missav_provider
        self._settings_loader = settings_loader

    def capture_manifest(
        self,
        code: object,
        *,
        provider: object = "auto",
        variant: object,
        timeout_seconds: float,
        requested_height: object | None,
        quality_strategy: object,
        cancel_event: threading.Event | None = None,
    ) -> object:
        settings = self._settings_loader()
        sites = {
            str(site.get("id")): site
            for site in settings.get("sites", [])
            if isinstance(site, dict)
        }
        requested_provider = str(provider or "auto").strip().lower()
        if requested_provider == "auto":
            raw_priority = settings.get("web_download_provider_priority", [])
            priority = (
                [str(item) for item in raw_priority]
                if isinstance(raw_priority, list)
                else []
            )
            if not priority:
                priority = list(BUILTIN_WEB_DOWNLOAD_SITE_IDS)
        elif requested_provider in BUILTIN_WEB_DOWNLOAD_SITE_IDS:
            priority = [requested_provider]
        else:
            raise WebDownloadProviderError(
                "Web download provider is invalid", code="configuration"
            )

        enabled: list[tuple[str, dict[str, object]]] = []
        for provider_id in priority:
            site = sites.get(provider_id)
            if (
                site is not None
                and site.get("enabled")
                and site_has_capability(site, WEB_DOWNLOAD_CAPABILITY)
            ):
                enabled.append((provider_id, site))
        if not enabled:
            raise WebDownloadProviderError(
                "No Web download provider is enabled", code="configuration"
            )

        failures: list[WebDownloadProviderError] = []
        for provider_id, site in enabled:
            if cancel_event is not None and cancel_event.is_set():
                raise WebDownloadProviderError(
                    "Web source discovery was cancelled", code="cancelled"
                )
            try:
                if provider_id == MISSAV_SITE_ID:
                    try:
                        return _capture_missav(
                            code,
                            str(site.get("base_url") or ""),
                            variant=variant,
                            timeout_seconds=timeout_seconds,
                            requested_height=requested_height,
                            quality_strategy=quality_strategy,
                            cancel_event=cancel_event,
                        )
                    except WebDownloadProviderNotFound:
                        raise
                    except WebDownloadProviderError as direct_error:
                        if direct_error.code in {"configuration", "host_policy"}:
                            raise
                    result = self._missav_provider.capture_manifest(
                        code,
                        variant=variant,
                        timeout_seconds=timeout_seconds,
                        requested_height=requested_height,
                        quality_strategy=quality_strategy,
                        cancel_event=cancel_event,
                    )
                    return ProviderManifest(
                        provider=provider_id,
                        url=str(getattr(result, "url")),
                        headers=dict(getattr(result, "headers")),
                        page_url=str(
                            getattr(result, "page_url", "https://missav.invalid/")
                        ),
                        selected_height=getattr(result, "selected_height", None),
                    )
                if provider_id == "av123":
                    return _capture_av123(code, str(site.get("base_url") or ""), variant=str(variant),
                        timeout_seconds=timeout_seconds, requested_height=requested_height,
                        quality_strategy=quality_strategy, cancel_event=cancel_event)
                if str(variant) != "original" and provider_id != JABLE_SITE_ID:
                    raise WebDownloadProviderNotFound()
                if provider_id == JABLE_SITE_ID:
                    return _capture_jable(
                        code,
                        str(site.get("base_url") or ""),
                        variant=str(variant),
                        timeout_seconds=timeout_seconds,
                        requested_height=requested_height,
                        quality_strategy=quality_strategy,
                    )
            except WebDownloadProviderError as exc:
                failures.append(exc)
            except Exception as exc:  # sanitized at this provider boundary
                code_value = str(getattr(exc, "code", "") or "upstream_unavailable")
                failures.append(
                    WebDownloadProviderError(
                        f"{_provider_label(provider_id)} source discovery failed",
                        code=code_value,
                    )
                )

        if failures and all(item.code == "not_found" for item in failures):
            raise WebDownloadProviderNotFound()
        raise WebDownloadProviderError(
            "All configured Web download providers are currently unavailable",
            code="all_providers_unavailable",
        )


def probe_web_download_provider_connection(
    provider: object,
    code: object,
    base_url: object,
    *,
    timeout_seconds: float,
) -> ProviderSearchPage:
    """Probe a Web provider with the same transport fingerprint as downloads."""

    provider_id = str(provider or "").strip().lower()
    origin = _safe_origin(str(base_url or ""), expected_profile=provider_id)
    query = str(code or "").strip()
    if canonical_catalog_code(query) is None:
        raise WebDownloadProviderError(
            "Web provider diagnostic code is invalid", code="configuration"
        )
    host = urlsplit(origin).hostname or ""

    def fetch() -> str:
        return _bounded_page(
            _session(impersonate="chrome"),
            _resource_search_url(provider_id, origin, query, 1),
            timeout_seconds,
            allowed_hosts={host},
        )

    try:
        document = (
            fetch()
        )
    except WebDownloadProviderNotFound as exc:
        # Resource-search endpoints must return a structured result document,
        # including for zero matches.  A 404 therefore describes endpoint or
        # configuration drift, not an absent catalog code.
        raise WebDownloadProviderError(
            "Web provider search endpoint is invalid", code="response_invalid"
        ) from exc
    except WebDownloadProviderError:
        raise
    except Exception as exc:
        error_code = (
            "timeout"
            if "timeout" in f"{type(exc).__name__} {exc}".casefold()
            else "upstream_unavailable"
        )
        raise WebDownloadProviderError(
            "Web provider connection probe failed", code=error_code
        ) from exc
    return ProviderSearchPage(provider_id, origin, query, document)


def probe_web_download_provider_search(
    page: ProviderSearchPage,
) -> None:
    """Validate a captured resource-search page without another request."""

    if not isinstance(page, ProviderSearchPage):
        raise WebDownloadProviderError(
            "Web provider search page is invalid", code="configuration"
        )
    provider_id = page.provider
    query = page.query
    wanted = canonical_catalog_code(query)
    if wanted is None:
        raise WebDownloadProviderError(
            "Web provider diagnostic code is invalid", code="configuration"
        )
    try:
        work = ResourceSearchWork(
            session_id="d" * 32,
            source_id=provider_id,
            query=query,
            result_limit=10,
            suffix_width=None,
            start=None,
            end=None,
            state=ResourceSearchState(1, (), 0, None, 0),
        )
        items, _ = _parse_resource_search_page(
            provider_id,
            page.document,
            page.origin,
            work,
            1,
        )
    except _ResourceSearchPageError as exc:
        raise WebDownloadProviderError(
            "Web provider search page is invalid",
            code="challenge_active" if exc.retryable else "response_invalid",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise WebDownloadProviderError(
            "Web provider search page is invalid", code="response_invalid"
        ) from exc
    except WebDownloadProviderError:
        raise
    except Exception as exc:
        error_code = (
            "timeout"
            if "timeout" in f"{type(exc).__name__} {exc}".casefold()
            else "upstream_unavailable"
        )
        raise WebDownloadProviderError(
            "Web provider search probe failed", code=error_code
        ) from exc
    if not any(canonical_catalog_code(item.code) == wanted for item in items):
        raise WebDownloadProviderNotFound()


def probe_jable_current_search(
    base_url: str,
    *,
    timeout_seconds: float,
) -> ProviderSearchPage:
    """Select a current catalog sample after a diagnostic title disappears."""

    origin = _safe_origin(base_url, expected_profile=JABLE_SITE_ID)
    document = _bounded_page(
        _session(),
        f"{origin}/",
        timeout_seconds,
        allowed_hosts={urlsplit(origin).hostname or ""},
    )
    work = ResourceSearchWork(
        session_id="d" * 32,
        source_id=JABLE_SITE_ID,
        query="recent",
        result_limit=1,
        suffix_width=None,
        start=None,
        end=None,
        state=ResourceSearchState(1, (), 0, None, 0),
    )
    try:
        items, _ = _parse_resource_search_page(
            JABLE_SITE_ID, document, origin, work, 1
        )
    except _ResourceSearchPageError as exc:
        raise WebDownloadProviderError(
            "JableTV diagnostic sample page is invalid",
            code="challenge_active" if exc.retryable else "response_invalid",
        ) from exc
    if not items:
        raise WebDownloadProviderNotFound()
    return probe_web_download_provider_connection(
        JABLE_SITE_ID,
        items[0].code,
        origin,
        timeout_seconds=timeout_seconds,
    )


def probe_exact_web_download_provider(
    provider: object,
    code: object,
    base_url: object,
    *,
    timeout_seconds: float,
) -> ProviderManifest:
    """Resolve one exact Web source without creating a download task."""

    provider_id = str(provider or "").strip().lower()
    try:
        if provider_id == "av123":
            return _capture_av123(code, str(base_url or ""), variant="original",
                timeout_seconds=timeout_seconds, requested_height=None, quality_strategy="legacy")
        if provider_id == JABLE_SITE_ID:
            return _capture_jable(
                code,
                str(base_url or ""),
                timeout_seconds=timeout_seconds,
                requested_height=None,
                quality_strategy="legacy",
            )
        if provider_id == MISSAV_SITE_ID:
            return _capture_missav(
                code,
                str(base_url or ""),
                variant=DEFAULT_WEB_DOWNLOAD_VARIANT,
                timeout_seconds=timeout_seconds,
                requested_height=None,
                quality_strategy="legacy",
            )
        raise WebDownloadProviderError(
            "Web download provider is invalid", code="configuration"
        )
    except WebDownloadProviderError:
        raise
    except Exception as exc:
        code_value = (
            "timeout"
            if "timeout" in f"{type(exc).__name__} {exc}".casefold()
            else "upstream_unavailable"
        )
        raise WebDownloadProviderError(
            "Web provider source probe failed", code=code_value
        ) from exc


def discover_exact_web_download_variant(
    code: object,
    *,
    variant_priority: Sequence[object],
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    settings_loader: Callable[[], dict[str, object]] = load_settings,
) -> MissavVariant:
    """Choose an exact automatic-download variant without a browser session.

    This is deliberately only a lightweight detail-page probe.  Manifest and
    quality resolution remain the download worker's responsibility.  Falling
    back to ``original`` when MissAV is unavailable also lets the worker try
    the configured JableTV/SupJav providers instead of losing the request in
    the MissAV batch-discovery queue.
    """

    try:
        priority = normalize_variant_priority(variant_priority)
    except ValueError as exc:
        raise WebDownloadProviderError(
            "Web download variant priority is invalid", code="configuration"
        ) from exc
    if cancel_event is not None and cancel_event.is_set():
        raise WebDownloadProviderError(
            "Web source discovery was cancelled", code="cancelled"
        )
    settings = settings_loader()
    site = site_by_id(MISSAV_SITE_ID, settings)
    if (
        settings.get("_config_error")
        or site is None
        or not site.get("enabled")
        or site.get("parser_profile") != MISSAV_SITE_ID
        or not site_has_capability(site, WEB_DOWNLOAD_CAPABILITY)
    ):
        return DEFAULT_WEB_DOWNLOAD_VARIANT
    base_url = str(site.get("base_url") or "")
    for variant in priority:
        if cancel_event is not None and cancel_event.is_set():
            raise WebDownloadProviderError(
                "Web source discovery was cancelled", code="cancelled"
            )
        try:
            _probe_missav_exact_variant(
                code,
                base_url,
                variant=variant,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        except WebDownloadProviderNotFound:
            continue
        except WebDownloadProviderError:
            # Do not let a provider challenge/configuration incident block an
            # automatic request before the multi-provider worker can try it.
            continue
        return variant
    return DEFAULT_WEB_DOWNLOAD_VARIANT


class WebProviderResourceSearcher:
    """Incrementally search one non-browser Web provider with a durable cursor."""

    def __init__(
        self,
        source_id: str,
        *,
        settings_loader: Callable[[], dict[str, object]] = load_settings,
    ) -> None:
        self._source_id = source_id
        self._settings_loader = settings_loader

    def __call__(
        self,
        work: ResourceSearchWork,
        *,
        on_event: Callable[[ResourceSearchWorkerEvent], None],
        cancel_event: threading.Event,
    ) -> ResourceSearchWorkerResult:
        if work.source_id != self._source_id:
            raise ResourceSearchUnavailableError(
                "resource search adapter does not match its source"
            )
        settings = self._settings_loader()
        site = site_by_id(self._source_id, settings)
        if (
            settings.get("_config_error")
            or site is None
            or not site.get("enabled")
            or site.get("parser_profile") != self._source_id
            or not site_has_capability(site, RESOURCE_SEARCH_CAPABILITY)
        ):
            raise ResourceSearchUnavailableError(
                "resource search site configuration is unavailable"
            )
        try:
            origin = _safe_origin(
                str(site.get("base_url") or ""),
                expected_profile=self._source_id,
            )
        except WebDownloadProviderError as exc:
            raise ResourceSearchUnavailableError(
                "resource search site configuration is unavailable"
            ) from exc

        next_page = work.state.next_page
        pending = work.state.pending
        cursor = work.state.cursor
        total_pages = work.state.total_pages
        scanned_pages = work.state.scanned_pages
        emitted = 0
        session = (
            _session()
        )

        def snapshot() -> ResourceSearchState:
            return ResourceSearchState(
                next_page=next_page,
                pending=pending,
                cursor=cursor,
                total_pages=total_pages,
                scanned_pages=scanned_pages,
            )

        def consume_pending(*, fetched: bool) -> None:
            nonlocal cursor, emitted, next_page, pending
            page_number = next_page
            first_delta = True
            while (
                page_number is not None
                and cursor < len(pending)
                and emitted < work.result_limit
            ):
                if cancel_event.is_set():
                    raise ResourceSearchCancelledError("resource search was cancelled")
                count = min(
                    _RESOURCE_DELTA_ITEMS,
                    len(pending) - cursor,
                    work.result_limit - emitted,
                )
                delta = pending[cursor : cursor + count]
                cursor += count
                if cursor >= len(pending):
                    pending = ()
                    cursor = 0
                    if total_pages is not None and page_number >= total_pages:
                        next_page = None
                    elif page_number >= 999:
                        next_page = None
                    else:
                        next_page = page_number + 1
                emitted += len(delta)
                on_event(
                    ResourceSearchPageEvent(
                        page=page_number,
                        fetched=fetched and first_delta,
                        items=delta,
                        state=snapshot(),
                    )
                )
                first_delta = False

        on_event(ResourceSearchStartedEvent(snapshot()))
        if pending:
            consume_pending(fetched=False)
        if emitted >= work.result_limit or next_page is None:
            state = snapshot()
            return ResourceSearchWorkerResult(state.next_page is None, state)

        while next_page is not None and emitted < work.result_limit:
            if cancel_event.is_set():
                raise ResourceSearchCancelledError("resource search was cancelled")
            page_number = next_page
            on_event(ResourceSearchHeartbeatEvent(snapshot()))
            try:
                search_url = _resource_search_url(
                    self._source_id, origin, work.query, page_number,
                )
                document = _bounded_page(session, search_url, 30.0, allowed_hosts={urlsplit(origin).hostname or ""})
            except WebDownloadProviderNotFound:
                # Search endpoints are expected to return a structured 200
                # page even for zero matches.  A 404 is endpoint/config drift,
                # not evidence that the query has no results.
                raise ResourceSearchWorkerError(
                    "discovery_unavailable",
                    retryable=False,
                    state=snapshot(),
                ) from None
            except (WebDownloadProviderError, FetchError) as exc:
                retryable = getattr(exc, "code", "upstream_unavailable") not in {
                    "configuration",
                    "host_policy",
                    "response_invalid",
                }
                raise ResourceSearchWorkerError(
                    "transient_browser_failure"
                    if retryable
                    else "discovery_unavailable",
                    retryable=retryable,
                    state=snapshot(),
                ) from None
            if cancel_event.is_set():
                raise ResourceSearchCancelledError("resource search was cancelled")
            try:
                page_items, discovered_pages = _parse_resource_search_page(
                    self._source_id,
                    document,
                    origin,
                    work,
                    page_number,
                )
            except _ResourceSearchPageError as exc:
                raise ResourceSearchWorkerError(
                    (
                        "transient_browser_failure"
                        if exc.retryable
                        else "discovery_unavailable"
                    ),
                    retryable=exc.retryable,
                    state=snapshot(),
                ) from None
            except (TypeError, ValueError):
                raise ResourceSearchWorkerError(
                    "discovery_unavailable",
                    retryable=False,
                    state=snapshot(),
                ) from None
            scanned_pages += 1
            total_pages = max(total_pages or 1, discovered_pages, page_number)
            pending = page_items
            cursor = 0
            if pending:
                consume_pending(fetched=True)
            else:
                next_page = None if page_number >= total_pages else page_number + 1
                on_event(
                    ResourceSearchPageEvent(
                        page=page_number,
                        fetched=True,
                        items=(),
                        state=snapshot(),
                    )
                )

        state = snapshot()
        return ResourceSearchWorkerResult(state.next_page is None, state)


def _resource_search_url(
    source_id: str,
    origin: str,
    query: str,
    page: int,
) -> str:
    encoded = quote(query, safe="")
    if source_id == "av123":
        return f"{origin}/cn/search?keyword={encoded}&sort=relevant&page={page}"
    if source_id == JABLE_SITE_ID:
        return (
            f"{origin}/search/?q={encoded}"
            if page == 1
            else f"{origin}/search/{encoded}/{page}/"
        )
    if source_id == MISSAV_SITE_ID:
        base = f"{origin}/cn/search/{encoded}"
        return base if page == 1 else f"{base}?page={page}"
    raise ValueError("Web resource search source is invalid")


def _parse_resource_search_page(
    source_id: str,
    document: str,
    origin: str,
    work: ResourceSearchWork,
    page: int,
) -> tuple[tuple[ResourceSearchItem, ...], int]:
    if source_id == "av123":
        # Share the validated card/identity parser with online browsing.
        from ..watching.catalog import parse_catalog

        try:
            parsed = parse_catalog(source_id, origin, document, page=page)
        except WebDownloadProviderError as exc:
            raise _ResourceSearchPageError(retryable=exc.code != "response_invalid") from exc
        exact = canonical_catalog_code(work.query, max_length=32) if work.start is None else None
        grouped: dict[str, tuple[str, str, set[MissavVariant]]] = {}
        edition_variants: dict[str, MissavVariant] = {
            "中文字幕": "chinese_subtitle",
            "无码": "uncensored_leak",
            "无码流出": "uncensored_leak",
        }
        for entry in parsed["items"]:
            normalized = normalize_catalog_code(entry["code"], max_length=32)
            if normalized is None or (exact is not None and normalized[1] != exact):
                continue
            if not _resource_code_in_range(normalized[1], work):
                continue
            variants = {
                edition_variants.get(label, DEFAULT_WEB_DOWNLOAD_VARIANT)
                for label in entry["editions"]
            }
            if normalized[1] in grouped:
                grouped[normalized[1]][2].update(variants)
            else:
                grouped[normalized[1]] = (normalized[0], entry["title"], variants)
        return tuple(
            ResourceSearchItem(
                code,
                tuple(v for v in WEB_DOWNLOAD_VARIANTS if v in variants),
                _safe_resource_title(title),
            )
            for code, title, variants in grouped.values()
        ), page + int(parsed["has_more"])
    soup = BeautifulSoup(document, "html.parser")
    card_selector = _RESOURCE_SEARCH_CARD_SELECTORS.get(source_id)
    if card_selector is None:
        raise ValueError("Web resource search source is invalid")
    result_cards = soup.select(card_selector)
    if not result_cards:
        # Jable includes Cloudflare's passive challenge-platform script on
        # successful pages too. Its own result container and explicit empty
        # message establish a valid zero-result page before that script check.
        jable_empty = (
            soup.select_one(
                "#list_videos_videos_list_search_result h5.inactive-color"
            )
            if source_id == JABLE_SITE_ID
            else None
        )
        if jable_empty is None or jable_empty.get_text(strip=True) not in {
            "暫無相關內容",
            "暂无相关内容",
        }:
            if _RESOURCE_SEARCH_CHALLENGE_RE.search(document):
                raise _ResourceSearchPageError(retryable=True)
            visible_text = soup.get_text(" ", strip=True)
            if not (
                soup.select_one(_RESOURCE_SEARCH_EMPTY_SELECTOR)
                or _RESOURCE_SEARCH_EMPTY_TEXT_RE.search(visible_text)
            ):
                raise _ResourceSearchPageError(retryable=False)

    candidates: list[tuple[str, str, tuple[MissavVariant, ...]]] = []
    if source_id == JABLE_SITE_ID:
        for card in result_cards:
            anchor = card.select_one('div.detail h6.title a[href*="/videos/"]')
            if anchor is None:
                continue
            target = urljoin(origin, str(anchor.get("href") or ""))
            parsed = urlsplit(target)
            match = re.fullmatch(r"/videos/([A-Za-z0-9._-]+)/", parsed.path)
            if _origin(target) != origin or match is None:
                continue
            normalized = normalize_catalog_code(match.group(1), max_length=32)
            if normalized is None:
                continue
            candidates.append(
                (
                    normalized[0],
                    str(anchor.get("title") or anchor.get_text(" ", strip=True)),
                    (DEFAULT_WEB_DOWNLOAD_VARIANT,),
                )
            )
    elif source_id == MISSAV_SITE_ID:
        grouped: dict[
            str,
            tuple[str, set[MissavVariant], str],
        ] = {}
        for card in result_cards:
            title_anchor = card.select_one("a.text-secondary")
            image = card.select_one("img")
            title = str(
                (title_anchor.get_text(" ", strip=True) if title_anchor else "")
                or (image.get("alt") if image else "")
                or ""
            )
            for anchor in card.select("a[href]"):
                candidate = _missav_resource_candidate(
                    anchor.get("href"),
                    origin,
                )
                if candidate is None:
                    continue
                display_code, code_key, variant = candidate
                existing = grouped.get(code_key)
                if existing is None:
                    grouped[code_key] = (display_code, {variant}, title)
                else:
                    existing[1].add(variant)
                    if len(title) > len(existing[2]):
                        grouped[code_key] = (existing[0], existing[1], title)
        candidates.extend(
            (
                display_code,
                title,
                tuple(
                    variant
                    for variant in WEB_DOWNLOAD_VARIANTS
                    if variant in variants
                ),
            )
            for display_code, variants, title in grouped.values()
        )
    else:
        raise ValueError("Web resource search source is invalid")

    if result_cards and not candidates:
        # A page containing the provider's card shell but no parseable card is
        # a layout/identity drift, not a legitimate empty search response.
        raise _ResourceSearchPageError(retryable=False)

    items: list[ResourceSearchItem] = []
    seen: set[str] = set()
    exact_query = (
        canonical_catalog_code(work.query, max_length=32)
        if work.suffix_width is None and work.start is None and work.end is None
        else None
    )
    for code, title, variants in candidates:
        normalized = normalize_catalog_code(code, max_length=32)
        if normalized is None or normalized[1] in seen:
            continue
        if exact_query is not None and normalized[1] != exact_query:
            continue
        if not _resource_code_in_range(normalized[1], work):
            continue
        seen.add(normalized[1])
        items.append(
            ResourceSearchItem(
                code=normalized[0],
                available_variants=variants,
                title=_safe_resource_title(title),
            )
        )
    if len(items) > _MAX_RESOURCE_PAGE_ITEMS:
        raise ValueError("Web resource search page has too many items")

    max_page = page
    pagination_anchors = (
        soup.select('a[href*="page="]')
        if source_id == MISSAV_SITE_ID
        else soup.select(".pagination a[href]")
    )
    search_path = urlsplit(_resource_search_url(source_id, origin, work.query, 1)).path
    for anchor in pagination_anchors:
        target = urljoin(origin, str(anchor.get("href") or ""))
        if _origin(target) != origin:
            continue
        parsed = urlsplit(target)
        if source_id == JABLE_SITE_ID:
            match = re.search(
                r"/search/(?:[^/]+/)?([1-9][0-9]{0,2})/",
                parsed.path,
            )
        else:
            match = (
                re.search(
                    r"(?:^|&)page=([1-9][0-9]{0,2})(?:&|$)",
                    parsed.query,
                )
                if parsed.path == search_path
                else None
            )
        if match is not None:
            max_page = max(max_page, min(999, int(match.group(1))))
    return tuple(items), max_page


def _missav_resource_candidate(
    href: object,
    origin: str,
) -> tuple[str, str, MissavVariant] | None:
    raw_href = str(href or "").strip()
    if not raw_href or any(character in raw_href for character in "\r\n\t\\"):
        return None
    target = urljoin(f"{origin}/", raw_href)
    parsed = urlsplit(target)
    if (
        _origin(target) != origin
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
    ):
        return None
    match = re.fullmatch(
        r"/(?:dm[0-9]+/)?(?:cn/)?([A-Za-z0-9._-]+)/?",
        parsed.path,
        re.IGNORECASE,
    )
    if match is None:
        return None
    slug = match.group(1)
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT
    base_slug = slug
    for candidate in WEB_DOWNLOAD_VARIANTS:
        suffix = web_download_variant_suffix(candidate)
        if suffix and slug.casefold().endswith(suffix):
            base_slug = slug[: -len(suffix)]
            variant = candidate
            break
    normalized = normalize_catalog_code(base_slug.upper(), max_length=32)
    if normalized is None:
        return None
    return normalized[0], normalized[1], variant


def _first_catalog_code(value: str) -> tuple[str, str] | None:
    for candidate in re.findall(
        r"(?i)(?:FC2[-_. ]?(?:PPV[-_. ]?)?\d{2,9}|[A-Z]{2,12}[-_ ]?\d{2,8})",
        value,
    ):
        normalized = normalize_catalog_code(candidate.replace(" ", "-"), max_length=32)
        if normalized is not None:
            return normalized
    return None


def _resource_code_in_range(code_key: str, work: ResourceSearchWork) -> bool:
    if work.suffix_width is None and work.start is None and work.end is None:
        return True
    prefix = "".join(
        character for character in work.query.upper() if character.isalnum()
    )
    if not prefix or not code_key.startswith(prefix):
        return False
    suffix = code_key[len(prefix) :]
    if not suffix.isdigit() or (
        work.suffix_width is not None and len(suffix) != work.suffix_width
    ):
        return False
    number = int(suffix)
    return not (
        (work.start is not None and number < work.start)
        or (work.end is not None and number > work.end)
    )


def _safe_resource_title(value: object) -> str | None:
    normalized = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    clean = " ".join(
        "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in normalized
        ).split()
    )
    if not clean or len(clean) > 512 or contains_sensitive_transport_text(clean):
        return None
    return clean


def _capture_av123(
    code: object, base_url: str, *, variant: str, timeout_seconds: float,
    requested_height: object | None, quality_strategy: object,
    cancel_event: threading.Event | None = None,
) -> ProviderManifest:
    from ..watching.catalog import parse_catalog, detail_url
    from ..watching.av123 import capture
    from .media import fetch_safe_media_resource, media_provider_scope

    origin = _safe_origin(base_url, expected_profile="av123")
    session = _session()
    deadline = time.monotonic() + timeout_seconds
    wanted = canonical_catalog_code(code)
    labels = {"original": "原片", "chinese_subtitle": "中文字幕", "uncensored_leak": "无码流出"}
    for page in range(1, 6):
        if cancel_event is not None and cancel_event.is_set():
            raise WebDownloadProviderError("Capture cancelled", code="interrupted")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebDownloadProviderError("Capture timed out", code="timeout")
        document = _bounded_page(session, _resource_search_url("av123", origin, str(code), page), remaining, allowed_hosts={urlsplit(origin).hostname or ""})
        result = parse_catalog("av123", origin, document, page=page)
        for entry in result["items"]:
            if canonical_catalog_code(entry["code"]) != wanted:
                continue
            editions = entry["editions"]
            if labels.get(variant) not in editions and not (variant == "uncensored_leak" and "无码" in editions):
                continue
            target = detail_url("av123", origin, entry["url"])
            body = _bounded_page(session, target, max(1, deadline - time.monotonic()), allowed_hosts={urlsplit(origin).hostname or ""})
            manifest = capture(session, body, target)
            with media_provider_scope("av123", allowed_hosts=manifest.allowed_media_hosts):
                playlist = fetch_safe_media_resource(manifest.url, manifest.headers, max_bytes=_MAX_PAGE_BYTES, timeout_seconds=max(1, deadline - time.monotonic()))
                text = playlist.body.decode("utf-8-sig")
                if "#EXT-X-STREAM-INF:" in text:
                    url, height = _select_media_playlist(manifest.url, text, requested_height=requested_height, quality_strategy=quality_strategy)
                    return ProviderManifest("av123", url, manifest.headers, target, height, allowed_media_hosts=manifest.allowed_media_hosts)
                if requested_height is not None:
                    raise WebDownloadProviderError("Requested quality is unavailable", code="quality_unavailable")
            return manifest
        if not result["has_more"]:
            break
    raise WebDownloadProviderNotFound()


def _provider_timeout(seconds: float) -> float:
    if seconds <= 0 or not math.isfinite(seconds):
        raise WebDownloadProviderError("Provider operation timed out", code="timeout")
    return max(0.001, min(float(seconds), 120.0))


def _capture_missav(
    code: object,
    base_url: str,
    *,
    variant: object,
    timeout_seconds: float,
    requested_height: object | None,
    quality_strategy: object,
    cancel_event: threading.Event | None = None,
) -> ProviderManifest:
    document, resolved_detail_url, origin = _missav_detail_document(
        code,
        base_url,
        variant=variant,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )
    manifest_url = _extract_missav_manifest(document)
    media_host = (urlsplit(manifest_url).hostname or "").lower()
    if media_host != "surrit.com":
        raise WebDownloadProviderError(
            "MissAV media host was rejected", code="host_policy"
        )
    headers = {"Referer": resolved_detail_url, "Origin": origin}
    session = _session()
    master = _bounded_get(
        session,
        manifest_url,
        timeout_seconds,
        allowed_hosts={media_host},
        headers=headers,
        max_bytes=MAX_MANIFEST_BYTES,
    )
    media_url, selected_height = _select_media_playlist(
        manifest_url,
        master,
        requested_height=requested_height,
        quality_strategy=quality_strategy,
    )
    if (urlsplit(media_url).hostname or "").lower() != media_host:
        raise WebDownloadProviderError(
            "MissAV media playlist host was rejected", code="host_policy"
        )
    return ProviderManifest(
        provider=MISSAV_SITE_ID,
        url=media_url,
        headers=headers,
        page_url=resolved_detail_url,
        selected_height=selected_height,
    )


def _probe_missav_exact_variant(
    code: object,
    base_url: str,
    *,
    variant: object,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> None:
    _missav_detail_document(
        code,
        base_url,
        variant=variant,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def _missav_detail_document(
    code: object,
    base_url: str,
    *,
    variant: object,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str, str]:
    origin = _safe_origin(base_url, expected_profile=MISSAV_SITE_ID)
    normalized = normalize_catalog_code(code, max_length=32)
    if normalized is None:
        raise WebDownloadProviderNotFound()
    display_code, wanted = normalized
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise WebDownloadProviderError(
            "MissAV download variant is invalid", code="configuration"
        ) from exc
    slug = f"{display_code.lower()}{web_download_variant_suffix(clean_variant)}"
    detail_url = f"{origin}/cn/{quote(slug, safe='-._')}"
    if cancel_event is not None and cancel_event.is_set():
        raise WebDownloadProviderError(
            "Web source discovery was cancelled", code="cancelled"
        )
    session = _session()
    document, resolved_detail_url = _bounded_get_with_redirects(
        session,
        detail_url,
        timeout_seconds,
        allowed_hosts={urlsplit(origin).hostname or ""},
        headers={"Referer": f"{origin}/"},
    )
    if cancel_event is not None and cancel_event.is_set():
        raise WebDownloadProviderError(
            "Web source discovery was cancelled", code="cancelled"
        )
    soup = BeautifulSoup(document, "html.parser")
    title = soup.select_one('meta[property="og:title"]')
    title_text = html.unescape(str(title.get("content") or "")) if title else ""
    if wanted not in _catalog_codes_in_text(title_text):
        raise WebDownloadProviderNotFound()
    canonical = soup.select_one('link[rel="canonical"], meta[property="og:url"]')
    canonical_url = (
        str(canonical.get("href") or canonical.get("content") or "")
        if canonical
        else ""
    )
    canonical_parsed = urlsplit(canonical_url)
    expected_path = re.compile(rf"/(?:dm\d+/)?(?:cn/)?{re.escape(slug)}", re.IGNORECASE)
    resolved_detail = urlsplit(resolved_detail_url)
    if (
        resolved_detail.scheme != "https"
        or (resolved_detail.hostname or "").lower()
        != (urlsplit(origin).hostname or "").lower()
        or expected_path.fullmatch(resolved_detail.path) is None
        or resolved_detail.query
        or resolved_detail.fragment
        or canonical_parsed.scheme != "https"
        or (canonical_parsed.hostname or "").lower()
        != (urlsplit(origin).hostname or "").lower()
        or expected_path.fullmatch(canonical_parsed.path) is None
        or canonical_parsed.query
        or canonical_parsed.fragment
    ):
        raise WebDownloadProviderError(
            "MissAV detail identity could not be verified", code="response_invalid"
        )
    return document, resolved_detail_url, origin


def _capture_jable(
    code: object,
    base_url: str,
    *,
    variant: str = "original",
    timeout_seconds: float,
    requested_height: object | None,
    quality_strategy: object,
) -> ProviderManifest:
    origin = _safe_origin(base_url, expected_profile=JABLE_SITE_ID)
    host = urlsplit(origin).hostname or ""
    session = _session()
    wanted = canonical_catalog_code(code)
    if wanted is None:
        raise WebDownloadProviderNotFound()
    slug = re.sub(r"[^a-z0-9]+", "-", str(code).strip().lower()).strip("-")
    detail_url = f"{origin}/videos/{slug}/"
    try:
        detail = _bounded_page(
            session, detail_url, timeout_seconds, allowed_hosts={host}
        )
        title_match = re.search(
            r'property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
            detail,
            re.IGNORECASE,
        )
        if title_match is None or wanted not in _catalog_codes_in_text(
            html.unescape(title_match.group(1))
        ):
            raise WebDownloadProviderNotFound()
    except WebDownloadProviderNotFound:
        search = _bounded_page(
            session,
            f"{origin}/search/?q={quote(str(code), safe='')}",
            timeout_seconds,
            allowed_hosts={host},
        )
        detail_url = _jable_exact_result(search, origin, code)
        detail = _bounded_page(
            session, detail_url, timeout_seconds, allowed_hosts={host}
        )
    if variant != "original":
        from bs4 import BeautifulSoup
        from ..watching.catalog import identity
        soup = BeautifulSoup(detail, "html.parser")
        title = soup.select_one('meta[property="og:title"]')
        labels = " ".join(item.get_text(" ", strip=True) for item in soup.select(".video-info .tags"))
        _, editions = identity(str(title.get("content") or "") + " " + labels if title else labels)
        expected = "中文字幕" if variant == "chinese_subtitle" else "无码流出"
        if expected not in editions and not (expected == "无码流出" and "无码" in editions):
            raise WebDownloadProviderNotFound()
    matches = re.findall(r"https://[^\s\"']+\.m3u8[^\s\"']*", detail, re.IGNORECASE)
    if len(matches) != 1:
        raise WebDownloadProviderNotFound()
    manifest_url = html.unescape(matches[0])
    media_host = (urlsplit(manifest_url).hostname or "").lower()
    if not _host_matches(media_host, "mushroomtrack.com"):
        raise WebDownloadProviderError(
            "JableTV media host was rejected", code="host_policy"
        )
    playlist = _bounded_get(
        session,
        manifest_url,
        timeout_seconds,
        allowed_hosts={media_host},
        headers={"Referer": detail_url, "Origin": origin},
        max_bytes=MAX_MANIFEST_BYTES,
    )
    if "#EXT-X-STREAM-INF" in playlist.upper():
        manifest_url, selected_height = _select_media_playlist(
            manifest_url,
            playlist,
            requested_height=requested_height,
            quality_strategy=quality_strategy,
        )
    else:
        if requested_height is not None and str(quality_strategy) == "selected":
            raise WebDownloadProviderNotFound(
                "Requested quality is unavailable"
            )
        selected_height = None
    return ProviderManifest(
        provider=JABLE_SITE_ID,
        url=manifest_url,
        headers={"Referer": detail_url, "Origin": origin},
        page_url=detail_url,
        selected_height=selected_height,
    )


def _session(*, impersonate: str = "chrome"):
    try:
        from curl_cffi import requests
    except Exception as exc:  # pragma: no cover - pinned production dependency
        raise WebDownloadProviderError(
            "Secure HTTP client is unavailable", code="configuration"
        ) from exc
    return requests.Session(impersonate=impersonate)


def _bounded_page(
    session: object, url: str, timeout_seconds: float, *, allowed_hosts: set[str],
    headers: dict[str, str] | None = None,
) -> str:
    def fetch() -> str:
        return _fetch_provider_page(session, url, _provider_timeout(timeout_seconds), allowed_hosts=allowed_hosts, headers=headers)
    return fetch()


def _fetch_provider_page(session: object, url: str, timeout_seconds: float, *, allowed_hosts: set[str], headers: dict[str, str] | None) -> str:
    deadline = time.monotonic() + timeout_seconds
    _require_allowed_public_host(url, allowed_hosts, "Provider request host was rejected")
    def http_fetch(request_url: str, **kwargs: object) -> str:
        try:
            try:
                return _bounded_get(session, request_url, _provider_timeout(deadline - time.monotonic()), allowed_hosts=allowed_hosts, headers=headers)
            except WebDownloadProviderError as exc:
                if exc.code != "redirect_policy":
                    raise
                document, _ = _bounded_get_with_redirects(session, request_url, _provider_timeout(deadline - time.monotonic()), allowed_hosts=allowed_hosts, headers=headers or {})
                return document
        except WebDownloadProviderError as exc:
            if exc.code == "challenge_active":
                raise FetchError("HTTP 403") from exc
            raise
    try:
        return fetch_site_text(url, timeout=timeout_seconds, max_bytes=_MAX_PAGE_BYTES,
                               allowed_origins=(_origin(url),), headers=headers, http_fetch=http_fetch)
    except FetchError as exc:
        message = str(exc).lower()
        code = "upstream_unavailable"
        if "browser fetch capacity is busy" in message:
            code = "busy"
        elif "timed out" in message or "timeout" in message:
            code = "timeout"
        elif "challenge" in message:
            code = "challenge_active"
        raise WebDownloadProviderError("Provider browser verification failed", code=code) from exc


def _bounded_get(
    session: object,
    url: str,
    timeout_seconds: float,
    *,
    allowed_hosts: set[str],
    headers: dict[str, str] | None = None,
    max_bytes: int = _MAX_PAGE_BYTES,
) -> str:
    request_host = _require_allowed_public_host(
        url, allowed_hosts, "Provider request host was rejected"
    )
    response = session.get(
        url,
        headers=headers or {},
        timeout=_provider_timeout(timeout_seconds),
        allow_redirects=False,
    )
    if int(response.status_code) in _REDIRECT_STATUSES:
        raise WebDownloadProviderError(
            "Unexpected provider redirect", code="redirect_policy"
        )
    if int(response.status_code) == 404:
        raise WebDownloadProviderNotFound()
    if int(response.status_code) == 429:
        raise WebDownloadProviderError("Provider rate limit is active", code="rate_limited")
    if int(response.status_code) in {403, 503}:
        raise WebDownloadProviderError(
            "Provider challenge is active", code="challenge_active"
        )
    if int(response.status_code) != 200:
        raise WebDownloadProviderError(
            "Provider request failed", code="upstream_unavailable"
        )
    # Redirects are disabled, so the response must come from the vetted host.
    if (urlsplit(str(response.url)).hostname or "").lower() != request_host:
        raise WebDownloadProviderError(
            "Provider response host was rejected", code="host_policy"
        )
    body = bytes(response.content)
    if not body or len(body) > max_bytes:
        raise WebDownloadProviderError(
            "Provider response size was rejected", code="response_invalid"
        )
    return body.decode("utf-8", errors="replace")


def _require_allowed_public_host(
    url: str,
    allowed_hosts: set[str],
    message: str,
) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host not in allowed_hosts or not PublicHostResolver(max_hosts=1).is_public(
        host
    ):
        raise WebDownloadProviderError(message, code="host_policy")
    return host


def _bounded_get_with_redirects(
    session: object,
    url: str,
    timeout_seconds: float,
    *,
    allowed_hosts: set[str],
    headers: dict[str, str],
) -> tuple[str, str]:
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        _require_allowed_public_host(
            current, allowed_hosts, "Provider redirect host was rejected"
        )
        response = session.get(
            current,
            headers=headers,
            timeout=_provider_timeout(timeout_seconds),
            allow_redirects=False,
        )
        if int(response.status_code) in _REDIRECT_STATUSES:
            location = str(response.headers.get("location") or "")
            if not location:
                break
            current = urljoin(current, location)
            continue
        if int(response.status_code) == 404:
            raise WebDownloadProviderNotFound()
        if int(response.status_code) in {403, 429, 503}:
            raise WebDownloadProviderError(
                "Provider challenge is active", code="challenge_active"
            )
        if int(response.status_code) != 200:
            raise WebDownloadProviderError(
                "Provider gateway failed", code="upstream_unavailable"
            )
        body = bytes(response.content)
        if not body or len(body) > _MAX_PAGE_BYTES:
            raise WebDownloadProviderError(
                "Provider gateway response was rejected", code="response_invalid"
            )
        return body.decode("utf-8", errors="replace"), str(response.url)
    raise WebDownloadProviderError(
        "Provider redirect limit was exceeded", code="redirect_policy"
    )


def _jable_exact_result(document: str, origin: str, code: object) -> str:
    wanted = canonical_catalog_code(code)
    if wanted is None:
        raise WebDownloadProviderNotFound()
    soup = BeautifulSoup(document, "html.parser")
    for anchor in soup.select('a[href*="/videos/"]'):
        title = str(anchor.get("title") or anchor.get_text(" ", strip=True))
        href = str(anchor.get("href") or "")
        if wanted not in _catalog_codes_in_text(f"{title} {href}"):
            continue
        target = urljoin(origin, href)
        if _origin(target) == origin and re.fullmatch(
            r"/videos/[A-Za-z0-9._-]+/", urlsplit(target).path
        ):
            return target
    raise WebDownloadProviderNotFound()


def _catalog_codes_in_text(value: str) -> set[str]:
    candidates = re.findall(
        r"(?i)(?:FC2[-_. ]?(?:PPV[-_. ]?)?\d{2,9}|[A-Z]{2,12}[-_ ]?\d{2,8})", value
    )
    return {
        code
        for item in candidates
        if (code := canonical_catalog_code(item)) is not None
    }


def _extract_missav_manifest(document: str) -> str:
    for script in re.findall(
        r"<script[^>]*>(.*?)</script>", document, re.DOTALL | re.IGNORECASE
    ):
        if "eval(function" not in script or "m3u8" not in script:
            continue
        unpacked = _unpack_js_eval(script)
        if not unpacked:
            continue
        match = re.search(
            r"\bsource\s*=\s*[\\']*(https://[^'\\;\s]+\.m3u8)",
            unpacked,
            re.IGNORECASE,
        )
        if match:
            return html.unescape(match.group(1))
    raise WebDownloadProviderError(
        "MissAV player script could not be parsed", code="response_invalid"
    )


def _unpack_js_eval(script: str) -> str | None:
    match = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',\s*(\d+),\s*(\d+),\s*'([^']*)'\s*\.split\('\|'\)",
        script,
        re.DOTALL,
    )
    if not match:
        return None
    packed, base, count = match.group(1), int(match.group(2)), int(match.group(3))
    keys = match.group(4).split("|")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if not 2 <= base <= len(digits) or not 0 <= count <= 200_000:
        return None

    def encoded(number: int) -> str:
        if number == 0:
            return "0"
        result = ""
        while number:
            result = digits[number % base] + result
            number //= base
        return result

    lookup = {
        encoded(index): keys[index]
        if index < len(keys) and keys[index]
        else encoded(index)
        for index in range(count)
    }
    return re.sub(
        r"\b(\w+)\b", lambda item: lookup.get(item.group(0), item.group(0)), packed
    )


def _select_media_playlist(
    master_url: str,
    document: str,
    *,
    requested_height: object | None,
    quality_strategy: object,
) -> tuple[str, int]:
    lines = [line.strip() for line in document.splitlines()]
    variants: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        resolution = re.search(r"RESOLUTION=\d+x(\d+)", line, re.IGNORECASE)
        if resolution is None:
            continue
        uri = next(
            (item for item in lines[index + 1 :] if item and not item.startswith("#")),
            "",
        )
        if uri:
            variants.append((int(resolution.group(1)), urljoin(master_url, uri)))
    if not variants:
        raise WebDownloadProviderError(
            "Provider master playlist is invalid", code="manifest_invalid"
        )
    variants.sort(key=lambda item: item[0])
    if requested_height is not None:
        try:
            wanted = int(requested_height)
        except (TypeError, ValueError):
            wanted = 0
        exact = [item for item in variants if item[0] == wanted]
        if str(quality_strategy) == "selected" and not exact:
            raise WebDownloadProviderNotFound("Requested quality is unavailable")
        eligible = [item for item in variants if item[0] <= wanted]
        if not eligible:
            raise WebDownloadProviderNotFound("Requested quality is unavailable")
        chosen = exact[-1] if exact else eligible[-1]
    else:
        chosen = variants[-1]
    return chosen[1], chosen[0]


def _safe_origin(value: str, *, expected_profile: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.port not in {None, 443}:
        raise WebDownloadProviderError(
            f"{_provider_label(expected_profile)} configuration is invalid",
            code="configuration",
        )
    if not PublicHostResolver(max_hosts=1).is_public(host):
        raise WebDownloadProviderError("Provider host was rejected", code="host_policy")
    return f"https://{host}"


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _provider_label(provider: str) -> str:
    return {
        MISSAV_SITE_ID: "MissAV",
        JABLE_SITE_ID: "JableTV",
        "av123": "123AV",
    }.get(provider, "Web")
