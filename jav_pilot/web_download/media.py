from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from contextlib import contextmanager
from enum import Enum
from http.client import IncompleteRead as HttpIncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping
from urllib.parse import urljoin, urlsplit

from ..net.network_guard import PublicHostResolver
from .resume import MediaPlaylist, parse_media_playlist


MEDIA_HOST = "surrit.com"
_PROVIDER_MEDIA_SUFFIXES = {
    "missav": ("surrit.com",),
    "supjav": (
        "cdn-centaurus.com",
        "premilkyway.com",
        "streamtape.com",
        "streamta.pe",
        "tapecontent.net",
    ),
    "jable": ("mushroomtrack.com",),
    "javnoni": ("tnmr.org",),
    "kissjav": ("kissjav.li",),
}
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PROBE_SEGMENT_BYTES = 32 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_MEDIA_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PROBE_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-language",
        "cookie",
        "origin",
        "referer",
        "user-agent",
    }
)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ACTIVE_TOTAL_TIMEOUT: ContextVar[float | None] = ContextVar(
    "active_surrit_total_timeout",
    default=None,
)
_ACTIVE_MEDIA_PROVIDER: ContextVar[str] = ContextVar(
    "active_web_download_media_provider", default="missav"
)
_RETRYABLE_MEDIA_HTTP_STATUSES = frozenset({408, 425, 429})
_REFRESH_MEDIA_HTTP_STATUSES = frozenset({401, 403, 404, 410})


class SafeMediaError(RuntimeError):
    pass


class MediaTransportDisposition(Enum):
    RETRY_SAME_URL = "retry_same_url"
    REFRESH_MANIFEST = "refresh_manifest"
    FATAL = "fatal"


@contextmanager
def media_provider_scope(provider: object):
    clean = str(provider or "").strip().lower()
    if clean not in _PROVIDER_MEDIA_SUFFIXES:
        raise SafeMediaError("media provider was rejected")
    token = _ACTIVE_MEDIA_PROVIDER.set(clean)
    try:
        yield
    finally:
        _ACTIVE_MEDIA_PROVIDER.reset(token)


def classify_media_transport_error(
    error: BaseException,
) -> MediaTransportDisposition:
    """Classify only typed yt-dlp transport failures, never local I/O errors."""

    if isinstance(error, HttpIncompleteRead):
        return MediaTransportDisposition.RETRY_SAME_URL

    try:
        from yt_dlp.networking.exceptions import (
            CertificateVerifyError,
            HTTPError,
            TransportError,
        )
    except Exception:  # noqa: BLE001 - dependency is optional outside Docker.
        return MediaTransportDisposition.FATAL

    if isinstance(error, HTTPError):
        status = getattr(error, "status", None)
        if type(status) is not int:
            return MediaTransportDisposition.FATAL
        if status in _REFRESH_MEDIA_HTTP_STATUSES:
            return MediaTransportDisposition.REFRESH_MANIFEST
        if status in _RETRYABLE_MEDIA_HTTP_STATUSES or 500 <= status <= 599:
            return MediaTransportDisposition.RETRY_SAME_URL
        return MediaTransportDisposition.FATAL
    if isinstance(error, CertificateVerifyError):
        return MediaTransportDisposition.FATAL
    if isinstance(error, TransportError):
        return MediaTransportDisposition.RETRY_SAME_URL
    return MediaTransportDisposition.FATAL


@dataclass(frozen=True, slots=True)
class SafeMediaResource:
    body: bytes = field(repr=False)
    status: int
    headers: dict[str, str] = field(repr=False)
    url: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "SafeMediaResource(body=<redacted>, status="
            f"{self.status}, headers=<redacted>, url=<redacted>)"
        )


