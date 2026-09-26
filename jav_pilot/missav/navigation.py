"""Navigation, challenge handling and exact detail page location."""

from __future__ import annotations

import threading
import time
from html import unescape
from typing import Callable
from urllib.parse import urlsplit

from ..web_download.variant import DEFAULT_WEB_DOWNLOAD_VARIANT, MissavVariant
from .errors import MissavError, MissavNotFound, MissavTransientError
from .parsing import find_exact_detail_url
from .routing import is_cloudflare_challenge_host
from .site import (
    CHALLENGE_TECHNICAL_MARKERS,
    CHALLENGE_TEXT_MARKERS,
    remaining_milliseconds,
)
from .urls import (
    build_detail_url,
    exact_detail_url,
    localized_detail_url,
    require_exact_detail_page,
    require_exact_search_page,
    require_missav_page,
    routed_challenge_detail_url,
    search_url,
)

def raise_capture_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise MissavError("MissAV request was cancelled")


def remove_page_listener(
    page: object,
    event_name: str,
    callback: Callable[[object], None] | None,
) -> None:
    if callback is None:
        return
    remove_listener = getattr(page, "remove_listener", None)
    if callable(remove_listener):
        try:
            remove_listener(event_name, callback)
        except Exception:
            pass


def _wait_for_exact_detail(
    page: object,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    cancel_event: threading.Event | None = None,
) -> str | None:
    render_deadline = min(deadline, time.monotonic() + 8.0)
    challenge_active = False
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise MissavError("MissAV request was cancelled")
        html = page.content()
        detail_url = find_exact_detail_url(
            html,
            canonical_code,
            variant=variant,
        )
        if detail_url:
            return detail_url
        now = time.monotonic()
        is_challenge = looks_like_missav_challenge(html)
        if is_challenge:
            challenge_active = True
            render_deadline = deadline
        elif challenge_active:
            challenge_active = False
            render_deadline = min(deadline, now + 8.0)
        if now >= render_deadline:
            if is_challenge:
                raise MissavTransientError(
                    "MissAV search is temporarily unavailable",
                    code="challenge_active",
                )
            return None
        page.wait_for_timeout(min(250, remaining_milliseconds(render_deadline)))


