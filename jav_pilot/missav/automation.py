"""Playwright flows behind the public MissAV operations.

Every flow is written once against a caller-owned page (``*_on_page``) so the
standalone worker processes and the trusted broker page run identical
navigation and parsing logic. The standalone entry points only add a
short-lived, isolated browser context around those flows.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterator, Mapping

from ..net.network_guard import PublicHostResolver
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
)
from .errors import MissavError, MissavNotFound, MissavTransientError
from .models import (
    ManifestRequest,
    MissavSeriesDiscovery,
)
from .navigation import (
    goto_with_transport_retry,
    locate_exact_detail,
    looks_like_missav_challenge,
    navigation_failure_code,
    navigation_succeeded,
    prepare_exact_detail_capture,
    raise_capture_cancelled,
    remove_page_listener,
    wait_for_challenge_clear,
)
from .parsing import (
    MissavSeriesParser,
    exact_series_code,
    extract_description_from_html,
    series_discovery_result,
)
from .player import (
    ManifestCaptureState,
    PlayerQualityChoice,
    discover_player_qualities,
    resolve_player_quality_choices,
    select_player_quality,
    start_player,
    stop_player_source,
    wait_for_manifest_count,
    wait_for_manifest_stability,
    wait_for_verified_manifest,
)
from .routing import (
    BrowserMediaGate,
    deny_routed_web_socket,
    route_metadata_request,
    route_request,
)
from .site import (
    MAX_SERIES_CANDIDATES,
    MAX_SERIES_PAGES,
    MEDIA_HOST,
    PLAYER_ASSET_HOST,
    QualityStrategy,
    current_missav_site,
    remaining_milliseconds,
)
from .urls import (
    require_exact_detail_page,
    require_missav_page,
    require_series_search_page,
    series_search_path,
    series_search_url,
)

# The challenge broker may touch MissAV, the player CDN, the media host and
# the Cloudflare challenge hosts during one operation.
_RESOLVER_MAX_HOSTS = 16
_LEGACY_MANIFEST_SETTLE_SECONDS = 1.5


def browser_proxy_server() -> str | None:
    """Return the outbound proxy every MissAV browser must use, if any."""

    return (
        os.environ.get("JAV_PILOT_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("ALL_PROXY")
        or None
    )


# ---------------------------------------------------------------------------
# Flows on a caller-owned page
# ---------------------------------------------------------------------------


def fetch_description_on_page(
    page: object,
    search_code: str,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    cancel_event: threading.Event | None = None,
) -> str | None:
    """Locate one exact work and read its optional description meta tags.

    ``locate_exact_detail`` only returns once the page itself shows the
    validated detail route (after any challenge), so the description is read
    from that document instead of loading the same page a second time.
    """

    raise_capture_cancelled(cancel_event)
    detail_url = locate_exact_detail(
        page,
        search_code,
        canonical_code,
        deadline,
        variant=variant,
        cancel_event=cancel_event,
    )
    if detail_url is None:
        raise MissavNotFound("No exact MissAV result was found for this catalog code")
    require_exact_detail_page(page.url, canonical_code, variant=variant)
    return extract_description_from_html(page.content())


def discover_qualities_on_page(
    page: object,
    search_code: str,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    media_gate: BrowserMediaGate | None,
    probe_parent: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[int, ...]:
    capture_state = ManifestCaptureState()
    request_callback = capture_state.capture_request
    try:
        detail_url = prepare_exact_detail_capture(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=variant,
            request_handler=request_callback,
            cancel_event=cancel_event,
        )
        capture_state.detail_page_url = detail_url
        start_player(page, deadline)
        return discover_player_qualities(
            page,
            deadline,
            capture_state=capture_state,
            probe_parent=probe_parent,
            media_gate=media_gate,
        )
    finally:
        stop_player_source(page)
        remove_page_listener(page, "request", request_callback)


def capture_manifest_on_prepared_page(
    page: object,
    search_code: str,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant,
    quality_strategy: QualityStrategy,
    requested_height: int | None,
    media_gate: BrowserMediaGate,
    probe_parent: Path | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str | None], None] | None = None,
) -> ManifestRequest:
    """Capture an in-memory HLS request for one exact work on *page*.

    The caller owns the page, the media gate policy and the mapping of raw
    browser failures; this function never serializes the captured request.
    """

    def stage(name: str | None) -> None:
        if stage_callback is not None:
            stage_callback(name)

    capture_state = ManifestCaptureState()
    request_callback = capture_state.capture_request
    try:
        raise_capture_cancelled(cancel_event)
        set_timeout = getattr(page, "set_default_timeout", None)
        if callable(set_timeout):
            set_timeout(remaining_milliseconds(deadline))
        detail_url = prepare_exact_detail_capture(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=variant,
            request_handler=request_callback,
            cancel_event=cancel_event,
            stage_callback=stage_callback,
        )
        capture_state.detail_page_url = detail_url
        raise_capture_cancelled(cancel_event)
        stage("start_player")
        start_player(page, deadline)
        raise_capture_cancelled(cancel_event)
        stage("manifest_wait")
        if quality_strategy == "legacy":
            return _wait_for_legacy_manifest(
                page, capture_state, deadline, cancel_event=cancel_event
            )

        initial_manifests = capture_state.manifests_for(capture_state.generation)
        wait_for_manifest_count(page, initial_manifests, 1, deadline)
        wait_for_manifest_stability(page, initial_manifests, deadline, seconds=0.5)
        stage("quality_resolution")
        resolved_choices: Mapping[int, PlayerQualityChoice] | None = None
        selected_height = requested_height
        if quality_strategy == "highest":
            resolved_choices = resolve_player_quality_choices(
                page,
                deadline,
                capture_state=capture_state,
                probe_parent=probe_parent,
                media_gate=media_gate,
            )
            eligible_heights = tuple(
                height
                for height in resolved_choices
                if requested_height is None or height <= requested_height
            )
            if not eligible_heights:
                raise MissavError("MissAV requested quality is unavailable")
            selected_height = max(eligible_heights)
        if selected_height is None:
            raise MissavError("MissAV selected quality requires a height")
        selection = select_player_quality(
            page,
            selected_height,
            deadline,
            manifest_generation=capture_state.generation,
            begin_manifest_generation=capture_state.begin_generation,
            capture_state=capture_state,
            probe_parent=probe_parent,
            media_gate=media_gate,
            resolved_choices=resolved_choices,
        )
        selected = selection.verified_manifest
        if selected is None:
            selected = wait_for_verified_manifest(
                page,
                capture_state.manifests_for(selection.manifest_generation),
                selection.selected_height,
                deadline,
                probe_parent=probe_parent,
            )
        return ManifestRequest(
            url=selected.url,
            headers=dict(selected.headers),
            page_url=selected.page_url,
            selected_height=selection.selected_height,
        )
    finally:
        stage(None)
        stop_player_source(page)
        remove_page_listener(page, "request", request_callback)


def _wait_for_legacy_manifest(
    page: object,
    capture_state: ManifestCaptureState,
    deadline: float,
    *,
    cancel_event: threading.Event | None,
) -> ManifestRequest:
    while time.monotonic() < deadline:
        raise_capture_cancelled(cancel_event)
        if (
            capture_state.manifests
            and capture_state.first_manifest_at is not None
            and time.monotonic() - capture_state.first_manifest_at
            >= _LEGACY_MANIFEST_SETTLE_SECONDS
        ):
            break
        page.wait_for_timeout(min(250, remaining_milliseconds(deadline)))
    if not capture_state.manifests:
        raise MissavError("MissAV did not expose a downloadable HLS stream")
    return capture_state.manifests[-1]


def load_series_search_page(
    page: object,
    display_prefix: str,
    canonical_prefix: str,
    *,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
    page_number: int,
    deadline: float,
    cancel_event: threading.Event,
) -> MissavSeriesParser:
    response = goto_with_transport_retry(
        page,
        series_search_url(display_prefix, page=page_number),
        deadline,
        cancel_event=cancel_event,
        timeout_cap_ms=10_000,
    )
    require_missav_page(page.url)
    html = page.content()
    was_challenged = looks_like_missav_challenge(html)
    if was_challenged:
        wait_for_challenge_clear(page, deadline, cancel_event=cancel_event)
        html = page.content()
    require_missav_page(page.url)
    require_series_search_page(
        page.url,
        display_prefix=display_prefix,
        page=page_number,
    )
    if not navigation_succeeded(response) and not was_challenged:
        raise MissavTransientError(
            "MissAV series search is temporarily unavailable",
            code=navigation_failure_code(response),
        )
    parser = MissavSeriesParser(
        display_prefix=display_prefix,
        canonical_prefix=canonical_prefix,
        suffix_width=suffix_width,
        start=start,
        end=end,
        search_path=series_search_path(display_prefix),
    )
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise MissavError(
            "MissAV series results could not be parsed",
            code="parse_drift",
        ) from exc
    return parser


def discover_series_on_page(
    page: object,
    display_prefix: str,
    canonical_prefix: str,
    *,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
    max_codes: int,
    deadline: float,
    cancel_event: threading.Event,
) -> MissavSeriesDiscovery:
    if cancel_event.is_set():
        return MissavSeriesDiscovery(codes=(), complete=False)
    exact_code = exact_series_code(
        display_prefix,
        suffix_width=suffix_width,
        start=start,
        end=end,
    )
    if exact_code is not None:
        return _discover_exact_series_code(
            page,
            exact_code,
            deadline=deadline,
            cancel_event=cancel_event,
        )

    found: dict[str, tuple[int, str, set[MissavVariant]]] = {}
    page_count = 1
    total_candidates = 0
    complete = True
    page_number = 1
    while page_number <= page_count:
        if cancel_event.is_set():
            return series_discovery_result(found, complete=False)
        if time.monotonic() >= deadline:
            raise MissavTransientError(
                "MissAV series discovery timed out",
                code="navigation_timeout",
            )
        try:
            parser = load_series_search_page(
                page,
                display_prefix,
                canonical_prefix,
                suffix_width=suffix_width,
                start=start,
                end=end,
                page_number=page_number,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except MissavError:
            raise
        except Exception:
            if cancel_event.is_set():
                return series_discovery_result(found, complete=False)
            raise
        if cancel_event.is_set():
            return series_discovery_result(found, complete=False)
        if page_number == 1:
            if parser.max_page > MAX_SERIES_PAGES:
                complete = False
            page_count = min(parser.max_page, MAX_SERIES_PAGES)
        total_candidates += parser.candidate_count
        candidates_exhausted = (
            total_candidates >= MAX_SERIES_CANDIDATES or parser.candidate_limit_hit
        )
        if candidates_exhausted:
            complete = False
        result_limit_reached = False
        for code, canonical_code, suffix, variant in parser.candidates:
            existing = found.get(canonical_code)
            if existing is None:
                if len(found) >= max_codes:
                    result_limit_reached = True
                    continue
                found[canonical_code] = (suffix, code, {variant})
            else:
                existing[2].add(variant)
        if result_limit_reached or len(found) >= max_codes:
            return series_discovery_result(found, complete=False)
        if candidates_exhausted:
            break
        page_number += 1
    return series_discovery_result(found, complete=complete)


def _discover_exact_series_code(
    page: object,
    exact_code: tuple[str, str],
    *,
    deadline: float,
    cancel_event: threading.Event,
) -> MissavSeriesDiscovery:
    """Probe every variant route of one exact code.

    A one-code range is common for retry/repair requests.  A later optional
    variant must not erase an already verified exact result: variant
    failures keep the aggregate incomplete while the remaining variants are
    still probed.
    """

    display_code, canonical_code = exact_code
    variants: list[MissavVariant] = []
    variant_error: MissavError | None = None

    def partial() -> MissavSeriesDiscovery:
        if not variants:
            return MissavSeriesDiscovery(codes=(), complete=False)
        return MissavSeriesDiscovery(
            codes=(display_code,),
            complete=False,
            variants_by_code=((display_code, tuple(variants)),),
        )

    for variant in WEB_DOWNLOAD_VARIANTS:
        if cancel_event.is_set():
            return partial()
        try:
            detail_url = locate_exact_detail(
                page,
                display_code,
                canonical_code,
                deadline,
                variant=variant,
                cancel_event=cancel_event,
                navigation_timeout_cap_ms=10_000,
                # Optional variants have exact, stable suffix routes, so a
                # direct 404 is authoritative.  The original route may be
                # dynamically prefixed; keep the exact search fallback that
                # locates that trusted route.
                direct_miss_is_final=variant != DEFAULT_WEB_DOWNLOAD_VARIANT,
                accept_routed_challenge=True,
            )
        except MissavNotFound:
            detail_url = None
        except MissavError as exc:
            if cancel_event.is_set():
                raise
            variant_error = variant_error or exc
            continue
        if cancel_event.is_set():
            return partial()
        if detail_url is not None:
            variants.append(variant)
    if not variants:
        if variant_error is not None:
            raise variant_error
        return MissavSeriesDiscovery(codes=(), complete=True)
    return MissavSeriesDiscovery(
        codes=(display_code,),
        complete=variant_error is None,
        variants_by_code=((display_code, tuple(variants)),),
    )


# ---------------------------------------------------------------------------
# Standalone entry points with a short-lived isolated browser
# ---------------------------------------------------------------------------


def _require_public_hosts(hosts: tuple[str, ...], resolver: PublicHostResolver) -> None:
    if not all(resolver.is_public(host) for host in hosts):
        raise MissavError("MissAV hosts must resolve to public addresses")


def _launch_isolated_context(playwright: object, user_data_dir: str) -> object:
    launch_options: dict[str, object] = {
        "user_data_dir": user_data_dir,
        "headless": False,
        "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
        "viewport": {"width": 1280, "height": 900},
        "service_workers": "block",
        "accept_downloads": False,
    }
    proxy = browser_proxy_server()
    if proxy:
        launch_options["proxy"] = {"server": proxy}
    chromium = playwright.chromium  # type: ignore[attr-defined]
    channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
    if channel is None:
        return chromium.launch_persistent_context(**launch_options)
    try:
        return chromium.launch_persistent_context(channel=channel, **launch_options)
    except Exception:  # noqa: BLE001 - bundled Chromium is the fallback.
        return chromium.launch_persistent_context(**launch_options)


@contextmanager
def _isolated_browser_context(
    purpose: str,
    *,
    route: Callable[[object], None],
    profile_parent: Path | None = None,
) -> Iterator[object]:
    """Yield a routed, single-use browser context with a temporary profile.

    Cleanup is best effort and never re-raises: Playwright cleanup errors can
    contain the current (signed) URL or the profile path.
    """

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError(f"Playwright is unavailable for MissAV {purpose}") from exc

    playwright = None
    context = None
    temporary_directory: TemporaryDirectory[str] | None = None
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(
            prefix=".missav-browser-",
            dir=str(profile_parent) if profile_parent is not None else None,
        )
        context = _launch_isolated_context(playwright, temporary_directory.name)
        context.route("**/*", route)  # type: ignore[attr-defined]
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)
        yield context
    finally:
        for cleanup in (
            getattr(context, "close", None),
            getattr(playwright, "stop", None),
            getattr(temporary_directory, "cleanup", None),
        ):
            if cleanup is None:
                continue
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - cleanup must not leak page state.
                pass


def _primary_page(context: object, deadline: float) -> object:
    pages = tuple(getattr(context, "pages", ()) or ())
    page = pages[0] if pages else context.new_page()  # type: ignore[attr-defined]
    page.set_default_timeout(remaining_milliseconds(deadline))  # type: ignore[attr-defined]
    return page


def _safe_browser_capture_error(error: BaseException) -> str:
    error_type = type(error).__name__.casefold()
    detail = str(error).casefold()
    if "timeout" in error_type or "timeout" in detail:
        return "MissAV browser capture timed out"
    if any(
        marker in error_type or marker in detail
        for marker in ("targetclosed", "crash", "browser closed")
    ):
        return "MissAV browser exited unexpectedly during capture"
    if "executable doesn't exist" in detail:
        return "MissAV browser runtime is unavailable"
    if any(marker in detail for marker in ("failed to launch", "browsertype.launch")):
        return "MissAV browser could not start"
    return "MissAV browser capture failed"


def _browser_runtime_is_missing(error: BaseException) -> bool:
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "executable doesn't exist",
            "playwright install",
            "browser executable was not found",
        )
    )


def fetch_description_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=_RESOLVER_MAX_HOSTS)
    _require_public_hosts((current_missav_site().host,), resolver)
    try:
        with _isolated_browser_context(
            "description",
            route=lambda route: route_metadata_request(route, resolver=resolver),
            profile_parent=profile_parent,
        ) as context:
            return fetch_description_on_page(
                _primary_page(context, deadline),
                search_code,
                canonical_code,
                deadline,
                variant=variant,
            )
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors can contain sensitive page state.
        raise MissavError("MissAV description fetch failed") from None


def capture_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    requested_height: int | None = None,
    quality_strategy: QualityStrategy,
) -> ManifestRequest:
    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=_RESOLVER_MAX_HOSTS)
    _require_public_hosts(
        (current_missav_site().host, MEDIA_HOST, PLAYER_ASSET_HOST), resolver
    )
    # The isolated capture keeps media traffic blocked except while a player
    # source is probed; the request event still exposes the manifest request.
    media_gate = BrowserMediaGate()
    try:
        with _isolated_browser_context(
            "capture",
            route=lambda route: route_request(
                route, resolver=resolver, media_gate=media_gate
            ),
            profile_parent=profile_parent,
        ) as context:
            return capture_manifest_on_prepared_page(
                _primary_page(context, deadline),
                search_code,
                canonical_code,
                deadline,
                variant=variant,
                quality_strategy=quality_strategy,
                requested_height=requested_height,
                media_gate=media_gate,
                probe_parent=profile_parent,
            )
    except MissavError:
        raise
    except Exception as exc:  # noqa: BLE001 - map browser failures without leaking URLs.
        raise MissavError(_safe_browser_capture_error(exc)) from None


def discover_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=_RESOLVER_MAX_HOSTS)
    _require_public_hosts(
        (current_missav_site().host, MEDIA_HOST, PLAYER_ASSET_HOST), resolver
    )
    media_gate = BrowserMediaGate()
    try:
        with _isolated_browser_context(
            "quality discovery",
            route=lambda route: route_request(
                route, resolver=resolver, media_gate=media_gate
            ),
            profile_parent=profile_parent,
        ) as context:
            return discover_qualities_on_page(
                _primary_page(context, deadline),
                search_code,
                canonical_code,
                deadline,
                variant=variant,
                media_gate=media_gate,
                probe_parent=profile_parent,
            )
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors may contain sensitive page state.
        raise MissavError("MissAV quality discovery failed") from None


def discover_series_with_playwright(
    display_prefix: str,
    canonical_prefix: str,
    *,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
    max_codes: int,
    timeout_seconds: float,
    cancel_event: threading.Event,
) -> MissavSeriesDiscovery:
    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=_RESOLVER_MAX_HOSTS)
    _require_public_hosts((current_missav_site().host, PLAYER_ASSET_HOST), resolver)
    try:
        with _isolated_browser_context(
            "series discovery",
            route=lambda route: route_metadata_request(route, resolver=resolver),
        ) as context:
            return discover_series_on_page(
                _primary_page(context, deadline),
                display_prefix,
                canonical_prefix,
                suffix_width=suffix_width,
                start=start,
                end=end,
                max_codes=max_codes,
                deadline=deadline,
                cancel_event=cancel_event,
            )
    except MissavError:
        raise
    except Exception as exc:  # noqa: BLE001 - browser errors can expose visited URLs.
        if _browser_runtime_is_missing(exc):
            raise MissavError("MissAV series browser runtime is unavailable") from None
        raise MissavTransientError(
            "MissAV series discovery failed",
            code="upstream_unavailable",
        ) from None
