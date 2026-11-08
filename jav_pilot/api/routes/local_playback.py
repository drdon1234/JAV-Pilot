"""Authenticated, seekable playback of indexed media library files."""
from __future__ import annotations

import math
import sqlite3
from pathlib import PurePosixPath
from urllib.parse import urlencode

from ...core.guards import QueryError
from ...library.errors import MediaLibraryError
from ...library.filesystem import regular_root, stat_identity
from ...library.models import MutableMetrics
from ...library.playback import byte_range, open_video, read_progress, save_progress
from ...library.subtitles import subtitle_files, subtitle_text
from ...watching.history import duration_value, history_id, record
from ..base import BaseHandler
from ..request import int_param, query_params, single_param
from ..services.media import media_library_manager


def indexed_file(entry_id: str, file_index: int):
    manager = media_library_manager()
    _, root_stat = regular_root(manager.index.root_path, MutableMetrics())
    published = manager.index.store.root(manager.index.root_key)
    if not published or stat_identity(root_stat) != (published["device"], published["inode"]):
        raise ValueError("Media library root changed")
    item = manager.index.store.get_entry(entry_id, root_key=manager.index.root_key)
    paths = item["media_paths"]
    if item["presence"] != "present" or not 0 <= file_index < len(paths):
        raise ValueError("Local video is unavailable")
    return manager.index.root_path, item, paths[file_index]


class LocalPlaybackRoutes(BaseHandler):
    def _handle_local_playback(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            entry_id = single_param(params, "entry")
            index = int_param(params, "file", 0)
            root, item, relative = indexed_file(entry_id, index)
            with open_video(root, relative) as (_, size, identity):
                position = read_progress(entry_id, identity)
            self._send_json({"ok": True, "entry": item, "file": index, "identity": identity,
                             "size": size, "position": position,
                             "subtitles": [{"id": track["id"], "label": track["label"], "url": "/api/library/subtitle?" + urlencode({"entry": entry_id, "file": index, "track": track["id"], "identity": track["identity"]})} for track in subtitle_files(root, relative)],
                             "media_url": "/api/library/media?" + urlencode({"entry": entry_id, "file": index, "identity": identity})})
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            self._send_json({"ok": False, "error": "Local video is unavailable"}, 404)

    def _handle_local_subtitle(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            root, _, relative = indexed_file(single_param(params, "entry"), int_param(params, "file", 0))
            track = next((track for track in subtitle_files(root, relative) if track["id"] == single_param(params, "track") and track["identity"] == single_param(params, "identity")), None)
            if track is None:
                raise ValueError("Subtitle unavailable")
            self._send_json({"text": subtitle_text(root, track)})
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            self._send_json({"ok": False, "error": "Subtitle is unavailable"}, 404)

    def _handle_local_media(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            root, _, relative = indexed_file(single_param(params, "entry"), int_param(params, "file", 0))
            with open_video(root, relative) as (stream, size, identity):
                if single_param(params, "identity") != identity:
                    self._send_json({"ok": False, "error": "Media file changed; reopen playback"}, 409)
                    return
                try:
                    start, end = byte_range(self.headers.get("Range"), size)
                except ValueError:
                    self._send_json({"ok": False, "error": "Invalid byte range"}, 416, headers={"Content-Range": f"bytes */{size}"})
                    return
                mime = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime"}.get(PurePosixPath(relative).suffix.lower(), "application/octet-stream")
                self.send_response(206 if self.headers.get("Range") else 200)
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, max-age=120")
                self.send_header("Vary", "Cookie")
                self.send_header("ETag", f'"{identity}"')
                self.send_header("Content-Length", str(end - start + 1))
                if self.headers.get("Range"):
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    data = stream.read(min(256 * 1024, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            if not self._response_started:
                self._send_json({"ok": False, "error": "Local video is unavailable"}, 404)

    def _handle_local_progress(self) -> None:
        try:
            body = self._read_json_body(2048)
            index = body.get("file", 0)
            position = body.get("position")
            duration = duration_value(body.get("duration", 0))
            if isinstance(index, bool) or not isinstance(index, int) or isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) or not 0 <= position <= 7 * 86400:
                raise ValueError("Invalid playback position")
            root, item, relative = indexed_file(str(body.get("entry", "")), index)
            with open_video(root, relative) as (_, _, identity):
                if body.get("identity") != identity:
                    raise ValueError("Media file changed")
                save_progress(str(item["entry_id"]), identity, float(position))
                record({"id": history_id("local", f"{item['entry_id']}:{identity}"), "kind": "local", "entry": item["entry_id"], "file": index, "identity": identity, "title": item["title"] or item["code"] or "", "code": item["code"] or "", "source": "", "url": "", "cover": "", "position": float(position), "duration": duration, "completed": body.get("completed") is True})
            self._send_json({"ok": True})
        except (ValueError, QueryError, MediaLibraryError, OSError, sqlite3.Error):
            self._send_json({"ok": False, "error": "Unable to save playback position"}, 400)