def locate_exact_detail(
    page: object,
    search_code: str,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    cancel_event: threading.Event | None = None,
    navigation_timeout_cap_ms: int | None = None,
    direct_miss_is_final: bool = False,
    accept_routed_challenge: bool = False,
) -> str | None:
    navigation_timeout = remaining_milliseconds(deadline)
    navigation_timeout = min(
        navigation_timeout,
        navigation_timeout_cap_ms
        if navigation_timeout_cap_ms is not None
        else 12_000,
    )
    direct_response: object
    direct_url = build_detail_url(search_code, variant=variant)
    localized_url = localized_detail_url(search_code, variant=variant)
    try:
        direct_response = goto_with_transport_retry(
            page,
            direct_url,
            deadline,
            timeout_cap_ms=navigation_timeout,
        )
    except Exception as exc:
        if not (
            _is_browser_navigation_timeout(exc)
            or is_browser_transport_reset(exc)
        ):
            raise
        direct_url = localized_url
        direct_response = goto_with_transport_retry(
            page,
            direct_url,
            deadline,
            timeout_cap_ms=min(
                remaining_milliseconds(deadline),
                navigation_timeout,
            ),
        )
    if cancel_event is not None and cancel_event.is_set():
        raise MissavError("MissAV request was cancelled")
    require_missav_page(page.url)
    direct_status = _navigation_status(direct_response)
    direct_challenged = looks_like_missav_challenge(page.content())
    if accept_routed_challenge and direct_status == 403:
        routed_detail = routed_challenge_detail_url(
            page.url,
            canonical_code,
            variant=variant,
        )
        if routed_detail is not None and looks_like_missav_challenge(page.content()):
            return routed_detail
    # MissAV applies Cloudflare policy per route as well as per host.  In
    # production the root exact route may be available while /cn/ is
    # challenged (or vice versa).  Try the legacy localized alias once before
    # waiting on a challenge.  This stays on the configured origin, keeps the
    # exact-code validator in force, and is deliberately bounded to one
    # alternate navigation.
    if (
        direct_url != localized_url
        and (direct_challenged or direct_status in {403, 429, 503})
    ):
        direct_url = localized_url
        direct_response = goto_with_transport_retry(
            page,
            direct_url,
            deadline,
            timeout_cap_ms=min(
                remaining_milliseconds(deadline),
                navigation_timeout,
            ),
        )
        if cancel_event is not None and cancel_event.is_set():
            raise MissavError("MissAV request was cancelled")
        require_missav_page(page.url)
        direct_status = _navigation_status(direct_response)
        direct_challenged = looks_like_missav_challenge(page.content())
        if accept_routed_challenge and direct_status == 403:
            routed_detail = routed_challenge_detail_url(
                page.url,
                canonical_code,
                variant=variant,
            )
            if routed_detail is not None and looks_like_missav_challenge(
                page.content()
            ):
                return routed_detail
    direct_detail = loaded_exact_detail_url(
        page,
        direct_response,
        canonical_code,
        deadline,
        variant=variant,
        cancel_event=cancel_event,
    )
    if direct_detail is not None:
        return direct_detail
    if direct_status in {403, 429, 503}:
        raise MissavTransientError(
            "MissAV detail page is temporarily unavailable",
            code=navigation_failure_code(
                direct_response,
                challenged=direct_challenged,
            ),
        )
    # A direct exact variant route returning 404/410 is authoritative.  Do
    # not turn it into a retryable upstream failure or probe unrelated search
    # pages; this is important for optional variants whose absence is a valid
    # result.
    if direct_miss_is_final and direct_status in {404, 410}:
        return None

    search_response = goto_with_transport_retry(
        page,
        search_url(search_code),
        deadline,
        timeout_cap_ms=(
            min(remaining_milliseconds(deadline), navigation_timeout_cap_ms)
            if navigation_timeout_cap_ms is not None
            else remaining_milliseconds(deadline)
        ),
    )
    if cancel_event is not None and cancel_event.is_set():
        raise MissavError("MissAV request was cancelled")
    require_missav_page(page.url)
    search_challenged = looks_like_missav_challenge(page.content())
    if not navigation_succeeded(search_response) and not search_challenged:
        raise MissavTransientError(
            "MissAV search is temporarily unavailable",
            code=navigation_failure_code(search_response),
        )
    detail_url = _wait_for_exact_detail(
        page,
        canonical_code,
        deadline,
        variant=variant,
        cancel_event=cancel_event,
    )
    require_exact_search_page(page.url, search_code)
    if detail_url is None:
        if not navigation_succeeded(search_response):
            raise MissavTransientError(
                "MissAV search is temporarily unavailable",
                code=navigation_failure_code(
                    search_response,
                    challenged=search_challenged,
                ),
            )
        return None

    detail_response = goto_with_transport_retry(
        page,
        detail_url,
        deadline,
        timeout_cap_ms=(
            min(remaining_milliseconds(deadline), navigation_timeout_cap_ms)
            if navigation_timeout_cap_ms is not None
            else remaining_milliseconds(deadline)
        ),
    )
    if cancel_event is not None and cancel_event.is_set():
        raise MissavError("MissAV request was cancelled")
    require_missav_page(page.url)
    detail_challenged = looks_like_missav_challenge(page.content())
    loaded_detail = loaded_exact_detail_url(
        page,
        detail_response,
        canonical_code,
        deadline,
        variant=variant,
        cancel_event=cancel_event,
    )
    if loaded_detail is not None:
        return loaded_detail
    if not navigation_succeeded(detail_response):
        raise MissavTransientError(
            "MissAV detail page is temporarily unavailable",
            code=navigation_failure_code(
                detail_response,
                challenged=detail_challenged,
            ),
        )
    require_exact_detail_page(page.url, canonical_code, variant=variant)
    raise MissavError("MissAV detail page could not be loaded")


def prepare_exact_detail_capture(
    page: object,
    search_code: str,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    request_handler: Callable[[object], None] | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str | None], None] | None = None,
) -> str:
    if stage_callback is not None:
        stage_callback("locate_exact_detail")
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
    if request_handler is not None:
        # Register the exact callback object so a caller-owned persistent page
        # can remove it after the operation.  Wrapping in an anonymous lambda
        # leaves listeners behind and causes duplicate manifest captures on
        # every subsequent broker task.
        page.on("request", request_handler)
    if stage_callback is not None:
        stage_callback("reload_detail")
    return reload_exact_detail_for_capture(
        page,
        detail_url,
        canonical_code,
        deadline,
        variant=variant,
        cancel_event=cancel_event,
    )


def reload_exact_detail_for_capture(
    page: object,
    detail_url: str,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    cancel_event: threading.Event | None = None,
) -> str:
    raise_capture_cancelled(cancel_event)
    response = goto_with_transport_retry(
        page,
        detail_url,
        deadline,
    )
    require_missav_page(page.url)
    was_challenged = looks_like_missav_challenge(page.content())
    if was_challenged:
        wait_for_challenge_clear(page, deadline, cancel_event=cancel_event)
        require_missav_page(page.url)
    if not navigation_succeeded(response) and not was_challenged:
        raise MissavTransientError(
            "MissAV detail page is temporarily unavailable",
            code=navigation_failure_code(response),
        )
    require_exact_detail_page(page.url, canonical_code, variant=variant)
    validated_url = exact_detail_url(page.url, canonical_code, variant=variant)
    if validated_url is None:
        raise MissavError("MissAV redirected away from the requested work")
    return validated_url


