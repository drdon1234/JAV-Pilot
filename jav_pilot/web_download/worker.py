"""Entry point of the web download worker process (python -m jav_pilot.web_download.worker)."""

from __future__ import annotations

import json
import signal
import sys
import threading
from pathlib import Path

from ..missav.client import capture_manifest
from ..missav.errors import MissavError, MissavNotFound
from .media import media_provider_scope
from .policy import LEGACY_EXISTING_POLICY, is_strict_quality_upgrade
from .quality import QualityHeightError, validate_quality_height
from .resume import has_resume_checkpoint
from .archive_writer import archive_video, resolve_policy_archive_target
from .hls import download_manifest
from .progressive import download_progressive
from .verify import verify_video
from .worker_common import (
    MAX_TASK_BYTES,
    PROGRESSIVE_CHECKPOINT_NAME,
    URL_RE,
    raise_if_cancelled,
)
from .worker_errors import (
    WebDownloadWorkerCancelled,
    WebDownloadWorkerDiskLowError,
    WebDownloadWorkerError,
    WebDownloadWorkerTransientTransportError,
)
from .worker_files import (
    exclusive_finalize_lock,
    prepare_job_dir,
    prepare_root,
    remove_job_dir,
    require_free_space,
)
from .worker_task import JsonEventEmitter, WorkerTask

def _has_progressive_checkpoint(job_dir: Path) -> bool:
    """Return whether a resumable progressive checkpoint lives in ``job_dir``.

    Progressive (direct-MP4) downloads persist ``progressive.json`` alongside a
    ``*.progressive.part`` payload instead of the HLS ``resume.json`` that
    :func:`has_resume_checkpoint` recognises. Transient/cancel cleanup must
    honour both so an interrupted progressive transfer can resume from its
    partial byte range rather than restart from zero.
    """

    path = job_dir / PROGRESSIVE_CHECKPOINT_NAME
    return path.is_file() and not path.is_symlink()


def _has_resumable_state(job_dir: Path) -> bool:
    """Return whether ``job_dir`` holds any resumable checkpoint (HLS or progressive)."""

    return has_resume_checkpoint(job_dir) or _has_progressive_checkpoint(job_dir)


