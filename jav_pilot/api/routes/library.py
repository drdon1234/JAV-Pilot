"""Media library endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...library.errors import (
    MediaLibraryConflictError,
    MediaLibraryError,
    MediaLibraryUnavailableError,
)
from ..base import BaseHandler
from ..request import int_param, query_params, single_param
from ..services.media import media_library_manager


class MediaLibraryRoutes(BaseHandler):
    def _handle_media_library(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            manager = media_library_manager()
            entry_id = single_param(params, "entry_id")
            if entry_id:
                item = manager.index.store.get_entry(entry_id)
                status = manager.status()
                self._send_json(
                    {
                        "ok": True,
                        "item": item,
                        "index_state": status["state"],
                        "revision": status["revision"],
                        "last_error_code": status["last_error_code"],
                        "last_completed_at": status["last_completed_at"],
                    }
                )
                return
            result = manager.index.store.list_entries(
                root_key=manager.index.root_key,
                limit=int_param(params, "limit", 50),
                offset=int_param(params, "offset", 0),
                query=single_param(params, "q") or None,
                actor=single_param(params, "actor") or None,
                maker=single_param(params, "maker") or None,
                tag=single_param(params, "tag") or None,
                series=single_param(params, "series") or None,
                source=single_param(params, "source") or None,
                presence=single_param(params, "presence") or None,
                completeness=single_param(params, "completeness") or None,
                anomaly=single_param(params, "anomaly") or None,
                min_height=single_param(params, "min_height") or None,
                max_height=single_param(params, "max_height") or None,
            )
            status = manager.status()
        except MediaLibraryUnavailableError:
            self._send_json(
                {"ok": False, "error": "Media library index is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except (QueryError, ValueError, MediaLibraryError) as exc:
            status_code = (
                HTTPStatus.NOT_FOUND
                if "not found" in str(exc).casefold()
                else HTTPStatus.BAD_REQUEST
            )
            self._send_json({"ok": False, "error": str(exc)}, status_code)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Media library index is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(
            {
                "ok": True,
                **result,
                "index_state": status["state"],
                "revision": status["revision"],
                "last_error_code": status["last_error_code"],
                "last_completed_at": status["last_completed_at"],
            }
        )

    def _handle_media_library_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if set(payload) != {"action", "expected_revision"}:
                raise ValueError("media library action fields are invalid")
            action = str(payload.get("action") or "").strip().lower()
            if action not in {"rebuild", "accept_root_change"}:
                raise ValueError("media library action is invalid")
            expected_revision = payload.get("expected_revision")
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or not 0 <= expected_revision <= 2**63 - 1
            ):
                raise ValueError("media library revision is invalid")
            manager = media_library_manager()
            before = manager.status()
            if (
                action == "accept_root_change"
                and before["last_error_code"] != "root_changed"
            ):
                raise MediaLibraryConflictError(
                    "media library root change is not pending"
                )
            report = manager.reconcile_now(
                force_full=True,
                accept_root_change=action == "accept_root_change",
                expected_revision=expected_revision,
            )
            status = manager.status()
        except MediaLibraryConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except MediaLibraryUnavailableError:
            self._send_json(
                {"ok": False, "error": "Media library index is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except (ValueError, MediaLibraryError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Media library index is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(
            {
                "ok": True,
                "scan_kind": report.scan_kind,
                "changed": report.changed,
                "published": report.published,
                "present": report.present,
                "missing": report.missing,
                "index_state": status["state"],
                "revision": status["revision"],
            }
        )
