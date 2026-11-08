"""HTML transport with bounded, origin-scoped browser verification recovery."""

from __future__ import annotations

import time
from collections import OrderedDict
import threading
from urllib.parse import urlsplit

from ..core.models import SearchBounds
from .browser_fetcher import BrowserFetchError, BrowserPageFetcher
from .challenges import age_gate, is_challenge, is_firewall_block
from .http_client import FetchError, fetch_text as fetch_http_text

_BROWSER_ORIGINS: OrderedDict[str, float] = OrderedDict()
_LOCK = threading.Lock()


def fetch_browser_text(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    allowed_origins: tuple[str, ...],
    headers: dict[str, str] | None = None,
) -> str:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in allowed_origins:
        raise FetchError("request URL is outside the configured source")
    try:
        with BrowserPageFetcher(
            SearchBounds(timeout_seconds=timeout, max_response_bytes=max_bytes),
            session_origin=origin,
            allowed_origins=allowed_origins,
            headers=headers,
        ) as browser:
            html = browser.fetch(url)
    except BrowserFetchError as exc:
        raise FetchError(str(exc)) from exc
    if len(html.encode("utf-8")) > max_bytes:
        raise FetchError(f"response exceeded {max_bytes} bytes")
    with _LOCK:
        _BROWSER_ORIGINS[origin] = time.monotonic() + 600
        _BROWSER_ORIGINS.move_to_end(origin)
        while len(_BROWSER_ORIGINS) > 64:
            _BROWSER_ORIGINS.popitem(last=False)
    return html


def fetch_site_text(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    allowed_origin: str | None = None,
    allowed_origins: tuple[str, ...] | None = None,
    data: bytes | None = None,
) -> str:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    trusted = allowed_origins or (allowed_origin or origin,)
    deadline = time.monotonic() + timeout
    with _LOCK:
        warm = _BROWSER_ORIGINS.get(origin, 0) > time.monotonic()
    if not warm or data is not None:
        try:
            html = fetch_http_text(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                headers=headers,
                allowed_origins=trusted,
                data=data,
            )
        except FetchError as exc:
            # Do not retry rate limits, outages, redirect rejections or arbitrary POSTs.
            if data is not None or str(exc) not in {"HTTP 403", "HTTP 503"}:
                raise
        else:
            if is_firewall_block(html):
                raise FetchError("HTTP 403: source firewall blocked this network")
            if not is_challenge(html) and not age_gate(html, url):
                return html
            if data is not None:
                raise FetchError(
                    "source access challenge requires browser verification"
                )
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise FetchError("source verification timed out")
    return fetch_browser_text(
        url,
        timeout=remaining,
        max_bytes=max_bytes,
        allowed_origins=trusted,
        headers=headers,
    )
