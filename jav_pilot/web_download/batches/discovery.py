"""MissAV series discovery through a worker subprocess or the browser broker."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError
from typing import Sequence

from ...missav import models as missav_models
from ..errors import WebDownloadConfigError, WebDownloadError
from ..jobs import normalize_web_download_code
from ..variant import MissavVariant
from .errors import WebDownloadBatchError, WebDownloadBatchTransientError
from .models import DISCOVERY_LIMIT
from .validation import normalize_available_variants

_MAX_DISCOVERY_OUTPUT_BYTES = 64 * 1024
_DISCOVERY_PROTOCOL_VERSION = 2
_DISCOVERY_TRANSIENT_ERROR_CODES = frozenset(
    {
        "challenge_active",
        "discovery_unavailable",
        "discovery_timeout",
        "navigation_timeout",
        "rate_limited",
        "transient_browser_failure",
        "upstream_unavailable",
        "worker_exit",
    }
)
_DISCOVERY_DETERMINISTIC_ERROR_CODES = frozenset(
    {
        "dependency_unavailable",
        # Older workers used this code for an unavailable discovery backend.
        # Accept it as deterministic too; _retryable_discovery_error() treats
        # it as recoverable when it is received without a retryable flag.
        "discovery_unavailable",
        "internal_failure",
        "invalid_instruction",
        "not_found",
        "parse_drift",
        "route_drift",
        "safety_rejected",
        "worker_protocol",
    }
)


def retryable_discovery_error(error: BaseException) -> bool:
    failure_code = getattr(error, "failure_code", None)
    if (
        failure_code in _DISCOVERY_DETERMINISTIC_ERROR_CODES
        and failure_code not in _DISCOVERY_TRANSIENT_ERROR_CODES
    ):
        # A malformed caller must not turn authoritative absence or a safety
        # rejection into an automatic retry merely by choosing the transient
        # exception subclass.
        return False
    # The worker's retryable flag is represented by the transient exception
    # type.  In particular, a legacy ``discovery_unavailable`` response with
    # retryable=false remains deterministic; only an explicitly retryable
    # response is allowed into this loop.
    return isinstance(error, WebDownloadBatchTransientError)


def _reject_duplicate_protocol_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _unstructured_worker_error(*, unexpected_exit: bool) -> WebDownloadBatchError:
    if unexpected_exit:
        return WebDownloadBatchTransientError(
            "MissAV batch discovery failed",
            failure_code="worker_exit",
        )
    return WebDownloadBatchError(
        "MissAV batch worker output is invalid",
        failure_code="worker_protocol",
    )


class SubprocessMissavSeriesDiscoverer:
    def __init__(self, command: Sequence[str] | None = None) -> None:
        self.command = tuple(
            command
            or (
                "xvfb-run",
                "-a",
                sys.executable,
                "-m",
                "jav_pilot.missav.batch_worker",
            )
        )
        if not self.command or any(
            not isinstance(part, str) or not part for part in self.command
        ):
            raise WebDownloadConfigError("MissAV batch worker command is invalid")

    def __call__(
        self,
        prefix: str,
        *,
        suffix_width: int | None,
        start: int | None,
        end: int | None,
        timeout_seconds: float,
        cancel_event: threading.Event,
    ) -> missav_models.MissavSeriesDiscovery:
        instruction = {
            "prefix": prefix,
            "suffix_width": suffix_width,
            "start": start,
            "end": end,
            "max_codes": DISCOVERY_LIMIT,
            "timeout_seconds": timeout_seconds,
        }
        payload = (
            json.dumps(instruction, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        process: subprocess.Popen[bytes] | None = None
        reader: threading.Thread | None = None
        output = bytearray()
        reader_done = threading.Event()
        try:
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
            process = subprocess.Popen(list(self.command), **kwargs)
            if process.stdin is None or process.stdout is None:
                raise WebDownloadBatchError("MissAV batch worker pipes are unavailable")
            process.stdin.write(payload)
            process.stdin.close()

            def read_output() -> None:
                try:
                    descriptor = process.stdout.fileno()
                    while len(output) <= _MAX_DISCOVERY_OUTPUT_BYTES:
                        chunk = os.read(
                            descriptor,
                            min(4096, _MAX_DISCOVERY_OUTPUT_BYTES + 1 - len(output)),
                        )
                        if not chunk:
                            break
                        output.extend(chunk)
                        # The worker protocol is one JSON line.  Parsing may
                        # proceed as soon as that frame is complete; it must
                        # not depend on EOF from browser grandchildren that
                        # accidentally inherited stdout.
                        if b"\n" in chunk:
                            break
                finally:
                    reader_done.set()

            reader = threading.Thread(
                target=read_output,
                name="jav-web-download-batch-output",
                daemon=True,
            )
            reader.start()
            deadline = time.monotonic() + max(1.0, float(timeout_seconds)) + 15.0
            while process.poll() is None:
                if cancel_event.is_set():
                    _terminate_process_group(process)
                    raise WebDownloadBatchError("batch discovery was cancelled")
                if reader_done.is_set() and len(output) > _MAX_DISCOVERY_OUTPUT_BYTES:
                    _terminate_process_group(process)
                    raise WebDownloadBatchError("MissAV batch worker output is invalid")
                if time.monotonic() >= deadline:
                    _terminate_process_group(process)
                    raise WebDownloadBatchTransientError(
                        "MissAV batch discovery timed out",
                        failure_code="discovery_timeout",
                    )
                cancel_event.wait(0.05)
            reader_done.wait(2.0)
            if reader is not None:
                reader.join(timeout=0.1)
            if len(output) > _MAX_DISCOVERY_OUTPUT_BYTES:
                raise WebDownloadBatchError("MissAV batch worker output is invalid")
            unexpected_exit = process.returncode != 0
            framed_output = bytes(output)
            if framed_output.count(b"\n") != 1 or not framed_output.endswith(b"\n"):
                raise _unstructured_worker_error(unexpected_exit=unexpected_exit)
            encoded_result = framed_output[:-1]
            if encoded_result.endswith(b"\r"):
                encoded_result = encoded_result[:-1]
            if not encoded_result or b"\r" in encoded_result:
                raise _unstructured_worker_error(unexpected_exit=unexpected_exit)
            try:
                result = json.loads(
                    encoded_result.decode("ascii"),
                    object_pairs_hook=_reject_duplicate_protocol_keys,
                    parse_constant=_reject_non_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise _unstructured_worker_error(
                    unexpected_exit=unexpected_exit
                ) from None
            if not isinstance(result, dict):
                raise _unstructured_worker_error(unexpected_exit=unexpected_exit)
            if (
                type(result.get("protocol")) is not int
                or result.get("protocol") != _DISCOVERY_PROTOCOL_VERSION
            ):
                raise WebDownloadBatchError("MissAV batch worker output is invalid")
            if result.get("ok") is False:
                if set(result) != {"protocol", "ok", "error"}:
                    raise WebDownloadBatchError("MissAV batch worker output is invalid")
                error = result.get("error")
                if not isinstance(error, dict) or set(error) != {
                    "code",
                    "retryable",
                }:
                    raise WebDownloadBatchError("MissAV batch worker output is invalid")
                error_code = error.get("code")
                retryable = error.get("retryable")
                if (
                    not isinstance(error_code, str)
                    or not isinstance(retryable, bool)
                    or error_code
                    not in (
                        _DISCOVERY_TRANSIENT_ERROR_CODES
                        if retryable
                        else _DISCOVERY_DETERMINISTIC_ERROR_CODES
                    )
                ):
                    raise WebDownloadBatchError("MissAV batch worker output is invalid")
                if retryable:
                    raise WebDownloadBatchTransientError(
                        "MissAV batch discovery failed",
                        failure_code=error_code,
                    )
                raise WebDownloadBatchError(
                    "MissAV batch discovery failed",
                    failure_code=error_code,
                )
            if (
                set(result) != {"protocol", "ok", "codes", "items", "complete"}
                or result.get("ok") is not True
                or not isinstance(result.get("codes"), list)
                or not isinstance(result.get("items"), list)
                or len(result["codes"]) > DISCOVERY_LIMIT
                or len(result["items"]) != len(result["codes"])
                or any(not isinstance(code, str) for code in result["codes"])
                or not isinstance(result.get("complete"), bool)
            ):
                raise WebDownloadBatchError("MissAV batch worker output is invalid")
            variants_by_code: list[tuple[str, tuple[MissavVariant, ...]]] = []
            seen_code_keys: set[str] = set()
            for expected_code, item in zip(
                result["codes"],
                result["items"],
                strict=True,
            ):
                if (
                    not isinstance(item, dict)
                    or set(item) != {"code", "variants"}
                    or item.get("code") != expected_code
                ):
                    raise WebDownloadBatchError("MissAV batch worker output is invalid")
                try:
                    _display, code_key = normalize_web_download_code(expected_code)
                except WebDownloadError:
                    raise WebDownloadBatchError(
                        "MissAV batch worker output is invalid"
                    ) from None
                if code_key in seen_code_keys:
                    raise WebDownloadBatchError("MissAV batch worker output is invalid")
                seen_code_keys.add(code_key)
                variants_by_code.append(
                    (
                        str(expected_code),
                        normalize_available_variants(item.get("variants")),
                    )
                )
            if unexpected_exit:
                raise WebDownloadBatchTransientError(
                    "MissAV batch discovery failed",
                    failure_code="worker_exit",
                )
            return missav_models.MissavSeriesDiscovery(
                codes=tuple(result["codes"]),
                complete=result["complete"],
                variants_by_code=tuple(variants_by_code),
            )
        except WebDownloadBatchError:
            raise
        except OSError:
            if process is not None:
                raise WebDownloadBatchTransientError(
                    "MissAV batch discovery failed",
                    failure_code="worker_exit",
                ) from None
            raise WebDownloadBatchError(
                "MissAV batch discovery failed",
                failure_code="dependency_unavailable",
            ) from None
        except Exception:
            raise WebDownloadBatchError(
                "MissAV batch discovery failed",
                failure_code="internal_failure",
            ) from None
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
            if reader is not None:
                reader.join(timeout=1.0)
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass


class BrokerMissavSeriesDiscoverer:
    """Discover batch codes through the shared trusted MissAV page."""

    def __init__(self, runtime: object | None = None) -> None:
        self._runtime = runtime

    def __call__(
        self,
        prefix: str,
        *,
        suffix_width: int | None,
        start: int | None,
        end: int | None,
        timeout_seconds: float,
        cancel_event: threading.Event,
    ) -> missav_models.MissavSeriesDiscovery:
        from ...missav.browser_runtime import (
            get_missav_browser_runtime,
            wait_for_browser_task,
        )
        from ...missav.browser_service import (
            BrowserChallengeActive,
            BrowserOperationCancelled,
            BrowserOperationError,
            BrowserOperationFailed,
            BrowserUpstreamError,
            BrowserRestartExhausted,
            BrowserServiceClosed,
        )

        runtime = self._runtime or get_missav_browser_runtime()
        try:
            task = runtime.submit(
                "series_discovery",
                {
                    "prefix": prefix,
                    "suffix_width": suffix_width,
                    "start": start,
                    "end": end,
                    "max_codes": DISCOVERY_LIMIT,
                    "timeout_seconds": timeout_seconds,
                },
            )
            result = wait_for_browser_task(
                task,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                timeout_message="MissAV batch discovery timed out",
                timeout_code="navigation_timeout",
            )
            if not isinstance(result, missav_models.MissavSeriesDiscovery):
                raise WebDownloadBatchError(
                    "MissAV batch discovery returned an invalid result",
                    failure_code="worker_protocol",
                )
            return result
        except (BrowserOperationCancelled, CancelledError):
            raise WebDownloadBatchError(
                "batch discovery was cancelled"
            ) from None
        except BrowserChallengeActive as exc:
            # Challenge state is already a bounded, redacted signal from the
            # broker; preserve its public code rather than collapsing it into
            # a generic upstream failure.
            del exc
            raise WebDownloadBatchTransientError(
                "MissAV batch discovery failed",
                failure_code="challenge_active",
            ) from None
        except BrowserUpstreamError as exc:
            # BrowserUpstreamError intentionally exposes no URL/cookie/header.
            # Derive only a stable public code from its status (or an already
            # validated code supplied by a compatible broker implementation).
            failure_code = getattr(exc, "code", None)
            if not isinstance(failure_code, str) or failure_code not in {
                "challenge_active",
                "discovery_unavailable",
                "navigation_timeout",
                "rate_limited",
                "upstream_unavailable",
            }:
                failure_code = (
                    "rate_limited"
                    if exc.status_code == 429
                    else (
                        "challenge_active"
                        if exc.status_code == 403
                        else "upstream_unavailable"
                    )
                )
            raise WebDownloadBatchTransientError(
                "MissAV batch discovery failed",
                failure_code=failure_code,
            ) from None
        except BrowserOperationError as exc:
            transient = exc.code in {
                "challenge_active",
                "rate_limited",
                "navigation_timeout",
                "upstream_unavailable",
                "discovery_unavailable",
            }
            error_type = (
                WebDownloadBatchTransientError if transient else WebDownloadBatchError
            )
            raise error_type(
                "MissAV batch discovery failed",
                failure_code=exc.code,
            ) from None
        except (BrowserOperationFailed, BrowserRestartExhausted) as exc:
            del exc
            raise WebDownloadBatchTransientError(
                "MissAV batch discovery failed",
                failure_code="transient_browser_failure",
            ) from None
        except BrowserServiceClosed:
            if cancel_event.is_set():
                raise WebDownloadBatchError(
                    "batch discovery was cancelled"
                ) from None
            raise WebDownloadBatchTransientError(
                "MissAV batch discovery failed",
                failure_code="dependency_unavailable",
            ) from None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2.0)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        pass
