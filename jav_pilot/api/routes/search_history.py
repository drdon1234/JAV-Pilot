"""Search history and ranking endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...core.selection import selected_ids
from ...search.history import KINDS as SEARCH_HISTORY_KINDS
from ...search.rankings import RankingError, RankingRequest, fetch_ranking, ranking_boards
from ...search.detail_prefetch import DetailPrefetchValidationError
from ...config.settings import load_settings
from ..services.detail_prefetch import detail_snapshot_from_url
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
            payload = self._read_json_body(512 * 1024)
            action = str(payload.get("action") or "")
            store = search_history_store()
            if action == "clear" and set(payload) == {"action"}:
                result: dict[str, object] = {"cleared": store.clear()}
            elif action == "remove" and set(payload) == {"action", "id"}:
                result = {"removed": store.remove(str(payload.get("id") or ""))}
            elif action == "remove" and set(payload) == {"action", "ids"}:
                result = {"removed": store.remove_many(selected_ids(payload["ids"]))}
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
            # ``type`` is the JavDB category parameter from before boards existed.
            request = RankingRequest(
                single_param(params, "board") or "javdb",
                single_param(params, "period") or "daily",
                single_param(params, "category") or single_param(params, "type") or "",
            )
            payload = fetch_ranking(request, refresh=single_param(params, "refresh") == "1")
        except RankingError as exc:
            status = (
                HTTPStatus.BAD_REQUEST
                if exc.code == "invalid"
                else HTTPStatus.CONFLICT
                if exc.code in {"login_required", "source_disabled", "region_restricted"}
                else HTTPStatus.BAD_GATEWAY
            )
            self._send_json({"ok": False, "code": exc.code, "error": str(exc)}, status)
            return
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        settings = load_settings()
        items = []
        for raw in payload.get("items", []):
            item = dict(raw)
            item["detail_source"] = None
            source = str(item.get("source_id") or "").removeprefix("ranking-")
            if item.get("work_id") and item.get("detail_url"):
                try:
                    detail_snapshot_from_url(
                        item["work_id"], item.get("code") or "", source,
                        item["detail_url"], settings,
                    )
                except (ValueError, DetailPrefetchValidationError):
                    pass
                else:
                    item["detail_source"] = source
            items.append(item)
        self._send_json({"ok": True, **payload, "items": items})

    def _handle_ranking_boards(self) -> None:
        self._send_json({"ok": True, "boards": ranking_boards()})
