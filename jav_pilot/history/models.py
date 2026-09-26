"""History limits, retention policy, export envelope and cleanup records."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from ..media_metadata.store import STATUSES as METADATA_STATUSES
from ..web_download.batches.schema import BATCH_STATUSES
from ..web_download.jobs import (
    ALL_STATUSES as WEB_STATUSES,
    TERMINAL_STATUSES as WEB_TERMINAL_STATUSES,
)
from .errors import HistoryLifecycleValidationError
from .fields import retention_days

__all__ = [
    "DEFAULT_PREVIEW_SECONDS",
    "HISTORY_EXPORT_REVISION",
    "HISTORY_EXPORT_SCHEMA",
    "HistoryExport",
    "MAX_CLEANUP_RECORDS",
    "MAX_EXPORT_RECORDS",
    "RetentionPolicy",
]


HISTORY_EXPORT_SCHEMA = "jav-pilot.task-history"
HISTORY_EXPORT_REVISION = 1
DEFAULT_PREVIEW_SECONDS = 10 * 60.0
MAX_PREVIEWS = 128
MAX_CLEANUP_RECORDS = 10_000
MAX_EXPORT_RECORDS = 100_000
MAX_SKIP_DETAILS = 200
HISTORY_CLEANUP_OPERATION_REVISION = 1
MAX_RECOVERY_FACTS = 100_000
MAX_RECOVERY_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_COMPLETED_OPERATIONS = 256

TASK_TYPES = ("web", "batch", "metadata")
BATCH_ACTIVE_STATUSES = frozenset({"queued", "discovering", "ready"})
METADATA_ACTIVE_STATUSES = frozenset({"waiting_media", "queued", "running", "retry"})
TASK_STATUS_VALUES = {
    "web": frozenset(WEB_STATUSES),
    "batch": frozenset(BATCH_STATUSES),
    "metadata": frozenset(METADATA_STATUSES),
}
TASK_TERMINAL_VALUES = {
    "web": frozenset(WEB_TERMINAL_STATUSES),
    "batch": frozenset(BATCH_STATUSES) - BATCH_ACTIVE_STATUSES,
    "metadata": frozenset(METADATA_STATUSES) - METADATA_ACTIVE_STATUSES,
}


HEX_ID_RE = re.compile(r"^[a-f0-9]{32}$")
GIT_REVISION_RE = re.compile(r"^[a-f0-9]{40}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")

SAFE_FACT_NAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")
SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookies?|headers?|manifest(?:_url)?|password|referer|"
    r"secrets?|sessions?|tokens?|urls?)",
    flags=re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(
    r"(?:https?|wss?|ftp)://|://|www\.|"
    r"\b(?:authorization|cookies?|headers?|manifest(?:_url)?|password|referer|"
    r"secret|session|token|url)\s*[:=]|"
    r"[?&](?:authorization|cookie|password|secret|session|token)\s*=",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    web_days: int | None = None
    batch_days: int | None = None
    metadata_days: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> RetentionPolicy:
        raw = {} if value is None else value
        if not isinstance(raw, Mapping):
            raise HistoryLifecycleValidationError("retention policy is invalid")
        unknown = set(raw) - set(TASK_TYPES)
        if unknown:
            raise HistoryLifecycleValidationError("retention task type is invalid")
        return cls(
            web_days=retention_days(raw.get("web")),
            batch_days=retention_days(raw.get("batch")),
            metadata_days=retention_days(raw.get("metadata")),
        )

    def enabled(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for task_type, days in (
            ("web", self.web_days),
            ("batch", self.batch_days),
            ("metadata", self.metadata_days),
        ):
            if days is not None:
                result[task_type] = days
        return result


@dataclass(frozen=True, slots=True)
class HistoryExport:
    body: bytes
    content_type: str
    extension: str
    checksum: str
    record_count: int


@dataclass(frozen=True, slots=True)
class Criteria:
    task_types: tuple[str, ...]
    statuses: tuple[tuple[str, tuple[str, ...]], ...]
    created_after: float | None
    created_before: float | None
    updated_after: float | None
    updated_before: float | None
    code_query: str | None
    limit: int
    retention_cutoffs: tuple[tuple[str, float], ...] = ()

    def statuses_for(self, task_type: str) -> frozenset[str] | None:
        for key, values in self.statuses:
            if key == task_type:
                return frozenset(values)
        return None

    def updated_before_for(self, task_type: str) -> float | None:
        for key, value in self.retention_cutoffs:
            if key == task_type:
                return value
        return self.updated_before

    def public(self) -> dict[str, object]:
        return {
            "task_types": list(self.task_types),
            "statuses": {key: list(values) for key, values in self.statuses},
            "created_after": self.created_after,
            "created_before": self.created_before,
            "updated_after": self.updated_after,
            "updated_before": self.updated_before,
            "code": self.code_query,
            "limit": self.limit,
            "retention_cutoffs": {key: value for key, value in self.retention_cutoffs},
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    task_type: str
    identity: str
    fingerprint: str
    status: str
    code: str | None
    variant: str | None
    record_count: int
    estimated_bytes: int

    def public(self) -> dict[str, object]:
        return {
            "task_type": self.task_type,
            "id": self.identity,
            "status": self.status,
            "code": self.code,
            "variant": self.variant,
            "record_count": self.record_count,
            "estimated_bytes": self.estimated_bytes,
        }


@dataclass(frozen=True, slots=True)
class Preview:
    token: str
    created_at: float
    expires_at: float
    criteria: Criteria
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    operation_id: str
    operation_digest: str
    candidates: tuple[Candidate, ...]
    fact_digests: tuple[tuple[int, str, str], ...]


class Skipped:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.details: list[dict[str, str]] = []

    def add(self, task_type: str, identity: str, reason: str) -> None:
        self.counts[reason] += 1
        if len(self.details) < MAX_SKIP_DETAILS:
            self.details.append(
                {"task_type": task_type, "id": identity, "reason": reason}
            )

    def public(self) -> dict[str, object]:
        return {
            "counts": dict(sorted(self.counts.items())),
            "items": list(self.details),
            "details_truncated": sum(self.counts.values()) > len(self.details),
        }
