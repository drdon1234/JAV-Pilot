"""Search history and ranking endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...search.history import KINDS as SEARCH_HISTORY_KINDS
from ...search.rankings import RankingError, RankingRequest, javdb_ranking
from ..base import BaseHandler
from ..request import int_param, query_params, single_param
from ..services.search_history import search_history_limit, search_history_store


class SearchHistoryRoutes(BaseHandler):
    def _handle_search_history(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            kind = single_param(params, "kind") or None
            if kind is not None and kind not in SEARCH_HISTORY_KINDS:
                raise QueryError("search history kind is invalid")
            result = search_history_store().list(
                limit=int_param(params, "limit", 100),
                offset=int_param(params, "offset", 0),
                kind=kind,
            )
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "search history is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json({"ok": True, **result, "limit": search_history_limit()})

    def _handle_search_history_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            action = str(payload.get("action") or "")
            store = search_history_store()
            if action == "clear" and set(payload) == {"action"}:
                result: dict[str, object] = {"cleared": store.clear()}
            elif action == "remove" and set(payload) == {"action", "id"}:
                result = {"removed": store.remove(str(payload.get("id") or ""))}
            else:
                raise ValueError("search history action is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "search history is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json({"ok": True, **result})

    def _handle_rankings(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            request = RankingRequest(
                single_param(params, "period") or "daily",
                single_param(params, "type") or "censored",
            )
            payload = javdb_ranking(request, refresh=single_param(params, "refresh") == "1")
        except RankingError as exc:
            status = (
                HTTPStatus.BAD_REQUEST
                if exc.code == "invalid"
                else HTTPStatus.CONFLICT
                if exc.code in {"login_required", "source_disabled"}
                else HTTPStatus.BAD_GATEWAY
            )
            self._send_json({"ok": False, "code": exc.code, "error": str(exc)}, status)
            return
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, **payload})
