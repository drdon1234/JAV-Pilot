"""Playwright flows behind the public MissAV operations."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Mapping

from ..net.network_guard import PublicHostResolver
from ..web_download.quality import WebDownloadVariantOption
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    WEB_DOWNLOAD_VARIANTS,
    MissavVariant,
)
from .errors import MissavError, MissavNotFound, MissavTransientError
from .models import (
    ManifestRequest,
    MissavResourceDiscovery,
    MissavResourceItem,
    MissavResourcePage,
    MissavResourceState,
    MissavSeriesDiscovery,
)
from .navigation import (
    goto_with_transport_retry,
    locate_exact_detail,
    looks_like_missav_challenge,
    navigation_failure_code,
    navigation_succeeded,
    prepare_exact_detail_capture,
    reload_exact_detail_for_capture,
    wait_for_challenge_clear,
)
from .parsing import (
    MissavResourceParser,
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
    MAX_RESOURCE_DELTA_ITEMS,
    MAX_RESOURCE_PAGES,
    MAX_SERIES_CANDIDATES,
    MAX_SERIES_PAGES,
    MEDIA_HOST,
    MISSAV_RESOURCE_CHALLENGE_WAIT_SECONDS,
    PLAYER_ASSET_HOST,
    QualityStrategy,
    current_missav_site,
    remaining_milliseconds,
    validate_quality_strategy,
)
from .urls import (
    require_missav_page,
    require_series_search_page,
    series_search_path,
    series_search_url,
)

def fetch_description_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError("Playwright is unavailable for MissAV description") from exc

    deadline = time.monotonic() + timeout_seconds
    # The challenge broker may touch MissAV, the player CDN, and the exact
    # Cloudflare challenge host during one operation.
    resolver = PublicHostResolver(max_hosts=16)
    missav_host = current_missav_site().host
    if not resolver.is_public(missav_host):
        raise MissavError("MissAV description host must resolve to a public address")

    context = None
    playwright = None
    temporary_directory: TemporaryDirectory[str] | None = None
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(
            prefix=".missav-description-",
            dir=str(profile_parent) if profile_parent is not None else None,
        )
        launch_options: dict[str, object] = {
            "user_data_dir": temporary_directory.name,
            "headless": False,
            "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
            "viewport": {"width": 1280, "height": 900},
            "service_workers": "block",
        }
        proxy = (
            os.environ.get("JAV_PILOT_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=channel,
                **launch_options,
            )
        except Exception:  # noqa: BLE001 - retry bundled Chromium when Chrome is absent.
            if not channel:
                raise
            context = playwright.chromium.launch_persistent_context(**launch_options)

        context.route(
            "**/*",
            lambda route: route_metadata_request(route, resolver=resolver),
        )
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(remaining_milliseconds(deadline))
        detail_url = locate_exact_detail(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=variant,
        )
        if detail_url is None:
            raise MissavNotFound(
                "No exact MissAV result was found for this catalog code"
            )
        reload_exact_detail_for_capture(
            page,
            detail_url,
            canonical_code,
            deadline,
            variant=variant,
        )
        return extract_description_from_html(page.content())
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors can contain sensitive page state.
        raise MissavError("MissAV description fetch failed") from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup errors can contain page state.
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - cleanup remains best effort.
            pass
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except Exception:  # noqa: BLE001 - never expose browser profile paths.
            pass


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


def capture_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    requested_height: int | None = None,
    quality_strategy: QualityStrategy | None = None,
) -> ManifestRequest:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError("Playwright is unavailable for MissAV capture") from exc

    effective_quality_strategy = validate_quality_strategy(
        quality_strategy,
        requested_height=requested_height,
    )
    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=16)
    missav_host = current_missav_site().host
    if not all(
        resolver.is_public(host)
        for host in (missav_host, MEDIA_HOST, PLAYER_ASSET_HOST)
    ):
        raise MissavError("MissAV capture hosts must resolve to public addresses")

    context = None
    playwright = None
    temporary_directory: TemporaryDirectory[str] | None = None
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(
            prefix=".missav-browser-",
            dir=str(profile_parent) if profile_parent is not None else None,
        )
        launch_options: dict[str, object] = {
            "user_data_dir": temporary_directory.name,
            "headless": False,
            "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
            "viewport": {"width": 1280, "height": 900},
            "service_workers": "block",
        }
        proxy = (
            os.environ.get("JAV_PILOT_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=channel, **launch_options
            )
        except Exception:  # noqa: BLE001 - retry bundled Chromium when Chrome is absent.
            if not channel:
                raise
            context = playwright.chromium.launch_persistent_context(**launch_options)

        media_gate = BrowserMediaGate()
        context.route(
            "**/*",
            lambda route: route_request(
                route,
                resolver=resolver,
                media_gate=media_gate,
            ),
        )
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(remaining_milliseconds(deadline))
        capture_state = ManifestCaptureState()

        detail_url = prepare_exact_detail_capture(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=variant,
            request_handler=capture_state.capture_request,
        )
        capture_state.detail_page_url = detail_url
        start_player(page, deadline)
        if effective_quality_strategy != "legacy":
            initial_manifests = capture_state.manifests_for(capture_state.generation)
            wait_for_manifest_count(page, initial_manifests, 1, deadline)
            wait_for_manifest_stability(
                page,
                initial_manifests,
                deadline,
                seconds=0.5,
            )
            resolved_choices: Mapping[int, PlayerQualityChoice] | None = None
            selected_height = requested_height
            if effective_quality_strategy == "highest":
                resolved_choices = resolve_player_quality_choices(
                    page,
                    deadline,
                    capture_state=capture_state,
                    probe_parent=profile_parent,
                    media_gate=media_gate,
                )
                if not resolved_choices:
                    raise MissavError("MissAV quality controls are unavailable")
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
                probe_parent=profile_parent,
                media_gate=media_gate,
                resolved_choices=resolved_choices,
            )
            if selection.verified_manifest is not None:
                selected = selection.verified_manifest
                return ManifestRequest(
                    url=selected.url,
                    headers=selected.headers,
                    page_url=selected.page_url,
                    selected_height=selection.selected_height,
                )
            selected_manifests = capture_state.manifests_for(
                selection.manifest_generation
            )
            try:
                selected = wait_for_verified_manifest(
                    page,
                    selected_manifests,
                    selection.selected_height,
                    deadline,
                    probe_parent=profile_parent,
                )
            finally:
                stop_player_source(page)
            return ManifestRequest(
                url=selected.url,
                headers=selected.headers,
                page_url=selected.page_url,
                selected_height=selection.selected_height,
            )
        while time.monotonic() < deadline:
            if (
                capture_state.manifests
                and capture_state.first_manifest_at is not None
                and time.monotonic() - capture_state.first_manifest_at >= 1.5
            ):
                break
            page.wait_for_timeout(min(250, remaining_milliseconds(deadline)))
        if not capture_state.manifests:
            raise MissavError("MissAV did not expose a downloadable HLS stream")
        return capture_state.manifests[-1]
    except MissavError:
        raise
    except Exception as exc:  # noqa: BLE001 - map browser failures without leaking URLs.
        raise MissavError(_safe_browser_capture_error(exc)) from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup errors can contain the current media URL.
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - cleanup remains best effort after context closure.
            pass
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except Exception:  # noqa: BLE001 - never expose a browser profile path in worker output.
            pass


def discover_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
) -> tuple[int, ...]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError("Playwright is unavailable for MissAV discovery") from exc

    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=16)
    missav_host = current_missav_site().host
    if not all(
        resolver.is_public(host)
        for host in (missav_host, MEDIA_HOST, PLAYER_ASSET_HOST)
    ):
        raise MissavError("MissAV discovery hosts must resolve to public addresses")

    context = None
    playwright = None
    temporary_directory: TemporaryDirectory[str] | None = None
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(
            prefix=".missav-browser-",
            dir=str(profile_parent) if profile_parent is not None else None,
        )
        launch_options: dict[str, object] = {
            "user_data_dir": temporary_directory.name,
            "headless": False,
            "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
            "viewport": {"width": 1280, "height": 900},
            "service_workers": "block",
        }
        proxy = (
            os.environ.get("JAV_PILOT_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=channel,
                **launch_options,
            )
        except Exception:  # noqa: BLE001 - retry bundled Chromium when Chrome is absent.
            if not channel:
                raise
            context = playwright.chromium.launch_persistent_context(**launch_options)

        media_gate = BrowserMediaGate()
        context.route(
            "**/*",
            lambda route: route_request(
                route,
                resolver=resolver,
                media_gate=media_gate,
            ),
        )
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(remaining_milliseconds(deadline))
        capture_state = ManifestCaptureState()
        detail_url = prepare_exact_detail_capture(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=variant,
            request_handler=capture_state.capture_request,
        )
        capture_state.detail_page_url = detail_url
        start_player(page, deadline)
        return discover_player_qualities(
            page,
            deadline,
            capture_state=capture_state,
            probe_parent=profile_parent,
            media_gate=media_gate,
        )
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors may contain sensitive page state.
        raise MissavError("MissAV quality discovery failed") from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup errors can contain page state.
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - cleanup remains best effort.
            pass
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except Exception:  # noqa: BLE001 - never expose browser profile paths.
            pass


def discover_options_with_playwright(
    search_code: str,
    canonical_code: str,
    timeout_seconds: float,
    *,
    profile_parent: Path | None = None,
) -> tuple[WebDownloadVariantOption, ...]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError("Playwright is unavailable for MissAV discovery") from exc

    deadline = time.monotonic() + timeout_seconds
    resolver = PublicHostResolver(max_hosts=16)
    missav_host = current_missav_site().host
    if not all(
        resolver.is_public(host)
        for host in (missav_host, MEDIA_HOST, PLAYER_ASSET_HOST)
    ):
        raise MissavError("MissAV discovery hosts must resolve to public addresses")

    context = None
    playwright = None
    temporary_directory: TemporaryDirectory[str] | None = None
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(
            prefix=".missav-browser-",
            dir=str(profile_parent) if profile_parent is not None else None,
        )
        launch_options: dict[str, object] = {
            "user_data_dir": temporary_directory.name,
            "headless": False,
            "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
            "viewport": {"width": 1280, "height": 900},
            "service_workers": "block",
        }
        proxy = (
            os.environ.get("JAV_PILOT_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=channel,
                **launch_options,
            )
        except Exception:  # noqa: BLE001 - retry bundled Chromium when Chrome is absent.
            if not channel:
                raise
            context = playwright.chromium.launch_persistent_context(**launch_options)

        media_gate = BrowserMediaGate()
        context.route(
            "**/*",
            lambda route: route_request(
                route,
                resolver=resolver,
                media_gate=media_gate,
            ),
        )
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)

        initial_pages = tuple(context.pages)
        context.new_page()
        for initial_page in initial_pages:
            try:
                initial_page.close()
            except Exception:  # noqa: BLE001 - context cleanup remains authoritative.
                pass

        options: list[WebDownloadVariantOption] = []
        for variant in WEB_DOWNLOAD_VARIANTS:
            if time.monotonic() >= deadline:
                options.append(WebDownloadVariantOption(variant, "failed", ()))
                continue
            page = None
            try:
                page = context.new_page()
                page.set_default_timeout(remaining_milliseconds(deadline))
                capture_state = ManifestCaptureState()
                media_gate.block_surrit_requests = False
                detail_url = prepare_exact_detail_capture(
                    page,
                    search_code,
                    canonical_code,
                    deadline,
                    variant=variant,
                    request_handler=capture_state.capture_request,
                )
                capture_state.detail_page_url = detail_url
                start_player(page, deadline)
                heights = discover_player_qualities(
                    page,
                    deadline,
                    capture_state=capture_state,
                    probe_parent=profile_parent,
                    media_gate=media_gate,
                )
                if not heights:
                    raise MissavError(
                        "MissAV did not expose selectable video qualities"
                    )
                options.append(WebDownloadVariantOption(variant, "available", heights))
            except MissavNotFound:
                options.append(WebDownloadVariantOption(variant, "not_found", ()))
            except BaseException:  # noqa: BLE001 - never expose browser request details.
                options.append(WebDownloadVariantOption(variant, "failed", ()))
            finally:
                try:
                    if page is not None:
                        page.close()
                except Exception:  # noqa: BLE001 - cleanup errors can expose page state.
                    pass
        return tuple(options)
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors may contain sensitive page state.
        raise MissavError("MissAV quality discovery failed") from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup errors can contain page state.
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - cleanup remains best effort.
            pass
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except Exception:  # noqa: BLE001 - never expose browser profile paths.
            pass


def load_resource_search_page(
    page: object,
    display_query: str,
    canonical_query: str | None,
    *,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
    page_number: int,
    deadline: float,
    cancel_event: threading.Event,
) -> MissavResourceParser:
    response = goto_with_transport_retry(
        page,
        series_search_url(display_query, page=page_number),
        deadline,
        cancel_event=cancel_event,
        timeout_cap_ms=10_000,
    )
    require_missav_page(page.url)
    html = page.content()
    was_challenged = looks_like_missav_challenge(html)
    if was_challenged:
        challenge_deadline = min(
            deadline,
            time.monotonic() + MISSAV_RESOURCE_CHALLENGE_WAIT_SECONDS,
        )
        wait_for_challenge_clear(
            page,
            challenge_deadline,
            cancel_event=cancel_event,
        )
        html = page.content()
    require_missav_page(page.url)
    require_series_search_page(
        page.url,
        display_prefix=display_query,
        page=page_number,
    )
    if not navigation_succeeded(response) and not was_challenged:
        raise MissavTransientError(
            "MissAV resource search is temporarily unavailable",
            code=navigation_failure_code(response),
        )
    parser = MissavResourceParser(
        display_query=display_query,
        canonical_query=canonical_query,
        suffix_width=suffix_width,
        start=start,
        end=end,
        search_path=series_search_path(display_query),
    )
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise MissavError(
            "MissAV resource results could not be parsed",
            code="parse_drift",
        ) from exc
    if parser.candidate_limit_hit:
        raise MissavError(
            "MissAV resource page has too many results",
            code="parse_drift",
        )
    return parser


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


def discover_resources_with_playwright(
    display_query: str,
    canonical_query: str | None,
    *,
    result_limit: int,
    suffix_width: int | None,
    start: int | None,
    end: int | None,
    state: MissavResourceState,
    timeout_seconds: float,
    cancel_event: threading.Event,
    on_page: Callable[[MissavResourcePage], None] | None,
) -> MissavResourceDiscovery:
    deadline = time.monotonic() + timeout_seconds
    next_page = state.next_page
    pending = state.pending
    cursor = state.cursor
    total_pages = state.total_pages
    scanned_pages = state.scanned_pages
    emitted: list[MissavResourceItem] = []

    def snapshot() -> MissavResourceState:
        return MissavResourceState(
            next_page=next_page,
            pending=pending,
            cursor=cursor,
            total_pages=total_pages,
            scanned_pages=scanned_pages,
        )

    def consume_pending(*, fetched: bool) -> None:
        nonlocal next_page, pending, cursor
        page_number = next_page
        first_delta = True
        while (
            page_number is not None
            and cursor < len(pending)
            and len(emitted) < result_limit
            and not cancel_event.is_set()
        ):
            count = min(
                MAX_RESOURCE_DELTA_ITEMS,
                len(pending) - cursor,
                result_limit - len(emitted),
            )
            delta = pending[cursor : cursor + count]
            cursor += count
            if cursor >= len(pending):
                pending = ()
                cursor = 0
                if total_pages is not None and page_number >= total_pages:
                    next_page = None
                elif page_number >= MAX_RESOURCE_PAGES:
                    next_page = None
                else:
                    next_page = page_number + 1
            emitted.extend(delta)
            event = MissavResourcePage(
                page=page_number,
                fetched=fetched and first_delta,
                items=delta,
                state=snapshot(),
            )
            if on_page is not None:
                on_page(event)
            first_delta = False

    if pending:
        consume_pending(fetched=False)
    if len(emitted) >= result_limit or next_page is None or cancel_event.is_set():
        final_state = snapshot()
        return MissavResourceDiscovery(
            items=tuple(emitted),
            state=final_state,
            complete=final_state.next_page is None,
        )

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError(
            "Playwright is unavailable for MissAV resource discovery"
        ) from exc
    # The challenge broker may touch MissAV, the player CDN, and the exact
    # Cloudflare challenge host during one operation.
    resolver = PublicHostResolver(max_hosts=16)
    missav_host = current_missav_site().host
    if not all(resolver.is_public(host) for host in (missav_host, PLAYER_ASSET_HOST)):
        raise MissavError("MissAV resource hosts must resolve to public addresses")

    context = None
    playwright = None
    temporary_directory: TemporaryDirectory[str] | None = None
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(prefix=".missav-browser-")
        launch_options: dict[str, object] = {
            "user_data_dir": temporary_directory.name,
            "headless": False,
            "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
            "viewport": {"width": 1280, "height": 900},
            "service_workers": "block",
            "accept_downloads": False,
        }
        proxy = (
            os.environ.get("JAV_PILOT_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=channel,
                **launch_options,
            )
        except Exception:  # noqa: BLE001 - bundled Chromium is the safe fallback.
            if not channel:
                raise
            context = playwright.chromium.launch_persistent_context(**launch_options)
        context.route(
            "**/*",
            lambda route: route_metadata_request(route, resolver=resolver),
        )
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(remaining_milliseconds(deadline))
        while (
            next_page is not None
            and len(emitted) < result_limit
            and not cancel_event.is_set()
        ):
            if time.monotonic() >= deadline:
                raise MissavTransientError(
                    "MissAV resource discovery timed out",
                    code="navigation_timeout",
                )
            page_number = next_page
            parser = load_resource_search_page(
                page,
                display_query,
                canonical_query,
                suffix_width=suffix_width,
                start=start,
                end=end,
                page_number=page_number,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                break
            discovered_total = max(page_number, parser.max_page)
            total_pages = max(total_pages or 1, discovered_total)
            scanned_pages += 1
            pending = parser.items
            cursor = 0
            if pending:
                consume_pending(fetched=True)
            else:
                if page_number >= total_pages or page_number >= MAX_RESOURCE_PAGES:
                    next_page = None
                else:
                    next_page = page_number + 1
                if on_page is not None:
                    on_page(
                        MissavResourcePage(
                            page=page_number,
                            fetched=True,
                            items=(),
                            state=snapshot(),
                        )
                    )
        final_state = snapshot()
        return MissavResourceDiscovery(
            items=tuple(emitted),
            state=final_state,
            complete=final_state.next_page is None,
        )
    except MissavError:
        raise
    except Exception as exc:  # noqa: BLE001 - browser failures may contain secrets.
        if cancel_event.is_set():
            return MissavResourceDiscovery(
                items=tuple(emitted),
                state=snapshot(),
                complete=False,
            )
        if _browser_runtime_is_missing(exc):
            raise MissavError(
                "MissAV resource browser runtime is unavailable"
            ) from None
        raise MissavTransientError(
            "MissAV resource discovery failed",
            code="upstream_unavailable",
        ) from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup errors are intentionally hidden.
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - cleanup remains best effort.
            pass
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except Exception:  # noqa: BLE001 - never expose browser profile paths.
            pass


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
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise MissavError(
            "Playwright is unavailable for MissAV series discovery"
        ) from exc

    deadline = time.monotonic() + timeout_seconds
    # The challenge broker may touch MissAV, the player CDN, and the exact
    # Cloudflare challenge host during one operation.
    resolver = PublicHostResolver(max_hosts=16)
    missav_host = current_missav_site().host
    if not all(resolver.is_public(host) for host in (missav_host, PLAYER_ASSET_HOST)):
        raise MissavError("MissAV series hosts must resolve to public addresses")

    context = None
    playwright = None
    temporary_directory: TemporaryDirectory[str] | None = None
    found: dict[str, tuple[int, str, set[MissavVariant]]] = {}
    try:
        playwright = sync_playwright().start()
        temporary_directory = TemporaryDirectory(prefix=".missav-browser-")
        launch_options: dict[str, object] = {
            "user_data_dir": temporary_directory.name,
            "headless": False,
            "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
            "viewport": {"width": 1280, "height": 900},
            "service_workers": "block",
            "accept_downloads": False,
        }
        proxy = (
            os.environ.get("JAV_PILOT_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=channel,
                **launch_options,
            )
        except Exception:  # noqa: BLE001 - retry bundled Chromium when Chrome is absent.
            if not channel:
                raise
            context = playwright.chromium.launch_persistent_context(**launch_options)

        context.route(
            "**/*",
            lambda route: route_metadata_request(route, resolver=resolver),
        )
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise MissavError("Playwright WebSocket routing is unavailable")
        route_web_socket("**/*", deny_routed_web_socket)

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(remaining_milliseconds(deadline))
        exact_code = exact_series_code(
            display_prefix,
            suffix_width=suffix_width,
            start=start,
            end=end,
        )
        if exact_code is not None:
            display_code, canonical_code = exact_code
            variants: list[MissavVariant] = []
            variant_error: MissavError | None = None
            for variant in WEB_DOWNLOAD_VARIANTS:
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
                        # dynamically prefixed; preserve the exact search fallback
                        # that locates that trusted route.
                        direct_miss_is_final=variant != "original",
                        accept_routed_challenge=True,
                    )
                except MissavNotFound:
                    detail_url = None
                except MissavError as exc:
                    if cancel_event.is_set():
                        raise
                    # A later optional variant must not erase an already verified
                    # exact result. Continue probing in case another isolated
                    # variant is available, but keep the aggregate incomplete.
                    variant_error = variant_error or exc
                    continue
                if cancel_event.is_set():
                    if variants:
                        return MissavSeriesDiscovery(
                            codes=(display_code,),
                            complete=False,
                            variants_by_code=((display_code, tuple(variants)),),
                        )
                    return MissavSeriesDiscovery(codes=(), complete=False)
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
            if cancel_event.is_set():
                return series_discovery_result(found, complete=False)
            if page_number == 1:
                if parser.max_page > MAX_SERIES_PAGES:
                    complete = False
                page_count = min(parser.max_page, MAX_SERIES_PAGES)
            total_candidates += parser.candidate_count
            if total_candidates >= MAX_SERIES_CANDIDATES or parser.candidate_limit_hit:
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
            if cancel_event.is_set():
                return series_discovery_result(found, complete=False)
            if time.monotonic() >= deadline:
                raise MissavTransientError(
                    "MissAV series discovery timed out",
                    code="navigation_timeout",
                )
            if not complete and (
                total_candidates >= MAX_SERIES_CANDIDATES or parser.candidate_limit_hit
            ):
                break
            page_number += 1
        return series_discovery_result(found, complete=complete)
    except MissavError:
        raise
    except Exception as exc:  # noqa: BLE001 - browser errors can expose visited URLs.
        if cancel_event.is_set():
            return series_discovery_result(found, complete=False)
        if _browser_runtime_is_missing(exc):
            raise MissavError("MissAV series browser runtime is unavailable") from None
        raise MissavTransientError(
            "MissAV series discovery failed",
            code="upstream_unavailable",
        ) from None
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup errors can contain page state.
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - cleanup remains best effort.
            pass
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except Exception:  # noqa: BLE001 - never expose browser profile paths.
            pass
