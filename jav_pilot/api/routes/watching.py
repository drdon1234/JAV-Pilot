"""Authenticated application browsing, bookmarks and same-origin playback."""
from __future__ import annotations

import sqlite3
import threading
from http import HTTPStatus
from urllib.parse import parse_qs

from ...watching import catalog, playback, store
from ...web_download.media import SafeMediaError
from ...web_download.providers import WebDownloadProviderError
from ...core.selection import selected_ids
from ..base import BaseHandler

_CATALOG_SLOTS = threading.BoundedSemaphore(4)
_CAPTURE_SLOTS = threading.BoundedSemaphore(2)


def _param(query: str, key: str, default: str = "") -> str:
    values = parse_qs(query).get(key, [default])
    if len(values) != 1:
        raise ValueError("请求参数重复")
    return values[0]


class WatchingRoutes(BaseHandler):
    def _watch_error(self, exc: Exception) -> None:
        if isinstance(exc, ValueError):
            message, status = str(exc), HTTPStatus.BAD_REQUEST
        elif isinstance(exc, (OSError, sqlite3.Error)):
            message, status = "收藏存储暂不可用，请稍后重试", HTTPStatus.SERVICE_UNAVAILABLE
        else:
            code = getattr(exc, "code", "")
            message = {
                "not_found": "此作品暂无可用播放线路，可返回结果选择其他版本或站点",
                "challenge_active": "站点验证尚未完成，请稍后重试",
                "rate_limited": "站点请求频繁，请稍后重试",
                "timeout": "站点响应超时，请稍后重试",
                "busy": "后台验证繁忙，请稍后重试",
                "response_invalid": "站点页面暂时无法解析，请稍后重试",
            }.get(code, "站点或播放线路暂不可用，请重试或切换站点")
            status = HTTPStatus.BAD_GATEWAY
        self._send_json({"ok": False, "error": message}, status)

    def _handle_watch_sites(self) -> None:
        self._send_json({"sites": catalog.sites()})

    def _handle_watch_detail(self, query: str) -> None:
        if not _CATALOG_SLOTS.acquire(blocking=False):
            self._send_json({"error": "站点请求繁忙，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            self._send_json(catalog.describe(_param(query, "source"), _param(query, "url")))
        except Exception as exc:
            self._watch_error(exc)
        finally:
            _CATALOG_SLOTS.release()

    def _handle_watch_recommendations(self, query: str) -> None:
        if not _CATALOG_SLOTS.acquire(blocking=False):
            self._send_json({"error": "站点请求繁忙，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            self._send_json({'items': catalog.recommendations(_param(query, 'source'), _param(query, 'url'))})
        except Exception as exc:
            self._watch_error(exc)
        finally:
            _CATALOG_SLOTS.release()

    def _handle_watch_catalog(self, query: str) -> None:
        if not _CATALOG_SLOTS.acquire(blocking=False):
            self._send_json({"error": "站点检索繁忙，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            source = _param(query, "source")
            term = _param(query, "q").strip()
            page = int(_param(query, "page", "1"))
            if len(term) > 120 or not 1 <= page <= 9999 or any(ord(c) < 32 for c in term):
                raise ValueError("检索条件无效")
            result = catalog.browse(source, term, page)
            if term and page == 1:
                from ..services.search_history import record_search_history
                record_search_history("resource", term, {"source_id": source, "query": term})
            self._send_json(result)
        except Exception as exc:
            self._watch_error(exc)
        finally:
            _CATALOG_SLOTS.release()

    def _handle_watch_resolve(self) -> None:
        if not _CAPTURE_SLOTS.acquire(blocking=False):
            self._send_json({"error": "播放解析繁忙，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            payload = self._read_json_body(8192)
            manifest = catalog.capture(str(payload.get("source") or ""), str(payload.get("url") or ""))
            self._send_json(playback.create_session(manifest))
        except Exception as exc:
            self._watch_error(exc)
        finally:
            _CAPTURE_SLOTS.release()

    def _handle_watch_close(self) -> None:
        try:
            payload = self._read_json_body(1024)
            playback.close_session(str(payload.get("session") or ""))
            self._send_json({"ok": True})
        except ValueError as exc:
            self._watch_error(exc)

    def _handle_watch_media(self, query: str) -> None:
        try:
            raw, content_type, status, headers = playback.media_resource(
                _param(query, "session"), _param(query, "resource"), self.headers.get("Range")
            )
            self._send_bytes(raw, content_type=content_type, status=HTTPStatus(status), headers=headers)
        except (ValueError, SafeMediaError, WebDownloadProviderError) as exc:
            self._watch_error(exc)
        except Exception:
            self._send_json({"error": "播放资源暂不可用，请重新解析"}, HTTPStatus.BAD_GATEWAY)

    def _handle_bookmarks(self) -> None:
        try:
            self._send_json({"items": store.list_bookmarks()})
        except (OSError, sqlite3.Error) as exc:
            self._watch_error(exc)

    def _handle_bookmark_action(self) -> None:
        try:
            payload = self._read_json_body(512 * 1024)
            if payload.get("action") == "remove":
                store.remove_bookmarks(selected_ids(payload.get("ids", [payload.get("id", "")])))
                self._send_json({"ok": True})
            elif payload.get("action") == "save":
                self._send_json({"ok": True, **store.save_bookmark(payload)})
            else:
                raise ValueError("收藏操作无效")
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._watch_error(exc)
