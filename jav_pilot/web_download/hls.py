"""HLS downloads: segment transfer, decryption, assembly and remuxing."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from ..core.catalog_code import canonical_catalog_code
from ..net.network_guard import PublicHostResolver
from .bandwidth import BandwidthBrokerError, BandwidthClient, BandwidthClientConfig
from .media import (
    MAX_MANIFEST_BYTES,
    MediaTransportDisposition,
    SafeMediaError,
    SilentYtdlpLogger,
    allowlisted_youtube_dl_type,
    classify_media_transport_error,
    require_allowed_media_url,
    validate_native_hls_manifest,
)
from .resume import MediaPlaylist, ResumeCheckpointError, ResumeIdentity, ResumeStore
from .variant import DEFAULT_WEB_DOWNLOAD_VARIANT, normalize_web_download_variant
from .verify import verify_video
from .worker_common import (
    COPY_CHUNK_BYTES,
    DURABLE_CHECKPOINT_BYTES,
    DURABLE_CHECKPOINT_FRAGMENTS,
    MAX_ENCRYPTED_SEGMENT_BYTES,
    MEDIA_SOCKET_TIMEOUT_SECONDS,
    MIN_STORAGE_ESTIMATE_FRAGMENTS,
    SEGMENT_RETRY_DELAYS,
    STORAGE_ESTIMATE_MARGIN_BYTES,
    STORAGE_ESTIMATE_MARGIN_PERCENT,
    close_media_transport_error,
    raise_if_cancelled,
)
from .worker_errors import (
    HlsDurationMismatch,
    MediaSegmentContentLengthMismatch,
    WebDownloadWorkerCancelled,
    WebDownloadWorkerDiskLowError,
    WebDownloadWorkerError,
    WebDownloadWorkerTransientTransportError,
)
from .worker_files import exclusive_finalize_lock, fsync_directory, require_free_space
from .worker_task import JsonEventEmitter

def download_manifest(
    manifest_url: str,
    headers: dict[str, str],
    job_dir: Path,
    code: str,
    emitter: JsonEventEmitter,
    cancel_event: threading.Event,
    max_file_bytes: int,
    min_free_bytes: int,
    *,
    finalize_lock_path: Path | None = None,
    requested_height: int | None = None,
    selected_height: int | None = None,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    bandwidth_config: BandwidthClientConfig | None = None,
    finalize: Callable[[Path], Path] | None = None,
) -> Path:
    try:
        import yt_dlp
        from yt_dlp.networking import Request
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception as exc:  # noqa: BLE001 - runtime dependency is optional outside Docker.
        raise WebDownloadWorkerError("yt-dlp runtime is unavailable") from exc

    safe_youtube_dl = worker_youtube_dl_type(yt_dlp)
    require_worker_media_url(manifest_url, resolver=PublicHostResolver(max_hosts=1))
    # yt-dlp's Chrome impersonation advertises compressed transfer encodings,
    # but its low-level response object can expose the encoded bytes directly
    # for HLS manifests.  Request identity transport explicitly so strict
    # UTF-8 parsing always receives the actual playlist, not gzip/brotli data.
    transport_headers = {
        name: value
        for name, value in headers.items()
        if name.casefold() != "accept-encoding"
    }
    transport_headers["Accept-Encoding"] = "identity"
    code_key = canonical_catalog_code(code, max_length=64)
    if code_key is None:
        raise WebDownloadWorkerError("invalid catalog code")
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise WebDownloadWorkerError("invalid worker variant") from exc
    resume_identity = ResumeIdentity(
        code=code,
        code_key=code_key,
        variant=clean_variant,
        requested_height=requested_height,
        selected_height=selected_height,
    )
    last_space_check_at = 0.0
    started_at = time.monotonic()
    downloaded_this_run = 0
    store: ResumeStore | None = None
    durable_checkpoint_bytes = 0
    durable_checkpoint_fragments = 0
    checkpoint_reset_pending = False
    bandwidth_client = (
        BandwidthClient(bandwidth_config) if bandwidth_config is not None else None
    )

    def persist_checkpoint_progress(*, force: bool = False) -> None:
        nonlocal durable_checkpoint_bytes, durable_checkpoint_fragments
        if store is None:
            return
        current_bytes = store.completed_bytes
        current_fragments = store.completed_count
        if (
            not force
            and current_bytes - durable_checkpoint_bytes < DURABLE_CHECKPOINT_BYTES
            and current_fragments - durable_checkpoint_fragments
            < DURABLE_CHECKPOINT_FRAGMENTS
        ):
            return
        store.flush(force=True)
        durable_checkpoint_bytes = current_bytes
        durable_checkpoint_fragments = current_fragments

    def emit_reconciled_checkpoint_snapshot(
        downloaded_bytes: int,
        fragment_index: int,
        fragment_count: int,
    ) -> None:
        nonlocal checkpoint_reset_pending
        if not checkpoint_reset_pending:
            return
        fraction = min(fragment_index / fragment_count, 1.0)
        emitter.progress(
            status="downloading",
            progress=8.0 + (83.0 * fraction),
            downloaded_bytes=downloaded_bytes,
            checkpoint_bytes=durable_checkpoint_bytes,
            checkpoint_fragments=durable_checkpoint_fragments,
            checkpoint_reset=True,
            force=True,
        )
        checkpoint_reset_pending = False

    def report_progress(
        downloaded_bytes: int,
        fragment_index: int,
        fragment_count: int,
        *,
        force: bool = False,
        complete: bool = False,
        checkpoint_reconcile: bool = False,
    ) -> None:
        nonlocal last_space_check_at, checkpoint_reset_pending
        fragment_estimate = _estimate_storage_bytes(
            downloaded_bytes=downloaded_bytes,
            fragment_index=fragment_index,
            fragment_count=fragment_count,
            max_file_bytes=max_file_bytes,
        )
        if downloaded_bytes > max_file_bytes:
            raise WebDownloadWorkerError("media exceeds the configured size limit")
        now = time.monotonic()
        if now - last_space_check_at >= 0.5:
            last_space_check_at = now
            try:
                free_bytes = shutil.disk_usage(job_dir).free
            except OSError as exc:
                raise WebDownloadWorkerError(
                    "download storage capacity could not be checked"
                ) from exc
            remaining_bytes = max(
                0,
                (fragment_estimate or downloaded_bytes) - downloaded_bytes,
            )
            if (
                free_bytes < min_free_bytes
                or remaining_bytes + min_free_bytes > free_bytes
            ):
                raise WebDownloadWorkerDiskLowError("staging")
        fraction = min(fragment_index / fragment_count, 1.0)
        elapsed = max(0.0, now - started_at)
        speed = downloaded_this_run / elapsed if elapsed > 0 else None
        eta = None
        if speed and fragment_estimate and fragment_estimate > downloaded_bytes:
            eta = (fragment_estimate - downloaded_bytes) / speed
        emitter.progress(
            status="downloading",
            progress=8.0 + (83.0 * fraction),
            downloaded_bytes=downloaded_bytes,
            total_bytes=downloaded_bytes if complete else None,
            storage_estimate_bytes=(
                downloaded_bytes if complete else fragment_estimate
            ),
            speed=speed,
            eta=eta,
            checkpoint_bytes=durable_checkpoint_bytes,
            checkpoint_fragments=durable_checkpoint_fragments,
            checkpoint_reset=checkpoint_reset_pending,
            checkpoint_reconcile=checkpoint_reconcile,
            force=force,
        )
        checkpoint_reset_pending = False

    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtdlpLogger(),
        "http_headers": dict(transport_headers),
        "impersonate": ImpersonateTarget(client="chrome", version="131"),
        # yt-dlp applies this timeout to both connect and subsequent socket reads.
        # Segment retries and the durable checkpoint path below turn a timeout into
        # a resumable transport failure instead of occupying a worker indefinitely.
        "socket_timeout": MEDIA_SOCKET_TIMEOUT_SECONDS,
        "_no_ytdl_file": True,
    }
    # The manifest fetch and ResumeStore.prepare below can legitimately stay
    # quiet for minutes (checkpoint revalidation re-hashes every completed
    # segment). The parent stall watchdog only arms on "downloading" events,
    # so this phase must report "validating"; the first "downloading" event is
    # emitted after prepare, when network transfer actually begins.
    emitter.progress(
        status="validating",
        progress=8.0,
        force=True,
    )
    try:
        with safe_youtube_dl(options) as downloader:
            manifest_request = Request(
                manifest_url,
                headers=dict(transport_headers),
            )
            with downloader.urlopen(manifest_request) as response:
                final_manifest_url = require_worker_media_url(
                    response.url,
                    resolver=PublicHostResolver(max_hosts=1),
                )
                manifest_bytes = response.read(MAX_MANIFEST_BYTES + 1)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise WebDownloadWorkerError(
                    "media manifest exceeds the safe size limit"
                )
            manifest_text = manifest_bytes.decode("utf-8", errors="strict")
            playlist = _validate_native_hls_manifest(manifest_text, final_manifest_url)
            raise_if_cancelled(cancel_event)
            store = ResumeStore.prepare(
                job_dir,
                playlist,
                resume_identity,
                manifest_url=final_manifest_url,
                reset_on_manifest_mismatch=True,
            )
            durable_checkpoint_bytes = store.completed_bytes
            durable_checkpoint_fragments = store.completed_count
            checkpoint_reset_pending = store.checkpoint_reconciled
            completed_bytes = store.completed_bytes
            completed_count = store.completed_count
            emit_reconciled_checkpoint_snapshot(
                completed_bytes,
                completed_count,
                playlist.segment_count,
            )
            report_progress(
                completed_bytes,
                completed_count,
                playlist.segment_count,
                force=True,
            )

            key_cache: dict[str, bytes] = {}

            def encryption_key(key_uri: str) -> bytes:
                key_url = require_worker_media_url(
                    urljoin(final_manifest_url, key_uri),
                    resolver=PublicHostResolver(max_hosts=1),
                )
                cached = key_cache.get(key_url)
                if cached is not None:
                    return cached
                key_request = Request(key_url, headers=dict(transport_headers))
                with downloader.urlopen(key_request) as key_response:
                    require_worker_media_url(
                        key_response.url,
                        resolver=PublicHostResolver(max_hosts=1),
                    )
                    key = key_response.read(17)
                if not isinstance(key, bytes) or len(key) != 16:
                    raise WebDownloadWorkerError("HLS encryption key is invalid")
                key_cache[key_url] = key
                return key

            for segment in playlist.segments:
                raise_if_cancelled(cancel_event)
                if store.is_completed(segment.index):
                    continue
                segment_url = require_worker_media_url(
                    urljoin(final_manifest_url, segment.uri),
                    resolver=PublicHostResolver(max_hosts=1),
                )
                for attempt in range(len(SEGMENT_RETRY_DELAYS) + 1):
                    raise_if_cancelled(cancel_event)
                    segment_size = 0
                    received_size = 0
                    try:
                        segment_digest = hashlib.sha256()
                        segment_request = Request(
                            segment_url,
                            headers=dict(transport_headers),
                        )
                        with downloader.urlopen(segment_request) as response:
                            require_worker_media_url(
                                response.url,
                                resolver=PublicHostResolver(max_hosts=1),
                            )
                            declared_size = _response_content_length(response)
                            encrypted_payload = (
                                bytearray() if segment.key_uri is not None else None
                            )
                            with store.open_segment(segment.index) as output:
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
                                    if not isinstance(chunk, bytes):
                                        raise WebDownloadWorkerError(
                                            "media segment response is invalid"
                                        )
                                    if len(chunk) > read_size:
                                        raise WebDownloadWorkerError(
                                            "media segment response exceeded its read allowance"
                                        )
                                    if (
                                        completed_bytes + segment_size + len(chunk)
                                        > max_file_bytes
                                    ):
                                        raise WebDownloadWorkerError(
                                            "media exceeds the configured size limit"
                                        )
                                    if encrypted_payload is not None:
                                        if (
                                            len(encrypted_payload) + len(chunk)
                                            > MAX_ENCRYPTED_SEGMENT_BYTES
                                        ):
                                            raise WebDownloadWorkerError(
                                                "encrypted HLS segment is too large"
                                            )
                                        encrypted_payload.extend(chunk)
                                    else:
                                        output.write(chunk)
                                        segment_digest.update(chunk)
                                    received_size += len(chunk)
                                    segment_size += len(chunk)
                                    downloaded_this_run += len(chunk)
                                    report_progress(
                                        completed_bytes + segment_size,
                                        completed_count,
                                        playlist.segment_count,
                                    )
                                if (
                                    declared_size is not None
                                    and received_size != declared_size
                                ):
                                    raise MediaSegmentContentLengthMismatch(
                                        "media segment response length did not match Content-Length"
                                    )
                                if encrypted_payload is not None:
                                    key = encryption_key(str(segment.key_uri))
                                    iv = segment.key_iv or (
                                        playlist.media_sequence + segment.index
                                    ).to_bytes(16, "big")
                                    clear = _decrypt_hls_aes128(
                                        bytes(encrypted_payload), key, iv
                                    )
                                    output.write(clear)
                                    segment_digest.update(clear)
                                    segment_size = len(clear)
                        raise_if_cancelled(cancel_event)
                        if segment_size <= 0:
                            raise WebDownloadWorkerError(
                                "media segment download produced no data"
                            )
                        store.commit_segment(
                            segment.index,
                            size=segment_size,
                            sha256=segment_digest.hexdigest(),
                        )
                        persist_checkpoint_progress()
                    except BaseException as exc:
                        store.discard_part(segment.index)
                        downloaded_this_run = max(
                            0,
                            downloaded_this_run - segment_size,
                        )
                        if segment_size > 0:
                            persist_checkpoint_progress(force=True)
                            completed_bytes = store.completed_bytes
                            completed_count = store.completed_count
                            report_progress(
                                completed_bytes,
                                completed_count,
                                playlist.segment_count,
                                force=True,
                                checkpoint_reconcile=True,
                            )
                        if isinstance(exc, WebDownloadWorkerCancelled):
                            raise
                        if isinstance(exc, MediaSegmentContentLengthMismatch):
                            if attempt >= len(SEGMENT_RETRY_DELAYS):
                                raise WebDownloadWorkerTransientTransportError(
                                    store.completed_bytes,
                                    store.completed_count,
                                    checkpoint_reset=checkpoint_reset_pending,
                                ) from exc
                            close_media_transport_error(exc)
                            if cancel_event.wait(SEGMENT_RETRY_DELAYS[attempt]):
                                raise_if_cancelled(cancel_event)
                            continue
                        disposition = classify_media_transport_error(exc)
                        if disposition is MediaTransportDisposition.FATAL:
                            raise
                        if (
                            disposition is MediaTransportDisposition.REFRESH_MANIFEST
                            or attempt >= len(SEGMENT_RETRY_DELAYS)
                        ):
                            raise
                        close_media_transport_error(exc)
                        if cancel_event.wait(SEGMENT_RETRY_DELAYS[attempt]):
                            raise_if_cancelled(cancel_event)
                    else:
                        break
                completed_bytes = store.completed_bytes
                completed_count = store.completed_count
                report_progress(
                    completed_bytes,
                    completed_count,
                    playlist.segment_count,
                )

            transport_size = store.completed_bytes
            if transport_size > max_file_bytes:
                raise WebDownloadWorkerError("media exceeds the configured size limit")
            persist_checkpoint_progress(force=True)
            report_progress(
                transport_size,
                playlist.segment_count,
                playlist.segment_count,
                force=True,
                complete=True,
            )
            # All network I/O is complete.  Assembly and remux can legitimately
            # take several minutes for multi-gigabyte files on a NAS, so leave the
            # downloading phase before the parent transport-stall watchdog applies.
            emitter.status("verifying")
            with exclusive_finalize_lock(finalize_lock_path, cancel_event):
                raise_if_cancelled(cancel_event)
                transport_path = job_dir / f"{code}.ts"
                _remove_local_transport(transport_path)
                _remove_local_transport(job_dir / f"{code}.mp4")
                require_free_space(job_dir, min_free_bytes + transport_size)
                _assemble_transport_stream(
                    store,
                    transport_path,
                    cancel_event,
                    max_file_bytes=max_file_bytes,
                )
                require_free_space(job_dir, min_free_bytes + transport_size)
                downloaded_path = _remux_local_video(
                    transport_path, job_dir / f"{code}.mp4"
                )
                if finalize is not None:
                    try:
                        verify_video(
                            downloaded_path,
                            selected_height=selected_height,
                            expected_duration_seconds=playlist.duration_seconds,
                        )
                    except HlsDurationMismatch as exc:
                        raise WebDownloadWorkerTransientTransportError(
                            store.completed_bytes,
                            store.completed_count,
                            checkpoint_reset=checkpoint_reset_pending,
                        ) from exc
                    # Persist all segment integrity state before a successful
                    # finalizer is allowed to remove the staging directory.
                    store.flush(force=True)
                    downloaded_path = finalize(downloaded_path)
                    store = None
    except UnicodeDecodeError as exc:
        raise WebDownloadWorkerError("media manifest is not valid UTF-8") from exc
    except ResumeCheckpointError as exc:
        raise WebDownloadWorkerError(str(exc)) from exc
    except BandwidthBrokerError as exc:
        raise WebDownloadWorkerError(
            "shared download bandwidth control is unavailable"
        ) from exc
    except WebDownloadWorkerCancelled:
        raise
    except WebDownloadWorkerError:
        raise
    except Exception as exc:  # noqa: BLE001 - network libraries expose broad errors.
        disposition = classify_media_transport_error(exc)
        close_media_transport_error(exc)
        if disposition is not MediaTransportDisposition.FATAL:
            if store is None:
                raise WebDownloadWorkerTransientTransportError() from exc
            try:
                store.flush(force=True)
            except ResumeCheckpointError as checkpoint_exc:
                raise WebDownloadWorkerError(str(checkpoint_exc)) from checkpoint_exc
            raise WebDownloadWorkerTransientTransportError(
                store.completed_bytes,
                store.completed_count,
                checkpoint_reset=checkpoint_reset_pending,
            ) from exc
        raise WebDownloadWorkerError("media download failed") from exc
    finally:
        try:
            if store is not None:
                active_error = sys.exc_info()[0] is not None
                try:
                    store.flush(force=True)
                except ResumeCheckpointError as exc:
                    if not active_error:
                        raise WebDownloadWorkerError(str(exc)) from exc
        finally:
            if bandwidth_client is not None:
                bandwidth_client.close()

    raise_if_cancelled(cancel_event)
    if downloaded_path.is_symlink() or not downloaded_path.is_file():
        raise WebDownloadWorkerError("media download produced no video file")
    return downloaded_path


def _estimate_storage_bytes(
    *,
    downloaded_bytes: int,
    fragment_index: int | None,
    fragment_count: int | None,
    max_file_bytes: int,
) -> int | None:
    if (
        downloaded_bytes <= 0
        or fragment_index is None
        or fragment_count is None
        or fragment_index < MIN_STORAGE_ESTIMATE_FRAGMENTS
        or fragment_count < fragment_index
        or max_file_bytes <= 0
    ):
        return None
    projected = (
        downloaded_bytes * fragment_count + fragment_index - 1
    ) // fragment_index
    proportional_margin = (projected * STORAGE_ESTIMATE_MARGIN_PERCENT + 99) // 100
    estimate = projected + max(
        STORAGE_ESTIMATE_MARGIN_BYTES,
        proportional_margin,
    )
    return min(max_file_bytes, max(downloaded_bytes, estimate))


def _validate_native_hls_manifest(manifest: str, manifest_url: str) -> MediaPlaylist:
    try:
        return validate_native_hls_manifest(manifest, manifest_url)
    except SafeMediaError as exc:
        raise WebDownloadWorkerError(str(exc)) from exc


def _response_content_length(response: object) -> int | None:
    raw_value: object | None = None
    getter = getattr(response, "get_header", None)
    if callable(getter):
        try:
            raw_value = getter("Content-Length")
        except Exception:  # noqa: BLE001 - missing optional response header access.
            raw_value = None
    if raw_value in (None, ""):
        headers = getattr(response, "headers", None)
        items = getattr(headers, "items", None)
        if callable(items):
            try:
                for name, value in items():
                    if str(name).casefold() == "content-length":
                        raw_value = value
                        break
            except Exception:  # noqa: BLE001 - malformed optional header mapping.
                raw_value = None
    if raw_value in (None, ""):
        return None
    raw = str(raw_value).strip()
    if not raw.isdigit():
        raise WebDownloadWorkerError("media segment Content-Length is invalid")
    return int(raw)


def _decrypt_hls_aes128(payload: bytes, key: bytes, iv: bytes) -> bytes:
    if not payload or len(payload) % 16 or len(key) != 16 or len(iv) != 16:
        raise WebDownloadWorkerError("encrypted HLS segment is invalid")
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(payload) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        clear = unpadder.update(padded) + unpadder.finalize()
    except Exception as exc:
        raise WebDownloadWorkerError(
            "encrypted HLS segment could not be decrypted"
        ) from exc
    if len(clear) < 188 * 3 or any(clear[index * 188] != 0x47 for index in range(3)):
        raise WebDownloadWorkerError("decrypted HLS segment is invalid")
    return clear


def _assemble_transport_stream(
    store: ResumeStore,
    target: Path,
    cancel_event: threading.Event,
    *,
    max_file_bytes: int,
) -> Path:
    temporary = target.with_name(f".{target.name}.assemble.part")
    _remove_local_transport(temporary)
    copied = 0
    try:
        with temporary.open("xb") as writer:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            for source in store.completed_sources():
                raise_if_cancelled(cancel_event)
                if source.path.is_symlink() or not source.path.is_file():
                    raise WebDownloadWorkerError(
                        "resume segment disappeared before assembly"
                    )
                remaining = source.size
                with source.path.open("rb") as reader:
                    reader.seek(source.offset)
                    while remaining > 0:
                        chunk = reader.read(min(COPY_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise WebDownloadWorkerError(
                                "resume segment disappeared before assembly"
                            )
                        raise_if_cancelled(cancel_event)
                        copied += len(chunk)
                        remaining -= len(chunk)
                        if copied > max_file_bytes:
                            raise WebDownloadWorkerError(
                                "media exceeds the configured size limit"
                            )
                        writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if copied <= 0 or copied != store.completed_bytes:
            raise WebDownloadWorkerError("downloaded HLS transport is invalid")
        os.replace(temporary, target)
        fsync_directory(target.parent)
        return target
    except BaseException:
        _remove_local_transport(temporary)
        raise


def _remove_local_transport(path: Path) -> None:
    if path.is_symlink():
        raise WebDownloadWorkerError("unsafe HLS transport path")
    if not path.exists():
        return
    if not path.is_file():
        raise WebDownloadWorkerError("unsafe HLS transport path")
    path.unlink()


def _remux_local_video(source: Path, target: Path) -> Path:
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise WebDownloadWorkerError("downloaded HLS transport is invalid")
    temporary = target.with_name(f".{target.name}.remux.part")
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise WebDownloadWorkerError("unsafe remux temporary path")
        temporary.unlink()
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mpegts",
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1800,
        )
        if (
            result.returncode != 0
            or not temporary.is_file()
            or temporary.stat().st_size <= 0
        ):
            raise WebDownloadWorkerError("downloaded media could not be remuxed")
        os.replace(temporary, target)
        source.unlink()
        return target
    except BaseException:
        if temporary.exists() and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise


def worker_youtube_dl_type(yt_dlp_module: object):
    try:
        return allowlisted_youtube_dl_type(yt_dlp_module)
    except SafeMediaError as exc:
        raise WebDownloadWorkerError("secure yt-dlp networking is unavailable") from exc


def require_worker_media_url(url: object, *, resolver: PublicHostResolver) -> str:
    try:
        return require_allowed_media_url(url, resolver=resolver)
    except SafeMediaError as exc:
        raise WebDownloadWorkerError("media URL was rejected") from exc
