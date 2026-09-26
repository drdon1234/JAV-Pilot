"""Persistent metadata search session endpoints."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from http import HTTPStatus

from ...core.guards import QueryError
from ...search.session_store import (
    MetadataSearchConflictError,
    MetadataSearchNotFoundError,
    MetadataSearchStoreError,
    SQLiteMetadataSearchStore,
)
from .. import state
from ..base import BaseHandler
from ..request import single_param, valid_request_id
from ..services.metadata_search import (
    METADATA_SEARCH_ERROR_REDACTION,
    metadata_search_continuation_payload,
    metadata_search_query_params,
    metadata_search_store,
    prepare_metadata_search_session,
    restore_metadata_search_continuation,
    sanitize_metadata_search_event_value,
)
from ..services.search_history import record_metadata_search_history
from .search import SearchRoutes


class _StoredMetadataSearchHandler:
    def __init__(
        self,
        store: SQLiteMetadataSearchStore,
        request_id: str,
        continuation_token: str,
    ) -> None:
        self.store = store
        self.request_id = request_id
        self.headers = (
            {"X-Search-Continuation": continuation_token} if continuation_token else {}
        )

    def _send_sse_headers(self) -> bool:
        return True

    def _send_event(self, event: str, payload: object) -> bool:
        if not isinstance(payload, dict):
            payload = {
                "request_id": self.request_id,
                "error": METADATA_SEARCH_ERROR_REDACTION,
            }
            event = "error"
        clean_payload = sanitize_metadata_search_event_value(payload)
        if not isinstance(clean_payload, dict):
            return False
        clean_payload["request_id"] = self.request_id
        continuation = None
        continuation_mode = None
        if event in {"done", "cancelled"}:
            token = str(payload.get("continuation_token") or "")
            mode = str(payload.get("continuation_mode") or "")
            envelope = state.SEARCH_CONTINUATIONS.export_ready(token)
            if (
                payload.get("can_continue") is True
                and mode in {"retry", "extend"}
                and envelope is not None
            ):
                try:
                    continuation = metadata_search_continuation_payload(envelope)
                    continuation_mode = mode
                except (TypeError, ValueError):
                    continuation = None
                    continuation_mode = None
            if continuation is not None:
                state.METADATA_SEARCH_CONTINUATIONS.remember(self.request_id, payload)
            else:
                state.METADATA_SEARCH_CONTINUATIONS.remove(self.request_id)
            clean_payload.update(
                {
                    "can_continue": False,
                    "continuation_token": None,
                    "continuation_mode": None,
                }
            )
        elif event == "error":
            state.METADATA_SEARCH_CONTINUATIONS.remove(self.request_id)
        result = self.store.append_event(
            self.request_id,
            event,
            clean_payload,
            continuation=continuation,
            continuation_mode=continuation_mode,
        )
        if result.storage_limited:
            state.METADATA_SEARCH_CONTINUATIONS.remove(self.request_id)
        return result.should_continue

    # Continuation error codes are safe to surface to the client: they carry no
    # upstream detail and the frontend keys its recovery (drop the stale token /
    # switch to retry mode) off them.  Everything else stays redacted.
    _SAFE_ERROR_CODES = frozenset(
        {"continuation_invalid", "continuation_in_progress"}
    )

    def _send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
        **_kwargs: object,
    ) -> None:
        source = payload if isinstance(payload, dict) else {}
        event_payload: dict[str, object] = {
            "request_id": self.request_id,
            "error": METADATA_SEARCH_ERROR_REDACTION,
        }
        code = source.get("code")
        if isinstance(code, str) and code in self._SAFE_ERROR_CODES:
            event_payload["code"] = code
        del source, status
        self._send_event("error", event_payload)


def _start_metadata_search_worker(
    store: SQLiteMetadataSearchStore,
    prepared: dict[str, object],
) -> None:
    request_id = str(prepared["request_id"])
    query_string = str(prepared["query_string"])
    continuation_token = str(prepared["continuation_token"])

    def run() -> None:
        sink = _StoredMetadataSearchHandler(store, request_id, continuation_token)
        try:
            SearchRoutes._handle_search_stream(sink, query_string)
            try:
                session = store.get(request_id)
            except MetadataSearchNotFoundError:
                return
            if session["status"] == "running":
                store.append_event(
                    request_id,
                    "error",
                    {
                        "request_id": request_id,
                        "error": "搜索任务提前结束，已保存此前接收的结果",
                        "code": "search_truncated",
                    },
                )
        except MetadataSearchNotFoundError:
            return
        except MetadataSearchStoreError:
            try:
                store.append_event(
                    request_id,
                    "error",
                    {
                        "request_id": request_id,
                        "error": "搜索结果保存失败，已停止当前任务",
                        "code": "search_storage_failed",
                    },
                )
            except (MetadataSearchStoreError, OSError, sqlite3.Error):
                pass
        except Exception:  # noqa: BLE001 - upstream details may contain private URLs.
            try:
                store.append_event(
                    request_id,
                    "error",
                    {
                        "request_id": request_id,
                        "error": "搜索任务异常结束，已保存此前接收的结果",
                        "code": "search_worker_failed",
                    },
                )
            except (MetadataSearchStoreError, OSError, sqlite3.Error):
                pass

    worker = threading.Thread(
        target=run,
        name=f"metadata-search-{request_id[-12:]}",
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError:
        store.append_event(
            request_id,
            "error",
            {
                "request_id": request_id,
                "error": "搜索后台任务无法启动",
                "code": "search_worker_unavailable",
            },
        )


class MetadataSearchRoutes(BaseHandler):
    def _send_metadata_search_unavailable(self) -> None:
        self._send_json(
            {"ok": False, "error": "Metadata search storage is unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )

    def _handle_metadata_search_session_create(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            prepared = prepare_metadata_search_session(payload)
            store = metadata_search_store()
            session, created = store.create_or_get(
                request_id=prepared["request_id"],
                fingerprint=prepared["fingerprint"],
                request=prepared["request"],
            )
            if created:
                state.METADATA_SEARCH_CONTINUATIONS.remove(str(prepared["request_id"]))
                _start_metadata_search_worker(store, prepared)
                if not prepared["continuation_token"]:
                    record_metadata_search_history(prepared["request"])
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MetadataSearchConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except (MetadataSearchStoreError, OSError, sqlite3.Error):
            self._send_metadata_search_unavailable()
            return
        self._send_json(
            {"ok": True, "created": created, "session": session},
            HTTPStatus.ACCEPTED if created else HTTPStatus.OK,
        )

    def _handle_metadata_search_session(self, query_string: str) -> None:
        try:
            params = metadata_search_query_params(
                query_string, allowed={"request_id", "events", "optional"}
            )
            request_id = single_param(params, "request_id").strip()
            if request_id and not valid_request_id(request_id):
                raise QueryError("invalid request_id")
            if single_param(params, "events") not in {"", "0", "1"}:
                raise QueryError("invalid metadata search query")
            if single_param(params, "optional") not in {"", "0", "1"}:
                raise QueryError("invalid metadata search query")
            if not request_id and single_param(params, "optional") == "1":
                raise QueryError("invalid metadata search query")
            store = metadata_search_store()
            if request_id:
                try:
                    session = store.get(request_id)
                except MetadataSearchNotFoundError:
                    if single_param(params, "optional") != "1":
                        raise
                    session = None
            else:
                session = store.latest()
            if session is not None and single_param(params, "events") == "1":
                session = store.snapshot(session["request_id"])
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MetadataSearchNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (MetadataSearchStoreError, OSError, sqlite3.Error):
            self._send_metadata_search_unavailable()
            return
        self._send_json({"ok": True, "session": session})

    def _handle_metadata_search_session_stream(self, query_string: str) -> None:
        try:
            params = metadata_search_query_params(
                query_string, allowed={"request_id", "after"}
            )
            request_id = single_param(params, "request_id").strip()
            if not valid_request_id(request_id):
                raise QueryError("invalid request_id")
            raw_after = single_param(params, "after").strip() or "0"
            if re.fullmatch(r"[0-9]+", raw_after) is None:
                raise QueryError("invalid search event cursor")
            after = int(raw_after)
            store = metadata_search_store()
            store.get(request_id)
            restore_metadata_search_continuation(store, request_id)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MetadataSearchNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (MetadataSearchStoreError, OSError, sqlite3.Error):
            self._send_metadata_search_unavailable()
            return
        if not self._send_sse_headers():
            return
        cursor = after
        stream_started = time.monotonic()
        last_heartbeat = stream_started
        while (
            not state.SERVER_STOPPING.is_set()
            and time.monotonic() - stream_started < 2 * 60 * 60
        ):
            try:
                events = store.events_since(request_id, cursor, limit=250)
                session = store.get(request_id)
            except MetadataSearchNotFoundError:
                return
            except (MetadataSearchStoreError, OSError, sqlite3.Error):
                return
            for item in events:
                cursor = int(item["id"])
                payload = dict(item["payload"])
                event_name = str(item["event"])
                if event_name in {"done", "cancelled"}:
                    payload = state.METADATA_SEARCH_CONTINUATIONS.apply(request_id, payload)
                payload["event_cursor"] = cursor
                if not self._send_event(event_name, payload):
                    return
            if session["status"] != "running" and cursor >= int(
                session["last_event_id"]
            ):
                return
            now = time.monotonic()
            if now - last_heartbeat >= 15.0:
                if not self._send_event("ping", {"event_cursor": cursor}):
                    return
                last_heartbeat = now
            time.sleep(0.1)

    def _handle_metadata_search_session_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if str(payload.get("action") or "").strip().lower() != "clear":
                raise QueryError("metadata search action is invalid")
            store = metadata_search_store()
            running = store.running_ids()
            for request_id in running:
                state.SEARCH_JOBS.cancel(request_id)
            cleared = store.clear()
            state.METADATA_SEARCH_CONTINUATIONS.clear()
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (MetadataSearchStoreError, OSError, sqlite3.Error):
            self._send_metadata_search_unavailable()
            return
        self._send_json({"ok": True, "cleared": cleared, "cancelled": len(running)})
