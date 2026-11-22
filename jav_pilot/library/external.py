"""Short-lived, file-specific media grants and bounded live transcoding."""

import hashlib
import hmac
import re
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode

_KEY = secrets.token_bytes(32)
_SLOTS = threading.BoundedSemaphore(2)
LIFETIME = 6 * 3600


def signature(entry, index, identity, expires):
    value = f"{entry}:{index}:{identity}:{expires}".encode("ascii")
    return hmac.digest(_KEY, value, hashlib.sha256).hex()


def grant(entry, index, identity):
    expires = int(time.time()) + LIFETIME
    return "/api/library/external-media?" + urlencode(
        {
            "entry": entry,
            "file": index,
            "identity": identity,
            "expires": expires,
            "token": signature(entry, index, identity, expires),
        }
    )


def verify(entry, index, identity, expires, token):
    if (
        not re.fullmatch(r"[a-f0-9]{40}", entry)
        or not re.fullmatch(r"[a-f0-9]{64}", identity)
        or not re.fullmatch(r"[a-f0-9]{64}", token)
        or not time.time() <= expires <= time.time() + LIFETIME
    ):
        return False
    return hmac.compare_digest(signature(entry, index, identity, expires), token)


@contextmanager
def compatible_stream(stream):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("服务端未安装 FFmpeg")
    if not _SLOTS.acquire(blocking=False):
        raise ValueError("转码任务已满，请稍后重试")
    try:
        # The fd protocol can seek in the already verified local file (including MP4 with a trailing moov atom).
        process = subprocess.Popen(
            [
                executable,
                "-nostdin",
                "-loglevel",
                "error",
                "-threads",
                "2",
                "-protocol_whitelist",
                "fd,pipe",
                "-format_whitelist",
                "mov,matroska,webm,avi,mpegts,mpeg,asf,flv",
                "-i",
                "fd:",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                "pipe:1",
            ],
            stdin=stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        timer = threading.Timer(4 * 3600, process.kill)
        timer.daemon = True
        timer.start()
        try:
            yield process.stdout
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
    finally:
        _SLOTS.release()
