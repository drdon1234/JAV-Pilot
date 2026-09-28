from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .lifecycle import HistoryLifecycle
from ..config.runtime_config import (
    DEFAULT_HISTORY_RETENTION_HOUR,
    DEFAULT_HISTORY_RETENTION_TIMEZONE,
    history_retention_config,
    runtime_config_path,
    runtime_config_transaction,
)
from ..core.storage import atomic_write_text


STATE_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 64 * 1024
DEFAULT_AUTO_BATCH_SIZE = 4_096
DEFAULT_AUTO_MAX_BATCHES = 4
DEFAULT_AUTO_MAX_RECORDS = 10_000
MAX_AUTO_BATCH_SIZE = 4_096
MAX_AUTO_BATCHES = 8
MAX_AUTO_RECORDS = 10_000
DEFAULT_RETRY_SECONDS = 300.0
MAX_RETRY_SECONDS = 21_600.0
DEFAULT_POLL_SECONDS = 60.0

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_OUTCOMES = frozenset({"never", "running", "succeeded", "failed", "interrupted"})
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "last_started_day",
        "last_started_at",
        "last_succeeded_at",
        "last_error_code",
        "outcome",
        "failure_count",
        "next_retry_at",
        "last_removed_records",
        "last_batches",
    }
)


class HistoryRetentionSchedulerError(RuntimeError):
    pass


class HistoryRetentionStateError(HistoryRetentionSchedulerError):
    pass


class HistoryRetentionScheduleError(HistoryRetentionSchedulerError):
    pass


class _HistoryRetentionScheduleChanged(HistoryRetentionSchedulerError):
    def __init__(self, removed_records: int, batches: int) -> None:
        super().__init__("history retention schedule changed")
        self.removed_records = removed_records
        self.batches = batches


class _HistoryRetentionExecutionError(HistoryRetentionSchedulerError):
    def __init__(
        self,
        cause: BaseException,
        *,
        removed_records: int,
        batches: int,
    ) -> None:
        super().__init__("history retention execution failed")
        self.cause = cause
        self.removed_records = removed_records
        self.batches = batches


@dataclass(frozen=True)
class HistoryRetentionSchedule:
    web_days: int | None
    batch_days: int | None
    metadata_days: int | None
    auto_enabled: bool
    timezone: str
    hour: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> HistoryRetentionSchedule:
        if not isinstance(value, Mapping):
            raise HistoryRetentionScheduleError("history retention schedule is invalid")
        timezone = str(
            value.get("timezone", DEFAULT_HISTORY_RETENTION_TIMEZONE) or ""
        ).strip()
        if (
            not timezone
            or len(timezone) > 128
            or _TIMEZONE_RE.fullmatch(timezone) is None
            or any(part in {".", ".."} for part in timezone.split("/"))
        ):
            raise HistoryRetentionScheduleError(
                "history retention schedule timezone is invalid"
            )
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HistoryRetentionScheduleError(
                "history retention schedule timezone is invalid"
            ) from exc
        auto_enabled = value.get("auto_enabled", False)
        if not isinstance(auto_enabled, bool):
            raise HistoryRetentionScheduleError(
                "history retention schedule state is invalid"
            )
        hour = _bounded_integer(
            value.get("hour", DEFAULT_HISTORY_RETENTION_HOUR),
            "history retention schedule hour",
            0,
            23,
        )
        schedule = cls(
            web_days=_retention_days(value.get("web")),
            batch_days=_retention_days(value.get("batch")),
            metadata_days=_retention_days(value.get("metadata")),
            auto_enabled=auto_enabled,
            timezone=timezone,
            hour=hour,
        )
        if schedule.auto_enabled and not schedule.has_policy:
            raise HistoryRetentionScheduleError(
                "automatic history retention requires a retention period"
            )
        return schedule

    @property
    def has_policy(self) -> bool:
        return any(
            value is not None
            for value in (self.web_days, self.batch_days, self.metadata_days)
        )

    @property
    def active(self) -> bool:
        return self.auto_enabled and self.has_policy

    def policy(self) -> dict[str, int | None]:
        return {
            "web": self.web_days,
            "batch": self.batch_days,
            "metadata": self.metadata_days,
        }


