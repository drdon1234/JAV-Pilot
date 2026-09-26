from __future__ import annotations

import posixpath
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .fc2_images import fc2_image_path_pattern, fc2_image_referer
from ..net.http_client import DEFAULT_HEADERS
from ..net.network_guard import PublicHostResolver
from ..net.pinned_http import PinnedHTTPHandler, PinnedHTTPSHandler
from ..config.settings import load_settings
from ..config.source_catalog import METADATA_CATALOG, METADATA_PROFILES
from ..indexers.metadata_catalog import image_url_allowed


MAX_COVER_BYTES = 5 * 1024 * 1024
DEFAULT_COVER_TIMEOUT_SECONDS = 10.0
MAX_COVER_ATTEMPTS = 2
COVER_RETRY_DELAY_SECONDS = 0.1
COVER_ORIGIN_CONCURRENCY = 2
COVER_ORIGIN_QUEUE_TIMEOUT_SECONDS = 10.0
_sleep = time.sleep

_MAX_ORIGIN_LIMITERS = 128
_ORIGIN_LIMITERS: OrderedDict[tuple[str, str, int], threading.BoundedSemaphore] = (
    OrderedDict()
)
_ORIGIN_LIMITERS_LOCK = threading.Lock()

_RASTER_IMAGE_TYPES = frozenset(
    {
        "image/apng",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)
_IMAGE_ACCEPT = (
    "image/avif,image/webp,image/apng,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1"
)
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_JAVBUS_DMM_PATH_PREFIXES = {
    "awsimgsrc.dmm.co.jp": ("/pics_dig/",),
    "pics.dmm.co.jp": ("/pics_dig/", "/digital/video/"),
}
_JAVBUS_CDN_PATH_PREFIXES = (
    "/pics/",
    "/cover/",
    "/covers/",
    "/sample/",
    "/samples/",
    "/thumb/",
    "/thumbs/",
)


class CoverProxyError(RuntimeError):
    category = "upstream"


class CoverRequestError(CoverProxyError):
    category = "request"


class CoverUpstreamError(CoverProxyError):
    category = "upstream"


@dataclass(frozen=True)
class CoverImage:
    body: bytes
    content_type: str
    source_url: str


@dataclass
class CoverStream:
    response: Any
    content_type: str
    source_url: str
    content_length: int | None
    max_bytes: int
    release_slot: Callable[[], None] | None = None
    _closed: bool = False

    def iter_chunks(self, *, chunk_size: int = 64 * 1024):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        total = 0
        while True:
            remaining = self.max_bytes - total
            read_size = min(chunk_size, remaining + 1)
            if self.content_length is not None:
                declared_remaining = self.content_length - total
                read_size = min(read_size, declared_remaining + 1)
            try:
                chunk = self.response.read(read_size)
            except (TimeoutError, URLError, HTTPException, OSError) as exc:
                raise CoverUpstreamError("cover upstream stream failed") from exc
            if not chunk:
                if self.content_length is not None and total != self.content_length:
                    raise CoverUpstreamError(
                        "cover response did not match Content-Length"
                    )
                break
            total += len(chunk)
            if total > self.max_bytes:
                raise CoverUpstreamError(
                    f"cover response exceeded {self.max_bytes} bytes"
                )
            if self.content_length is not None and total > self.content_length:
                raise CoverUpstreamError("cover response did not match Content-Length")
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.response.close()
        finally:
            if self.release_slot is not None:
                self.release_slot()
                self.release_slot = None

    def __enter__(self) -> "CoverStream":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


@dataclass(frozen=True)
class _CoverPolicy:
    origin: tuple[str, str, int]
    referer: str
    path_prefixes: tuple[str, ...] = ()
    path_pattern: re.Pattern[str] | None = None


class _RestrictedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, policy: _CoverPolicy) -> None:
        super().__init__()
        self._policy = policy

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        try:
            _validate_cover_url(newurl, self._policy)
        except CoverRequestError as exc:
            raise CoverUpstreamError("cover upstream redirect was rejected") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_cover(
    source: str,
    url: str,
    *,
    settings: dict[str, Any] | None = None,
    timeout: float = DEFAULT_COVER_TIMEOUT_SECONDS,
    max_bytes: int = MAX_COVER_BYTES,
) -> CoverImage:
    with open_cover(
        source,
        url,
        settings=settings,
        timeout=timeout,
        max_bytes=max_bytes,
    ) as stream:
        body = b"".join(stream.iter_chunks())
        return CoverImage(
            body=body, content_type=stream.content_type, source_url=stream.source_url
        )


