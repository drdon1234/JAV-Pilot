"""History maintenance endpoints: preview, cleanup, export and retention."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...config.runtime_config import (
    RuntimeConfigError,
    history_retention_config,
    history_retention_policy,
    update_history_retention_config,
)
from ...history.errors import (
    HistoryLifecycleConflictError,
    HistoryLifecycleError,
    HistoryLifecycleValidationError,
)
from ...history.models import MAX_CLEANUP_RECORDS
from ...history.retention import (
    HistoryRetentionSchedule,
    HistoryRetentionScheduleError,
    HistoryRetentionSchedulerError,
    HistoryRetentionStateError,
    validate_history_retention_state,
)
from ...library.errors import MediaLibraryError
from ...media_metadata.manager import MediaMetadataError
from ...media_metadata.store import MediaMetadataStoreError
from ...web_download.errors import WebDownloadError
from .. import state
from ..base import BaseHandler
from ..services.history import (
    history_backup_path,
    history_lifecycle,
    history_status_payload,
    recover_history_retention_scheduler_state,
    wake_history_retention_scheduler,
)


class HistoryRoutes(BaseHandler):
    def _handle_history_status(self) -> None:
        try:
            lifecycle = history_lifecycle()
            payload = history_status_payload(lifecycle)
        except (
            HistoryLifecycleError,
            HistoryRetentionSchedulerError,
            MediaLibraryError,
            MediaMetadataError,
            MediaMetadataStoreError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
        ) as exc:
            self._send_history_error(exc)
            return
        self._send_json(payload)

    def _handle_history_preview(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            if set(payload) != {"filters"} or not isinstance(
                payload.get("filters"), dict
            ):
                raise HistoryLifecycleValidationError(
                    "history preview request is invalid"
                )
            result = history_lifecycle().preview_cleanup(payload["filters"])
        except (
            HistoryLifecycleError,
            MediaLibraryError,
            MediaMetadataError,
            MediaMetadataStoreError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            self._send_history_error(exc)
            return
        self._send_json({"ok": True, **result})

    def _handle_history_execute(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if set(payload) != {"preview_token"}:
                raise HistoryLifecycleValidationError(
                    "history cleanup request is invalid"
                )
            result = history_lifecycle().execute_cleanup(payload.get("preview_token"))
        except (
            HistoryLifecycleError,
            MediaLibraryError,
            MediaMetadataError,
            MediaMetadataStoreError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            self._send_history_error(exc)
            return
        self._send_json({"ok": True, **result})

    def _handle_history_export(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            if set(payload) != {"filters", "format"} or not isinstance(
                payload.get("filters"), dict
            ):
                raise HistoryLifecycleValidationError(
                    "history export request is invalid"
                )
            exported = history_lifecycle().export_history(
                payload["filters"],
                format=payload.get("format"),
            )
        except (
            HistoryLifecycleError,
            MediaLibraryError,
            MediaMetadataError,
            MediaMetadataStoreError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            self._send_history_error(exc)
            return
        self._send_bytes(
            exported.body,
            content_type=exported.content_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="jav-pilot-history.{exported.extension}"'
                ),
                "X-History-Checksum": f"sha256:{exported.checksum}",
                "X-History-Record-Count": str(exported.record_count),
            },
        )

    def _handle_history_retention(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            action = payload.get("action")
            if action == "update":
                if (
                    not set(payload).issubset({"action", "policy", "schedule"})
                    or "policy" not in payload
                    or not isinstance(payload.get("policy"), dict)
                    or (
                        "schedule" in payload
                        and not isinstance(payload.get("schedule"), dict)
                    )
                ):
                    raise HistoryLifecycleValidationError(
                        "history retention request is invalid"
                    )
                policy = payload["policy"]
                schedule = payload.get("schedule", {})
                if set(policy) != {"web", "batch", "metadata"} or not set(
                    schedule
                ).issubset({"auto_enabled", "timezone", "hour"}):
                    raise HistoryLifecycleValidationError(
                        "history retention request is invalid"
                    )
                try:
                    prospective = HistoryRetentionSchedule.from_mapping(
                        {**history_retention_config(), **policy, **schedule}
                    )
                except HistoryRetentionScheduleError as exc:
                    raise HistoryLifecycleValidationError(str(exc)) from exc
                if prospective.active:
                    try:
                        validate_history_retention_state()
                    except HistoryRetentionStateError as exc:
                        raise HistoryLifecycleConflictError(
                            "automatic history retention state requires recovery"
                        ) from exc
                try:
                    update_history_retention_config({**policy, **schedule})
                except RuntimeConfigError as exc:
                    raise HistoryLifecycleValidationError(str(exc)) from exc
                wake_history_retention_scheduler()
                result = history_status_payload(history_lifecycle())
            elif action == "recover_state":
                if set(payload) != {"action"}:
                    raise HistoryLifecycleValidationError(
                        "history retention recovery request is invalid"
                    )
                current = HistoryRetentionSchedule.from_mapping(
                    history_retention_config()
                )
                if current.active:
                    raise HistoryLifecycleConflictError(
                        "disable automatic history retention before state recovery"
                    )
                try:
                    backup_name = recover_history_retention_scheduler_state()
                except HistoryRetentionSchedulerError as exc:
                    raise HistoryLifecycleConflictError(
                        "automatic history retention state could not be recovered"
                    ) from exc
                wake_history_retention_scheduler()
                result = {
                    **history_status_payload(history_lifecycle()),
                    "state_recovery": {
                        "recovered": backup_name is not None,
                        "backup_name": backup_name,
                    },
                }
            elif action == "preview":
                if not set(payload).issubset({"action", "limit"}):
                    raise HistoryLifecycleValidationError(
                        "history retention request is invalid"
                    )
                result = {
                    "ok": True,
                    **history_lifecycle().preview_retention(
                        history_retention_policy(),
                        limit=payload.get("limit", MAX_CLEANUP_RECORDS),
                    ),
                }
            else:
                raise HistoryLifecycleValidationError(
                    "history retention action is invalid"
                )
        except (
            HistoryLifecycleError,
            HistoryRetentionSchedulerError,
            MediaLibraryError,
            MediaMetadataError,
            MediaMetadataStoreError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            self._send_history_error(exc)
            return
        self._send_json(result)

    def _handle_history_vacuum(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if set(payload) != {"target"}:
                raise HistoryLifecycleValidationError(
                    "history vacuum request is invalid"
                )
            backup_path = history_backup_path()
            if backup_path is None:
                raise HistoryLifecycleConflictError(
                    "vacuum requires a configured verified backup"
                )
            result = history_lifecycle().vacuum(
                payload.get("target"),
                backup_path=backup_path,
            )
            state.HISTORY_BACKUP_STATUS_CACHE.clear()
        except (
            HistoryLifecycleError,
            MediaLibraryError,
            MediaMetadataError,
            MediaMetadataStoreError,
            RuntimeConfigError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            self._send_history_error(exc)
            return
        self._send_json({"ok": True, **result})

    def _send_history_error(self, exc: BaseException) -> None:
        if isinstance(exc, (HistoryLifecycleValidationError, ValueError)):
            self._send_json(
                {"ok": False, "error": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if isinstance(exc, HistoryLifecycleConflictError):
            self._send_json(
                {"ok": False, "error": str(exc)},
                HTTPStatus.CONFLICT,
            )
            return
        self._send_json(
            {"ok": False, "error": "History service is unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
