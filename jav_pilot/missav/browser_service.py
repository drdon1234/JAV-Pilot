from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast

from ..core.guards import contains_sensitive_transport_text


class BrowserOperationKind(str, Enum):
    MANIFEST_REFRESH = "manifest_refresh"
    DOWNLOAD_CAPTURE = "download_capture"
    INTERACTIVE = "interactive"
    SEARCH_PAGE = "search_page"
    DIAGNOSTIC = "diagnostic"
    QUALITY_DISCOVERY = "quality_discovery"
    DESCRIPTION = "description"
    RESOURCE_SEARCH = "resource_search"
    SERIES_DISCOVERY = "series_discovery"


_OPERATION_PRIORITY = {
    BrowserOperationKind.MANIFEST_REFRESH: 0,
    BrowserOperationKind.DOWNLOAD_CAPTURE: 1,
    BrowserOperationKind.INTERACTIVE: 2,
    BrowserOperationKind.QUALITY_DISCOVERY: 2,
    BrowserOperationKind.SEARCH_PAGE: 3,
    BrowserOperationKind.RESOURCE_SEARCH: 3,
    BrowserOperationKind.SERIES_DISCOVERY: 3,
    BrowserOperationKind.DESCRIPTION: 4,
    BrowserOperationKind.DIAGNOSTIC: 4,
}
_DOWNLOAD_CRITICAL_KINDS = frozenset(
    {
        BrowserOperationKind.MANIFEST_REFRESH,
        BrowserOperationKind.DOWNLOAD_CAPTURE,
    }
)


class BrowserServiceState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    TRUSTED = "trusted"
    COOLDOWN = "cooldown"
    PROBING = "probing"
    RESTARTING = "restarting"
    SHUTTING_DOWN = "shutting_down"


class BrowserServiceError(RuntimeError):
    pass


class BrowserServiceClosed(BrowserServiceError):
    pass


class BrowserOwnershipError(BrowserServiceError):
    pass


_BROWSER_RESTART_REASONS = (
    "transport_reset",
    "target_closed",
    "session_invariant",
    "startup_failure",
)


class BrowserDriverCrashed(BrowserServiceError):
    """A browser failure classified without retaining transport details."""

    def __init__(self, reason: str = "target_closed") -> None:
        if reason not in _BROWSER_RESTART_REASONS:
            raise ValueError("browser restart reason is invalid")
        super().__init__("MissAV browser session must be restarted")
        self.reason = reason


class BrowserDriverInvariantError(BrowserDriverCrashed):
    def __init__(self) -> None:
        super().__init__("session_invariant")


class BrowserOperationFailed(BrowserServiceError):
    """A non-retryable operation failure without upstream/browser details.

    Playwright exceptions frequently include the current URL, cookies, or
    signed request data in their text.  The broker must never hand those
    implementation details to callers (which may persist the exception or
    render it in a job error), so unexpected driver failures are normalized to
    this fixed message at the broker boundary.
    """

    def __init__(self) -> None:
        super().__init__("MissAV browser operation failed")


class BrowserOperationError(BrowserServiceError):
    """A bounded, non-secret operation result from the MissAV adapter."""

    def __init__(self, message: str, *, code: str = "discovery_unavailable") -> None:
        allowed = {
            "challenge_active",
            "dependency_unavailable",
            "discovery_unavailable",
            "navigation_timeout",
            "not_found",
            "parse_drift",
            "rate_limited",
            "route_drift",
            "safety_rejected",
            "transport_reset",
            "upstream_unavailable",
        }
        if code not in allowed:
            raise ValueError("browser operation error code is invalid")
        safe_message = str(message or "")
        if contains_sensitive_transport_text(safe_message):
            safe_message = "MissAV browser operation failed"
        super().__init__(safe_message[:240])
        self.code = code


class BrowserRestartExhausted(BrowserServiceError):
    pass


class BrowserOperationCancelled(BrowserServiceError):
    pass


