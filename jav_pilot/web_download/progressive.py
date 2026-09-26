"""Progressive (single file) downloads with resumable checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from ..core.catalog_code import canonical_catalog_code
from ..net.network_guard import PublicHostResolver
from .bandwidth import BandwidthClient, BandwidthClientConfig
from .media import (
    MediaTransportDisposition,
    SilentYtdlpLogger,
    classify_media_transport_error,
)
from .hls import require_worker_media_url, worker_youtube_dl_type
from .worker_common import (
    COPY_CHUNK_BYTES,
    DURABLE_CHECKPOINT_BYTES,
    MEDIA_SOCKET_TIMEOUT_SECONDS,
    PRE_CANONICAL_PROGRESSIVE_CHECKPOINT_VERSION,
    PROGRESSIVE_CHECKPOINT_MAX_BYTES,
    PROGRESSIVE_CHECKPOINT_NAME,
    PROGRESSIVE_CHECKPOINT_VERSION,
    PROGRESSIVE_CONTENT_RANGE_RE,
    close_media_transport_error,
    raise_if_cancelled,
)
from .worker_errors import (
    WebDownloadWorkerCancelled,
    WebDownloadWorkerError,
    WebDownloadWorkerTransientTransportError,
)
from .worker_files import exclusive_finalize_lock, fsync_directory, require_free_space
from .worker_task import JsonEventEmitter

def download_progressive(
    media_url: str,
    headers: dict[str, str],
    job_dir: Path,
    code: str,
    emitter: JsonEventEmitter,
    cancel_event: threading.Event,
    max_file_bytes: int,
    min_free_bytes: int,
    *,
    finalize_lock_path: Path | None = None,
    bandwidth_config: BandwidthClientConfig | None = None,
    finalize: Callable[[Path], Path] | None = None,
) -> Path:
    """Download an allowlisted progressive source with safe range resume."""

    try:
        import yt_dlp
        from yt_dlp.networking import Request
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception as exc:  # noqa: BLE001 - pinned in the production image.
        raise WebDownloadWorkerError("yt-dlp runtime is unavailable") from exc

    safe_youtube_dl = worker_youtube_dl_type(yt_dlp)
    require_worker_media_url(media_url, resolver=PublicHostResolver(max_hosts=1))
    code_key = canonical_catalog_code(code, max_length=64)
    if code_key is None:
        raise WebDownloadWorkerError("invalid catalog code")
    part_path = job_dir / f"{code}.progressive.part"
    output_path = job_dir / f"{code}.mp4"
    checkpoint_path = job_dir / PROGRESSIVE_CHECKPOINT_NAME
    for path in (part_path, output_path, checkpoint_path):
        if path.is_symlink():
            raise WebDownloadWorkerError("unsafe progressive download path")

    checkpoint = _read_progressive_checkpoint(checkpoint_path, code_key)
    existing = part_path.stat().st_size if part_path.is_file() else 0
    checkpoint_reset_pending = checkpoint is None
    if existing > max_file_bytes:
        raise WebDownloadWorkerError("media exceeds the configured size limit")
    if checkpoint is None and existing:
        part_path.unlink()
        existing = 0

    transport_headers = {
        name: value
        for name, value in headers.items()
        if name.casefold() not in {"accept-encoding", "range"}
    }
    transport_headers["Accept-Encoding"] = "identity"
    source_digest = hashlib.sha256(media_url.encode("utf-8")).hexdigest()
    bandwidth_client = (
        BandwidthClient(bandwidth_config) if bandwidth_config is not None else None
    )
    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtdlpLogger(),
        "http_headers": dict(transport_headers),
        "impersonate": ImpersonateTarget(client="chrome", version="131"),
        "socket_timeout": MEDIA_SOCKET_TIMEOUT_SECONDS,
        "_no_ytdl_file": True,
    }
    downloaded_this_run = 0
    started_at = time.monotonic()
    response = None
    downloaded = existing
    total = 0
    try:
        with safe_youtube_dl(options) as downloader:
            # One reconciliation retry is sufficient: a changed object resets
            # the partial file, while a stable object resumes at its exact byte.
            for reconciliation in range(2):
                raise_if_cancelled(cancel_event)
                request_headers = dict(transport_headers)
                request_headers["Range"] = f"bytes={existing}-"
                request = Request(media_url, headers=request_headers)
                response = downloader.urlopen(request)
                require_worker_media_url(
                    response.url, resolver=PublicHostResolver(max_hosts=1)
                )
                status = int(getattr(response, "status", 0) or 0)
                content_range = _parse_progressive_content_range(
                    response.get_header("Content-Range")
                )
                if content_range is not None:
                    range_start, range_end, total = content_range
                    if (
                        status != 206
                        or range_start != existing
                        or range_end < range_start
                    ):
                        raise WebDownloadWorkerError(
                            "progressive media range response is invalid"
                        )
                else:
                    raw_length = str(response.get_header("Content-Length") or "")
                    if not raw_length.isdigit() or int(raw_length) <= 0:
                        raise WebDownloadWorkerError(
                            "progressive media size is unavailable"
                        )
                    total = int(raw_length)
                    if status != 200 or existing:
                        response.close()
                        response = None
                        if reconciliation == 0 and existing:
                            part_path.unlink(missing_ok=True)
                            checkpoint_path.unlink(missing_ok=True)
                            checkpoint = None
                            existing = 0
                            checkpoint_reset_pending = True
                            continue
                        raise WebDownloadWorkerError(
                            "progressive media does not support safe resume"
                        )
                if total <= 0 or total > max_file_bytes or existing > total:
                    raise WebDownloadWorkerError(
                        "progressive media exceeds the configured size limit"
                    )
                identity = {
                    "version": PROGRESSIVE_CHECKPOINT_VERSION,
                    "code_key": code_key,
                    "total_bytes": total,
                    "etag": str(response.get_header("ETag") or "")[:512],
                    "last_modified": str(response.get_header("Last-Modified") or "")[
                        :512
                    ],
                    "source_digest": source_digest,
                }
                if existing and not _progressive_checkpoint_matches(
                    checkpoint, identity
                ):
                    response.close()
                    response = None
                    if reconciliation == 0:
                        part_path.unlink(missing_ok=True)
                        checkpoint_path.unlink(missing_ok=True)
                        checkpoint = None
                        existing = 0
                        checkpoint_reset_pending = True
                        continue
                    raise WebDownloadWorkerError(
                        "progressive media identity changed during resume"
                    )
                _write_progressive_checkpoint(checkpoint_path, identity)
                checkpoint = identity
                break
            else:  # pragma: no cover - bounded loop always returns or raises.
                raise WebDownloadWorkerError("progressive media resume failed")

            require_free_space(
                job_dir,
                min_free_bytes + max(0, total - existing),
                volume_key="staging",
            )
            emitter.progress(
                status="downloading",
                progress=8.0 + (83.0 * existing / total),
                downloaded_bytes=existing,
                total_bytes=total,
                checkpoint_bytes=existing,
                checkpoint_fragments=existing // DURABLE_CHECKPOINT_BYTES,
                checkpoint_reset=checkpoint_reset_pending,
                force=True,
            )
            checkpoint_reset_pending = False
            downloaded = existing
            durable_at = existing
            mode = "ab" if existing else "wb"
            with part_path.open(mode) as output:
                while True:
                    raise_if_cancelled(cancel_event)
                    read_size = (
                        bandwidth_client.allowance(COPY_CHUNK_BYTES)
                        if bandwidth_client is not None
                        else COPY_CHUNK_BYTES
                    )
                    chunk = response.read(read_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes) or len(chunk) > read_size:
                        raise WebDownloadWorkerError(
                            "progressive media response is invalid"
                        )
                    if (
                        downloaded + len(chunk) > total
                        or downloaded + len(chunk) > max_file_bytes
                    ):
                        raise WebDownloadWorkerError(
                            "progressive media exceeds the configured size limit"
                        )
                    output.write(chunk)
                    downloaded += len(chunk)
                    downloaded_this_run += len(chunk)
                    if downloaded - durable_at >= DURABLE_CHECKPOINT_BYTES:
                        output.flush()
                        os.fsync(output.fileno())
                        durable_at = downloaded
                    elapsed = max(0.001, time.monotonic() - started_at)
                    speed = downloaded_this_run / elapsed
                    emitter.progress(
                        status="downloading",
                        progress=8.0 + (83.0 * downloaded / total),
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        speed=speed,
                        eta=(total - downloaded) / speed if speed else None,
                        checkpoint_bytes=durable_at,
                        checkpoint_fragments=durable_at // DURABLE_CHECKPOINT_BYTES,
                    )
                output.flush()
                os.fsync(output.fileno())
            if downloaded != total or part_path.stat().st_size != total:
                raise WebDownloadWorkerTransientTransportError(
                    downloaded,
                    downloaded // DURABLE_CHECKPOINT_BYTES,
                )
            response.close()
            response = None
            emitter.progress(
                status="downloading",
                progress=91.0,
                downloaded_bytes=downloaded,
                total_bytes=total,
                checkpoint_bytes=downloaded,
                checkpoint_fragments=downloaded // DURABLE_CHECKPOINT_BYTES,
                force=True,
            )
            emitter.status("verifying")
            with exclusive_finalize_lock(finalize_lock_path, cancel_event):
                raise_if_cancelled(cancel_event)
                output_path.unlink(missing_ok=True)
                os.replace(part_path, output_path)
                fsync_directory(job_dir)
                downloaded_path = (
                    finalize(output_path) if finalize is not None else output_path
                )
    except WebDownloadWorkerCancelled:
        raise
    except WebDownloadWorkerError:
        raise
    except Exception as exc:  # noqa: BLE001 - typed below at the transport boundary.
        disposition = classify_media_transport_error(exc)
        close_media_transport_error(exc)
        if disposition is not MediaTransportDisposition.FATAL:
            checkpoint_bytes = _durable_progressive_file_size(
                part_path,
                max_file_bytes=max_file_bytes,
            )
            raise WebDownloadWorkerTransientTransportError(
                checkpoint_bytes,
                checkpoint_bytes // DURABLE_CHECKPOINT_BYTES,
            ) from exc
        raise WebDownloadWorkerError("progressive media download failed") from exc
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if bandwidth_client is not None:
            bandwidth_client.close()

    raise_if_cancelled(cancel_event)
    if downloaded_path.is_symlink() or not downloaded_path.is_file():
        raise WebDownloadWorkerError("progressive media produced no video file")
    return downloaded_path


def _durable_progressive_file_size(path: Path, *, max_file_bytes: int) -> int:
    if not path.exists():
        return 0
    if path.is_symlink() or not path.is_file():
        raise WebDownloadWorkerError("progressive checkpoint is invalid")
    with path.open("r+b") as checkpoint_file:
        checkpoint_file.flush()
        os.fsync(checkpoint_file.fileno())
    size = path.stat().st_size
    if size < 0 or size > max_file_bytes:
        raise WebDownloadWorkerError("progressive checkpoint is invalid")
    return size


def _parse_progressive_content_range(value: object) -> tuple[int, int, int] | None:
    match = PROGRESSIVE_CONTENT_RANGE_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    start, end, total = (int(item) for item in match.groups())
    if start > end or end >= total:
        raise WebDownloadWorkerError("progressive media range response is invalid")
    return start, end, total


def _read_progressive_checkpoint(path: Path, code_key: str) -> dict[str, object] | None:
    if (
        not isinstance(code_key, str)
        or canonical_catalog_code(code_key, max_length=64) != code_key
    ):
        raise WebDownloadWorkerError("progressive resume checkpoint is invalid")
    if not path.exists():
        return None
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > PROGRESSIVE_CHECKPOINT_MAX_BYTES
    ):
        raise WebDownloadWorkerError("progressive resume checkpoint is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebDownloadWorkerError(
            "progressive resume checkpoint is invalid"
        ) from exc
    version = value.get("version") if isinstance(value, dict) else None
    if type(version) is not int or version not in {
        PRE_CANONICAL_PROGRESSIVE_CHECKPOINT_VERSION,
        PROGRESSIVE_CHECKPOINT_VERSION,
    }:
        raise WebDownloadWorkerError("progressive resume checkpoint is invalid")
    stored_code_key = value.get("code_key")
    if version == PROGRESSIVE_CHECKPOINT_VERSION:
        if (
            not isinstance(stored_code_key, str)
            or stored_code_key != code_key
            or canonical_catalog_code(stored_code_key, max_length=64)
            != stored_code_key
        ):
            raise WebDownloadWorkerError("progressive resume checkpoint is invalid")
        return value
    if (
        not isinstance(stored_code_key, str)
        or not stored_code_key
        or len(stored_code_key) > 64
        or not stored_code_key.isascii()
        or not stored_code_key.isalnum()
        or not stored_code_key.isupper()
        or canonical_catalog_code(stored_code_key, max_length=64) != code_key
    ):
        raise WebDownloadWorkerError("progressive resume checkpoint is invalid")
    migrated = {
        **value,
        "version": PROGRESSIVE_CHECKPOINT_VERSION,
        "code_key": code_key,
    }
    _write_progressive_checkpoint(path, migrated)
    return migrated


def _progressive_checkpoint_matches(
    previous: dict[str, object] | None, current: dict[str, object]
) -> bool:
    if previous is None:
        return False
    if (
        previous.get("version") != current.get("version")
        or previous.get("code_key") != current.get("code_key")
        or previous.get("total_bytes") != current.get("total_bytes")
    ):
        return False
    for key in ("etag", "last_modified"):
        old = str(previous.get(key) or "")
        new = str(current.get(key) or "")
        if old or new:
            return bool(old and new and old == new)
    return previous.get("source_digest") == current.get("source_digest")


def _write_progressive_checkpoint(path: Path, value: dict[str, object]) -> None:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(raw) > PROGRESSIVE_CHECKPOINT_MAX_BYTES:
        raise WebDownloadWorkerError("progressive resume checkpoint is invalid")
    temporary = path.with_suffix(".tmp")
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise WebDownloadWorkerError("progressive resume checkpoint is unsafe")
        temporary.unlink()
    with temporary.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)
