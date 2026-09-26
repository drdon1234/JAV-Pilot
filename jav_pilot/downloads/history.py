"""Code-level download history shared by every download entry point.

A work can reach the library through qBittorrent, the Web queue, or by being
copied into the library by hand. Before creating another download the UI asks
this module whether the same catalog code already exists anywhere, so a user
never downloads a work twice by accident.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..core.catalog_code import CodePattern, parse_code_pattern
from ..web_download.jobs import ACTIVE_STATUSES as WEB_ACTIVE_STATUSES

MAX_HISTORY_LOOKUP_CODES = 200
_NAME_CODE_RE = re.compile(
    r"(?<![A-Z0-9])"
    r"(FC2[-_. ]*(?:PPV[-_. ]*)?\d{2,9}|[A-Z0-9]*[A-Z][A-Z0-9]*[-_. ]?\d{2,9})"
    # Release suffixes: subtitles, uncensored leaks, quality tags.
    r"(?:[-_ ]?(?:C|CH|UC|U|HHB\d*|FHD|HD|4K|UHD))?"
    r"(?![A-Z0-9])"
)
_WEB_ACTIVE = frozenset(WEB_ACTIVE_STATUSES) - {"cancelling"}


class DownloadHistoryError(ValueError):
    pass


def code_identity(value: object) -> tuple[str, int] | None:
    """Separator- and zero-padding-insensitive identity: ``SSIS-0123`` == ``ssis123``."""

    pattern = parse_code_pattern(value)
    if pattern is None or pattern.number is None:
        return None
    return pattern.prefix, pattern.number


def code_identities_in_name(name: object) -> frozenset[tuple[str, int]]:
    clean = unicodedata.normalize("NFKC", str(name or "")).upper()
    identities: set[tuple[str, int]] = set()
    for match in _NAME_CODE_RE.finditer(clean):
        identity = code_identity(match.group(1))
        if identity is not None:
            identities.add(identity)
    return frozenset(identities)


@dataclass
class CodeHistory:
    code: str
    identity: tuple[str, int]
    torrent: list[str] = field(default_factory=list)
    web: list[str] = field(default_factory=list)
    library: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        torrent_state = _torrent_state(self.torrent)
        web_state = _web_state(self.web)
        downloaded = bool(self.library) or torrent_state == "completed" or web_state == "completed"
        active = torrent_state in {"downloading", "paused"} or web_state == "active"
        return {
            "code": self.code,
            "torrent": torrent_state,
            "web": web_state,
            "library": bool(self.library),
            "library_path": self.library[0] if self.library else None,
            "state": "downloaded" if downloaded else "active" if active else "none",
        }


def _torrent_state(stages: Sequence[str]) -> str | None:
    if not stages:
        return None
    for stage in ("completed", "downloading", "paused", "error"):
        if stage in stages:
            return stage
    return "downloading"


def _web_state(statuses: Sequence[str]) -> str | None:
    if "completed" in statuses:
        return "completed"
    if any(status in _WEB_ACTIVE for status in statuses):
        return "active"
    return None


def normalize_lookup_codes(values: object) -> list[tuple[str, tuple[str, int]]]:
    if not isinstance(values, list) or not values:
        raise DownloadHistoryError("codes must be a non-empty array")
    if len(values) > MAX_HISTORY_LOOKUP_CODES:
        raise DownloadHistoryError("too many catalog codes")
    seen: set[tuple[str, int]] = set()
    output: list[tuple[str, tuple[str, int]]] = []
    for value in values:
        if not isinstance(value, str) or len(value) > 64:
            raise DownloadHistoryError("catalog code is invalid")
        identity = code_identity(value)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        output.append((value.strip(), identity))
    return output


def lookup_download_history(
    codes: Iterable[tuple[str, tuple[str, int]]],
    *,
    torrent_tasks: Callable[[], Iterable[Mapping[str, object]]] | None,
    web_jobs: Callable[[str], Iterable[Mapping[str, object]]] | None,
    library_entries: Callable[[str], Iterable[Mapping[str, object]]] | None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Return per-code history and the list of sources that could not be read."""

    histories = [CodeHistory(code, identity) for code, identity in codes]
    by_identity = {history.identity: history for history in histories}
    unavailable: list[str] = []

    if torrent_tasks is not None:
        try:
            for task in torrent_tasks():
                stage = str(task.get("stage") or "")
                state = str(task.get("state") or "")
                if state in {"pausedDL", "stoppedDL"} and stage != "completed":
                    stage = "paused"
                for identity in code_identities_in_name(task.get("name")):
                    history = by_identity.get(identity)
                    if history is not None:
                        history.torrent.append(stage or "downloading")
        except Exception:  # noqa: BLE001 - one unreachable history source must not hide the others.
            unavailable.append("torrent")

    for source, reader, target in (
        ("web", web_jobs, "web"),
        ("library", library_entries, "library"),
    ):
        if reader is None:
            continue
        try:
            for history in histories:
                pattern = CodePattern(*history.identity)
                for row in reader(history.code):
                    if code_identity(row.get("code") or row.get("code_key")) != (
                        pattern.prefix,
                        pattern.number,
                    ):
                        continue
                    if target == "web":
                        status = str(row.get("status") or "")
                        if status == "completed" and row.get("archive_status") == "missing":
                            continue  # the archived file was deleted since
                        history.web.append(status)
                    elif str(row.get("presence") or "present") != "missing":
                        history.library.append(str(row.get("primary_media_path") or ""))
        except Exception:  # noqa: BLE001 - reported as unavailable, never as "not downloaded".
            unavailable.append(source)
    return [history.to_dict() for history in histories], unavailable
