from __future__ import annotations

import html
import math
import os
import re
import stat
import threading
import time
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Sequence, TypeVar
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
    SUPJAV_SITE_ID,
    WEB_DOWNLOAD_CAPABILITY,
    load_settings,
    site_by_id,
    site_has_capability,
)
from .media import MAX_MANIFEST_BYTES
from ..net.http_client import FetchError, fetch_text
from .source_catalog import (
    CANDIDATE_WEB_SOURCE_IDS,
    WebCatalogError,
    catalog_search_url,
    parse_catalog_search,
    probe_catalog_media,
)
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
_SUPJAV_GATEWAY_HOST = "lk1.supremejav.com"
_SUPJAV_EMBED_HOSTS = frozenset({"fc2stream.tv", "www.fc2stream.tv"})
_SUPJAV_STREAMTAPE_HOSTS = frozenset(
    {"streamtape.com", "www.streamtape.com", "streamta.pe", "www.streamta.pe"}
)
_RESOURCE_DELTA_ITEMS = 64
_MAX_RESOURCE_PAGE_ITEMS = 128
_SUPJAV_CAPTURE_SPACING_SECONDS = 3.0
_SUPJAV_CHALLENGE_COOLDOWN_SECONDS = 30.0
_SUPJAV_CAPTURE_LOCK = threading.Lock()
# SupJav's Cloudflare policy currently admits only Safari TLS/HTTP2
# fingerprints, and each admits roughly half of first requests. Rotating
# through several Safari builds before the legacy profiles makes one search
# page succeed with near certainty instead of failing on the first challenge.
_SUPJAV_IMPERSONATE_PROFILES = (
    "safari15_5",
    "safari2601",
    "safari184_ios",
    "safari17_0",
    "safari170",
    "safari17_2_ios",
    "safari155",
    "firefox147",
    "chrome131",
)
_SUPJAV_CAPTURE_STATE_PATH = (
    Path(tempfile.gettempdir()) / "jav-pilot-supjav-capture.lock"
)
_RESOURCE_SEARCH_CARD_SELECTORS = {
    JABLE_SITE_ID: "div.video-img-box",
    SUPJAV_SITE_ID: "div.post",
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


@dataclass(frozen=True, slots=True)
class ProviderSearchPage:
    provider: str
    origin: str
    query: str
    document: str = field(repr=False)


_CoordinatedResult = TypeVar("_CoordinatedResult")


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
        if requested_provider in {"auto", MISSAV_SITE_ID}:
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
                if str(variant) != "original":
                    if provider_id not in CANDIDATE_WEB_SOURCE_IDS:
                        raise WebDownloadProviderNotFound()
                if provider_id in CANDIDATE_WEB_SOURCE_IDS:
                    return _capture_catalog_provider(
                        provider_id, code, str(site.get("base_url") or ""),
                        variant=str(variant), timeout_seconds=timeout_seconds,
                        requested_height=requested_height, quality_strategy=quality_strategy,
                    )
                if provider_id == SUPJAV_SITE_ID:
                    return _capture_supjav(
                        code,
                        str(site.get("base_url") or ""),
                        timeout_seconds=timeout_seconds,
                        requested_height=requested_height,
                        quality_strategy=quality_strategy,
                        cancel_event=cancel_event,
                    )
                if provider_id == JABLE_SITE_ID:
                    return _capture_jable(
                        code,
                        str(site.get("base_url") or ""),
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
    if provider_id not in {
        JABLE_SITE_ID,
        SUPJAV_SITE_ID,
        MISSAV_SITE_ID,
        *CANDIDATE_WEB_SOURCE_IDS,
    }:
        raise WebDownloadProviderError(
            "Web download provider is invalid", code="configuration"
        )
    origin = _safe_origin(str(base_url or ""), expected_profile=provider_id)
    query = str(code or "").strip()
    if canonical_catalog_code(query) is None:
        raise WebDownloadProviderError(
            "Web provider diagnostic code is invalid", code="configuration"
        )
    host = urlsplit(origin).hostname or ""

    def fetch() -> str:
        if provider_id in CANDIDATE_WEB_SOURCE_IDS:
            return fetch_text(
                _resource_search_url(provider_id, origin, query, 1),
                timeout=timeout_seconds, max_bytes=_MAX_PAGE_BYTES,
                allowed_origin=origin,
            )
        return _bounded_get(
            _supjav_session()
            if provider_id == SUPJAV_SITE_ID
            else _session(impersonate="chrome"),
            _resource_search_url(provider_id, origin, query, 1),
            timeout_seconds,
            allowed_hosts={host},
        )

    try:
        document = (
            _run_supjav_coordinated(fetch)
            if provider_id == SUPJAV_SITE_ID
            else fetch()
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
    if provider_id not in {
        JABLE_SITE_ID,
        SUPJAV_SITE_ID,
        MISSAV_SITE_ID,
        *CANDIDATE_WEB_SOURCE_IDS,
    }:
        raise WebDownloadProviderError(
            "Web download provider is invalid", code="configuration"
        )
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
    document = _bounded_get(
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
        if provider_id in CANDIDATE_WEB_SOURCE_IDS:
            return _capture_catalog_provider(
                provider_id, code, str(base_url or ""), variant="original",
                timeout_seconds=timeout_seconds, requested_height=None,
                quality_strategy="legacy",
            )
        if provider_id == JABLE_SITE_ID:
            return _capture_jable(
                code,
                str(base_url or ""),
                timeout_seconds=timeout_seconds,
                requested_height=None,
                quality_strategy="legacy",
            )
        if provider_id == SUPJAV_SITE_ID:
            return _capture_supjav(
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
        if source_id not in {
            JABLE_SITE_ID,
            SUPJAV_SITE_ID,
            MISSAV_SITE_ID,
            *CANDIDATE_WEB_SOURCE_IDS,
        }:
            raise ValueError("Web resource search source is invalid")
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
            _supjav_session()
            if self._source_id == SUPJAV_SITE_ID
            else _session()
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
                document = (
                    fetch_text(search_url, timeout=30.0, max_bytes=_MAX_PAGE_BYTES, allowed_origin=origin)
                    if self._source_id in CANDIDATE_WEB_SOURCE_IDS
                    else _bounded_get(session, search_url, 30.0, allowed_hosts={urlsplit(origin).hostname or ""})
                )
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
    if source_id in CANDIDATE_WEB_SOURCE_IDS:
        return catalog_search_url(source_id, origin, query, page)
    if source_id == JABLE_SITE_ID:
        return (
            f"{origin}/search/?q={encoded}"
            if page == 1
            else f"{origin}/search/{encoded}/{page}/"
        )
    if source_id == SUPJAV_SITE_ID:
        return (
            f"{origin}/?s={encoded}"
            if page == 1
            else f"{origin}/page/{page}?s={encoded}"
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
    if source_id in CANDIDATE_WEB_SOURCE_IDS:
        try:
            parsed_page = parse_catalog_search(source_id, document, origin, page=page)
        except WebCatalogError as exc:
            raise _ResourceSearchPageError(retryable=exc.retryable) from exc
        exact_query = canonical_catalog_code(work.query, max_length=32)
        grouped: dict[str, tuple[str, str, set[MissavVariant]]] = {}
        for entry in parsed_page.entries:
            normalized = normalize_catalog_code(entry.code, max_length=32)
            if normalized is None or (exact_query is not None and normalized[1] != exact_query):
                continue
            if not _resource_code_in_range(normalized[1], work):
                continue
            existing = grouped.get(normalized[1])
            if existing is None:
                grouped[normalized[1]] = (normalized[0], entry.title, {entry.variant})
            else:
                existing[2].add(entry.variant)
        return tuple(
            ResourceSearchItem(code, tuple(v for v in WEB_DOWNLOAD_VARIANTS if v in variants), _safe_resource_title(title))
            for code, title, variants in grouped.values()
        ), parsed_page.total_pages
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
    elif source_id == SUPJAV_SITE_ID:
        for post in result_cards:
            anchor = post.select_one('h3 a[href$=".html"]')
            if anchor is None:
                continue
            target = urljoin(origin, str(anchor.get("href") or ""))
            if (
                _origin(target) != origin
                or re.fullmatch(r"/\d+\.html", urlsplit(target).path) is None
            ):
                continue
            title = str(anchor.get("title") or anchor.get_text(" ", strip=True))
            normalized = _first_catalog_code(title)
            if normalized is None:
                continue
            candidates.append(
                (normalized[0], title, (DEFAULT_WEB_DOWNLOAD_VARIANT,))
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
        elif source_id == SUPJAV_SITE_ID:
            match = re.search(r"/page/([1-9][0-9]{0,2})/?$", parsed.path)
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


def _capture_catalog_provider(
    source_id: str, code: object, base_url: str, *, variant: str,
    timeout_seconds: float, requested_height: object | None, quality_strategy: object,
) -> ProviderManifest:
    origin = _safe_origin(base_url, expected_profile=source_id)
    deadline = time.monotonic() + max(1.0, min(timeout_seconds, 120.0))
    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise WebDownloadProviderError("Web catalog probe timed out", code="timeout")
        return value
    wanted = canonical_catalog_code(code)
    if wanted is None:
        raise WebDownloadProviderError("Web provider code is invalid", code="configuration")
    try:
        document = fetch_text(
            catalog_search_url(source_id, origin, str(code), 1),
            timeout=remaining(), max_bytes=_MAX_PAGE_BYTES, allowed_origin=origin,
        )
        page = parse_catalog_search(source_id, document, origin)
        entries = [entry for entry in page.entries if canonical_catalog_code(entry.code) == wanted and entry.variant == variant]
        if not entries:
            raise WebDownloadProviderNotFound()
        def select(url: str, body: str) -> tuple[str, int | None]:
            if "#EXT-X-STREAM-INF:" not in body:
                return url, None
            return _select_media_playlist(url, body, requested_height=requested_height, quality_strategy=quality_strategy)
        failure_code = "media_unverified"
        for entry in entries[:3]:
            try:
                detail = fetch_text(entry.detail_url, timeout=remaining(), max_bytes=_MAX_PAGE_BYTES, allowed_origin=origin)
                media = probe_catalog_media(
                    source_id, entry, detail, timeout=remaining(),
                    unpack=_unpack_js_eval, select_playlist=select,
                )
            except (WebCatalogError, FetchError) as exc:
                failure_code = str(getattr(exc, "code", "upstream_unavailable"))
                continue
            if not media.full_length_verified:
                failure_code = "full_length_unverified"
                continue
            return ProviderManifest(
                source_id, media.url, {"Referer": media.referer}, media.page_url,
                media.selected_height,
            )
        raise WebDownloadProviderError(
            "Web source media or complete-film duration could not be verified",
            code=failure_code,
        )
    except WebCatalogError as exc:
        raise WebDownloadProviderError("Web catalog media could not be verified", code=exc.code) from exc


def _capture_supjav(
    code: object,
    base_url: str,
    *,
    timeout_seconds: float,
    requested_height: object | None,
    quality_strategy: object,
    cancel_event: threading.Event | None = None,
) -> ProviderManifest:
    """Capture SupJav without allowing concurrent retry waves.

    SupJav starts returning a challenge page when several Web workers reach
    discovery together.  Serialize the complete capture, leave a small gap
    after every attempt, and give an observed challenge a longer quiet
    window.  The lock deliberately covers the wait and the network exchange:
    only one caller may become the next probe after a cooldown.
    """

    return _run_supjav_coordinated(
        lambda: _capture_supjav_uncoordinated(
            code,
            base_url,
            timeout_seconds=timeout_seconds,
            requested_height=requested_height,
            quality_strategy=quality_strategy,
            cancel_event=cancel_event,
        ),
        cancel_event=cancel_event,
    )


def _run_supjav_coordinated(
    operation: Callable[[], _CoordinatedResult],
    *,
    cancel_event: threading.Event | None = None,
) -> _CoordinatedResult:
    """Run any SupJav request wave under the shared pacing coordinator."""

    while not _SUPJAV_CAPTURE_LOCK.acquire(timeout=0.1):
        _raise_if_provider_cancelled(cancel_event)
    try:
        with _locked_supjav_capture_state(cancel_event=cancel_event) as state:
            _raise_if_provider_cancelled(cancel_event)
            delay = min(
                _SUPJAV_CHALLENGE_COOLDOWN_SECONDS,
                max(0.0, _read_supjav_next_at(state) - time.time()),
            )
            _wait_for_provider_delay(delay, cancel_event)
            _raise_if_provider_cancelled(cancel_event)
            _write_supjav_next_at(
                state,
                time.time() + _SUPJAV_CAPTURE_SPACING_SECONDS,
            )
            try:
                result = operation()
            except WebDownloadProviderError as exc:
                cooldown = (
                    _SUPJAV_CHALLENGE_COOLDOWN_SECONDS
                    if exc.code in {"challenge_active", "rate_limited"}
                    else _SUPJAV_CAPTURE_SPACING_SECONDS
                )
                _write_supjav_next_at(state, time.time() + cooldown)
                raise
            except BaseException:
                _write_supjav_next_at(
                    state,
                    time.time() + _SUPJAV_CAPTURE_SPACING_SECONDS,
                )
                raise
            _write_supjav_next_at(
                state,
                time.time() + _SUPJAV_CAPTURE_SPACING_SECONDS,
            )
            return result
    finally:
        _SUPJAV_CAPTURE_LOCK.release()


@contextmanager
def _locked_supjav_capture_state(
    *, cancel_event: threading.Event | None = None
) -> Iterator[BinaryIO]:
    path = _SUPJAV_CAPTURE_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags | nofollow, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WebDownloadProviderError(
                "SupJav capture coordinator is invalid", code="configuration"
            )
        state = os.fdopen(descriptor, "r+b", closefd=False)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                state.seek(0, os.SEEK_END)
                if state.tell() == 0:
                    state.write(b"0\n")
                    state.flush()
                while not locked:
                    state.seek(0)
                    try:
                        msvcrt.locking(state.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                    except OSError as exc:
                        if getattr(exc, "errno", None) != 13:
                            raise
                        _wait_for_provider_delay(0.1, cancel_event)
            else:
                import fcntl

                while not locked:
                    try:
                        fcntl.flock(state.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError:
                        _wait_for_provider_delay(0.1, cancel_event)
            try:
                yield state
            finally:
                if locked and os.name == "nt":
                    import msvcrt

                    state.seek(0)
                    msvcrt.locking(state.fileno(), msvcrt.LK_UNLCK, 1)
                elif locked:
                    import fcntl

                    fcntl.flock(state.fileno(), fcntl.LOCK_UN)
        finally:
            state.close()
    finally:
        os.close(descriptor)


def _read_supjav_next_at(state: BinaryIO) -> float:
    state.seek(0)
    raw = state.read(64).strip()
    try:
        value = float(raw or b"0")
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value >= 0.0 else 0.0


def _write_supjav_next_at(state: BinaryIO, value: float) -> None:
    state.seek(0)
    state.write(f"{max(0.0, value):.6f}\n".encode("ascii"))
    state.truncate()
    state.flush()
    os.fsync(state.fileno())


def _wait_for_provider_delay(
    delay: float, cancel_event: threading.Event | None
) -> None:
    if delay <= 0:
        return
    if cancel_event is not None:
        if cancel_event.wait(delay):
            _raise_if_provider_cancelled(cancel_event)
        return
    time.sleep(delay)


def _capture_supjav_uncoordinated(
    code: object,
    base_url: str,
    *,
    timeout_seconds: float,
    requested_height: object | None,
    quality_strategy: object,
    cancel_event: threading.Event | None = None,
) -> ProviderManifest:
    origin = _safe_origin(base_url, expected_profile=SUPJAV_SITE_ID)
    session = _supjav_session()
    _raise_if_provider_cancelled(cancel_event)
    search_url = f"{origin}/?s={quote(str(code), safe='')}"
    search = _bounded_get(
        session,
        search_url,
        timeout_seconds,
        allowed_hosts={urlsplit(origin).hostname or ""},
    )
    detail_url = _supjav_exact_result(search, origin, code)
    _raise_if_provider_cancelled(cancel_event)
    detail = _bounded_get(
        session,
        detail_url,
        timeout_seconds,
        allowed_hosts={urlsplit(origin).hostname or ""},
    )
    soup = BeautifulSoup(detail, "html.parser")
    _raise_if_provider_cancelled(cancel_event)
    servers: dict[str, str] = {}
    for anchor in soup.select("a.btn-server[data-link]"):
        name = anchor.get_text(" ", strip=True).upper()
        token = str(anchor.get("data-link") or "")
        if name in {"FST", "ST"} and re.fullmatch(r"[A-Za-z0-9]{32,512}", token):
            servers.setdefault(name, token)
    if not servers:
        raise WebDownloadProviderNotFound()

    fst_error: WebDownloadProviderError | None = None
    fst = servers.get("FST")
    if fst is not None:
        try:
            _raise_if_provider_cancelled(cancel_event)
            return _capture_supjav_fst(
                session,
                fst,
                origin=origin,
                detail_url=detail_url,
                timeout_seconds=timeout_seconds,
                requested_height=requested_height,
                quality_strategy=quality_strategy,
            )
        except WebDownloadProviderError as exc:
            fst_error = exc
        except Exception:
            fst_error = WebDownloadProviderError(
                "SupJav FST source is temporarily unavailable",
                code="upstream_unavailable",
            )

    # Streamtape is a progressive MP4 fallback.  It is intentionally used
    # only for legacy/automatic quality because the source exposes no trusted
    # height label.  The worker downloads it through the same allowlisted
    # curl-cffi handler and never hands the network URL to ffmpeg.
    streamtape = servers.get("ST")
    if (
        streamtape is not None
        and requested_height is None
        and str(quality_strategy) == "legacy"
    ):
        try:
            _raise_if_provider_cancelled(cancel_event)
            return _capture_supjav_streamtape(
                session,
                streamtape,
                origin=origin,
                detail_url=detail_url,
                timeout_seconds=timeout_seconds,
            )
        except WebDownloadProviderError:
            pass
        except Exception:
            pass
    if fst_error is not None:
        raise fst_error
    raise WebDownloadProviderError(
        "SupJav sources are temporarily unavailable", code="upstream_unavailable"
    )


def _raise_if_provider_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise WebDownloadProviderError(
            "Web source discovery was cancelled", code="cancelled"
        )


def _capture_supjav_fst(
    session: object,
    token: str,
    *,
    origin: str,
    detail_url: str,
    timeout_seconds: float,
    requested_height: object | None,
    quality_strategy: object,
) -> ProviderManifest:
    gateway = f"https://{_SUPJAV_GATEWAY_HOST}/supjav.php?c={token[::-1]}"
    embed, final_url = _bounded_get_with_redirects(
        session,
        gateway,
        timeout_seconds,
        allowed_hosts={_SUPJAV_GATEWAY_HOST, *_SUPJAV_EMBED_HOSTS},
        headers={"Referer": f"{origin}/"},
    )
    manifest_url = _extract_packed_manifest(embed)
    media_host = (urlsplit(manifest_url).hostname or "").lower()
    if not any(
        _host_matches(media_host, suffix)
        for suffix in ("cdn-centaurus.com", "premilkyway.com")
    ):
        raise WebDownloadProviderError(
            "SupJav media host was rejected", code="host_policy"
        )
    referer = _origin(final_url)
    master = _bounded_get(
        session,
        manifest_url,
        timeout_seconds,
        allowed_hosts={media_host},
        headers={"Referer": final_url, "Origin": referer},
        max_bytes=MAX_MANIFEST_BYTES,
    )
    media_url, selected_height = _select_media_playlist(
        manifest_url,
        master,
        requested_height=requested_height,
        quality_strategy=quality_strategy,
    )
    return ProviderManifest(
        provider=SUPJAV_SITE_ID,
        url=media_url,
        headers={"Referer": final_url, "Origin": referer},
        page_url=detail_url,
        selected_height=selected_height,
    )


def _capture_supjav_streamtape(
    session: object,
    token: str,
    *,
    origin: str,
    detail_url: str,
    timeout_seconds: float,
) -> ProviderManifest:
    gateway = f"https://{_SUPJAV_GATEWAY_HOST}/supjav.php?c={token[::-1]}"
    embed, final_url = _bounded_get_with_redirects(
        session,
        gateway,
        timeout_seconds,
        allowed_hosts={_SUPJAV_GATEWAY_HOST, *_SUPJAV_STREAMTAPE_HOSTS},
        headers={"Referer": f"{origin}/"},
    )
    direct_url = _extract_streamtape_direct_url(embed)
    direct_host = (urlsplit(direct_url).hostname or "").lower()
    if direct_host not in _SUPJAV_STREAMTAPE_HOSTS:
        raise WebDownloadProviderError(
            "SupJav progressive host was rejected", code="host_policy"
        )
    return ProviderManifest(
        provider=SUPJAV_SITE_ID,
        url=direct_url,
        headers={"Referer": final_url},
        page_url=detail_url,
        selected_height=None,
        media_kind="progressive",
    )


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
        detail = _bounded_get(
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
        search = _bounded_get(
            session,
            f"{origin}/search/?q={quote(str(code), safe='')}",
            timeout_seconds,
            allowed_hosts={host},
        )
        detail_url = _jable_exact_result(search, origin, code)
        detail = _bounded_get(
            session, detail_url, timeout_seconds, allowed_hosts={host}
        )
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


class _SupjavAdaptiveSession:
    """Retry only challenge responses with a different TLS/browser profile."""

    def __init__(self) -> None:
        first = _SUPJAV_IMPERSONATE_PROFILES[0]
        self._sessions: dict[str, object] = {
            first: _session(impersonate=first)
        }
        self._preferred_index = 0

    def get(self, url: str, **kwargs: object) -> object:
        last_response: object | None = None
        profile_count = len(_SUPJAV_IMPERSONATE_PROFILES)
        for offset in range(profile_count):
            index = (self._preferred_index + offset) % profile_count
            profile = _SUPJAV_IMPERSONATE_PROFILES[index]
            session = self._sessions.get(profile)
            if session is None:
                session = _session(impersonate=profile)
                self._sessions[profile] = session
            response = session.get(url, **kwargs)
            last_response = response
            status_code = int(getattr(response, "status_code", 0) or 0)
            headers = getattr(response, "headers", {})
            mitigated = str(
                headers.get("cf-mitigated", "")
                if hasattr(headers, "get")
                else ""
            ).casefold()
            if status_code not in {403, 429, 503} and "challenge" not in mitigated:
                self._preferred_index = index
                return response
        if last_response is None:  # pragma: no cover - profiles are constant.
            raise WebDownloadProviderError(
                "SupJav browser profiles are unavailable", code="configuration"
            )
        return last_response


def _supjav_session() -> _SupjavAdaptiveSession:
    return _SupjavAdaptiveSession()


def _bounded_get(
    session: object,
    url: str,
    timeout_seconds: float,
    *,
    allowed_hosts: set[str],
    headers: dict[str, str] | None = None,
    max_bytes: int = _MAX_PAGE_BYTES,
) -> str:
    response = session.get(
        url,
        headers=headers or {},
        timeout=max(5.0, min(float(timeout_seconds), 120.0)),
        allow_redirects=False,
    )
    if int(response.status_code) in _REDIRECT_STATUSES:
        raise WebDownloadProviderError(
            "Unexpected provider redirect", code="redirect_policy"
        )
    if int(response.status_code) == 404:
        raise WebDownloadProviderNotFound()
    if int(response.status_code) in {403, 429, 503}:
        raise WebDownloadProviderError(
            "Provider challenge is active", code="challenge_active"
        )
    if int(response.status_code) != 200:
        raise WebDownloadProviderError(
            "Provider request failed", code="upstream_unavailable"
        )
    final_host = (urlsplit(str(response.url)).hostname or "").lower()
    if final_host not in allowed_hosts or not PublicHostResolver(max_hosts=1).is_public(
        final_host
    ):
        raise WebDownloadProviderError(
            "Provider response host was rejected", code="host_policy"
        )
    body = bytes(response.content)
    if not body or len(body) > max_bytes:
        raise WebDownloadProviderError(
            "Provider response size was rejected", code="response_invalid"
        )
    return body.decode("utf-8", errors="replace")


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
        host = (urlsplit(current).hostname or "").lower()
        if host not in allowed_hosts or not PublicHostResolver(max_hosts=1).is_public(
            host
        ):
            raise WebDownloadProviderError(
                "Provider redirect host was rejected", code="host_policy"
            )
        response = session.get(
            current,
            headers=headers,
            timeout=max(5.0, min(float(timeout_seconds), 120.0)),
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


def _supjav_exact_result(document: str, origin: str, code: object) -> str:
    wanted = canonical_catalog_code(code)
    if wanted is None:
        raise WebDownloadProviderNotFound()
    soup = BeautifulSoup(document, "html.parser")
    candidates: list[str] = []
    for post in soup.select("div.post"):
        anchor = post.select_one('a[href*=".html"]')
        if anchor is None:
            continue
        title = html.unescape(
            str(anchor.get("title") or anchor.get_text(" ", strip=True))
        )
        if wanted not in _catalog_codes_in_text(title):
            continue
        target = urljoin(origin, str(anchor.get("href") or ""))
        parsed = urlsplit(target)
        if _origin(target) == origin and re.fullmatch(r"/\d+\.html", parsed.path):
            candidates.append(target)
    if not candidates:
        raise WebDownloadProviderNotFound()
    return candidates[0]


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


def _extract_packed_manifest(document: str) -> str:
    for script in re.findall(
        r"<script[^>]*>(.*?)</script>", document, re.DOTALL | re.IGNORECASE
    ):
        if "eval(function" not in script:
            continue
        unpacked = _unpack_js_eval(script)
        if not unpacked:
            continue
        match = re.search(r"https?://[^'\"\\;\s]+\.m3u8[^'\"\\;\s]*", unpacked)
        if match:
            return html.unescape(match.group(0))
    raise WebDownloadProviderNotFound()


def _extract_streamtape_direct_url(document: str) -> str:
    """Resolve Streamtape's bounded static ``robotlink`` expression."""

    match = re.search(
        r"getElementById\(\s*['\"]robotlink['\"]\s*\)\.innerHTML\s*=\s*"
        r"['\"]([^'\"]*)['\"]\s*\+\s*(?:['\"]{2}\s*\+\s*)?"
        r"\(\s*['\"]([^'\"]*)['\"]\s*\)((?:\.substring\(\s*\d+\s*\))+)",
        document,
    )
    if match is None:
        raise WebDownloadProviderError(
            "SupJav progressive source was not found", code="response_invalid"
        )
    suffix = match.group(2)
    for raw_offset in re.findall(r"substring\(\s*(\d+)\s*\)", match.group(3)):
        offset = int(raw_offset)
        if offset > len(suffix):
            raise WebDownloadProviderError(
                "SupJav progressive source was rejected", code="response_invalid"
            )
        suffix = suffix[offset:]
    link = (match.group(1) + suffix).lstrip("/")
    if len(link) > 8192 or "get_video" not in link:
        raise WebDownloadProviderError(
            "SupJav progressive source was rejected", code="response_invalid"
        )
    candidate = f"https://{link}"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise WebDownloadProviderError(
            "SupJav progressive source was rejected", code="response_invalid"
        )
    return candidate


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
        SUPJAV_SITE_ID: "SupJav",
        "kissjav": "KissJAV",
        "javnoni": "JAV-NONI",
    }.get(provider, "Web")
