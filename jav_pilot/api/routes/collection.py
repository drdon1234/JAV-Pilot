"""Personal library browsing and optional collection integrations."""

import sqlite3
from http import HTTPStatus
from ..base import BaseHandler
from ..request import query_params, single_param, int_param
from ..services.media import media_library_manager
from ...library.errors import MediaLibraryError
from ...media_metadata.custom_source import settings
from ...watching.history import records
from ...watching.personal import playlists, edit


class CollectionRoutes(BaseHandler):
    def _handle_storage(self, query_string):
        from ...library import mounts, cloud_download, direct_playback, fnos_download

        try:
            params = query_params(query_string)
            name = single_param(params, "mount")
            if name and single_param(params, "mode") == "direct":
                url = direct_playback.resolve(name, single_param(params, "path"), self.headers.get("User-Agent", ""))
                self._send_json({"url": url})
                return
            if name:
                payload = mounts.browse(
                    name, single_param(params, "path"), int_param(params, "offset", 0)
                )
            elif single_param(params, "cloud") == "1":
                payload = cloud_download.tasks(int_param(params, "page", 1))
            else:
                payload = {
                    "mounts": list(mounts.mounts()),
                    "cloud": cloud_download.available(),
                    "fnos": bool(fnos_download.configuration()),
                    "direct": list(direct_playback.sources()),
                }
            self._send_json(payload)
        except (ValueError, OSError, sqlite3.Error, MediaLibraryError):
            self._send_json({"error": "无法读取指定的存储或云任务"}, 400)

    def _handle_cloud_submit(self):
        from ...library import cloud_download, fnos_download

        try:
            body = self._read_json_body(2048)
            provider = body.get("provider", "115")
            if provider not in ("115", "fnos"):
                raise ValueError("下载服务无效")
            service = fnos_download if provider == "fnos" else cloud_download
            self._send_json(service.submit(str(body.get("magnet", ""))))
        except (ValueError, OSError) as exc:
            self._send_json(
                {
                    "error": str(exc)
                    if isinstance(exc, ValueError)
                    else "115 云下载不可用"
                },
                400,
            )

    def _handle_explore(self, query_string):
        try:
            params = query_params(query_string)
            view = single_param(params, "view") or "unseen"
            name = single_param(params, "name")
            index = media_library_manager().index
            after = single_param(params, "after")
            if after and view == "playlist":
                entries = playlists(name)
                remaining = entries[entries.index(after)+1:] if after in entries else []
                result = index.store.explore(index.root_key, seen=[], view="playlist", playlist=remaining)
                self._send_json({"next":result["entries"][0]["entry_id"] if result["entries"] else None})
                return
            seen = list(
                {
                    r["entry"]
                    for r in records()
                    if r.get("kind") == "local" and r.get("entry")
                }
            )
            result = index.store.explore(
                index.root_key,
                seen=seen,
                view=view,
                name=name,
                offset=int_param(params, "offset", 0),
                playlist=playlists(name) if view == "playlist" else [],
            )
            result["playlists"] = playlists()
            self._send_json(result)
        except (ValueError, OSError, sqlite3.Error, MediaLibraryError):
            self._send_json({"error": "无法读取馆藏浏览资料"}, HTTPStatus.BAD_REQUEST)

    def _handle_playlist(self):
        try:
            body = self._read_json_body(2048)
            entry = str(body.get("entry", ""))
            index = media_library_manager().index
            if body.get("action") != "remove":
                index.store.get_entry(entry, root_key=index.root_key)
            edit(body.get("name"), entry, body.get("action") == "remove")
            self._send_json({"ok": True})
        except (ValueError, OSError, sqlite3.Error, MediaLibraryError):
            self._send_json({"error": "无法更新播放列表"}, 400)

    def _handle_custom_source(self):
        self._send_json(settings())

    def _handle_custom_source_save(self):
        try:
            self._send_json(settings(self._read_json_body(8192)))
        except (ValueError, OSError, sqlite3.Error):
            self._send_json({"error": "请检查 HTTPS 接口与字段映射"}, 400)
