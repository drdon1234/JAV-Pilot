"""HTTP plumbing shared by every route group: correlation IDs, security headers and bodies."""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from .. import __version__
from ..core.observability import (
    RUNTIME_METRICS,
    current_correlation_id,
    emit_json_log,
    new_correlation_id,
    normalize_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from ..security.auth import verify_session
from .request import request_is_secure

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' "
    "'sha256-IHVJxNTxF+p7uXRaHZ2A/YiUQerTP2cWRz9fipQp9+A='; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
    "media-src 'self' blob: https:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


class BaseHandler(BaseHTTPRequestHandler):
    timeout = 30.0
    server_version = f"jav-pilot/{__version__}"

    def handle_one_request(self) -> None:
        token = set_correlation_id(new_correlation_id())
        self._correlation_id = current_correlation_id()
        self._response_status = 500
        self.command = ""
        started = time.monotonic()
        failed = False
        try:
            super().handle_one_request()
        except Exception:
            failed = True
            raise
        finally:
            if getattr(self, "command", ""):
                duration = max(0.0, time.monotonic() - started)
                method = getattr(self, "command", "OTHER")
                status = int(getattr(self, "_response_status", 500))
                RUNTIME_METRICS.observe_request(method, status, duration)
                emit_json_log(
                    "server",
                    "http_request_completed",
                    level="error" if failed else "info",
                    correlation_id=self._correlation_id,
                    method=method,
                    status=status,
                    status_class=f"{min(5, max(1, status // 100))}xx",
                    duration_ms=round(duration * 1000),
                    outcome="error" if failed else "completed",
                )
            reset_correlation_id(token)

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            self._correlation_id = normalize_correlation_id(
                self.headers.get("X-Correlation-ID", "")
            )
            set_correlation_id(self._correlation_id)
        return parsed

    def send_response(self, code: int, message: str | None = None) -> None:
        self._response_status = int(code)
        super().send_response(code, message)

    def end_headers(self) -> None:
        self.send_header(
            "X-Correlation-ID",
            getattr(self, "_correlation_id", current_correlation_id()),
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            CONTENT_SECURITY_POLICY,
        )
        if request_is_secure(self):
            self.send_header(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authenticated(self) -> bool:
        return verify_session(self.headers.get("Cookie", ""))

    def _send_unauthorized(self, path: str) -> None:
        if path.startswith("/api/"):
            self._send_json(
                {"ok": False, "error": "authentication required"},
                HTTPStatus.UNAUTHORIZED,
            )
            return
        self._redirect("/login")

    def _send_sse_headers(self) -> bool:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return False

    def _send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Vary", "Cookie")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _read_json_body(self, max_bytes: int) -> dict[str, object]:
        raw = self._read_body(max_bytes)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def _read_body(self, max_bytes: int) -> bytes:
        raw_length = self.headers.get("content-length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > max_bytes:
            raise ValueError(f"request body exceeds {max_bytes} bytes")
        return self.rfile.read(length)

    def _send_text(
        self, text: str, *, content_type: str, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        self._send_bytes(
            text.encode("utf-8"),
            content_type=content_type,
            status=status,
            cache_control="no-store",
        )

    def _send_bytes(
        self,
        raw: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _redirect(self, location: str) -> None:
        try:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _send_event(self, event: str, payload: object) -> bool:
        raw = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode(
            "utf-8"
        )
        try:
            self.wfile.write(raw)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return False
