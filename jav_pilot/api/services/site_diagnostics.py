"""Site diagnostic store and scheduler lifecycle."""

from __future__ import annotations

import os

from ...sites.diagnostic_store import SQLiteSiteDiagnosticStore
from .. import state
from .environment import site_diagnostic_database_path
from .notifications import ensure_notification_outbox_registered


def stop_site_diagnostic_scheduler(*, timeout: float) -> bool:
    scheduler = state.SITE_DIAGNOSTIC_SCHEDULER
    if scheduler is None:
        return True
    stopped = scheduler.stop(timeout=timeout)
    if stopped and state.SITE_DIAGNOSTIC_SCHEDULER is scheduler:
        state.SITE_DIAGNOSTIC_SCHEDULER = None
    return stopped


def site_diagnostic_store() -> SQLiteSiteDiagnosticStore:
    database_path = site_diagnostic_database_path()
    with state.SITE_DIAGNOSTICS_LOCK:
        if state.SITE_DIAGNOSTICS is None or state.SITE_DIAGNOSTICS.path != database_path:
            state.SITE_DIAGNOSTICS = SQLiteSiteDiagnosticStore(database_path)
        store = state.SITE_DIAGNOSTICS
    ensure_notification_outbox_registered(store.path, component="site_diagnostics")
    return store


def site_diagnostic_delay(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if not 60.0 <= value <= 86_400.0:
        raise ValueError(f"{name} is invalid")
    return value