def run_task(
    task: WorkerTask,
    *,
    emitter: JsonEventEmitter | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    with media_provider_scope(task.provider):
        return _run_task_scoped(task, emitter=emitter, cancel_event=cancel_event)


def _run_task_scoped(
    task: WorkerTask,
    *,
    emitter: JsonEventEmitter | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    events = emitter or JsonEventEmitter()
    cancelled = cancel_event or threading.Event()
    raise_if_cancelled(cancelled)
    incoming_root = prepare_root(task.incoming_root)
    library_root = prepare_root(task.library_root)
    require_free_space(incoming_root, task.min_free_bytes, volume_key="staging")
    require_free_space(library_root, task.min_free_bytes, volume_key="library")
    job_dir = prepare_job_dir(incoming_root, task.job_id)

    try:
        events.status("locating")
        events.progress(status="locating", progress=1.0, force=True)
        locked_resume_height = (
            task.resume_selected_height
            if has_resume_checkpoint(job_dir)
            else None
        )
        manifest = task.manifest
        if manifest is None:
            manifest = capture_manifest(
                task.code,
                variant=task.variant,
                timeout_seconds=task.capture_timeout_seconds,
                profile_parent=job_dir,
                requested_height=(locked_resume_height or task.requested_height),
                quality_strategy=(
                    "selected"
                    if locked_resume_height is not None
                    else task.quality_strategy
                ),
            )
        raise_if_cancelled(cancelled)
        selected_height: int | None = None
        if manifest.selected_height is None:
            if task.quality_strategy != "legacy":
                raise WebDownloadWorkerError(
                    "MissAV did not confirm the selected video quality"
                )
        else:
            try:
                selected_height = validate_quality_height(manifest.selected_height)
            except QualityHeightError as exc:
                raise WebDownloadWorkerError(
                    "MissAV selected video quality is invalid"
                ) from exc
            if (
                task.quality_strategy == "selected"
                and selected_height != task.requested_height
            ):
                raise WebDownloadWorkerError(
                    "MissAV selected video quality did not match the request"
                )
            if (
                task.quality_strategy == "highest"
                and task.requested_height is not None
                and selected_height > task.requested_height
            ):
                raise WebDownloadWorkerError(
                    "MissAV selected video quality exceeded the requested ceiling"
                )
            events.emit("quality", selected_height=selected_height)

        events.status("validating")
        events.progress(status="validating", progress=6.0, force=True)
        if task.existing_policy != LEGACY_EXISTING_POLICY:
            with exclusive_finalize_lock(task.finalize_lock_path, cancelled):
                archive_target = resolve_policy_archive_target(
                    library_root,
                    task.code,
                    task.job_id,
                    task.incumbent_output_path,
                    variant=task.variant,
                )
        else:
            archive_target = None
        if (
            task.existing_policy in {"higher_quality", "skip"}
            and archive_target is not None
            and archive_target.identity is not None
        ):
            if task.existing_policy == "higher_quality" and selected_height is None:
                raise WebDownloadWorkerError(
                    "MissAV did not confirm the candidate video quality"
                )
            incumbent_height = verify_video(archive_target.path)
            if task.existing_policy == "skip" or (
                selected_height is not None
                and not is_strict_quality_upgrade(selected_height, incumbent_height)
            ):
                output_size = archive_target.path.stat().st_size
                try:
                    remove_job_dir(job_dir, incoming_root)
                except (OSError, WebDownloadWorkerError):
                    pass
                events.emit(
                    "completed",
                    status="completed",
                    progress=100.0,
                    downloaded_bytes=output_size,
                    total_bytes=output_size,
                    output_path=archive_target.path.relative_to(
                        library_root
                    ).as_posix(),
                    verified_height=incumbent_height,
                    publication_outcome="kept_existing",
                )
                return archive_target.path

        verified_height: int | None = None
        publication_outcome = (
            "replaced"
            if archive_target is not None and archive_target.identity is not None
            else "published"
        )

        def finalize_download(downloaded: Path) -> Path:
            nonlocal verified_height
            events.status("verifying")
            events.progress(
                status="verifying",
                progress=93.0,
                downloaded_bytes=downloaded.stat().st_size,
                total_bytes=downloaded.stat().st_size,
                force=True,
            )
            verified_height = verify_video(
                downloaded,
                selected_height=manifest.selected_height,
            )
            raise_if_cancelled(cancelled)
            downloaded_size = downloaded.stat().st_size
            if downloaded_size > task.max_file_bytes:
                raise WebDownloadWorkerError(
                    "downloaded media exceeds the configured size limit"
                )
            events.status("archiving")
            require_free_space(
                library_root,
                task.min_free_bytes + downloaded_size,
                volume_key="library",
            )
            output = archive_video(
                downloaded,
                library_root,
                task.code,
                task.job_id,
                events,
                cancelled,
                replace_target=archive_target,
                variant=task.variant,
            )
            try:
                remove_job_dir(job_dir, incoming_root)
            except (OSError, WebDownloadWorkerError):
                pass
            return output

        download_options: dict[str, object] = {
            "finalize_lock_path": task.finalize_lock_path,
            "requested_height": task.requested_height,
            "selected_height": selected_height,
            "variant": task.variant,
            "finalize": finalize_download,
        }
        if task.bandwidth is not None:
            download_options["bandwidth_config"] = task.bandwidth
        if task.media_kind == "progressive":
            if task.quality_strategy != "legacy" or selected_height is not None:
                raise WebDownloadWorkerError(
                    "progressive media does not expose a verified quality"
                )
            output_path = download_progressive(
                manifest.url,
                manifest.headers,
                job_dir,
                task.code,
                events,
                cancelled,
                task.max_file_bytes,
                task.min_free_bytes,
                finalize_lock_path=task.finalize_lock_path,
                bandwidth_config=task.bandwidth,
                finalize=finalize_download,
            )
        else:
            output_path = download_manifest(
                manifest.url,
                manifest.headers,
                job_dir,
                task.code,
                events,
                cancelled,
                task.max_file_bytes,
                task.min_free_bytes,
                **download_options,  # type: ignore[arg-type]
            )
        # ``finalize`` has already atomically published the archive.  A late
        # cancellation must not relabel that durable success as cancelled,
        # otherwise the library contains an orphan while the queue retries.
        output_size = output_path.stat().st_size
        events.emit(
            "completed",
            status="completed",
            progress=100.0,
            downloaded_bytes=output_size,
            total_bytes=output_size,
            output_path=output_path.relative_to(library_root).as_posix(),
            verified_height=verified_height,
            publication_outcome=publication_outcome,
        )
        return output_path
    except BaseException:
        if job_dir.exists() and not _has_resumable_state(job_dir):
            try:
                remove_job_dir(job_dir, incoming_root)
            except (OSError, WebDownloadWorkerError):
                pass
        raise


def _redact_error(error: BaseException) -> str:
    if isinstance(error, WebDownloadWorkerCancelled):
        return "web download cancelled"
    if isinstance(error, MissavNotFound):
        return "MissAV has no exact result for this catalog code"
    if isinstance(error, MissavError):
        code = str(getattr(error, "code", "") or "")
        return {
            "challenge_active": "MissAV is waiting for an upstream challenge to clear",
            "rate_limited": "MissAV is temporarily rate limited",
            "navigation_timeout": "MissAV navigation timed out",
            "upstream_unavailable": "MissAV browser capture is temporarily unavailable",
            "discovery_unavailable": "MissAV resource discovery is temporarily unavailable",
        }.get(code, "MissAV media lookup failed")
    if isinstance(error, WebDownloadWorkerError):
        message = str(error).strip() or "web download failed"
    else:
        message = "web download failed unexpectedly"
    message = URL_RE.sub("[redacted URL]", message)
    return message[:500]


def _read_task_from_stdin() -> WorkerTask:
    raw = sys.stdin.buffer.read(MAX_TASK_BYTES + 1)
    if len(raw) > MAX_TASK_BYTES:
        raise WebDownloadWorkerError("worker task is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebDownloadWorkerError("worker task is invalid JSON") from exc
    return WorkerTask.from_payload(payload)


def main() -> int:
    emitter = JsonEventEmitter()
    cancel_event = threading.Event()

    def request_cancel(signum: int, frame: object) -> None:
        if cancel_event.is_set():
            return
        cancel_event.set()
        raise WebDownloadWorkerCancelled("web download cancelled")

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, request_cancel)

    try:
        task = _read_task_from_stdin()
        run_task(task, emitter=emitter, cancel_event=cancel_event)
        return 0
    except WebDownloadWorkerCancelled:
        return 2
    except BaseException as exc:  # noqa: BLE001 - worker must return a bounded failure event.
        failure: dict[str, object] = {
            "status": "failed",
            "error": _redact_error(exc),
        }
        if isinstance(exc, WebDownloadWorkerDiskLowError):
            failure.update(error_code="disk_low", volume_key=exc.volume_key)
        elif isinstance(exc, WebDownloadWorkerTransientTransportError):
            failure["error_code"] = "media_transport_transient"
            if exc.checkpoint_bytes is not None:
                failure.update(
                    checkpoint_bytes=exc.checkpoint_bytes,
                    checkpoint_fragments=exc.checkpoint_fragments,
                )
                if exc.checkpoint_reset:
                    failure["checkpoint_reset"] = True
        elif isinstance(exc, MissavNotFound):
            failure["error_code"] = "missav_not_found"
        elif isinstance(exc, MissavError):
            code = str(getattr(exc, "code", "") or "upstream_unavailable")
            failure["error_code"] = f"missav_{code}"
            failure["retryable"] = bool(getattr(exc, "retryable", False))
        emitter.emit("failed", **failure)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
