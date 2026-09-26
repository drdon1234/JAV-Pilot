"""Web download batch, chain and rule endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...web_download.errors import WebDownloadError
from ...web_download.variant import DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY
from ..request import query_params, single_param, strict_int_param
from ..services.web_downloads import (
    require_web_download_site_available,
    web_download_batch_manager,
)
from .common import CommonErrorResponses

WEB_DOWNLOAD_BATCH_ACTION_MAX_BODY_BYTES = 256 * 1024


class WebDownloadBatchRoutes(CommonErrorResponses):
    def _handle_web_download_batch(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            batch_id = single_param(params, "id")
            if not batch_id:
                raise QueryError("batch id is required")
            batch = web_download_batch_manager().get(batch_id)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "batch": batch})

    def _handle_web_download_batch_action(self) -> None:
        try:
            payload = self._read_json_body(WEB_DOWNLOAD_BATCH_ACTION_MAX_BODY_BYTES)
            if not set(payload).issubset(
                {
                    "batch_id",
                    "action",
                    "preview_token",
                    "selected_codes",
                    "item_intents",
                }
            ):
                raise ValueError("web download batch action is invalid")
            action = str(payload.get("action") or "").strip().lower()
            if action in {"continue", "retry"}:
                require_web_download_site_available()
            manager = web_download_batch_manager()
            action_kwargs: dict[str, object] = {
                "selected_codes": payload.get("selected_codes"),
                "item_intents": payload.get("item_intents"),
            }
            if action == "commit":
                action_kwargs["commit_guard"] = require_web_download_site_available
            batch = manager.action(
                str(payload.get("batch_id") or ""),
                action,
                payload.get("preview_token"),
                **action_kwargs,
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json(
            {"ok": True, "batch": batch},
            HTTPStatus.ACCEPTED if action == "commit" else HTTPStatus.OK,
        )

    def _handle_web_download_batch_chains(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            root_chain_id = single_param(params, "root_id")
            manager = web_download_batch_manager()
            if root_chain_id:
                if not set(params).issubset({"root_id", "page_limit", "page_offset"}):
                    raise QueryError("batch chain query is invalid")
                result = manager.get_chain(
                    root_chain_id,
                    page_limit=strict_int_param(params, "page_limit", 16),
                    page_offset=strict_int_param(params, "page_offset", 0),
                )
            else:
                if not set(params).issubset({"limit", "offset"}):
                    raise QueryError("batch chain query is invalid")
                result = manager.list_chains(
                    limit=strict_int_param(params, "limit", 50),
                    offset=strict_int_param(params, "offset", 0),
                )
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, **result})

    def _handle_web_download_batch_chain_export(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            if set(params) != {"root_id"}:
                raise QueryError("batch chain root id is required")
            exported = web_download_batch_manager().export_chain(
                single_param(params, "root_id")
            )
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "export": exported})

    def _handle_web_download_batch_chain_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if set(payload) != {"root_chain_id", "action"}:
                raise ValueError("batch chain action is invalid")
            if str(payload.get("action") or "").strip().lower() != "cancel":
                raise ValueError("batch chain action is invalid")
            result = web_download_batch_manager().cancel_chain(
                str(payload.get("root_chain_id") or "")
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, **result})

    def _handle_web_download_batch_rules(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            if not set(params).issubset({"id"}):
                raise QueryError("batch rule query is invalid")
            rule_id = single_param(params, "id")
            manager = web_download_batch_manager()
            if rule_id:
                result: dict[str, object] = {
                    "ok": True,
                    "rule": manager.get_rule(rule_id),
                }
            else:
                result = {"ok": True, "rules": manager.list_rules()}
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json(result)

    def _handle_web_download_batch_rule_save(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            allowed = {
                "rule_id",
                "name",
                "code_or_prefix",
                "start",
                "end",
                "max_height",
                "existing_policy",
                "default_quality_strategy",
                "default_height",
                "selection_mode",
                "variant_priority",
                "expected_revision",
            }
            if not set(payload).issubset(allowed):
                raise ValueError("batch rule request is invalid")
            rule = web_download_batch_manager().save_rule(
                payload.get("name"),
                payload.get("code_or_prefix"),
                payload.get("start"),
                payload.get("end"),
                payload.get("max_height", 2160),
                payload.get("existing_policy", "higher_quality"),
                payload.get(
                    "variant_priority",
                    DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
                ),
                rule_id=payload.get("rule_id"),
                default_quality_strategy=payload.get(
                    "default_quality_strategy", "highest"
                ),
                default_height=payload.get("default_height"),
                selection_mode=payload.get("selection_mode", "all"),
                expected_revision=payload.get("expected_revision"),
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "rule": rule})

    def _handle_web_download_batch_rule_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            rule_id = str(payload.get("rule_id") or "")
            action = str(payload.get("action") or "").strip().lower()
            if (
                action != "remove"
                or not {"rule_id", "action"}.issubset(payload)
                or not set(payload).issubset({"rule_id", "action", "expected_revision"})
            ):
                raise ValueError("batch rule action is invalid")
            result = {
                "ok": True,
                **web_download_batch_manager().remove_rule(
                    rule_id,
                    expected_revision=payload.get("expected_revision"),
                ),
            }
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json(result)
