"""Resource search records, worker events, limits and status codes."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ...config.source_catalog import WEB_CATALOG
from ...web_download.variant import MissavVariant

__all__ = [
    "DEFAULT_RESOURCE_SEARCH_RESULTS",
    "MAX_RESOURCE_SEARCH_RESULTS",
    "RESOURCE_SEARCH_PROTOCOL_VERSION",
    "RESOURCE_SEARCH_SCHEMA_COMPONENT",
    "RESOURCE_SEARCH_SCHEMA_VERSION",
    "RESOURCE_SEARCH_SOURCE_IDS",
    "RESOURCE_SEARCH_STATUSES",
    "ResourceSearchDiscoverer",
    "ResourceSearchHeartbeatEvent",
    "ResourceSearchItem",
    "ResourceSearchPageEvent",
    "ResourceSearchStartedEvent",
    "ResourceSearchState",
    "ResourceSearchWork",
    "ResourceSearchWorkerEvent",
    "ResourceSearchWorkerResult",
]


RESOURCE_SEARCH_SCHEMA_COMPONENT = "resource_search"
RESOURCE_SEARCH_SCHEMA_VERSION = 6
RESOURCE_SEARCH_PROTOCOL_VERSION = 2
RESOURCE_SEARCH_STATUSES = (
    "queued",
    "running",
    "limit_reached",
    "completed",
    "failed",
    "cancelled",
)
RESOURCE_SEARCH_SOURCE_IDS = ("jable", "supjav", "missav", *WEB_CATALOG)
MAX_RESOURCE_SEARCH_RESULTS = 999
DEFAULT_RESOURCE_SEARCH_RESULTS = 100
MAX_RESOURCE_PAGE_SIZE = 100
MAX_PENDING_ITEMS = 999
MAX_WORKER_DELTA_ITEMS = 64
MAX_WORKER_LINE_BYTES = 8 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_WORKER_EVENTS = 2048
MAX_WORKER_INPUT_BYTES = 8 * 1024 * 1024
MAX_RESOURCE_TITLE_LENGTH = 512
STORE_RETRY_DELAYS = (0.05, 0.1, 0.2)
CLAIM_REQUEUE_DELAY = 0.5

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RANGE_QUERY_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")
RANGE_BOUND_RE = re.compile(r"^[0-9]{1,9}$")
ERROR_CODES = frozenset(
    {
        "challenge_active",
        "dependency_unavailable",
        "discovery_unavailable",
        "internal_failure",
        "interrupted",
        "invalid_instruction",
        "navigation_timeout",
        "not_found",
        "parse_drift",
        "protocol_failure",
        "rate_limited",
        "route_drift",
        "safety_rejected",
        "timeout",
        "transient_browser_failure",
        "upstream_unavailable",
    }
)
RETRYABLE_ERROR_CODES = frozenset(
    {
        "challenge_active",
        "interrupted",
        "navigation_timeout",
        "protocol_failure",
        "rate_limited",
        "timeout",
        "transient_browser_failure",
        "upstream_unavailable",
    }
)
TERMINAL_STATUSES = frozenset({"limit_reached", "completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class ResourceSearchItem:
    code: str
    available_variants: tuple[MissavVariant, ...]
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceSearchState:
    next_page: int | None
    pending: tuple[ResourceSearchItem, ...]
    cursor: int
    total_pages: int | None
    scanned_pages: int


@dataclass(frozen=True, slots=True)
class ResourceSearchWork:
    session_id: str
    source_id: str
    query: str
    result_limit: int
    suffix_width: int | None
    start: int | None
    end: int | None
    state: ResourceSearchState


@dataclass(frozen=True, slots=True)
class ResourceSearchStartedEvent:
    state: ResourceSearchState


@dataclass(frozen=True, slots=True)
class ResourceSearchHeartbeatEvent:
    state: ResourceSearchState


@dataclass(frozen=True, slots=True)
class ResourceSearchPageEvent:
    page: int
    fetched: bool
    items: tuple[ResourceSearchItem, ...]
    state: ResourceSearchState


ResourceSearchWorkerEvent = (
    ResourceSearchStartedEvent | ResourceSearchHeartbeatEvent | ResourceSearchPageEvent
)


@dataclass(frozen=True, slots=True)
class ResourceSearchWorkerResult:
    complete: bool
    state: ResourceSearchState


class ResourceSearchDiscoverer(Protocol):
    def __call__(
        self,
        work: ResourceSearchWork,
        *,
        on_event: Callable[[ResourceSearchWorkerEvent], None],
        cancel_event: threading.Event,
    ) -> ResourceSearchWorkerResult: ...
