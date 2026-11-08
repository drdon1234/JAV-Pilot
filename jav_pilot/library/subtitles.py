"""Discover bounded sidecar subtitles beside an indexed video, without path input."""
from __future__ import annotations

import hashlib
from itertools import islice
from pathlib import Path, PurePosixPath

from .playback import open_library_file
from .models import VIDEO_SUFFIXES

SUBTITLE_SUFFIXES = {".srt", ".vtt", ".ass", ".ssa"}
MAX_SUBTITLE_BYTES = 4 * 1024 * 1024


def subtitle_files(root: Path, relative: str) -> list[dict]:
    media = PurePosixPath(relative)
    # Validate every component through the same descriptor-based opener as video.
    with open_library_file(root, relative):
        pass
    directory = root.joinpath(*media.parent.parts)
    try:
        files = sorted(islice(directory.iterdir(), 2000), key=lambda p: p.name.casefold())
    except OSError:
        return []
    sole_video = sum(p.suffix.lower() in VIDEO_SUFFIXES for p in files) == 1
    tracks = []
    for path in files:
        stem = path.stem.casefold()
        base = media.stem.casefold()
        if path.suffix.lower() not in SUBTITLE_SUFFIXES or not (
            stem == base or any(stem.startswith(base + separator) for separator in (".", "-", "_", " ")) or sole_video
        ):
            continue
        candidate = str(media.parent / path.name)
        try:
            with open_library_file(root, candidate) as (_, size, identity):
                if size > MAX_SUBTITLE_BYTES:
                    continue
        except (OSError, ValueError):
            continue
        tracks.append({"id": hashlib.sha256(candidate.encode()).hexdigest()[:24],
                       "label": path.name, "relative": candidate, "identity": identity})
        if len(tracks) == 64:
            break
    return tracks


def subtitle_text(root: Path, track: dict) -> str:
    with open_library_file(root, track["relative"]) as (stream, size, identity):
        if size > MAX_SUBTITLE_BYTES or identity != track["identity"]:
            raise ValueError("Subtitle changed")
        raw = stream.read(size)
    encodings = ["utf-8-sig", "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8", "gb18030", "big5", "cp932"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("Unsupported subtitle encoding")
