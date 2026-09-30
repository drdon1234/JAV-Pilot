"""Names of the subtitle files that belong to a media file.

Players pair ``<stem>.srt`` and ``<stem>.<language>.srt`` with ``<stem>.mp4``,
so every rename of a media file has to carry these files along.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import FORMATS, SCRIPTS

SUBTITLE_SUFFIXES = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"})


def is_subtitle_sidecar(name: str, media_stem: str) -> bool:
    if not media_stem or "/" in name or "\\" in name:
        return False
    dot = name.rfind(".")
    if dot <= 0 or name[dot:].lower() not in SUBTITLE_SUFFIXES:
        return False
    base = name[:dot]
    return base == media_stem or base.startswith(f"{media_stem}.")


def subtitle_sidecars(media_stem: str, names: Iterable[str]) -> list[str]:
    return sorted(name for name in names if is_subtitle_sidecar(name, media_stem))


def renamed_sidecar(name: str, old_stem: str, new_stem: str) -> str:
    if not is_subtitle_sidecar(name, old_stem):
        raise ValueError("name is not a subtitle of the media file")
    return f"{new_stem}{name[len(old_stem):]}"


def subtitle_file_name(media_stem: str, script: str, subtitle_format: str) -> str:
    return f"{media_stem}.{script}.{subtitle_format}"


def is_generated_subtitle(name: str, media_stem: str) -> bool:
    """Whether ``name`` is a subtitle file this application writes for the media."""

    return any(
        name == subtitle_file_name(media_stem, script, subtitle_format)
        for script in SCRIPTS
        for subtitle_format in FORMATS
    )