def open_cover(
    source: str,
    url: str,
    *,
    settings: dict[str, Any] | None = None,
    timeout: float = DEFAULT_COVER_TIMEOUT_SECONDS,
    max_bytes: int = MAX_COVER_BYTES,
) -> CoverStream:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    active_settings = settings if settings is not None else load_settings()
    policy = _policy_for_source(source, url, active_settings)
    _validate_cover_url(url, policy)
    origin_slot = _origin_limiter(policy.origin)
    if not origin_slot.acquire(timeout=COVER_ORIGIN_QUEUE_TIMEOUT_SECONDS):
        raise CoverUpstreamError("cover upstream is busy; retry later")
    slot_owned = True

    request_headers = dict(DEFAULT_HEADERS)
    request_headers.update(
        {
            "Accept": _IMAGE_ACCEPT,
            "Referer": policy.referer,
        }
    )
    request = Request(url, headers=request_headers)
    opener = build_opener(
        ProxyHandler(),
        PinnedHTTPHandler(),
        PinnedHTTPSHandler(),
        _RestrictedRedirectHandler(policy),
    )
    deadline = time.monotonic() + timeout

    for attempt in range(MAX_COVER_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CoverUpstreamError("cover upstream request timed out")
        response = None
        try:
            response = opener.open(request, timeout=max(0.001, remaining))
            final_url = response.geturl() or url
            try:
                _validate_cover_url(final_url, policy)
            except CoverRequestError as exc:
                raise CoverUpstreamError(
                    "cover upstream redirect was rejected"
                ) from exc

            content_type = _response_content_type(response.headers)
            if content_type not in _RASTER_IMAGE_TYPES:
                raise CoverUpstreamError(
                    "cover upstream did not return a supported raster image"
                )

            content_length = _content_length(response.headers)
            if content_length is not None and content_length > max_bytes:
                raise CoverUpstreamError(f"cover response exceeded {max_bytes} bytes")
            return CoverStream(
                response=response,
                content_type=content_type,
                source_url=final_url,
                content_length=content_length,
                max_bytes=max_bytes,
                release_slot=origin_slot.release,
            )
        except CoverProxyError:
            if response is not None:
                response.close()
            if slot_owned:
                origin_slot.release()
                slot_owned = False
            raise
        except HTTPError as exc:
            exc.close()
            error = CoverUpstreamError(f"cover upstream returned HTTP {exc.code}")
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            cause: BaseException = exc
            retry_delay = _retry_after_seconds(exc)
        except TimeoutError as exc:
            if response is not None:
                response.close()
            error = CoverUpstreamError("cover upstream request timed out")
            retryable = True
            cause = exc
            retry_delay = None
        except (URLError, HTTPException, OSError) as exc:
            if response is not None:
                response.close()
            error = CoverUpstreamError("cover upstream request failed")
            retryable = True
            cause = exc
            retry_delay = None
        except ValueError as exc:
            if response is not None:
                response.close()
            if slot_owned:
                origin_slot.release()
                slot_owned = False
            raise CoverUpstreamError("cover upstream request failed") from exc

        if not retryable or attempt + 1 >= MAX_COVER_ATTEMPTS:
            if slot_owned:
                origin_slot.release()
                slot_owned = False
            raise error from cause
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if slot_owned:
                origin_slot.release()
                slot_owned = False
            raise error from cause
        delay = retry_delay if retry_delay is not None else COVER_RETRY_DELAY_SECONDS
        _sleep(min(delay, remaining / 2))

    if slot_owned:
        origin_slot.release()
    raise CoverUpstreamError("cover upstream request failed")


def _policy_for_source(
    source: str,
    url: str,
    settings: dict[str, Any],
) -> _CoverPolicy:
    sites = settings.get("sites", []) if isinstance(settings, dict) else []
    if not isinstance(sites, list):
        raise CoverUpstreamError("cover source configuration is invalid")

    selected: dict[str, Any] | None = None
    for site in sites:
        if not isinstance(site, dict):
            continue
        if site.get("id") != source:
            continue
        if not site.get("enabled") or site.get("parser_profile") not in METADATA_PROFILES:
            break
        selected = site
        break
    if selected is None:
        raise CoverRequestError("cover source is unavailable")

    base_url = str(selected.get("base_url") or "").strip().rstrip("/")
    try:
        parsed = _split_http_url(base_url)
    except CoverRequestError as exc:
        raise CoverUpstreamError("cover source configuration is invalid") from exc
    if parsed.query or parsed.fragment:
        raise CoverUpstreamError("cover source configuration is invalid")
    if not PublicHostResolver(max_hosts=1).is_public(str(parsed.hostname or "")):
        raise CoverUpstreamError("cover source must resolve to a public address")

    profile = str(selected.get("parser_profile") or "")
    target = _split_http_url(url)
    if profile == "javbus":
        return _javbus_cover_policy(parsed, target, referer=base_url + "/")
    if profile == "fc2":
        return _fc2_cover_policy(target, default_referer=base_url + "/")
    if profile in METADATA_CATALOG:
        if not image_url_allowed(profile, url, base_url):
            raise CoverRequestError("cover URL is outside the configured source")
        if not PublicHostResolver(max_hosts=1).is_public(str(target.hostname or "")):
            raise CoverRequestError("cover URL host must resolve to a public address")
        return _CoverPolicy(origin=_origin(target), referer=base_url + "/",
                            path_pattern=re.compile(re.escape(target.path)))

    target_host = str(target.hostname or "").rstrip(".").lower()
    if (
        target.scheme.lower() != "https"
        or (target.port or 443) != 443
        or not _is_javdb_cdn_host(target_host)
    ):
        raise CoverRequestError("cover URL is outside the configured source")
    if not PublicHostResolver(max_hosts=1).is_public(target_host):
        raise CoverRequestError("cover URL host must resolve to a public address")
    return _CoverPolicy(
        origin=_origin(target),
        referer=base_url + "/",
        path_prefixes=("/covers/", "/samples/"),
    )


def _validate_cover_url(url: str, policy: _CoverPolicy) -> None:
    parsed = _split_http_url(url)
    if _origin(parsed) != policy.origin:
        raise CoverRequestError("cover URL is outside the configured source")

    decoded_path = parsed.path
    for _ in range(6):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    if (
        _PERCENT_ESCAPE.search(decoded_path)
        or "\\" in decoded_path
        or "\x00" in decoded_path
    ):
        raise CoverRequestError("cover URL path is invalid")
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise CoverRequestError("cover URL path is invalid")
    normalized_path = posixpath.normpath(decoded_path)
    prefix_allowed = any(
        normalized_path.startswith(prefix) for prefix in policy.path_prefixes
    )
    pattern_allowed = bool(
        policy.path_pattern and policy.path_pattern.fullmatch(normalized_path)
    )
    if not prefix_allowed and not pattern_allowed:
        raise CoverRequestError("cover URL path is not allowed")


def _is_javdb_cdn_host(hostname: str) -> bool:
    return hostname == "jdbstatic.com" or hostname.endswith(".jdbstatic.com")


def _fc2_cover_policy(
    target: SplitResult,
    *,
    default_referer: str,
) -> _CoverPolicy:
    target_host = str(target.hostname or "").rstrip(".").lower()
    if (
        target.scheme.lower() != "https"
        or (target.port or 443) != 443
        or bool(target.query)
    ):
        raise CoverRequestError("cover URL is outside the configured source")
    pattern = fc2_image_path_pattern(target_host)
    referer = fc2_image_referer(target_host, avsox_referer=default_referer)
    if pattern is None or referer is None:
        raise CoverRequestError("cover URL is outside the configured source")
    if not PublicHostResolver(max_hosts=1).is_public(target_host):
        raise CoverRequestError("cover URL host must resolve to a public address")
    return _CoverPolicy(
        origin=_origin(target),
        referer=referer,
        path_pattern=pattern,
    )


def _javbus_cover_policy(
    base: SplitResult,
    target: SplitResult,
    *,
    referer: str,
) -> _CoverPolicy:
    base_origin = _origin(base)
    target_origin = _origin(target)
    if target_origin == base_origin:
        return _CoverPolicy(
            origin=base_origin,
            referer=referer,
            path_prefixes=("/pics/",),
        )

    base_host = str(base.hostname or "").rstrip(".").lower()
    target_host = str(target.hostname or "").rstrip(".").lower()
    site_domain = base_host.removeprefix("www.")
    is_site_cdn = bool(site_domain) and target_host == f"pics.{site_domain}"
    dmm_path_prefixes = _JAVBUS_DMM_PATH_PREFIXES.get(target_host)
    is_dmm_cdn = dmm_path_prefixes is not None
    if (
        target.scheme.lower() != "https"
        or (target.port or 443) != 443
        or not (is_site_cdn or is_dmm_cdn)
    ):
        raise CoverRequestError("cover URL is outside the configured source")
    if not PublicHostResolver(max_hosts=1).is_public(target_host):
        raise CoverRequestError("cover URL host must resolve to a public address")
    return _CoverPolicy(
        origin=target_origin,
        referer=referer,
        path_prefixes=(
            dmm_path_prefixes
            if dmm_path_prefixes is not None
            else _JAVBUS_CDN_PATH_PREFIXES
        ),
    )


def _split_http_url(url: str) -> SplitResult:
    if not isinstance(url, str) or not url or url != url.strip() or len(url) > 4096:
        raise CoverRequestError("cover URL is invalid")
    if "#" in url:
        raise CoverRequestError("cover URL fragments are not allowed")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise CoverRequestError("cover URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CoverRequestError("cover URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise CoverRequestError("cover URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise CoverRequestError("cover URL credentials are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise CoverRequestError("cover URL is invalid")
    return parsed


def _origin(parsed: SplitResult) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    return scheme, str(parsed.hostname).lower(), parsed.port or default_port


def _origin_limiter(origin: tuple[str, str, int]) -> threading.BoundedSemaphore:
    with _ORIGIN_LIMITERS_LOCK:
        limiter = _ORIGIN_LIMITERS.get(origin)
        if limiter is None:
            limiter = threading.BoundedSemaphore(COVER_ORIGIN_CONCURRENCY)
            _ORIGIN_LIMITERS[origin] = limiter
            _prune_origin_limiters_locked(protected=origin)
        else:
            _ORIGIN_LIMITERS.move_to_end(origin)
        return limiter


def _prune_origin_limiters_locked(*, protected: tuple[str, str, int]) -> None:
    for key, candidate in tuple(_ORIGIN_LIMITERS.items()):
        if len(_ORIGIN_LIMITERS) <= _MAX_ORIGIN_LIMITERS:
            return
        if key == protected:
            continue
        acquired = 0
        for _index in range(COVER_ORIGIN_CONCURRENCY):
            if not candidate.acquire(blocking=False):
                break
            acquired += 1
        for _index in range(acquired):
            candidate.release()
        if acquired == COVER_ORIGIN_CONCURRENCY:
            _ORIGIN_LIMITERS.pop(key, None)


def _retry_after_seconds(error: HTTPError) -> float | None:
    """Return a bounded Retry-After delay for rate-limited image origins."""

    raw = str(error.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds <= 0:
        return 0.0
    return min(seconds, 5.0)


def _response_content_type(headers: Any) -> str:
    raw = str(headers.get("Content-Type") or "")
    return raw.split(";", 1)[0].strip().lower()


def _content_length(headers: Any) -> int | None:
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
