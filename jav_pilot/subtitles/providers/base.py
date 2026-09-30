from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

from ...net.http_client import FetchError
from ..models import SubtitleCandidate

PROVIDER_ERROR_KINDS = frozenset({"network", "blocked", "parse"})
_BLOCKED_MARKERS = ("http 403", "http 429", "challenge", "captcha", "cloudflare")

Throttle = Callable[[], None]


class ProviderError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        if kind not in PROVIDER_ERROR_KINDS:
            raise ValueError("subtitle provider error kind is invalid")
        super().__init__(message)
        self.kind = kind


class SubtitleProvider(Protocol):
    provider_id: str

    def search(self, code: str, *, timeout: float) -> list[SubtitleCandidate]: ...

    def fetch(self, candidate: SubtitleCandidate, *, timeout: float) -> bytes: ...


def no_throttle() -> None:
    return None


class RequestThrottle:
    """Keep at least ``interval_seconds`` between requests to one source."""

    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = max(0.0, float(interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def __call__(self) -> None:
        with self._lock:
            wait = self._next_at - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._next_at = self._clock() + self._interval


def provider_error(exc: FetchError) -> ProviderError:
    message = str(exc).casefold()
    if any(marker in message for marker in _BLOCKED_MARKERS):
        return ProviderError("blocked", "subtitle source rejected the request")
    return ProviderError("network", "subtitle source is unavailable")