class BrowserUpstreamError(BrowserServiceError):
    """A sanitized, retryable response from the browser's upstream site."""

    def __init__(
        self,
        status_code: int,
        *,
        retry_after: str | int | float | None = None,
    ) -> None:
        if status_code not in {403, 429, 503}:
            raise ValueError("retryable browser status must be 403, 429, or 503")
        super().__init__(f"browser upstream returned retryable status {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


class BrowserChallengeActive(BrowserUpstreamError):
    """A Cloudflare/Turnstile challenge that has not cleared yet.

    It is represented as a 403 for the shared cooldown machinery, while the
    distinct type lets a driver adapter classify challenge timeouts without
    putting page text or a URL into an error payload.
    """

    def __init__(self, *, retry_after: str | int | float | None = None) -> None:
        super().__init__(403, retry_after=retry_after)


@dataclass(frozen=True)
class BrowserSessionCounts:
    browsers: int
    contexts: int
    pages: int

    @property
    def is_single_page(self) -> bool:
        return (self.browsers, self.contexts, self.pages) == (1, 1, 1)


@dataclass(frozen=True)
class BrowserOperation:
    kind: BrowserOperationKind
    payload: object = field(repr=False)


class SinglePageBrowserDriver(Protocol):
    """Driver contract for exactly one browser, one context, and one page.

    Every method is invoked by the service's sole owner thread. Implementations
    must keep credentials and navigation state inside the driver and must not
    persist or log operation payloads.
    """

    def start(self) -> None: ...

    def execute(
        self,
        operation: BrowserOperation,
        *,
        cancel_event: threading.Event,
    ) -> object: ...

    def session_counts(self) -> BrowserSessionCounts: ...

    def close(self) -> None: ...


class BrowserOwnerLease(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class FileBrowserOwnerLease:
    """Non-blocking cross-process lease for the single browser owner."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or Path(tempfile.gettempdir()) / "jav-pilot-missav-browser.lock")
        self._guard = threading.Lock()
        self._handle: object | None = None

    def acquire(self) -> bool:
        with self._guard:
            if self._handle is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, PermissionError):
                handle.close()
                return False
            self._handle = handle
            return True

    def release(self) -> None:
        with self._guard:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                if os.name == "nt":
                    import msvcrt

                    cast(object, handle).seek(0)
                    msvcrt.locking(cast(object, handle).fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(cast(object, handle).fileno(), fcntl.LOCK_UN)
            finally:
                cast(object, handle).close()


@dataclass(frozen=True)
class BrowserServiceMetrics:
    state: BrowserServiceState
    owner_active: bool
    browser_count: int
    context_count: int
    page_count: int
    queue_depth: int
    queued_by_kind: tuple[tuple[str, int], ...]
    active_kind: str | None
    active_elapsed_seconds: float
    active_stage: str | None
    active_stage_elapsed_seconds: float
    submitted: int
    completed: int
    cancelled: int
    failed: int
    upstream_events: int
    challenge_events: int
    cooldown_windows: int
    cooldown_remaining_seconds: float
    probes_started: int
    probes_succeeded: int
    restart_attempts: int
    restart_exhausted: int
    restart_by_reason: tuple[tuple[str, int], ...]


T = TypeVar("T")


class BrowserTask(Generic[T]):
    def __init__(
        self,
        *,
        kind: BrowserOperationKind,
        future: Future[object],
        cancel: Callable[[], bool],
    ) -> None:
        self.kind = kind
        self._future = future
        self._cancel = cancel

    def cancel(self) -> bool:
        return self._cancel()

    def cancelled(self) -> bool:
        return self._future.cancelled()

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> T:
        return cast(T, self._future.result(timeout=timeout))

    def exception(self, timeout: float | None = None) -> BaseException | None:
        return self._future.exception(timeout=timeout)


@dataclass
class _QueuedOperation:
    sequence: int
    enqueued_at: float
    operation: BrowserOperation
    future: Future[object]
    cancel_event: threading.Event
    retry_on_upstream: bool
    restart_attempts: int = 0
    upstream_attempts: int = 0
    is_probe: bool = False


class MissavBrowserService:
    """Serializes all MissAV browser work through one trusted browser session."""

    def __init__(
        self,
        driver: SinglePageBrowserDriver | None = None,
        *,
        driver_factory: Callable[[], SinglePageBrowserDriver] | None = None,
        owner_lease: BrowserOwnerLease | None = None,
        cooldown_schedule: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0, 21600.0),
        max_cooldown: float = 21600.0,
        starvation_after: float = 30.0,
        max_restart_attempts: int = 2,
        max_upstream_attempts: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not cooldown_schedule or any(delay < 0 for delay in cooldown_schedule):
            raise ValueError("cooldown schedule must contain non-negative delays")
        if max_cooldown < 0:
            raise ValueError("maximum cooldown must be non-negative")
        if starvation_after <= 0:
            raise ValueError("starvation threshold must be positive")
        if isinstance(max_restart_attempts, bool) or max_restart_attempts < 0:
            raise ValueError("maximum restart attempts must be non-negative")
        if isinstance(max_upstream_attempts, bool) or max_upstream_attempts < 1:
            raise ValueError("maximum upstream attempts must be positive")

        # Accept a factory in the positional slot as well.  This keeps the
        # small broker API convenient for adapters while retaining backwards
        # compatibility with tests/callers that pass a concrete driver.
        if (
            driver_factory is None
            and driver is not None
            and callable(driver)
            and not hasattr(driver, "execute")
        ):
            driver_factory = cast(Callable[[], SinglePageBrowserDriver], driver)
            driver = None
        if driver is None and driver_factory is None:
            raise ValueError("a browser driver or driver factory is required")
        if driver is not None and driver_factory is not None:
            raise ValueError("provide either a browser driver or a driver factory")
        self._driver = driver
        self._driver_factory = driver_factory
        self._owner_lease = owner_lease or FileBrowserOwnerLease()
        self._cooldown_schedule = cooldown_schedule
        self._max_cooldown = max_cooldown
        self._starvation_after = starvation_after
        self._max_restart_attempts = max_restart_attempts
        self._max_upstream_attempts = max_upstream_attempts
        self._monotonic = monotonic
        self._wall_clock = wall_clock

        self._condition = threading.Condition()
        self._lifecycle_lock = threading.Lock()
        self._pending: list[_QueuedOperation] = []
        self._active: _QueuedOperation | None = None
        self._active_started_at: float | None = None
        self._thread: threading.Thread | None = None
        self._owner_thread_id: int | None = None
        self._owner_held = False
        self._driver_started = False
        self._ever_started = False
        self._stopping = False
        self._startup_ready = threading.Event()
        self._startup_error: BrowserServiceError | None = None
        self._closed = threading.Event()
        self._state = BrowserServiceState.STOPPED
        self._sequence = 0
        self._cooldown_until = 0.0
        self._retry_streak = 0
        self._probe_in_flight = False
        self._session_counts = BrowserSessionCounts(0, 0, 0)

        self._submitted = 0
        self._completed = 0
        self._cancelled = 0
        self._failed = 0
        self._upstream_events = 0
        self._challenge_events = 0
        self._cooldown_windows = 0
        self._probes_started = 0
        self._probes_succeeded = 0
        self._restart_attempt_count = 0
        self._restart_exhausted_count = 0
        self._restart_reason_counts = {
            reason: 0 for reason in _BROWSER_RESTART_REASONS
        }

    def start(self, *, timeout: float = 30.0) -> None:
        with self._lifecycle_lock:
            with self._condition:
                if self._thread is not None and self._thread.is_alive():
                    return
                if self._ever_started and not self._closed.is_set():
                    # The previous run has not finished tearing down yet; its
                    # state cannot be recycled safely.
                    raise BrowserServiceClosed("browser service is shutting down")
                if not self._owner_lease.acquire():
                    raise BrowserOwnershipError("another MissAV browser owner is active")
                self._owner_held = True
                self._ever_started = True
                # Recycle per-run lifecycle state: a launch failure (or a later
                # crash that exhausted the restart budget) leaves the service
                # STOPPED, and every browser capability would otherwise stay
                # dead until the whole process restarts.
                self._stopping = False
                self._driver_started = False
                self._startup_error = None
                self._startup_ready = threading.Event()
                self._closed = threading.Event()
                self._cooldown_until = 0.0
                self._retry_streak = 0
                self._probe_in_flight = False
                self._state = BrowserServiceState.STARTING
                startup_ready = self._startup_ready
                self._thread = threading.Thread(
                    target=self._run,
                    name="missav-browser-service",
                    daemon=True,
                )
                self._thread.start()

        if not startup_ready.wait(max(0.0, timeout)):
            self.shutdown(timeout=0.0)
            raise BrowserRestartExhausted("browser driver startup timed out")
        if self._startup_error is not None:
            raise self._startup_error

    def is_running(self) -> bool:
        """Return whether the owner thread is alive and accepting work."""

        with self._condition:
            return (
                self._thread is not None
                and self._thread.is_alive()
                and not self._stopping
                and self._state is not BrowserServiceState.STOPPED
            )

    def submit(
        self,
        kind: BrowserOperationKind | str,
        payload: object,
        *,
        retry_on_upstream: bool = True,
    ) -> BrowserTask[T]:
        operation_kind = BrowserOperationKind(kind)
        with self._condition:
            if (
                not self._ever_started
                or self._stopping
                or self._state is BrowserServiceState.STOPPED
            ):
                raise BrowserServiceClosed("browser service is not running")
            self._sequence += 1
            future: Future[object] = Future()
            item = _QueuedOperation(
                sequence=self._sequence,
                enqueued_at=self._monotonic(),
                operation=BrowserOperation(operation_kind, payload),
                future=future,
                cancel_event=threading.Event(),
                retry_on_upstream=retry_on_upstream,
            )
            self._pending.append(item)
            self._submitted += 1
            self._condition.notify_all()
        return BrowserTask(
            kind=operation_kind,
            future=future,
            cancel=lambda: self._cancel_item(item),
        )

    def shutdown(self, *, timeout: float | None = 10.0) -> bool:
        with self._condition:
            thread = self._thread
            if thread is None:
                return True
            if not self._stopping:
                self._stopping = True
                self._state = BrowserServiceState.SHUTTING_DOWN
                for item in self._pending:
                    self._cancel_item_locked(item)
                if self._active is not None:
                    self._cancel_item_locked(self._active)
                self._condition.notify_all()
        thread.join(timeout=None if timeout is None else max(0.0, timeout))
        return not thread.is_alive()

    def metrics_snapshot(self) -> BrowserServiceMetrics:
        with self._condition:
            counts = {kind.value: 0 for kind in BrowserOperationKind}
            for item in self._pending:
                if not item.future.cancelled():
                    counts[item.operation.kind.value] += 1
            remaining = 0.0
            if self._state in {BrowserServiceState.COOLDOWN, BrowserServiceState.PROBING}:
                remaining = max(0.0, self._cooldown_until - self._monotonic())
            now = self._monotonic()
            active_elapsed = (
                max(0.0, now - self._active_started_at)
                if self._active is not None and self._active_started_at is not None
                else 0.0
            )
            active_stage, active_stage_elapsed = self._driver_stage_snapshot()
            return BrowserServiceMetrics(
                state=self._state,
                owner_active=self._owner_held,
                browser_count=self._session_counts.browsers,
                context_count=self._session_counts.contexts,
                page_count=self._session_counts.pages,
                queue_depth=sum(counts.values()),
                queued_by_kind=tuple(counts.items()),
                active_kind=(self._active.operation.kind.value if self._active else None),
                active_elapsed_seconds=active_elapsed,
                active_stage=active_stage,
                active_stage_elapsed_seconds=active_stage_elapsed,
                submitted=self._submitted,
                completed=self._completed,
                cancelled=self._cancelled,
                failed=self._failed,
                upstream_events=self._upstream_events,
                challenge_events=self._challenge_events,
                cooldown_windows=self._cooldown_windows,
                cooldown_remaining_seconds=remaining,
                probes_started=self._probes_started,
                probes_succeeded=self._probes_succeeded,
                restart_attempts=self._restart_attempt_count,
                restart_exhausted=self._restart_exhausted_count,
                restart_by_reason=tuple(self._restart_reason_counts.items()),
            )

    def _run(self) -> None:
        self._owner_thread_id = threading.get_ident()
        try:
            try:
                self._start_driver_initially()
            except BrowserServiceError as error:
                with self._condition:
                    self._startup_error = error
                    self._state = BrowserServiceState.STOPPED
                    self._startup_ready.set()
                return
            with self._condition:
                self._state = BrowserServiceState.TRUSTED
                self._startup_ready.set()
                self._condition.notify_all()

            while True:
                with self._condition:
                    item = self._take_next_locked()
                    while item is None and not self._stopping:
                        self._condition.wait(timeout=self._wait_timeout_locked())
                        item = self._take_next_locked()
                    if item is None and self._stopping:
                        return
                    self._active = item
                    self._active_started_at = self._monotonic()
                try:
                    self._execute_item(item)
                finally:
                    with self._condition:
                        if self._active is item:
                            self._active = None
                            self._active_started_at = None
                        self._condition.notify_all()
        finally:
            self._close_driver()
            with self._condition:
                for item in self._pending:
                    self._cancel_item_locked(item)
                self._pending.clear()
                self._session_counts = BrowserSessionCounts(0, 0, 0)
                self._owner_thread_id = None
                self._state = BrowserServiceState.STOPPED
                self._startup_ready.set()
                self._closed.set()
                if self._owner_held:
                    self._owner_lease.release()
                    self._owner_held = False
                self._condition.notify_all()

    def _start_driver_initially(self) -> None:
        attempts = self._max_restart_attempts + 1
        for attempt in range(attempts):
            try:
                self._assert_owner_thread()
                driver = self._new_driver()
                driver.start()
                self._driver_started = True
                self._validate_driver_session()
                return
            except Exception:
                self._driver_started = False
                self._close_driver()
                if attempt + 1 >= attempts:
                    raise BrowserRestartExhausted(
                        "browser driver could not establish a single-page session"
                    ) from None
                self._record_restart_locked("startup_failure")
        raise AssertionError("unreachable browser startup state")

    def _execute_item(self, item: _QueuedOperation) -> None:
        if item.future.cancelled() or item.cancel_event.is_set():
            self._finish_probe_after_cancel(item)
            return
        if not self._driver_started and not self._recover_driver(item):
            self._fail_restart(item)
            return
        try:
            self._assert_owner_thread()
            driver = self._driver
            if driver is None:
                raise BrowserDriverInvariantError("browser driver is not initialized")
            result = driver.execute(
                item.operation,
                cancel_event=item.cancel_event,
            )
            self._validate_driver_session()
        except BrowserUpstreamError as error:
            self._handle_upstream_error(item, error)
            return
        except BrowserOperationCancelled:
            self._cancel_item(item)
            self._finish_probe_after_cancel(item)
            return
        except BrowserOperationError as error:
            if item.is_probe:
                self._finish_successful_probe()
            self._set_exception(item, error)
            return
        except BrowserDriverCrashed as error:
            if item.cancel_event.is_set():
                self._cancel_item(item)
                self._finish_probe_after_cancel(item)
                return
            if not self._recover_driver(item, reason=error.reason):
                self._fail_restart(item)
                return
            with self._condition:
                self._pending.append(item)
                self._condition.notify_all()
            return
        except Exception:
            if item.cancel_event.is_set():
                self._cancel_item(item)
                self._finish_probe_after_cancel(item)
                return
            # A Playwright TargetClosed/browser-disconnected exception may not
            # use our typed BrowserDriverCrashed class.  A failed session
            # health check is safe evidence that the broker must restart; do
            # this before converting the exception to a non-retryable error.
            if (
                not item.cancel_event.is_set()
                and not self._driver_session_is_healthy()
            ):
                if self._recover_driver(item, reason="target_closed"):
                    with self._condition:
                        self._pending.append(item)
                        self._condition.notify_all()
                    return
                self._fail_restart(item)
                return
            if item.is_probe:
                self._finish_successful_probe()
            # Never propagate raw Playwright/driver text.  Those exceptions
            # can contain manifest URLs, cookies, signed headers, or profile
            # paths and callers may persist or display the resulting error.
            self._set_exception(item, BrowserOperationFailed())
            return

        if item.is_probe:
            self._finish_successful_probe()
        with self._condition:
            if not item.future.cancelled():
                item.future.set_result(result)
                self._completed += 1

    def _handle_upstream_error(
        self,
        item: _QueuedOperation,
        error: BrowserUpstreamError,
    ) -> None:
        with self._condition:
            self._upstream_events += 1
            if error.status_code == 403:
                self._challenge_events += 1
            item.upstream_attempts += 1
            self._retry_streak += 1
            schedule_delay = self._cooldown_schedule[
                min(self._retry_streak - 1, len(self._cooldown_schedule) - 1)
            ]
            retry_after = _parse_retry_after(error.retry_after, self._wall_clock())
            delay = min(self._max_cooldown, max(schedule_delay, retry_after))
            self._cooldown_until = self._monotonic() + delay
            self._cooldown_windows += 1
            self._probe_in_flight = False
            self._state = BrowserServiceState.COOLDOWN
            item.is_probe = False
            if (
                item.retry_on_upstream
                and item.upstream_attempts < self._max_upstream_attempts
                and not item.future.cancelled()
            ):
                self._pending.append(item)
            else:
                self._set_exception_locked(item, error)
            self._condition.notify_all()

    def _finish_successful_probe(self) -> None:
        with self._condition:
            self._probe_in_flight = False
            self._retry_streak = 0
            self._cooldown_until = 0.0
            self._state = BrowserServiceState.TRUSTED
            self._probes_succeeded += 1
            self._condition.notify_all()

    def _finish_probe_after_cancel(self, item: _QueuedOperation) -> None:
        if not item.is_probe:
            return
        with self._condition:
            self._probe_in_flight = False
            self._cooldown_until = self._monotonic() + self._cooldown_schedule[
                min(max(0, self._retry_streak - 1), len(self._cooldown_schedule) - 1)
            ]
            self._cooldown_windows += 1
            self._state = BrowserServiceState.COOLDOWN
            item.is_probe = False
            self._condition.notify_all()

    def _recover_driver(
        self,
        item: _QueuedOperation,
        *,
        reason: str = "startup_failure",
    ) -> bool:
        while item.restart_attempts < self._max_restart_attempts:
            item.restart_attempts += 1
            with self._condition:
                self._state = BrowserServiceState.RESTARTING
                self._record_restart_locked(reason)
            self._close_driver()
            try:
                self._assert_owner_thread()
                driver = self._new_driver()
                driver.start()
                self._driver_started = True
                self._validate_driver_session()
            except Exception:
                self._driver_started = False
                reason = "startup_failure"
                continue
            with self._condition:
                # A crash while a cooldown probe was active invalidates that
                # probe.  Clear its in-flight marker before requeueing; leaving
                # PROBING set would make _take_next_locked wait forever for a
                # probe that can no longer complete.
                if item.is_probe:
                    item.is_probe = False
                    self._probe_in_flight = False
                    self._retry_streak = 0
                    self._cooldown_until = 0.0
                self._state = BrowserServiceState.TRUSTED
            return True
        self._close_driver()
        return False

    def _record_restart_locked(self, reason: str) -> None:
        if reason not in self._restart_reason_counts:
            reason = "target_closed"
        self._restart_attempt_count += 1
        self._restart_reason_counts[reason] += 1

    def _driver_stage_snapshot(self) -> tuple[str | None, float]:
        driver = self._driver
        snapshot = getattr(driver, "active_stage_snapshot", None)
        if not callable(snapshot):
            return None, 0.0
        try:
            stage, elapsed = snapshot()
        except Exception:
            return None, 0.0
        allowed_stages = {
            "locate_exact_detail",
            "reload_detail",
            "start_player",
            "manifest_wait",
            "quality_resolution",
        }
        if stage not in allowed_stages:
            return None, 0.0
        try:
            clean_elapsed = max(0.0, float(elapsed))
        except (TypeError, ValueError, OverflowError):
            clean_elapsed = 0.0
        return stage, clean_elapsed

    def _fail_restart(self, item: _QueuedOperation) -> None:
        with self._condition:
            self._restart_exhausted_count += 1
            self._state = BrowserServiceState.STARTING
            self._set_exception_locked(
                item,
                BrowserRestartExhausted("browser driver restart limit was reached"),
            )
            if item.is_probe:
                self._probe_in_flight = False
                self._state = BrowserServiceState.COOLDOWN
                self._cooldown_until = self._monotonic() + self._cooldown_schedule[
                    min(max(0, self._retry_streak - 1), len(self._cooldown_schedule) - 1)
                ]
                self._cooldown_windows += 1
            self._condition.notify_all()

    def _validate_driver_session(self) -> None:
        driver = self._driver
        if driver is None:
            raise BrowserDriverInvariantError("browser driver is not initialized")
        counts = driver.session_counts()
        with self._condition:
            self._session_counts = counts
        if not counts.is_single_page:
            raise BrowserDriverInvariantError(
                "browser driver violated the single-page session invariant"
            )

    def _driver_session_is_healthy(self) -> bool:
        driver = self._driver
        if driver is None:
            return False
        try:
            counts = driver.session_counts()
            return bool(counts.is_single_page)
        except Exception:
            return False

    def _take_next_locked(self) -> _QueuedOperation | None:
        self._pending = [item for item in self._pending if not item.future.cancelled()]
        if self._stopping or not self._pending:
            return None
        now = self._monotonic()
        if self._state is BrowserServiceState.COOLDOWN:
            if self._probe_in_flight or now < self._cooldown_until:
                return None
            item = self._select_pending_locked(now)
            item.is_probe = True
            self._probe_in_flight = True
            self._state = BrowserServiceState.PROBING
            self._probes_started += 1
            return item
        if self._state is BrowserServiceState.PROBING:
            return None
        return self._select_pending_locked(now)

    def _select_pending_locked(self, now: float) -> _QueuedOperation:
        critical = [
            item
            for item in self._pending
            if item.operation.kind in _DOWNLOAD_CRITICAL_KINDS
        ]
        if critical:
            # Capturing or refreshing a manifest is part of an already
            # admitted download.  An old bulk scan must not hold all worker
            # slots in ``locating`` while its own browser task is repeatedly
            # challenged.  Critical work is finite; lower-priority discovery
            # resumes immediately after these captures drain.
            selected = min(
                critical,
                key=lambda item: (
                    _OPERATION_PRIORITY[item.operation.kind],
                    item.sequence,
                ),
            )
        else:
            oldest = min(self._pending, key=lambda item: item.sequence)
            if now - oldest.enqueued_at >= self._starvation_after:
                selected = oldest
            else:
                selected = min(
                    self._pending,
                    key=lambda item: (
                        _OPERATION_PRIORITY[item.operation.kind],
                        item.sequence,
                    ),
                )
        self._pending.remove(selected)
        return selected

    def _wait_timeout_locked(self) -> float:
        if self._state is BrowserServiceState.COOLDOWN and self._pending:
            return min(0.25, max(0.0, self._cooldown_until - self._monotonic()))
        return 0.25

    def _cancel_item(self, item: _QueuedOperation) -> bool:
        with self._condition:
            cancelled = self._cancel_item_locked(item)
            self._condition.notify_all()
            return cancelled

    def _cancel_item_locked(self, item: _QueuedOperation) -> bool:
        item.cancel_event.set()
        cancelled = item.future.cancel()
        if cancelled:
            self._cancelled += 1
        return cancelled

    def _set_exception(self, item: _QueuedOperation, error: BaseException) -> None:
        with self._condition:
            self._set_exception_locked(item, error)

    def _set_exception_locked(
        self,
        item: _QueuedOperation,
        error: BaseException,
    ) -> None:
        if not item.future.done():
            item.future.set_exception(error)
            self._failed += 1

    def _close_driver(self) -> None:
        self._assert_owner_thread()
        driver = self._driver
        try:
            if driver is not None:
                driver.close()
        except Exception:
            pass
        self._driver_started = False

    def _new_driver(self) -> SinglePageBrowserDriver:
        if self._driver_factory is not None:
            self._driver = self._driver_factory()
        driver = self._driver
        if driver is None:
            raise BrowserDriverInvariantError("browser driver is not initialized")
        return driver

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("browser driver accessed outside its owner thread")


def _parse_retry_after(value: str | int | float | None, now: float) -> float:
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - now)
