"""Notification events: identity, field validation and factories."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Literal

from ..core.catalog_code import normalize_catalog_code
from .errors import NotificationError, NotificationStoreError

__all__ = [
    "NotificationEvent",
    "completed_event",
    "create_test_notification_event",
    "failed_event",
]


EVENT_TYPES = frozenset(
    {"completed", "failed", "disk_low", "site_failure", "test"}
)
TERMINAL_EVENT_TYPES = frozenset({"completed", "failed"})


MAX_EVENT_OCCURRENCES = 1_000_000


EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class NotificationEvent:
    event_id: str
    event_type: str
    source: str
    code: str | None
    status: str
    error_code: str | None
    occurrence_count: int
    occurred_at: float
    subject_kind: str | None = None
    subject_id: str | None = None
    stage: str | None = None

    def __post_init__(self) -> None:
        if not EVENT_ID_RE.fullmatch(self.event_id):
            raise NotificationError("notification event identity is invalid")
        if self.event_type not in EVENT_TYPES:
            raise NotificationError("notification event type is invalid")
        if safe_name(self.source, "notification source") != self.source:
            raise NotificationError("notification source is invalid")
        if safe_name(self.status, "notification status") != self.status:
            raise NotificationError("notification status is invalid")
        if self.code is not None and event_catalog_code(self.code) != self.code:
            raise NotificationError("notification catalog code is invalid")
        if self.error_code is not None:
            if (
                safe_name(self.error_code, "notification error code")
                != self.error_code
            ):
                raise NotificationError("notification error code is invalid")
        if self.subject_kind is not None:
            if safe_name(self.subject_kind, "notification subject kind") != self.subject_kind:
                raise NotificationError("notification subject kind is invalid")
        if self.subject_id is not None:
            if safe_name(self.subject_id, "notification subject identity") != self.subject_id:
                raise NotificationError("notification subject identity is invalid")
        if self.stage is not None:
            if safe_name(self.stage, "notification stage") != self.stage:
                raise NotificationError("notification stage is invalid")
        if self.event_type == "completed":
            if self.status != "completed" or self.error_code is not None:
                raise NotificationError("completed notification event is invalid")
        elif self.event_type == "failed":
            if self.status != "failed" or self.error_code is None:
                raise NotificationError("failed notification event is invalid")
        elif self.event_type == "disk_low":
            if self.status != "low" or self.error_code != "disk_low":
                raise NotificationError("disk notification event is invalid")
        elif self.event_type == "site_failure":
            if (
                self.status != "failed"
                or self.error_code is None
                or self.subject_kind != "site"
                or self.subject_id is None
                or self.stage is None
            ):
                raise NotificationError("site notification event is invalid")
        elif self.event_type == "test" and (
            self.source != "manual"
            or self.status != "test"
            or self.code is not None
            or self.error_code is not None
        ):
            raise NotificationError("test notification event is invalid")
        if (
            isinstance(self.occurrence_count, bool)
            or not isinstance(self.occurrence_count, int)
            or not 1 <= self.occurrence_count <= MAX_EVENT_OCCURRENCES
        ):
            raise NotificationError("notification occurrence count is invalid")
        if not math.isfinite(self.occurred_at) or self.occurred_at < 0:
            raise NotificationError("notification timestamp is invalid")

    @classmethod
    def terminal(
        cls,
        *,
        source: object,
        entity_id: object,
        status: Literal["completed", "failed"],
        code: object | None,
        error_code: object | None = None,
        occurred_at: float | None = None,
    ) -> "NotificationEvent":
        clean_source = safe_name(source, "notification source")
        clean_entity = _identity_part(entity_id, "notification entity")
        if status not in TERMINAL_EVENT_TYPES:
            raise NotificationError("terminal notification status is invalid")
        clean_code = event_catalog_code(code) if code is not None else None
        clean_error = (
            None
            if status == "completed"
            else safe_name(error_code, "notification error code")
        )
        return cls(
            event_id=_event_id("terminal", clean_source, clean_entity, status),
            event_type=status,
            source=clean_source,
            code=clean_code,
            status=status,
            error_code=clean_error,
            occurrence_count=1,
            occurred_at=event_timestamp(occurred_at),
        )

    @classmethod
    def disk_low(
        cls,
        *,
        volume_key: object,
        incident_id: object,
        occurrence_count: int = 1,
        occurred_at: float | None = None,
    ) -> "NotificationEvent":
        volume = _identity_part(volume_key, "disk volume")
        incident = _identity_part(incident_id, "disk incident")
        return cls(
            event_id=_event_id("disk_low", volume, incident),
            event_type="disk_low",
            source="disk",
            code=None,
            status="low",
            error_code="disk_low",
            occurrence_count=occurrence_count,
            occurred_at=event_timestamp(occurred_at),
        )

    @classmethod
    def site_failure(
        cls,
        *,
        site: object,
        stage: object,
        incident_id: object,
        consecutive_failures: int,
        threshold: int,
        error_code: object,
        code: object | None = None,
        occurred_at: float | None = None,
    ) -> "NotificationEvent | None":
        clean_site = safe_name(site, "site identity")
        clean_stage = safe_name(stage, "site stage")
        incident = _identity_part(incident_id, "site incident")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or not 1 <= threshold <= 10_000
        ):
            raise NotificationError("site failure threshold is invalid")
        if (
            isinstance(consecutive_failures, bool)
            or not isinstance(consecutive_failures, int)
            or not 0 <= consecutive_failures <= MAX_EVENT_OCCURRENCES
        ):
            raise NotificationError("site failure count is invalid")
        if consecutive_failures < threshold:
            return None
        return cls(
            event_id=_event_id(
                "site_failure",
                clean_site,
                clean_stage,
                incident,
            ),
            event_type="site_failure",
            source="site",
            code=event_catalog_code(code) if code is not None else None,
            status="failed",
            error_code=safe_name(error_code, "notification error code"),
            occurrence_count=consecutive_failures,
            occurred_at=event_timestamp(occurred_at),
            subject_kind="site",
            subject_id=clean_site,
            stage=clean_stage,
        )

    @classmethod
    def test(
        cls,
        *,
        request_id: object,
        occurred_at: float | None = None,
    ) -> "NotificationEvent":
        request = _identity_part(request_id, "test notification request")
        return cls(
            event_id=_event_id("test", request),
            event_type="test",
            source="manual",
            code=None,
            status="test",
            error_code=None,
            occurrence_count=1,
            occurred_at=event_timestamp(occurred_at),
        )

    def payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "type": self.event_type,
            "source": self.source,
            "code": self.code,
            "status": self.status,
            "error_code": self.error_code,
            "occurrence_count": self.occurrence_count,
            "occurred_at": self.occurred_at,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "stage": self.stage,
        }


def completed_event(
    *,
    source: object,
    entity_id: object,
    code: object,
    occurred_at: float | None = None,
) -> NotificationEvent:
    return NotificationEvent.terminal(
        source=source,
        entity_id=entity_id,
        status="completed",
        code=code,
        occurred_at=occurred_at,
    )


def failed_event(
    *,
    source: object,
    entity_id: object,
    code: object,
    error_code: object,
    occurred_at: float | None = None,
) -> NotificationEvent:
    return NotificationEvent.terminal(
        source=source,
        entity_id=entity_id,
        status="failed",
        code=code,
        error_code=error_code,
        occurred_at=occurred_at,
    )


def create_test_notification_event(
    *,
    request_id: object,
    occurred_at: float | None = None,
) -> NotificationEvent:
    return NotificationEvent.test(
        request_id=request_id,
        occurred_at=occurred_at,
    )


def safe_name(value: object, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not _SAFE_NAME_RE.fullmatch(clean):
        raise NotificationError(f"{label} is invalid")
    return clean


def _identity_part(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if (
        not clean
        or len(clean) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        raise NotificationError(f"{label} is invalid")
    return clean


def _event_id(*parts: str) -> str:
    raw = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    return f"evt_{hashlib.sha256(raw).hexdigest()}"


def event_catalog_code(value: object) -> str:
    normalized = normalize_catalog_code(value, max_length=64)
    if normalized is None:
        raise NotificationError("notification catalog code is invalid")
    return normalized[0]


def event_timestamp(value: float | None) -> float:
    try:
        clean = float(time.time() if value is None else value)
    except (TypeError, ValueError) as exc:
        raise NotificationError("notification timestamp is invalid") from exc
    if not math.isfinite(clean) or clean < 0:
        raise NotificationError("notification timestamp is invalid")
    return clean


def bounded_limit(value: object, *, maximum: int = 500) -> int:
    if isinstance(value, bool):
        raise NotificationStoreError("notification result limit is invalid")
    try:
        clean = int(value)
    except (TypeError, ValueError) as exc:
        raise NotificationStoreError("notification result limit is invalid") from exc
    if not 1 <= clean <= maximum:
        raise NotificationStoreError("notification result limit is invalid")
    return clean
