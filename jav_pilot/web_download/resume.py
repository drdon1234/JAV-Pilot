from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..core.catalog_code import normalize_catalog_code
from .variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
)

_CHECKPOINT_VERSION = 5
_LEGACY_CHECKPOINT_VERSION = 2
_PRE_VARIANT_CHECKPOINT_VERSION = 3
_PRE_CANONICAL_CODE_CHECKPOINT_VERSION = 4
_CHECKPOINT_NAME = "resume.json"
_CHECKPOINT_PART_NAME = "resume.json.part"
_JOURNAL_NAME = "resume.journal"
_JOURNAL_PART_NAME = "resume.journal.part"
_SEGMENTS_DIR_NAME = "segments"
_BATCH_PART_NAME = "batch.part"
_MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
_MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_MAX_SEGMENTS = 100_000
_HASH_CHUNK_BYTES = 4 * 1024 * 1024
_JOURNAL_BATCH_SEGMENTS = 16
_JOURNAL_FLUSH_SECONDS = 5.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_FILE_RE = re.compile(r"^(\d{8})\.(segment|part)$")
_BATCH_FILE_RE = re.compile(r"^batch-(\d{8})-(\d{8})-([0-9a-f]{16})\.segments$")
_EPHEMERAL_QUERY_KEYS = frozenset(
    {
        "auth",
        "authorization",
        "expire",
        "expires",
        "hdnea",
        "hdnts",
        "jwt",
        "keypairid",
        "policy",
        "session",
        "sig",
        "signature",
        "token",
    }
)
_EPHEMERAL_QUERY_KEYS_BY_MEDIA_SUFFIX = {
    "cdn-centaurus.com": frozenset({"s", "t"}),
    "premilkyway.com": frozenset({"s", "t"}),
}


class ResumeCheckpointError(RuntimeError):
    pass


class ResumeManifestMismatch(ResumeCheckpointError):
    pass


class ResumeIdentityMismatch(ResumeCheckpointError):
    pass


@dataclass(frozen=True)
class MediaSegment:
    index: int
    duration: str
    uri: str
    discontinuity: bool
    key_uri: str | None = None
    key_iv: bytes | None = None


@dataclass(frozen=True)
class MediaPlaylist:
    media_sequence: int
    segments: tuple[MediaSegment, ...]
    extinf_sha256: str
    structure_sha256: str

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def duration_seconds(self) -> float:
        return float(
            sum((Decimal(segment.duration) for segment in self.segments), Decimal(0))
        )


@dataclass(frozen=True)
class SegmentIntegrity:
    index: int
    size: int
    sha256: str
    batch: str | None = None
    offset: int = 0


@dataclass(frozen=True)
class SegmentSource:
    path: Path
    offset: int
    size: int


@dataclass(frozen=True)
class _RegularFileIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class ResumeIdentity:
    code: str
    code_key: str
    requested_height: int | None
    selected_height: int | None
    variant: MissavVariant = DEFAULT_WEB_DOWNLOAD_VARIANT


def parse_media_playlist(manifest: str, *, allow_aes128: bool = False) -> MediaPlaylist:
    if not isinstance(manifest, str):
        raise ValueError("media response is not an HLS manifest")
    lines = [raw_line.strip() for raw_line in manifest.splitlines() if raw_line.strip()]
    if not lines or lines[0] != "#EXTM3U" or lines.count("#EXTM3U") != 1:
        raise ValueError("media response is not an HLS manifest")
    if "#EXT-X-STREAM-INF" in manifest or "#EXT-X-I-FRAME-STREAM-INF" in manifest:
        raise ValueError("HLS master manifests are not accepted")

    media_sequence = 0
    saw_media_sequence = False
    saw_playlist_type = False
    saw_endlist = False
    pending_duration: str | None = None
    pending_discontinuity = False
    segments: list[MediaSegment] = []
    current_key_uri: str | None = None
    current_key_iv: bytes | None = None

    for line in lines[1:]:
        if saw_endlist:
            raise ValueError("HLS manifest has content after its end marker")
        if line == "#EXT-X-ENDLIST":
            if pending_duration is not None or pending_discontinuity:
                raise ValueError("HLS manifest has a dangling segment marker")
            saw_endlist = True
            continue
        if line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            if (
                saw_playlist_type
                or line.removeprefix("#EXT-X-PLAYLIST-TYPE:").strip() != "VOD"
            ):
                raise ValueError("only HLS VOD playlists are accepted")
            saw_playlist_type = True
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            if saw_media_sequence or segments or pending_duration is not None:
                raise ValueError("HLS media sequence is invalid")
            value = line.removeprefix("#EXT-X-MEDIA-SEQUENCE:").strip()
            try:
                media_sequence = int(value)
            except ValueError as exc:
                raise ValueError("HLS media sequence is invalid") from exc
            if media_sequence < 0 or str(media_sequence) != value:
                raise ValueError("HLS media sequence is invalid")
            saw_media_sequence = True
            continue
        if line.startswith("#EXT-X-SESSION-KEY:"):
            raise ValueError("encrypted HLS manifests are not accepted")
        if line.startswith("#EXT-X-KEY:"):
            if line == "#EXT-X-KEY:METHOD=NONE":
                current_key_uri = None
                current_key_iv = None
                continue
            if not allow_aes128:
                raise ValueError("encrypted HLS manifests are not accepted")
            attributes = _hls_attribute_list(line.removeprefix("#EXT-X-KEY:"))
            if set(attributes) - {"METHOD", "URI", "IV"} or attributes.get("METHOD") != "AES-128":
                raise ValueError("HLS encryption method is not accepted")
            key_uri = attributes.get("URI", "")
            if not (key_uri.startswith('"') and key_uri.endswith('"')):
                raise ValueError("HLS encryption key URI is invalid")
            key_uri = key_uri[1:-1]
            if not key_uri or len(key_uri) > 8192 or any(c in key_uri for c in "\r\n\0"):
                raise ValueError("HLS encryption key URI is invalid")
            raw_iv = attributes.get("IV")
            if raw_iv is not None:
                if re.fullmatch(r"0x[0-9A-Fa-f]{32}", raw_iv) is None:
                    raise ValueError("HLS encryption IV is invalid")
                current_key_iv = bytes.fromhex(raw_iv[2:])
            else:
                current_key_iv = None
            current_key_uri = key_uri
            continue
        if line.startswith("#EXT-X-TOKEN="):
            if re.fullmatch(r"#EXT-X-TOKEN=[A-Za-z0-9_=-]{1,512}", line) is None:
                raise ValueError("HLS media token tag is invalid")
            continue
        if line.startswith(
            (
                "#EXT-X-MAP:",
                "#EXT-X-BYTERANGE:",
                "#EXT-X-PART:",
                "#EXT-X-PRELOAD-HINT:",
            )
        ):
            raise ValueError("HLS manifest uses unsupported segment framing")
        if line == "#EXT-X-DISCONTINUITY":
            if pending_duration is not None or pending_discontinuity:
                raise ValueError("HLS discontinuity marker is invalid")
            pending_discontinuity = True
            continue
        if line.startswith("#EXTINF:"):
            if pending_duration is not None:
                raise ValueError("HLS segment duration is invalid")
            raw_duration = line.removeprefix("#EXTINF:").split(",", 1)[0].strip()
            pending_duration = _canonical_duration(raw_duration)
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-"):
                allowed_tag = line == "#EXT-X-INDEPENDENT-SEGMENTS" or line.startswith(
                    (
                        "#EXT-X-VERSION:",
                        "#EXT-X-TARGETDURATION:",
                        "#EXT-X-PROGRAM-DATE-TIME:",
                        "#EXT-X-DATERANGE:",
                        "#EXT-X-BITRATE:",
                        "#EXT-X-START:",
                        "#EXT-X-ALLOW-CACHE:",
                    )
                )
                if not allowed_tag:
                    raise ValueError("HLS manifest uses an unsupported media tag")
            continue
        if pending_duration is None:
            raise ValueError("HLS media segment has no duration")
        if len(segments) >= _MAX_SEGMENTS:
            raise ValueError("HLS manifest contains too many media segments")
        segments.append(
            MediaSegment(
                index=len(segments),
                duration=pending_duration,
                uri=line,
                discontinuity=pending_discontinuity,
                key_uri=current_key_uri,
                key_iv=current_key_iv,
            )
        )
        pending_duration = None
        pending_discontinuity = False

    if not saw_endlist:
        raise ValueError("live or incomplete HLS manifests are not accepted")
    if not segments:
        raise ValueError("HLS manifest contains no media segments")

    durations = [segment.duration for segment in segments]
    extinf_sha256 = _json_sha256(durations)
    structure_sha256 = _json_sha256(
        {
            "media_sequence": media_sequence,
            "segment_count": len(segments),
            "extinf_sha256": extinf_sha256,
            "discontinuities": [
                segment.index for segment in segments if segment.discontinuity
            ],
            "encryption": [
                {
                    "index": segment.index,
                    "key_uri_sha256": hashlib.sha256((segment.key_uri or "").encode("utf-8")).hexdigest(),
                    "iv": segment.key_iv.hex() if segment.key_iv is not None else None,
                }
                for segment in segments
                if segment.key_uri is not None
            ],
        }
    )
    return MediaPlaylist(
        media_sequence=media_sequence,
        segments=tuple(segments),
        extinf_sha256=extinf_sha256,
        structure_sha256=structure_sha256,
    )


