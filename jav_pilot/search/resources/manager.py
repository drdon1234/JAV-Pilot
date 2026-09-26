"""Runs resource search sessions and routes them to the configured searchers."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from pathlib import Path

from ...missav.browser_gate import MISSAV_BROWSER_GATE
from .errors import (
    ResourceSearchCancelledError,
    ResourceSearchError,
    ResourceSearchUnavailableError,
)
from .models import (
    CLAIM_REQUEUE_DELAY,
    DEFAULT_RESOURCE_SEARCH_RESULTS,
    RESOURCE_SEARCH_SOURCE_IDS,
    STORE_RETRY_DELAYS,
    ResourceSearchDiscoverer,
    ResourceSearchPageEvent,
    ResourceSearchState,
    ResourceSearchWork,
    ResourceSearchWorkerEvent,
    ResourceSearchWorkerResult,
)
from .protocol import ResourceSearchWorkerError
from .searchers import SubprocessMissavResourceSearcher
from .store import ResourceSearchStore
from .validation import validate_session_id

__all__ = [
    "ResourceSearchManager",
]


class ResourceSearchManager:
    def __init__(
        self,
        database_path: Path | str,
        *,
        discoverer: ResourceSearchDiscoverer | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = ResourceSearchStore(
            database_path,
            clock=clock,
            id_factory=id_factory,
        )
        self._discoverer = discoverer or SubprocessMissavResourceSearcher()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._pending_sessions: set[str] = set()
        self._stopping = False
        self._stop_event = threading.Event()
        queued_ids = self.store.queued_ids()
        self._threads = tuple(
            threading.Thread(
                target=self._run,
                name=f"jav-resource-search-manager-{index}",
                daemon=True,
            )
            for index in range(len(RESOURCE_SEARCH_SOURCE_IDS))
        )
        for session_id in queued_ids:
            self._queue.put(session_id)
        for thread in self._threads:
            thread.start()

    def create(
        self,
        query: object,
        *,
        source_id: object = "missav",
        source_ids: Sequence[str] | None = None,
        result_limit: object = DEFAULT_RESOURCE_SEARCH_RESULTS,
        suffix_width: object | None = None,
        start: object | None = None,
        end: object | None = None,
        exact_match: bool = False,
    ) -> dict[str, object]:
        session = self.store.create(
            source_id,
            query,
            source_ids=source_ids,
            result_limit=result_limit,
            suffix_width=suffix_width,
            start=start,
            end=end,
            exact_match=exact_match,
        )
        self._enqueue_search(str(session["session_id"]))
        return session

    def get(
        self,
        session_id: object,
        *,
        limit: object = 25,
        offset: object = 0,
        keyword: object | None = None,
        variant: object | None = None,
    ) -> dict[str, object]:
        return self.store.get(
            session_id,
            limit=limit,
            offset=offset,
            keyword=keyword,
            variant=variant,
        )

    def continue_search(
        self,
        session_id: object,
        expected_revision: object,
        result_limit: object,
    ) -> dict[str, object]:
        session = self.store.continue_search(
            session_id,
            expected_revision,
            result_limit,
        )
        self._enqueue_search(str(session["session_id"]))
        return session

    def retry(
        self,
        session_id: object,
        expected_revision: object,
    ) -> dict[str, object]:
        session = self.store.retry(session_id, expected_revision)
        self._enqueue_search(str(session["session_id"]))
        return session

    def cancel(
        self,
        session_id: object,
        expected_revision: object,
    ) -> dict[str, object]:
        clean_id = validate_session_id(session_id)
        session = self.store.cancel(clean_id, expected_revision)
        targets = self.store.member_ids(clean_id) or (clean_id,)
        with self._lock:
            for target in targets:
                cancel_event = self._cancel_events.get(target)
                if cancel_event is not None:
                    cancel_event.set()
        return session

    def remove(
        self,
        session_id: object,
        expected_revision: object,
    ) -> dict[str, object]:
        return self.store.remove(session_id, expected_revision)

    def snapshot_selected(
        self,
        session_id: object,
        expected_revision: object,
        item_ids: Sequence[object],
    ) -> tuple[dict[str, object], ...]:
        return self.store.snapshot_selected(
            session_id,
            expected_revision,
            item_ids,
        )

    def is_alive(self) -> bool:
        return all(thread.is_alive() for thread in self._threads)

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        with self._lock:
            if not self._stopping:
                self._stopping = True
                self._stop_event.set()
                for cancel_event in self._cancel_events.values():
                    cancel_event.set()
                for _ in self._threads:
                    self._queue.put(None)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def _enqueue_search(self, session_id: str) -> None:
        for target in self.store.member_ids(session_id) or (session_id,):
            self._enqueue(target)

    def _enqueue(self, session_id: str) -> None:
        with self._lock:
            if self._stopping:
                raise ResourceSearchUnavailableError(
                    "resource search manager is stopping"
                )
            self._queue.put(session_id)

    def _run(self) -> None:
        while True:
            session_id = self._queue.get()
            if session_id is None:
                return
            with self._lock:
                if self._stopping:
                    return
                if session_id in self._cancel_events:
                    self._pending_sessions.add(session_id)
                    continue
                cancel_event = threading.Event()
                self._cancel_events[session_id] = cancel_event
            reschedule = False
            try:
                reschedule = self._run_session(session_id, cancel_event)
            except Exception:
                self._fail_with_retry(
                    session_id,
                    "internal_failure",
                    retryable=False,
                )
            finally:
                with self._lock:
                    self._cancel_events.pop(session_id, None)
                    requested = session_id in self._pending_sessions
                    self._pending_sessions.discard(session_id)
                    if (reschedule or requested) and not self._stopping:
                        self._queue.put(session_id)

    def _run_session(
        self,
        session_id: str,
        cancel_event: threading.Event,
    ) -> bool:
        work = self._claim_with_retry(session_id)
        if work is None:
            return False
        if work.result_limit == 0:
            status = self._finish_with_retry(
                session_id,
                ResourceSearchWorkerResult(False, work.state),
            )
            if status is None:
                self._fail_with_retry(session_id, "interrupted", retryable=True)
            return status == "queued"

        def on_event(event: ResourceSearchWorkerEvent) -> None:
            if cancel_event.is_set():
                raise ResourceSearchCancelledError("resource search was cancelled")
            if isinstance(event, ResourceSearchPageEvent):
                self.store.append_page(session_id, event)
            else:
                self.store.heartbeat(session_id)

        try:
            pending_remaining = len(work.state.pending) - work.state.cursor
            permit = None
            if work.source_id == "missav" and pending_remaining < work.result_limit:
                gate_deadline = time.monotonic() + 120.0
                while permit is None:
                    permit = MISSAV_BROWSER_GATE.acquire_background(
                        blocking=True,
                        cancel_event=cancel_event,
                        timeout=min(
                            2.0,
                            max(0.0, gate_deadline - time.monotonic()),
                        ),
                    )
                    if permit is not None:
                        break
                    if cancel_event.is_set():
                        raise ResourceSearchCancelledError(
                            "resource search was cancelled"
                        )
                    self.store.heartbeat(session_id)
                    if time.monotonic() >= gate_deadline:
                        raise ResourceSearchWorkerError("timeout", retryable=True)
            with permit if permit is not None else nullcontext():
                result = self._discoverer(
                    work,
                    on_event=on_event,
                    cancel_event=cancel_event,
                )
            status = self._finish_with_retry(session_id, result)
            if status is None:
                self._fail_with_retry(session_id, "interrupted", retryable=True)
            return status == "queued" and not cancel_event.is_set()
        except ResourceSearchCancelledError:
            if self._stopping:
                self._fail_with_retry(session_id, "interrupted", retryable=True)
        except ResourceSearchWorkerError as exc:
            self._checkpoint_with_retry(session_id, exc.state)
            self._fail_with_retry(session_id, exc.code, retryable=exc.retryable)
        except ResourceSearchUnavailableError:
            self._fail_with_retry(
                session_id,
                "dependency_unavailable",
                retryable=False,
            )
        except (OSError, sqlite3.Error):
            self._fail_with_retry(session_id, "interrupted", retryable=True)
        except ResourceSearchError:
            self._fail_with_retry(session_id, "protocol_failure", retryable=True)
        except Exception:
            self._fail_with_retry(session_id, "internal_failure", retryable=False)
        return False

    def _claim_with_retry(self, session_id: str) -> ResourceSearchWork | None:
        for attempt in range(len(STORE_RETRY_DELAYS) + 1):
            with self._lock:
                if self._stopping:
                    return None
            try:
                return self.store.claim(session_id)
            except (OSError, sqlite3.Error):
                if attempt >= len(STORE_RETRY_DELAYS):
                    break
                if self._stop_event.wait(STORE_RETRY_DELAYS[attempt]):
                    return None
            except ResourceSearchError:
                return None
        if self._stop_event.wait(CLAIM_REQUEUE_DELAY):
            return None
        with self._lock:
            if self._stopping:
                return None
            self._queue.put(session_id)
        return None

    def _finish_with_retry(
        self,
        session_id: str,
        result: ResourceSearchWorkerResult,
    ) -> str | None:
        for attempt in range(len(STORE_RETRY_DELAYS) + 1):
            try:
                return self.store.finish(session_id, result)
            except (OSError, sqlite3.Error):
                if attempt >= len(STORE_RETRY_DELAYS):
                    return None
                if self._stop_event.wait(STORE_RETRY_DELAYS[attempt]):
                    return None
            except ResourceSearchError:
                return None
        return None

    def _checkpoint_with_retry(
        self,
        session_id: str,
        state: ResourceSearchState | None,
    ) -> bool:
        if state is None:
            return True
        for attempt in range(len(STORE_RETRY_DELAYS) + 1):
            try:
                return self.store.checkpoint(session_id, state)
            except (OSError, sqlite3.Error):
                if attempt >= len(STORE_RETRY_DELAYS):
                    return False
                if self._stop_event.wait(STORE_RETRY_DELAYS[attempt]):
                    return False
            except ResourceSearchError:
                return False
        return False

    def _fail_with_retry(
        self,
        session_id: str,
        code: str,
        *,
        retryable: bool,
    ) -> bool:
        for attempt in range(len(STORE_RETRY_DELAYS) + 1):
            try:
                return self.store.fail(
                    session_id,
                    code,
                    retryable=retryable,
                )
            except (OSError, sqlite3.Error):
                if attempt >= len(STORE_RETRY_DELAYS):
                    return False
                if self._stop_event.wait(STORE_RETRY_DELAYS[attempt]):
                    return False
            except Exception:
                return False
        return False
