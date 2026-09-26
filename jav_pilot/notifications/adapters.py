"""Delivery adapters for Webhook, Gotify, Telegram and NAS endpoints, plus the HTTP transport."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .errors import (
    NotificationConfigurationError,
    NotificationSecurityError,
    NotificationTransportError,
)
from .events import NotificationEvent

__all__ = [
    "GotifyAdapter",
    "GotifyConfig",
    "HttpRequest",
    "HttpResponse",
    "NasNotificationAdapter",
    "NasNotificationConfig",
    "NotificationAdapter",
    "NotificationConfig",
    "NotificationTransport",
    "StdlibNotificationTransport",
    "TelegramAdapter",
    "TelegramConfig",
    "WebhookAdapter",
    "WebhookConfig",
    "build_notification_adapters",
]


MAX_RESPONSE_BYTES = 64 * 1024


_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~:@+/-]{1,512}$")


AddressResolver = Callable[[str, int], Sequence[str]]


@dataclass(frozen=True)
class HttpRequest:
    method: str
    scheme: str
    hostname: str
    port: int
    timeout: float
    target: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    pinned_address: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.method != "POST" or self.scheme not in {"http", "https"}:
            raise NotificationConfigurationError("notification request is invalid")
        if not self.hostname or not 1 <= self.port <= 65535:
            raise NotificationConfigurationError("notification request is invalid")
        if not 1 <= self.timeout <= 30:
            raise NotificationConfigurationError("notification timeout is invalid")
        if not self.target.startswith("/") or len(self.body) > 64 * 1024:
            raise NotificationConfigurationError("notification request is invalid")
        try:
            ipaddress.ip_address(self.pinned_address)
        except ValueError as exc:
            raise NotificationConfigurationError(
                "notification pinned address is invalid"
            ) from exc


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise NotificationTransportError("notification response is invalid")


class NotificationTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class NotificationAdapter(Protocol):
    name: str

    def build_request(self, event: NotificationEvent) -> HttpRequest: ...


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool = False
    endpoint: str = field(default="", repr=False)
    signing_secret: str | None = field(default=None, repr=False)
    private_origin: str | None = field(default=None, repr=False)
    pinned_addresses: tuple[str, ...] = field(default=(), repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class GotifyConfig:
    enabled: bool = False
    origin: str = field(default="", repr=False)
    app_token: str = field(default="", repr=False)
    private_origin: str | None = field(default=None, repr=False)
    pinned_addresses: tuple[str, ...] = field(default=(), repr=False)
    priority: int = 5
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    api_origin: str = field(default="https://api.telegram.org", repr=False)
    private_origin: str | None = field(default=None, repr=False)
    pinned_addresses: tuple[str, ...] = field(default=(), repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class NasNotificationConfig:
    enabled: bool = False
    endpoint: str = field(default="", repr=False)
    signing_secret: str | None = field(default=None, repr=False)
    private_origin: str | None = field(default=None, repr=False)
    pinned_addresses: tuple[str, ...] = field(default=(), repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class NotificationConfig:
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    gotify: GotifyConfig = field(default_factory=GotifyConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    nas: NasNotificationConfig = field(default_factory=NasNotificationConfig)


@dataclass(frozen=True)
class _EndpointPolicy:
    scheme: str
    hostname: str
    port: int
    target: str = field(repr=False)
    pinned_addresses: tuple[str, ...] = field(repr=False)
    resolver: AddressResolver = field(repr=False, compare=False)
    # True when the pins came from operator configuration (always the case for
    # private targets); False when they were captured from the first public DNS
    # resolution and may be refreshed as the upstream rotates records.
    pins_configured: bool = field(default=True, repr=False)

    @classmethod
    def create(
        cls,
        url: object,
        *,
        resolver: AddressResolver,
        private_origin: object | None,
        pinned_addresses: Sequence[str],
    ) -> "_EndpointPolicy":
        scheme, hostname, port, target = _parse_endpoint(url)
        resolved = _resolve_addresses(resolver, hostname, port)
        configured_pins = _configured_addresses(pinned_addresses)
        public = all(address.is_global for address in resolved)
        if public:
            if scheme != "https":
                raise NotificationSecurityError(
                    "public notification targets require HTTPS"
                )
            if private_origin is not None:
                raise NotificationSecurityError(
                    "private notification origin does not match its target"
                )
            if configured_pins and configured_pins != resolved:
                raise NotificationSecurityError(
                    "notification address pin does not match DNS"
                )
        else:
            if any(not _allowed_private_address(address) for address in resolved):
                raise NotificationSecurityError(
                    "notification target address is prohibited"
                )
            expected_origin = _parse_origin(private_origin)
            if expected_origin != (scheme, hostname, port):
                raise NotificationSecurityError(
                    "private notification target requires an exact fixed origin"
                )
            if not configured_pins or configured_pins != resolved:
                raise NotificationSecurityError(
                    "private notification target requires exact address pins"
                )
        return cls(
            scheme=scheme,
            hostname=hostname,
            port=port,
            target=target,
            pinned_addresses=tuple(
                sorted(address.compressed for address in resolved)
            ),
            resolver=resolver,
            pins_configured=bool(configured_pins) or not public,
        )

    def request(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpRequest:
        current = _resolve_addresses(self.resolver, self.hostname, self.port)
        expected = frozenset(
            ipaddress.ip_address(address) for address in self.pinned_addresses
        )
        if current != expected:
            if self.pins_configured:
                raise NotificationSecurityError(
                    "notification target DNS changed after validation"
                )
            # Auto-pinned public targets (CDN, multi-A round robin) rotate
            # records routinely; re-validate the fresh resolution under the
            # same public-target rules and re-pin. Only a downgrade to a
            # non-public address (DNS rebinding) stays fatal — the transport
            # below still connects to the validated pinned address only.
            if not all(address.is_global for address in current):
                raise NotificationSecurityError(
                    "notification target DNS changed after validation"
                )
            object.__setattr__(
                self,
                "pinned_addresses",
                tuple(sorted(address.compressed for address in current)),
            )
        return HttpRequest(
            method="POST",
            scheme=self.scheme,
            hostname=self.hostname,
            port=self.port,
            target=self.target,
            headers=dict(headers),
            body=body,
            timeout=_timeout(timeout),
            pinned_address=self.pinned_addresses[0],
        )


class WebhookAdapter:
    name = "webhook"

    def __init__(
        self,
        config: WebhookConfig,
        *,
        resolver: AddressResolver = lambda host, port: _default_resolver(host, port),
    ) -> None:
        _require_enabled(config.enabled, "webhook")
        self._endpoint = _EndpointPolicy.create(
            config.endpoint,
            resolver=resolver,
            private_origin=config.private_origin,
            pinned_addresses=config.pinned_addresses,
        )
        self._secret = _optional_secret(config.signing_secret)
        self._timeout = _timeout(config.timeout_seconds)

    def build_request(self, event: NotificationEvent) -> HttpRequest:
        body = _json_bytes(event.payload())
        headers = _json_headers(event.event_id)
        if self._secret is not None:
            signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            headers["X-JAV-Pilot-Signature"] = f"sha256={signature}"
        return self._endpoint.request(
            headers=headers,
            body=body,
            timeout=self._timeout,
        )


class GotifyAdapter:
    name = "gotify"

    def __init__(
        self,
        config: GotifyConfig,
        *,
        resolver: AddressResolver = lambda host, port: _default_resolver(host, port),
    ) -> None:
        _require_enabled(config.enabled, "Gotify")
        token = _secret_token(config.app_token, "Gotify token")
        if isinstance(config.priority, bool) or not -10 <= config.priority <= 10:
            raise NotificationConfigurationError("Gotify priority is invalid")
        endpoint = f"{_origin_url(config.origin)}/message?{urlencode({'token': token})}"
        self._endpoint = _EndpointPolicy.create(
            endpoint,
            resolver=resolver,
            private_origin=config.private_origin,
            pinned_addresses=config.pinned_addresses,
        )
        self._priority = int(config.priority)
        self._timeout = _timeout(config.timeout_seconds)

    def build_request(self, event: NotificationEvent) -> HttpRequest:
        body = _json_bytes(
            {
                "title": _event_title(event),
                "message": _event_message(event),
                "priority": self._priority,
                "extras": {"jav_pilot": event.payload()},
            }
        )
        return self._endpoint.request(
            headers=_json_headers(event.event_id),
            body=body,
            timeout=self._timeout,
        )


class TelegramAdapter:
    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        *,
        resolver: AddressResolver = lambda host, port: _default_resolver(host, port),
    ) -> None:
        _require_enabled(config.enabled, "Telegram")
        token = _secret_token(config.bot_token, "Telegram token")
        chat_id = _secret_token(config.chat_id, "Telegram chat identity")
        endpoint = (
            f"{_origin_url(config.api_origin)}/bot{quote(token, safe=':')}/sendMessage"
        )
        self._endpoint = _EndpointPolicy.create(
            endpoint,
            resolver=resolver,
            private_origin=config.private_origin,
            pinned_addresses=config.pinned_addresses,
        )
        self._chat_id = chat_id
        self._timeout = _timeout(config.timeout_seconds)

    def build_request(self, event: NotificationEvent) -> HttpRequest:
        body = urlencode(
            {
                "chat_id": self._chat_id,
                "text": _event_message(event),
                "disable_web_page_preview": "true",
            }
        ).encode("ascii")
        return self._endpoint.request(
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Idempotency-Key": event.event_id,
            },
            body=body,
            timeout=self._timeout,
        )


class NasNotificationAdapter:
    name = "nas"

    def __init__(
        self,
        config: NasNotificationConfig,
        *,
        resolver: AddressResolver = lambda host, port: _default_resolver(host, port),
    ) -> None:
        _require_enabled(config.enabled, "NAS")
        self._endpoint = _EndpointPolicy.create(
            config.endpoint,
            resolver=resolver,
            private_origin=config.private_origin,
            pinned_addresses=config.pinned_addresses,
        )
        self._secret = _optional_secret(config.signing_secret)
        self._timeout = _timeout(config.timeout_seconds)

    def build_request(self, event: NotificationEvent) -> HttpRequest:
        body = _json_bytes(
            {
                "title": _event_title(event),
                "message": _event_message(event),
                "event": event.payload(),
            }
        )
        headers = _json_headers(event.event_id)
        if self._secret is not None:
            signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            headers["X-JAV-Pilot-Signature"] = f"sha256={signature}"
        return self._endpoint.request(
            headers=headers,
            body=body,
            timeout=self._timeout,
        )


def build_notification_adapters(
    config: NotificationConfig | None = None,
    *,
    resolver: AddressResolver = lambda host, port: _default_resolver(host, port),
) -> tuple[NotificationAdapter, ...]:
    settings = config or NotificationConfig()
    if not isinstance(settings, NotificationConfig):
        raise NotificationConfigurationError("notification config is invalid")
    for name, enabled in (
        ("webhook", settings.webhook.enabled),
        ("Gotify", settings.gotify.enabled),
        ("Telegram", settings.telegram.enabled),
        ("NAS", settings.nas.enabled),
    ):
        if not isinstance(enabled, bool):
            raise NotificationConfigurationError(
                f"{name} notification enabled state is invalid"
            )
    adapters: list[NotificationAdapter] = []
    if settings.webhook.enabled:
        adapters.append(WebhookAdapter(settings.webhook, resolver=resolver))
    if settings.gotify.enabled:
        adapters.append(GotifyAdapter(settings.gotify, resolver=resolver))
    if settings.telegram.enabled:
        adapters.append(TelegramAdapter(settings.telegram, resolver=resolver))
    if settings.nas.enabled:
        adapters.append(NasNotificationAdapter(settings.nas, resolver=resolver))
    return tuple(adapters)


class StdlibNotificationTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        connection: http.client.HTTPConnection
        try:
            if request.scheme == "https":
                connection = _PinnedHTTPSConnection(
                    request.hostname,
                    request.pinned_address,
                    request.port,
                    timeout=request.timeout,
                )
            else:
                connection = http.client.HTTPConnection(
                    request.pinned_address,
                    request.port,
                    timeout=request.timeout,
                )
            headers = dict(request.headers)
            headers["Host"] = _host_header(
                request.hostname,
                request.port,
                request.scheme,
            )
            connection.request(
                request.method,
                request.target,
                body=request.body,
                headers=headers,
            )
            response = connection.getresponse()
            response.read(MAX_RESPONSE_BYTES + 1)
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            return HttpResponse(status=int(response.status), headers=response_headers)
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError) as exc:
            raise NotificationTransportError(
                "notification transport failed"
            ) from exc
        finally:
            if "connection" in locals():
                connection.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        pinned_address: str,
        port: int,
        *,
        timeout: float,
    ) -> None:
        super().__init__(
            hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = pinned_address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _parse_endpoint(value: object) -> tuple[str, str, int, str]:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise NotificationConfigurationError("notification endpoint is invalid")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise NotificationConfigurationError(
            "notification endpoint is invalid"
        ) from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise NotificationConfigurationError("notification endpoint is invalid")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise NotificationConfigurationError(
            "notification endpoint is invalid"
        ) from exc
    clean_port = port or (443 if scheme == "https" else 80)
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return scheme, hostname, clean_port, target


def _parse_origin(value: object | None) -> tuple[str, str, int] | None:
    if value is None:
        return None
    scheme, hostname, port, target = _parse_endpoint(value)
    if target != "/":
        raise NotificationConfigurationError(
            "private notification origin must not include a path or query"
        )
    return scheme, hostname, port


def _origin_url(value: object) -> str:
    scheme, hostname, port = _parse_origin(value) or ("", "", 0)
    default_port = 443 if scheme == "https" else 80
    host = f"[{hostname}]" if ":" in hostname else hostname
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def _resolve_addresses(
    resolver: AddressResolver,
    hostname: str,
    port: int,
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        values = tuple(resolver(hostname, port))
    except Exception as exc:
        raise NotificationSecurityError(
            "notification target DNS resolution failed"
        ) from exc
    if not 1 <= len(values) <= 16:
        raise NotificationSecurityError("notification target DNS result is invalid")
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        for value in values:
            text = str(value).split("%", 1)[0]
            addresses.add(ipaddress.ip_address(text))
    except ValueError as exc:
        raise NotificationSecurityError(
            "notification target DNS result is invalid"
        ) from exc
    if not addresses:
        raise NotificationSecurityError("notification target DNS result is empty")
    return frozenset(addresses)


def _configured_addresses(
    values: Sequence[str],
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if isinstance(values, (str, bytes)) or len(values) > 16:
        raise NotificationConfigurationError("notification address pins are invalid")
    try:
        return frozenset(ipaddress.ip_address(str(value)) for value in values)
    except ValueError as exc:
        raise NotificationConfigurationError(
            "notification address pins are invalid"
        ) from exc


def _allowed_private_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NotificationSecurityError(
            "notification target DNS resolution failed"
        ) from exc
    return tuple(sorted({str(row[4][0]).split("%", 1)[0] for row in rows}))


def _timeout(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError) as exc:
        raise NotificationConfigurationError(
            "notification timeout is invalid"
        ) from exc
    if not math.isfinite(clean) or not 1 <= clean <= 30:
        raise NotificationConfigurationError("notification timeout is invalid")
    return clean


def _require_enabled(value: object, label: str) -> None:
    if not isinstance(value, bool):
        raise NotificationConfigurationError(
            f"{label} notification enabled state is invalid"
        )
    if not value:
        raise NotificationConfigurationError(f"{label} adapter is disabled")


def _secret_token(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if not _SAFE_TOKEN_RE.fullmatch(clean):
        raise NotificationConfigurationError(f"{label} is invalid")
    return clean


def _optional_secret(value: object | None) -> bytes | None:
    if value is None:
        return None
    clean = str(value).encode("utf-8")
    if not 16 <= len(clean) <= 1024 or any(byte < 32 or byte == 127 for byte in clean):
        raise NotificationConfigurationError("notification signing secret is invalid")
    return clean


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > 64 * 1024:
        raise NotificationConfigurationError("notification payload is too large")
    return raw


def _json_headers(event_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Idempotency-Key": event_id,
    }


def _event_title(event: NotificationEvent) -> str:
    labels = {
        "completed": "JAV Pilot completed",
        "failed": "JAV Pilot failed",
        "disk_low": "JAV Pilot disk space warning",
        "site_failure": "JAV Pilot site warning",
        "test": "JAV Pilot test notification",
    }
    return labels[event.event_type]


def _event_message(event: NotificationEvent) -> str:
    subject = event.subject_id or event.code or event.source
    if event.stage is not None:
        subject = f"{subject}/{event.stage}"
    detail = event.error_code or event.status
    return (
        f"{subject}: {detail} "
        f"(event {event.event_id}, occurrences {event.occurrence_count})"
    )


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default = 443 if scheme == "https" else 80
    return host if port == default else f"{host}:{port}"
