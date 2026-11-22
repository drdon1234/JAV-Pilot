"""115 Open Platform cloud tasks. Credentials stay on the application host."""

import json
import os
import re
from http.client import HTTPSConnection
from urllib.parse import urlencode

from ..torrent.magnet import parse_magnet


def available():
    return bool(os.getenv("JAV_PILOT_115_ACCESS_TOKEN")) and bool(
        re.fullmatch(r"\d{1,24}", os.getenv("JAV_PILOT_115_FOLDER_ID", ""))
    )


def _call(method, path, form=None):
    if not available():
        raise ValueError("尚未配置 115 开放平台令牌与目标目录 ID")
    conn = HTTPSConnection("proapi.115.com", timeout=15)
    try:
        conn.request(
            method,
            "/open/offline/" + path,
            body=urlencode(form) if form else None,
            headers={
                "Authorization": "Bearer " + os.environ["JAV_PILOT_115_ACCESS_TOKEN"],
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = conn.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError("115 请求失败，请检查授权和服务状态")
        result = json.loads(raw)
        if not isinstance(result, dict) or not result.get("state"):
            raise ValueError("115 拒绝了请求，请检查令牌、目录和配额")
        return result["data"]
    finally:
        conn.close()


def submit(uri):
    magnet = parse_magnet(uri)
    # Only the content hash is needed; private tracker parameters stay local.
    result = _call(
        "POST",
        "add_task_urls",
        {
            "urls": "magnet:?xt=urn:btih:" + magnet.info_hash,
            "wp_path_id": os.environ.get("JAV_PILOT_115_FOLDER_ID", ""),
        },
    )
    if not any(
        r.get("state") and r.get("info_hash", "").lower() == magnet.info_hash.lower()
        for r in result
    ):
        raise ValueError("115 未确认添加该任务，请查看云下载列表")
    return {"ok": True}


def tasks(page):
    if not 1 <= page <= 10000:
        raise ValueError("页码无效")
    data = _call("GET", "get_task_list?page=" + str(page))
    return {
        "tasks": [
            {key: r.get(key) for key in ("name", "info_hash", "percentDone", "status")}
            for r in data.get("tasks", [])
        ],
        "more": page < int(data.get("page_count") or 0),
    }
