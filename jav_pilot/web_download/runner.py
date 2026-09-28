"""Runs download workers as subprocesses and maps their events."""

from __future__ import annotations

import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from ..missav.browser_gate import MISSAV_BROWSER_GATE, BrowserPermit
from .bandwidth import AggregateBandwidthBroker
from .control import normalize_bandwidth_limit
from .policy import LEGACY_EXISTING_POLICY
from .resume import (
    ResumeCheckpointError,
    ResumeIdentity,
    has_resume_checkpoint,
    load_resume_identity,
)
from .variant import DEFAULT_WEB_DOWNLOAD_VARIANT
from .cleanup import path_is_linklike
from .config import WebDownloadConfig
from .errors import WebDownloadConfigError, WebDownloadError, WebDownloadRunnerError
from .jobs import (
    AUTO_PROVIDER,
    DEFAULT_DOWNLOAD_STALL_SECONDS,
    JOB_ID_RE,
    MAX_EVENT_BYTES,
    MAX_PENDING_WORKER_EVENTS,
    MAX_STDOUT_BYTES,
    PROVIDER,
    TERMINAL_STATUSES,
    WEB_DOWNLOAD_PROVIDERS,
    WEB_SOURCE_FAILURE_CODES,
    normalize_web_download_code,
    redact_worker_error,
    validate_web_download_variant,
)

class WebDownloadRunner(Protocol):
    def run(
        self,
        *,
        job: dict[str, object],
        config: WebDownloadConfig,
        on_event: Callable[[dict[str, object]], None],
        cancel_event: threading.Event,
    ) -> None: ...


class MissavManifestProvider(Protocol):
    def capture_manifest(
        self,
        code: object,
        *,
        provider: object,
        variant: object,
        timeout_seconds: float,
        requested_height: object | None,
        quality_strategy: object,
        cancel_event: threading.Event | None = None,
    ) -> object: ...


