"""Frontend, static asset and cover proxy endpoints."""

from __future__ import annotations

from http import HTTPStatus

from ...config.settings import load_settings
from ...core.guards import QueryError
from ...search.cover_proxy import CoverProxyError, open_cover
from .. import state
from ..base import BaseHandler
from ..request import query_params, single_param
from ..static_files import StaticFileError, read_asset, read_index

COVER_QUEUE_TIMEOUT_SECONDS = 10.0


class AssetRoutes(BaseHandler):
    def _handle_cover(self, query_string: str) -> None:
        if not state.COVER_REQUEST_SLOTS.acquire(blocking=False):
            self._send_json(
                {"ok": False, "error": "too many active image requests"},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        active_slot = False
        response_started = False
        try:
            if not state.COVER_SLOTS.acquire(timeout=COVER_QUEUE_TIMEOUT_SECONDS):
                self._send_json(
                    {"ok": False, "error": "too many active image requests"},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            active_slot = True
            params = query_params(query_string)
            with open_cover(
                single_param(params, "source"),
                single_param(params, "url"),
                settings=load_settings(),
            ) as cover:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", cover.content_type)
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                if cover.content_length is not None:
                    self.send_header("Content-Length", str(cover.content_length))
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                response_started = True
                for chunk in cover.iter_chunks():
                    self.wfile.write(chunk)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except CoverProxyError as exc:
            if response_started:
                self.close_connection = True
            else:
                status = (
                    HTTPStatus.BAD_REQUEST
                    if exc.category == "request"
                    else HTTPStatus.BAD_GATEWAY
                )
                self._send_json({"ok": False, "error": str(exc)}, status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        finally:
            if active_slot:
                state.COVER_SLOTS.release()
            state.COVER_REQUEST_SLOTS.release()

    def _handle_login_page(self) -> None:
        if self._authenticated():
            self._redirect("/search")
        else:
            self._handle_frontend()

    def _handle_root(self) -> None:
        self._redirect("/search")

    def _handle_frontend(self) -> None:
        try:
            raw = read_index()
        except StaticFileError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_bytes(
            raw, content_type="text/html; charset=utf-8", cache_control="no-store"
        )

    def _handle_static_asset(self, path: str) -> None:
        try:
            raw, content_type = read_asset(path)
        except FileNotFoundError:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        except StaticFileError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_bytes(
            raw,
            content_type=content_type,
            cache_control="public, max-age=31536000, immutable",
        )
