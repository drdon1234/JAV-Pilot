"""Read short catalog previews from the video sites' preview CDNs."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..net.curl_http import fetch_bytes
from ..net.http_client import FetchError


_ASSETS = {
    "missav": ("fourhoi.com", re.compile(r"/[a-z0-9._-]+/preview\.(?:mp4|gif)")),
    "jable": ("assets.fs1.app", re.compile(r"/contents/videos_screenshots/\d+/(\d+)/\1_preview\.(?:mp4|gif)")),
    "av123": ("icdn.123av.me", re.compile(r"/preview/[a-f0-9]{2}/[a-z0-9_-]+/preview\.png")),
}


def fetch_preview(source: str, url: str, referer: str) -> tuple[bytes, str]:
    host, pattern = _ASSETS[source]
    target = urlsplit(url)
    if (len(url) > 2048 or not url.isascii() or not url.isprintable() or target.scheme != "https"
            or target.netloc != host or target.fragment or not pattern.fullmatch(target.path)):
        raise ValueError("预览地址无效")
    body = fetch_bytes(url, allowed_origins=(f"https://{host}",), timeout=12,
                       max_bytes=8 * 1024 * 1024, headers={"Referer": referer + "/", "Accept": "video/mp4,image/gif,*/*;q=0.5"})
    if body[4:8] == b"ftyp":
        return body, "video/mp4"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return body, "image/gif"
    raise FetchError("Preview format is unavailable")
