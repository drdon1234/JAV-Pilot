"""Media library records, scan metrics and limits."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ASSET_STATES",
    "CURRENT_SCHEMA_VERSION",
    "MAX_NFO_BYTES",
    "PRESENCE_STATES",
    "ReconcileReport",
    "ScanMetrics",
]


SCHEMA_COMPONENT = "media_library"
CURRENT_SCHEMA_VERSION = 8
MAX_NFO_BYTES = 2 * 1024 * 1024
MAX_NFO_NODES = 4096
MAX_NFO_DEPTH = 8
MAX_NFO_TEXT_BYTES = 1024 * 1024
MAX_SCAN_DEPTH = 16
MAX_SCAN_ENTRIES = 1_000_000
DEFAULT_FULL_SCAN_SECONDS = 24 * 60 * 60
DEFAULT_AUDIT_STEP_SECONDS = 5 * 60
IN_PLACE_AUDIT_FRACTION = 0.01
MAX_AUDIT_DIRECTORIES = 512
MAX_REFRESH_PATHS = 256
VIDEO_SUFFIXES = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".rmvb",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)
PRESENCE_STATES = frozenset({"present", "missing", "unknown"})
ASSET_STATES = frozenset({"present", "missing", "invalid", "unknown"})
TERM_KINDS = frozenset({"actor", "maker", "publisher", "tag", "series", "director"})

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
PART_SUFFIX_RE = re.compile(
    r"(?:[-._ ](?:CD|DISC|DISK|PART|PT|VOL|VOLUME)[-._ ]?\d{1,3})$",
    flags=re.IGNORECASE,
)
SCAN_LAYOUT_NAME_RE = re.compile(
    r"^(?:CD|DISC|DISK|PART|PT|VOL|VOLUME|SEASON)[-._ ]?\d{1,3}$",
    flags=re.IGNORECASE,
)
SCAN_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:"
    r"FC2[-._ ]?(?:PPV[-._ ]?)?\d{2,9}"
    r"|"
    r"[A-Z0-9]{2,16}(?:[-._ ][A-Z0-9]{2,10})*[-._ ]\d{2,9}"
    r"|[A-Z]{2,12}\d{2,8}"
    r")(?![A-Z0-9])",
    flags=re.IGNORECASE,
)
QUALITY_RE = re.compile(
    r"(?<![A-Z0-9])(?:(4320|2160|1440|1080|720|576|540|480|360)P|([248])K)(?![A-Z0-9])",
    flags=re.IGNORECASE,
)
ENTRY_ID_RE = re.compile(r"^[a-f0-9]{40}$")
GENERATION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
ROOT_KEY_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_RELATIVE_MAX = 2048


@dataclass(frozen=True, slots=True)
class ScanMetrics:
    scandir_calls: int = 0
    stat_calls: int = 0
    entries_seen: int = 0
    directories_scanned: int = 0
    files_indexed: int = 0


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    root_key: str
    generation_id: str | None
    previous_generation_id: str | None
    scan_kind: str
    changed: bool
    published: bool
    present: int
    missing: int
    metrics: ScanMetrics


@dataclass(frozen=True, slots=True)
class MediaLibraryDedupRecord:
    code_key: str | None
    primary_media_path: str
    variant: str | None
    quality_height: int | None


@dataclass(frozen=True, slots=True)
class MediaLibraryDedupSnapshot:
    root_key: str
    revision: int
    generation_id: str
    records: tuple[MediaLibraryDedupRecord, ...]

    @property
    def revision_token(self) -> str:
        return f"{self.root_key}:{self.revision}:{self.generation_id}"


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    relative_path: str
    device: str
    inode: str
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class FileRecord:
    relative_path: str
    parent_path: str
    entry_id: str
    scope_path: str
    code: str | None
    code_key: str | None
    variant: str | None
    source: str
    device: str
    inode: str
    size: int
    modified_ns: int
    changed_ns: int
    suffix: str
    part_key: str | None
    quality_height: int | None
    quality_source: str | None
    nfo_status: str
    nfo_path: str | None
    nfo_json: str | None
    portrait_status: str
    landscape_status: str


@dataclass(slots=True)
class MutableMetrics:
    scandir_calls: int = 0
    stat_calls: int = 0
    entries_seen: int = 0
    directories_scanned: int = 0
    files_indexed: int = 0

    def freeze(self) -> ScanMetrics:
        return ScanMetrics(
            scandir_calls=self.scandir_calls,
            stat_calls=self.stat_calls,
            entries_seen=self.entries_seen,
            directories_scanned=self.directories_scanned,
            files_indexed=self.files_indexed,
        )
