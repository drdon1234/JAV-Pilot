"""Subtitle source adapters and the configured source list."""

from __future__ import annotations

from collections.abc import Mapping

from ...config.settings import SUBTITLE_CAPABILITY, site_has_capability
from ..models import PROVIDER_IDS
from .base import ProviderError, RequestThrottle, SubtitleProvider, Throttle, no_throttle
from .subtitlecat import SubtitleCatProvider
from .xunlei import XunleiProvider

MIN_INTERVAL_SECONDS = {"xunlei": 1.0, "subtitlecat": 3.0}
_FACTORIES = {"xunlei": XunleiProvider, "subtitlecat": SubtitleCatProvider}

__all__ = [
    "MIN_INTERVAL_SECONDS",
    "ProviderError",
    "RequestThrottle",
    "SubtitleProvider",
    "build_providers",
]


def build_providers(
    sites: object,
    throttles: Mapping[str, Throttle] | None = None,
) -> list[SubtitleProvider]:
    """Enabled subtitle sources in catalog order (Xunlei first)."""

    if not isinstance(sites, list):
        return []
    configured: dict[str, str] = {}
    for site in sites:
        if not isinstance(site, dict) or not site.get("enabled"):
            continue
        site_id = site.get("id")
        if (
            site_id in _FACTORIES
            and site.get("parser_profile") == site_id
            and site_has_capability(site, SUBTITLE_CAPABILITY)
            and str(site.get("base_url") or "").strip()
        ):
            configured[str(site_id)] = str(site["base_url"]).strip()
    active_throttles = throttles or {}
    return [
        _FACTORIES[provider_id](
            configured[provider_id],
            throttle=active_throttles.get(provider_id, no_throttle),
        )
        for provider_id in PROVIDER_IDS
        if provider_id in configured
    ]
