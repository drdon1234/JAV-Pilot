from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence

from ..config.settings import SITE_DIAGNOSTIC_SITE_IDS
from .diagnostics import SiteDiagnosticService


DEFAULT_DIAGNOSTIC_SITES = SITE_DIAGNOSTIC_SITE_IDS


class SiteDiagnosticScheduler:
    def __init__(
        self,
        service_factory: Callable[[], SiteDiagnosticService],
        *,
        sites: Sequence[str] = DEFAULT_DIAGNOSTIC_SITES,
        base_delay_seconds: float = 900.0,
        max_delay_seconds: float = 21_600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        clean_sites = tuple(str(site or "").strip().lower() for site in sites)
        if (
            not clean_sites
            or len(clean_sites) > len(DEFAULT_DIAGNOSTIC_SITES)
            or len(set(clean_sites)) != len(clean_sites)
            or not set(clean_sites).issubset(DEFAULT_DIAGNOSTIC_SITES)
        ):
            raise ValueError("site diagnostic scheduler sites are invalid")
        self._base_delay = _bounded_delay(base_delay_seconds)
        self._max_delay = _bounded_delay(max_delay_seconds)
        if self._max_delay < self._base_delay:
            raise ValueError("site diagnostic scheduler maximum delay is invalid")
        self._service_factory = service_factory
        self._sites = clean_sites
        self._monotonic = monotonic
        now = _safe_monotonic(monotonic())
        self._next_due = {site: now + self._base_delay for site in clean_sites}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.is_alive:
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="jav-site-diagnostics",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> bool:
        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError("site diagnostic scheduler timeout is invalid")
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return not self.is_alive

    def run_due(self, *, now: float | None = None) -> tuple[str, ...]:
        current = _safe_monotonic(self._monotonic() if now is None else now)
        with self._lock:
            due_sites = tuple(
                site for site in self._sites if self._next_due[site] <= current
            )
        completed: list[str] = []
        for site in due_sites:
            delay = self._base_delay
            try:
                outcome = self._service_factory().probe_automatic(
                    site,
                    base_delay_seconds=self._base_delay,
                    max_delay_seconds=self._max_delay,
                )
                delay = max(self._base_delay, outcome.next_delay_seconds)
            except Exception:  # noqa: BLE001 - probe details must not reach logs.
                delay = self._base_delay
            completed_at = (
                current if now is not None else _safe_monotonic(self._monotonic())
            )
            with self._lock:
                self._next_due[site] = completed_at + min(self._max_delay, delay)
            completed.append(site)
        return tuple(completed)

    def seconds_until_next(self, *, now: float | None = None) -> float:
        current = _safe_monotonic(self._monotonic() if now is None else now)
        with self._lock:
            return max(0.0, min(self._next_due.values()) - current)

    def _run(self) -> None:
        while not self._stop.is_set():
            timeout = self.seconds_until_next()
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.run_due()


def _bounded_delay(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("site diagnostic scheduler delay is invalid") from exc
    if not math.isfinite(parsed) or not 1.0 <= parsed <= 86_400.0:
        raise ValueError("site diagnostic scheduler delay is invalid")
    return parsed


def _safe_monotonic(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("site diagnostic scheduler clock is invalid")
    return float(value)


__all__ = ["SiteDiagnosticScheduler"]
