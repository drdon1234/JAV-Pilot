from __future__ import annotations

from collections.abc import Sequence
import re
from types import MappingProxyType
from typing import Literal, TypeAlias, cast
import unicodedata


__all__ = [
    "DEFAULT_WEB_DOWNLOAD_VARIANT",
    "DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY",
    "MissavVariant",
    "WEB_DOWNLOAD_VARIANTS",
    "normalize_variant_priority",
    "normalize_web_download_variant",
    "web_download_variant_from_stem",
    "web_download_variant_label",
    "web_download_variant_suffix",
]

MissavVariant: TypeAlias = Literal[
    "original",
    "chinese_subtitle",
    "uncensored_leak",
]

WEB_DOWNLOAD_VARIANTS: tuple[MissavVariant, ...] = (
    "original",
    "chinese_subtitle",
    "uncensored_leak",
)
DEFAULT_WEB_DOWNLOAD_VARIANT: MissavVariant = "original"
DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY: tuple[MissavVariant, ...] = WEB_DOWNLOAD_VARIANTS

_VARIANT_LABELS = MappingProxyType(
    {
        "original": "原片",
        "chinese_subtitle": "中文字幕",
        "uncensored_leak": "无码影片",
    }
)
_VARIANT_SUFFIXES = MappingProxyType(
    {
        "original": "",
        "chinese_subtitle": "-chinese-subtitle",
        "uncensored_leak": "-uncensored-leak",
    }
)
_SAFE_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,16}$")
_INVALID_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')


def normalize_web_download_variant(value: object) -> MissavVariant:
    if not isinstance(value, str) or value not in WEB_DOWNLOAD_VARIANTS:
        raise ValueError("web download variant is invalid")
    return cast(MissavVariant, value)


def normalize_variant_priority(value: object) -> tuple[MissavVariant, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("web download variant priority is invalid")
    if len(value) != len(WEB_DOWNLOAD_VARIANTS):
        raise ValueError("web download variant priority is invalid")
    try:
        priority = tuple(normalize_web_download_variant(item) for item in value)
    except ValueError as exc:
        raise ValueError("web download variant priority is invalid") from exc
    if len(set(priority)) != len(priority) or frozenset(priority) != frozenset(
        WEB_DOWNLOAD_VARIANTS
    ):
        raise ValueError("web download variant priority is invalid")
    return priority


def web_download_variant_label(value: object) -> str:
    return _VARIANT_LABELS[normalize_web_download_variant(value)]


def web_download_variant_suffix(value: object) -> str:
    return _VARIANT_SUFFIXES[normalize_web_download_variant(value)]


def web_download_variant_from_stem(value: object) -> MissavVariant | None:
    if not isinstance(value, str):
        return None
    filename = unicodedata.normalize("NFKC", value)
    if (
        not filename
        or filename in {".", ".."}
        or len(filename.encode("utf-8")) > 255
        or filename != filename.strip()
        or any(character in _INVALID_FILENAME_CHARACTERS for character in filename)
        or any(
            unicodedata.category(character).startswith("C") for character in filename
        )
    ):
        return None
    extension = _SAFE_EXTENSION_RE.search(filename)
    stem = filename[: extension.start()] if extension is not None else filename
    for variant in WEB_DOWNLOAD_VARIANTS:
        marker = f"_{_VARIANT_LABELS[variant]}"
        if stem.endswith(marker) and len(stem) > len(marker):
            return variant
    return None
