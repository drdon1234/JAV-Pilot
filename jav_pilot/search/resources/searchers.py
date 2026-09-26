"""Resource searchers backed by a worker subprocess or the MissAV browser broker."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence

from .errors import (
    ResourceSearchCancelledError,
    ResourceSearchError,
    ResourceSearchUnavailableError,
)
from .models import (
    MAX_WORKER_EVENTS,
    MAX_WORKER_INPUT_BYTES,
    MAX_WORKER_LINE_BYTES,
    MAX_WORKER_OUTPUT_BYTES,
    RESOURCE_SEARCH_PROTOCOL_VERSION,
    RESOURCE_SEARCH_SOURCE_IDS,
    ResourceSearchDiscoverer,
    ResourceSearchHeartbeatEvent,
    ResourceSearchItem,
    ResourceSearchPageEvent,
    ResourceSearchStartedEvent,
    ResourceSearchState,
    ResourceSearchWork,
    ResourceSearchWorkerEvent,
    ResourceSearchWorkerResult,
)
from .protocol import ResourceSearchWorkerError, decode_worker_message, state_payload
from .validation import validate_work

__all__ = [
    "RoutedResourceSearcher",
    "BrokerMissavResourceSearcher",
    "SubprocessMissavResourceSearcher",
]


class RoutedResourceSearcher:
    """Dispatch one persisted search session to its configured site adapter."""

    def __init__(self, discoverers: Mapping[str, ResourceSearchDiscoverer]) -> None:
        configured = dict(discoverers)
        if set(configured) != set(RESOURCE_SEARCH_SOURCE_IDS) or any(
            not callable(discoverer) for discoverer in configured.values()
        ):
            raise ResourceSearchError("resource search adapters are invalid")
        self._discoverers = configured

    def __call__(
        self,
        work: ResourceSearchWork,
        *,
        on_event: Callable[[ResourceSearchWorkerEvent], None],
        cancel_event: threading.Event,
    ) -> ResourceSearchWorkerResult:
        clean_work = validate_work(work)
        return self._discoverers[clean_work.source_id](
            clean_work,
            on_event=on_event,
            cancel_event=cancel_event,
        )


class SubprocessMissavResourceSearcher:
    def __init__(self, command: Sequence[str] | None = None) -> None:
        self.command = tuple(
            command
            or (
                "xvfb-run",
                "-a",
                sys.executable,
                "-m",
                "jav_pilot.missav.resource_worker",
            )
        )
        if not self.command or any(
            not isinstance(part, str) or not part for part in self.command
        ):
            raise ResourceSearchError("resource search worker command is invalid")

    def __call__(
        self,
        work: ResourceSearchWork,
        *,
        on_event: Callable[[ResourceSearchWorkerEvent], None],
        cancel_event: threading.Event,
    ) -> ResourceSearchWorkerResult:
        clean_work = validate_work(work)
        if clean_work.result_limit < 1:
            raise ResourceSearchError("resource search worker limit is invalid")
        instruction = {
            "protocol": RESOURCE_SEARCH_PROTOCOL_VERSION,
            "source_id": clean_work.source_id,
            "query": clean_work.query,
            "result_limit": clean_work.result_limit,
            "suffix_width": clean_work.suffix_width,
            "start": clean_work.start,
            "end": clean_work.end,
            "state": state_payload(clean_work.state),
            "timeout_seconds": 120,
        }
        payload = (
            json.dumps(instruction, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        if len(payload) > MAX_WORKER_INPUT_BYTES:
            raise ResourceSearchError("resource search worker input is too large")
        process: subprocess.Popen[bytes] | None = None
        reader: threading.Thread | None = None
        reader_stop = threading.Event()
        output_queue: queue.Queue[bytes | BaseException | None] = queue.Queue(
            maxsize=16
        )
        result: ResourceSearchWorkerResult | None = None
        structured_error: ResourceSearchWorkerError | None = None
        saw_started = False
        stream_state: ResourceSearchState | None = None
        event_count = 0
        output_bytes = 0
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
                raise ResourceSearchUnavailableError(
                    "resource search worker pipes are unavailable"
                )
            process.stdin.write(payload)
            process.stdin.close()

            def relay_output(value: bytes | BaseException | None) -> bool:
                while not reader_stop.is_set():
                    try:
                        output_queue.put(value, timeout=0.1)
                        return True
                    except queue.Full:
                        continue
                return False

            def read_output() -> None:
                try:
                    while True:
                        line = process.stdout.readline(MAX_WORKER_LINE_BYTES + 1)
                        if not line:
                            break
                        if len(line) > MAX_WORKER_LINE_BYTES:
                            raise ResourceSearchError(
                                "resource search worker output is too large"
                            )
                        if not relay_output(line):
                            return
                except BaseException as exc:  # noqa: BLE001 - relayed safely.
                    relay_output(exc)
                finally:
                    relay_output(None)

            reader = threading.Thread(
                target=read_output,
                name="jav-resource-search-output",
                daemon=True,
            )
            reader.start()
            deadline = time.monotonic() + 135.0
            eof = False
            while not eof:
                if cancel_event.is_set():
                    _terminate_process_group(process)
                    raise ResourceSearchCancelledError("resource search was cancelled")
                if time.monotonic() >= deadline:
                    _terminate_process_group(process)
                    raise ResourceSearchWorkerError("timeout", retryable=True)
                try:
                    raw = output_queue.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if raw is None:
                    eof = True
                    continue
                if isinstance(raw, BaseException):
                    raise ResourceSearchWorkerError(
                        "protocol_failure",
                        retryable=True,
                    ) from None
                output_bytes += len(raw)
                event_count += 1
                if (
                    output_bytes > MAX_WORKER_OUTPUT_BYTES
                    or event_count > MAX_WORKER_EVENTS
                ):
                    raise ResourceSearchWorkerError(
                        "protocol_failure",
                        retryable=True,
                    )
                message = decode_worker_message(raw, previous_state=stream_state)
                if result is not None or structured_error is not None:
                    raise ResourceSearchWorkerError(
                        "protocol_failure",
                        retryable=True,
                    )
                if isinstance(message, ResourceSearchStartedEvent):
                    if saw_started:
                        raise ResourceSearchWorkerError(
                            "protocol_failure",
                            retryable=True,
                        )
                    saw_started = True
                    stream_state = message.state
                    on_event(message)
                elif isinstance(
                    message,
                    (ResourceSearchHeartbeatEvent, ResourceSearchPageEvent),
                ):
                    if not saw_started:
                        raise ResourceSearchWorkerError(
                            "protocol_failure",
                            retryable=True,
                        )
                    stream_state = message.state
                    on_event(message)
                elif isinstance(message, ResourceSearchWorkerResult):
                    if not saw_started:
                        raise ResourceSearchWorkerError(
                            "protocol_failure",
                            retryable=True,
                        )
                    stream_state = message.state
                    result = message
                elif isinstance(message, ResourceSearchWorkerError):
                    structured_error = message
                else:
                    raise ResourceSearchWorkerError(
                        "protocol_failure",
                        retryable=True,
                    )
            try:
                return_code = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)
                raise ResourceSearchWorkerError("timeout", retryable=True) from None
            if return_code != 0:
                raise ResourceSearchWorkerError(
                    "protocol_failure",
                    retryable=True,
                )
            if structured_error is not None:
                raise structured_error
            if result is None:
                raise ResourceSearchWorkerError(
                    "protocol_failure",
                    retryable=True,
                )
            return result
        except ResourceSearchWorkerError as exc:
            exc.attach_state(stream_state)
            raise
        except ResourceSearchError:
            raise
        except OSError:
            raise ResourceSearchUnavailableError(
                "resource search worker is unavailable"
            ) from None
        except Exception:
            raise ResourceSearchWorkerError(
                "protocol_failure",
                retryable=True,
            ) from None
        finally:
            reader_stop.set()
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


class BrokerMissavResourceSearcher:
    """Run MissAV resource discovery on the process-wide trusted page.

    The legacy subprocess worker remains available for isolated tests and
    migration tooling. Production search uses this adapter so resource search,
    quality discovery, descriptions, and Web-download capture share one
    browser/context/page and one challenge cooldown window.
    """

    def __init__(self, runtime: object | None = None) -> None:
        self._runtime = runtime

    @staticmethod
    def _item(value: object) -> ResourceSearchItem:
        return ResourceSearchItem(
            code=str(getattr(value, "code", "")),
            available_variants=tuple(getattr(value, "available_variants", ()) or ()),
            title=(
                str(getattr(value, "title"))
                if getattr(value, "title", None) is not None
                else None
            ),
        )

    @classmethod
    def _state(cls, value: object) -> ResourceSearchState:
        return ResourceSearchState(
            next_page=getattr(value, "next_page", None),
            pending=tuple(
                cls._item(item) for item in getattr(value, "pending", ()) or ()
            ),
            cursor=int(getattr(value, "cursor", 0)),
            total_pages=getattr(value, "total_pages", None),
            scanned_pages=int(getattr(value, "scanned_pages", 0)),
        )

    def __call__(
        self,
        work: ResourceSearchWork,
        *,
        on_event: Callable[[ResourceSearchWorkerEvent], None],
        cancel_event: threading.Event,
    ) -> ResourceSearchWorkerResult:
        clean_work = validate_work(work)
        from ...missav.browser_runtime import get_missav_browser_runtime
        from ...missav.browser_service import (
            BrowserOperationCancelled,
            BrowserOperationError,
            BrowserOperationFailed,
            BrowserOwnershipError,
            BrowserUpstreamError,
            BrowserRestartExhausted,
            BrowserServiceClosed,
        )

        runtime = self._runtime or get_missav_browser_runtime()

        def relay(page: object) -> None:
            state = self._state(getattr(page, "state", None))
            items = tuple(self._item(item) for item in getattr(page, "items", ()) or ())
            on_event(
                ResourceSearchPageEvent(
                    page=int(getattr(page, "page", 0)),
                    fetched=bool(getattr(page, "fetched", False)),
                    items=items,
                    state=state,
                )
            )

        on_event(ResourceSearchStartedEvent(clean_work.state))
        state = clean_work.state
        payload = {
            "query": clean_work.query,
            "result_limit": clean_work.result_limit,
            "suffix_width": clean_work.suffix_width,
            "start": clean_work.start,
            "end": clean_work.end,
            "state": {
                "next_page": state.next_page,
                "pending": [
                    {
                        "code": item.code,
                        "available_variants": list(item.available_variants),
                        "title": item.title,
                    }
                    for item in state.pending
                ],
                "cursor": state.cursor,
                "total_pages": state.total_pages,
                "scanned_pages": state.scanned_pages,
            },
            "timeout_seconds": 120.0,
            # This callback is process-local and never enters a serialized
            # worker payload, log line, or SQLite value.
            "on_page": relay,
        }
        try:
            # The payload snapshots this run's cursor.  Once page events start
            # relaying, the SQLite cursor advances past that snapshot, so a
            # broker-side upstream requeue would replay stale pages and trip the
            # "out of order" guard.  Disable the broker's requeue and let
            # ResourceSearchManager retry from the latest SQLite cursor instead.
            task = runtime.submit("resource_search", payload, retry_on_upstream=False)
            from ...missav.browser_runtime import wait_for_browser_task

            result = wait_for_browser_task(
                task,
                timeout_seconds=float(payload["timeout_seconds"]),
                cancel_event=cancel_event,
                timeout_message="MissAV resource search timed out",
                timeout_code="navigation_timeout",
            )
            result_state = self._state(getattr(result, "state", None))
            return ResourceSearchWorkerResult(
                complete=bool(getattr(result, "complete", False)),
                state=result_state,
            )
        except BrowserOperationCancelled:
            raise ResourceSearchCancelledError(
                "resource search was cancelled"
            ) from None
        except BrowserUpstreamError:
            raise ResourceSearchWorkerError(
                "transient_browser_failure",
                retryable=True,
                state=state,
            ) from None
        except BrowserOperationError as exc:
            transient = exc.code in {
                "challenge_active",
                "rate_limited",
                "navigation_timeout",
                "upstream_unavailable",
                "discovery_unavailable",
            }
            raise ResourceSearchWorkerError(
                "transient_browser_failure" if transient else "discovery_unavailable",
                retryable=transient,
            ) from None
        except (
            BrowserOperationFailed,
            BrowserOwnershipError,
            BrowserRestartExhausted,
        ) as exc:
            del exc
            raise ResourceSearchWorkerError(
                "transient_browser_failure", retryable=True
            ) from None
        except BrowserServiceClosed:
            if cancel_event.is_set():
                raise ResourceSearchCancelledError(
                    "resource search was cancelled"
                ) from None
            raise ResourceSearchUnavailableError(
                "resource search browser is unavailable"
            ) from None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1.0)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
