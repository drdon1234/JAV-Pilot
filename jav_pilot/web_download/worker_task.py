"""The task a web download worker receives, and the events it emits."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..missav.errors import MissavError
from ..missav.models import ManifestRequest
from ..missav.site import QualityStrategy, validate_quality_strategy
from ..net.network_guard import PublicHostResolver
from .bandwidth import (
    BandwidthBrokerError,
    BandwidthClientConfig,
    bandwidth_config_from_payload,
)
from .media import SafeMediaError, media_provider_scope, require_allowed_media_url
from .policy import LEGACY_EXISTING_POLICY, ExistingPolicy, validate_existing_policy
from .quality import QualityHeightError, validate_quality_height
from .variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
)
from .worker_common import (
    JOB_ID_RE,
    PROGRESS_MAX_INTERVAL_SECONDS,
    PROGRESS_MIN_DELTA_PERCENT,
    PROGRESS_MIN_INTERVAL_SECONDS,
)
from .worker_errors import WebDownloadWorkerError
from .worker_files import (
    absolute_file_path,
    absolute_root,
    optional_relative_output_path,
    safe_display_code,
)

@dataclass(frozen=True)
class WorkerTask:
    job_id: str
    code: str
    variant: MissavVariant
    incoming_root: Path
    library_root: Path
    capture_timeout_seconds: float
    min_free_bytes: int
    max_file_bytes: int
    requested_height: int | None
    quality_strategy: QualityStrategy
    resume_selected_height: int | None
    finalize_lock_path: Path | None
    provider: str = "missav"
    existing_policy: ExistingPolicy = LEGACY_EXISTING_POLICY
    incumbent_output_path: str | None = None
    bandwidth: BandwidthClientConfig | None = None
    manifest: ManifestRequest | None = None
    media_kind: str = "hls"

    @classmethod
    def from_payload(cls, payload: object) -> "WorkerTask":
        if not isinstance(payload, dict):
            raise WebDownloadWorkerError("worker task must be an object")

        job_id = str(payload.get("job_id") or "").strip()
        if not JOB_ID_RE.fullmatch(job_id):
            raise WebDownloadWorkerError("invalid worker job_id")

        code = safe_display_code(payload.get("code"))
        provider = (
            str(payload.get("manifest_provider") or payload.get("provider") or "missav")
            .strip()
            .lower()
        )
        if provider == "auto" and payload.get("manifest_url") in (None, ""):
            provider = "missav"
        if provider not in {"missav", "jable", "supjav", "javnoni", "kissjav"}:
            raise WebDownloadWorkerError("invalid worker provider")
        if provider in {"javnoni", "kissjav"} and payload.get("manifest_url") in (None, ""):
            raise WebDownloadWorkerError("catalog providers require a verified media manifest")
        try:
            variant = normalize_web_download_variant(
                payload.get("variant", DEFAULT_WEB_DOWNLOAD_VARIANT)
            )
        except ValueError as exc:
            raise WebDownloadWorkerError("invalid worker variant") from exc
        incoming_root = absolute_root(
            payload.get("incoming_root")
            or payload.get("staging_root")
            or os.environ.get("JAV_PILOT_WEB_DOWNLOAD_STAGING_PATH")
            or os.environ.get("JAV_PILOT_WEB_DOWNLOAD_INCOMING_PATH")
        )
        library_root = absolute_root(
            payload.get("library_root")
            or payload.get("archive_root")
            or os.environ.get("JAV_PILOT_WEB_DOWNLOAD_LIBRARY_PATH")
        )
        raw_timeout = payload.get("capture_timeout_seconds")
        if raw_timeout in (None, ""):
            raw_timeout = os.environ.get(
                "JAV_PILOT_WEB_DOWNLOAD_CAPTURE_TIMEOUT_SECONDS", "45"
            )
        try:
            capture_timeout = float(raw_timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadWorkerError("invalid capture timeout") from exc
        capture_timeout = max(15.0, min(capture_timeout, 120.0))
        min_free_bytes = _bounded_positive_int(
            payload.get("min_free_bytes"),
            default=5 * 1024 * 1024 * 1024,
            maximum=1024 * 1024 * 1024 * 1024,
        )
        max_file_bytes = _bounded_positive_int(
            payload.get("max_file_bytes"),
            default=50 * 1024 * 1024 * 1024,
            maximum=1024 * 1024 * 1024 * 1024,
        )
        raw_requested_height = payload.get("requested_height")
        if raw_requested_height is None:
            requested_height = None
        else:
            try:
                requested_height = validate_quality_height(raw_requested_height)
            except QualityHeightError as exc:
                raise WebDownloadWorkerError(
                    "invalid requested quality height"
                ) from exc
        try:
            quality_strategy = validate_quality_strategy(
                payload.get("quality_strategy"),
                requested_height=requested_height,
            )
        except MissavError as exc:
            raise WebDownloadWorkerError("invalid quality strategy") from exc
        raw_resume_height = payload.get("resume_selected_height")
        if raw_resume_height is None:
            resume_selected_height = None
        else:
            try:
                resume_selected_height = validate_quality_height(raw_resume_height)
            except QualityHeightError as exc:
                raise WebDownloadWorkerError("invalid resume quality height") from exc
        raw_finalize_lock = payload.get("finalize_lock_path")
        finalize_lock_path = (
            None
            if raw_finalize_lock in (None, "")
            else absolute_file_path(raw_finalize_lock, "finalize lock")
        )
        try:
            existing_policy = validate_existing_policy(
                payload.get("existing_policy"),
                default=LEGACY_EXISTING_POLICY,
                allow_legacy=True,
            )
        except ValueError as exc:
            raise WebDownloadWorkerError(str(exc)) from exc
        incumbent_output_path = optional_relative_output_path(
            payload.get("incumbent_output_path")
        )
        try:
            bandwidth = bandwidth_config_from_payload(payload)
        except BandwidthBrokerError as exc:
            raise WebDownloadWorkerError(
                "invalid shared bandwidth broker configuration"
            ) from exc
        with media_provider_scope(provider):
            manifest = _manifest_from_payload(payload)
        media_kind = str(payload.get("manifest_media_kind") or "hls").strip().lower()
        if media_kind not in {"hls", "progressive"}:
            raise WebDownloadWorkerError("invalid worker media kind")
        if media_kind == "progressive" and (provider != "supjav" or manifest is None):
            raise WebDownloadWorkerError("invalid worker media kind")
        return cls(
            job_id=job_id,
            provider=provider,
            code=code,
            variant=variant,
            incoming_root=incoming_root,
            library_root=library_root,
            capture_timeout_seconds=capture_timeout,
            min_free_bytes=min_free_bytes,
            max_file_bytes=max_file_bytes,
            requested_height=requested_height,
            quality_strategy=quality_strategy,
            resume_selected_height=resume_selected_height,
            finalize_lock_path=finalize_lock_path,
            bandwidth=bandwidth,
            existing_policy=existing_policy,
            incumbent_output_path=incumbent_output_path,
            manifest=manifest,
            media_kind=media_kind,
        )


class JsonEventEmitter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_progress_at = 0.0
        self._last_progress = -1.0

    def emit(self, event: str, **payload: object) -> None:
        message = {"event": event, **payload}
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            sys.stdout.write(raw + "\n")
            sys.stdout.flush()

    def status(self, status: str) -> None:
        self.emit("status", status=status)

    def progress(
        self,
        *,
        status: str,
        progress: float,
        downloaded_bytes: int = 0,
        total_bytes: int | None = None,
        storage_estimate_bytes: int | None = None,
        speed: float | None = None,
        eta: float | None = None,
        checkpoint_bytes: int | None = None,
        checkpoint_fragments: int | None = None,
        checkpoint_reset: bool = False,
        checkpoint_reconcile: bool = False,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        bounded_progress = max(0.0, min(float(progress), 100.0))
        elapsed = now - self._last_progress_at
        delta = abs(bounded_progress - self._last_progress)
        if not force:
            if elapsed < PROGRESS_MIN_INTERVAL_SECONDS:
                return
            if (
                delta < PROGRESS_MIN_DELTA_PERCENT
                and elapsed < PROGRESS_MAX_INTERVAL_SECONDS
            ):
                return
        self._last_progress_at = now
        self._last_progress = bounded_progress
        payload: dict[str, object] = {
            "status": status,
            "progress": bounded_progress,
            "downloaded_bytes": max(0, int(downloaded_bytes or 0)),
            "total_bytes": _optional_nonnegative_int(total_bytes),
            "storage_estimate_bytes": _optional_nonnegative_int(storage_estimate_bytes),
            "speed": _optional_nonnegative_float(speed),
            "eta": _optional_nonnegative_float(eta),
        }
        if checkpoint_bytes is not None:
            payload["checkpoint_bytes"] = max(0, int(checkpoint_bytes))
        if checkpoint_fragments is not None:
            payload["checkpoint_fragments"] = max(0, int(checkpoint_fragments))
        if checkpoint_reset:
            if checkpoint_bytes is None or checkpoint_fragments is None:
                raise WebDownloadWorkerError("checkpoint reset progress is incomplete")
            payload["checkpoint_reset"] = True
        if checkpoint_reconcile:
            if checkpoint_reset:
                raise WebDownloadWorkerError("checkpoint progress mode is invalid")
            if checkpoint_bytes is None or checkpoint_fragments is None:
                raise WebDownloadWorkerError(
                    "checkpoint reconciliation progress is incomplete"
                )
            if payload["downloaded_bytes"] != payload["checkpoint_bytes"]:
                raise WebDownloadWorkerError(
                    "checkpoint reconciliation progress is invalid"
                )
            payload["checkpoint_reconcile"] = True
        self.emit("progress", **payload)


def _manifest_from_payload(payload: object) -> ManifestRequest | None:
    if not isinstance(payload, dict):
        raise WebDownloadWorkerError("worker task is invalid")
    raw_url = payload.get("manifest_url")
    raw_headers = payload.get("manifest_headers")
    if raw_url in (None, "") and raw_headers in (None, ""):
        return None
    if not isinstance(raw_url, str) or not isinstance(raw_headers, dict):
        raise WebDownloadWorkerError("worker manifest request is invalid")
    try:
        resolver = PublicHostResolver(max_hosts=1)
        clean_url = require_allowed_media_url(raw_url, resolver=resolver)
    except SafeMediaError as exc:
        raise WebDownloadWorkerError("worker manifest request is invalid") from exc
    if len(raw_headers) > 32:
        raise WebDownloadWorkerError("worker manifest headers are invalid")
    clean_headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name or "").strip().lower()
        value = str(raw_value or "").strip()
        if (
            not name
            or len(name) > 128
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name)
            or not value
            or len(value) > 8192
            or any(character in value for character in "\r\n\0")
            or name in {"cookie", "authorization"}
        ):
            # MissAV currently only emits safe replay headers.  Reject
            # credentials rather than allowing a worker task to smuggle them.
            if name in {"cookie", "authorization"}:
                raise WebDownloadWorkerError("worker manifest headers are invalid")
            continue
        clean_headers[name] = value
    page_url = payload.get("manifest_page_url")
    # page_url is intentionally not accepted from the wire; it would be a
    # navigation hint and is unnecessary for media transfer.
    del page_url
    selected_height = payload.get("manifest_selected_height")
    if selected_height is not None:
        try:
            selected_height = validate_quality_height(selected_height)
        except QualityHeightError as exc:
            raise WebDownloadWorkerError("worker manifest quality is invalid") from exc
    return ManifestRequest(
        url=clean_url,
        headers=clean_headers,
        page_url="https://missav.invalid/",
        selected_height=selected_height,
    )


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _bounded_positive_int(value: object, *, default: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebDownloadWorkerError("invalid worker size limit") from exc
    if parsed < 0 or parsed > maximum:
        raise WebDownloadWorkerError("worker size limit is out of range")
    return parsed


def _optional_nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed != parsed or parsed == float("inf"):
        return None
    return parsed