class SilentYtdlpLogger:
    def debug(self, message: str) -> None:
        return

    def info(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        return

    def error(self, message: str) -> None:
        return


def allowlisted_youtube_dl_type(yt_dlp_module: object):
    """Return the sole yt-dlp request handler permitted to access media."""

    try:
        import curl_cffi.requests as curl_requests
        from curl_cffi.const import CurlOpt
        from yt_dlp.networking._curlcffi import CurlCFFIRH
        from yt_dlp.networking.exceptions import HTTPError, RequestError
    except Exception as exc:  # noqa: BLE001 - exact pinned runtime dependency.
        raise SafeMediaError("secure yt-dlp networking is unavailable") from exc

    class _NoRedirectSession(curl_requests.Session):
        def request(self, *args: object, **kwargs: object):
            kwargs.pop("max_redirects", None)
            kwargs["allow_redirects"] = False
            total_timeout = _ACTIVE_TOTAL_TIMEOUT.get()
            # curl-cffi clones this handle for streaming responses.
            self.curl.setopt(
                CurlOpt.TIMEOUT_MS,
                max(1, int(total_timeout * 1000)) if total_timeout is not None else 0,
            )
            return super().request(*args, **kwargs)

    class SurritOnlyCurlCFFIRH(CurlCFFIRH):
        def __init__(self, *args: object, **kwargs: object) -> None:
            # Streamtape resolves its allowlisted landing host to one
            # allowlisted tapecontent CDN host.  Other providers retain the
            # stricter single-host budget.
            self._media_resolver = PublicHostResolver(
                max_hosts=2 if _ACTIVE_MEDIA_PROVIDER.get() == "supjav" else 1
            )
            super().__init__(*args, **kwargs)

        def _create_instance(self, cookiejar: object = None):
            return _NoRedirectSession(cookies=cookiejar)

        def _check_extensions(self, extensions: dict[str, object]) -> None:
            extensions.pop("total_deadline", None)
            super()._check_extensions(extensions)

        def send(self, request: object):
            current = request.copy()
            for redirect_count in range(_MAX_MEDIA_REDIRECTS + 1):
                try:
                    require_allowed_media_url(
                        current.url,
                        resolver=self._media_resolver,
                    )
                except SafeMediaError as exc:
                    raise RequestError(
                        "media request was rejected by the host policy",
                        handler=self,
                    ) from exc
                if current.method not in {"GET", "HEAD"}:
                    raise RequestError(
                        "media request method was rejected",
                        handler=self,
                    )
                raw_total_deadline = current.extensions.get("total_deadline")
                if isinstance(raw_total_deadline, (int, float)) and not isinstance(
                    raw_total_deadline, bool
                ):
                    total_timeout = float(raw_total_deadline) - time.monotonic()
                    if total_timeout <= 0:
                        raise RequestError(
                            "media request deadline was exceeded",
                            handler=self,
                        )
                else:
                    total_timeout = None
                timeout_token = _ACTIVE_TOTAL_TIMEOUT.set(total_timeout)
                try:
                    response = super().send(current)
                except HTTPError as exc:
                    if exc.status not in _REDIRECT_STATUSES:
                        raise
                    location = exc.response.get_header("Location")
                    exc.close()
                    if redirect_count >= _MAX_MEDIA_REDIRECTS or not location:
                        raise RequestError(
                            "media redirect was rejected",
                            handler=self,
                        ) from None
                    next_url = urljoin(current.url, str(location))
                    try:
                        require_allowed_media_url(
                            next_url,
                            resolver=self._media_resolver,
                        )
                    except SafeMediaError as policy_error:
                        raise RequestError(
                            "media redirect was rejected by the host policy",
                            handler=self,
                        ) from policy_error
                    redirected = current.copy()
                    redirected.url = next_url
                    if exc.status == 303 and redirected.method != "HEAD":
                        redirected.method = "GET"
                        redirected.data = None
                    current = redirected
                    continue
                finally:
                    _ACTIVE_TOTAL_TIMEOUT.reset(timeout_token)
                try:
                    require_allowed_media_url(
                        response.url,
                        resolver=self._media_resolver,
                    )
                except SafeMediaError as exc:
                    response.close()
                    raise RequestError(
                        "media response was rejected by the host policy",
                        handler=self,
                    ) from exc
                return response
            raise RequestError("media redirect limit was exceeded", handler=self)

    class SurritOnlyYoutubeDL(yt_dlp_module.YoutubeDL):
        def build_request_director(self, handlers: object, preferences: object = None):
            return super().build_request_director(
                (SurritOnlyCurlCFFIRH,),
                preferences,
            )

    return SurritOnlyYoutubeDL


def require_allowed_media_url(
    url: object,
    *,
    resolver: PublicHostResolver,
) -> str:
    raw = str(url or "")
    if not raw or len(raw) > 8192 or any(character in raw for character in "\r\n\0"):
        raise SafeMediaError("media URL was rejected")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SafeMediaError("media URL was rejected") from exc
    hostname = (parsed.hostname or "").lower()
    provider = _ACTIVE_MEDIA_PROVIDER.get()
    suffixes = _PROVIDER_MEDIA_SUFFIXES.get(provider, ())
    allowed_host = any(
        hostname == suffix or (suffix != MEDIA_HOST and hostname.endswith(f".{suffix}"))
        for suffix in suffixes
    )
    if (
        parsed.scheme != "https"
        or not allowed_host
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not resolver.is_public(hostname)
    ):
        raise SafeMediaError("media URL was rejected")
    return raw


def fetch_safe_media_resource(
    url: object,
    headers: Mapping[object, object] | object,
    *,
    max_bytes: int,
    timeout_seconds: float,
    range_header: str | None = None,
) -> SafeMediaResource:
    """Fetch one bounded surrit resource through the sole safe Curl handler."""

    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_MANIFEST_BYTES
    ):
        raise SafeMediaError("media response size limit is invalid")
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        raise SafeMediaError("media request timeout is invalid") from None
    if not 1.0 <= clean_timeout <= 120.0:
        raise SafeMediaError("media request timeout is invalid")

    request_headers = dict(_clean_probe_headers(headers))
    if range_header is not None:
        match = re.fullmatch(r"bytes=0-([0-9]{1,8})", str(range_header))
        if match is None or int(match.group(1)) >= max_bytes:
            raise SafeMediaError("media range request is invalid")
        request_headers["Range"] = match.group(0)

    try:
        import yt_dlp
        from yt_dlp.networking import Request
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception:  # noqa: BLE001 - exact runtime dependency is optional locally.
        raise SafeMediaError("secure media request is unavailable") from None

    safe_url = require_allowed_media_url(
        url,
        resolver=PublicHostResolver(max_hosts=1),
    )
    deadline = time.monotonic() + clean_timeout
    safe_youtube_dl = allowlisted_youtube_dl_type(yt_dlp)
    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtdlpLogger(),
        "http_headers": dict(request_headers),
        "impersonate": ImpersonateTarget(client="chrome", version="131"),
        "socket_timeout": clean_timeout,
        "_no_ytdl_file": True,
    }
    try:
        with safe_youtube_dl(options) as downloader:
            request = Request(
                safe_url,
                headers=dict(request_headers),
                extensions={
                    "timeout": clean_timeout,
                    "total_deadline": deadline,
                },
            )
            with downloader.urlopen(request) as response:
                final_url = require_allowed_media_url(
                    response.url,
                    resolver=PublicHostResolver(max_hosts=1),
                )
                status = int(getattr(response, "status", 0))
                response_headers = {
                    str(name): str(value)
                    for name, value in dict(getattr(response, "headers", {})).items()
                }
                body = b""
                if range_header is None or status == 206:
                    declared = _safe_content_length(response_headers)
                    if declared is not None and declared > max_bytes:
                        raise SafeMediaError(
                            "media response exceeds the safe size limit"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        _remaining_probe_seconds(deadline)
                        chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise SafeMediaError("media response is invalid")
                        total += len(chunk)
                        if total > max_bytes:
                            raise SafeMediaError(
                                "media response exceeds the safe size limit"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
    except SafeMediaError:
        raise
    except Exception:  # noqa: BLE001 - credentials and signed URLs stay private.
        raise SafeMediaError("secure media request failed") from None
    return SafeMediaResource(
        body=body,
        status=status,
        headers=response_headers,
        url=final_url,
    )


def validate_native_hls_manifest(
    manifest: str,
    manifest_url: str,
) -> MediaPlaylist:
    try:
        playlist = parse_media_playlist(
            manifest,
            allow_aes128=_ACTIVE_MEDIA_PROVIDER.get() == "jable",
        )
    except ValueError as exc:
        raise SafeMediaError(str(exc)) from exc

    resolver = PublicHostResolver(max_hosts=1)
    for raw_line in manifest.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("#"):
            require_allowed_media_url(
                urljoin(manifest_url, line),
                resolver=resolver,
            )
            continue
        for match in re.finditer(r'URI="([^"\r\n]+)"', line):
            require_allowed_media_url(
                urljoin(manifest_url, match.group(1)),
                resolver=resolver,
            )
    return playlist


def probe_media_dimensions(
    manifest_url: object,
    headers: Mapping[object, object] | object,
    *,
    timeout_seconds: float,
    temp_root: Path | None = None,
) -> tuple[int, int]:
    """Probe one bounded HLS segment and return local-only video dimensions."""

    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        raise SafeMediaError("media dimension probe timeout is invalid") from None
    if not 1.0 <= clean_timeout <= 30.0:
        raise SafeMediaError("media dimension probe timeout is invalid")

    if temp_root is not None:
        if (
            not temp_root.is_absolute()
            or temp_root.is_symlink()
            or not temp_root.is_dir()
        ):
            raise SafeMediaError("media dimension probe directory is invalid")
        probe_root = temp_root.resolve(strict=True)
    else:
        probe_root = None

    try:
        import yt_dlp
        from yt_dlp.networking import Request
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception:  # noqa: BLE001 - runtime dependency is optional outside Docker.
        raise SafeMediaError("secure media dimension probe is unavailable") from None

    deadline = time.monotonic() + clean_timeout
    clean_headers = _clean_probe_headers(headers)
    safe_youtube_dl = allowlisted_youtube_dl_type(yt_dlp)
    initial_url = require_allowed_media_url(
        manifest_url,
        resolver=PublicHostResolver(max_hosts=1),
    )
    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtdlpLogger(),
        "http_headers": dict(clean_headers),
        "impersonate": ImpersonateTarget(client="chrome", version="131"),
        "socket_timeout": clean_timeout,
        "_no_ytdl_file": True,
    }

    try:
        with TemporaryDirectory(
            prefix=".missav-media-probe-",
            dir=str(probe_root) if probe_root is not None else None,
        ) as temporary_directory:
            segment_path = Path(temporary_directory) / "segment.ts"
            with safe_youtube_dl(options) as downloader:
                manifest_timeout = _remaining_probe_seconds(deadline)
                manifest_request = Request(
                    initial_url,
                    headers=dict(clean_headers),
                    extensions={
                        "timeout": manifest_timeout,
                        "total_deadline": deadline,
                    },
                )
                with downloader.urlopen(manifest_request) as response:
                    final_manifest_url = require_allowed_media_url(
                        response.url,
                        resolver=PublicHostResolver(max_hosts=1),
                    )
                    manifest_bytes = response.read(MAX_MANIFEST_BYTES + 1)
                _remaining_probe_seconds(deadline)
                if (
                    not isinstance(manifest_bytes, bytes)
                    or len(manifest_bytes) > MAX_MANIFEST_BYTES
                ):
                    raise SafeMediaError("media manifest exceeds the safe size limit")
                try:
                    manifest_text = manifest_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise SafeMediaError("media manifest is not valid UTF-8") from None
                playlist = validate_native_hls_manifest(
                    manifest_text,
                    final_manifest_url,
                )
                segment_url = require_allowed_media_url(
                    urljoin(final_manifest_url, playlist.segments[0].uri),
                    resolver=PublicHostResolver(max_hosts=1),
                )
                segment_timeout = _remaining_probe_seconds(deadline)
                segment_request = Request(
                    segment_url,
                    headers=dict(clean_headers),
                    extensions={
                        "timeout": segment_timeout,
                        "total_deadline": deadline,
                    },
                )
                with downloader.urlopen(segment_request) as response:
                    require_allowed_media_url(
                        response.url,
                        resolver=PublicHostResolver(max_hosts=1),
                    )
                    with segment_path.open("xb") as output:
                        os.chmod(segment_path, stat.S_IRUSR | stat.S_IWUSR)
                        written = 0
                        while True:
                            _remaining_probe_seconds(deadline)
                            chunk = response.read(
                                min(
                                    _COPY_CHUNK_BYTES,
                                    MAX_PROBE_SEGMENT_BYTES + 1 - written,
                                )
                            )
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes):
                                raise SafeMediaError(
                                    "media segment response is invalid"
                                )
                            written += len(chunk)
                            if written > MAX_PROBE_SEGMENT_BYTES:
                                raise SafeMediaError(
                                    "media probe segment exceeds the safe size limit"
                                )
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
            if not segment_path.is_file() or segment_path.stat().st_size <= 0:
                raise SafeMediaError("media dimension probe returned no data")
            remaining = _remaining_probe_seconds(deadline)
            return _probe_local_dimensions(
                segment_path,
                timeout_seconds=min(15.0, remaining),
            )
    except SafeMediaError:
        raise
    except Exception:  # noqa: BLE001 - network errors can contain signed media URLs.
        raise SafeMediaError("media dimensions could not be verified") from None


