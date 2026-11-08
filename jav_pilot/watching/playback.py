"""Short-lived playback sessions with opaque, allowlisted same-origin media URLs."""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from ..net.network_guard import PublicHostResolver
from ..web_download.media import (
    SafeMediaError, SilentYtdlpLogger, allowlisted_youtube_dl_type,
    media_provider_scope, require_allowed_media_url,
)
from ..web_download.providers import ProviderManifest

_LOCK = threading.RLock()
_SLOTS = threading.BoundedSemaphore(8)
_SESSIONS: dict[str, PlaybackSession] = {}
MAX_BODY = 32 * 1024 * 1024
TTL = 2 * 60 * 60
CACHE_LIMIT = 128 * 1024 * 1024
CACHE_SECONDS = 180
_CACHE: OrderedDict[tuple[str, str, str], tuple[float, tuple]] = OrderedDict()
_CACHE_BYTES = 0


def _evict_cache(key) -> None:
    global _CACHE_BYTES
    cached = _CACHE.pop(key, None)
    if cached:
        _CACHE_BYTES -= len(cached[1][0])


def _cached_media(token: str, resource: str, byte_range: str):
    with _LOCK:
        now = time.monotonic()
        for key, (expires, _) in list(_CACHE.items()):
            if expires < now or key[0] not in _SESSIONS:
                _evict_cache(key)
        key = (token, resource, byte_range)
        if key in _CACHE:
            result = _CACHE[key][1]
            _CACHE.move_to_end(key)
            return result
        requested = re.fullmatch(r"bytes=(\d+)-(\d+)", byte_range)
        if requested:
            start, end = map(int, requested.groups())
            for cached_key in reversed(_CACHE):
                if cached_key[:2] != (token, resource):
                    continue
                raw, mime, status, headers = _CACHE[cached_key][1]
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
                if status != 206 or not match:
                    continue
                first, last, total = map(int, match.groups())
                if first <= start <= last and len(raw) == last - first + 1:
                    last = min(last, end)
                    return raw[start-first:last-first+1], mime, 206, {**headers, "content-range": f"bytes {start}-{last}/{total}"}
    return None


def _cache_media(token: str, resource: str, byte_range: str, result: tuple) -> None:
    global _CACHE_BYTES
    if result[2] not in {200, 206} or len(result[0]) > MAX_BODY:
        return
    with _LOCK:
        if token not in _SESSIONS:
            return
        key = (token, resource, byte_range)
        _evict_cache(key)
        _CACHE[key] = (time.monotonic() + CACHE_SECONDS, result)
        _CACHE_BYTES += len(result[0])
        while _CACHE_BYTES > CACHE_LIMIT:
            _evict_cache(next(iter(_CACHE)))


@dataclass
class PlaybackSession:
    manifest: ProviderManifest = field(repr=False)
    expires: float
    resources: dict[str, str] = field(default_factory=dict, repr=False)
    reverse: dict[str, str] = field(default_factory=dict, repr=False)
    resolver: PublicHostResolver = field(default_factory=lambda: PublicHostResolver(max_hosts=32), repr=False)


def _resource_url(token: str, session: PlaybackSession, url: str) -> str:
    # Reject a malicious playlist before it can manufacture proxy destinations.
    with _LOCK:
        with media_provider_scope(session.manifest.provider, allowed_hosts=session.manifest.allowed_media_hosts):
            require_allowed_media_url(url, resolver=session.resolver)
        key = session.reverse.get(url)
        if key is None:
            if len(session.resources) >= 20000:
                raise SafeMediaError("播放清单包含过多资源")
            key = secrets.token_hex(12)
            session.resources[key] = url
            session.reverse[url] = key
    return f"/api/watch/media?session={token}&resource={key}"


def create_session(manifest: ProviderManifest) -> dict:
    token = secrets.token_hex(24)
    session = PlaybackSession(manifest, time.monotonic() + TTL)
    with _LOCK:
        expired = [key for key, item in _SESSIONS.items() if item.expires < time.monotonic()]
        for key in expired:
            del _SESSIONS[key]
        if len(_SESSIONS) >= 64:
            raise ValueError("当前播放会话过多，请稍后重试")
        media_url = _resource_url(token, session, manifest.url)
        _SESSIONS[token] = session
    return {"session": token, "media_url": media_url, "media_kind": manifest.media_kind,
            "height": manifest.selected_height, "expires_in": TTL}


def close_session(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token, None)
        for key in list(_CACHE):
            if key[0] == token:
                _evict_cache(key)


