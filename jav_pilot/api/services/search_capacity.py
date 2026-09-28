"""Admission control for concurrent metadata searches."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future

from .. import state


def acquire_search_capacity(lane: threading.BoundedSemaphore) -> bool:
    if not lane.acquire(blocking=False):
        return False
    if state.SEARCH_TOTAL_SLOTS.acquire(blocking=False):
        return True
    lane.release()
    return False


def release_search_capacity(lane: threading.BoundedSemaphore) -> None:
    state.SEARCH_TOTAL_SLOTS.release()
    lane.release()


def inline_future(
    function: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> Future:
    future: Future = Future()
    try:
        future.set_result(function(*args, **kwargs))
    except BaseException as exc:  # noqa: BLE001 - preserve Future exception semantics.
        future.set_exception(exc)
    return future
