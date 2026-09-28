from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from .observability import emit_json_log


# Leave time for interpreter and child-process cleanup within Compose's 120s grace.
SHUTDOWN_TIMEOUT_SECONDS = 90.0


def stop_services(
    services: Mapping[str, Callable[..., object]],
    *,
    timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
) -> bool:
    """Request every service to stop concurrently within one overall deadline."""
    deadline = time.monotonic() + max(0.0, timeout)
    failures: set[str] = set()
    result_lock = threading.Lock()

    def stop(name: str, callback: Callable[..., object]) -> None:
        try:
            result = callback(timeout=max(0.0, deadline - time.monotonic()))
            if result is not False:
                return
            event = "shutdown_deadline_exceeded"
        except Exception:
            event = "shutdown_failed"
        with result_lock:
            failures.add(name)
        emit_json_log(name, event, level="warning", outcome="pending")

    workers: dict[str, threading.Thread] = {}
    for name, callback in services.items():
        try:
            worker = threading.Thread(
                target=stop,
                args=(name, callback),
                name=f"jav-stop-{name}",
                daemon=True,
            )
            worker.start()
            workers[name] = worker
        except RuntimeError:
            with result_lock:
                failures.add(name)
            emit_json_log(name, "shutdown_failed", level="warning", outcome="pending")
    for worker in workers.values():
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    pending = [name for name, worker in workers.items() if worker.is_alive()]
    for name in pending:
        emit_json_log(
            name,
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    with result_lock:
        return not pending and not failures
