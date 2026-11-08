"""Browser-compatible TLS for sources that reject the standard HTTP transport."""

from __future__ import annotations

import time
from urllib.parse import urljoin

from .http_client import FetchError, _url_origin
from .network_guard import resolve_public_addresses


def fetch_text(url, *, timeout, max_bytes, allowed_origins, headers=None, data=None):
    from curl_cffi import CurlOpt, requests

    origins = frozenset(_url_origin(item) for item in allowed_origins)
    deadline = time.monotonic() + timeout
    for _ in range(6):
        origin = _url_origin(url)
        if origin not in origins:
            raise FetchError("cross-origin redirect was rejected")
        addresses = resolve_public_addresses(origin[1], origin[2])
        if not addresses:
            raise FetchError("request host must resolve to a public address")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("request timed out")
        address = addresses[0]
        if ":" in address:
            address = f"[{address}]"
        try:
            with requests.Session(curl_options={
                CurlOpt.RESOLVE: [f"{origin[1]}:{origin[2]}:{address}"]
            }) as session:
                response = session.request(
                    "POST" if data is not None else "GET", url,
                    headers=headers, data=data, impersonate="chrome",
                    allow_redirects=False, timeout=remaining, stream=True,
                )
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if data is not None:
                            raise FetchError("POST redirect was rejected")
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("redirect location is missing")
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 400:
                        raise FetchError(f"HTTP {response.status_code}")
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise FetchError(f"response exceeded {max_bytes} bytes")
                        if time.monotonic() >= deadline:
                            raise FetchError("request timed out")
                    return body.decode(response.charset or "utf-8", errors="replace")
                finally:
                    response.close()
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError("browser-compatible HTTP request failed") from exc
    raise FetchError("too many redirects")
