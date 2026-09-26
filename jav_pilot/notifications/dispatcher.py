"""Delivers pending outbox entries through the configured adapters."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Literal

from .adapters import (
    HttpResponse,
    NotificationAdapter,
    NotificationTransport,
    StdlibNotificationTransport,
)
from .errors import (
    NotificationConfigurationError,
    NotificationSecurityError,
    NotificationTransportError,
)
from .events import bounded_limit, event_timestamp, safe_name
from .outbox import DeliveryAttemptResult, SQLiteNotificationOutbox

__all__ = [
    "DispatchResult",
    "NotificationDispatcher",
]


@dataclass(frozen=True)
class DispatchResult:
    event_id: str
    adapter: str
    outcome: Literal["delivered", "retry", "dead"]
    attempt: int
    error_code: str | None
    http_status: int | None
    next_attempt_at: float | None


class NotificationDispatcher:
    def __init__(
        self,
        store: SQLiteNotificationOutbox,
        adapters: Sequence[NotificationAdapter],
        *,
        transport: NotificationTransport | None = None,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 60.0,
        max_attempts: int = 6,
        base_retry_seconds: float = 10.0,
        max_retry_seconds: float = 3600.0,
    ) -> None:
        adapter_map: dict[str, NotificationAdapter] = {}
        for adapter in adapters:
            name = safe_name(getattr(adapter, "name", None), "notification adapter")
            if name in adapter_map:
                raise NotificationConfigurationError(
                    "notification adapter names must be unique"
                )
            adapter_map[name] = adapter
        if not 5 <= lease_seconds <= 3600:
            raise NotificationConfigurationError(
                "notification lease duration is invalid"
            )
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise NotificationConfigurationError(
                "notification attempt bound is invalid"
            )
        if not 1 <= base_retry_seconds <= max_retry_seconds <= 86_400:
            raise NotificationConfigurationError("notification retry bounds are invalid")
        self.store = store
        self.adapters = adapter_map
        self.transport = transport or StdlibNotificationTransport()
        self.clock = clock
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = int(max_attempts)
        self.base_retry_seconds = float(base_retry_seconds)
        self.max_retry_seconds = float(max_retry_seconds)
        self._adapter_names = tuple(adapter_map)
        self._registry_lock = threading.Lock()
        self._registry_reconciled = False

    def dispatch_once(self) -> DispatchResult | None:
        self._ensure_registry_reconciled()
        if not self.adapters:
            return None
        claimed_at = event_timestamp(self.clock())
        delivery = self.store.claim(
            self._adapter_names,
            now=claimed_at,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if delivery is None:
            return None
        adapter = self.adapters[delivery.adapter]
        try:
            request = adapter.build_request(delivery.event)
            response = self.transport.send(request)
        except (NotificationConfigurationError, NotificationSecurityError):
            finished_at = event_timestamp(self.clock())
            result = DeliveryAttemptResult(
                "dead",
                error_code="target_rejected",
            )
        except (NotificationTransportError, OSError, TimeoutError):
            finished_at = event_timestamp(self.clock())
            result = self._retry_result(
                attempt=delivery.attempt,
                now=finished_at,
                error_code="transport_error",
            )
        except Exception:
            finished_at = event_timestamp(self.clock())
            result = self._retry_result(
                attempt=delivery.attempt,
                now=finished_at,
                error_code="transport_error",
            )
        else:
            finished_at = event_timestamp(self.clock())
            result = self._http_result(response, delivery.attempt, finished_at)
        self.store.finish(delivery, result, now=finished_at)
        return DispatchResult(
            event_id=delivery.event.event_id,
            adapter=delivery.adapter,
            outcome=result.outcome,
            attempt=delivery.attempt,
            error_code=result.error_code,
            http_status=result.http_status,
            next_attempt_at=result.next_attempt_at,
        )

    def dispatch_available(
        self,
        *,
        limit: int = 16,
        stop_requested: Callable[[], bool] | None = None,
    ) -> list[DispatchResult]:
        clean_limit = bounded_limit(limit, maximum=100)
        results: list[DispatchResult] = []
        for _index in range(clean_limit):
            if stop_requested is not None and stop_requested():
                break
            result = self.dispatch_once()
            if result is None:
                break
            results.append(result)
        return results

    def _ensure_registry_reconciled(self) -> None:
        if self._registry_reconciled:
            return
        with self._registry_lock:
            if self._registry_reconciled:
                return
            self.store.configure_adapters(
                self._adapter_names,
                now=event_timestamp(self.clock()),
            )
            self._registry_reconciled = True

    def _http_result(
        self,
        response: HttpResponse,
        attempt: int,
        now: float,
    ) -> DeliveryAttemptResult:
        status = response.status
        if 200 <= status <= 299:
            return DeliveryAttemptResult("delivered", http_status=status)
        if 300 <= status <= 399:
            return DeliveryAttemptResult(
                "dead",
                error_code="redirect_rejected",
                http_status=status,
            )
        if status == 429:
            retry_after = _retry_after_seconds(response.headers, now)
            return self._retry_result(
                attempt=attempt,
                now=now,
                error_code="rate_limited",
                http_status=status,
                requested_delay=retry_after,
            )
        if status in {408, 425} or 500 <= status <= 599:
            return self._retry_result(
                attempt=attempt,
                now=now,
                error_code="remote_unavailable",
                http_status=status,
            )
        return DeliveryAttemptResult(
            "dead",
            error_code="remote_rejected",
            http_status=status,
        )

    def _retry_result(
        self,
        *,
        attempt: int,
        now: float,
        error_code: str,
        http_status: int | None = None,
        requested_delay: float | None = None,
    ) -> DeliveryAttemptResult:
        if attempt >= self.max_attempts:
            return DeliveryAttemptResult(
                "dead",
                error_code=error_code,
                http_status=http_status,
            )
        exponent = min(attempt - 1, 20)
        delay = min(
            self.max_retry_seconds,
            self.base_retry_seconds * (2**exponent),
        )
        if requested_delay is not None:
            delay = min(
                self.max_retry_seconds,
                max(delay, requested_delay),
            )
        return DeliveryAttemptResult(
            "retry",
            error_code=error_code,
            http_status=http_status,
            next_attempt_at=now + delay,
        )


def _retry_after_seconds(headers: Mapping[str, str], now: float) -> float | None:
    value = next(
        (
            str(header_value).strip()
            for header_name, header_value in headers.items()
            if str(header_name).lower() == "retry-after"
        ),
        "",
    )
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            seconds = parsed.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(1.0, seconds)
