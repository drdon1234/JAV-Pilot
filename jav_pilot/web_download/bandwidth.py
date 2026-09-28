from __future__ import annotations

import hmac
import json
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .control import normalize_bandwidth_limit


LOOPBACK_HOST = "127.0.0.1"
MAX_ALLOWANCE_BYTES = 4 * 1024 * 1024
ALLOWANCE_QUANTUM_BYTES = 64 * 1024
MAX_PROTOCOL_LINE_BYTES = 512
_TOKEN_HEX_LENGTH = 64


class BandwidthBrokerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BandwidthClientConfig:
    host: str
    port: int
    token: str

    def payload(self) -> dict[str, object]:
        return {
            "bandwidth_host": self.host,
            "bandwidth_port": self.port,
            "bandwidth_token": self.token,
        }


class _BrokerServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, broker: AggregateBandwidthBroker) -> None:
        self.broker = broker
        super().__init__((LOOPBACK_HOST, 0), _BrokerHandler)


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        broker = self.server.broker  # type: ignore[attr-defined]
        while True:
            try:
                raw = self.rfile.readline(MAX_PROTOCOL_LINE_BYTES + 1)
            except OSError:
                return
            if not raw:
                return
            if len(raw) > MAX_PROTOCOL_LINE_BYTES or not raw.endswith(b"\n"):
                return
            try:
                request = json.loads(raw.decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(request, dict) or set(request) != {"token", "bytes"}:
                return
            token = request.get("token")
            requested = request.get("bytes")
            if (
                not isinstance(token, str)
                or not hmac.compare_digest(token, broker.token)
                or isinstance(requested, bool)
                or not isinstance(requested, int)
                or not 1 <= requested <= MAX_ALLOWANCE_BYTES
            ):
                return
            try:
                allowance = broker.acquire(requested)
            except BandwidthBrokerError:
                return
            response = json.dumps(
                {"bytes": allowance}, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            try:
                self.wfile.write(response + b"\n")
                self.wfile.flush()
            except OSError:
                return


class AggregateBandwidthBroker:
    def __init__(
        self,
        limit: int = 0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = normalize_bandwidth_limit(limit)
        self._clock = clock
        self._condition = threading.Condition(threading.Lock())
        self._tokens = 0.0
        self._last_refill = float(clock())
        self._closed = False
        self._next_ticket = 0
        self._serving_ticket = 0
        self.token = secrets.token_hex(_TOKEN_HEX_LENGTH // 2)
        self._server = _BrokerServer(self)
        host, port = self._server.server_address
        self.config = BandwidthClientConfig(str(host), int(port), self.token)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="jav-web-bandwidth-broker",
            daemon=True,
        )
        self._thread.start()

    @property
    def limit(self) -> int:
        with self._condition:
            return self._limit

    def update_limit(self, limit: int) -> None:
        clean_limit = normalize_bandwidth_limit(limit)
        with self._condition:
            if self._closed:
                raise BandwidthBrokerError("bandwidth broker is closed")
            previous = self._limit
            self._refill_locked()
            self._limit = clean_limit
            if previous == 0 and clean_limit > 0:
                self._tokens = 0.0
            elif clean_limit > 0:
                self._tokens = min(self._tokens, self._capacity(clean_limit))
            self._last_refill = float(self._clock())
            self._condition.notify_all()

    def acquire(self, requested: int) -> int:
        if (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or not 1 <= requested <= MAX_ALLOWANCE_BYTES
        ):
            raise BandwidthBrokerError("bandwidth allowance request is invalid")
        target = min(requested, ALLOWANCE_QUANTUM_BYTES)
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while True:
                if self._closed:
                    raise BandwidthBrokerError("bandwidth broker is closed")
                if ticket != self._serving_ticket:
                    self._condition.wait(timeout=1.0)
                    continue
                if self._limit == 0:
                    self._serving_ticket += 1
                    self._condition.notify_all()
                    return requested
                self._refill_locked()
                if self._tokens >= target:
                    allowance = min(target, int(self._tokens))
                    self._tokens -= allowance
                    self._serving_ticket += 1
                    self._condition.notify_all()
                    return allowance
                missing = target - self._tokens
                wait_for = max(0.001, min(missing / self._limit, 1.0))
                self._condition.wait(timeout=wait_for)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def _refill_locked(self) -> None:
        now = float(self._clock())
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if self._limit > 0 and elapsed > 0:
            self._tokens = min(
                self._capacity(self._limit),
                self._tokens + elapsed * self._limit,
            )

    @staticmethod
    def _capacity(limit: int) -> int:
        return max(ALLOWANCE_QUANTUM_BYTES, limit // 4)


class BandwidthClient:
    def __init__(
        self,
        config: BandwidthClientConfig,
        *,
        connect_timeout: float = 5.0,
        response_timeout: float = 30.0,
    ) -> None:
        self.config = validate_bandwidth_client_config(config)
        self._connect_timeout = max(0.1, min(float(connect_timeout), 30.0))
        self._response_timeout = max(1.0, min(float(response_timeout), 120.0))
        self._socket: socket.socket | None = None
        self._reader = None

    def allowance(self, requested: int) -> int:
        if (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or not 1 <= requested <= MAX_ALLOWANCE_BYTES
        ):
            raise BandwidthBrokerError("bandwidth allowance request is invalid")
        try:
            self._connect()
            if self._socket is None or self._reader is None:
                raise OSError("bandwidth broker connection is unavailable")
            request = json.dumps(
                {"token": self.config.token, "bytes": requested},
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            self._socket.sendall(request + b"\n")
            raw = self._reader.readline(MAX_PROTOCOL_LINE_BYTES + 1)
            if (
                not raw
                or len(raw) > MAX_PROTOCOL_LINE_BYTES
                or not raw.endswith(b"\n")
            ):
                raise OSError("bandwidth broker response is unavailable")
            response = json.loads(raw.decode("ascii"))
            allowance = response.get("bytes") if isinstance(response, dict) else None
            if (
                not isinstance(response, dict)
                or set(response) != {"bytes"}
                or (
                    isinstance(allowance, bool)
                    or not isinstance(allowance, int)
                    or not 1 <= allowance <= requested
                )
            ):
                raise OSError("bandwidth broker response is invalid")
            return allowance
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.close()
            raise BandwidthBrokerError("shared bandwidth broker is unavailable") from exc

    def close(self) -> None:
        reader = self._reader
        connection = self._socket
        self._reader = None
        self._socket = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _connect(self) -> None:
        if self._socket is not None:
            return
        connection = socket.create_connection(
            (self.config.host, self.config.port), timeout=self._connect_timeout
        )
        connection.settimeout(self._response_timeout)
        self._socket = connection
        self._reader = connection.makefile("rb")

    def __enter__(self) -> BandwidthClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def bandwidth_config_from_payload(
    payload: dict[str, object],
) -> BandwidthClientConfig | None:
    values = (
        payload.get("bandwidth_host"),
        payload.get("bandwidth_port"),
        payload.get("bandwidth_token"),
    )
    if values == (None, None, None):
        return None
    return validate_bandwidth_client_config(
        BandwidthClientConfig(
            host=str(values[0] or ""),
            port=values[1] if isinstance(values[1], int) else 0,
            token=str(values[2] or ""),
        )
    )


def validate_bandwidth_client_config(
    config: BandwidthClientConfig,
) -> BandwidthClientConfig:
    if (
        config.host != LOOPBACK_HOST
        or isinstance(config.port, bool)
        or not isinstance(config.port, int)
        or not 1 <= config.port <= 65535
        or len(config.token) != _TOKEN_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in config.token)
    ):
        raise BandwidthBrokerError("shared bandwidth broker configuration is invalid")
    return config
