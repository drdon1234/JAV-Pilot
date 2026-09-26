from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass


@dataclass
class _AttemptBucket:
    failures: int
    in_flight: int
    first_failure_at: float
    last_failure_at: float
    blocked_until: float


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: float = 15 * 60,
        base_block_seconds: float = 30,
        max_block_seconds: float = 15 * 60,
        max_entries: int = 4096,
        clock: object = time.monotonic,
    ) -> None:
        if not 2 <= max_failures <= 20:
            raise ValueError("max failures must be between 2 and 20")
        if window_seconds <= 0 or base_block_seconds <= 0 or max_block_seconds <= 0:
            raise ValueError("rate limit durations must be positive")
        if not 128 <= max_entries <= 65536:
            raise ValueError("rate limit entry bound is invalid")
        self._max_failures = int(max_failures)
        self._window_seconds = float(window_seconds)
        self._base_block_seconds = float(base_block_seconds)
        self._max_block_seconds = float(max_block_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], _AttemptBucket] = {}

    def retry_after(self, source: object, username: object) -> int:
        key = self._key(source, username)
        now = self._now()
        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None or bucket.blocked_until <= now:
                return 0
            return max(1, math.ceil(bucket.blocked_until - now))

    def begin_attempt(self, source: object, username: object) -> int:
        """Atomically reserve one password verification slot for an identity."""

        key = self._key(source, username)
        now = self._now()
        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None or (
                bucket.in_flight == 0
                and now - bucket.first_failure_at >= self._window_seconds
            ):
                bucket = _AttemptBucket(0, 0, now, now, 0.0)
            if bucket.blocked_until > now:
                return max(1, math.ceil(bucket.blocked_until - now))
            if bucket.failures >= self._max_failures:
                if bucket.in_flight == 0:
                    bucket.in_flight = 1
                    bucket.last_failure_at = now
                    self._buckets[key] = bucket
                    return 0
                bucket.blocked_until = now + self._base_block_seconds
                bucket.last_failure_at = now
                self._buckets[key] = bucket
                return max(1, math.ceil(self._base_block_seconds))
            if bucket.failures + bucket.in_flight >= self._max_failures:
                exponent = max(
                    0,
                    bucket.failures + bucket.in_flight - self._max_failures,
                )
                delay = min(
                    self._max_block_seconds,
                    self._base_block_seconds * (2**exponent),
                )
                bucket.blocked_until = now + delay
                bucket.last_failure_at = now
                self._buckets[key] = bucket
                self._trim(now)
                return max(1, math.ceil(delay))
            bucket.in_flight += 1
            bucket.last_failure_at = now
            self._buckets[key] = bucket
            self._trim(now)
            return 0

    def finish_attempt(
        self,
        source: object,
        username: object,
        *,
        success: bool,
    ) -> int:
        """Release a reserved verification and record its authenticated outcome."""

        key = self._key(source, username)
        now = self._now()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.in_flight <= 0:
                raise RuntimeError("login attempt reservation is missing")
            bucket.in_flight -= 1
            bucket.last_failure_at = now
            if success:
                bucket.failures = 0
                bucket.first_failure_at = now
                bucket.blocked_until = 0.0
                if bucket.in_flight == 0:
                    self._buckets.pop(key, None)
                else:
                    self._buckets[key] = bucket
                return 0
            retry_after = self._record_failure_locked(bucket, now)
            self._buckets[key] = bucket
            self._trim(now)
            return retry_after

    def record_failure(self, source: object, username: object) -> int:
        key = self._key(source, username)
        now = self._now()
        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None or (
                bucket.in_flight == 0
                and now - bucket.first_failure_at >= self._window_seconds
            ):
                bucket = _AttemptBucket(0, 0, now, now, 0.0)
            retry_after = self._record_failure_locked(bucket, now)
            self._buckets[key] = bucket
            self._trim(now)
            return retry_after

    def record_success(self, source: object, username: object) -> None:
        key = self._key(source, username)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.in_flight == 0:
                self._buckets.pop(key, None)
                return
            bucket.failures = 0
            bucket.first_failure_at = self._now()
            bucket.blocked_until = 0.0
            self._buckets[key] = bucket

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def _key(self, source: object, username: object) -> tuple[str, str]:
        clean_source = str(source or "unknown").strip().lower()[:128] or "unknown"
        clean_username = str(username or "").strip().casefold()[:80]
        return clean_source, clean_username

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds - self._max_block_seconds
        expired = [
            key
            for key, bucket in self._buckets.items()
            if (
                bucket.in_flight == 0
                and bucket.last_failure_at < cutoff
                and bucket.blocked_until <= now
            )
        ]
        for key in expired:
            self._buckets.pop(key, None)

    def _trim(self, now: float) -> None:
        overflow = len(self._buckets) - self._max_entries
        if overflow <= 0:
            return
        # Never evict a live block: otherwise an attacker can fill the bounded
        # map with random usernames and remove their own active ban.
        evictable = [
            key
            for key, bucket in self._buckets.items()
            if bucket.in_flight == 0 and bucket.blocked_until <= now
        ]
        oldest = sorted(
            evictable,
            key=lambda key: self._buckets[key].last_failure_at,
        )[:overflow]
        for key in oldest:
            self._buckets.pop(key, None)

    def _record_failure_locked(self, bucket: _AttemptBucket, now: float) -> int:
        bucket.failures += 1
        bucket.last_failure_at = now
        if bucket.failures >= self._max_failures:
            exponent = bucket.failures - self._max_failures
            delay = min(
                self._max_block_seconds,
                self._base_block_seconds * (2**exponent),
            )
            bucket.blocked_until = max(bucket.blocked_until, now + delay)
        return (
            max(1, math.ceil(bucket.blocked_until - now))
            if bucket.blocked_until > now
            else 0
        )
