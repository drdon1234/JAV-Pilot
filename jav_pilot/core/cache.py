from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expires_at: float
    weight: int = 1


class TtlCache(Generic[T]):
    def __init__(
        self,
        *,
        max_items: int = 128,
        ttl_seconds: int = 600,
        max_weight: int | None = None,
        weigher: Callable[[T], int] | None = None,
    ) -> None:
        self.max_items = max(1, max_items)
        self.ttl_seconds = max(1, ttl_seconds)
        self.max_weight = max(1, int(max_weight)) if max_weight is not None else None
        self._weigher = weigher
        self._weight = 0
        self._items: OrderedDict[Hashable, CacheEntry[T]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: Hashable) -> T | None:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if not entry:
                return None
            if entry.expires_at <= now:
                self._remove(key)
                return None
            self._items.move_to_end(key)
            return entry.value

    def set(self, key: Hashable, value: T) -> None:
        weight = max(0, int(self._weigher(value))) if self._weigher else 1
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._purge_expired()
            self._remove(key)
            if self.max_weight is not None and weight > self.max_weight:
                return
            self._items[key] = CacheEntry(value=value, expires_at=expires_at, weight=weight)
            self._weight += weight
            self._items.move_to_end(key)
            while len(self._items) > self.max_items or (
                self.max_weight is not None and self._weight > self.max_weight
            ):
                oldest = next(iter(self._items))
                self._remove(oldest)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._weight = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._purge_expired()
            return {
                "items": len(self._items),
                "max_items": self.max_items,
                "ttl_seconds": self.ttl_seconds,
                "weight": self._weight,
                "max_weight": self.max_weight or 0,
            }

    def _remove(self, key: Hashable) -> None:
        entry = self._items.pop(key, None)
        if entry is not None:
            self._weight = max(0, self._weight - entry.weight)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [key for key, entry in self._items.items() if entry.expires_at <= now]
        for key in expired:
            self._remove(key)
