"""Extract one still from a size-limited 123AV MP4 preview."""
from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def preview_jpeg(data: bytes) -> bytes:
    if data[4:8] != b"ftyp":
        raise ValueError("invalid preview container")
    with TemporaryDirectory() as directory:
        clip = Path(directory) / "preview.mp4"
        frame = Path(directory) / "cover.jpg"
        clip.write_bytes(data)
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-loglevel", "error", "-max_alloc", "67108864",
                "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
                "-max_pixels", "4194304", "-threads", "1", "-i", str(clip),
                "-map", "0:v:0", "-frames:v", "1", "-filter_threads", "1",
                "-vf", "scale=480:480:force_original_aspect_ratio=decrease",
                "-c:v", "mjpeg", "-threads", "1", "-q:v", "3",
                "-f", "image2", "-update", "1", str(frame),
            ],
            check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return frame.read_bytes()
