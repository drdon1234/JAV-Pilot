"""Notification outbox registration and dispatcher runtime."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from ...config.runtime_config import RuntimeConfigError, notification_config
from ...core.observability import emit_json_log
from ...notifications.adapters import NotificationConfig, build_notification_adapters
from ...notifications.dispatcher import NotificationDispatcher
from ...notifications.errors import NotificationError, NotificationStoreError
from ...notifications.outbox import SQLiteNotificationOutbox
from .. import state
from .environment import site_diagnostic_database_path
from .history import history_maintenance_mode

NOTIFICATION_RUNTIME_DRAIN_TIMEOUT_SECONDS = 35.0
NOTIFICATION_REGISTRATION_RETRY_MIN_SECONDS = 2.0
NOTIFICATION_REGISTRATION_RETRY_MAX_SECONDS = 60.0


NOTIFICATION_REGISTRATION_ERRORS = (
    NotificationError,
    RuntimeConfigError,
    OSError,
    sqlite3.Error,
    RuntimeError,
    TypeError,
    ValueError,
)


def build_notification_runtime(
    config: NotificationConfig,
) -> tuple[
    tuple[SQLiteNotificationOutbox, ...],
    tuple[NotificationDispatcher, ...],
]:
    adapters = build_notification_adapters(config)
    paths: list[Path] = []
    if state.SITE_DIAGNOSTICS is not None:
        paths.append(state.SITE_DIAGNOSTICS.path)
    if state.WEB_DOWNLOADS is not None:
        paths.append(state.WEB_DOWNLOADS.store.path)
    if state.MEDIA_METADATA is not None:
        paths.append(state.MEDIA_METADATA.store.path)
    if not paths:
        paths.append(site_diagnostic_database_path())
    unique_paths = tuple(dict.fromkeys(path.resolve() for path in paths))
    outboxes = tuple(SQLiteNotificationOutbox(path) for path in unique_paths)
    dispatchers = tuple(NotificationDispatcher(outbox, adapters) for outbox in outboxes)
    return outboxes, dispatchers


def install_notification_runtime(
    outboxes: tuple[SQLiteNotificationOutbox, ...],
    dispatchers: tuple[NotificationDispatcher, ...],
    *,
    paused_runtime: tuple[int, bool] | None = None,
) -> None:
    with state.NOTIFICATION_RUNTIME_CONDITION:
        if (
            paused_runtime is not None
            and state.NOTIFICATION_RUNTIME_GENERATION != paused_runtime[0]
        ):
            raise NotificationStoreError("notification runtime transition was lost")
        previous_generation = state.NOTIFICATION_RUNTIME_GENERATION
        state.NOTIFICATION_OUTBOXES = outboxes
        state.NOTIFICATION_DISPATCHERS = dispatchers
        state.NOTIFICATION_RUNTIME_GENERATION += 1
        state.NOTIFICATION_RUNTIME_ACCEPTING = True
        state.NOTIFICATION_RUNTIME_INFLIGHT.pop(previous_generation, None)
        state.NOTIFICATION_RUNTIME_CONDITION.notify_all()
    state.NOTIFICATION_WAKE.set()


def refresh_notification_runtime() -> None:
    with state.NOTIFICATION_CONFIG_LOCK:
        runtime = build_notification_runtime(notification_config())
        paused_runtime = pause_notification_runtime(
            timeout=NOTIFICATION_RUNTIME_DRAIN_TIMEOUT_SECONDS
        )
        install_notification_runtime(*runtime, paused_runtime=paused_runtime)


def _register_notification_outbox(expected: Path) -> None:
    with state.NOTIFICATION_CONFIG_LOCK:
        with state.NOTIFICATION_RUNTIME_LOCK:
            if any(
                outbox.path.resolve() == expected for outbox in state.NOTIFICATION_OUTBOXES
            ):
                return
        refresh_notification_runtime()
        with state.NOTIFICATION_RUNTIME_LOCK:
            registered = any(
                outbox.path.resolve() == expected for outbox in state.NOTIFICATION_OUTBOXES
            )
    if not registered:
        raise NotificationStoreError("notification outbox registration was lost")


def _defer_notification_outbox_registration(expected: Path, component: str) -> None:
    now = time.monotonic()
    with state.NOTIFICATION_REGISTRATION_LOCK:
        if expected not in state.NOTIFICATION_PENDING_OUTBOXES:
            state.NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS = (
                NOTIFICATION_REGISTRATION_RETRY_MIN_SECONDS
            )
            retry_at = now + NOTIFICATION_REGISTRATION_RETRY_MIN_SECONDS
            if state.NOTIFICATION_REGISTRATION_RETRY_AT <= 0:
                state.NOTIFICATION_REGISTRATION_RETRY_AT = retry_at
            else:
                state.NOTIFICATION_REGISTRATION_RETRY_AT = min(
                    state.NOTIFICATION_REGISTRATION_RETRY_AT,
                    retry_at,
                )
        state.NOTIFICATION_PENDING_OUTBOXES[expected] = component
    state.NOTIFICATION_WAKE.set()


def _complete_notification_outbox_registration(expected: Path) -> None:
    with state.NOTIFICATION_REGISTRATION_LOCK:
        state.NOTIFICATION_PENDING_OUTBOXES.pop(expected, None)
        if not state.NOTIFICATION_PENDING_OUTBOXES:
            state.NOTIFICATION_REGISTRATION_RETRY_AT = 0.0
            state.NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS = (
                NOTIFICATION_REGISTRATION_RETRY_MIN_SECONDS
            )


def ensure_notification_outbox_registered(path: Path, *, component: str) -> bool:
    if state.SERVER_STOPPING.is_set():
        return False
    try:
        expected = Path(path).resolve()
    except NOTIFICATION_REGISTRATION_ERRORS:
        emit_json_log(
            component,
            "notification_outbox_registration_deferred",
            level="warning",
            error_code="storage_unavailable",
            outcome="pending",
        )
        return False
    try:
        _register_notification_outbox(expected)
    except NOTIFICATION_REGISTRATION_ERRORS:
        _defer_notification_outbox_registration(expected, component)
        emit_json_log(
            component,
            "notification_outbox_registration_deferred",
            level="warning",
            error_code="storage_unavailable",
            outcome="pending",
        )
        return False
    _complete_notification_outbox_registration(expected)
    return True


def _retry_pending_notification_outboxes() -> None:
    if state.NOTIFICATION_STOP.is_set() or state.SERVER_STOPPING.is_set():
        return
    now = time.monotonic()
    with state.NOTIFICATION_REGISTRATION_LOCK:
        if (
            not state.NOTIFICATION_PENDING_OUTBOXES
            or now < state.NOTIFICATION_REGISTRATION_RETRY_AT
        ):
            return
        pending = tuple(state.NOTIFICATION_PENDING_OUTBOXES.items())
    failed = False
    for expected, _component in pending:
        if state.NOTIFICATION_STOP.is_set() or state.SERVER_STOPPING.is_set():
            return
        try:
            _register_notification_outbox(expected)
        except NOTIFICATION_REGISTRATION_ERRORS:
            failed = True
        else:
            _complete_notification_outbox_registration(expected)
    with state.NOTIFICATION_REGISTRATION_LOCK:
        pending_count = len(state.NOTIFICATION_PENDING_OUTBOXES)
        if pending_count:
            if failed:
                state.NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS = min(
                    NOTIFICATION_REGISTRATION_RETRY_MAX_SECONDS,
                    max(
                        NOTIFICATION_REGISTRATION_RETRY_MIN_SECONDS,
                        state.NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS * 2,
                    ),
                )
            state.NOTIFICATION_REGISTRATION_RETRY_AT = (
                time.monotonic() + state.NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS
            )
        else:
            state.NOTIFICATION_REGISTRATION_RETRY_AT = 0.0
            state.NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS = (
                NOTIFICATION_REGISTRATION_RETRY_MIN_SECONDS
            )
    if failed:
        emit_json_log(
            "notifications",
            "notification_outbox_registration_retry_deferred",
            level="warning",
            error_code="storage_unavailable",
            outcome="pending",
            count=pending_count,
        )


def _notification_dispatch_wait_seconds() -> float:
    default = 2.0
    with state.NOTIFICATION_REGISTRATION_LOCK:
        if not state.NOTIFICATION_PENDING_OUTBOXES:
            return default
        remaining = state.NOTIFICATION_REGISTRATION_RETRY_AT - time.monotonic()
    return max(0.01, min(default, remaining))


def pause_notification_runtime(*, timeout: float) -> tuple[int, bool]:
    try:
        clean_timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise NotificationStoreError(
            "notification runtime transition timeout is invalid"
        ) from exc
    if not 0 < clean_timeout <= 120:
        raise NotificationStoreError(
            "notification runtime transition timeout is invalid"
        )
    deadline = time.monotonic() + clean_timeout
    with state.NOTIFICATION_RUNTIME_CONDITION:
        generation = state.NOTIFICATION_RUNTIME_GENERATION
        previously_accepting = state.NOTIFICATION_RUNTIME_ACCEPTING
        state.NOTIFICATION_RUNTIME_ACCEPTING = False
        state.NOTIFICATION_RUNTIME_CONDITION.notify_all()
        state.NOTIFICATION_WAKE.set()
        while state.NOTIFICATION_RUNTIME_INFLIGHT.get(generation, 0) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.NOTIFICATION_RUNTIME_ACCEPTING = previously_accepting
                state.NOTIFICATION_RUNTIME_CONDITION.notify_all()
                state.NOTIFICATION_WAKE.set()
                raise NotificationStoreError(
                    "notification runtime transition timed out"
                )
            state.NOTIFICATION_RUNTIME_CONDITION.wait(timeout=remaining)
        return generation, previously_accepting


def resume_notification_runtime(paused_runtime: tuple[int, bool]) -> None:
    generation, previously_accepting = paused_runtime
    with state.NOTIFICATION_RUNTIME_CONDITION:
        if state.NOTIFICATION_RUNTIME_GENERATION != generation:
            return
        state.NOTIFICATION_RUNTIME_ACCEPTING = previously_accepting
        state.NOTIFICATION_RUNTIME_CONDITION.notify_all()
    state.NOTIFICATION_WAKE.set()


def primary_notification_outbox() -> SQLiteNotificationOutbox:
    with state.NOTIFICATION_RUNTIME_LOCK:
        if state.NOTIFICATION_OUTBOXES:
            return state.NOTIFICATION_OUTBOXES[0]
    refresh_notification_runtime()
    with state.NOTIFICATION_RUNTIME_LOCK:
        if not state.NOTIFICATION_OUTBOXES:
            raise NotificationStoreError("notification outbox is unavailable")
        return state.NOTIFICATION_OUTBOXES[0]


def ensure_notification_worker() -> bool:
    try:
        if history_maintenance_mode():
            return False
    except RuntimeConfigError:
        return False
    if state.SERVER_STOPPING.is_set():
        return False
    with state.NOTIFICATION_RUNTIME_LOCK:
        existing = state.NOTIFICATION_DISPATCHER_WORKER
        if existing is not None and existing.is_alive():
            return True
        state.NOTIFICATION_STOP.clear()
        worker = threading.Thread(
            target=_run_notification_dispatcher,
            name="jav-notification-dispatcher",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as exc:
            if state.NOTIFICATION_DISPATCHER_WORKER is existing:
                state.NOTIFICATION_DISPATCHER_WORKER = None
            raise NotificationStoreError(
                "notification dispatcher could not start"
            ) from exc
        if not worker.is_alive():
            if state.NOTIFICATION_DISPATCHER_WORKER is existing:
                state.NOTIFICATION_DISPATCHER_WORKER = None
            raise NotificationStoreError(
                "notification dispatcher did not remain running"
            )
        state.NOTIFICATION_DISPATCHER_WORKER = worker
        return True


def _acquire_notification_runtime() -> (
    tuple[int, tuple[NotificationDispatcher, ...]] | None
):
    with state.NOTIFICATION_RUNTIME_CONDITION:
        if state.NOTIFICATION_STOP.is_set() or not state.NOTIFICATION_RUNTIME_ACCEPTING:
            return None
        generation = state.NOTIFICATION_RUNTIME_GENERATION
        state.NOTIFICATION_RUNTIME_INFLIGHT[generation] = (
            state.NOTIFICATION_RUNTIME_INFLIGHT.get(generation, 0) + 1
        )
        return generation, tuple(state.NOTIFICATION_DISPATCHERS)


def _release_notification_runtime(generation: int) -> None:
    with state.NOTIFICATION_RUNTIME_CONDITION:
        inflight = state.NOTIFICATION_RUNTIME_INFLIGHT.get(generation, 0)
        if inflight <= 1:
            state.NOTIFICATION_RUNTIME_INFLIGHT.pop(generation, None)
        else:
            state.NOTIFICATION_RUNTIME_INFLIGHT[generation] = inflight - 1
        state.NOTIFICATION_RUNTIME_CONDITION.notify_all()


def _notification_dispatch_should_stop(generation: int) -> bool:
    with state.NOTIFICATION_RUNTIME_LOCK:
        return (
            state.NOTIFICATION_STOP.is_set()
            or not state.NOTIFICATION_RUNTIME_ACCEPTING
            or state.NOTIFICATION_RUNTIME_GENERATION != generation
        )


def _run_notification_dispatcher() -> None:
    while not state.NOTIFICATION_STOP.is_set():
        state.NOTIFICATION_WAKE.clear()
        _retry_pending_notification_outboxes()
        runtime = _acquire_notification_runtime()
        if runtime is None:
            state.NOTIFICATION_WAKE.wait(timeout=_notification_dispatch_wait_seconds())
            continue
        generation, dispatchers = runtime
        dispatched = 0
        try:
            for dispatcher in dispatchers:
                if _notification_dispatch_should_stop(generation):
                    break
                try:
                    dispatched += len(
                        dispatcher.dispatch_available(
                            limit=8,
                            stop_requested=lambda generation=generation: (
                                _notification_dispatch_should_stop(generation)
                            ),
                        )
                    )
                except (NotificationError, OSError, sqlite3.Error):
                    emit_json_log(
                        "notifications",
                        "dispatch_cycle_failed",
                        level="warning",
                        error_code="delivery_unavailable",
                    )
        finally:
            _release_notification_runtime(generation)
        if dispatched:
            continue
        state.NOTIFICATION_WAKE.wait(timeout=_notification_dispatch_wait_seconds())


def stop_notification_worker(*, timeout: float) -> bool:
    state.NOTIFICATION_STOP.set()
    state.NOTIFICATION_WAKE.set()
    with state.NOTIFICATION_RUNTIME_CONDITION:
        state.NOTIFICATION_RUNTIME_ACCEPTING = False
        state.NOTIFICATION_RUNTIME_CONDITION.notify_all()
        worker = state.NOTIFICATION_DISPATCHER_WORKER
    if worker is not None and worker.ident is not None:
        worker.join(timeout=max(0.0, timeout))
    if worker is not None and worker.is_alive():
        emit_json_log(
            "notifications",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
        return False
    with state.NOTIFICATION_RUNTIME_CONDITION:
        if state.NOTIFICATION_DISPATCHER_WORKER is worker:
            state.NOTIFICATION_DISPATCHER_WORKER = None
        state.NOTIFICATION_RUNTIME_CONDITION.notify_all()
    return True