def loaded_exact_detail_url(
    page: object,
    response: object,
    canonical_code: str,
    deadline: float,
    *,
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT,
    cancel_event: threading.Event | None = None,
) -> str | None:
    html = page.content()
    if looks_like_missav_challenge(html):
        wait_for_challenge_clear(page, deadline, cancel_event=cancel_event)
        require_missav_page(page.url)
        return exact_detail_url(page.url, canonical_code, variant=variant)
    if not navigation_succeeded(response):
        return None
    return exact_detail_url(page.url, canonical_code, variant=variant)


def wait_for_challenge_clear(
    page: object,
    deadline: float,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    interaction_attempted = False
    while looks_like_missav_challenge(page.content()):
        if cancel_event is not None and cancel_event.is_set():
            raise MissavError("MissAV request was cancelled")
        now = time.monotonic()
        if now >= deadline:
            raise MissavTransientError(
                "MissAV page is temporarily unavailable",
                code="challenge_active",
            )
        if not interaction_attempted:
            _attempt_cloudflare_challenge(page, deadline)
            interaction_attempted = True
        page.wait_for_timeout(min(250, remaining_milliseconds(deadline)))


def _attempt_cloudflare_challenge(page: object, deadline: float) -> bool:
    """Click only the explicit verification control in a challenge iframe.

    Managed Cloudflare pages may render a Turnstile checkbox instead of an
    entirely automatic proof.  The browser remains headful and isolated, so
    clicking that control is equivalent to the user action the page requests;
    no challenge token or page content leaves the browser context.  Any
    missing/changed control is ignored and the normal bounded wait remains the
    source of truth.
    """

    try:
        frames = tuple(getattr(page, "frames", ()) or ())
    except Exception:
        return False
    selectors = (
        'input[type="checkbox"]',
        '[role="checkbox"]',
        "button",
    )
    for frame in frames:
        frame_url = str(getattr(frame, "url", "") or "")
        try:
            host = urlsplit(frame_url).hostname or ""
        except ValueError:
            continue
        if not is_cloudflare_challenge_host(host):
            continue
        for selector in selectors:
            if time.monotonic() >= deadline:
                return False
            try:
                locator = frame.locator(selector).first
                if int(locator.count()) < 1 or not locator.is_visible():
                    continue
                locator.click(timeout=min(1000, remaining_milliseconds(deadline)))
                return True
            except Exception:
                continue
        # The checkbox may be inside a closed shadow tree. Targeting the
        # iframe element itself does not always dispatch into its child
        # document, so click the already origin-verified frame body at the
        # narrow checkbox position before falling back to outer geometry.
        try:
            frame_element = frame.frame_element()
            box = frame_element.bounding_box()
            width = float(box["width"])
            height = float(box["height"])
            if 180.0 <= width <= 500.0 and 40.0 <= height <= 180.0:
                frame.locator("body").click(
                    position={"x": min(24.0, width * 0.1), "y": height / 2.0},
                    timeout=min(1000, remaining_milliseconds(deadline)),
                )
                return True
        except Exception:
            pass
        # Turnstile's visible checkbox can live in a closed shadow tree.  In
        # that mode no selector can reach it, but Playwright can still click
        # the verified Cloudflare iframe at the checkbox's fixed left-hand
        # position.  Keep the accepted geometry deliberately narrow so this
        # cannot become a generic coordinate click on arbitrary content.
        try:
            frame_element = frame.frame_element()
            box = frame_element.bounding_box()
            width = float(box["width"])
            height = float(box["height"])
            if not (180.0 <= width <= 500.0 and 40.0 <= height <= 180.0):
                continue
            frame_element.click(
                position={"x": min(24.0, width * 0.1), "y": height / 2.0},
                timeout=min(1000, remaining_milliseconds(deadline)),
            )
            return True
        except Exception:
            continue
    # Some Turnstile builds keep the visible checkbox in a frame element
    # whose child Frame still reports an empty/about:blank URL. In that case
    # the frame loop above cannot establish the Cloudflare origin even though
    # the main document exposes a fully qualified, verifiable iframe ``src``.
    # Validate that attribute independently, then apply the same narrow
    # geometry restriction to the iframe element itself.
    try:
        iframe_elements = page.locator("iframe")
        iframe_count = min(16, int(iframe_elements.count()))
    except Exception:
        return False
    for index in range(iframe_count):
        try:
            iframe = iframe_elements.nth(index)
            source = str(iframe.get_attribute("src") or "")
            host = urlsplit(source).hostname or ""
            if not is_cloudflare_challenge_host(host):
                continue
            box = iframe.bounding_box()
            width = float(box["width"])
            height = float(box["height"])
            if not (180.0 <= width <= 500.0 and 40.0 <= height <= 180.0):
                continue
            iframe.click(
                position={"x": min(24.0, width * 0.1), "y": height / 2.0},
                timeout=min(1000, remaining_milliseconds(deadline)),
            )
            return True
        except Exception:
            continue
    # Some managed-challenge builds render the visible Turnstile control in
    # a closed shadow tree hosted by the top-level ``#challenge-stage``
    # element.  In that mode Playwright exposes neither a child Frame nor a
    # useful iframe ``src`` even though the narrow checkbox widget is visible.
    # Keep the fallback tied to the verified MissAV challenge document, the
    # exact Cloudflare stage id, and the same bounded widget geometry used by
    # the frame paths above.  Clicking the element (rather than page.mouse)
    # prevents this from becoming a generic coordinate click primitive.
    try:
        require_missav_page(page.url)
        if not looks_like_missav_challenge(page.content()):
            return False
        stage = page.locator("#challenge-stage").first
        if int(stage.count()) != 1 or not stage.is_visible():
            return False
        box = stage.bounding_box()
        width = float(box["width"])
        height = float(box["height"])
        # The top-level stage spans the challenge content column rather than
        # only the 300px Turnstile card.  Keep it below a normal 1280px page
        # width and click only its fixed left-hand checkbox position.
        if not (180.0 <= width <= 1024.0 and 40.0 <= height <= 180.0):
            return False
        stage.click(
            position={"x": min(24.0, width * 0.1), "y": height / 2.0},
            timeout=min(1000, remaining_milliseconds(deadline)),
        )
        return True
    except Exception:
        return False


def looks_like_missav_challenge(html: object) -> bool:
    lowered = unescape(str(html or "")).lower()
    return any(marker in lowered for marker in CHALLENGE_TECHNICAL_MARKERS) and any(
        marker in lowered for marker in CHALLENGE_TEXT_MARKERS
    )


def navigation_succeeded(response: object) -> bool:
    status = _navigation_status(response)
    return status is not None and 200 <= status < 300


def goto_with_transport_retry(
    page: object,
    url: str,
    deadline: float,
    *,
    timeout_cap_ms: int | None = None,
    cancel_event: threading.Event | None = None,
) -> object:
    """Retry only bounded proxy/TCP resets within the current trusted page.

    A transient proxy tunnel reset is not evidence that the persistent
    browser profile or page is corrupt. Restarting Chromium for every reset
    discards the warm network session, amplifies Cloudflare challenges, and
    makes all queued downloads wait behind browser startup. Retry the same
    navigation up to three times inside the caller's existing deadline. No
    URL or raw Playwright exception crosses this boundary.
    """

    for attempt in range(3):
        if cancel_event is not None and cancel_event.is_set():
            raise MissavError("MissAV request was cancelled")
        timeout_ms = remaining_milliseconds(deadline)
        if timeout_cap_ms is not None:
            timeout_ms = min(timeout_ms, max(1, int(timeout_cap_ms)))
        try:
            return page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 - Playwright is runtime-only.
            if not is_browser_transport_reset(exc) or attempt >= 2:
                raise
            delay_ms = min(1000, 250 * (2**attempt))
            if time.monotonic() + (delay_ms / 1000.0) >= deadline:
                raise
            if cancel_event is not None and cancel_event.is_set():
                raise MissavError("MissAV request was cancelled")
            page.wait_for_timeout(delay_ms)
    raise AssertionError("unreachable MissAV navigation retry state")


def is_browser_transport_reset(error: BaseException) -> bool:
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "err_connection_reset",
            "err_connection_closed",
            "err_network_changed",
            "err_proxy_connection_failed",
        )
    )


def is_browser_target_closed(error: BaseException) -> bool:
    """Return whether *error* is a closed/disconnected Playwright target.

    Both class names and the stable Playwright wording are covered because the
    concrete exception class changed between Playwright releases. A dead
    browser must never be classified as an upstream failure: upstream cooldown
    retries reuse the same page and can never succeed, while the
    transport-reset path replaces the driver (and, standalone, the process).
    """

    error_type = type(error).__name__.casefold()
    if error_type in {
        "targetclosederror",
        "browserclosederror",
        "browserdisconnectederror",
    }:
        return True
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser disconnected",
            "target closed",
        )
    )


def _is_browser_navigation_timeout(error: BaseException) -> bool:
    """Recognize Playwright's bounded navigation timeout without leaking it."""

    return type(error).__name__ == "TimeoutError"


def navigation_failure_code(
    response: object,
    *,
    challenged: bool = False,
) -> str:
    status = _navigation_status(response)
    if challenged and status in {403, 503}:
        return "challenge_active"
    if status == 429:
        return "rate_limited"
    return "upstream_unavailable"


def _navigation_status(response: object) -> int | None:
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return status if 100 <= status <= 599 else None
