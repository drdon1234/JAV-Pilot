"""Chinese subtitle endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...library.errors import MediaLibraryError, MediaLibraryUnavailableError
from ...subtitles.manager import (
    MAX_BATCH_ITEMS,
    SubtitleConfig,
    SubtitleError,
    SubtitleMediaMissingError,
    SubtitleMultipartError,
    SubtitleUnavailableError,
)
from ...subtitles.store import SubtitleStoreError
from ..base import BaseHandler
from ..request import query_params, single_param, strict_int_param
from ..services.media import media_library_manager
from ..services.subtitles import subtitle_manager

_ERROR_STATUS = {
    "subtitle_job_not_found": HTTPStatus.NOT_FOUND,
    "subtitle_candidate_not_found": HTTPStatus.NOT_FOUND,
    "subtitle_job_busy": HTTPStatus.CONFLICT,
    "subtitle_file_conflict": HTTPStatus.CONFLICT,
    "subtitle_file_modified": HTTPStatus.CONFLICT,
    "subtitle_not_present": HTTPStatus.CONFLICT,
    "subtitle_disabled": HTTPStatus.SERVICE_UNAVAILABLE,
    "subtitle_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
    "subtitle_provider_unavailable": HTTPStatus.BAD_GATEWAY,
}
_EMPTY_SUMMARY = {
    "total": 0,
    "waiting": 0,
    "running": 0,
    "completed": 0,
    "not_found": 0,
    "skipped": 0,
    "failed": 0,
}
_BATCH_PAGE_SIZE = 100


class SubtitleRoutes(BaseHandler):
    def _handle_subtitles(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            limit = strict_int_param(params, "limit", 50)
            offset = strict_int_param(params, "offset", 0)
            status_filter = single_param(params, "filter") or "all"
            query = single_param(params, "q") or None
            if not 1 <= limit <= 200 or not 0 <= offset <= 10_000_000:
                raise QueryError("subtitle pagination is invalid")
            config = SubtitleConfig.from_env()
            if not config.enabled:
                self._send_json(
                    {
                        "ok": True,
                        "enabled": False,
                        "jobs": [],
                        "count": 0,
                        "offset": offset,
                        "limit": limit,
                        "has_more": False,
                        "summary": dict(_EMPTY_SUMMARY),
                    }
                )
                return
            manager = subtitle_manager(config)
            jobs = manager.list(limit=limit, offset=offset, status_filter=status_filter, query=query)
            count = manager.count(status_filter=status_filter, query=query)
            summary = manager.summary()
        except (QueryError, SubtitleStoreError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except SubtitleError as exc:
            self._send_subtitle_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_subtitle_error(SubtitleUnavailableError("subtitle storage is unavailable"))
            return
        self._send_json(
            {
                "ok": True,
                "enabled": True,
                "jobs": jobs,
                "count": count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(jobs) < count,
                "summary": summary,
            }
        )

    def _handle_subtitle_candidates(self, query_string: str) -> None:
        try:
            job_id = single_param(query_params(query_string), "job_id").strip()
            manager = subtitle_manager()
            job = manager.get(job_id)
            listing = manager.candidates(job_id)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except SubtitleError as exc:
            self._send_subtitle_error(exc)
            return
        except (OSError, sqlite3.Error, SubtitleStoreError):
            self._send_subtitle_error(SubtitleUnavailableError("subtitle storage is unavailable"))
            return
        self._send_json({"ok": True, "job": job, **listing})

    def _handle_subtitle_entry(self, query_string: str) -> None:
        try:
            entry_id = single_param(query_params(query_string), "entry_id").strip()
            entry = media_library_manager().index.store.get_entry(entry_id)
            media_paths = [str(path) for path in entry.get("media_paths") or []]
            job = (
                subtitle_manager().store.get_by_path(media_paths[0])
                if len(media_paths) == 1
                else None
            )
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MediaLibraryUnavailableError:
            self._send_library_unavailable()
            return
        except MediaLibraryError:
            self._send_entry_not_found()
            return
        except SubtitleError as exc:
            self._send_subtitle_error(exc)
            return
        except (OSError, sqlite3.Error, SubtitleStoreError):
            self._send_subtitle_error(SubtitleUnavailableError("subtitle storage is unavailable"))
            return
        self._send_json({"ok": True, "job": job, "multipart": len(media_paths) > 1})

    def _handle_subtitle_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            action = str(payload.get("action") or "").strip().lower()
            manager = subtitle_manager()
            result: dict[str, object]
            if action == "fetch":
                _require_fields(payload, {"action", "entry_id"})
                media_path, code, variant = _library_entry_media(str(payload["entry_id"]))
                result = {"job": manager.request(media_path, code, variant)}
            elif action == "batch_missing":
                _require_fields(payload, {"action"})
                result = {"queued": manager.request_batch(_missing_subtitle_items())}
            elif action in {"retry", "remove", "forget"}:
                _require_fields(payload, {"action", "job_id"})
                job_id = payload["job_id"]
                if action == "retry":
                    result = {"job": manager.retry(job_id)}
                elif action == "remove":
                    result = {"job": manager.remove(job_id)}
                else:
                    manager.forget(job_id)
                    result = {}
            elif action == "select":
                _require_fields(payload, {"action", "job_id", "candidate_id"})
                result = {"job": manager.select(payload["job_id"], payload["candidate_id"])}
            else:
                raise ValueError("unsupported subtitle action")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except SubtitleError as exc:
            self._send_subtitle_error(exc)
            return
        except MediaLibraryUnavailableError:
            self._send_library_unavailable()
            return
        except MediaLibraryError:
            self._send_entry_not_found()
            return
        except SubtitleStoreError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_subtitle_error(SubtitleUnavailableError("subtitle storage is unavailable"))
            return
        self._send_json({"ok": True, **result})

    def _send_subtitle_error(self, error: SubtitleError) -> None:
        status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
        self._send_json({"ok": False, "error": str(error), "code": error.code}, status)

    def _send_library_unavailable(self) -> None:
        self._send_json(
            {
                "ok": False,
                "error": "media library is unavailable",
                "code": "subtitle_library_unavailable",
            },
            HTTPStatus.SERVICE_UNAVAILABLE,
        )

    def _send_entry_not_found(self) -> None:
        self._send_json(
            {
                "ok": False,
                "error": "library entry was not found",
                "code": "subtitle_entry_not_found",
            },
            HTTPStatus.NOT_FOUND,
        )


def _require_fields(payload: dict[str, object], fields: set[str]) -> None:
    if set(payload) != fields:
        raise ValueError("subtitle action fields are invalid")


def _library_entry_media(entry_id: str) -> tuple[str, str, str | None]:
    entry = media_library_manager().index.store.get_entry(entry_id)
    media_paths = [str(path) for path in entry.get("media_paths") or []]
    code = entry.get("code")
    if entry.get("presence") != "present" or not code or not media_paths:
        raise SubtitleMediaMissingError("library entry has no identified media")
    if len(media_paths) != 1:
        raise SubtitleMultipartError("multi-part media is not supported")
    variant = entry.get("variant")
    return media_paths[0], str(code), str(variant) if variant else None


def _missing_subtitle_items() -> list[tuple[str, str, str | None]]:
    library = media_library_manager()
    items: list[tuple[str, str, str | None]] = []
    offset = 0
    while len(items) < MAX_BATCH_ITEMS:
        page = library.index.store.list_entries(
            root_key=library.index.root_key,
            limit=_BATCH_PAGE_SIZE,
            offset=offset,
            presence="present",
            anomaly="subtitle",
        )
        for entry in page["items"]:
            media_paths = entry.get("media_paths") or []
            if entry.get("code") and len(media_paths) == 1:
                variant = entry.get("variant")
                items.append(
                    (str(media_paths[0]), str(entry["code"]), str(variant) if variant else None)
                )
        if not page.get("has_more"):
            break
        offset += _BATCH_PAGE_SIZE
    return items[:MAX_BATCH_ITEMS]
