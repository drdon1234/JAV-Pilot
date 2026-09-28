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
# IPv6 prefixes whose low 32 bits carry an IPv4 destination that a NAT64
# gateway or IPv4-compatible route may reach.  ``ipaddress`` reports these as
# global regardless of the embedded address.
_EMBEDDED_IPV4_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("::/96"),
)


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


def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve a host once and return only a bounded, entirely public set.

    Callers must connect to one of the returned literals rather than resolve
    the hostname again; this makes the SSRF decision and transport atomic.
    """

    try:
        address = ipaddress.ip_address(hostname)
        return (address.compressed,) if _is_global_address(address) else ()
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
    if not parsed_addresses or not all(
        address_is_public(address) for address in parsed_addresses
    ):
        return ()
    return tuple(sorted(address.compressed for address in parsed_addresses))


def address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an outbound target address counts as public.

    Addresses from a known fake-IP DNS range count as public when fake-IP DNS
    is allowed (explicitly, or implied by a configured proxy), matching the
    policy of every other outbound request.
    """
    return _is_global_address(address) or (
        _fake_ip_dns_allowed() and _is_known_fake_ip(address)
    )


def _is_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped
        if embedded is None and any(
            address in network for network in _EMBEDDED_IPV4_NETWORKS
        ):
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            return embedded.is_global
    return True


def _fake_ip_dns_allowed() -> bool:
    if (os.environ.get("JAV_PILOT_ALLOW_FAKE_IP_DNS") or "").strip() == "1":
        return True
    return any((os.environ.get(key) or "").strip() for key in _PROXY_ENV_KEYS)


def _is_known_fake_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address.version == network.version and address in network for network in _FAKE_IP_NETWORKS)
