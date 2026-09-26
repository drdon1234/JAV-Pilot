"""Request routing policy for pages opened on MissAV."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote, urlsplit

from ..net.network_guard import PublicHostResolver
from .site import (
    CLOUDFLARE_CHALLENGE_HOST,
    CLOUDFLARE_CHALLENGE_METHODS,
    CLOUDFLARE_CHALLENGE_PATH_PREFIXES,
    CLOUDFLARE_CHALLENGE_RESOURCE_TYPES,
    CLOUDFLARE_DYNAMIC_LABEL_RE,
    HEADER_NAME_RE,
    LOCAL_RESOURCE_SCHEMES,
    MEDIA_HOST,
    PLAYER_ASSET_HOST,
    PLAYER_ASSET_RESOURCE_TYPES,
    REPLAY_HEADER_NAMES,
    current_missav_site,
)
from .urls import validated_https_url

@dataclass(slots=True)
class BrowserMediaGate:
    block_surrit_requests: bool = True


def clean_replay_headers(headers: Mapping[object, object] | object) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    missav_site = current_missav_site()
    clean: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip().lower()
        if name not in REPLAY_HEADER_NAMES or not HEADER_NAME_RE.fullmatch(name):
            continue
        value = str(raw_value or "").strip()
        if (
            not value
            or len(value) > 8192
            or any(character in value for character in "\r\n\0")
        ):
            continue
        if name == "origin" and value.rstrip("/").lower() != missav_site.origin:
            continue
        if (
            name == "referer"
            and validated_https_url(
                value,
                allowed_hosts=frozenset({missav_site.host}),
            )
            is None
        ):
            continue
        clean[name] = value
    return clean


def _request_is_top_level_navigation(request: object, *, navigation: bool) -> bool:
    """Return whether a navigation request targets the browser's main frame.

    ``Request.is_navigation_request`` is also true for iframe navigations.  A
    Cloudflare challenge is rendered in a cross-origin iframe, so using that
    flag alone would either block the challenge or (if the host were broadly
    allowlisted) permit a top-level cross-origin redirect.  Missing frame
    metadata is treated as top-level to fail closed in mocks and unusual
    driver implementations.
    """

    if not navigation:
        return False
    try:
        frame = getattr(request, "frame", None)
        if frame is None:
            return True
        parent_frame = getattr(frame, "parent_frame", None)
        if callable(parent_frame):
            parent_frame = parent_frame()
        return parent_frame is None
    except Exception:  # noqa: BLE001 - routing callbacks must fail closed.
        return True


def is_cloudflare_challenge_host(hostname: object) -> bool:
    """Accept the fixed challenge host or one dynamic subdomain only."""

    host = str(hostname or "").rstrip(".").casefold()
    base = CLOUDFLARE_CHALLENGE_HOST.casefold()
    suffix = f".{base}"
    if host == base:
        return True
    if (
        not host.endswith(suffix)
        or host.count(".") != base.count(".") + 1
        or len(host) <= len(suffix)
    ):
        return False
    dynamic_label = host[: -len(suffix)]
    return (
        not dynamic_label.startswith("xn--")
        and CLOUDFLARE_DYNAMIC_LABEL_RE.fullmatch(dynamic_label) is not None
    )


def _is_cloudflare_challenge_namespace(hostname: object) -> bool:
    """Return whether *hostname* is the base challenge zone or its suffix."""

    host = str(hostname or "").rstrip(".").casefold()
    base = CLOUDFLARE_CHALLENGE_HOST.casefold()
    return host == base or host.endswith(f".{base}")


def _cloudflare_challenge_request_allowed(
    request: object,
    *,
    parsed_url: object,
    resource_type: str,
    navigation: bool,
) -> bool:
    """Validate one narrowly scoped Cloudflare challenge dependency.

    Only the exact Cloudflare challenge host and documented challenge paths
    are accepted.  Top-level navigation is never accepted; a ``document``
    request is permitted only when it is a child-frame navigation.  This keeps
    the MissAV origin as the sole page/navigation origin while still allowing
    Turnstile's script, telemetry and iframe to execute.
    """

    host = str(getattr(parsed_url, "hostname", "") or "").rstrip(".").lower()
    if not is_cloudflare_challenge_host(host):
        return False
    if resource_type not in CLOUDFLARE_CHALLENGE_RESOURCE_TYPES:
        return False
    method = str(getattr(request, "method", "") or "").strip().upper()
    if method not in CLOUDFLARE_CHALLENGE_METHODS:
        return False
    try:
        raw_path = str(getattr(parsed_url, "path", "") or "")
        if (
            not raw_path
            or any(character in raw_path for character in "\\\x00\r\n")
            # Challenge values are carried in the query; encoded path
            # separators/dot-segments are not needed and fail closed.
            or "%" in raw_path
        ):
            return False
        path = unquote(raw_path).casefold()
    except Exception:  # noqa: BLE001 - malformed URLs are rejected.
        return False
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    if host == CLOUDFLARE_CHALLENGE_HOST:
        allowed_prefixes = CLOUDFLARE_CHALLENGE_PATH_PREFIXES
    else:
        # Dynamic challenge subdomains are used for the challenge platform
        # transport only; Turnstile's public API remains on the base host.
        allowed_prefixes = ("/cdn-cgi/challenge-platform/",)
    if not any(path.startswith(prefix) for prefix in allowed_prefixes):
        return False
    if _request_is_top_level_navigation(request, navigation=navigation):
        return False
    return True


def deny_routed_web_socket(web_socket: object) -> None:
    """Keep WebSockets disabled for MissAV, including challenge traffic.

    The Cloudflare challenge paths used by MissAV are HTTP script/fetch/frame
    requests; they do not require a WebSocket.  Denying every socket avoids
    introducing an unbounded cross-origin tunnel while the HTTP allowlist
    above remains narrowly scoped.
    """

    close = getattr(web_socket, "close", None)
    if callable(close):
        close()


def route_request(
    route: object,
    *,
    resolver: PublicHostResolver,
    media_gate: BrowserMediaGate | None = None,
) -> None:
    missav_host = current_missav_site().host
    request = getattr(route, "request", None)
    request_url = str(getattr(request, "url", "") or "")
    is_navigation = getattr(request, "is_navigation_request", None)
    navigation = bool(callable(is_navigation) and is_navigation())
    resource_type = str(getattr(request, "resource_type", "") or "").lower()
    try:
        scheme = urlsplit(request_url).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme in LOCAL_RESOURCE_SCHEMES:
        if navigation:
            route.abort()
        else:
            route.continue_()
        return

    parsed = validated_https_url(
        request_url,
        allowed_hosts=frozenset(
            {missav_host, MEDIA_HOST, PLAYER_ASSET_HOST, CLOUDFLARE_CHALLENGE_HOST}
        ),
        allowed_host_suffixes=frozenset({CLOUDFLARE_CHALLENGE_HOST}),
    )
    if parsed is None or not resolver.is_public(parsed.hostname or ""):
        route.abort()
        return
    host = (parsed.hostname or "").rstrip(".").lower()
    if _is_cloudflare_challenge_namespace(host):
        if _cloudflare_challenge_request_allowed(
            request,
            parsed_url=parsed,
            resource_type=resource_type,
            navigation=navigation,
        ):
            route.continue_()
        else:
            route.abort()
        return
    if navigation and host != missav_host:
        route.abort()
        return
    if host == PLAYER_ASSET_HOST and resource_type not in PLAYER_ASSET_RESOURCE_TYPES:
        route.abort()
        return
    if (
        host == MEDIA_HOST
        and media_gate is not None
        and media_gate.block_surrit_requests
    ):
        route.abort()
        return
    if resource_type == "media" and host != MEDIA_HOST:
        route.abort()
        return
    route.continue_()


def route_metadata_request(route: object, *, resolver: PublicHostResolver) -> None:
    """Allow only the same-origin document traffic needed to read meta tags."""

    missav_host = current_missav_site().host
    request = getattr(route, "request", None)
    request_url = str(getattr(request, "url", "") or "")
    is_navigation = getattr(request, "is_navigation_request", None)
    navigation = bool(callable(is_navigation) and is_navigation())
    resource_type = str(getattr(request, "resource_type", "") or "").casefold()
    try:
        scheme = urlsplit(request_url).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme in LOCAL_RESOURCE_SCHEMES:
        if navigation or resource_type in {"font", "image", "media", "websocket"}:
            route.abort()
        else:
            route.continue_()
        return

    parsed = validated_https_url(
        request_url,
        allowed_hosts=frozenset({missav_host, PLAYER_ASSET_HOST, CLOUDFLARE_CHALLENGE_HOST}),
        allowed_host_suffixes=frozenset({CLOUDFLARE_CHALLENGE_HOST}),
    )
    if parsed is None or not resolver.is_public(parsed.hostname or ""):
        route.abort()
        return
    host = (parsed.hostname or "").rstrip(".").lower()
    if _is_cloudflare_challenge_namespace(host):
        if _cloudflare_challenge_request_allowed(
            request,
            parsed_url=parsed,
            resource_type=resource_type,
            navigation=navigation,
        ):
            route.continue_()
        else:
            route.abort()
        return
    if resource_type in {"font", "image", "media", "websocket"}:
        route.abort()
        return
    if host == PLAYER_ASSET_HOST:
        if navigation or resource_type not in PLAYER_ASSET_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
        return
    path = unquote(parsed.path).casefold()
    if any(marker in path for marker in ("/player/", "/player.", "/plyr")):
        route.abort()
        return
    if navigation and host != missav_host:
        route.abort()
        return
    route.continue_()
