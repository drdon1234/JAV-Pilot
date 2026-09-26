"""Crash-safe publication: journals, artifact backups, replacement and recovery."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ...config.source_catalog import METADATA_PROFILES
from ..publish import (
    MAX_NFO_BYTES,
    MetadataPublishConflict,
    MetadataPublishError,
    _apply_file_metadata,
    _descriptor_xattrs,
    _directory_entry_has_identity,
    _existing_or_missing,
    _fsync_directory,
    _open_nfo_backup_target,
    _open_publish_directory,
    _path_is_link_or_reparse,
    _publish_no_replace,
    _regular_root,
    _RegularFileIdentity,
    _rename_exchange,
    _secure_directory_operations_supported,
    _verify_open_directory,
)
from .errors import (
    MediaMetadataReviewConflict,
    MediaMetadataReviewError,
    MediaMetadataReviewValidationError,
)
from .fields import (
    bounded_int,
    enum,
    hex_id,
    json_load,
    json_text,
    reject_sensitive_text,
    validated_sha256,
)
from .models import (
    MAX_PUBLISH_JOURNAL_ARTIFACTS,
    MAX_PUBLISH_JOURNAL_BYTES,
    MAX_REVIEW_IMAGE_BYTES,
    PUBLICATION_LOCK_BODY,
    PUBLICATION_LOCK_NAME,
    PUBLISH_JOURNAL_DIRECTORY,
    PUBLISH_JOURNAL_FILE_RE,
    PUBLISH_JOURNAL_TEMP_RE,
    PUBLISH_JOURNAL_VERSION,
    ArtifactState,
    JournalArtifact,
    LoadedJournal,
    PreviewArtifact,
    PublishJournal,
    RecoveryArtifact,
)

class JournalDiscardablePublishConflict(MetadataPublishConflict):
    """The current artifact was not changed, or its change was rolled back."""


def _publish_journal_body(journal: PublishJournal) -> bytes:
    payload = {
        "version": PUBLISH_JOURNAL_VERSION,
        "publication_id": journal.publication_id,
        "review_id": journal.review_id,
        "review_revision": journal.review_revision,
        "artifacts": [
            {
                "relative_path": artifact.relative_path,
                "action": artifact.action,
                "original_sha256": artifact.original_sha256,
                "proposed_sha256": artifact.proposed_sha256,
            }
            for artifact in journal.artifacts
        ],
    }
    body = json_text(payload).encode("ascii")
    if not 0 < len(body) <= MAX_PUBLISH_JOURNAL_BYTES:
        raise MediaMetadataReviewValidationError(
            "metadata review publish journal is too large"
        )
    return body


def _decode_publish_journal(body: bytes) -> PublishJournal:
    if not 0 < len(body) <= MAX_PUBLISH_JOURNAL_BYTES or not body.isascii():
        raise MediaMetadataReviewConflict("metadata review publish journal is invalid")
    try:
        payload = json_load(body.decode("ascii"))
    except MediaMetadataReviewError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal is invalid"
        ) from exc
    expected_keys = {
        "version",
        "publication_id",
        "review_id",
        "review_revision",
        "artifacts",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal schema is invalid"
        )
    if payload["version"] != PUBLISH_JOURNAL_VERSION:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal version is unsupported"
        )
    try:
        publication_id = hex_id(payload["publication_id"], "publication id")
        review_id = hex_id(payload["review_id"], "review id")
        review_revision = bounded_int(
            payload["review_revision"],
            "publish journal revision",
            1,
            2_147_483_647,
        )
    except MediaMetadataReviewValidationError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal identity is invalid"
        ) from exc
    values = payload["artifacts"]
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_PUBLISH_JOURNAL_ARTIFACTS
    ):
        raise MediaMetadataReviewConflict(
            "metadata review publish journal artifacts are invalid"
        )
    artifacts: list[JournalArtifact] = []
    seen: set[str] = set()
    for value in values:
        artifact = _decode_journal_artifact(value)
        if artifact.relative_path in seen:
            raise MediaMetadataReviewConflict(
                "metadata review publish journal has duplicate targets"
            )
        seen.add(artifact.relative_path)
        artifacts.append(artifact)
    journal = PublishJournal(
        publication_id=publication_id,
        review_id=review_id,
        review_revision=review_revision,
        artifacts=tuple(artifacts),
    )
    if _publish_journal_body(journal) != body:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal is not canonical"
        )
    return journal


def _decode_journal_artifact(value: object) -> JournalArtifact:
    expected_keys = {
        "relative_path",
        "action",
        "original_sha256",
        "proposed_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal artifact is invalid"
        )
    relative_path = _journal_relative_path(value["relative_path"])
    try:
        action = enum(
            value["action"], {"create", "replace", "unchanged"}, "journal action"
        )
        original_sha256 = (
            None
            if value["original_sha256"] is None
            else validated_sha256(value["original_sha256"], "journal original digest")
        )
        proposed_sha256 = validated_sha256(value["proposed_sha256"], "journal proposed digest")
    except MediaMetadataReviewValidationError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal artifact is invalid"
        ) from exc
    if (
        (action == "create" and original_sha256 is not None)
        or (action == "replace" and original_sha256 is None)
        or (action == "replace" and original_sha256 == proposed_sha256)
        or (action == "unchanged" and original_sha256 != proposed_sha256)
    ):
        raise MediaMetadataReviewConflict(
            "metadata review publish journal transition is invalid"
        )
    return JournalArtifact(
        relative_path=relative_path,
        action=action,
        original_sha256=original_sha256,
        proposed_sha256=proposed_sha256,
    )


def _journal_relative_path(value: object) -> str:
    raw = str(value or "").strip()
    relative = PurePosixPath(raw)
    if (
        not raw
        or len(raw.encode("utf-8")) > 1024
        or relative.is_absolute()
        or "\\" in raw
        or "\x00" in raw
        or any(ord(character) < 32 for character in raw)
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw
        or relative.suffix.lower() not in {".nfo", ".jpg"}
    ):
        raise MediaMetadataReviewConflict(
            "metadata review publish journal path is invalid"
        )
    try:
        reject_sensitive_text(raw, "publish journal path")
    except MediaMetadataReviewValidationError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal path is invalid"
        ) from exc
    return raw


def require_secure_review_publication() -> None:
    if os.name == "nt":
        raise MediaMetadataReviewError(
            "secure metadata review publication is unavailable on Windows"
        )
    if not _secure_directory_operations_supported():
        raise MediaMetadataReviewError(
            "secure metadata review publication operations are unavailable"
        )


@contextmanager
def cross_process_publication_lock(backup_root: Path) -> Iterator[None]:
    require_secure_review_publication()
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - guarded by the platform check
        raise MediaMetadataReviewError(
            "secure metadata review publication locking is unavailable"
        ) from exc

    descriptor: int | None = None
    with _open_nfo_backup_target(
        backup_root=backup_root,
        backup_run_id=PUBLISH_JOURNAL_DIRECTORY,
        relative_nfo=PUBLICATION_LOCK_NAME,
    ) as (target, directory_fd):
        assert directory_fd is not None
        status_value = _publish_no_replace(
            target,
            PUBLICATION_LOCK_BODY,
            target.parent,
            directory_fd=directory_fd,
        )
        if status_value not in {"generated", "existing"}:
            raise MediaMetadataReviewError(
                "metadata review publication lock is unavailable"
            )
        try:
            descriptor = os.open(
                target.name,
                os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            opened = os.fstat(descriptor)
            current = os.stat(
                target.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != len(PUBLICATION_LOCK_BODY)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise MediaMetadataReviewConflict(
                    "metadata review publication lock is unsafe"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.lseek(descriptor, 0, os.SEEK_SET)
            body = os.read(descriptor, len(PUBLICATION_LOCK_BODY) + 1)
            if body != PUBLICATION_LOCK_BODY:
                raise MediaMetadataReviewConflict(
                    "metadata review publication lock changed"
                )
            _verify_open_directory(target.parent, directory_fd)
        except MediaMetadataReviewError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise MediaMetadataReviewError(
                "metadata review publication lock could not be acquired"
            ) from exc
        try:
            yield
        finally:
            os.close(descriptor)


def persist_publish_journal(backup_root: Path, journal: PublishJournal) -> bytes:
    body = _publish_journal_body(journal)
    with _open_nfo_backup_target(
        backup_root=backup_root,
        backup_run_id=PUBLISH_JOURNAL_DIRECTORY,
        relative_nfo=f"{journal.publication_id}.json",
    ) as (target, directory_fd):
        status_value = _publish_no_replace(
            target,
            body,
            target.parent,
            directory_fd=directory_fd,
        )
        if status_value != "generated":
            raise MediaMetadataReviewConflict(
                "metadata review publish journal identifier collided"
            )
        persisted, _ = read_regular_artifact(
            target,
            directory_fd=directory_fd,
            max_bytes=MAX_PUBLISH_JOURNAL_BYTES,
        )
        if persisted != body:
            raise MediaMetadataReviewError(
                "metadata review publish journal could not be verified"
            )
    return body


@contextmanager
def _open_existing_publish_journal_root(
    backup_root: Path,
) -> Iterator[tuple[Path, int | None] | None]:
    root = Path(backup_root)
    if not root.is_absolute():
        raise MediaMetadataReviewValidationError(
            "metadata review backup path must be absolute"
        )
    journal_root = root / PUBLISH_JOURNAL_DIRECTORY
    if os.name == "nt":
        current = Path(journal_root.anchor)
        try:
            for part in journal_root.parts[1:]:
                current /= part
                if not os.path.lexists(current):
                    yield None
                    return
                if _path_is_link_or_reparse(current):
                    raise MediaMetadataReviewConflict(
                        "metadata review publish journal path is unsafe"
                    )
        except MediaMetadataReviewError:
            raise
        except OSError as exc:
            raise MediaMetadataReviewError(
                "metadata review publish journal path is unavailable"
            ) from exc
        yield journal_root, None
        return

    if not _secure_directory_operations_supported():
        if not os.path.lexists(journal_root):
            yield None
            return
        raise MediaMetadataReviewError(
            "secure metadata review journal operations are unavailable"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd: int | None = None
    try:
        current_fd = os.open(journal_root.anchor, flags)
        for part in journal_root.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                yield None
                return
            os.close(current_fd)
            current_fd = next_fd
        _verify_open_directory(journal_root, current_fd)
        yield journal_root, current_fd
    except MediaMetadataReviewError:
        raise
    except OSError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review publish journal path is unsafe"
        ) from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)


def reject_unrecoverable_windows_journals(backup_root: Path) -> None:
    with _open_existing_publish_journal_root(backup_root) as opened:
        if opened is None:
            return
        directory, directory_fd = opened
        names = _directory_names(directory, directory_fd)
        if any(
            PUBLISH_JOURNAL_FILE_RE.fullmatch(name)
            or PUBLISH_JOURNAL_TEMP_RE.fullmatch(name)
            for name in names
        ):
            raise MediaMetadataReviewError(
                "metadata review publish recovery requires secure Linux operations"
            )


def _directory_names(directory: Path, directory_fd: int | None) -> list[str]:
    try:
        names = os.listdir(directory if directory_fd is None else directory_fd)
    except OSError as exc:
        raise MediaMetadataReviewError(
            "metadata review internal directory could not be read"
        ) from exc
    return sorted(str(name) for name in names)


def load_publish_journals(backup_root: Path) -> list[LoadedJournal]:
    loaded: list[LoadedJournal] = []
    with _open_existing_publish_journal_root(backup_root) as opened:
        if opened is None:
            return loaded
        directory, directory_fd = opened
        for name in _directory_names(directory, directory_fd):
            if PUBLISH_JOURNAL_TEMP_RE.fullmatch(name):
                _unlink_internal_regular_file(
                    directory / name,
                    directory_fd=directory_fd,
                )
                continue
            match = PUBLISH_JOURNAL_FILE_RE.fullmatch(name)
            if match is None:
                continue
            body, _ = read_regular_artifact(
                directory / name,
                directory_fd=directory_fd,
                max_bytes=MAX_PUBLISH_JOURNAL_BYTES,
            )
            journal = _decode_publish_journal(body)
            if journal.publication_id != match.group(1):
                raise MediaMetadataReviewConflict(
                    "metadata review publish journal filename is invalid"
                )
            loaded.append(LoadedJournal(journal=journal, body=body))
    return loaded


def remove_publish_journal(
    backup_root: Path,
    journal: PublishJournal,
    *,
    expected_body: bytes,
) -> None:
    with _open_existing_publish_journal_root(backup_root) as opened:
        if opened is None:
            raise MediaMetadataReviewConflict(
                "metadata review publish journal disappeared"
            )
        directory, directory_fd = opened
        target = directory / f"{journal.publication_id}.json"
        current, _ = read_regular_artifact(
            target,
            directory_fd=directory_fd,
            max_bytes=MAX_PUBLISH_JOURNAL_BYTES,
        )
        if current != expected_body:
            raise MediaMetadataReviewConflict("metadata review publish journal changed")
        _unlink_internal_regular_file(target, directory_fd=directory_fd)


def _unlink_internal_regular_file(
    target: Path,
    *,
    directory_fd: int | None,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    try:
        current = (
            os.stat(target, follow_symlinks=False)
            if directory_fd is None
            else os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        )
        if not stat.S_ISREG(current.st_mode):
            raise MediaMetadataReviewConflict("metadata review internal file is unsafe")
        identity = (current.st_dev, current.st_ino)
        if expected_identity is not None and identity != expected_identity:
            raise MediaMetadataReviewConflict("metadata review internal file changed")
        if directory_fd is None:
            target.unlink()
        else:
            os.unlink(target.name, dir_fd=directory_fd)
        _fsync_directory(target.parent, descriptor=directory_fd)
    except MediaMetadataReviewError:
        raise
    except OSError as exc:
        raise MediaMetadataReviewError(
            "metadata review internal file could not be removed"
        ) from exc


def require_publication_matches_journal(
    publication: Mapping[str, object], journal: PublishJournal
) -> None:
    if (
        publication.get("publication_id") != journal.publication_id
        or publication.get("review_id") != journal.review_id
        or publication.get("review_revision") != journal.review_revision
    ):
        raise MediaMetadataReviewConflict(
            "metadata review committed publication does not match its journal"
        )
    values = publication.get("artifacts")
    if not isinstance(values, list) or len(values) != len(journal.artifacts):
        raise MediaMetadataReviewConflict(
            "metadata review committed publication artifacts do not match"
        )
    by_path: dict[str, Mapping[str, object]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise MediaMetadataReviewConflict(
                "metadata review committed publication artifacts are invalid"
            )
        relative_path = str(value.get("relative_path") or "")
        if relative_path in by_path:
            raise MediaMetadataReviewConflict(
                "metadata review committed publication has duplicate targets"
            )
        by_path[relative_path] = value
    for artifact in journal.artifacts:
        value = by_path.get(artifact.relative_path)
        if (
            value is None
            or value.get("action") != artifact.action
            or value.get("sha256") != artifact.proposed_sha256
        ):
            raise MediaMetadataReviewConflict(
                "metadata review committed publication artifacts do not match"
            )


def preflight_journal_artifacts(
    *,
    journal: PublishJournal,
    library_root: Path,
    backup_root: Path,
    committed: bool,
) -> tuple[Path, tuple[RecoveryArtifact, ...]]:
    try:
        root = _regular_root(library_root)
    except MetadataPublishError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review library is unavailable for recovery"
        ) from exc
    recoveries: list[RecoveryArtifact] = []
    for artifact in journal.artifacts:
        target = _journal_target(root, artifact.relative_path)
        with _open_publish_directory(target.parent, root) as directory_fd:
            state = artifact_state(target, directory_fd=directory_fd)
        backup_body: bytes | None = None
        if artifact.action == "replace" and not committed:
            backup_body = _read_journal_backup(
                backup_root=backup_root,
                publication_id=journal.publication_id,
                artifact=artifact,
            )
        current_digest = state.sha256
        if committed:
            valid = current_digest == artifact.proposed_sha256
        elif artifact.action == "create":
            valid = current_digest in {None, artifact.proposed_sha256}
        elif artifact.action == "replace":
            valid = current_digest in {
                artifact.original_sha256,
                artifact.proposed_sha256,
            }
        else:
            valid = current_digest == artifact.original_sha256
        if not valid:
            raise MediaMetadataReviewConflict(
                "metadata review recovery found an external artifact change"
            )
        recoveries.append(
            RecoveryArtifact(
                journal=artifact,
                target=target,
                state=state,
                backup_body=backup_body,
            )
        )
    return root, tuple(recoveries)


def _journal_target(root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    parent = root
    try:
        for part in relative.parts[:-1]:
            parent /= part
            if _path_is_link_or_reparse(parent):
                raise MediaMetadataReviewConflict(
                    "metadata review recovery target path is unsafe"
                )
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise MediaMetadataReviewConflict(
            "metadata review recovery target directory is unavailable"
        ) from exc
    if not resolved_parent.is_relative_to(root):
        raise MediaMetadataReviewConflict(
            "metadata review recovery target escaped the library"
        )
    return resolved_parent / relative.name


def _read_journal_backup(
    *,
    backup_root: Path,
    publication_id: str,
    artifact: JournalArtifact,
) -> bytes:
    assert artifact.original_sha256 is not None
    with _open_nfo_backup_target(
        backup_root=backup_root,
        backup_run_id=publication_id,
        relative_nfo=artifact.relative_path,
    ) as (target, directory_fd):
        body, _ = read_regular_artifact(
            target,
            directory_fd=directory_fd,
            max_bytes=_artifact_maximum(target),
        )
    if hashlib.sha256(body).hexdigest() != artifact.original_sha256:
        raise MediaMetadataReviewConflict(
            "metadata review recovery backup is not byte-identical"
        )
    return body


def rollback_journal_artifacts(
    recoveries: Sequence[RecoveryArtifact], root: Path
) -> None:
    for recovery in reversed(recoveries):
        artifact = recovery.journal
        if recovery.state.sha256 == artifact.original_sha256:
            continue
        with _open_publish_directory(recovery.target.parent, root) as directory_fd:
            current = artifact_state(recovery.target, directory_fd=directory_fd)
            if not same_artifact_state(current, recovery.state):
                raise MediaMetadataReviewConflict(
                    "metadata review recovery target changed during rollback"
                )
            if artifact.action == "create":
                assert current.identity is not None
                _unlink_internal_regular_file(
                    recovery.target,
                    directory_fd=directory_fd,
                    expected_identity=(
                        current.identity.device,
                        current.identity.inode,
                    ),
                )
                continue
            if artifact.action == "replace":
                if current.body is None or current.identity is None:
                    raise MediaMetadataReviewConflict(
                        "metadata review recovery target disappeared"
                    )
                if recovery.backup_body is None:
                    raise MediaMetadataReviewError(
                        "metadata review recovery backup is unavailable"
                    )
                replace_regular_artifact(
                    recovery.target,
                    recovery.backup_body,
                    current.body,
                    current.identity,
                    recovery.target.parent,
                    directory_fd=directory_fd,
                )


def verify_journal_original_state(
    recoveries: Sequence[RecoveryArtifact], root: Path
) -> None:
    for recovery in recoveries:
        with _open_publish_directory(recovery.target.parent, root) as directory_fd:
            restored = artifact_state(recovery.target, directory_fd=directory_fd)
        if restored.sha256 != recovery.journal.original_sha256:
            raise MediaMetadataReviewError(
                "metadata review recovery rollback verification failed"
            )


def sync_artifact_directories(
    recoveries: Sequence[RecoveryArtifact], root: Path
) -> None:
    for directory in sorted({item.target.parent for item in recoveries}):
        with _open_publish_directory(directory, root) as directory_fd:
            _fsync_directory(directory, descriptor=directory_fd)


def cleanup_artifact_temporaries(
    recoveries: Sequence[RecoveryArtifact], root: Path
) -> None:
    entries: list[tuple[Path, tuple[int, int]]] = []
    seen: set[Path] = set()
    for recovery in recoveries:
        directory = recovery.target.parent
        if directory in seen:
            continue
        seen.add(directory)
        patterns = [
            re.compile(rf"^\.{re.escape(item.target.name)}\.[a-f0-9]{{32}}\.tmp$")
            for item in recoveries
            if item.target.parent == directory
        ]
        with _open_publish_directory(directory, root) as directory_fd:
            for name in _directory_names(directory, directory_fd):
                if not any(pattern.fullmatch(name) for pattern in patterns):
                    continue
                try:
                    entry = (
                        os.stat(directory / name, follow_symlinks=False)
                        if directory_fd is None
                        else os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    )
                except OSError as exc:
                    raise MediaMetadataReviewConflict(
                        "metadata review temporary artifact is unavailable"
                    ) from exc
                if not stat.S_ISREG(entry.st_mode):
                    raise MediaMetadataReviewConflict(
                        "metadata review temporary artifact is unsafe"
                    )
                entries.append((directory / name, (entry.st_dev, entry.st_ino)))
    for target, identity in entries:
        with _open_publish_directory(target.parent, root) as directory_fd:
            _unlink_internal_regular_file(
                target,
                directory_fd=directory_fd,
                expected_identity=identity,
            )


def _artifact_maximum(target: Path) -> int:
    return MAX_NFO_BYTES if target.suffix.lower() == ".nfo" else MAX_REVIEW_IMAGE_BYTES


def artifact_state(target: Path, *, directory_fd: int | None) -> ArtifactState:
    status_value = _existing_or_missing(target, directory_fd=directory_fd)["status"]
    if status_value == "missing":
        return ArtifactState(body=None, identity=None)
    if status_value != "existing":
        raise MediaMetadataReviewConflict(
            "metadata review target is not a regular file"
        )
    maximum = _artifact_maximum(target)
    body, identity = read_regular_artifact(
        target,
        directory_fd=directory_fd,
        max_bytes=maximum,
    )
    return ArtifactState(body=body, identity=identity)


def read_regular_artifact(
    target: Path,
    *,
    directory_fd: int | None,
    max_bytes: int,
) -> tuple[bytes, _RegularFileIdentity]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target if directory_fd is None else target.name,
            flags,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= max_bytes:
            raise MetadataPublishConflict(
                "metadata review artifact is not a bounded regular file"
            )
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise MetadataPublishError(
                    "metadata review artifact changed while being read"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MetadataPublishError(
                "metadata review artifact changed while being read"
            )
        current = (
            os.stat(target, follow_symlinks=False)
            if directory_fd is None
            else os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        )
        identity = _RegularFileIdentity(
            device=opened.st_dev,
            inode=opened.st_ino,
            size=opened.st_size,
            modified_ns=opened.st_mtime_ns,
            mode=stat.S_IMODE(opened.st_mode),
            uid=opened.st_uid,
            gid=opened.st_gid,
            xattrs=_descriptor_xattrs(descriptor),
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != identity.device
            or current.st_ino != identity.inode
            or current.st_size != identity.size
            or current.st_mtime_ns != identity.modified_ns
            or stat.S_IMODE(current.st_mode) != identity.mode
            or current.st_uid != identity.uid
            or current.st_gid != identity.gid
        ):
            raise MetadataPublishError("metadata review artifact identity changed")
        return b"".join(chunks), identity
    except (MetadataPublishError, MetadataPublishConflict):
        raise
    except OSError as exc:
        raise MetadataPublishError(
            "metadata review artifact could not be read safely"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def same_artifact_state(left: ArtifactState, right: ArtifactState) -> bool:
    return left.body == right.body and left.identity == right.identity


def backup_artifact(
    *,
    backup_root: Path,
    backup_run_id: str,
    relative_path: str,
    original: ArtifactState,
) -> str:
    if original.body is None:
        raise MediaMetadataReviewError("metadata review backup source is missing")
    with _open_nfo_backup_target(
        backup_root=backup_root,
        backup_run_id=backup_run_id,
        relative_nfo=relative_path,
    ) as (backup_target, backup_directory_fd):
        status_value = _publish_no_replace(
            backup_target,
            original.body,
            backup_target.parent,
            directory_fd=backup_directory_fd,
        )
        if status_value not in {"generated", "existing"}:
            raise MediaMetadataReviewError(
                "metadata review backup could not be created"
            )
        backup_body, _ = read_regular_artifact(
            backup_target,
            directory_fd=backup_directory_fd,
            max_bytes=max(MAX_NFO_BYTES, MAX_REVIEW_IMAGE_BYTES),
        )
        if backup_body != original.body:
            raise MediaMetadataReviewConflict(
                "metadata review backup does not match the source"
            )
    return backup_target.as_posix()


def replace_regular_artifact(
    target: Path,
    body: bytes,
    original: bytes,
    identity: _RegularFileIdentity,
    directory: Path,
    *,
    directory_fd: int | None,
) -> None:
    if not body:
        raise MetadataPublishError("metadata review replacement is empty")
    _verify_open_directory(directory, directory_fd)
    temporary_name = f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary_path = directory / temporary_name
    published = False
    preserve_temporary = False
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_path if directory_fd is None else temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("metadata review replacement write stopped")
            view = view[written:]
        _apply_file_metadata(descriptor, identity)
        os.fsync(descriptor)
        replacement_stat = os.fstat(descriptor)
        replacement_identity = (replacement_stat.st_dev, replacement_stat.st_ino)
        # Keep the POSIX inode pinned through exchange and rollback validation.
        if directory_fd is None:
            os.close(descriptor)
            descriptor = None
        _verify_open_directory(directory, directory_fd)
        if directory_fd is None:
            current_body, current_identity = read_regular_artifact(
                target,
                directory_fd=None,
                max_bytes=max(MAX_NFO_BYTES, MAX_REVIEW_IMAGE_BYTES),
            )
            if current_identity != identity or current_body != original:
                raise JournalDiscardablePublishConflict(
                    "metadata review artifact changed before replacement"
                )
            os.replace(temporary_path, target)
            published = True
        else:
            _rename_exchange(temporary_name, target.name, directory_fd=directory_fd)
            exchanged = True
            try:
                exchanged_body, exchanged_identity = read_regular_artifact(
                    directory / temporary_name,
                    directory_fd=directory_fd,
                    max_bytes=max(MAX_NFO_BYTES, MAX_REVIEW_IMAGE_BYTES),
                )
                if exchanged_identity != identity or exchanged_body != original:
                    raise MetadataPublishConflict(
                        "metadata review artifact changed before atomic exchange"
                    )
                os.unlink(temporary_name, dir_fd=directory_fd)
                exchanged = False
                published = True
            except BaseException as exc:
                if exchanged:
                    preserve_temporary = True
                    if _directory_entry_has_identity(
                        target.name,
                        replacement_identity,
                        directory_fd=directory_fd,
                    ):
                        _rename_exchange(
                            temporary_name,
                            target.name,
                            directory_fd=directory_fd,
                        )
                        rollback_confirmed = _directory_entry_has_identity(
                            temporary_name,
                            replacement_identity,
                            directory_fd=directory_fd,
                        ) and not _directory_entry_has_identity(
                            target.name,
                            replacement_identity,
                            directory_fd=directory_fd,
                        )
                        preserve_temporary = not rollback_confirmed
                        if rollback_confirmed and isinstance(
                            exc, MetadataPublishConflict
                        ):
                            raise JournalDiscardablePublishConflict(str(exc)) from exc
                raise
        published_body, published_identity = read_regular_artifact(
            target,
            directory_fd=directory_fd,
            max_bytes=max(MAX_NFO_BYTES, MAX_REVIEW_IMAGE_BYTES),
        )
        if published_body != body or (
            published_identity.mode,
            published_identity.uid,
            published_identity.gid,
            published_identity.xattrs,
        ) != (identity.mode, identity.uid, identity.gid, identity.xattrs):
            raise MetadataPublishError(
                "metadata review replacement could not be verified"
            )
        _fsync_directory(directory, descriptor=directory_fd)
    except (MetadataPublishError, MetadataPublishConflict):
        raise
    except OSError as exc:
        raise MetadataPublishError(
            "metadata review artifact could not be replaced"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not published and not preserve_temporary:
            try:
                if directory_fd is None:
                    if temporary_path.is_file() and not temporary_path.is_symlink():
                        temporary_path.unlink()
                else:
                    entry = os.stat(
                        temporary_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISREG(entry.st_mode):
                        os.unlink(temporary_name, dir_fd=directory_fd)
            except (FileNotFoundError, OSError):
                pass


def artifact_preview(artifact: PreviewArtifact) -> dict[str, object]:
    return {
        "relative_path": artifact.relative_path,
        "kind": artifact.kind,
        "source_id": artifact.source_id,
        "action": artifact.action,
        "current_sha256": artifact.original.sha256,
        "proposed_sha256": hashlib.sha256(artifact.body).hexdigest(),
        "proposed_bytes": len(artifact.body),
    }


def publication_artifact(value: Mapping[str, object]) -> dict[str, object]:
    allowed = {"relative_path", "kind", "source_id", "action", "sha256", "backup_path"}
    if not isinstance(value, Mapping) or set(value) != allowed:
        raise MediaMetadataReviewValidationError(
            "metadata review publication artifact is invalid"
        )
    relative = str(value["relative_path"] or "").strip()
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or ".." in PurePosixPath(relative).parts
    ):
        raise MediaMetadataReviewValidationError(
            "metadata review publication path is invalid"
        )
    reject_sensitive_text(relative, "publication path")
    kind = enum(value["kind"], {"nfo", "portrait", "landscape"}, "artifact kind")
    source_id = str(value["source_id"] or "").strip()
    if source_id not in METADATA_PROFILES | {"draft", "manual"}:
        raise MediaMetadataReviewValidationError(
            "metadata review publication source is invalid"
        )
    action = enum(
        value["action"], {"create", "replace", "unchanged"}, "artifact action"
    )
    digest = validated_sha256(value["sha256"], "artifact digest")
    backup = str(value["backup_path"] or "").strip() or None
    if backup is not None:
        reject_sensitive_text(backup, "backup path")
    return {
        "relative_path": relative,
        "kind": kind,
        "source_id": source_id,
        "action": action,
        "sha256": digest,
        "backup_path": backup,
    }
