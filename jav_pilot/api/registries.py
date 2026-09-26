"""In-memory registries that track search jobs, continuations and download dispositions."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from ..core.models import SearchContinuation, WorkResult
from ..downloads.replacements import (
    DownloadReplacementConflictError,
    DownloadReplacementError,
)
from . import state


@dataclass(frozen=True)
class SearchContinuationEnvelope:
    binding: tuple[object, ...]
    state: SearchContinuation
    works: tuple[WorkResult, ...]


@dataclass(frozen=True)
class _SearchContinuationRecord:
    envelope: SearchContinuationEnvelope
    status: str
    expires_at: float
    target_limit: int | None = None
    lease_id: str | None = None
    lease_expires_at: float | None = None
    snapshot: dict[str, object] | None = None


@dataclass(frozen=True)
class SearchContinuationDecision:
    status: str
    envelope: SearchContinuationEnvelope | None = None
    lease_id: str | None = None
    snapshot: dict[str, object] | None = None


class SearchContinuationRegistry:
    def __init__(
        self,
        *,
        max_items: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._clock = clock
        self._records: OrderedDict[str, _SearchContinuationRecord] = OrderedDict()
        self._lock = threading.RLock()

    def inspect(
        self,
        token: str,
        *,
        binding: tuple[object, ...],
        result_limit: int | None,
    ) -> SearchContinuationDecision:
        with self._lock:
            return self._decide_locked(
                token,
                binding=binding,
                result_limit=result_limit,
                claim=False,
            )

    def begin(
        self,
        token: str,
        *,
        binding: tuple[object, ...],
        result_limit: int | None,
    ) -> SearchContinuationDecision:
        with self._lock:
            return self._decide_locked(
                token,
                binding=binding,
                result_limit=result_limit,
                claim=True,
            )

    def remember(self, token: str, envelope: SearchContinuationEnvelope) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            if token in self._records:
                return False
            self._records[token] = _SearchContinuationRecord(
                envelope=envelope,
                status="ready",
                expires_at=now + self.ttl_seconds,
            )
            self._records.move_to_end(token)
            self._evict_locked(protected={token})
            return token in self._records

    def commit(
        self,
        token: str,
        *,
        lease_id: str,
        envelope: SearchContinuationEnvelope,
        result_limit: int,
        snapshot: dict[str, object],
        next_token: str | None,
        next_envelope: SearchContinuationEnvelope | None,
    ) -> bool:
        if (next_token is None) != (next_envelope is None):
            return False
        if not _replayable_stream_snapshot(snapshot):
            return False
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            current = self._records.get(token)
            if (
                current is None
                or current.status != "inflight"
                or current.lease_id != lease_id
                or current.envelope != envelope
                or current.target_limit != result_limit
            ):
                return False
            if next_token is not None and next_token in self._records:
                return False
            expires_at = now + self.ttl_seconds
            self._records[token] = _SearchContinuationRecord(
                envelope=envelope,
                status="completed",
                expires_at=expires_at,
                target_limit=result_limit,
                snapshot=copy_stream_snapshot(snapshot),
            )
            self._records.move_to_end(token)
            if next_token is not None and next_envelope is not None:
                self._records[next_token] = _SearchContinuationRecord(
                    envelope=next_envelope,
                    status="ready",
                    expires_at=expires_at,
                )
                self._records.move_to_end(next_token)
            protected = {token}
            if next_token is not None:
                protected.add(next_token)
            self._evict_locked(protected=protected)
            return token in self._records and (
                next_token is None or next_token in self._records
            )

    def abort(
        self,
        token: str,
        *,
        lease_id: str,
        envelope: SearchContinuationEnvelope,
    ) -> bool:
        now = self._clock()
        with self._lock:
            current = self._records.get(token)
            if (
                current is None
                or current.status != "inflight"
                or current.lease_id != lease_id
                or current.envelope != envelope
            ):
                self._purge_expired_locked(now)
                return False
            if current.expires_at <= now:
                self._records.pop(token, None)
                return False
            self._records[token] = _SearchContinuationRecord(
                envelope=envelope,
                status="ready",
                expires_at=current.expires_at,
            )
            self._records.move_to_end(token)
            return True

    def contains(self, token: str) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            return token in self._records

    def renew(self, token: str, *, lease_id: str) -> bool:
        now = self._clock()
        with self._lock:
            record = self._records.get(token)
            if (
                record is None
                or record.status != "inflight"
                or record.lease_id != lease_id
            ):
                self._purge_expired_locked(now)
                return False
            self._records[token] = replace(
                record,
                lease_expires_at=now + self.ttl_seconds,
            )
            self._records.move_to_end(token)
            return True

    def export_ready(self, token: str) -> SearchContinuationEnvelope | None:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(token)
            if record is None or record.status != "ready":
                return None
            self._records.move_to_end(token)
            return record.envelope

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def stats(self) -> dict[str, int]:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            return {
                "items": len(self._records),
                "max_items": self.max_items,
                "ttl_seconds": self.ttl_seconds,
                "weight": len(self._records),
                "max_weight": 0,
                "inflight": sum(
                    record.status == "inflight" for record in self._records.values()
                ),
                "completed": sum(
                    record.status == "completed" for record in self._records.values()
                ),
            }

    def _decide_locked(
        self,
        token: str,
        *,
        binding: tuple[object, ...],
        result_limit: int | None,
        claim: bool,
    ) -> SearchContinuationDecision:
        now = self._clock()
        self._purge_expired_locked(now)
        record = self._records.get(token)
        if record is None or not _continuation_request_matches(
            record.envelope,
            binding=binding,
            result_limit=result_limit,
        ):
            return SearchContinuationDecision("invalid")
        if record.status == "completed":
            if record.target_limit != result_limit or record.snapshot is None:
                return SearchContinuationDecision("invalid")
            self._records.move_to_end(token)
            return SearchContinuationDecision(
                "replay",
                envelope=record.envelope,
                snapshot=copy_stream_snapshot(record.snapshot),
            )
        if record.status == "inflight":
            if record.target_limit != result_limit:
                return SearchContinuationDecision("invalid")
            self._records.move_to_end(token)
            return SearchContinuationDecision("busy", envelope=record.envelope)
        if record.status != "ready" or not claim:
            self._records.move_to_end(token)
            return SearchContinuationDecision("ready", envelope=record.envelope)
        lease_id = secrets.token_urlsafe(18)
        self._records[token] = replace(
            record,
            status="inflight",
            target_limit=result_limit,
            lease_id=lease_id,
            lease_expires_at=now + self.ttl_seconds,
        )
        self._records.move_to_end(token)
        return SearchContinuationDecision(
            "claimed",
            envelope=record.envelope,
            lease_id=lease_id,
        )

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            token
            for token, record in self._records.items()
            if (
                record.lease_expires_at or record.expires_at
                if record.status == "inflight"
                else record.expires_at
            )
            <= now
        ]
        for token in expired:
            self._records.pop(token, None)

    def _evict_locked(self, *, protected: set[str]) -> None:
        while len(self._records) > self.max_items:
            evicted = next(
                (
                    token
                    for token, record in self._records.items()
                    if record.status != "inflight" and token not in protected
                ),
                None,
            )
            if evicted is None:
                return
            self._records.pop(evicted, None)


@dataclass(frozen=True)
class _MetadataSearchContinuationCapability:
    token: str
    mode: str
    expires_at: float


class MetadataSearchContinuationOverlay:
    def __init__(
        self,
        *,
        max_items: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._clock = clock
        self._records: OrderedDict[str, _MetadataSearchContinuationCapability] = (
            OrderedDict()
        )
        self._lock = threading.RLock()

    def remember(self, request_id: str, payload: dict[str, object]) -> None:
        token = str(payload.get("continuation_token") or "")
        mode = str(payload.get("continuation_mode") or "")
        if (
            payload.get("can_continue") is not True
            or mode not in {"retry", "extend"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token)
            or not state.SEARCH_CONTINUATIONS.contains(token)
        ):
            self.remove(request_id)
            return
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            self._records[request_id] = _MetadataSearchContinuationCapability(
                token=token,
                mode=mode,
                expires_at=now + self.ttl_seconds,
            )
            self._records.move_to_end(request_id)
            while len(self._records) > self.max_items:
                self._records.popitem(last=False)

    def apply(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            capability = self._records.get(request_id)
            if capability is not None:
                self._records.move_to_end(request_id)
        if capability is None or not state.SEARCH_CONTINUATIONS.contains(capability.token):
            self.remove(request_id)
            return {
                **payload,
                "can_continue": False,
                "continuation_token": None,
                "continuation_mode": None,
            }
        return {
            **payload,
            "can_continue": True,
            "continuation_token": capability.token,
            "continuation_mode": capability.mode,
        }

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._records.pop(request_id, None)

    def contains_valid(self, request_id: str) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            capability = self._records.get(request_id)
            if capability is None:
                return False
            if not state.SEARCH_CONTINUATIONS.contains(capability.token):
                self._records.pop(request_id, None)
                return False
            self._records.move_to_end(request_id)
            return True

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _purge_locked(self, now: float) -> None:
        expired = [
            request_id
            for request_id, capability in self._records.items()
            if capability.expires_at <= now
        ]
        for request_id in expired:
            self._records.pop(request_id, None)


class SearchJobRegistry:
    def __init__(
        self,
        *,
        pending_ttl_seconds: float = 30.0,
        max_pending_cancels: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jobs: dict[str, threading.Event] = {}
        self._pending_cancels: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.RLock()
        self._pending_ttl_seconds = max(1.0, float(pending_ttl_seconds))
        self._max_pending_cancels = max(1, int(max_pending_cancels))
        self._clock = clock

    def register(self, request_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._purge_pending_locked(self._clock())
            cancelled_before_register = request_id in self._pending_cancels
            previous = self._jobs.get(request_id)
            if previous:
                previous.set()
            self._jobs[request_id] = event
            if cancelled_before_register:
                event.set()
        return event

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            now = self._clock()
            self._purge_pending_locked(now)
            event = self._jobs.get(request_id)
            self._pending_cancels[request_id] = now + self._pending_ttl_seconds
            self._pending_cancels.move_to_end(request_id)
            while len(self._pending_cancels) > self._max_pending_cancels:
                self._pending_cancels.popitem(last=False)
            if event is not None:
                event.set()
                return True
        return False

    def unregister(self, request_id: str, event: threading.Event) -> None:
        with self._lock:
            if self._jobs.get(request_id) is event:
                self._jobs.pop(request_id, None)

    def _purge_pending_locked(self, now: float) -> None:
        expired = [
            request_id
            for request_id, expires_at in self._pending_cancels.items()
            if expires_at <= now
        ]
        for request_id in expired:
            self._pending_cancels.pop(request_id, None)


@dataclass(frozen=True)
class _DownloadDispositionSnapshot:
    candidates: tuple[dict[str, object], ...]
    status: str
    expires_at: float
    disposition: str | None = None
    result: dict[str, object] | None = None


class DownloadDispositionSnapshotRegistry:
    def __init__(
        self,
        *,
        max_items: int = 32,
        ttl_seconds: float = 10 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._records: OrderedDict[str, _DownloadDispositionSnapshot] = OrderedDict()
        self._lock = threading.RLock()

    def create(self, candidates: Sequence[Mapping[str, object]]) -> str:
        now = self._clock()
        copied = tuple(dict(candidate) for candidate in candidates)
        with self._lock:
            self._purge_expired_locked(now)
            token = secrets.token_hex(16)
            while token in self._records:
                token = secrets.token_hex(16)
            self._records[token] = _DownloadDispositionSnapshot(
                candidates=copied,
                status="ready",
                expires_at=now + self.ttl_seconds,
            )
            self._records.move_to_end(token)
            self._evict_locked(protected=token)
        return token

    def begin(
        self,
        token: object,
        *,
        disposition: str,
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object] | None]:
        clean_token = self._validate_token(token)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            current = self._records.get(clean_token)
            if current is None:
                raise DownloadReplacementConflictError(
                    "failed download snapshot expired; preview the operation again"
                )
            if current.disposition is not None and current.disposition != disposition:
                raise DownloadReplacementConflictError(
                    "failed download snapshot already has another disposition"
                )
            if current.status == "running":
                raise DownloadReplacementConflictError(
                    "failed download disposition is still running"
                )
            if current.status == "complete":
                return (), dict(current.result or {})
            running = replace(
                current,
                status="running",
                disposition=disposition,
                expires_at=now + self.ttl_seconds,
            )
            self._records[clean_token] = running
            self._records.move_to_end(clean_token)
            return tuple(dict(candidate) for candidate in running.candidates), None

    def complete(
        self,
        token: object,
        *,
        disposition: str,
        result: Mapping[str, object],
    ) -> None:
        clean_token = self._validate_token(token)
        now = self._clock()
        with self._lock:
            current = self._records.get(clean_token)
            if (
                current is None
                or current.status != "running"
                or current.disposition != disposition
            ):
                raise DownloadReplacementConflictError(
                    "failed download snapshot is no longer active"
                )
            self._records[clean_token] = replace(
                current,
                status="complete",
                expires_at=now + self.ttl_seconds,
                result=dict(result),
            )
            self._records.move_to_end(clean_token)

    def discard(self, token: object) -> None:
        try:
            clean_token = self._validate_token(token)
        except DownloadReplacementError:
            return
        with self._lock:
            self._records.pop(clean_token, None)

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            token
            for token, record in self._records.items()
            if record.status != "running" and record.expires_at <= now
        ]
        for token in expired:
            self._records.pop(token, None)

    def _evict_locked(self, *, protected: str) -> None:
        while len(self._records) > self.max_items:
            evicted = next(
                (
                    token
                    for token, record in self._records.items()
                    if token != protected and record.status != "running"
                ),
                None,
            )
            if evicted is None:
                return
            self._records.pop(evicted, None)

    @staticmethod
    def _validate_token(token: object) -> str:
        clean = str(token or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{32}", clean) is None:
            raise DownloadReplacementError("failed download snapshot token is invalid")
        return clean


def _continuation_request_matches(
    envelope: SearchContinuationEnvelope,
    *,
    binding: tuple[object, ...],
    result_limit: int | None,
) -> bool:
    return not (
        envelope.binding != binding
        or result_limit is None
        or result_limit < envelope.state.result_limit
        or (
            result_limit == envelope.state.result_limit
            and (
                not envelope.state.errors
                or len(envelope.works) >= envelope.state.result_limit
            )
        )
    )


def completed_stream_snapshot(value: object) -> bool:
    if not _replayable_stream_snapshot(value):
        return False
    assert isinstance(value, dict)
    done = value.get("done")
    return (
        isinstance(done, dict)
        and value.get("terminal", "done") == "done"
        and not done.get("errors")
        and not done.get("can_continue")
        and not _stream_snapshot_has_stage_errors(value)
    )


def _stream_snapshot_has_stage_errors(snapshot: dict[str, object]) -> bool:
    def work_has_errors(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        sources = value.get("sources")
        if not isinstance(sources, (list, tuple)):
            return False
        return any(
            isinstance(source, dict)
            and (
                source.get("parse_status") == "error"
                or any(
                    source.get(key)
                    for key in (
                        "error",
                        "details_error",
                        "image_error",
                        "magnet_error",
                    )
                )
            )
            for source in sources
        )

    base = snapshot.get("base")
    if isinstance(base, dict) and any(
        work_has_errors(work) for work in base.get("results") or []
    ):
        return True
    for source_event in snapshot.get("sources") or []:
        if isinstance(source_event, dict) and any(
            work_has_errors(work) for work in source_event.get("results") or []
        ):
            return True
    return any(
        isinstance(result_event, dict) and work_has_errors(result_event.get("result"))
        for result_event in snapshot.get("results") or []
    )


def _replayable_stream_snapshot(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("base"), dict)
        and isinstance(value.get("done"), dict)
    )


def copy_stream_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "sources": [
            dict(source)
            for source in snapshot.get("sources") or []
            if isinstance(source, dict)
        ],
        "base": (
            dict(snapshot["base"]) if isinstance(snapshot.get("base"), dict) else None
        ),
        "results": [
            dict(result)
            for result in snapshot.get("results") or []
            if isinstance(result, dict)
        ],
        "done": (
            dict(snapshot["done"]) if isinstance(snapshot.get("done"), dict) else None
        ),
        "terminal": (
            snapshot.get("terminal")
            if snapshot.get("terminal") in {"done", "cancelled"}
            else None
        ),
    }
