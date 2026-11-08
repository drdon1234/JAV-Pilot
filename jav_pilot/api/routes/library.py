"""Media library endpoints."""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath
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
from ...library.playback import open_library_file
from ...library.folders import cover_selection, set_cover_selection, catalog_prefix
from ...library.artwork import portrait_thumbnail, cover_entry
from .local_playback import indexed_file


class MediaLibraryRoutes(BaseHandler):
    def _handle_library_folder_cover(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            manager = media_library_manager()
            prefix = single_param(params, "group") or ""
            root = manager.index.root_path
            selected = cover_selection(manager.index.root_key, prefix)
            entries = []
            candidates = single_param(params, "candidates") == "1"
            if candidates:
                result = manager.index.store.list_entries(
                    root_key=manager.index.root_key,
                    group_key=prefix,
                    presence="present",
                    query=single_param(params, "q") or None,
                    limit=50,
                    offset=int_param(params, "offset", 0),
                )
                entries = [
                    cover
                    for item in result["items"]
                    if (cover := cover_entry(root, item)) is not None
                ]
                self._send_json(
                    {
                        "ok": True,
                        "items": entries,
                        "has_more": result["has_more"],
                        "next_offset": result["offset"] + len(result["items"]),
                    }
                )
                return
            for entry_id in selected:
                try:
                    item = manager.index.store.get_entry(
                        entry_id, root_key=manager.index.root_key
                    )
                    if (
                        catalog_prefix(item["code"]) == prefix
                        and (cover := cover_entry(root, item)) is not None
                    ):
                        entries.append(cover)
                except MediaLibraryError:
                    continue
            # Fill unavailable selections from the current default order.
            offset = 0
            seen = {entry["entry_id"] for entry in entries}
            while len(entries) < 9:
                result = manager.index.store.list_entries(
                    root_key=manager.index.root_key,
                    group_key=prefix,
                    presence="present",
                    limit=50,
                    offset=offset,
                )
                for item in result["items"]:
                    if (
                        item["entry_id"] not in seen
                        and (cover := cover_entry(root, item)) is not None
                    ):
                        seen.add(item["entry_id"])
                        entries.append(cover)
                        if len(entries) == 9:
                            break
                offset += len(result["items"])
                if not result["has_more"]:
                    break
            self._send_json(
                {
                    "ok": True,
                    "group": prefix,
                    "entries": entries,
                    "custom": bool(selected),
                }
            )
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Folder cover is unavailable"},
                HTTPStatus.BAD_REQUEST,
            )

    def _handle_library_folder_cover_action(self) -> None:
        try:
            payload = self._read_json_body(8192)
            prefix = str(payload.get("group") or "")
            entries = payload.get("entries")
            if (
                not prefix
                or not isinstance(entries, list)
                or len(entries) > 9
                or any(not isinstance(item, str) for item in entries)
                or len(set(entries)) != len(entries)
            ):
                raise ValueError("Folder cover selection is invalid")
            manager = media_library_manager()
            for entry_id in entries:
                item = manager.index.store.get_entry(
                    entry_id, root_key=manager.index.root_key
                )
                if (
                    catalog_prefix(item["code"]) != prefix
                    or cover_entry(manager.index.root_path, item) is None
                ):
                    raise ValueError("An existing portrait in this folder is required")
            set_cover_selection(manager.index.root_key, prefix, entries)
            self._send_json({"ok": True, "entries": entries})
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "Folder cover selection is invalid"},
                HTTPStatus.BAD_REQUEST,
            )

    def _handle_library_artwork(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            root, item, _ = indexed_file(single_param(params, "entry"), 0)
            if single_param(params, "thumbnail") == "1":
                raw, identity = portrait_thumbnail(root, item)
                self._send_bytes(
                    raw,
                    content_type="image/jpeg",
                    cache_control="private, max-age=300",
                    headers={"ETag": f'"{identity}"'},
                )
                return
            media = PurePosixPath(item["primary_media_path"])
            artwork = "fanart" if single_param(params, "kind") == "landscape" else "poster"
            name = (
                f"{media.stem}-{artwork}.jpg"
                if media.parent == PurePosixPath(".")
                else f"{artwork}.jpg"
            )
            with open_library_file(root, str(media.parent / name)) as (
                stream,
                size,
                identity,
            ):
                if size > 16 * 1024 * 1024:
                    raise ValueError("Artwork is too large")
                etag = f'"{identity}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                raw = stream.read(size)
                if not raw.startswith(b"\xff\xd8\xff"):
                    raise ValueError("Invalid poster")
            self._send_bytes(
                raw,
                content_type="image/jpeg",
                cache_control="private, max-age=300",
                headers={"ETag": etag},
            )
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            self._send_json({"ok": False, "error": "Artwork is unavailable"}, 404)

    def _handle_media_library(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            manager = media_library_manager()
            entry_id = single_param(params, "entry_id")
            if entry_id:
                item = manager.index.store.get_entry(
                    entry_id, root_key=manager.index.root_key
                )
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
                grouped=single_param(params, "grouped") == "1",
                group_key=single_param(params, "group") or None,
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
