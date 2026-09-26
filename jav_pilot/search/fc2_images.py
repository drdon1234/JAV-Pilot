from __future__ import annotations

import re
import posixpath


_AVSOX_PPV_IMAGE_PATH_RE = re.compile(
    r"/storage/fc2ppv/(?P<product_id>\d{2,9})/[^/]+\.(?:avif|jpe?g|png|webp)",
    flags=re.IGNORECASE,
)
_AVSOX_FC2_IMAGE_PATH_RE = re.compile(
    r"/storage/fc2/movies/FC2-PPV/(?P<product_id>\d{2,9})/"
    r"[^/]+\.(?:avif|jpe?g|png|webp)",
    flags=re.IGNORECASE,
)
_AVSOX_SCREENSHOT_IMAGE_PATH_RE = re.compile(
    r"/ave/vodimages/screenshot/(?:small|large)/"
    r"FC2-PPV-(?P<product_id>\d{2,9})/"
    r"[^/]+\.(?:avif|jpe?g|png|webp)",
    flags=re.IGNORECASE,
)
_AVSOX_IMAGE_PATH_RE = re.compile(
    r"(?:/storage/fc2ppv/\d{2,9}/[^/]+\.(?:avif|jpe?g|png|webp)|"
    r"/storage/fc2/movies/FC2-PPV/\d{2,9}/[^/]+\.(?:avif|jpe?g|png|webp)|"
    r"/ave/vodimages/screenshot/(?:small|large)/FC2-PPV-\d{2,9}/"
    r"[^/]+\.(?:avif|jpe?g|png|webp))",
    flags=re.IGNORECASE,
)
_OFFICIAL_STORAGE_HOST_RE = re.compile(r"storage\d+\.contents\.fc2\.com")
_OFFICIAL_STORAGE_IMAGE_PATH_RE = re.compile(
    r"/file/.+\.(?:avif|jpe?g|png|webp)",
    flags=re.IGNORECASE,
)
_OFFICIAL_THUMBNAIL_HOST_RE = re.compile(r"contents-thumbnail\d*\.fc2\.com")
_OFFICIAL_THUMBNAIL_IMAGE_PATH_RE = re.compile(
    r"/w\d+/storage\d+\.contents\.fc2\.com/file/"
    r".+\.(?:avif|jpe?g|png|webp)",
    flags=re.IGNORECASE,
)
_PPV_DATABANK_IMAGE_PATH_RE = re.compile(
    r"/article/(?P<product_id>\d{2,9})/img/(?:thumb|p[sl]\d+)\.webp",
    flags=re.IGNORECASE,
)


def fc2_image_path_pattern(hostname: object) -> re.Pattern[str] | None:
    host = str(hostname or "").rstrip(".").lower()
    if host == "file.netcdn.space":
        return _AVSOX_IMAGE_PATH_RE
    if _OFFICIAL_STORAGE_HOST_RE.fullmatch(host):
        return _OFFICIAL_STORAGE_IMAGE_PATH_RE
    if _OFFICIAL_THUMBNAIL_HOST_RE.fullmatch(host):
        return _OFFICIAL_THUMBNAIL_IMAGE_PATH_RE
    if host == "ppvdatabank.com":
        return _PPV_DATABANK_IMAGE_PATH_RE
    return None


def fc2_image_path_allowed(
    hostname: object,
    path: object,
    *,
    product_id: str | None = None,
) -> bool:
    host = str(hostname or "").rstrip(".").lower()
    value = str(path or "")
    if (
        not value.startswith("/")
        or "%" in value
        or "\\" in value
        or "\x00" in value
        or posixpath.normpath(value) != value
    ):
        return False
    patterns: tuple[re.Pattern[str], ...]
    if host == "file.netcdn.space":
        patterns = (
            _AVSOX_PPV_IMAGE_PATH_RE,
            _AVSOX_FC2_IMAGE_PATH_RE,
            _AVSOX_SCREENSHOT_IMAGE_PATH_RE,
        )
    else:
        pattern = fc2_image_path_pattern(host)
        patterns = (pattern,) if pattern is not None else ()
    for pattern in patterns:
        match = pattern.fullmatch(value)
        if match is None:
            continue
        path_product_id = match.groupdict().get("product_id")
        return (
            product_id is None
            or path_product_id is None
            or path_product_id == product_id
        )
    return False


def fc2_image_referer(hostname: object, *, avsox_referer: str) -> str | None:
    host = str(hostname or "").rstrip(".").lower()
    if host == "file.netcdn.space":
        return avsox_referer
    if _OFFICIAL_STORAGE_HOST_RE.fullmatch(
        host
    ) or _OFFICIAL_THUMBNAIL_HOST_RE.fullmatch(host):
        return "https://adult.contents.fc2.com/"
    if host == "ppvdatabank.com":
        return "https://ppvdatabank.com/"
    return None


__all__ = [
    "fc2_image_path_allowed",
    "fc2_image_path_pattern",
    "fc2_image_referer",
]
