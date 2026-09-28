from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPSHandler

from .network_guard import resolve_public_addresses


class PinnedHTTPConnection(http.client.HTTPConnection):
    _proxy_mode = False

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):  # type: ignore[no-untyped-def]
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            addresses = resolve_public_addresses(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            if not addresses:
                raise OSError("request host must resolve to a public address")
            pinned = f"[{addresses[0]}]" if ":" in addresses[0] else addresses[0]
            port = parsed.port
            netloc = pinned if port is None else f"{pinned}:{port}"
            url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
            self._proxy_mode = True
        return super().request(
            method, url, body, headers or {}, encode_chunked=encode_chunked
        )

    def connect(self) -> None:
        if self._proxy_mode:
            self.sock = socket.create_connection(
                (self.host, self.port), self.timeout, self.source_address
            )
            return
        addresses = resolve_public_addresses(self.host, self.port)
        if not addresses:
            raise OSError("request host must resolve to a public address")
        last_error: OSError | None = None
        for address in addresses:
            try:
                self.sock = socket.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                if self._tunnel_host:
                    self._tunnel()
                return
            except OSError as exc:
                last_error = exc
        raise last_error or OSError("request connection failed")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        if self._tunnel_host:
            # A CONNECT proxy must receive the origin hostname as its
            # authority.  Replacing it with a pinned IP makes some proxies
            # close the tunnel before TLS starts.  Keep the public-address
            # check, then let the stdlib preserve the hostname for CONNECT
            # and SNI while it establishes the proxy tunnel.
            if not resolve_public_addresses(
                self._tunnel_host, self._tunnel_port or 443
            ):
                raise OSError("request host must resolve to a public address")
            return super().connect()
        addresses = resolve_public_addresses(self.host, self.port)
        if not addresses:
            raise OSError("request host must resolve to a public address")
        last_error: OSError | None = None
        for address in addresses:
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        raise last_error or OSError("request connection failed")


class PinnedHTTPHandler(HTTPHandler):
    def http_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(PinnedHTTPConnection, req)


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self) -> None:
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):  # type: ignore[no-untyped-def]
        # Python 3.12 removed HTTPSHandler._check_hostname.  The configured
        # SSL context already carries the hostname-checking policy, and
        # passing a separate flag would be forwarded to HTTPSConnection,
        # which does not accept it.
        return self.do_open(PinnedHTTPSConnection, req, context=self._context)
