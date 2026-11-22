"""Delegate magnet downloads to fnOS through an administrator-configured official CLI."""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from ..torrent.magnet import parse_magnet


def configuration():
    binary = Path(os.getenv("JAV_PILOT_FNOS_CLI", ""))
    target = os.getenv("JAV_PILOT_FNOS_DOWNLOAD_DIR", "")
    if not (binary.is_absolute() and binary.is_file() and os.access(binary, os.X_OK)):
        return None
    if (
        not re.fullmatch(r"/vol[0-9]+/[^\x00-\x1f]+", target)
        or ".." in PurePosixPath(target).parts
    ):
        return None
    return binary, target


def submit(uri):
    config = configuration()
    if not config:
        raise ValueError("请配置飞牛 CLI 与下载目录")
    binary, target = config
    magnet = parse_magnet(uri)
    try:
        result = subprocess.run(
            [
                str(binary),
                "download",
                "add-uri",
                "magnet:?xt=urn:btih:" + magnet.info_hash,
                target,
                "--yes",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=True,
        )
        reply = json.loads(result.stdout)
        # The CLI exposes either the operation's fields or its decoded response body.
        if isinstance(reply, dict):
            data = reply.get("data", reply)
            if isinstance(data, dict):
                body = data.get("body", data)
                if isinstance(body, dict) and not body.get("errs"):
                    ids = body.get("ids") or []
                    if (
                        isinstance(ids, list)
                        and ids
                        and all(type(id_) is int and id_ > 0 for id_ in ids)
                    ):
                        return {"ok": True, "ids": ids}
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    raise ValueError("飞牛未确认创建任务，请在 NAS 下载中心核对任务与授权后再试")
