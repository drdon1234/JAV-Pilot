"""Verification of downloaded video streams with ffprobe."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .quality import (
    QualityHeightError,
    height_matches_selected_quality,
    validate_quality_height,
)
from .worker_common import (
    AUDIO_VIDEO_DURATION_TOLERANCE_SECONDS,
    HLS_DURATION_TOLERANCE_SECONDS,
)
from .worker_errors import HlsDurationMismatch, WebDownloadWorkerError

def verify_video(
    path: Path,
    *,
    selected_height: int | None = None,
    expected_duration_seconds: float | None = None,
) -> int:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise WebDownloadWorkerError("downloaded media is not a regular file")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-show_entries",
        "stream=codec_type,width,height,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=45,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WebDownloadWorkerError("video verification could not run") from exc
    if result.returncode != 0:
        raise WebDownloadWorkerError("downloaded media failed video verification")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WebDownloadWorkerError(
            "video verification returned invalid data"
        ) from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    stream_list = streams if isinstance(streams, list) else []
    video_streams = [
        stream
        for stream in stream_list
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    video_stream = video_streams[0] if video_streams else None
    if video_stream is None:
        raise WebDownloadWorkerError("downloaded media contains no video stream")
    actual_width = video_stream.get("width")
    actual_height = video_stream.get("height")
    if (
        isinstance(actual_width, bool)
        or isinstance(actual_height, bool)
        or not isinstance(actual_width, int)
        or not isinstance(actual_height, int)
        or not 64 <= actual_width <= 16_384
        or not 64 <= actual_height <= 16_384
    ):
        raise WebDownloadWorkerError("downloaded media has invalid video dimensions")
    try:
        quality_height = validate_quality_height(min(actual_width, actual_height))
    except QualityHeightError as exc:
        raise WebDownloadWorkerError(
            "downloaded media has invalid video dimensions"
        ) from exc

    def stream_duration(stream: object) -> float | None:
        if not isinstance(stream, dict):
            return None
        try:
            duration = float(stream.get("duration"))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(duration) or duration <= 0:
            return None
        return duration

    video_durations = [
        duration
        for stream in video_streams
        if (duration := stream_duration(stream)) is not None
    ]
    audio_durations = [
        duration
        for stream in stream_list
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        if (duration := stream_duration(stream)) is not None
    ]
    if video_durations and audio_durations:
        if max(audio_durations) + AUDIO_VIDEO_DURATION_TOLERANCE_SECONDS < max(
            video_durations
        ):
            raise HlsDurationMismatch(
                "downloaded audio stream is shorter than the video stream"
            )
    if selected_height is not None and not height_matches_selected_quality(
        quality_height,
        selected_height,
    ):
        raise WebDownloadWorkerError(
            "downloaded media resolution did not match the selected quality"
        )
    if expected_duration_seconds is not None:
        try:
            expected_duration = float(expected_duration_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadWorkerError("HLS manifest duration is invalid") from exc
        if not math.isfinite(expected_duration) or expected_duration <= 0:
            raise WebDownloadWorkerError("HLS manifest duration is invalid")
        format_payload = payload.get("format") if isinstance(payload, dict) else None
        raw_duration = (
            format_payload.get("duration")
            if isinstance(format_payload, dict)
            else None
        )
        try:
            actual_duration = float(raw_duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadWorkerError(
                "downloaded media has no valid duration"
            ) from exc
        if not math.isfinite(actual_duration) or actual_duration <= 0:
            raise WebDownloadWorkerError("downloaded media has no valid duration")
        measured_duration = max(video_durations, default=actual_duration)
        if measured_duration + HLS_DURATION_TOLERANCE_SECONDS < expected_duration:
            raise HlsDurationMismatch(
                "downloaded media is shorter than its HLS manifest"
            )
    return quality_height