def _clean_probe_headers(
    headers: Mapping[object, object] | object,
) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    clean: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip().lower()
        value = str(raw_value or "").strip()
        if (
            name not in _PROBE_HEADER_NAMES
            or not _HEADER_NAME_RE.fullmatch(name)
            or not value
            or len(value) > 8192
            or any(character in value for character in "\r\n\0")
        ):
            continue
        clean[name] = value
    return clean


def _safe_content_length(headers: Mapping[str, str]) -> int | None:
    raw = next(
        (
            value
            for name, value in headers.items()
            if name.casefold() == "content-length"
        ),
        None,
    )
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _remaining_probe_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SafeMediaError("media dimension probe timed out")
    return remaining


def _probe_local_dimensions(
    path: Path,
    *,
    timeout_seconds: float,
) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise SafeMediaError("media dimension probe returned no data")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-f",
        "mpegts",
        "-i",
        str(path),
        "-show_entries",
        "stream=codec_type,width,height",
        "-of",
        "json",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        raise SafeMediaError("media dimensions could not be verified") from None
    if result.returncode != 0:
        raise SafeMediaError("media dimensions could not be verified")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SafeMediaError("media dimensions could not be verified") from None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    video_stream = (
        next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if isinstance(streams, list)
        else None
    )
    if video_stream is None:
        raise SafeMediaError("media dimensions could not be verified")
    width = video_stream.get("width")
    height = video_stream.get("height")
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not 64 <= width <= 16_384
        or not 64 <= height <= 16_384
    ):
        raise SafeMediaError("media dimensions could not be verified")
    return width, height
