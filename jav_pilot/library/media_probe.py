from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..web_download.quality import QualityHeightError, validate_quality_height


FFPROBE_TIMEOUT_SECONDS = 5.0
MAX_FFPROBE_OUTPUT_BYTES = 64 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class LocalMediaProbeSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LocalFileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def probe_local_video_height(
    path: Path,
    allowed_root: Path,
    expected_identity: LocalFileIdentity,
) -> int | None:
    root = _regular_root(allowed_root)
    target = Path(path)
    if not target.is_absolute():
        raise LocalMediaProbeSafetyError("local media path must be absolute")
    _require_expected_identity(expected_identity)
    _require_unchanged(target, root, expected_identity)
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type,width,height",
        "-of",
        "json",
        str(target),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=FFPROBE_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        _require_unchanged(target, root, expected_identity)
        return None
    _require_unchanged(target, root, expected_identity)
    if (
        result.returncode != 0
        or not isinstance(result.stdout, bytes)
        or len(result.stdout) > MAX_FFPROBE_OUTPUT_BYTES
    ):
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    video_stream = (
        next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if isinstance(streams, list)
        else None
    )
    if video_stream is None:
        return None
    width = video_stream.get("width")
    height = video_stream.get("height")
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not 64 <= width <= 16_384
        or not 64 <= height <= 16_384
    ):
        return None
    try:
        return validate_quality_height(min(width, height))
    except QualityHeightError:
        return None


def _regular_root(path: Path) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise LocalMediaProbeSafetyError("local media root must be absolute")
    absolute = Path(os.path.abspath(raw))
    try:
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            current_stat = current.lstat()
            if _is_linklike(current_stat):
                raise LocalMediaProbeSafetyError(
                    "local media root contains an unsafe link"
                )
        root_stat = absolute.lstat()
        if _is_linklike(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise LocalMediaProbeSafetyError("local media root is unavailable")
        resolved = absolute.resolve(strict=True)
    except LocalMediaProbeSafetyError:
        raise
    except OSError as exc:
        raise LocalMediaProbeSafetyError("local media root is unavailable") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        raise LocalMediaProbeSafetyError(
            "local media root resolves outside its configured path"
        )
    return resolved


def _require_unchanged(
    path: Path,
    root: Path,
    expected: LocalFileIdentity,
) -> None:
    if _file_identity(path, root) != expected:
        raise LocalMediaProbeSafetyError(
            "local media changed while it was being inspected"
        )


def _file_identity(path: Path, root: Path) -> LocalFileIdentity:
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise LocalMediaProbeSafetyError(
            "local media path escaped its controlled root"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise LocalMediaProbeSafetyError("local media path is invalid")
    current = root
    try:
        for part in relative.parts[:-1]:
            current /= part
            current_stat = current.lstat()
            if _is_linklike(current_stat) or not stat.S_ISDIR(current_stat.st_mode):
                raise LocalMediaProbeSafetyError(
                    "local media path contains an unsafe directory"
                )
        file_stat = lexical.lstat()
        if (
            _is_linklike(file_stat)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
        ):
            raise LocalMediaProbeSafetyError("local media path is not a regular file")
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except LocalMediaProbeSafetyError:
        raise
    except (OSError, ValueError) as exc:
        raise LocalMediaProbeSafetyError(
            "local media path escaped its controlled root"
        ) from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(lexical)):
        raise LocalMediaProbeSafetyError("local media path contains an unsafe link")
    return LocalFileIdentity(
        device=int(file_stat.st_dev),
        inode=int(file_stat.st_ino),
        size=int(file_stat.st_size),
        modified_ns=int(file_stat.st_mtime_ns),
        changed_ns=int(file_stat.st_ctime_ns),
    )


def _require_expected_identity(identity: LocalFileIdentity) -> None:
    values = (
        identity.device,
        identity.inode,
        identity.size,
        identity.modified_ns,
        identity.changed_ns,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise LocalMediaProbeSafetyError("local media identity is invalid")
    if identity.device < 0 or identity.inode < 0 or identity.size <= 0:
        raise LocalMediaProbeSafetyError("local media identity is invalid")
    if identity.modified_ns < 0 or identity.changed_ns < 0:
        raise LocalMediaProbeSafetyError("local media identity is invalid")


def _is_linklike(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


__all__ = [
    "FFPROBE_TIMEOUT_SECONDS",
    "LocalFileIdentity",
    "LocalMediaProbeSafetyError",
    "MAX_FFPROBE_OUTPUT_BYTES",
    "probe_local_video_height",
]