def _hls_attribute_list(value: str) -> dict[str, str]:
    parts = re.findall(r'(?:^|,)([A-Z0-9-]+)=((?:"[^"\r\n]*")|[^,]*)', value)
    attributes = {name: item for name, item in parts}
    reconstructed = ",".join(f"{name}={item}" for name, item in parts)
    if not parts or reconstructed != value:
        raise ValueError("HLS encryption attributes are invalid")
    return attributes


class ResumeStore:
    def __init__(
        self,
        root: Path,
        playlist: MediaPlaylist,
        identity: ResumeIdentity,
        completed: dict[int, SegmentIntegrity],
        *,
        manifest_url: str | None,
    ) -> None:
        self.root = root
        self.playlist = playlist
        self.identity = _validated_identity(identity)
        self.manifest_url = manifest_url
        self.checkpoint_path = root / _CHECKPOINT_NAME
        self.checkpoint_part_path = root / _CHECKPOINT_PART_NAME
        self.journal_path = root / _JOURNAL_NAME
        self.journal_part_path = root / _JOURNAL_PART_NAME
        self.segments_dir = root / _SEGMENTS_DIR_NAME
        self.batch_part_path = self.segments_dir / _BATCH_PART_NAME
        self._completed = completed
        self._completed_bytes = sum(record.size for record in completed.values())
        self._pending: dict[int, SegmentIntegrity] = {}
        self._active_segment: tuple[int, int, _RegularFileIdentity] | None = None
        self._batch_part_identity: _RegularFileIdentity | None = None
        self._pending_batch_path: Path | None = None
        self._pending_batch_identity: _RegularFileIdentity | None = None
        self._last_flush_at = time.monotonic()
        self._journal_usable = True
        self._journal_directory_pending = not self.journal_path.exists()
        self._journal_identity = (
            _owned_path_identity(self.journal_path)
            if self.journal_path.exists()
            else None
        )
        self._checkpoint_reconciled = False

    @classmethod
    def prepare(
        cls,
        root: Path,
        playlist: MediaPlaylist,
        identity: ResumeIdentity,
        *,
        manifest_url: str | None = None,
        reset_on_manifest_mismatch: bool = False,
    ) -> "ResumeStore":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        _require_directory(root)
        segments_dir = root / _SEGMENTS_DIR_NAME
        segments_dir.mkdir(exist_ok=True)
        _require_directory(segments_dir)

        checkpoint_path = root / _CHECKPOINT_NAME
        checkpoint_part_path = root / _CHECKPOINT_PART_NAME
        journal_path = root / _JOURNAL_NAME
        journal_part_path = root / _JOURNAL_PART_NAME
        _unlink_regular_file(checkpoint_part_path)
        _unlink_regular_file(journal_part_path)
        journal_needs_rewrite = False
        if checkpoint_path.exists():
            stored_payload = _read_checkpoint(checkpoint_path)
            checkpoint_version = int(stored_payload["version"])
            payload = _checkpoint_payload_for_version(
                stored_payload,
                checkpoint_version=checkpoint_version,
            )
            _require_matching_identity(payload, identity)
            try:
                _require_matching_manifest(
                    payload,
                    playlist,
                    manifest_url=manifest_url,
                )
            except ResumeManifestMismatch:
                if not reset_on_manifest_mismatch:
                    raise
                _validate_discardable_resume_state(
                    payload,
                    journal_path,
                    checkpoint_version=checkpoint_version,
                )
                _discard_resume_state(root, segments_dir)
                reset_store = cls.prepare(
                    root,
                    playlist,
                    identity,
                    manifest_url=manifest_url,
                )
                reset_store._checkpoint_reconciled = True
                return reset_store
            completed = _parse_completed(payload, playlist.segment_count)
            journal_completed, journal_needs_rewrite = _read_journal(
                journal_path,
                playlist.segment_count,
                allow_batches=checkpoint_version != _LEGACY_CHECKPOINT_VERSION,
            )
            if set(completed).intersection(journal_completed):
                raise ResumeCheckpointError("resume checkpoint is invalid")
            completed.update(journal_completed)
            if checkpoint_version == _LEGACY_CHECKPOINT_VERSION:
                _validate_v2_segments_for_migration(segments_dir, completed)
            if checkpoint_version != _CHECKPOINT_VERSION:
                _write_checkpoint_atomically(
                    checkpoint_path,
                    checkpoint_part_path,
                    root,
                    payload,
                )
        else:
            completed = {}
            _unlink_regular_file(journal_path)

        store = cls(
            root,
            playlist,
            identity,
            completed,
            manifest_url=manifest_url,
        )
        changed = store._clean_and_revalidate_segments()
        if changed:
            _fsync_directory(segments_dir)
            store._checkpoint_reconciled = True
        if not checkpoint_path.exists():
            store._save_checkpoint()
        if changed or journal_needs_rewrite:
            store._rewrite_journal()
        return store

    @property
    def completed_indices(self) -> tuple[int, ...]:
        return tuple(sorted((*self._completed, *self._pending)))

    @property
    def checkpoint_reconciled(self) -> bool:
        return self._checkpoint_reconciled

    @property
    def completed_bytes(self) -> int:
        return self._completed_bytes + sum(
            record.size for record in self._pending.values()
        )

    @property
    def completed_count(self) -> int:
        return len(self._completed) + len(self._pending)

    def is_completed(self, index: int) -> bool:
        self._require_index(index)
        return index in self._completed or index in self._pending

    def segment_path(self, index: int) -> Path:
        self._require_index(index)
        record = self._completed.get(index) or self._pending.get(index)
        if record is not None and record.batch is not None:
            return self.segments_dir / record.batch
        if index in self._pending:
            return self.batch_part_path
        return self.segments_dir / f"{index:08d}.segment"

    @contextmanager
    def open_segment(self, index: int) -> Iterator[BinaryIO]:
        self._require_index(index)
        if self._active_segment is not None:
            raise ResumeCheckpointError("resume segment write is already active")
        if self.is_completed(index):
            raise ResumeCheckpointError("resume segment is already complete")
        if self._pending_batch_path is not None:
            raise ResumeCheckpointError("resume checkpoint could not be saved")
        expected_size = sum(record.size for record in self._pending.values())
        create = self._batch_part_identity is None
        flags = os.O_WRONLY | os.O_APPEND
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor, identity = _open_owned_regular_file(
            self.batch_part_path,
            flags,
            expected=self._batch_part_identity,
        )
        try:
            if os.fstat(descriptor).st_size != expected_size:
                raise ResumeCheckpointError("resume segment integrity is invalid")
            self._batch_part_identity = identity
            self._active_segment = (index, expected_size, identity)
            with os.fdopen(descriptor, "ab", buffering=0) as handle:
                descriptor = -1
                yield handle
                _require_owned_descriptor(handle.fileno(), identity)
            _require_owned_path(self.batch_part_path, identity)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def discard_part(self, index: int) -> None:
        self._require_index(index)
        if self._active_segment is None:
            return
        active_index, offset, identity = self._active_segment
        if active_index != index:
            raise ResumeCheckpointError("resume segment write is invalid")
        descriptor = -1
        try:
            descriptor, _ = _open_owned_regular_file(
                self.batch_part_path,
                os.O_WRONLY,
                expected=identity,
            )
            os.ftruncate(descriptor, offset)
            _require_owned_descriptor(descriptor, identity)
            _require_owned_path(self.batch_part_path, identity)
        except BaseException:
            self._batch_part_identity = None
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            self._active_segment = None

    def commit_segment(
        self,
        index: int,
        *,
        size: int | None = None,
        sha256: str | None = None,
    ) -> Path:
        self._require_index(index)
        if self.is_completed(index):
            raise ResumeCheckpointError("resume segment is already complete")
        if self._active_segment is None or self._active_segment[0] != index:
            raise ResumeCheckpointError("resume segment write is invalid")
        offset = self._active_segment[1]
        identity = self._active_segment[2]
        descriptor, _ = _open_owned_regular_file(
            self.batch_part_path,
            os.O_RDONLY,
            expected=identity,
        )
        try:
            actual_size = os.fstat(descriptor).st_size - offset
            if size is None and sha256 is None:
                size, sha256 = _descriptor_range_integrity(
                    descriptor,
                    offset,
                    actual_size,
                )
            elif (
                type(size) is not int
                or size <= 0
                or size != actual_size
                or not isinstance(sha256, str)
                or _SHA256_RE.fullmatch(sha256) is None
            ):
                raise ResumeCheckpointError("resume segment integrity is invalid")
            else:
                size = actual_size
            _require_owned_descriptor(descriptor, identity)
        finally:
            os.close(descriptor)
        _require_owned_path(self.batch_part_path, identity)
        if size <= 0 or sha256 is None:
            raise ResumeCheckpointError("resume segment is empty")
        record = SegmentIntegrity(index, size, sha256, offset=offset)
        self._pending[index] = record
        self._active_segment = None
        self.flush()
        return self.segment_path(index)

    def flush(self, *, force: bool = False) -> None:
        if not self._pending:
            return
        if self._active_segment is not None:
            raise ResumeCheckpointError("resume segment write is incomplete")
        if not self._journal_usable:
            raise ResumeCheckpointError("resume checkpoint could not be saved")
        now = time.monotonic()
        if (
            not force
            and len(self._pending) < _JOURNAL_BATCH_SEGMENTS
            and now - self._last_flush_at < _JOURNAL_FLUSH_SECONDS
        ):
            return

        records = tuple(
            sorted(self._pending.values(), key=lambda record: record.offset)
        )
        batch_descriptor = -1
        try:
            if self._pending_batch_path is None:
                # The journal is the commit record: persist batch data and its
                # directory entry before allowing the journal to reference it.
                identity = self._batch_part_identity
                if identity is None:
                    raise ResumeCheckpointError("resume segment path is invalid")
                batch_descriptor, _ = _open_owned_regular_file(
                    self.batch_part_path,
                    os.O_RDWR,
                    expected=identity,
                )
                if os.fstat(batch_descriptor).st_size != sum(
                    record.size for record in records
                ):
                    raise ResumeCheckpointError("resume segment integrity is invalid")
                os.fsync(batch_descriptor)
                _require_owned_descriptor(batch_descriptor, identity)
                _require_owned_path(self.batch_part_path, identity)
                os.close(batch_descriptor)
                batch_descriptor = -1
                batch_name = _batch_file_name(records)
                batch_path = self.segments_dir / batch_name
                if batch_path.exists() or batch_path.is_symlink():
                    raise ResumeCheckpointError("resume segment path is invalid")
                _require_owned_path(self.batch_part_path, identity)
                os.replace(self.batch_part_path, batch_path)
                _require_owned_path(batch_path, identity)
                batch_descriptor, _ = _open_owned_regular_file(
                    batch_path,
                    os.O_RDONLY,
                    expected=identity,
                )
                self._batch_part_identity = None
                self._pending_batch_path = batch_path
                self._pending_batch_identity = identity
                self._pending = {
                    record.index: SegmentIntegrity(
                        record.index,
                        record.size,
                        record.sha256,
                        batch=batch_name,
                        offset=record.offset,
                    )
                    for record in records
                }
                records = tuple(
                    sorted(
                        self._pending.values(),
                        key=lambda record: record.offset,
                    )
                )
            else:
                batch_path = self._pending_batch_path
                identity = self._pending_batch_identity
                if identity is None:
                    raise ResumeCheckpointError("resume segment path is invalid")
                batch_descriptor, _ = _open_owned_regular_file(
                    batch_path,
                    os.O_RDONLY,
                    expected=identity,
                )

            _require_owned_descriptor(batch_descriptor, identity)
            _require_owned_path(batch_path, identity)
            _fsync_directory(self.segments_dir)
            raw = _journal_batch_bytes(batch_path.name, records)
            existing_size = 0
            journal_existed = self.journal_path.exists()
            journal_descriptor = -1
            journal_identity: _RegularFileIdentity | None = None
            try:
                journal_flags = os.O_WRONLY | os.O_APPEND
                if not journal_existed:
                    journal_flags |= os.O_CREAT | os.O_EXCL
                    self._journal_directory_pending = True
                if not journal_existed and self._journal_identity is not None:
                    raise ResumeCheckpointError("resume journal path changed")
                journal_descriptor, journal_identity = _open_owned_regular_file(
                    self.journal_path,
                    journal_flags,
                    expected=self._journal_identity,
                )
                self._journal_identity = journal_identity
                existing_size = os.fstat(journal_descriptor).st_size
                if existing_size + len(raw) > _MAX_JOURNAL_BYTES:
                    raise ResumeCheckpointError("resume checkpoint is too large")
                _write_all(journal_descriptor, raw)
                os.fsync(journal_descriptor)
                _require_owned_descriptor(journal_descriptor, journal_identity)
                _require_owned_path(self.journal_path, journal_identity)
                _require_owned_descriptor(batch_descriptor, identity)
                _require_owned_path(batch_path, identity)
                if self._journal_directory_pending:
                    _fsync_directory(self.root)
                    self._journal_directory_pending = False
            except (OSError, ResumeCheckpointError) as exc:
                self._journal_usable = (
                    journal_descriptor >= 0
                    and journal_identity is not None
                    and _repair_owned_file(
                        journal_descriptor,
                        journal_identity,
                        existing_size,
                        path=self.journal_path,
                    )
                )
                raise ResumeCheckpointError(
                    "resume checkpoint could not be saved"
                ) from exc
            finally:
                if journal_descriptor >= 0:
                    os.close(journal_descriptor)
        except OSError as exc:
            raise ResumeCheckpointError("resume checkpoint could not be saved") from exc
        finally:
            if batch_descriptor >= 0:
                os.close(batch_descriptor)
        for record in records:
            self._completed[record.index] = record
            self._completed_bytes += record.size
        self._pending.clear()
        self._pending_batch_path = None
        self._pending_batch_identity = None
        self._last_flush_at = now

    def completed_sources(self) -> tuple[SegmentSource, ...]:
        if self.completed_count != self.playlist.segment_count:
            raise ResumeCheckpointError("resume checkpoint is incomplete")
        self.flush(force=True)
        return tuple(
            SegmentSource(
                path=self.segment_path(index),
                offset=self._completed[index].offset,
                size=self._completed[index].size,
            )
            for index in range(self.playlist.segment_count)
        )

    def _require_index(self, index: int) -> None:
        if type(index) is not int or not 0 <= index < self.playlist.segment_count:
            raise ResumeCheckpointError("resume segment index is invalid")

    def _clean_and_revalidate_segments(self) -> bool:
        changed = False
        referenced_batches = {
            record.batch
            for record in self._completed.values()
            if record.batch is not None
        }
        for path in self.segments_dir.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ResumeCheckpointError("resume segment path is unsafe")
            if path.name == _BATCH_PART_NAME:
                path.unlink()
                changed = True
                continue
            match = _SEGMENT_FILE_RE.fullmatch(path.name)
            if match is not None:
                index = int(match.group(1))
                kind = match.group(2)
                record = self._completed.get(index)
                if kind == "part" or record is None or record.batch is not None:
                    path.unlink()
                    changed = True
                continue
            if _BATCH_FILE_RE.fullmatch(path.name) is not None:
                if path.name not in referenced_batches:
                    path.unlink()
                    changed = True
                continue
            raise ResumeCheckpointError("resume segment path is invalid")

        for index, record in tuple(self._completed.items()):
            if record.batch is not None:
                continue
            path = self.segments_dir / f"{index:08d}.segment"
            if not path.exists():
                del self._completed[index]
                changed = True
                continue
            _require_regular_file(path)
            size, sha256 = _file_integrity(path)
            if size != record.size or sha256 != record.sha256:
                path.unlink()
                del self._completed[index]
                changed = True

        for batch_name in referenced_batches:
            if batch_name is None:
                continue
            records = tuple(
                sorted(
                    (
                        record
                        for record in self._completed.values()
                        if record.batch == batch_name
                    ),
                    key=lambda record: record.offset,
                )
            )
            path = self.segments_dir / batch_name
            valid = path.exists()
            if valid:
                _require_regular_file(path)
                valid = _batch_records_are_valid(path, records)
            if valid:
                continue
            if path.exists():
                _unlink_regular_file(path)
            for record in records:
                self._completed.pop(record.index, None)
            changed = True
        self._completed_bytes = sum(record.size for record in self._completed.values())
        return changed

    def _save_checkpoint(self) -> None:
        payload = {
            "version": _CHECKPOINT_VERSION,
            "identity": {
                "code": self.identity.code,
                "code_key": self.identity.code_key,
                "variant": self.identity.variant,
                "requested_height": self.identity.requested_height,
                "selected_height": self.identity.selected_height,
            },
            "manifest": {
                "media_sequence": self.playlist.media_sequence,
                "segment_count": self.playlist.segment_count,
                "extinf_sha256": self.playlist.extinf_sha256,
                "structure_sha256": self.playlist.structure_sha256,
                "segment_uri_sha256": _segment_uri_sha256(
                    self.playlist,
                    self.manifest_url,
                ),
            },
            "segments": [],
        }
        _write_checkpoint_atomically(
            self.checkpoint_path,
            self.checkpoint_part_path,
            self.root,
            payload,
        )

    def _rewrite_journal(self) -> None:
        raw = _journal_bytes_for_records(self._completed.values())
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise ResumeCheckpointError("resume checkpoint is too large")
        _unlink_regular_file(self.journal_part_path)
        try:
            with self.journal_part_path.open("xb") as handle:
                os.chmod(
                    self.journal_part_path,
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(self.journal_part_path, self.journal_path)
            _fsync_directory(self.root)
            self._journal_identity = _owned_path_identity(self.journal_path)
            self._journal_directory_pending = False
        except BaseException:
            _unlink_regular_file(self.journal_part_path)
            raise


def has_resume_checkpoint(root: Path) -> bool:
    path = Path(root) / _CHECKPOINT_NAME
    return path.exists() and path.is_file() and not path.is_symlink()


def load_resume_identity(root: Path) -> ResumeIdentity | None:
    checkpoint = Path(root) / _CHECKPOINT_NAME
    if not checkpoint.exists():
        return None
    payload = _read_checkpoint(checkpoint)
    payload = _checkpoint_payload_for_version(
        payload,
        checkpoint_version=int(payload["version"]),
    )
    raw_identity = payload.get("identity")
    if not isinstance(raw_identity, dict) or set(raw_identity) != {
        "code",
        "code_key",
        "variant",
        "requested_height",
        "selected_height",
    }:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    code = raw_identity.get("code")
    code_key = raw_identity.get("code_key")
    variant = raw_identity.get("variant")
    requested_height = raw_identity.get("requested_height")
    selected_height = raw_identity.get("selected_height")
    if (
        not isinstance(code, str)
        or not isinstance(code_key, str)
        or not isinstance(variant, str)
        or any(
            height is not None and type(height) is not int
            for height in (requested_height, selected_height)
        )
    ):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    return _validated_identity(
        ResumeIdentity(
            code=code,
            code_key=code_key,
            variant=variant,
            requested_height=requested_height,
            selected_height=selected_height,
        )
    )


def _canonical_duration(value: str) -> str:
    try:
        duration = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("HLS segment duration is invalid") from exc
    if not duration.is_finite() or duration <= 0 or duration > 86_400:
        raise ValueError("HLS segment duration is invalid")
    return format(duration.normalize(), "f")


def _json_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _segment_uri_sha256(
    playlist: MediaPlaylist,
    manifest_url: str | None,
) -> str:
    return _json_sha256(
        [
            _normalized_segment_uri(segment.uri, manifest_url)
            for segment in playlist.segments
        ]
    )


def _normalized_segment_uri(uri: str, manifest_url: str | None) -> str:
    if not isinstance(uri, str) or not uri or len(uri) > 8192:
        raise ResumeCheckpointError("resume manifest segment identity is invalid")
    resolved = urljoin(manifest_url, uri) if manifest_url else uri
    try:
        parsed = urlsplit(resolved)
        port = parsed.port
    except ValueError as exc:
        raise ResumeCheckpointError(
            "resume manifest segment identity is invalid"
        ) from exc
    if parsed.username is not None or parsed.password is not None:
        raise ResumeCheckpointError("resume manifest segment identity is invalid")
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if parsed.scheme or parsed.netloc:
        scheme = parsed.scheme.lower()
        if not scheme or not hostname:
            raise ResumeCheckpointError("resume manifest segment identity is invalid")
        default_port = (
            port is None
            or (scheme == "https" and port == 443)
            or (scheme == "http" and port == 80)
        )
        netloc = hostname if default_port else f"{hostname}:{port}"
    else:
        scheme = ""
        netloc = ""
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _ephemeral_query_key(key, hostname=hostname)
        ],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, parsed.path, query, ""))