class SubprocessWebDownloadRunner:
    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_stdout_bytes: int = MAX_STDOUT_BYTES,
        cancel_grace_seconds: float = 5.0,
        download_stall_seconds: float = DEFAULT_DOWNLOAD_STALL_SECONDS,
        capture_retry_delays: Sequence[float] = (1.0, 3.0),
        manifest_provider: MissavManifestProvider | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        default_command = (
            ("python", "-m", "jav_pilot.web_download.worker")
            if manifest_provider is not None
            else ("xvfb-run", "-a", "python", "-m", "jav_pilot.web_download.worker")
        )
        self.command = tuple(command or default_command)
        if not self.command or any(
            not isinstance(part, str) or not part for part in self.command
        ):
            raise WebDownloadConfigError("web download worker command is invalid")
        self.max_event_bytes = max(128, min(int(max_event_bytes), MAX_STDOUT_BYTES))
        self.max_stdout_bytes = max(
            self.max_event_bytes, min(int(max_stdout_bytes), 16 * MAX_STDOUT_BYTES)
        )
        self.cancel_grace_seconds = max(0.1, min(float(cancel_grace_seconds), 30.0))
        try:
            clean_stall_seconds = float(download_stall_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadConfigError(
                "web download stall timeout is invalid"
            ) from exc
        if not math.isfinite(clean_stall_seconds) or not (
            0.1 <= clean_stall_seconds <= 600.0
        ):
            raise WebDownloadConfigError("web download stall timeout is invalid")
        self.download_stall_seconds = clean_stall_seconds
        try:
            retry_delays = tuple(float(delay) for delay in capture_retry_delays)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadConfigError(
                "web download capture retry delays are invalid"
            ) from exc
        if len(retry_delays) > 4 or any(
            not math.isfinite(delay) or not 0 <= delay <= 30 for delay in retry_delays
        ):
            raise WebDownloadConfigError(
                "web download capture retry delays are invalid"
            )
        self.capture_retry_delays = retry_delays
        self.manifest_provider = manifest_provider
        self._popen_factory = popen_factory
        self._bandwidth_lock = threading.Lock()
        self._bandwidth_broker: AggregateBandwidthBroker | None = None

    def configure_bandwidth(self, limit: int) -> None:
        clean_limit = normalize_bandwidth_limit(limit)
        with self._bandwidth_lock:
            if self._bandwidth_broker is None:
                self._bandwidth_broker = AggregateBandwidthBroker(clean_limit)
            else:
                self._bandwidth_broker.update_limit(clean_limit)

    def close_bandwidth(self) -> None:
        with self._bandwidth_lock:
            broker = self._bandwidth_broker
            self._bandwidth_broker = None
        if broker is not None:
            broker.close()

    def _bandwidth_instruction(self) -> dict[str, object]:
        with self._bandwidth_lock:
            broker = self._bandwidth_broker
            return {} if broker is None else broker.config.payload()

    def run(
        self,
        *,
        job: dict[str, object],
        config: WebDownloadConfig,
        on_event: Callable[[dict[str, object]], None],
        cancel_event: threading.Event,
    ) -> None:
        has_durable_checkpoint, resume_identity = _resume_checkpoint_context(
            job,
            config.staging_path,
        )
        capture_provider = _resume_capture_provider(
            job,
            has_durable_checkpoint=has_durable_checkpoint,
        )
        resume_selected_height = (
            resume_identity.selected_height
            if resume_identity is not None
            else job.get("selected_height")
        )
        locked_resume_height = (
            resume_selected_height
            if resume_identity is not None and resume_selected_height is not None
            else None
        )
        instruction = {
            "job_id": str(job["job_id"]),
            "provider": capture_provider,
            "code": str(job["code"]),
            "variant": validate_web_download_variant(
                job.get("variant", DEFAULT_WEB_DOWNLOAD_VARIANT)
            ),
            "requested_height": job.get("requested_height"),
            "quality_strategy": job.get("quality_strategy", "legacy"),
            "resume_selected_height": resume_selected_height,
            "existing_policy": job.get("existing_policy", LEGACY_EXISTING_POLICY),
            "incumbent_output_path": job.get("incumbent_output_path"),
            "incoming_root": config.staging_path,
            "library_root": config.library_path,
            "finalize_lock_path": str(
                config.database_path.with_name("web_downloads.finalize.lock")
            ),
            "min_free_bytes": config.min_free_bytes,
            "max_file_bytes": config.max_file_bytes,
            "capture_timeout_seconds": config.capture_timeout_seconds,
            **self._bandwidth_instruction(),
        }
        payload = (
            json.dumps(instruction, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        attempts = len(self.capture_retry_delays) + 1
        startup_deadline = time.monotonic() + config.capture_timeout_seconds
        for attempt in range(attempts):
            if time.monotonic() >= startup_deadline:
                on_event(_startup_timeout_failure())
                return
            manifest_payload: dict[str, object] | None = None
            if self.manifest_provider is not None:
                try:
                    manifest = self.manifest_provider.capture_manifest(
                        job["code"],
                        provider=capture_provider,
                        variant=job.get("variant", DEFAULT_WEB_DOWNLOAD_VARIANT),
                        timeout_seconds=config.capture_timeout_seconds,
                        requested_height=(
                            locked_resume_height
                            if locked_resume_height is not None
                            else job.get("requested_height")
                        ),
                        quality_strategy=(
                            "selected"
                            if locked_resume_height is not None
                            else job.get("quality_strategy", "legacy")
                        ),
                        cancel_event=cancel_event,
                    )
                    manifest_payload = _manifest_payload_for_worker(manifest)
                    on_event(
                        {
                            "event": "source",
                            "provider": manifest_payload["manifest_provider"],
                        }
                    )
                    if time.monotonic() >= startup_deadline:
                        on_event(_startup_timeout_failure())
                        return
                except Exception as exc:
                    failure = _manifest_capture_failure(exc)
                    if failure is None:
                        raise WebDownloadRunnerError(
                            "MissAV browser capture failed"
                        ) from None
                    if attempt >= len(self.capture_retry_delays):
                        final_failure = dict(failure)
                        final_failure["error"] = (
                            str(
                                final_failure.get("error")
                                or "MissAV browser capture failed"
                            )
                            + f" after {attempts} automatic attempts"
                        )[:500]
                        on_event(final_failure)
                        return
                    remaining = max(0.0, startup_deadline - time.monotonic())
                    if remaining <= 0:
                        on_event(_startup_timeout_failure())
                        return
                    if cancel_event.wait(
                        min(self.capture_retry_delays[attempt], remaining)
                    ):
                        return
                    continue
            failure = self._run_attempt(
                job=job,
                payload=payload,
                manifest_payload=manifest_payload,
                on_event=on_event,
                cancel_event=cancel_event,
                startup_deadline=startup_deadline,
            )
            if failure is None or cancel_event.is_set():
                return
            if attempt >= len(self.capture_retry_delays):
                final_failure = dict(failure)
                message = str(final_failure.get("error") or "MissAV capture failed")
                final_failure["error"] = (
                    f"{message} after {attempts} automatic attempts"
                )[:500]
                on_event(final_failure)
                return
            remaining = max(0.0, startup_deadline - time.monotonic())
            if remaining <= 0:
                on_event(_startup_timeout_failure())
                return
            if cancel_event.wait(min(self.capture_retry_delays[attempt], remaining)):
                return

    def _run_attempt(
        self,
        *,
        job: dict[str, object],
        payload: bytes,
        manifest_payload: dict[str, object] | None,
        on_event: Callable[[dict[str, object]], None],
        cancel_event: threading.Event,
        startup_deadline: float,
    ) -> dict[str, object] | None:
        process: subprocess.Popen[bytes] | None = None
        reader: threading.Thread | None = None
        browser_permit: BrowserPermit | None = None
        output: queue.Queue[tuple[str, object]] = queue.Queue(
            maxsize=MAX_PENDING_WORKER_EVENTS
        )
        reader_stop = threading.Event()
        terminated = False
        retryable_failure: dict[str, object] | None = None
        download_activity_at: float | None = None
        download_started = False
        try:
            if self.manifest_provider is None:
                remaining = max(0.0, startup_deadline - time.monotonic())
                browser_permit = MISSAV_BROWSER_GATE.acquire_download(
                    cancel_event=cancel_event,
                    timeout=remaining,
                )
                if browser_permit is None:
                    return (
                        None
                        if cancel_event.is_set()
                        else _startup_timeout_failure()
                    )
            kwargs: dict[str, object] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "shell": False,
                "bufsize": 0,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            process = self._popen_factory(list(self.command), **kwargs)
            if process.stdin is None or process.stdout is None:
                raise WebDownloadRunnerError("download worker pipes are unavailable")
            if manifest_payload is not None:
                try:
                    instruction = json.loads(payload.decode("utf-8"))
                    if isinstance(instruction, dict):
                        instruction.update(manifest_payload)
                        payload = (
                            json.dumps(
                                instruction, ensure_ascii=True, separators=(",", ":")
                            )
                            + "\n"
                        ).encode("utf-8")
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                    raise WebDownloadRunnerError(
                        "download worker instruction is invalid"
                    )
            process.stdin.write(payload)
            process.stdin.close()

            reader = threading.Thread(
                target=self._read_events,
                args=(process.stdout, output, reader_stop),
                name=f"jav-web-download-output-{str(job['job_id'])[:8]}",
                daemon=True,
            )
            reader.start()
            reader_done = False
            terminal_delivered = False
            while not reader_done or not output.empty():
                if cancel_event.is_set() and process.poll() is None and not terminated:
                    terminated = True
                    self._terminate_process_group(process)
                try:
                    kind, value = output.get(timeout=0.1)
                except queue.Empty:
                    reader_done = reader is not None and not reader.is_alive()
                    if process.poll() is not None and reader_done:
                        break
                    if (
                        not download_started
                        and time.monotonic() >= startup_deadline
                    ):
                        terminated = True
                        self._terminate_process_group(process)
                        retryable_failure = _startup_timeout_failure()
                        break
                    if (
                        download_activity_at is not None
                        and time.monotonic() - download_activity_at
                        >= self.download_stall_seconds
                    ):
                        # The worker is isolated specifically so a blocking network
                        # read can be recovered without leaking a Python thread.  A
                        # fixed, redacted event preserves the durable checkpoint and
                        # moves the job to retry_wait, where it will refresh the
                        # manifest before resuming.
                        terminated = True
                        self._terminate_process_group(process)
                        on_event(
                            {
                                "event": "failed",
                                "status": "failed",
                                "error": "Web media transfer stalled and will retry automatically",
                                "error_code": "media_transport_transient",
                                "retryable": True,
                            }
                        )
                        terminal_delivered = True
                        break
                    continue
                if kind == "event":
                    event_type = _worker_event_type(value)
                    event_status = (
                        str(value.get("status") or "").strip().lower()
                        if isinstance(value, dict)
                        else ""
                    )
                    if event_status == "downloading":
                        download_started = True
                        download_activity_at = time.monotonic()
                    elif event_status in {
                        "verifying",
                        "archiving",
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        download_activity_at = None
                    if (
                        _worker_capture_is_complete(value)
                        and browser_permit is not None
                    ):
                        browser_permit.release()
                        browser_permit = None
                    if _retryable_capture_failure_event(value):
                        if not terminal_delivered:
                            retryable_failure = dict(value)  # type: ignore[arg-type]
                    elif event_type in TERMINAL_STATUSES:
                        if terminal_delivered:
                            continue
                        on_event(value)  # type: ignore[arg-type]
                        terminal_delivered = True
                        retryable_failure = None
                    else:
                        on_event(value)  # type: ignore[arg-type]
                elif kind == "error":
                    raise WebDownloadRunnerError(str(value))
                elif kind == "done":
                    reader_done = True

            return_code = process.wait(timeout=self.cancel_grace_seconds)
            if cancel_event.is_set():
                return None
            if terminal_delivered:
                return None
            if retryable_failure is not None:
                return retryable_failure
            if return_code != 0:
                raise WebDownloadRunnerError("download worker exited unsuccessfully")
            return None
        except WebDownloadError:
            if process is not None and process.poll() is None:
                self._terminate_process_group(process)
            raise
        except Exception as exc:
            if process is not None and process.poll() is None:
                self._terminate_process_group(process)
            raise WebDownloadRunnerError(redact_worker_error(exc)) from exc
        finally:
            reader_stop.set()
            if browser_permit is not None:
                browser_permit.release()
            if process is not None and process.poll() is None:
                self._terminate_process_group(process)
            if reader is not None:
                reader.join(timeout=1.0)
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass

    def _read_events(
        self,
        stream: object,
        output: queue.Queue[tuple[str, object]],
        stop_event: threading.Event,
    ) -> None:
        total = 0
        window_started = time.monotonic()
        try:
            while True:
                raw = stream.readline(self.max_event_bytes + 1)  # type: ignore[attr-defined]
                if not raw:
                    break
                now = time.monotonic()
                if now - window_started >= 60.0:
                    total = 0
                    window_started = now
                total += len(raw)
                if len(raw) > self.max_event_bytes or total > self.max_stdout_bytes:
                    _put_worker_output(
                        output,
                        ("error", "download worker output exceeded its safe limit"),
                        stop_event,
                    )
                    return
                if len(raw) == self.max_event_bytes + 1 and not raw.endswith(b"\n"):
                    _put_worker_output(
                        output,
                        ("error", "download worker event exceeded its safe limit"),
                        stop_event,
                    )
                    return
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    _put_worker_output(
                        output,
                        ("error", "download worker emitted an invalid JSON event"),
                        stop_event,
                    )
                    return
                if not isinstance(event, dict):
                    _put_worker_output(
                        output,
                        ("error", "download worker event must be a JSON object"),
                        stop_event,
                    )
                    return
                if not _put_worker_output(output, ("event", event), stop_event):
                    return
        except Exception:
            _put_worker_output(
                output,
                ("error", "download worker output could not be read"),
                stop_event,
            )
        finally:
            _put_worker_output(output, ("done", None), stop_event)

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError, ValueError):
            try:
                process.terminate()
            except OSError:
                return
        try:
            process.wait(timeout=self.cancel_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, ValueError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _put_worker_output(
    output: queue.Queue[tuple[str, object]],
    item: tuple[str, object],
    stop_event: threading.Event,
) -> bool:
    while not stop_event.is_set():
        try:
            output.put(item, timeout=0.1)
            return True
        except queue.Full:
            continue
    return False


def _worker_capture_is_complete(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    event_type = _worker_event_type(value)
    status = str(value.get("status") or "").strip().lower()
    return event_type in {"status", "progress"} and status in {
        "validating",
        "downloading",
        "verifying",
        "archiving",
    }


def _manifest_payload_for_worker(manifest: object) -> dict[str, object]:
    """Convert an in-memory capture into the anonymous worker pipe payload."""
    url = getattr(manifest, "url", None)
    headers = getattr(manifest, "headers", None)
    selected_height = getattr(manifest, "selected_height", None)
    media_kind = str(getattr(manifest, "media_kind", "hls") or "").strip().lower()
    provider = str(getattr(manifest, "provider", PROVIDER) or "").strip().lower()
    if not isinstance(url, str) or not isinstance(headers, dict):
        raise WebDownloadRunnerError(
            "MissAV browser capture returned an invalid manifest"
        )
    if len(url) > 8192 or len(headers) > 32:
        raise WebDownloadRunnerError(
            "MissAV browser capture returned an invalid manifest"
        )
    clean_headers: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip().lower()
        value = str(raw_value or "").strip()
        if not name or not value or len(value) > 8192:
            continue
        if any(character in value for character in "\r\n\0"):
            raise WebDownloadRunnerError(
                "MissAV browser capture returned invalid headers"
            )
        clean_headers[name] = value
    if provider not in {"missav", "jable", "supjav"}:
        raise WebDownloadRunnerError("Web source provider is invalid")
    if media_kind not in {"hls", "progressive"}:
        raise WebDownloadRunnerError("Web source media kind is invalid")
    if media_kind == "progressive" and provider != "supjav":
        raise WebDownloadRunnerError("Web source media kind is invalid")
    payload: dict[str, object] = {
        "manifest_url": url,
        "manifest_headers": clean_headers,
        "manifest_provider": provider,
        "manifest_media_kind": media_kind,
    }
    if selected_height is not None:
        payload["manifest_selected_height"] = selected_height
    return payload


def _resume_capture_provider(
    job: dict[str, object],
    *,
    has_durable_checkpoint: bool = False,
) -> str:
    requested = str(job.get("provider") or AUTO_PROVIDER).strip().lower()
    resolved = str(job.get("resolved_provider") or "").strip().lower()
    try:
        checkpoint_bytes = int(job.get("checkpoint_bytes") or 0)
        checkpoint_fragments = int(job.get("checkpoint_fragments") or 0)
    except (TypeError, ValueError, OverflowError):
        checkpoint_bytes = 0
        checkpoint_fragments = 0
    if (
        resolved in WEB_DOWNLOAD_PROVIDERS - {AUTO_PROVIDER}
        and (
            checkpoint_bytes > 0
            or checkpoint_fragments > 0
            or has_durable_checkpoint
        )
    ):
        return resolved
    return requested


def _resume_checkpoint_context(
    job: dict[str, object],
    staging_root: str,
) -> tuple[bool, ResumeIdentity | None]:
    job_id = str(job.get("job_id") or "").strip()
    if JOB_ID_RE.fullmatch(job_id) is None:
        return False, None
    root = Path(staging_root)
    try:
        if path_is_linklike(root) or not root.is_dir():
            return False, None
        job_root = root / job_id
        if path_is_linklike(job_root) or not job_root.is_dir():
            return False, None
        hls_checkpoint = has_resume_checkpoint(job_root)
        progressive_checkpoint = job_root / "progressive.json"
        has_progressive_checkpoint = (
            progressive_checkpoint.is_file()
            and not path_is_linklike(progressive_checkpoint)
        )
        if not hls_checkpoint:
            return has_progressive_checkpoint, None
        try:
            identity = load_resume_identity(job_root)
        except ResumeCheckpointError:
            return True, None
    except OSError:
        return False, None
    if identity is None:
        return True, None
    try:
        code, code_key = normalize_web_download_code(job.get("code"))
        variant = validate_web_download_variant(
            job.get("variant", DEFAULT_WEB_DOWNLOAD_VARIANT)
        )
    except WebDownloadError:
        return True, None
    requested_height = job.get("requested_height")
    if requested_height is not None and type(requested_height) is not int:
        return True, None
    if (
        identity.code != code
        or identity.code_key != code_key
        or identity.variant != variant
        or identity.requested_height != requested_height
    ):
        return True, None
    return True, identity


def _startup_timeout_failure() -> dict[str, object]:
    return {
        "event": "failed",
        "status": "failed",
        "error": "Web source discovery did not reach media transfer before the startup deadline",
        "error_code": "missav_navigation_timeout",
        "retryable": True,
    }


def _manifest_capture_failure(error: BaseException) -> dict[str, object] | None:
    """Map broker failures to bounded worker events without exposing details."""
    if error.__class__.__module__.endswith(".web_download.providers"):
        code = str(getattr(error, "code", "") or "upstream_unavailable").strip().lower()
        if code not in WEB_SOURCE_FAILURE_CODES:
            code = "upstream_unavailable"
        not_found = code == "not_found"
        return {
            "event": "failed",
            "status": "failed",
            "error": (
                "No configured Web download site has an exact result for this catalog code"
                if not_found
                else "All configured Web download sites are currently unavailable"
            ),
            "error_code": f"web_source_{code}",
            "retryable": bool(getattr(error, "retryable", True)) and not not_found,
        }
    if error.__class__.__name__ in {
        "BrowserServiceClosed",
        "BrowserOperationCancelled",
    }:
        return None
    code = str(getattr(error, "code", "") or "").strip().lower()
    status_code = getattr(error, "status_code", None)
    if not code:
        code = {
            403: "challenge_active",
            429: "rate_limited",
            503: "upstream_unavailable",
        }.get(status_code, "")
    if code == "not_found":
        return {
            "event": "failed",
            "status": "failed",
            "error": "MissAV has no exact result for this catalog code",
            "error_code": "missav_not_found",
            "retryable": False,
        }
    if code in {
        "challenge_active",
        "rate_limited",
        "upstream_unavailable",
        "navigation_timeout",
        "discovery_unavailable",
    } or error.__class__.__name__ in {
        "BrowserUpstreamError",
        "BrowserChallengeActive",
    }:
        message = {
            "challenge_active": "MissAV is waiting for an upstream challenge to clear",
            "rate_limited": "MissAV is temporarily rate limited",
            "navigation_timeout": "MissAV navigation timed out",
            "discovery_unavailable": "MissAV resource discovery is temporarily unavailable",
        }.get(code, "MissAV browser capture is temporarily unavailable")
        return {
            "event": "failed",
            "status": "failed",
            "error": message,
            "error_code": f"missav_{code or 'transient'}",
            "retryable": True,
        }
    if error.__class__.__name__ in {
        "BrowserOperationFailed",
        "BrowserRestartExhausted",
    }:
        return {
            "event": "failed",
            "status": "failed",
            "error": "MissAV browser capture is temporarily unavailable",
            "error_code": "transient_browser_failure",
            "retryable": True,
        }
    return None


def _retryable_capture_failure_event(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    event_type = _worker_event_type(value)
    if event_type != "failed":
        return False
    if value.get("retryable") is True:
        return True
    if str(value.get("error_code") or "").startswith("missav_"):
        return True
    message = str(value.get("error") or "").strip().casefold()
    return any(
        marker in message
        for marker in (
            "missav browser capture failed",
            "missav browser capture timed out",
            "missav browser exited unexpectedly during capture",
            "missav browser could not start",
            "missav capture timed out",
            "missav series search is temporarily unavailable",
            "missav did not expose a downloadable hls stream",
            "missav quality controls are unavailable",
        )
    )


def _worker_event_type(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("type") or value.get("event") or "").strip().lower()
