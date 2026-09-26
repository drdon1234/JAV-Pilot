"""Review field catalog, limits and the records passed between review stages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ...config.source_catalog import METADATA_CATALOG, METADATA_PROFILES
from ..publish import _RegularFileIdentity

SCHEMA_COMPONENT = "media_metadata_review"
CURRENT_SCHEMA_VERSION = 6
REFETCH_RESTART_ERROR_CODE = "service_restarted"
REFETCH_CONFLICT_ERROR_CODE = "revision_conflict"
DEFAULT_PREVIEW_SECONDS = 10.0 * 60.0
MAX_REVIEW_IMAGE_BYTES = 12 * 1024 * 1024
MAX_REVIEW_IMAGE_PIXELS = 60_000_000
MAX_PUBLISH_JOURNAL_BYTES = 64 * 1024
MAX_PUBLISH_JOURNAL_ARTIFACTS = 16

PUBLISH_JOURNAL_DIRECTORY = ".media-metadata-review-journal"
PUBLISH_JOURNAL_VERSION = 1
PUBLISH_JOURNAL_FILE_RE = re.compile(r"^([a-f0-9]{32})\.json$")
PUBLISH_JOURNAL_TEMP_RE = re.compile(r"^\.([a-f0-9]{32})\.json\.[a-f0-9]{32}\.tmp$")
PUBLICATION_LOCK_NAME = ".publication.lock"
PUBLICATION_LOCK_BODY = b"metadata-review-publication-lock-v1\n"

METADATA_FIELDS = (
    "title",
    "original_title",
    "release_date",
    "duration_minutes",
    "rating",
    "makers",
    "publishers",
    "series",
    "directors",
    "actors",
    "tags",
    "description",
)
LIST_FIELDS = frozenset(
    {"makers", "publishers", "series", "directors", "actors", "tags"}
)
TEXT_FIELDS = frozenset({"title", "original_title", "release_date", "description"})
IMAGE_KINDS = frozenset({"portrait", "landscape"})
SNAPSHOT_SOURCES = METADATA_PROFILES | {"nfo", "missav"}
REMOTE_SOURCES = METADATA_PROFILES | {"missav"}
IMAGE_SOURCES = METADATA_PROFILES | {"nfo", "manual"}
NON_DESCRIPTION_FIELDS = frozenset(METADATA_FIELDS) - {"description"}
SOURCE_FIELDS = {
    "nfo": frozenset(METADATA_FIELDS),
    "javbus": NON_DESCRIPTION_FIELDS,
    "javdb": NON_DESCRIPTION_FIELDS,
    "fc2": NON_DESCRIPTION_FIELDS,
    "missav": frozenset({"description"}),
    **{source: frozenset(METADATA_FIELDS) for source in METADATA_CATALOG},
}
FIELD_PRIORITIES = {
    field_name: ("javbus", "javdb", "fc2", *METADATA_CATALOG, "nfo")
    for field_name in NON_DESCRIPTION_FIELDS
}
FIELD_PRIORITIES["description"] = ("missav", *METADATA_CATALOG, "nfo")

HEX_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RELEASE_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookies?|headers?|manifest(?:_url)?|password|referer|"
    r"secrets?|sessions?|tokens?|urls?)",
    flags=re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(
    r"(?:https?|wss?|ftp)://|://|www\.|"
    r"\b(?:authorization|cookies?|headers?|manifest(?:_url)?|password|referer|"
    r"secret|session|token|url)\s*[:=]|"
    r"[?&](?:authorization|cookie|password|secret|session|token)\s*=",
    flags=re.IGNORECASE,
)
INVALID_XML_BYTES_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_UNSET = object()


@dataclass(frozen=True, slots=True)
class DraftMetadata:
    code: str
    title: str
    original_title: str | None
    release_date: str | None
    duration_minutes: int | None
    rating: float | None
    makers: tuple[str, ...]
    publishers: tuple[str, ...]
    series: tuple[str, ...]
    directors: tuple[str, ...]
    actors: tuple[str, ...]
    tags: tuple[str, ...]
    description: str | None


@dataclass(frozen=True, slots=True)
class ArtifactState:
    body: bytes | None = field(repr=False)
    identity: _RegularFileIdentity | None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.body).hexdigest() if self.body is not None else None


@dataclass(frozen=True, slots=True)
class PreviewArtifact:
    relative_path: str
    target: Path
    body: bytes = field(repr=False)
    source_id: str
    kind: str
    original: ArtifactState

    @property
    def action(self) -> str:
        if self.original.body is None:
            return "create"
        if self.original.body == self.body:
            return "unchanged"
        return "replace"


@dataclass(frozen=True, slots=True)
class PublishPreview:
    preview_token: str
    review_id: str
    revision: int
    draft_digest: str
    created_at: float
    expires_at: float
    artifacts: tuple[PreviewArtifact, ...]


@dataclass(frozen=True, slots=True)
class JournalArtifact:
    relative_path: str
    action: str
    original_sha256: str | None
    proposed_sha256: str


@dataclass(frozen=True, slots=True)
class PublishJournal:
    publication_id: str
    review_id: str
    review_revision: int
    artifacts: tuple[JournalArtifact, ...]


@dataclass(frozen=True, slots=True)
class LoadedJournal:
    journal: PublishJournal
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class RecoveryArtifact:
    journal: JournalArtifact
    target: Path
    state: ArtifactState
    backup_body: bytes | None = field(default=None, repr=False)
