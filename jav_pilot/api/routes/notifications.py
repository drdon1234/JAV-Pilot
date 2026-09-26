"""Notification configuration and delivery endpoints."""

from __future__ import annotations

import secrets
import sqlite3
from http import HTTPStatus

from ...config.runtime_config import (
    RuntimeConfigError,
    notification_public_config,
    notification_public_config_for,
    prepare_notification_config,
    runtime_config_transaction,
    save_prepared_notification_config,
)
from ...core.guards import QueryError
from ...notifications.errors import (
    NotificationConfigurationError,
    NotificationError,
    NotificationSecurityError,
    NotificationStoreError,
)
from ...notifications.events import create_test_notification_event
from .. import state
from ..base import BaseHandler
from ..request import int_param, query_params, single_param
from ..services.notifications import (
    NOTIFICATION_RUNTIME_DRAIN_TIMEOUT_SECONDS,
    build_notification_runtime,
    ensure_notification_worker,
    install_notification_runtime,
    pause_notification_runtime,
    primary_notification_outbox,
    resume_notification_runtime,
)
from ..services.readiness import invalidate_readiness_probes


class NotificationRoutes(BaseHandler):
    def _handle_notifications(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            event_id = single_param(params, "event_id").strip()
            limit = int_param(params, "limit", 50)
            if not 1 <= limit <= 100:
                raise ValueError("notification history limit is invalid")
            with state.NOTIFICATION_RUNTIME_LOCK:
                stores = tuple(state.NOTIFICATION_OUTBOXES)
            if event_id:
                for store in stores:
                    event = store.get(event_id)
                    if event is None:
                        continue
                    self._send_json(
                        {
                            "ok": True,
                            "event": event.payload(),
                            "deliveries": store.delivery_states(event_id),
                            "history": store.delivery_history(event_id),
                            "config": notification_public_config(),
                        }
                    )
                    return
                self._send_json(
                    {"ok": False, "error": "Notification event was not found"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            events_by_id: dict[str, dict[str, object]] = {}
            for store in stores:
                for event in store.list_events(limit=limit):
                    events_by_id.setdefault(str(event["event_id"]), event)
            events = sorted(
                events_by_id.values(),
                key=lambda item: (
                    float(item.get("created_at") or 0),
                    str(item.get("event_id") or ""),
                ),
                reverse=True,
            )[:limit]
            self._send_json(
                {
                    "ok": True,
                    "events": events,
                    "config": notification_public_config(),
                }
            )
        except (NotificationStoreError, OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Notification history is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (QueryError, ValueError, NotificationError, RuntimeConfigError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _handle_notification_config(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            with state.NOTIFICATION_CONFIG_LOCK, runtime_config_transaction():
                if not ensure_notification_worker():
                    raise NotificationStoreError(
                        "notification dispatcher is unavailable"
                    )
                prospective, candidate = prepare_notification_config(payload)
                prepared_runtime = build_notification_runtime(candidate)
                paused_runtime = pause_notification_runtime(
                    timeout=NOTIFICATION_RUNTIME_DRAIN_TIMEOUT_SECONDS
                )
                try:
                    save_prepared_notification_config(prospective)
                except BaseException:
                    resume_notification_runtime(paused_runtime)
                    raise
                install_notification_runtime(
                    *prepared_runtime,
                    paused_runtime=paused_runtime,
                )
                public_config = notification_public_config_for(candidate)
            invalidate_readiness_probes()
        except (
            ValueError,
            RuntimeConfigError,
            NotificationConfigurationError,
            NotificationSecurityError,
        ) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (NotificationStoreError, OSError, sqlite3.Error, RuntimeError):
            self._send_json(
                {"ok": False, "error": "Notification configuration is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json({"ok": True, "config": public_config})

    def _handle_notification_test(self) -> None:
        try:
            payload = self._read_json_body(1024)
            if payload:
                raise ValueError("test notification body must be empty")
            event = create_test_notification_event(request_id=secrets.token_hex(16))
            store = primary_notification_outbox()
            created = store.enqueue(event)
            state.NOTIFICATION_WAKE.set()
        except (NotificationStoreError, OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Notification outbox is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except (ValueError, NotificationError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {"ok": True, "event_id": event.event_id, "created": created},
            HTTPStatus.ACCEPTED,
        )

    def _handle_notification_retry(self) -> None:
        try:
            payload = self._read_json_body(4096)
            event_id = str(payload.get("event_id") or "").strip()
            adapter = str(payload.get("adapter") or "").strip() or None
            if not event_id:
                raise ValueError("notification event_id is required")
            retried = 0
            with state.NOTIFICATION_RUNTIME_LOCK:
                stores = tuple(state.NOTIFICATION_OUTBOXES)
            for store in stores:
                if store.get(event_id) is not None:
                    retried += store.manual_retry(event_id, adapter=adapter)
            if retried:
                state.NOTIFICATION_WAKE.set()
        except (NotificationStoreError, OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Notification retry is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except (ValueError, NotificationError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "retried": retried})
