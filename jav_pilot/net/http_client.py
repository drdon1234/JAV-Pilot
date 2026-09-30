from __future__ import annotations

import time
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .. import __version__
from .network_guard import PublicHostResolver
from .pinned_http import PinnedHTTPHandler, PinnedHTTPSHandler


class FetchError(RuntimeError):
    pass


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/126.0.0.0 Safari/537.36 jav-pilot/{__version__}"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
MAX_FETCH_ATTEMPTS = 2
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class _AllowedOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_origins: frozenset[tuple[str, str, int]]) -> None:
        super().__init__()
        self.allowed_origins = allowed_origins

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        absolute = urljoin(req.full_url, newurl)
        if _url_origin(absolute) not in self.allowed_origins:
            raise FetchError("cross-origin redirect was rejected")
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def fetch_text(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    allowed_origin: str | None = None,
    data: bytes | None = None,
) -> str:
    raw, charset = _fetch(
        url,
        timeout=timeout,
        max_bytes=max_bytes,
        headers=headers,
        allowed_origins=(allowed_origin or url,),
        data=data,
    )
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError as exc:
        raise FetchError("response declared an unsupported charset") from exc


def fetch_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    allowed_origins: tuple[str, ...],
    headers: dict[str, str] | None = None,
) -> bytes:
    """Fetch a bounded body from one of ``allowed_origins``.

    Unlike ``fetch_text`` the request may target any listed origin, such as a
    source's API host and its download host; redirects stay within the list.
    """

    if not allowed_origins:
        raise FetchError("request URL is outside the configured source")
    raw, _charset = _fetch(
        url,
        timeout=timeout,
        max_bytes=max_bytes,
        headers=headers,
        allowed_origins=tuple(allowed_origins),
        data=None,
    )
    return raw


def _fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str] | None,
    allowed_origins: tuple[str, ...],
    data: bytes | None,
) -> tuple[bytes, str | None]:
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    origins = frozenset(_url_origin(value) for value in allowed_origins)
    origin = _url_origin(url)
    if origin not in origins:
        raise FetchError("request URL is outside the configured source")
    if not PublicHostResolver(max_hosts=1).is_public(origin[1]):
        raise FetchError("request host must resolve to a public address")
    request = Request(url, data=data, headers=request_headers)
    opener = build_opener(
        ProxyHandler(),
        PinnedHTTPHandler(),
        PinnedHTTPSHandler(),
        _AllowedOriginRedirectHandler(origins),
    )
    deadline = time.monotonic() + timeout
    for attempt in range(MAX_FETCH_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("request timed out")
        try:
            with opener.open(request, timeout=max(0.001, remaining)) as response:
                if _url_origin(response.geturl() or url) not in origins:
                    raise FetchError("cross-origin redirect was rejected")
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise FetchError(f"response exceeded {max_bytes} bytes")
                return raw, response.headers.get_content_charset()
        except HTTPError as exc:
            status = exc.code
            exc.close()
            error = FetchError(f"HTTP {status}")
            retryable = status in RETRYABLE_HTTP_STATUSES
            cause: BaseException = exc
        except URLError as exc:
            error = FetchError(str(exc.reason))
            retryable = True
            cause = exc
        except TimeoutError as exc:
            error = FetchError("request timed out")
            retryable = True
            cause = exc
        except (HTTPException, OSError) as exc:
            error = FetchError(str(exc) or "request failed")
            retryable = True
            cause = exc
        except ValueError as exc:
            # Non-ASCII paths/headers or malformed redirect targets are
            # rejected by http.client with ValueError (UnicodeEncodeError);
            # callers only handle FetchError.
            error = FetchError("request could not be encoded")
            retryable = False
            cause = exc

        if not retryable or attempt + 1 >= MAX_FETCH_ATTEMPTS:
            raise error from cause
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise error from cause
        time.sleep(min(0.05, remaining / 2))

    raise FetchError("request failed")


def _url_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise FetchError("invalid request URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise FetchError("request URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise FetchError("request URL credentials are not allowed")
    return scheme, parsed.hostname.lower(), port or (443 if scheme == "https" else 80)
