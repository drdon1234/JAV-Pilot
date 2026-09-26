from __future__ import annotations

import ipaddress
import os
import socket


_PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal")
_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001::/32"),
)
_PROXY_ENV_KEYS = ("JAV_PILOT_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")


class PublicHostResolver:
    """Bounded, operation-scoped DNS policy for outbound HTTP targets."""

    def __init__(self, *, max_hosts: int = 32) -> None:
        self.max_hosts = max(1, min(int(max_hosts), 128))
        self._cache: dict[str, bool] = {}

    def is_public(self, hostname: str) -> bool:
        normalized = str(hostname or "").rstrip(".").lower()
        cached = self._cache.get(normalized)
        if cached is not None:
            return cached
        if not normalized or len(self._cache) >= self.max_hosts:
            return False

        result = bool(resolve_public_addresses(normalized, 443))
        self._cache[normalized] = result
        return result


def _hostname_is_public(hostname: str) -> bool:
    return bool(resolve_public_addresses(hostname, 443))


def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve a host once and return only a bounded, entirely public set.

    Callers must connect to one of the returned literals rather than resolve
    the hostname again; this makes the SSRF decision and transport atomic.
    """

    try:
        address = ipaddress.ip_address(hostname)
        return (address.compressed,) if address.is_global else ()
    except ValueError:
        pass
    if (
        hostname == "localhost"
        or hostname.endswith(_PRIVATE_HOST_SUFFIXES)
        or "." not in hostname
    ):
        return ()

    try:
        resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return ()
    if len(resolved) > 32:
        return ()

    addresses: set[str] = set()
    for item in resolved:
        address = str(item[4][0]).split("%", 1)[0]
        addresses.add(address)
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError:
        return ()
    allow_fake_ip = _fake_ip_dns_allowed()
    if not parsed_addresses or not all(
        address.is_global or (allow_fake_ip and _is_known_fake_ip(address))
        for address in parsed_addresses
    ):
        return ()
    return tuple(sorted(address.compressed for address in parsed_addresses))


def _fake_ip_dns_allowed() -> bool:
    if (os.environ.get("JAV_PILOT_ALLOW_FAKE_IP_DNS") or "").strip() == "1":
        return True
    return any((os.environ.get(key) or "").strip() for key in _PROXY_ENV_KEYS)


def _is_known_fake_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address.version == network.version and address in network for network in _FAKE_IP_NETWORKS)
