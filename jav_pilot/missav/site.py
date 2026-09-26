"""MissAV site constants, the configured site and per-operation scope."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Callable, Iterator, Literal, ParamSpec, TypeAlias, TypeVar
from urllib.parse import urlsplit

from ..config.settings import (
    DESCRIPTION_CAPABILITY,
    RESOURCE_SEARCH_CAPABILITY,
    WEB_DOWNLOAD_CAPABILITY,
    load_settings,
    site_has_capability,
)
from .errors import MissavError, MissavTransientError

__all__ = [
    "QualityStrategy",
    "validate_quality_strategy",
]


MEDIA_ORIGIN = "https://surrit.com"
MEDIA_HOST = "surrit.com"
PLAYER_ASSET_HOST = "cdnjs.cloudflare.com"
# Cloudflare's managed challenge/Turnstile page intentionally loads a small
# set of cross-origin resources from this exact host.  Keep this separate from
# the player CDN allowlist: challenge traffic must never become a general
# third-party dependency or a navigation target.
CLOUDFLARE_CHALLENGE_HOST = "challenges.cloudflare.com"

CODE_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")
DETAIL_ROUTE_PREFIX_RE = re.compile(r"^dm(?:[1-9]|[1-9][0-9]{1,2})$")
DETAIL_LOCALE_SEGMENTS = frozenset({"cn", "zh", "en", "ja", "ko", "tw"})
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
DESCRIPTION_SENSITIVE_RE = re.compile(
    r"(?:https?://|(?:authorization|cookie|manifest(?:_url)?|referer|token)\s*[:=])",
    re.IGNORECASE,
)
LOCAL_RESOURCE_SCHEMES = frozenset({"about", "blob", "data"})
PLAYER_ASSET_RESOURCE_TYPES = frozenset({"script", "stylesheet"})
# The CSP emitted by Cloudflare challenges uses script/image/connect/frame
# resources.  Playwright reports the connect directives as xhr/fetch (and
# occasionally eventsource) and frame navigations as document requests.
CLOUDFLARE_CHALLENGE_RESOURCE_TYPES = frozenset(
    {"document", "script", "stylesheet", "image", "xhr", "fetch", "eventsource"}
)
CLOUDFLARE_CHALLENGE_METHODS = frozenset({"GET", "POST", "OPTIONS"})
CLOUDFLARE_DYNAMIC_LABEL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
CLOUDFLARE_CHALLENGE_PATH_PREFIXES = (
    "/turnstile/",
    "/cdn-cgi/challenge-platform/",
)
REPLAY_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-language",
        "cookie",
        "origin",
        "referer",
        "user-agent",
    }
)
_MANIFEST_CONTENT_TYPES = (
    "application/mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)
PLAYER_SOURCE_NAMES = ("source1280", "source842")
CHALLENGE_TECHNICAL_MARKERS = (
    "cf-chl",
    "challenge-platform",
    "_cf_chl_opt",
    "challenge-error-text",
)
CHALLENGE_TEXT_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
    "\u8bf7\u7a0d\u5019",
    "\u8acb\u7a0d\u5019",
)
SERIES_PREFIX_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")
SERIES_PAGE_QUERY_RE = re.compile(r"^page=([1-9][0-9]{0,2})$")
_QUALITY_STRATEGIES = frozenset({"legacy", "selected", "highest"})
QUALITY_HEIGHT_LADDER = (144, 240, 360, 480, 540, 576, 720, 1080, 1440, 2160, 2880, 4320)
MAX_SERIES_PAGES = 100
MAX_SERIES_CANDIDATES = 1000
MAX_RESOURCE_RESULTS = 999
MAX_RESOURCE_PAGES = 999
MAX_RESOURCE_DELTA_ITEMS = 64
MISSAV_RESOURCE_CHALLENGE_WAIT_SECONDS = 15.0
MAX_RESOURCE_TITLE_LENGTH = 512
RESOURCE_RESULT_GRID_CLASSES = frozenset(
    {"grid", "grid-cols-2", "md:grid-cols-3", "xl:grid-cols-4", "gap-5"}
)
RESOURCE_CARD_CLASSES = frozenset({"thumbnail", "group"})
RESOURCE_TITLE_LINK_CLASSES = frozenset({"text-secondary", "group-hover:text-primary"})
HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

QualityStrategy: TypeAlias = Literal["legacy", "selected", "highest"]


@dataclass(frozen=True, slots=True)
class _MissavSiteConfig:
    origin: str
    host: str


_ACTIVE_MISSAV_SITE: ContextVar[_MissavSiteConfig | None] = ContextVar(
    "active_missav_site",
    default=None,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _load_missav_site_config(
    required_capability: str = WEB_DOWNLOAD_CAPABILITY,
) -> _MissavSiteConfig:
    settings = load_settings()
    if settings.get("_config_error"):
        raise MissavError("MissAV site configuration is invalid")
    site = next(
        (
            item
            for item in settings.get("sites", [])
            if isinstance(item, dict) and item.get("id") == "missav"
        ),
        None,
    )
    if (
        site is None
        or not site_has_capability(site, required_capability)
        or site.get("parser_profile") != "missav"
    ):
        raise MissavError("MissAV site configuration is invalid")
    if not site.get("enabled"):
        raise MissavError("MissAV site is disabled")
    origin = str(site.get("base_url") or "").strip()
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise MissavError("MissAV site configuration is invalid") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    canonical_host = f"[{host}]" if ":" in host else host
    if (
        parsed.scheme != "https"
        or not host
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != f"https://{canonical_host}"
    ):
        raise MissavError("MissAV site configuration is invalid")
    return _MissavSiteConfig(origin=origin, host=host)


def current_missav_site() -> _MissavSiteConfig:
    return _ACTIVE_MISSAV_SITE.get() or _load_missav_site_config()


@contextmanager
def missav_operation(
    required_capability: str = WEB_DOWNLOAD_CAPABILITY,
) -> Iterator[_MissavSiteConfig]:
    current = _ACTIVE_MISSAV_SITE.get()
    if current is not None:
        yield current
        return
    config = _load_missav_site_config(required_capability)
    token = _ACTIVE_MISSAV_SITE.set(config)
    try:
        yield config
    finally:
        _ACTIVE_MISSAV_SITE.reset(token)


def uses_configured_missav_site(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(operation)
    def configured(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with missav_operation():
            return operation(*args, **kwargs)

    return configured


def uses_configured_missav_resource_site(
    operation: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(operation)
    def configured(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with missav_operation(RESOURCE_SEARCH_CAPABILITY):
            return operation(*args, **kwargs)

    return configured


def uses_configured_missav_description_site(
    operation: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(operation)
    def configured(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with missav_operation(DESCRIPTION_CAPABILITY):
            return operation(*args, **kwargs)

    return configured


def validate_quality_strategy(
    value: object | None,
    *,
    requested_height: int | None,
) -> QualityStrategy:
    if value is None:
        return "selected" if requested_height is not None else "legacy"
    if not isinstance(value, str) or value not in _QUALITY_STRATEGIES:
        raise MissavError("MissAV quality strategy is invalid")
    strategy: QualityStrategy = value  # type: ignore[assignment]
    if strategy == "selected" and requested_height is None:
        raise MissavError("MissAV selected quality requires a height")
    if strategy == "legacy" and requested_height is not None:
        raise MissavError("MissAV requested quality conflicts with its strategy")
    return strategy


def remaining_milliseconds(deadline: float) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise MissavTransientError(
            "MissAV capture timed out",
            code="navigation_timeout",
        )
    return max(1, remaining)
