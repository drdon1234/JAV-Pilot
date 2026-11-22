"""Resolve one mounted video through its configured HTTPS WebDAV redirect service."""

import base64
import json
import os
import re
import stat
from functools import lru_cache
from http.client import HTTPSConnection, HTTPException
from pathlib import Path
from urllib.parse import quote, urlsplit

from .filesystem import safe_relative_path
from .models import VIDEO_SUFFIXES
from .mounts import directory, mount_root

_HOST = re.compile(r"(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


@lru_cache(maxsize=1)
def _sources(raw):
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise ValueError("网盘直连配置无效")
    sources = {}
    for name, options in values.items():
        if not isinstance(options, dict):
            raise ValueError("网盘直连配置无效")
        base = urlsplit(str(options.get("webdav", "")))
        hosts = options.get("hosts")
        if (
            base.scheme != "https"
            or not base.hostname
            or base.query
            or base.fragment
            or base.username is not None
            or not isinstance(hosts, list)
            or not hosts
            or not all(
                isinstance(host, str) and _HOST.fullmatch(host) for host in hosts
            )
        ):
            raise ValueError("请配置 HTTPS WebDAV 根地址与网盘直链域名")
        sources[name] = (
            base,
            tuple(hosts),
            str(options.get("username", "")),
            str(options.get("password", "")),
        )
    return sources


def sources():
    return _sources(os.getenv("JAV_PILOT_DIRECT_PLAYBACK", "{}"))


def media_origins():
    try:
        return sorted(
            {
                "https://" + host
                for _, hosts, _, _ in sources().values()
                for host in hosts
            }
        )
    except ValueError:
        return []


def resolve(name, relative, user_agent):
    root = mount_root(name)
    relative = safe_relative_path(relative, allow_root=False)
    path = Path(relative)
    if path.suffix.lower() not in VIDEO_SUFFIXES or name not in sources():
        raise ValueError("该文件未配置直连播放")
    with directory(root, str(path.parent)) as parent:
        info = (
            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if isinstance(parent, int)
            else (parent / path.name).lstat()
        )
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("媒体文件不可用")
    base, hosts, username, password = sources()[name]
    headers = {"Range": "bytes=0-0", "User-Agent": user_agent[:512]}
    if username or password:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        )
    conn = HTTPSConnection(base.hostname, base.port, timeout=8)
    try:
        conn.request(
            "GET",
            base.path.rstrip("/") + "/" + quote(relative, safe="/"),
            headers=headers,
        )
        response = conn.getresponse()
        url = response.getheader("Location") or ""
        target = urlsplit(url)
        if (
            response.status not in {301, 302, 303, 307, 308}
            or target.scheme != "https"
            or target.username is not None
            or target.port not in (None, 443)
            or not target.hostname
            or not any(
                target.hostname.endswith(h[1:])
                if h.startswith("*.")
                else target.hostname == h
                for h in hosts
            )
        ):
            raise ValueError("网盘未返回可用直链，请切换 NAS 代理播放")
        return url
    except (HTTPException, OSError) as exc:
        raise ValueError("直连服务不可用，请切换 NAS 代理播放") from exc
    finally:
        conn.close()
