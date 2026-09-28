"""The web download job model: statuses, archive states, limits and field helpers."""

from __future__ import annotations

import math
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Sequence

from ..core.catalog_code import normalize_catalog_code
from .quality import validate_selected_height
from .variant import normalize_web_download_variant
from .errors import WebDownloadError, WebDownloadRunnerError

DEFAULT_MIN_FREE_BYTES = 5 * 1024**3
DEFAULT_INITIAL_MEDIA_BYTES = 512 * 1024**2
DEFAULT_MAX_FILE_BYTES = 64 * 1024**3
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 45.0
MAX_CONCURRENCY = 8
MAX_QUEUE_PRIORITY = 1000
MAX_EVENT_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_PENDING_WORKER_EVENTS = 16
MAX_CHECKPOINT_ENTRIES = 100_000
CHECKPOINT_METADATA_ALLOWANCE_BYTES = 64 * 1024**2
PROVIDER = "missav"
AUTO_PROVIDER = "auto"
WEB_DOWNLOAD_PROVIDERS = frozenset({AUTO_PROVIDER, PROVIDER, "jable", "supjav"})
WEB_DOWNLOAD_SCHEMA_COMPONENT = "web_downloads"
WEB_DOWNLOAD_SCHEMA_VERSION = 9
QUALITY_STRATEGIES = ("legacy", "selected", "highest")
DISK_LOW_NOTIFICATION_INTERVAL_SECONDS = 60 * 60
DISK_LOW_VOLUME_KEYS = frozenset({"shared", "staging", "library"})
LONG_RETRY_DELAYS_SECONDS = (120.0, 600.0, 1800.0)
LONG_RETRY_JITTER_RATIO = 0.2
RETRY_PROGRESS_BYTES = 64 * 1024**2
RETRY_PROGRESS_FRAGMENTS = 10
DEFAULT_DOWNLOAD_STALL_SECONDS = 90.0

QUEUED_STATUS = "queued"
RETRY_WAIT_STATUS = "retry_wait"
PENDING_STATUSES = (QUEUED_STATUS, RETRY_WAIT_STATUS)
WORKER_STATUSES = (
    "locating",
    "validating",
    "downloading",
    "verifying",
    "archiving",
)
PAUSABLE_WORKER_STATUSES = ("locating", "validating", "downloading")
PAUSE_STATUSES = ("pausing", "paused")
ACTIVE_STATUSES = (
    QUEUED_STATUS,
    RETRY_WAIT_STATUS,
    *WORKER_STATUSES,
    "cancelling",
    *PAUSE_STATUSES,
)
SLOT_STATUSES = (*WORKER_STATUSES, "cancelling", "pausing")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ALL_STATUSES = (*ACTIVE_STATUSES, *TERMINAL_STATUSES)
ARCHIVE_AVAILABLE = "available"
ARCHIVE_MISSING = "missing"
ARCHIVE_UNKNOWN = "unknown"
ARCHIVE_REPLACED = "replaced"

PHASE_ORDER = {status: index for index, status in enumerate(WORKER_STATUSES)}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MISSAV_BROWSER_PROFILE_RE = re.compile(r"^\.missav-browser-[A-Za-z0-9_-]{6,64}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>'\"]+")
_SECRET_RE = re.compile(
    r"(?i)\b(token|signature|sig|key|authorization|cookie|referer)\s*[:=]\s*[^\s,;]+"
)
WEB_SOURCE_FAILURE_CODES = frozenset(
    {
        "all_providers_unavailable",
        "cancelled",
        "challenge_active",
        "configuration",
        "host_policy",
        "manifest_invalid",
        "not_found",
        "redirect_policy",
        "response_invalid",
        "upstream_unavailable",
    }
)
CAPTURE_FAILURE_CODES = frozenset(
    {
        "transient_browser_failure",
        *{f"web_source_{code}" for code in WEB_SOURCE_FAILURE_CODES},
        *{
            f"missav_{code}"
            for code in (
                "challenge_active",
                "discovery_unavailable",
                "navigation_timeout",
                "not_found",
                "rate_limited",
                "transient",
                "upstream_unavailable",
            )
        },
    }
)
AUTO_STORAGE_RESERVATIONS = object()
UNSET = object()


def normalize_web_download_code(raw_code: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(raw_code, max_length=40)
    if normalized is None:
        raise WebDownloadError("invalid catalog code")
    return normalized


def validate_requested_height(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebDownloadError("requested video height is invalid")
    try:
        return validate_selected_height(value)
    except ValueError as exc:
        raise WebDownloadError("requested video height is invalid") from exc


def validate_web_download_variant(value: object) -> str:
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise WebDownloadError("web download variant is invalid") from exc


def validate_relative_output_path(value: object) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 512
        or "\\" in raw
        or "://" in raw
        or Path(raw).anchor
        or any(ord(character) < 32 for character in raw)
    ):
        raise WebDownloadRunnerError("download worker output path is invalid")
    path = PurePosixPath(raw)
    if (
        raw in {".", ".."}
        or not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise WebDownloadRunnerError("download worker output path is invalid")
    if posixpath.normpath(raw) != raw:
        raise WebDownloadRunnerError("download worker output path is invalid")
    return raw


def sql_slots(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


def replacement_revision_timestamp(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise WebDownloadError(f"{field} is invalid")
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebDownloadError(f"{field} is invalid") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise WebDownloadError(f"{field} is invalid")
    return timestamp


def redact_worker_error(value: object) -> str:
    text = str(value or "Download worker failed")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    text = _URL_RE.sub("[redacted-url]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return (text or "Download worker failed")[:500]
