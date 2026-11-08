"""Bounded portrait validation and thumbnail generation for local library covers."""

from __future__ import annotations

import io
import threading
import warnings
from collections import OrderedDict

from PIL import Image, ImageOps, UnidentifiedImageError

from .folders import poster_path
from .playback import open_library_file

_CACHE: OrderedDict[str, bytes] = OrderedDict()
_LOCK = threading.Lock()


def portrait_thumbnail(root, item: dict) -> tuple[bytes, str]:
    if item["presence"] != "present" or item["portrait_status"] != "present":
        raise ValueError("Portrait is unavailable")
    with open_library_file(root, poster_path(item)) as (stream, size, identity):
        if size > 16 * 1024 * 1024:
            raise ValueError("Portrait is too large")
        with _LOCK:
            if identity in _CACHE:
                _CACHE.move_to_end(identity)
                return _CACHE[identity], identity
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(stream) as original:
                    if original.width * original.height > 24_000_000:
                        raise ValueError("Portrait is too large")
                    image = ImageOps.exif_transpose(original)
                    if image.height <= image.width:
                        raise ValueError("A portrait poster is required")
                    image.thumbnail((240, 360))
                    output = io.BytesIO()
                    image.convert("RGB").save(output, format="JPEG", quality=82)
                    raw = output.getvalue()
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ValueError("Invalid portrait") from exc
    with _LOCK:
        _CACHE[identity] = raw
        while len(_CACHE) > 256:
            _CACHE.popitem(last=False)
    return raw, identity


def cover_entry(root, item: dict) -> dict | None:
    try:
        _, identity = portrait_thumbnail(root, item)
    except (ValueError, OSError):
        return None
    return {
        "entry_id": item["entry_id"],
        "code": item["code"],
        "title": item["title"],
        "identity": identity,
    }
