"""HTML transport with bounded, origin-scoped browser verification recovery."""

from __future__ import annotations

import time
import json
import os
import signal
import subprocess
import sys
from collections import OrderedDict
import threading
from urllib.parse import urlsplit

from .browser_fetcher import BROWSER_SLOTS, BROWSER_QUEUE_TIMEOUT_SECONDS
from .challenges import age_gate, is_challenge, is_firewall_block
from .http_client import FetchError, fetch_text as fetch_http_text

_BROWSER_ORIGINS: OrderedDict[str, float] = OrderedDict()
_LOCK = threading.Lock()


def _remember_browser_origin(origin: str) -> None:
    with _LOCK:
        _BROWSER_ORIGINS[origin] = time.monotonic() + 600
        _BROWSER_ORIGINS.move_to_end(origin)
        while len(_BROWSER_ORIGINS) > 64:
            _BROWSER_ORIGINS.popitem(last=False)


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
    deadline = time.monotonic() + timeout
    if not BROWSER_SLOTS.acquire(timeout=max(0, min(timeout, BROWSER_QUEUE_TIMEOUT_SECONDS))):
        raise FetchError("browser fetch capacity is busy")
    process = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("browser request timed out")
        process = subprocess.Popen(
            [sys.executable, "-m", "jav_pilot.net.browser_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", start_new_session=os.name != "nt",
        )
        request = dict(url=url, origin=origin, timeout=remaining, max_bytes=max_bytes,
                       allowed_origins=allowed_origins, headers=headers)
        try:
            output, _ = process.communicate(json.dumps(request), timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise FetchError("browser verification timed out") from exc
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise FetchError("browser runtime returned an invalid response") from exc
        if result.get("error"):
            raise FetchError(result["error"])
        html = result.get("html")
        if process.returncode or not isinstance(html, str):
            raise FetchError("browser runtime returned an invalid response")
    finally:
        try:
            if process is not None:
                # Also reap descendants after normal exit: browser shutdown can
                # leave a driver or Chromium process holding the session lock.
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                process.wait()
        finally:
            BROWSER_SLOTS.release()
    if len(html.encode("utf-8")) > max_bytes:
        raise FetchError(f"response exceeded {max_bytes} bytes")
    _remember_browser_origin(origin)
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
    http_fetch=None,
) -> str:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    trusted = allowed_origins or (allowed_origin or origin,)
    deadline = time.monotonic() + timeout
    with _LOCK:
        warm = _BROWSER_ORIGINS.get(origin, 0) > time.monotonic()
    if not warm or data is not None:
        try:
            html = (http_fetch or fetch_http_text)(
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
    # A known browser gate remains one even when this verification attempt times out.
    # Keep subsequent requests on the same browser profile instead of probing it
    # again with a fresh HTTP fingerprint.
    _remember_browser_origin(origin)
    return fetch_browser_text(
        url,
        timeout=remaining,
        max_bytes=max_bytes,
        allowed_origins=trusted,
        headers=headers,
    )
