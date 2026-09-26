from __future__ import annotations

import logging
import os
import threading
import time


DEFAULT_BROWSER_CAPACITY = 2
MAX_BROWSER_CAPACITY = 8
LOGGER = logging.getLogger(__name__)


class BrowserPermit:
    def __init__(self, gate: "MissavBrowserGate") -> None:
        self._gate = gate
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._gate._release()

    def __enter__(self) -> "BrowserPermit":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class MissavBrowserGate:
    def __init__(self, capacity: int = 8) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("browser gate capacity must be a positive integer")
        self.capacity = capacity
        self._condition = threading.Condition()
        self._active = 0
        self._active_peak = 0
        self._download_waiters = 0

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active

    @property
    def active_peak(self) -> int:
        with self._condition:
            return self._active_peak

    @property
    def download_waiter_count(self) -> int:
        with self._condition:
            return self._download_waiters

    def acquire_download(
        self,
        *,
        cancel_event: threading.Event | None = None,
        timeout: float | None = None,
    ) -> BrowserPermit | None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._download_waiters += 1
            try:
                while self._active >= self.capacity:
                    if cancel_event is not None and cancel_event.is_set():
                        return None
                    remaining = (
                        None if deadline is None else deadline - time.monotonic()
                    )
                    if remaining is not None and remaining <= 0:
                        return None
                    self._condition.wait(
                        timeout=(min(0.1, remaining) if remaining is not None else 0.1)
                    )
                if cancel_event is not None and cancel_event.is_set():
                    return None
                return self._grant_locked()
            finally:
                self._download_waiters -= 1
                self._condition.notify_all()

    def acquire_background(
        self,
        *,
        blocking: bool,
        cancel_event: threading.Event | None = None,
        timeout: float | None = None,
    ) -> BrowserPermit | None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._active >= self.capacity or self._download_waiters > 0:
                if not blocking:
                    return None
                if cancel_event is not None and cancel_event.is_set():
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(
                    timeout=(min(0.1, remaining) if remaining is not None else 0.1)
                )
            if cancel_event is not None and cancel_event.is_set():
                return None
            return self._grant_locked()

    def _grant_locked(self) -> BrowserPermit:
        self._active += 1
        self._active_peak = max(self._active_peak, self._active)
        return BrowserPermit(self)

    def _release(self) -> None:
        with self._condition:
            if self._active < 1:
                raise RuntimeError("browser gate permit was released unexpectedly")
            self._active -= 1
            self._condition.notify_all()


def _configured_browser_capacity() -> int:
    raw = os.environ.get(
        "JAV_PILOT_MISSAV_BROWSER_CONCURRENCY",
        str(DEFAULT_BROWSER_CAPACITY),
    ).strip()
    try:
        capacity = int(raw)
    except (TypeError, ValueError, OverflowError):
        capacity = 0
    if 1 <= capacity <= MAX_BROWSER_CAPACITY:
        return capacity
    LOGGER.warning(
        "invalid MissAV browser concurrency; using the safe default of %d",
        DEFAULT_BROWSER_CAPACITY,
    )
    return DEFAULT_BROWSER_CAPACITY


MISSAV_BROWSER_GATE = MissavBrowserGate(capacity=_configured_browser_capacity())