def _ephemeral_query_key(key: str, *, hostname: str = "") -> bool:
    clean_key = str(key or "").strip().casefold()
    clean_hostname = str(hostname or "").strip().casefold().rstrip(".")
    provider_keys = next(
        (
            keys
            for suffix, keys in _EPHEMERAL_QUERY_KEYS_BY_MEDIA_SUFFIX.items()
            if clean_hostname == suffix or clean_hostname.endswith(f".{suffix}")
        ),
        frozenset(),
    )
    normalized = "".join(
        character for character in clean_key if character.isalnum()
    )
    return (
        clean_key in provider_keys
        or normalized in _EPHEMERAL_QUERY_KEYS
        or normalized.startswith("xamz")
        or normalized.startswith("xgoog")
    )


def _legacy_journal_record_bytes(record: SegmentIntegrity) -> bytes:
    return (
        json.dumps(
            {
                "index": record.index,
                "size": record.size,
                "sha256": record.sha256,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _batch_file_name(records: tuple[SegmentIntegrity, ...]) -> str:
    if not records or len(records) > _JOURNAL_BATCH_SEGMENTS:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    expected_offset = 0
    previous_index = -1
    identities: list[dict[str, object]] = []
    for record in records:
        if record.offset != expected_offset or record.index <= previous_index:
            raise ResumeCheckpointError("resume checkpoint is invalid")
        identities.append(
            {
                "index": record.index,
                "offset": record.offset,
                "size": record.size,
                "sha256": record.sha256,
            }
        )
        expected_offset += record.size
        previous_index = record.index
    digest = _json_sha256(identities)[:16]
    return f"batch-{records[0].index:08d}-{records[-1].index:08d}-{digest}.segments"


def _journal_batch_bytes(
    batch_name: str,
    records: tuple[SegmentIntegrity, ...],
) -> bytes:
    if batch_name != _batch_file_name(records) or any(
        record.batch != batch_name for record in records
    ):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    return (
        json.dumps(
            {
                "batch": batch_name,
                "segments": [
                    {
                        "index": record.index,
                        "offset": record.offset,
                        "size": record.size,
                        "sha256": record.sha256,
                    }
                    for record in records
                ],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _journal_bytes_for_records(records: Iterable[SegmentIntegrity]) -> bytes:
    ordered = sorted(records, key=lambda record: record.index)
    entries: list[tuple[int, bytes]] = []
    batches: dict[str, list[SegmentIntegrity]] = {}
    for record in ordered:
        if record.batch is None:
            entries.append((record.index, _legacy_journal_record_bytes(record)))
        else:
            batches.setdefault(record.batch, []).append(record)
    for batch_name, batch_records in batches.items():
        records_tuple = tuple(sorted(batch_records, key=lambda record: record.offset))
        entries.append(
            (
                min(record.index for record in records_tuple),
                _journal_batch_bytes(batch_name, records_tuple),
            )
        )
    return b"".join(raw for _, raw in sorted(entries, key=lambda entry: entry[0]))


def _write_checkpoint_atomically(
    checkpoint_path: Path,
    checkpoint_part_path: Path,
    root: Path,
    payload: dict[str, object],
) -> None:
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ResumeCheckpointError("resume checkpoint is too large")
    _unlink_regular_file(checkpoint_part_path)
    try:
        with checkpoint_part_path.open("xb") as handle:
            os.chmod(checkpoint_part_path, stat.S_IRUSR | stat.S_IWUSR)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(checkpoint_part_path, checkpoint_path)
        _fsync_directory(root)
    except BaseException:
        _unlink_regular_file(checkpoint_part_path)
        raise


def _read_checkpoint(path: Path) -> dict[str, object]:
    _require_regular_file(path)
    if path.stat().st_size > _MAX_CHECKPOINT_BYTES:
        raise ResumeCheckpointError("resume checkpoint is too large")
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise ResumeCheckpointError("resume checkpoint is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "identity",
        "manifest",
        "segments",
    }:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    if type(payload.get("version")) is not int or payload.get("version") not in {
        _LEGACY_CHECKPOINT_VERSION,
        _PRE_VARIANT_CHECKPOINT_VERSION,
        _PRE_CANONICAL_CODE_CHECKPOINT_VERSION,
        _CHECKPOINT_VERSION,
    }:
        raise ResumeCheckpointError("resume checkpoint version is unsupported")
    return payload


def _validated_identity(identity: ResumeIdentity) -> ResumeIdentity:
    if not isinstance(identity, ResumeIdentity):
        raise ResumeCheckpointError("resume identity is invalid")
    try:
        variant = normalize_web_download_variant(identity.variant)
    except (AttributeError, ValueError) as exc:
        raise ResumeCheckpointError("resume identity is invalid") from exc
    normalized_code = normalize_catalog_code(identity.code, max_length=64)
    legacy_key = (
        "".join(character for character in identity.code if character.isalnum())
        if isinstance(identity.code, str)
        else ""
    )
    if (
        not isinstance(identity.code, str)
        or not isinstance(identity.code_key, str)
        or normalized_code is None
        or not identity.code_key
        or not identity.code_key.isascii()
        or identity.code_key not in {normalized_code[1], legacy_key}
        or not identity.code_key.isupper()
        or any(
            height is not None and (type(height) is not int or height <= 0)
            for height in (identity.requested_height, identity.selected_height)
        )
    ):
        raise ResumeCheckpointError("resume identity is invalid")
    if identity.variant != variant:
        raise ResumeCheckpointError("resume identity is invalid")
    return ResumeIdentity(
        code=normalized_code[0],
        code_key=normalized_code[1],
        requested_height=identity.requested_height,
        selected_height=identity.selected_height,
        variant=variant,
    )


def _checkpoint_payload_for_version(
    payload: dict[str, object],
    *,
    checkpoint_version: int,
) -> dict[str, object]:
    raw_identity = payload.get("identity")
    legacy_identity_fields = {
        "code",
        "code_key",
        "requested_height",
        "selected_height",
    }
    current_identity_fields = {
        *legacy_identity_fields,
        "variant",
    }
    if not isinstance(raw_identity, dict):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    if checkpoint_version in {
        _LEGACY_CHECKPOINT_VERSION,
        _PRE_VARIANT_CHECKPOINT_VERSION,
    }:
        expected_identity_fields = legacy_identity_fields
    elif checkpoint_version in {
        _PRE_CANONICAL_CODE_CHECKPOINT_VERSION,
        _CHECKPOINT_VERSION,
    }:
        expected_identity_fields = current_identity_fields
    else:
        raise ResumeCheckpointError("resume checkpoint version is unsupported")
    if set(raw_identity) != expected_identity_fields:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    raw_code = raw_identity.get("code")
    raw_code_key = raw_identity.get("code_key")
    raw_variant = raw_identity.get("variant", DEFAULT_WEB_DOWNLOAD_VARIANT)
    requested_height = raw_identity.get("requested_height")
    selected_height = raw_identity.get("selected_height")
    try:
        variant = normalize_web_download_variant(raw_variant)
    except (AttributeError, ValueError) as exc:
        raise ResumeCheckpointError("resume checkpoint is invalid") from exc
    normalized_code = normalize_catalog_code(raw_code, max_length=64)
    legacy_key = (
        "".join(character for character in raw_code if character.isalnum())
        if isinstance(raw_code, str)
        else ""
    )
    if (
        not isinstance(raw_code, str)
        or not isinstance(raw_code_key, str)
        or not isinstance(raw_variant, str)
        or normalized_code is None
        or raw_variant != variant
        or not raw_code.isascii()
        or not raw_code_key
        or not raw_code_key.isascii()
        or not raw_code_key.isupper()
        or raw_code_key != legacy_key
        or any(
            height is not None and (type(height) is not int or height <= 0)
            for height in (requested_height, selected_height)
        )
    ):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    if checkpoint_version == _CHECKPOINT_VERSION:
        if (raw_code, raw_code_key) != normalized_code:
            raise ResumeCheckpointError("resume checkpoint is invalid")
        return payload
    migrated = dict(payload)
    migrated["version"] = _CHECKPOINT_VERSION
    migrated["identity"] = {
        "variant": variant,
        "code": normalized_code[0],
        "code_key": normalized_code[1],
        "requested_height": requested_height,
        "selected_height": selected_height,
    }
    return migrated


def _require_matching_identity(
    payload: dict[str, object], identity: ResumeIdentity
) -> None:
    expected_identity = _validated_identity(identity)
    raw_identity = payload.get("identity")
    if not isinstance(raw_identity, dict) or set(raw_identity) != {
        "code",
        "code_key",
        "variant",
        "requested_height",
        "selected_height",
    }:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    raw_code = raw_identity.get("code")
    raw_code_key = raw_identity.get("code_key")
    raw_variant = raw_identity.get("variant")
    raw_requested_height = raw_identity.get("requested_height")
    raw_selected_height = raw_identity.get("selected_height")
    if (
        not isinstance(raw_code, str)
        or not isinstance(raw_code_key, str)
        or not isinstance(raw_variant, str)
        or any(
            height is not None and type(height) is not int
            for height in (raw_requested_height, raw_selected_height)
        )
    ):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    if raw_identity != {
        "code": expected_identity.code,
        "code_key": expected_identity.code_key,
        "variant": expected_identity.variant,
        "requested_height": expected_identity.requested_height,
        "selected_height": expected_identity.selected_height,
    }:
        raise ResumeIdentityMismatch("resume media identity changed")


def _require_matching_manifest(
    payload: dict[str, object],
    playlist: MediaPlaylist,
    *,
    manifest_url: str | None,
) -> None:
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "media_sequence",
        "segment_count",
        "extinf_sha256",
        "structure_sha256",
        "segment_uri_sha256",
    }:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    media_sequence = manifest.get("media_sequence")
    segment_count = manifest.get("segment_count")
    extinf_sha256 = manifest.get("extinf_sha256")
    structure_sha256 = manifest.get("structure_sha256")
    segment_uri_sha256 = manifest.get("segment_uri_sha256")
    if (
        type(media_sequence) is not int
        or media_sequence < 0
        or type(segment_count) is not int
        or segment_count <= 0
        or not isinstance(extinf_sha256, str)
        or _SHA256_RE.fullmatch(extinf_sha256) is None
        or not isinstance(structure_sha256, str)
        or _SHA256_RE.fullmatch(structure_sha256) is None
        or not isinstance(segment_uri_sha256, str)
        or _SHA256_RE.fullmatch(segment_uri_sha256) is None
    ):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    expected = {
        "media_sequence": playlist.media_sequence,
        "segment_count": playlist.segment_count,
        "extinf_sha256": playlist.extinf_sha256,
        "structure_sha256": playlist.structure_sha256,
        "segment_uri_sha256": _segment_uri_sha256(playlist, manifest_url),
    }
    if manifest != expected:
        raise ResumeManifestMismatch("resume manifest structure changed")


def _parse_completed(
    payload: dict[str, object], segment_count: int
) -> dict[int, SegmentIntegrity]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or len(raw_segments) > segment_count:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    if raw_segments:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    return {}


def _read_journal(
    path: Path,
    segment_count: int,
    *,
    allow_batches: bool = True,
) -> tuple[dict[int, SegmentIntegrity], bool]:
    if not path.exists():
        return {}, False
    identity = _owned_path_identity(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ResumeCheckpointError("resume checkpoint is invalid") from exc
    _require_owned_path(path, identity)
    if len(raw) > _MAX_JOURNAL_BYTES:
        raise ResumeCheckpointError("resume checkpoint is too large")

    completed: dict[int, SegmentIntegrity] = {}
    batch_names: set[str] = set()
    needs_rewrite = False
    lines = raw.splitlines(keepends=True)
    for position, raw_line in enumerate(lines):
        if not raw_line.endswith(b"\n"):
            if position != len(lines) - 1:
                raise ResumeCheckpointError("resume checkpoint is invalid")
            needs_rewrite = True
            break
        try:
            raw_record = json.loads(raw_line[:-1].decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResumeCheckpointError("resume checkpoint is invalid") from exc
        if not isinstance(raw_record, dict):
            raise ResumeCheckpointError("resume checkpoint is invalid")
        if set(raw_record) == {"index", "size", "sha256"}:
            record = _parse_journal_segment(
                raw_record,
                segment_count,
                batch=None,
            )
            if record.index in completed:
                raise ResumeCheckpointError("resume checkpoint is invalid")
            completed[record.index] = record
            continue
        if not allow_batches:
            raise ResumeCheckpointError("resume checkpoint is invalid")
        if set(raw_record) != {"batch", "segments"}:
            raise ResumeCheckpointError("resume checkpoint is invalid")
        batch_name = raw_record.get("batch")
        raw_segments = raw_record.get("segments")
        if (
            not isinstance(batch_name, str)
            or _BATCH_FILE_RE.fullmatch(batch_name) is None
            or batch_name in batch_names
            or not isinstance(raw_segments, list)
            or not 1 <= len(raw_segments) <= _JOURNAL_BATCH_SEGMENTS
        ):
            raise ResumeCheckpointError("resume checkpoint is invalid")
        records: list[SegmentIntegrity] = []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict):
                raise ResumeCheckpointError("resume checkpoint is invalid")
            record = _parse_journal_segment(
                raw_segment,
                segment_count,
                batch=batch_name,
            )
            if record.index in completed:
                raise ResumeCheckpointError("resume checkpoint is invalid")
            completed[record.index] = record
            records.append(record)
        records_tuple = tuple(records)
        if batch_name != _batch_file_name(records_tuple):
            raise ResumeCheckpointError("resume checkpoint is invalid")
        batch_names.add(batch_name)
    return completed, needs_rewrite


def _parse_journal_segment(
    raw_record: dict[str, object],
    segment_count: int,
    *,
    batch: str | None,
) -> SegmentIntegrity:
    expected_keys = {"index", "size", "sha256"}
    if batch is not None:
        expected_keys.add("offset")
    if set(raw_record) != expected_keys:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    index = raw_record.get("index")
    size = raw_record.get("size")
    sha256 = raw_record.get("sha256")
    offset = raw_record.get("offset", 0)
    if (
        type(index) is not int
        or not 0 <= index < segment_count
        or type(size) is not int
        or size <= 0
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or type(offset) is not int
        or offset < 0
    ):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    return SegmentIntegrity(index, size, sha256, batch=batch, offset=offset)


def _validate_discardable_resume_state(
    payload: dict[str, object],
    journal_path: Path,
    *,
    checkpoint_version: int,
) -> None:
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ResumeCheckpointError("resume checkpoint is invalid")
    segment_count = manifest.get("segment_count")
    if type(segment_count) is not int or segment_count <= 0:
        raise ResumeCheckpointError("resume checkpoint is invalid")
    _parse_completed(payload, segment_count)
    _read_journal(
        journal_path,
        segment_count,
        allow_batches=checkpoint_version != _LEGACY_CHECKPOINT_VERSION,
    )


def _validate_v2_segments_for_migration(
    segments_dir: Path,
    completed: dict[int, SegmentIntegrity],
) -> None:
    _require_directory(segments_dir)
    for path in segments_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ResumeCheckpointError("resume segment path is unsafe")
        if _SEGMENT_FILE_RE.fullmatch(path.name) is None:
            raise ResumeCheckpointError("resume segment path is invalid")
    for record in completed.values():
        if record.batch is not None or record.offset != 0:
            raise ResumeCheckpointError("resume checkpoint is invalid")


def _file_integrity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _descriptor_range_integrity(
    descriptor: int,
    offset: int,
    size: int,
) -> tuple[int, str]:
    if offset < 0 or size < 0:
        raise ResumeCheckpointError("resume segment integrity is invalid")
    digest = hashlib.sha256()
    remaining = size
    os.lseek(descriptor, offset, os.SEEK_SET)
    while remaining > 0:
        chunk = os.read(descriptor, min(_HASH_CHUNK_BYTES, remaining))
        if not chunk:
            raise ResumeCheckpointError("resume segment integrity is invalid")
        digest.update(chunk)
        remaining -= len(chunk)
    return size, digest.hexdigest()


def _batch_records_are_valid(
    path: Path,
    records: tuple[SegmentIntegrity, ...],
) -> bool:
    try:
        if path.name != _batch_file_name(records):
            return False
        expected_size = sum(record.size for record in records)
        if path.stat().st_size != expected_size:
            return False
        with path.open("rb") as handle:
            for record in records:
                if handle.tell() != record.offset:
                    return False
                remaining = record.size
                digest = hashlib.sha256()
                while remaining > 0:
                    chunk = handle.read(min(_HASH_CHUNK_BYTES, remaining))
                    if not chunk:
                        return False
                    digest.update(chunk)
                    remaining -= len(chunk)
                if digest.hexdigest() != record.sha256:
                    return False
    except ResumeCheckpointError:
        return False
    return True


def _require_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ResumeCheckpointError("resume directory is unsafe")


def _require_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ResumeCheckpointError("resume file is unsafe")


def _regular_file_identity(file_stat: os.stat_result) -> _RegularFileIdentity:
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise ResumeCheckpointError("resume segment path is unsafe")
    return _RegularFileIdentity(file_stat.st_dev, file_stat.st_ino)


def _require_owned_descriptor(
    descriptor: int,
    expected: _RegularFileIdentity,
) -> None:
    identity = _regular_file_identity(os.fstat(descriptor))
    if identity != expected:
        raise ResumeCheckpointError("resume segment path changed")


def _owned_path_identity(path: Path) -> _RegularFileIdentity:
    try:
        if path.is_symlink():
            raise ResumeCheckpointError("resume segment path is unsafe")
        return _regular_file_identity(path.lstat())
    except OSError as exc:
        raise ResumeCheckpointError("resume segment path is unsafe") from exc


def _require_owned_path(path: Path, expected: _RegularFileIdentity) -> None:
    identity = _owned_path_identity(path)
    if identity != expected:
        raise ResumeCheckpointError("resume segment path changed")


def _open_owned_regular_file(
    path: Path,
    flags: int,
    *,
    expected: _RegularFileIdentity | None,
) -> tuple[int, _RegularFileIdentity]:
    safe_flags = (
        flags
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, safe_flags, 0o600)
    except OSError as exc:
        raise ResumeCheckpointError("resume segment path is unsafe") from exc
    try:
        identity = _regular_file_identity(os.fstat(descriptor))
        if expected is not None and identity != expected:
            raise ResumeCheckpointError("resume segment path changed")
        _require_owned_path(path, identity)
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("resume file write made no progress")
        remaining = remaining[written:]


def _repair_owned_file(
    descriptor: int,
    identity: _RegularFileIdentity,
    valid_size: int,
    *,
    path: Path,
) -> bool:
    try:
        _require_owned_descriptor(descriptor, identity)
        _require_owned_path(path, identity)
        os.ftruncate(descriptor, valid_size)
        os.fsync(descriptor)
        _require_owned_descriptor(descriptor, identity)
        _require_owned_path(path, identity)
        return True
    except (OSError, ResumeCheckpointError):
        return False


def _unlink_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise ResumeCheckpointError("resume file is unsafe")
    if not path.exists():
        return
    _require_regular_file(path)
    path.unlink()


def _discard_resume_state(root: Path, segments_dir: Path) -> None:
    """Discard only validated staging state after a manifest identity change."""

    _require_directory(root)
    _require_directory(segments_dir)
    for path in segments_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ResumeCheckpointError("resume segment path is unsafe")
        if (
            path.name != _BATCH_PART_NAME
            and _SEGMENT_FILE_RE.fullmatch(path.name) is None
            and _BATCH_FILE_RE.fullmatch(path.name) is None
        ):
            raise ResumeCheckpointError("resume segment path is invalid")
        path.unlink()
    _fsync_directory(segments_dir)
    for name in (
        _CHECKPOINT_PART_NAME,
        _JOURNAL_PART_NAME,
        _JOURNAL_NAME,
        _CHECKPOINT_NAME,
    ):
        _unlink_regular_file(root / name)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)
