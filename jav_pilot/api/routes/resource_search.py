"""Web resource search endpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError, normalize_query
from ...search.resources.errors import (
    ResourceSearchConflictError,
    ResourceSearchError,
    ResourceSearchNotFoundError,
    ResourceSearchUnavailableError,
)
from ...web_download.errors import WebDownloadError
from ...web_download.variant import DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY
from ..request import query_params, single_param, strict_int_param
from ..services.resource_search import (
    enabled_resource_search_sources,
    require_resource_search_source,
    resource_search_manager,
)
from ..services.search_history import record_search_history
from ..services.web_downloads import (
    require_web_download_site_available,
    web_download_batch_manager,
)
from .common import CommonErrorResponses


class ResourceSearchRoutes(CommonErrorResponses):
    def _handle_resource_search(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            allowed = {"id", "limit", "offset", "keyword", "variant"}
            if not set(params).issubset(allowed) or any(
                len(values) != 1 for values in params.values()
            ):
                raise QueryError("resource search query is invalid")
            session_id = single_param(params, "id")
            if not session_id:
                raise QueryError("resource search id is required")
            search_result = resource_search_manager().get(
                session_id,
                limit=strict_int_param(params, "limit", 25),
                offset=strict_int_param(params, "offset", 0),
                keyword=single_param(params, "keyword") or None,
                variant=single_param(params, "variant") or None,
            )
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except ResourceSearchError as exc:
            self._send_resource_search_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_resource_search_unavailable()
            return
        self._send_json({"ok": True, "search": search_result})

    def _handle_resource_search_create(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            required = {"source_id", "query", "result_limit"}
            allowed = required | {"suffix_width", "start", "end", "exact_match"}
            if not required.issubset(payload) or not set(payload).issubset(allowed):
                raise ValueError("resource search request is invalid")
            query = normalize_query(payload.get("query"))
            source_id = require_resource_search_source(payload.get("source_id"))
            source_options = (
                {"source_ids": enabled_resource_search_sources()}
                if source_id == "all"
                else {}
            )
            range_start = payload.get("start")
            range_end = payload.get("end")
            if (range_start is None) != (range_end is None):
                raise ValueError("resource search range is invalid")
            search_result = resource_search_manager().create(
                query,
                source_id=source_id,
                result_limit=payload.get("result_limit"),
                suffix_width=payload.get("suffix_width"),
                start=range_start,
                end=range_end,
                exact_match=payload.get("exact_match", False),
                **source_options,
            )
            record_search_history(
                "resource",
                query,
                {
                    "source_id": source_id,
                    "query": query,
                    "result_limit": payload.get("result_limit"),
                    "exact_match": bool(payload.get("exact_match", False)),
                    "start": range_start,
                    "end": range_end,
                    "suffix_width": payload.get("suffix_width"),
                },
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except ResourceSearchError as exc:
            self._send_resource_search_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_resource_search_unavailable()
            return
        self._send_json(
            {"ok": True, "search": search_result},
            HTTPStatus.ACCEPTED,
        )

    def _handle_resource_search_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            action = payload.get("action")
            if not isinstance(action, str):
                raise ValueError("resource search action is invalid")
            action = action.strip().lower()
            if action not in {"continue", "retry", "cancel", "remove"}:
                raise ValueError("resource search action is invalid")
            base_fields = {"session_id", "expected_revision", "action"}
            expected_fields = (
                base_fields | {"result_limit"} if action == "continue" else base_fields
            )
            if set(payload) != expected_fields:
                raise ValueError("resource search action is invalid")
            manager = resource_search_manager()
            session_id = payload.get("session_id")
            expected_revision = payload.get("expected_revision")
            if action in {"continue", "retry"}:
                current = manager.get(session_id, limit=1, offset=0)
                require_resource_search_source(current.get("source_id"))
            if action == "continue":
                search_result = manager.continue_search(
                    session_id,
                    expected_revision,
                    payload.get("result_limit"),
                )
                accepted = True
            elif action == "retry":
                search_result = manager.retry(session_id, expected_revision)
                accepted = True
            elif action == "cancel":
                search_result = manager.cancel(session_id, expected_revision)
                accepted = False
            elif action == "remove":
                search_result = manager.remove(session_id, expected_revision)
                accepted = False
            else:
                raise ValueError("resource search action is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except ResourceSearchError as exc:
            self._send_resource_search_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_resource_search_unavailable()
            return
        if action == "remove":
            self._send_json({"ok": True, **search_result}, HTTPStatus.OK)
            return
        self._send_json(
            {"ok": True, "search": search_result},
            HTTPStatus.ACCEPTED if accepted else HTTPStatus.OK,
        )

    def _handle_resource_search_downloads(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            required = {
                "session_id",
                "expected_revision",
                "item_ids",
                "idempotency_key",
            }
            allowed = required | {
                "max_height",
                "existing_policy",
                "variant_priority",
                "default_quality_strategy",
                "default_height",
                "rule_id",
                "rule_revision",
            }
            if not required.issubset(payload) or not set(payload).issubset(allowed):
                raise ValueError("resource search download request is invalid")
            if (payload.get("rule_id") is None) != (
                payload.get("rule_revision") is None
            ):
                raise ValueError("resource search batch rule binding is invalid")
            session_id = payload.get("session_id")
            source_revision = payload.get("expected_revision")
            idempotency_key = payload.get("idempotency_key")
            request_hash = hashlib.sha256(
                json.dumps(
                    {
                        "session_id": session_id,
                        "expected_revision": source_revision,
                        "item_ids": payload.get("item_ids"),
                        "max_height": payload.get("max_height", 2160),
                        "existing_policy": payload.get(
                            "existing_policy",
                            "higher_quality",
                        ),
                        "variant_priority": payload.get(
                            "variant_priority",
                            DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
                        ),
                        "default_quality_strategy": payload.get(
                            "default_quality_strategy",
                            "highest",
                        ),
                        "default_height": payload.get("default_height"),
                        "rule_id": payload.get("rule_id"),
                        "rule_revision": payload.get("rule_revision"),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii")
            ).hexdigest()
            batch_manager = web_download_batch_manager()
            batch = batch_manager.replay_selected_submission(
                idempotency_key,
                request_hash,
            )
            if batch is not None:
                self._send_json(
                    {"ok": True, "batch": batch},
                    HTTPStatus.ACCEPTED,
                )
                return
            require_web_download_site_available()
            try:
                selected = resource_search_manager().snapshot_selected(
                    session_id,
                    source_revision,
                    payload.get("item_ids"),
                )
            except (OSError, sqlite3.Error):
                self._send_resource_search_unavailable()
                return
            batch_items = [
                {
                    "code": item["code"],
                    "available_variants": item["available_variants"],
                }
                for item in selected
            ]
            batch = batch_manager.queue_selected(
                batch_items,
                session_id,
                source_revision,
                payload.get("max_height", 2160),
                payload.get("existing_policy", "higher_quality"),
                payload.get(
                    "variant_priority",
                    DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
                ),
                default_quality_strategy=payload.get(
                    "default_quality_strategy",
                    "highest",
                ),
                default_height=payload.get("default_height"),
                rule_id=payload.get("rule_id"),
                rule_revision=payload.get("rule_revision"),
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except ResourceSearchError as exc:
            self._send_resource_search_error(exc)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json(
            {"ok": True, "batch": batch},
            HTTPStatus.ACCEPTED,
        )

    def _send_resource_search_error(self, error: ResourceSearchError) -> None:
        if isinstance(error, ResourceSearchNotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(error, ResourceSearchConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(error, ResourceSearchUnavailableError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_json({"ok": False, "error": str(error)}, status)

    def _send_resource_search_unavailable(self) -> None:
        self._send_json(
            {"ok": False, "error": "Resource search storage is unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