@dataclass(frozen=True)
class HistoryRetentionState:
    last_started_day: str | None = None
    last_started_at: float | None = None
    last_succeeded_at: float | None = None
    last_error_code: str | None = None
    outcome: str = "never"
    failure_count: int = 0
    next_retry_at: float | None = None
    last_removed_records: int = 0
    last_batches: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_started_day": self.last_started_day,
            "last_started_at": self.last_started_at,
            "last_succeeded_at": self.last_succeeded_at,
            "last_error_code": self.last_error_code,
            "outcome": self.outcome,
            "failure_count": self.failure_count,
            "next_retry_at": self.next_retry_at,
            "last_removed_records": self.last_removed_records,
            "last_batches": self.last_batches,
        }


class HistoryRetentionStateStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> HistoryRetentionState:
        with self._lock:
            try:
                raw = _read_bounded_regular_file(self.path)
            except FileNotFoundError:
                return HistoryRetentionState()
            try:
                payload = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HistoryRetentionStateError(
                    "history retention state is invalid"
                ) from exc
            return _state_from_payload(payload)

    def save(self, state: HistoryRetentionState) -> None:
        payload = _state_from_payload(state.to_dict()).to_dict()
        raw = (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            raise HistoryRetentionStateError(
                "history retention state exceeds its size limit"
            )
        with self._lock:
            try:
                atomic_write_text(self.path, raw, restrict_permissions=True)
            except OSError as exc:
                raise HistoryRetentionStateError(
                    "history retention state is unavailable"
                ) from exc

    def recover_invalid(self) -> str | None:
        with self._lock:
            try:
                self.load()
            except HistoryRetentionStateError:
                pass
            else:
                return None
            try:
                details = self.path.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise HistoryRetentionStateError(
                    "history retention state is unavailable"
                ) from exc
            if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise HistoryRetentionStateError(
                    "unsafe history retention state requires manual recovery"
                )
            stamp = time.time_ns()
            quarantine: Path | None = None
            for attempt in range(100):
                candidate = self.path.with_name(
                    f"{self.path.name}.invalid.{stamp}.{attempt:02d}"
                )
                if not candidate.exists():
                    quarantine = candidate
                    break
            if quarantine is None:
                raise HistoryRetentionStateError(
                    "history retention state recovery path is unavailable"
                )
            try:
                self.path.replace(quarantine)
                self.save(HistoryRetentionState())
            except OSError as exc:
                raise HistoryRetentionStateError(
                    "history retention state could not be recovered"
                ) from exc
            return quarantine.name


class HistoryRetentionScheduler:
    def __init__(
        self,
        lifecycle_factory: Callable[[], HistoryLifecycle],
        *,
        config_provider: Callable[[], Mapping[str, object]] = (
            history_retention_config
        ),
        state_path: Path | str | None = None,
        batch_size: int = DEFAULT_AUTO_BATCH_SIZE,
        max_batches_per_run: int = DEFAULT_AUTO_MAX_BATCHES,
        max_records_per_run: int = DEFAULT_AUTO_MAX_RECORDS,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        max_retry_seconds: float = MAX_RETRY_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.time,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._lifecycle_factory = lifecycle_factory
        self._config_provider = config_provider
        self._state_store = HistoryRetentionStateStore(
            state_path if state_path is not None else history_retention_state_path()
        )
        self._batch_size = _bounded_integer(
            batch_size, "history retention batch size", 1, MAX_AUTO_BATCH_SIZE
        )
        self._max_batches = _bounded_integer(
            max_batches_per_run,
            "history retention batch count",
            1,
            MAX_AUTO_BATCHES,
        )
        self._max_records = _bounded_integer(
            max_records_per_run,
            "history retention record limit",
            self._batch_size,
            MAX_AUTO_RECORDS,
        )
        self._retry_seconds = _bounded_seconds(
            retry_seconds, "history retention retry delay", 1.0, MAX_RETRY_SECONDS
        )
        self._max_retry_seconds = _bounded_seconds(
            max_retry_seconds,
            "history retention maximum retry delay",
            self._retry_seconds,
            86_400.0,
        )
        self._poll_seconds = _bounded_seconds(
            poll_seconds, "history retention poll delay", 1.0, 3_600.0
        )
        self._clock = clock
        self._fault_hook = fault_hook or (lambda _point: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._runtime_error_code: str | None = None
        schedule = self._schedule()
        self._recover_interrupted_state(strict=schedule.active)

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def ready(self) -> bool:
        if not self.is_alive or self._runtime_error_code is not None:
            return False
        try:
            schedule = self._schedule()
            if schedule.active:
                self._state_store.load()
        except HistoryRetentionSchedulerError:
            return False
        return True

    @property
    def state_path(self) -> Path:
        return self._state_store.path

    def start(self) -> None:
        with self._thread_lock:
            if self.is_alive:
                return
            schedule = self._schedule()
            self._recover_interrupted_state(strict=schedule.active)
            self._stop.clear()
            self._wake.clear()
            self._runtime_error_code = None
            self._thread = threading.Thread(
                target=self._run,
                name="jav-history-retention",
                daemon=True,
            )
            self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout: float = 10.0) -> bool:
        clean_timeout = _bounded_seconds(
            timeout, "history retention shutdown timeout", 0.0, 120.0
        )
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=clean_timeout)
        return not self.is_alive

    def run_due(self, *, now: float | None = None) -> bool:
        current = _timestamp(self._clock() if now is None else now)
        schedule = self._schedule()
        if not schedule.active:
            return False
        state = self._state_store.load()
        if current < _next_due_timestamp(schedule, state, current):
            return False
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            state = self._state_store.load()
            if current < _next_due_timestamp(schedule, state, current):
                return False
            local_day = _local_day(schedule, current).isoformat()
            same_day_retry = (
                state.last_started_day == local_day and state.outcome == "failed"
            )
            running = replace(
                state,
                last_started_day=local_day,
                last_started_at=current,
                last_error_code=None,
                outcome="running",
                failure_count=state.failure_count if same_day_retry else 0,
                next_retry_at=None,
                last_removed_records=(
                    state.last_removed_records if same_day_retry else 0
                ),
                last_batches=state.last_batches if same_day_retry else 0,
            )
            self._state_store.save(running)
            try:
                removed, batches = self._execute_bounded(
                    schedule,
                    removed_records=running.last_removed_records,
                    batches=running.last_batches,
                )
            except _HistoryRetentionScheduleChanged as exc:
                self._state_store.save(
                    replace(
                        running,
                        last_succeeded_at=current,
                        outcome="succeeded",
                        failure_count=0,
                        next_retry_at=None,
                        last_removed_records=exc.removed_records,
                        last_batches=exc.batches,
                    )
                )
                return True
            except Exception as exc:  # noqa: BLE001 - only a fixed error code persists.
                failure = (
                    exc
                    if isinstance(exc, _HistoryRetentionExecutionError)
                    else _HistoryRetentionExecutionError(
                        exc,
                        removed_records=running.last_removed_records,
                        batches=running.last_batches,
                    )
                )
                failures = min(1_000, running.failure_count + 1)
                delay = min(
                    self._max_retry_seconds,
                    self._retry_seconds * (2 ** min(failures - 1, 10)),
                )
                self._state_store.save(
                    replace(
                        running,
                        last_error_code=_error_code(failure.cause),
                        outcome="failed",
                        failure_count=failures,
                        next_retry_at=current + delay,
                        last_removed_records=failure.removed_records,
                        last_batches=failure.batches,
                    )
                )
                return True
            self._state_store.save(
                replace(
                    running,
                    last_succeeded_at=current,
                    outcome="succeeded",
                    failure_count=0,
                    next_retry_at=None,
                    last_removed_records=removed,
                    last_batches=batches,
                )
            )
            return True
        finally:
            self._run_lock.release()

    def seconds_until_next(self, *, now: float | None = None) -> float:
        current = _timestamp(self._clock() if now is None else now)
        schedule = self._schedule()
        if not schedule.active:
            return self._poll_seconds
        due = _next_due_timestamp(schedule, self._state_store.load(), current)
        return min(self._poll_seconds, max(0.0, due - current))

    def status(self, *, now: float | None = None) -> dict[str, object]:
        current = _timestamp(self._clock() if now is None else now)
        schedule = self._schedule()
        state_error = False
        try:
            state = self._state_store.load()
        except HistoryRetentionStateError:
            state = HistoryRetentionState()
            state_error = True
        next_run_at = (
            _next_due_timestamp(schedule, state, current)
            if schedule.active and not state_error
            else None
        )
        return {
            "auto_enabled": schedule.auto_enabled,
            "active": schedule.active,
            "timezone": schedule.timezone,
            "hour": schedule.hour,
            "worker_alive": self.is_alive,
            "ready": bool(
                self.is_alive
                and self._runtime_error_code is None
                and (not schedule.active or not state_error)
            ),
            "last_started_at": state.last_started_at,
            "last_succeeded_at": state.last_succeeded_at,
            "last_error_code": (
                "state_unavailable" if state_error else state.last_error_code
            ),
            "outcome": "unavailable" if state_error else state.outcome,
            "next_retry_at": state.next_retry_at,
            "next_run_at": next_run_at,
            "last_removed_records": state.last_removed_records,
            "last_batches": state.last_batches,
            "batch_size": self._batch_size,
            "max_batches_per_run": self._max_batches,
            "max_records_per_run": self._max_records,
            "state_recovery_required": state_error,
        }

    def recover_state(self) -> str | None:
        if not self._run_lock.acquire(blocking=False):
            raise HistoryRetentionStateError("history retention state is busy")
        try:
            with runtime_config_transaction():
                if self._schedule().active:
                    raise HistoryRetentionScheduleError(
                        "automatic history retention must be disabled before state recovery"
                    )
                return self._state_store.recover_invalid()
        finally:
            self._run_lock.release()

    def _execute_bounded(
        self,
        schedule: HistoryRetentionSchedule,
        *,
        removed_records: int = 0,
        batches: int = 0,
    ) -> tuple[int, int]:
        try:
            lifecycle = self._lifecycle_factory()
        except Exception as exc:
            raise _HistoryRetentionExecutionError(
                exc,
                removed_records=removed_records,
                batches=batches,
            ) from exc
        removed_total = removed_records
        completed_batches = batches
        for _index in range(max(0, self._max_batches - completed_batches)):
            if self._stop.is_set():
                raise _HistoryRetentionExecutionError(
                    HistoryRetentionSchedulerError(
                        "history retention scheduler is stopping"
                    ),
                    removed_records=removed_total,
                    batches=completed_batches,
                )
            self._require_current_schedule(
                schedule,
                removed_records=removed_total,
                batches=completed_batches,
            )
            remaining_records = self._max_records - removed_total
            if remaining_records <= 0:
                break
            preview_limit = min(self._batch_size, remaining_records)
            try:
                preview = lifecycle.preview_retention(
                    schedule.policy(), limit=preview_limit
                )
            except Exception as exc:
                raise _HistoryRetentionExecutionError(
                    exc,
                    removed_records=removed_total,
                    batches=completed_batches,
                ) from exc
            selected = preview.get("selected")
            if not isinstance(selected, Mapping):
                raise _HistoryRetentionExecutionError(
                    HistoryRetentionSchedulerError(
                        "history retention preview is invalid"
                    ),
                    removed_records=removed_total,
                    batches=completed_batches,
                )
            selected_records = _bounded_integer(
                selected.get("records"),
                "history retention preview records",
                0,
                preview_limit,
            )
            if selected_records == 0:
                break
            if self._stop.is_set():
                raise _HistoryRetentionExecutionError(
                    HistoryRetentionSchedulerError(
                        "history retention scheduler is stopping"
                    ),
                    removed_records=removed_total,
                    batches=completed_batches,
                )
            token = preview.get("preview_token")
            if (
                not isinstance(token, str)
                or re.fullmatch(r"[0-9a-f]{64}", token) is None
            ):
                raise _HistoryRetentionExecutionError(
                    HistoryRetentionSchedulerError(
                        "history retention preview token is invalid"
                    ),
                    removed_records=removed_total,
                    batches=completed_batches,
                )
            try:
                with runtime_config_transaction():
                    self._require_current_schedule(
                        schedule,
                        removed_records=removed_total,
                        batches=completed_batches,
                    )
                    result = lifecycle.execute_cleanup(token)
            except _HistoryRetentionScheduleChanged:
                raise
            except Exception as exc:
                raise _HistoryRetentionExecutionError(
                    exc,
                    removed_records=removed_total,
                    batches=completed_batches + 1,
                ) from exc
            self._fault_hook("after_execute")
            removed = result.get("removed")
            if not isinstance(removed, Mapping):
                raise _HistoryRetentionExecutionError(
                    HistoryRetentionSchedulerError(
                        "history retention cleanup result is invalid"
                    ),
                    removed_records=removed_total,
                    batches=completed_batches + 1,
                )
            removed_records = _bounded_integer(
                removed.get("records"),
                "history retention removed records",
                0,
                selected_records,
            )
            removed_total += removed_records
            completed_batches += 1
            if removed_records == 0 or selected_records < preview_limit:
                break
        return removed_total, completed_batches

    def _require_current_schedule(
        self,
        expected: HistoryRetentionSchedule,
        *,
        removed_records: int,
        batches: int,
    ) -> None:
        if self._schedule() != expected:
            raise _HistoryRetentionScheduleChanged(removed_records, batches)

    def _recover_interrupted_state(self, *, strict: bool) -> None:
        try:
            state = self._state_store.load()
            if state.outcome == "running":
                self._state_store.save(
                    replace(
                        state,
                        last_error_code="interrupted",
                        outcome="interrupted",
                        next_retry_at=None,
                    )
                )
        except HistoryRetentionStateError:
            if strict:
                raise

    def _schedule(self) -> HistoryRetentionSchedule:
        try:
            return HistoryRetentionSchedule.from_mapping(self._config_provider())
        except HistoryRetentionScheduleError:
            raise
        except Exception as exc:
            raise HistoryRetentionScheduleError(
                "history retention schedule is unavailable"
            ) from exc

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
                self._runtime_error_code = None
                timeout = self.seconds_until_next()
            except Exception as exc:  # noqa: BLE001 - status exposes only a fixed code.
                self._runtime_error_code = _error_code(exc)
                timeout = self._poll_seconds
            self._wake.wait(timeout=timeout)
            self._wake.clear()


def history_retention_state_path() -> Path:
    return runtime_config_path().with_name("history_retention_state.json")


def validate_history_retention_state(path: Path | str | None = None) -> None:
    HistoryRetentionStateStore(
        history_retention_state_path() if path is None else path
    ).load()


def recover_history_retention_state(path: Path | str | None = None) -> str | None:
    return HistoryRetentionStateStore(
        history_retention_state_path() if path is None else path
    ).recover_invalid()


def _next_due_timestamp(
    schedule: HistoryRetentionSchedule,
    state: HistoryRetentionState,
    now: float,
) -> float:
    zone = ZoneInfo(schedule.timezone)
    today = datetime.fromtimestamp(now, zone).date()
    state_day = _optional_day(state.last_started_day)
    if (
        state_day is not None
        and state_day >= today
        and state.outcome
        in {
            "running",
            "succeeded",
            "interrupted",
        }
    ):
        return _scheduled_timestamp(state_day + timedelta(days=1), zone, schedule.hour)
    if (
        state_day == today
        and state.outcome == "failed"
        and state.next_retry_at is not None
    ):
        return state.next_retry_at
    scheduled = _scheduled_timestamp(today, zone, schedule.hour)
    return scheduled if now < scheduled else now


def _scheduled_timestamp(day: date, zone: ZoneInfo, hour: int) -> float:
    candidate = datetime(day.year, day.month, day.day, hour, tzinfo=zone, fold=0)
    return candidate.timestamp()


def _local_day(schedule: HistoryRetentionSchedule, timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, ZoneInfo(schedule.timezone)).date()


def _state_from_payload(value: object) -> HistoryRetentionState:
    if not isinstance(value, dict):
        raise HistoryRetentionStateError("history retention state is invalid")
    version = value.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HistoryRetentionStateError(
            "history retention state schema version is invalid"
        )
    if version > STATE_SCHEMA_VERSION:
        raise HistoryRetentionStateError(
            "history retention state is newer than this application supports"
        )
    if set(value) != _STATE_KEYS:
        raise HistoryRetentionStateError("history retention state is invalid")
    started_day = value.get("last_started_day")
    if started_day is not None:
        started_day = _day(started_day).isoformat()
    error_code = value.get("last_error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or _ERROR_CODE_RE.fullmatch(error_code) is None
    ):
        raise HistoryRetentionStateError("history retention state error is invalid")
    outcome = value.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        raise HistoryRetentionStateError("history retention state outcome is invalid")
    try:
        state = HistoryRetentionState(
            last_started_day=started_day,
            last_started_at=_optional_timestamp(value.get("last_started_at")),
            last_succeeded_at=_optional_timestamp(value.get("last_succeeded_at")),
            last_error_code=error_code,
            outcome=outcome,
            failure_count=_bounded_integer(
                value.get("failure_count"),
                "history retention failure count",
                0,
                1_000,
            ),
            next_retry_at=_optional_timestamp(value.get("next_retry_at")),
            last_removed_records=_bounded_integer(
                value.get("last_removed_records"),
                "history retention removed records",
                0,
                MAX_AUTO_BATCH_SIZE * MAX_AUTO_BATCHES,
            ),
            last_batches=_bounded_integer(
                value.get("last_batches"),
                "history retention batch count",
                0,
                MAX_AUTO_BATCHES,
            ),
        )
    except HistoryRetentionScheduleError as exc:
        raise HistoryRetentionStateError("history retention state is invalid") from exc
    if state.outcome == "never":
        if (
            any(
                value is not None
                for value in (
                    state.last_started_day,
                    state.last_started_at,
                    state.last_succeeded_at,
                    state.last_error_code,
                    state.next_retry_at,
                )
            )
            or state.failure_count
            or state.last_removed_records
            or state.last_batches
        ):
            raise HistoryRetentionStateError("history retention state is inconsistent")
    elif state.last_started_day is None or state.last_started_at is None:
        raise HistoryRetentionStateError("history retention state is inconsistent")
    if state.outcome == "running" and (
        state.last_error_code is not None or state.next_retry_at is not None
    ):
        raise HistoryRetentionStateError("history retention state is inconsistent")
    if state.outcome == "interrupted" and (
        state.last_error_code != "interrupted" or state.next_retry_at is not None
    ):
        raise HistoryRetentionStateError("history retention state is inconsistent")
    if state.outcome == "succeeded" and (
        state.last_succeeded_at is None
        or state.last_error_code is not None
        or state.next_retry_at is not None
        or state.failure_count != 0
    ):
        raise HistoryRetentionStateError("history retention state is inconsistent")
    if state.outcome == "failed" and (
        state.last_error_code is None
        or state.next_retry_at is None
        or state.failure_count < 1
    ):
        raise HistoryRetentionStateError("history retention state is inconsistent")
    return state


def _read_bounded_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise HistoryRetentionStateError(
            "history retention state is unavailable"
        ) from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_STATE_BYTES:
            raise HistoryRetentionStateError("history retention state is unsafe")
        chunks: list[bytes] = []
        remaining = MAX_STATE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_STATE_BYTES:
            raise HistoryRetentionStateError(
                "history retention state exceeds its size limit"
            )
        return raw
    finally:
        os.close(descriptor)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, HistoryRetentionStateError):
        return "state_unavailable"
    if isinstance(exc, HistoryRetentionScheduleError):
        return "configuration_invalid"
    name = type(exc).__name__.casefold()
    if "conflict" in name or "locked" in str(exc).casefold():
        return "state_conflict"
    if isinstance(exc, OSError):
        return "storage_unavailable"
    if isinstance(exc, HistoryRetentionSchedulerError):
        return "scheduler_unavailable"
    return "cleanup_failed"


