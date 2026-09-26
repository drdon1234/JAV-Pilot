from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .replacements import DownloadReplacementError, DownloadReplacementStore


@dataclass(frozen=True, slots=True)
class MagnetDiscovery:
    status: str
    count: int = 0
    error_code: str | None = None
    magnets: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class WebDiscovery:
    status: str
    provider_ids: tuple[str, ...] = ()
    variant: str | None = None
    error_code: str | None = None


ProbeMagnets = Callable[[str, tuple[str, ...], threading.Event], MagnetDiscovery]
ProbeWeb = Callable[[str, threading.Event], WebDiscovery]


class DownloadResourceRecoveryManager:
    """Durably schedules one explicitly selected recovery channel per request."""

    def __init__(
        self,
        store: DownloadReplacementStore,
        *,
        probe_magnets: ProbeMagnets,
        probe_web: ProbeWeb,
        worker_count: int = 2,
    ) -> None:
        self.store = store
        self._probe_magnets = probe_magnets
        self._probe_web = probe_web
        self._worker_count = max(1, min(int(worker_count), 4))
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._scheduled: set[str] = set()
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._workers = tuple(
            threading.Thread(
                target=self._worker_loop,
                name=f"jav-download-recovery-{index + 1}",
                daemon=True,
            )
            for index in range(self._worker_count)
        )
        self.store.recover_discoveries()
        for replacement_id in self.store.pending_discovery_ids(limit=500):
            self._schedule(replacement_id)
        for worker in self._workers:
            worker.start()

    def enqueue(
        self,
        replacement_id: object,
        *,
        mode: object,
    ) -> dict[str, object]:
        if self._stopping.is_set():
            raise DownloadReplacementError("download resource recovery is shutting down")
        replacement = self.store.queue_discovery(replacement_id, mode=mode)
        self._schedule(str(replacement["replacement_id"]))
        return replacement

    def resume(self, replacement_id: object) -> dict[str, object]:
        if self._stopping.is_set():
            raise DownloadReplacementError("download resource recovery is shutting down")
        replacement = self.store.get(replacement_id)
        if str(replacement.get("discovery_status") or "") == "queued":
            self._schedule(str(replacement["replacement_id"]))
        return replacement

    def _schedule(self, replacement_id: str) -> None:
        clean_id = str(replacement_id)
        with self._lock:
            if clean_id not in self._scheduled:
                self._scheduled.add(clean_id)
                self._queue.put(clean_id)

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        self._stopping.set()
        for _ in self._workers:
            self._queue.put(None)
        # Joining the daemon workers is bounded; network probes receive the
        # same stop signal and are allowed to finish outside the deadline.
        for worker in self._workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(not worker.is_alive() for worker in self._workers)

    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                replacement_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if replacement_id is None:
                self._queue.task_done()
                return
            try:
                self._run_one(replacement_id)
            finally:
                with self._lock:
                    self._scheduled.discard(replacement_id)
                self._queue.task_done()

    def _run_one(self, replacement_id: str) -> None:
        claimed = self.store.claim_discovery(replacement_id)
        if claimed is None:
            return
        lease_token = str(claimed.get("discovery_lease_token") or "")
        if not lease_token:
            return
        code = str(claimed.get("code") or "")
        mode = str(claimed.get("recovery_mode") or "")
        if mode in {"smart_magnet", "manual_magnet"}:
            excluded_hashes = (
                (str(claimed.get("source_id") or "").lower(),)
                if str(claimed.get("source_kind") or "") == "qb"
                else ()
            )
            try:
                result = self._probe_magnets(code, excluded_hashes, self._stopping)
            except Exception as exc:  # discovery failures are persisted as inconclusive
                result = MagnetDiscovery("unavailable", error_code=_safe_error_code(exc))
            if self._stopping.is_set():
                return
            try:
                self.store.finish_magnet_discovery(
                    replacement_id,
                    lease_token,
                    magnet_status=result.status,
                    magnet_count=result.count,
                    magnets=result.magnets,
                    magnet_error_code=result.error_code,
                )
            except Exception:
                return
            return
        if mode == "web":
            try:
                result = self._probe_web(code, self._stopping)
            except Exception as exc:  # discovery failures are persisted as inconclusive
                result = WebDiscovery("unavailable", error_code=_safe_error_code(exc))
            if self._stopping.is_set():
                return
            try:
                self.store.finish_web_discovery(
                    replacement_id,
                    lease_token,
                    web_status=result.status,
                    web_provider_ids=result.provider_ids,
                    web_variant=result.variant,
                    web_error_code=result.error_code,
                )
            except Exception:
                return


def _safe_error_code(error: BaseException) -> str:
    value = str(getattr(error, "code", "") or "discovery_failed").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value or "") is None:
        return "discovery_failed"
    return value[:64]


__all__ = [
    "DownloadResourceRecoveryManager",
    "MagnetDiscovery",
    "WebDiscovery",
]
