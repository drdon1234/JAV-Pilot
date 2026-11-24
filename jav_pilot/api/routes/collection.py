"""Remote storage, cloud downloads and custom metadata sources."""

import sqlite3
from ..base import BaseHandler
from ..request import query_params, single_param, int_param
from ...library.errors import MediaLibraryError
from ...media_metadata.custom_source import settings


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

    def _handle_custom_source(self):
        self._send_json(settings())

    def _handle_custom_source_save(self):
        try:
            self._send_json(settings(self._read_json_body(8192)))
        except (ValueError, OSError, sqlite3.Error):
            self._send_json({"error": "请检查 HTTPS 接口与字段映射"}, 400)