def _retention_days(value: object) -> int | None:
    if value is None or value is False or value == 0 or value == "0":
        return None
    return _bounded_integer(value, "history retention days", 1, 36_500)


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise HistoryRetentionScheduleError(f"{label} is invalid")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryRetentionScheduleError(f"{label} is invalid") from exc
    if parsed < minimum or parsed > maximum or str(value).strip() != str(parsed):
        raise HistoryRetentionScheduleError(f"{label} is invalid")
    return parsed


def _bounded_seconds(
    value: object, label: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} is invalid")
    return parsed


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryRetentionScheduleError("history retention clock is invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise HistoryRetentionScheduleError("history retention clock is invalid")
    return parsed


def _optional_timestamp(value: object) -> float | None:
    return None if value is None else _timestamp(value)


def _day(value: object) -> date:
    if not isinstance(value, str) or _DAY_RE.fullmatch(value) is None:
        raise HistoryRetentionStateError("history retention state day is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoryRetentionStateError(
            "history retention state day is invalid"
        ) from exc
    if parsed.isoformat() != value:
        raise HistoryRetentionStateError("history retention state day is invalid")
    return parsed


def _optional_day(value: str | None) -> date | None:
    return None if value is None else _day(value)


__all__ = [
    "DEFAULT_AUTO_BATCH_SIZE",
    "DEFAULT_AUTO_MAX_BATCHES",
    "DEFAULT_AUTO_MAX_RECORDS",
    "HistoryRetentionSchedule",
    "HistoryRetentionScheduler",
    "HistoryRetentionSchedulerError",
    "HistoryRetentionState",
    "HistoryRetentionStateError",
    "HistoryRetentionStateStore",
    "STATE_SCHEMA_VERSION",
    "history_retention_state_path",
    "recover_history_retention_state",
    "validate_history_retention_state",
]