def rewrite_playlist(document: str, base: str, token: str, session: PlaybackSession) -> bytes:
    if not document.lstrip().startswith("#EXTM3U"):
        raise SafeMediaError("播放清单无效，请重新解析")
    lines = []
    for line in document.splitlines():
        if line.startswith("#"):
            if line.startswith(("#EXT-X-SESSION-DATA:", "#EXT-X-CONTENT-STEERING:")):
                continue
            if re.search(r'URI\s*=\s*[^"\s]', line):
                raise SafeMediaError("播放清单地址格式不受支持")
            line = re.sub(r'URI="([^"\r\n]+)"', lambda m: 'URI="' + _resource_url(
                token, session, urljoin(base, m.group(1))
            ) + '"', line)
        elif line.strip():
            line = _resource_url(token, session, urljoin(base, line.strip()))
        lines.append(line)
    return ("\n".join(lines) + "\n").encode()


def media_resource(token: str, resource: str, range_header: str | None) -> tuple[bytes, str, int, dict]:
    with _LOCK:
        session = _SESSIONS.get(token)
        if session is None or session.expires < time.monotonic():
            _SESSIONS.pop(token, None)
            raise ValueError("播放链接已过期，请重新解析")
        url = session.resources.get(resource)
        if url is None:
            raise ValueError("播放资源不存在，请重新解析")
        session.expires = time.monotonic() + TTL
    if not _SLOTS.acquire(blocking=False):
        raise SafeMediaError("播放请求繁忙，请稍后重试")
    try:
        playlist = urlsplit(url).path.lower().endswith(".m3u8")
        mp4 = session.manifest.media_kind == "progressive"
        headers = dict(session.manifest.headers)
        range_limit = 1024 * 1024 if mp4 else MAX_BODY
        if range_header and not playlist:
            match = re.fullmatch(r"bytes=(\d{1,14})-(\d{0,14})", range_header)
            if match is None:
                raise ValueError("不支持的播放范围请求")
            start = int(match[1])
            end = min(int(match[2]) if match[2] else start + range_limit - 1, start + range_limit - 1)
            if end < start:
                raise ValueError("播放范围请求无效")
            headers["Range"] = f"bytes={start}-{end}"
        elif mp4:
            headers["Range"] = f"bytes=0-{range_limit - 1}"
        cached = None if playlist else _cached_media(token, resource, headers.get("Range", ""))
        if cached is not None:
            return cached
        body, content_type, status, response_headers, final_url = _fetch(session, url, headers, playlist)
        if playlist:
            body = rewrite_playlist(body.decode("utf-8-sig"), final_url, token, session)
            return body, "application/vnd.apple.mpegurl", 200, {}
        # Some CDNs serve transport streams with a desktop .ts file MIME type.
        suffix = urlsplit(final_url).path.lower().rsplit(".", 1)[-1]
        content_type = {"ts": "video/mp2t", "m4s": "video/mp4", "mp4": "video/mp4",
                        "aac": "audio/aac", "key": "application/octet-stream"}.get(suffix, content_type)
        result = body, content_type, status, response_headers
        _cache_media(token, resource, headers.get("Range", ""), result)
        return result
    finally:
        _SLOTS.release()


def _fetch(session: PlaybackSession, url: str, headers: dict, playlist: bool):
    import yt_dlp
    from yt_dlp.networking import Request
    from yt_dlp.networking.impersonate import ImpersonateTarget

    limit = 4 * 1024 * 1024 if playlist else MAX_BODY
    with media_provider_scope(session.manifest.provider, allowed_hosts=session.manifest.allowed_media_hosts):
        require_allowed_media_url(url, resolver=PublicHostResolver(max_hosts=1))
        client_type = allowlisted_youtube_dl_type(yt_dlp)
        with client_type({"quiet": True, "no_warnings": True, "logger": SilentYtdlpLogger(),
                          "impersonate": ImpersonateTarget(client="chrome"),
                          "socket_timeout": 20, "_no_ytdl_file": True}) as client:
            try:
                response = client.urlopen(Request(url, headers=headers, extensions={
                    "timeout": 20, "total_deadline": time.monotonic() + 25,
                }))
            except Exception as exc:
                if getattr(exc, 'status', None) == 403:
                    raise SafeMediaError("播放线路拒绝访问，请重新解析；如仍失败，请检查代理是否为站点和视频 CDN 使用同一出口") from None
                raise SafeMediaError("播放线路暂不可用，请重新解析或切换站点") from None
            with response:
                final = require_allowed_media_url(response.url, resolver=PublicHostResolver(max_hosts=1))
                if headers.get("Range") and response.status != 206:
                    raise SafeMediaError("线路未接受播放范围请求，请换一条线路")
                raw_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                declared = raw_headers.get("content-length")
                if declared and int(declared) > limit:
                    raise SafeMediaError("播放资源过大，请换一条线路")
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise SafeMediaError("播放资源过大，请换一条线路")
                content_type = raw_headers.get("content-type", "application/octet-stream").split(";")[0]
                if "html" in content_type or "javascript" in content_type or content_type == 'image/svg+xml':
                    raise SafeMediaError("线路返回了验证页，请重新解析")
                forwarded = {key: raw_headers[key] for key in ("content-range", "accept-ranges") if key in raw_headers}
                return body, content_type, response.status, forwarded, final
