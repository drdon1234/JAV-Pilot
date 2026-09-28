"""Limits and small helpers shared by the web download worker stages."""

from __future__ import annotations

import re
import threading

from .worker_errors import WebDownloadWorkerCancelled

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SAFE_CODE_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")
URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".m4v", ".mov", ".ts"})
# Keep reads small enough that the parent process receives progress heartbeats even
# when a CDN serves a segment slowly.  A multi-megabyte blocking read can hide a
# live connection for several minutes and is indistinguishable from a dead one.
COPY_CHUNK_BYTES = 256 * 1024
MEDIA_SOCKET_TIMEOUT_SECONDS = 30.0
MAX_TASK_BYTES = 32 * 1024
MIN_STORAGE_ESTIMATE_FRAGMENTS = 8
STORAGE_ESTIMATE_MARGIN_BYTES = 256 * 1024**2
STORAGE_ESTIMATE_MARGIN_PERCENT = 15
PROGRESS_MIN_INTERVAL_SECONDS = 1.0
PROGRESS_MAX_INTERVAL_SECONDS = 5.0
PROGRESS_MIN_DELTA_PERCENT = 0.5
SEGMENT_RETRY_DELAYS = (2.0, 5.0, 15.0)
DURABLE_CHECKPOINT_BYTES = 64 * 1024**2
DURABLE_CHECKPOINT_FRAGMENTS = 10
MAX_ENCRYPTED_SEGMENT_BYTES = 64 * 1024 * 1024
HLS_DURATION_TOLERANCE_SECONDS = 8.0
# Some providers mux a short silent tail out of the audio track. Only a
# materially missing tail should make an otherwise valid HLS rendition fail.
AUDIO_VIDEO_DURATION_TOLERANCE_SECONDS = 30.0
PROGRESSIVE_CHECKPOINT_VERSION = 2
PRE_CANONICAL_PROGRESSIVE_CHECKPOINT_VERSION = 1
PROGRESSIVE_CHECKPOINT_MAX_BYTES = 16 * 1024
PROGRESSIVE_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)
PROGRESSIVE_CHECKPOINT_NAME = "progressive.json"


def raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise WebDownloadWorkerCancelled("web download cancelled")


def close_media_transport_error(error: BaseException) -> None:
    try:
        from yt_dlp.networking.exceptions import HTTPError
    except Exception:  # noqa: BLE001 - dependency is optional outside Docker.
        return
    if not isinstance(error, HTTPError):
        return
    close = getattr(error, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - response cleanup is best effort.
        pass
