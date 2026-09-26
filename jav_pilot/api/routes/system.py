"""Health, readiness, metrics and client telemetry endpoints."""

from __future__ import annotations

from http import HTTPStatus

from ... import __version__
from ...core.observability import emit_json_log
from ..base import BaseHandler
from ..services.environment import app_revision
from ..services.metrics import render_observability_metrics
from ..services.readiness import readiness_status


class SystemRoutes(BaseHandler):
    def _handle_healthz(self) -> None:
        self._send_json(
            {
                "ok": True,
                "app": "jav-pilot",
                "version": __version__,
                "revision": app_revision(),
            }
        )

    def _handle_readyz(self) -> None:
        readiness = readiness_status()
        self._send_json(
            readiness,
            (
                HTTPStatus.OK
                if bool(readiness.get("ok"))
                else HTTPStatus.SERVICE_UNAVAILABLE
            ),
        )

    def _handle_metrics(self) -> None:
        self._send_text(
            render_observability_metrics(),
            content_type="text/plain; version=0.0.4; charset=utf-8",
        )

    def _handle_client_event(self) -> None:
        try:
            payload = self._read_json_body(1024)
            code = str(payload.get("code") or "").strip().lower()
            if code != "frontend_render_failure" or set(payload) != {"code"}:
                raise ValueError("client event is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        emit_json_log("frontend", code, outcome="reported")
        self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
