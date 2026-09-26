from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Sequence


BACKUP_FORMAT_VERSION = 4
SUPPORTED_BACKUP_FORMAT_VERSIONS = frozenset({1, 2, 3, BACKUP_FORMAT_VERSION})
RESTORE_JOURNAL_VERSION = 4
SUPPORTED_RESTORE_JOURNAL_VERSIONS = frozenset({1, 2, 3, RESTORE_JOURNAL_VERSION})
SNAPSHOT_KINDS = frozenset({"deployment", "drill", "manual"})
MANIFEST_NAME = "manifest.json"
MAINTENANCE_LOCK_NAME = ".jav-pilot-maintenance.lock"
UTC = timezone.utc
REQUIRED_FILES = (
    ".env",
    "data/app_config.json",
    "data/settings.json",
    "data/web_downloads.sqlite3",
    "data/media_metadata.sqlite3",
)
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_SNAPSHOT_NAME_RE = re.compile(r"^jav-pilot-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
_RESTORE_STAGING_NAME_RE = re.compile(r"^\.jav-pilot-restore-staging-[0-9a-f]{32}$")
_RESTORE_ROLLBACK_NAME_RE = re.compile(r"^\.jav-pilot-restore-rollback-[0-9a-f]{32}$")
_MISSAV_PROFILE_DIR_RE = re.compile(
    r"^missav-browser-profile(?:-quarantine(?:-[A-Za-z0-9_.-]+)?)?$"
)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_BACKUP_FILES = 4096
_MAX_RESTORE_OPERATIONS = _MAX_BACKUP_FILES * 3
_MAX_BACKUP_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_BACKUP_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
_MAX_RETENTION_SNAPSHOTS = 10_000
_SQLITE_SNAPSHOT_ATTEMPTS = 4
_SQLITE_SNAPSHOT_RETRY_SECONDS = 0.05
_SCHEMA_CONTRACT_VERSION = 1
_JSON_SCHEMA_COMPONENTS = {
    "data/app_config.json": "app_config",
    "data/settings.json": "settings",
}

FaultInjector = Callable[[str], None]


class BackupError(RuntimeError):
    pass


@contextmanager
def _maintenance_locks(
    roots: tuple[Path, ...],
    operation: str,
) -> Iterator[None]:
    try:
        from .lock import MaintenanceLockError, MaintenanceLocks
    except (ImportError, SyntaxError) as exc:
        raise BackupError(f"{operation} maintenance lock is unavailable") from exc
    try:
        with MaintenanceLocks(roots):
            yield
    except MaintenanceLockError as exc:
        raise BackupError(f"{operation} maintenance lock is unavailable") from exc


@dataclass(frozen=True)
class BackupEntry:
    path: str
    size: int
    sha256: str
    source_mode: int
    backup_mode: int
    source_uid: int | None = None
    source_gid: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "source_mode": self.source_mode,
            "backup_mode": self.backup_mode,
            "source_uid": self.source_uid,
            "source_gid": self.source_gid,
        }


@dataclass(frozen=True)
class BackupRetentionPolicy:
    max_unprotected: int = 10
    max_age_days: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_unprotected, bool)
            or not isinstance(self.max_unprotected, int)
            or not 0 <= self.max_unprotected <= _MAX_RETENTION_SNAPSHOTS
        ):
            raise BackupError("backup retention count is invalid")
        if self.max_age_days is not None and (
            isinstance(self.max_age_days, bool)
            or not isinstance(self.max_age_days, int)
            or not 1 <= self.max_age_days <= 36_500
        ):
            raise BackupError("backup retention age is invalid")


@dataclass(frozen=True)
class _VerifiedSnapshot:
    path: Path
    manifest: dict[str, Any]
    created_at: datetime
    identity: tuple[int, int]


@dataclass(frozen=True)
class _FinishedRestore:
    path: Path
    identity: tuple[int, int]
    journal_sha256: str


def create_backup(
    app_root: Path,
    backup_parent: Path,
    *,
    revision: str,
    now: datetime | None = None,
    snapshot_kind: str = "manual",
    user_marked: bool = False,
    label: str | None = None,
    fault_injector: FaultInjector | None = None,
    maintenance_lock_held: bool = False,
    include_paths: Sequence[str] | None = None,
) -> Path:
    if not isinstance(maintenance_lock_held, bool):
        raise BackupError("maintenance lock state is invalid")
    root = _existing_directory(app_root, "application root")
    parent = _directory(backup_parent, "backup parent", create=True)
    if maintenance_lock_held:
        return _create_backup_locked(
            root,
            parent,
            revision=revision,
            now=now,
            snapshot_kind=snapshot_kind,
            user_marked=user_marked,
            label=label,
            fault_injector=fault_injector,
            include_paths=include_paths,
        )
    with _maintenance_locks((root, parent), "backup"):
        return _create_backup_locked(
            root,
            parent,
            revision=revision,
            now=now,
            snapshot_kind=snapshot_kind,
            user_marked=user_marked,
            label=label,
            fault_injector=fault_injector,
            include_paths=include_paths,
        )


def _create_backup_locked(
    app_root: Path,
    backup_parent: Path,
    *,
    revision: str,
    now: datetime | None,
    snapshot_kind: str,
    user_marked: bool,
    label: str | None,
    fault_injector: FaultInjector | None,
    include_paths: Sequence[str] | None,
) -> Path:
    root = _existing_directory(app_root, "application root")
    parent = _directory(backup_parent, "backup parent", create=True)
    clean_revision = str(revision or "").strip().lower()
    if not _REVISION_RE.fullmatch(clean_revision):
        raise BackupError("backup revision must be a full lowercase Git SHA")
    clean_kind = _snapshot_kind(snapshot_kind)
    if not isinstance(user_marked, bool):
        raise BackupError("backup mark must be boolean")
    clean_label = _backup_label(label)
    created = (now or datetime.now(UTC)).astimezone(UTC)
    timestamp = created.strftime("%Y%m%dT%H%M%SZ")
    target = parent / f"jav-pilot-{timestamp}-{clean_revision[:12]}"
    if target.exists() or target.is_symlink():
        raise BackupError("backup target already exists")
    selected_paths = None if include_paths is None else _selected_backup_paths(include_paths)
    sources = (
        _backup_sources(root)
        if selected_paths is None
        else _selected_backup_sources(root, selected_paths)
    )
    _ensure_capacity(parent, sources)
    _inject_fault(fault_injector, "after_capacity_check")
    staging = parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        entries: list[BackupEntry] = []
        databases = [
            (source, relative)
            for source, relative in sources
            if relative.endswith(".sqlite3")
        ]
        database_paths = {
            relative
            for _source, database_relative in databases
            for relative in (
                database_relative,
                f"{database_relative}-wal",
                f"{database_relative}-shm",
            )
        }
        for source, relative in sources:
            if relative in database_paths:
                continue
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            entries.append(_copy_exact(source, destination, relative))
            _inject_fault(fault_injector, "after_copy")
        for database, relative in databases:
            entries.extend(
                _copy_stable_sqlite_set(
                    database,
                    staging,
                    relative,
                    fault_injector=fault_injector,
                )
            )
        entries.sort(key=lambda entry: entry.path)
        database_checks = _verify_copied_databases(staging, entries)
        _inject_fault(fault_injector, "after_database_check")
        json_schemas = _json_schema_versions(staging)
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": timestamp,
            "revision": clean_revision,
            "snapshot_kind": clean_kind,
            "user_marked": user_marked,
            "schema_contract": _schema_contract(json_schemas, database_checks),
            "json_schemas": json_schemas,
            "sqlite": database_checks,
            "entries": [entry.to_dict() for entry in entries],
        }
        if selected_paths is not None:
            manifest["selected_paths"] = list(selected_paths)
        if clean_label is not None:
            manifest["label"] = clean_label
        _atomic_write_text(
            staging / MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        _inject_fault(fault_injector, "after_manifest_write")
        _set_snapshot_reader(staging, root / "data")
        verify_backup(staging)
        _inject_fault(fault_injector, "after_verify")
        os.replace(staging, target)
        _fsync_directory(parent)
        return target
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _set_snapshot_reader(snapshot: Path, data_root: Path) -> None:
    """Give the service data owner access without exposing backup credentials."""
    if os.name == "nt":
        return
    owner = data_root.stat(follow_symlinks=False)
    if data_root.is_symlink() or not stat.S_ISDIR(owner.st_mode):
        raise BackupError("backup service identity is unsafe")
    # The tree was just created under our private staging directory. Do not
    # follow links, and finish its permission/ownership contract before publish.
    for directory, subdirs, files in os.walk(snapshot, followlinks=False):
        for name in files:
            path = Path(directory) / name
            details = path.lstat()
            if _is_linklike(path, details) or not stat.S_ISREG(details.st_mode):
                raise BackupError("backup reader path is unsafe")
            if (details.st_uid, details.st_gid) != (owner.st_uid, owner.st_gid):
                _chown_durable(path, int(owner.st_uid), int(owner.st_gid))
            _chmod_durable(path, 0o600)
        for name in ["", *subdirs]:
            path = Path(directory) / name
            details = path.lstat()
            if _is_linklike(path, details) or not stat.S_ISDIR(details.st_mode):
                raise BackupError("backup reader directory is unsafe")
            if (details.st_uid, details.st_gid) != (owner.st_uid, owner.st_gid):
                _chown_durable(path, int(owner.st_uid), int(owner.st_gid))
            _chmod_durable(path, 0o700)


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    root = _existing_directory(backup_dir, "backup")
    manifest = _read_manifest(root)
    entries = _manifest_entries(manifest)
    paths = {entry.path for entry in entries}
    if "selected_paths" in manifest:
        if not isinstance(manifest["selected_paths"], list):
            raise BackupError("selected backup paths are invalid")
        required = set(_selected_backup_paths(manifest["selected_paths"]))
        allowed = required | {
            f"{relative}{suffix}"
            for relative in required
            if relative.endswith(".sqlite3")
            for suffix in ("-wal", "-shm")
        }
        if not paths.issubset(allowed):
            raise BackupError("backup entries exceed the selected paths")
    else:
        required = set(REQUIRED_FILES)
    missing = sorted(required - paths)
    if missing:
        raise BackupError(f"backup is missing required files: {', '.join(missing)}")
    for entry in entries:
        path = _safe_child(root, entry.path)
        info = path.lstat()
        if _is_linklike(path, info) or not stat.S_ISREG(info.st_mode):
            raise BackupError(f"backup entry is not a regular file: {entry.path}")
        if info.st_size != entry.size or _sha256(path) != entry.sha256:
            raise BackupError(f"backup checksum mismatch: {entry.path}")
        if stat.S_IMODE(info.st_mode) != entry.backup_mode:
            raise BackupError(f"backup permission mismatch: {entry.path}")
    actual_sqlite = _verify_copied_databases(root, entries)
    _verify_manifest_metadata(root, manifest, actual_sqlite)
    return manifest


def restore_backup(
    backup_dir: Path,
    target_root: Path,
    *,
    dry_run: bool = True,
    fault_injector: FaultInjector | None = None,
    maintenance_lock_held: bool = False,
) -> dict[str, object]:
    if not isinstance(maintenance_lock_held, bool):
        raise BackupError("maintenance lock state is invalid")
    backup = _existing_directory(backup_dir, "backup")
    target = _existing_directory(target_root, "restore target")
    if maintenance_lock_held:
        return _restore_backup_locked(
            backup,
            target,
            dry_run=dry_run,
            fault_injector=fault_injector,
        )
    with _maintenance_locks((target, backup.parent), "restore"):
        return _restore_backup_locked(
            backup,
            target,
            dry_run=dry_run,
            fault_injector=fault_injector,
        )


def _restore_backup_locked(
    backup_dir: Path,
    target_root: Path,
    *,
    dry_run: bool,
    fault_injector: FaultInjector | None,
) -> dict[str, object]:
    backup = _existing_directory(backup_dir, "backup")
    target = _existing_directory(target_root, "restore target")
    manifest = verify_backup(backup)
    entries = _manifest_entries(manifest)
    finished_restores = _recover_interrupted_restores(target, dry_run=dry_run)
    _inject_fault(fault_injector, "after_recovery")
    removals = _sqlite_sidecar_removals(entries)
    operation_paths = _restore_operation_paths(entries, removals)
    plan = _restore_plan(entries, target, removals=removals)

    staging = target / f".jav-pilot-restore-staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        for entry in entries:
            source = _safe_child(backup, entry.path)
            destination = _safe_child(staging, entry.path)
            copied = _copy_exact(source, destination, entry.path)
            if copied.sha256 != entry.sha256:
                raise BackupError(f"restore staging checksum mismatch: {entry.path}")
            _inject_fault(fault_injector, "after_staging_copy")
        _verify_copied_databases(staging, entries)
        _inject_fault(fault_injector, "after_staging_verify")
        if dry_run:
            return {"dry_run": True, "changes": plan, "revision": manifest["revision"]}

        rollback = target / f".jav-pilot-restore-rollback-{uuid.uuid4().hex}"
        rollback.mkdir(mode=0o700)
        journal = {
            "journal_version": RESTORE_JOURNAL_VERSION,
            "status": "preparing",
            "backup": str(backup),
            "staging": staging.name,
            "entries": operation_paths,
            "remove": sorted(removals),
            "replaced": [],
            "originally_missing": [],
            "originals": {},
            "pending": None,
        }
        journal_path = rollback / "restore-journal.json"
        rollback_prepared = False
        try:
            _write_journal(journal_path, journal)
            for relative in operation_paths:
                current = _safe_child(target, relative)
                if current.exists():
                    copied = _copy_exact(
                        current,
                        _safe_child(rollback, relative),
                        relative,
                    )
                    journal["originals"][relative] = _entry_metadata(copied)
                else:
                    journal["originally_missing"].append(relative)
                _inject_fault(fault_injector, "after_rollback_copy")
            journal["status"] = "prepared"
            _write_journal(journal_path, journal)
            rollback_prepared = True
            for finished in finished_restores:
                _remove_finished_restore(target, finished)
        except BaseException:
            if not rollback_prepared:
                shutil.rmtree(rollback, ignore_errors=True)
            raise
        try:
            _inject_fault(fault_injector, "after_journal_write")
            entries_by_path = {entry.path: entry for entry in entries}
            for relative in operation_paths:
                entry = entries_by_path.get(relative)
                destination = _safe_child(target, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                journal["status"] = "applying"
                journal["pending"] = relative
                _write_journal(journal_path, journal)
                _inject_fault(fault_injector, "before_replace")
                if entry is None:
                    destination.unlink(missing_ok=True)
                    _fsync_directory(destination.parent)
                    _inject_fault(fault_injector, "after_remove")
                    journal["replaced"].append(relative)
                    journal["pending"] = None
                    _write_journal(journal_path, journal)
                    _inject_fault(fault_injector, "after_replace_journal")
                    continue
                source = _safe_child(staging, entry.path)
                legacy_uid: int | None = None
                legacy_gid: int | None = None
                if entry.source_uid is None and destination.exists():
                    current_details = destination.lstat()
                    legacy_uid = int(current_details.st_uid)
                    legacy_gid = int(current_details.st_gid)
                os.replace(source, destination)
                _chown_durable(
                    destination,
                    entry.source_uid if entry.source_uid is not None else legacy_uid,
                    entry.source_gid if entry.source_gid is not None else legacy_gid,
                )
                _chmod_durable(destination, entry.source_mode)
                _inject_fault(fault_injector, "after_replace")
                journal["replaced"].append(relative)
                journal["pending"] = None
                _write_journal(journal_path, journal)
                _inject_fault(fault_injector, "after_replace_journal")
            journal["status"] = "completed"
            _write_journal(journal_path, journal)
            _inject_fault(fault_injector, "after_complete_journal")
            return {
                "dry_run": False,
                "changes": plan,
                "revision": manifest["revision"],
                "rollback_path": str(rollback),
            }
        except BaseException:
            _rollback_restore(target, rollback, journal)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def apply_backup_retention(
    backup_parent: Path,
    policy: BackupRetentionPolicy | None = None,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
    maintenance_lock_held: bool = False,
    release_state_root: Path | None = None,
) -> dict[str, object]:
    if not isinstance(maintenance_lock_held, bool):
        raise BackupError("maintenance lock state is invalid")
    parent = _existing_directory(backup_parent, "backup parent")
    if maintenance_lock_held:
        return _apply_backup_retention_locked(
            parent,
            policy,
            dry_run=dry_run,
            now=now,
            release_state_root=release_state_root,
        )
    with _maintenance_locks((parent,), "backup retention"):
        return _apply_backup_retention_locked(
            parent,
            policy,
            dry_run=dry_run,
            now=now,
            release_state_root=release_state_root,
        )


def _apply_backup_retention_locked(
    backup_parent: Path,
    policy: BackupRetentionPolicy | None,
    *,
    dry_run: bool,
    now: datetime | None,
    release_state_root: Path | None,
) -> dict[str, object]:
    parent = _existing_directory(backup_parent, "backup parent")
    active_policy = policy or BackupRetentionPolicy()
    if not isinstance(active_policy, BackupRetentionPolicy):
        raise BackupError("backup retention policy is invalid")
    if not isinstance(dry_run, bool):
        raise BackupError("backup retention dry-run flag is invalid")
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    snapshots = _verified_snapshots(parent)
    if any(snapshot.manifest["snapshot_kind"] == "deployment" for snapshot in snapshots) and release_state_root is None:
        raise BackupError("release state root is required to protect deployment rollback backups")
    release_references = (
        _release_backup_references(parent, release_state_root)
        if release_state_root is not None else set()
    )
    latest_by_kind: dict[str, _VerifiedSnapshot] = {}
    for snapshot in snapshots:
        kind = str(snapshot.manifest["snapshot_kind"])
        latest_by_kind.setdefault(kind, snapshot)

    protected_paths = {
        snapshot.path
        for snapshot in snapshots
        if snapshot.manifest["user_marked"] is True
    }
    protected_paths.update(snapshot.path for snapshot in latest_by_kind.values())
    protected_paths.update(release_references)
    protected = [snapshot for snapshot in snapshots if snapshot.path in protected_paths]
    unprotected = [
        snapshot for snapshot in snapshots if snapshot.path not in protected_paths
    ]
    cutoff = (
        current_time - timedelta(days=active_policy.max_age_days)
        if active_policy.max_age_days is not None
        else None
    )
    delete: list[_VerifiedSnapshot] = []
    kept: list[_VerifiedSnapshot] = []
    for index, snapshot in enumerate(unprotected):
        over_count = index >= active_policy.max_unprotected
        over_age = cutoff is not None and snapshot.created_at < cutoff
        (delete if over_count or over_age else kept).append(snapshot)

    deleted: list[dict[str, object]] = []
    if not dry_run:
        for snapshot in delete:
            if release_state_root is not None and snapshot.path in _release_backup_references(parent, release_state_root):
                raise BackupError("release backup references changed during retention")
            _delete_verified_snapshot(parent, snapshot)
            deleted.append(_snapshot_public(snapshot))
    return {
        "dry_run": dry_run,
        "policy": {
            "max_unprotected": active_policy.max_unprotected,
            "max_age_days": active_policy.max_age_days,
        },
        "snapshot_count": len(snapshots),
        "protected": [_snapshot_public(snapshot) for snapshot in protected],
        "kept": [_snapshot_public(snapshot) for snapshot in kept],
        "delete": [_snapshot_public(snapshot) for snapshot in delete],
        "deleted": deleted,
        "release_protected": sorted(path.name for path in release_references),
    }


def _release_backup_references(backup_parent: Path, release_state_root: Path) -> set[Path]:
    """Protect every snapshot still reachable through the active rollback chain."""
    state = _existing_directory(release_state_root, "release state")
    parent = _existing_directory(backup_parent, "backup parent")
    references: set[Path] = set()
    visited: set[str] = set()

    def read(path: Path) -> tuple[dict[str, Any], bytes]:
        file = _required_regular(path, "release state file")
        if file.stat().st_size > 4 * 1024 * 1024:
            raise BackupError("release state file is too large")
        raw = file.read_bytes()
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise BackupError("release state is invalid") from exc
        if not isinstance(value, dict):
            raise BackupError("release state is invalid")
        return value, raw

    def protect(value: object) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not value:
            raise BackupError("release backup reference is invalid")
        path = _existing_directory(Path(value), "referenced release backup")
        if path.parent != parent or _SNAPSHOT_NAME_RE.fullmatch(path.name) is None:
            raise BackupError("release backup reference is outside the backup root")
        references.add(path)

    def walk(release_id: object, *, expected_hash: object = None) -> None:
        if not isinstance(release_id, str) or re.fullmatch(r"[0-9a-f]{12}-[0-9a-f]{12}", release_id) is None:
            raise BackupError("release identity is invalid")
        if release_id in visited:
            return
        if len(visited) >= 1000:
            raise BackupError("release rollback chain exceeds its limit")
        visited.add(release_id)
        manifest, raw = read(_safe_child(state, f"manifests/{release_id}.json"))
        if expected_hash is not None and (not isinstance(expected_hash, str) or hashlib.sha256(raw).hexdigest() != expected_hash):
            raise BackupError("current release manifest checksum mismatch")
        if manifest.get("release_id") != release_id:
            raise BackupError("release manifest identity mismatch")
        protect(manifest.get("backup_path"))
        previous = manifest.get("previous")
        if isinstance(previous, dict) and previous.get("release_id") is not None:
            walk(previous["release_id"])

    pointer = state / "current.json"
    journal_path = state / "release-operation.json"
    if not pointer.exists() and not journal_path.exists():
        raise BackupError("release state has no current pointer or pending operation")
    if pointer.exists():
        current, _ = read(pointer)
        if not isinstance(current.get("manifest_sha256"), str):
            raise BackupError("current release manifest checksum is missing")
        walk(current.get("release_id"), expected_hash=current["manifest_sha256"])
    if journal_path.exists():
        journal, _ = read(journal_path)
        protect(journal.get("backup_path"))
        protect(journal.get("recovery_backup"))
        for key in ("current", "target", "previous"):
            item = journal.get(key)
            if isinstance(item, dict):
                protect(item.get("backup_path"))
                if item.get("release_id") is not None:
                    walk(item["release_id"])
    return references


def create_media_manifest(media_root: Path, output_path: Path) -> dict[str, object]:
    root = _existing_directory(media_root, "media root")
    output = Path(output_path)
    files: list[dict[str, object]] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        safe_names: list[str] = []
        for name in sorted(names):
            candidate = Path(directory) / name
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise BackupError("media manifest directory is unavailable") from exc
            if not _is_linklike(candidate, info) and stat.S_ISDIR(info.st_mode):
                safe_names.append(name)
        names[:] = safe_names
        for name in sorted(filenames):
            path = Path(directory) / name
            try:
                info = path.lstat()
            except OSError as exc:
                raise BackupError("media manifest file is unavailable") from exc
            if _is_linklike(path, info) or not stat.S_ISREG(info.st_mode):
                continue
            relative = path.relative_to(root).as_posix()
            info = path.stat()
            files.append(
                {
                    "path": relative,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "sha256": _sha256(path),
                }
            )
            if len(files) > 1_000_000:
                raise BackupError("media manifest file limit exceeded")
    payload = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "file_count": len(files),
        "total_bytes": sum(int(item["size"]) for item in files),
        "files": files,
    }
    _atomic_write_text(
        output,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return payload


def _verified_snapshots(parent: Path) -> list[_VerifiedSnapshot]:
    snapshots: list[_VerifiedSnapshot] = []
    try:
        children = tuple(parent.iterdir())
    except OSError as exc:
        raise BackupError("backup parent cannot be inspected") from exc
    for path in children:
        if not path.name.startswith("jav-pilot-"):
            continue
        if _SNAPSHOT_NAME_RE.fullmatch(path.name) is None:
            raise BackupError("backup parent contains an invalid snapshot path")
        identity = _directory_identity(path, "backup snapshot")
        manifest = verify_backup(path)
        if _directory_identity(path, "backup snapshot") != identity:
            raise BackupError("backup snapshot changed during verification")
        snapshots.append(
            _VerifiedSnapshot(
                path.resolve(strict=True),
                manifest,
                _manifest_created_at(manifest["created_at"]),
                identity,
            )
        )
        if len(snapshots) > _MAX_RETENTION_SNAPSHOTS:
            raise BackupError("backup snapshot limit exceeded")
    snapshots.sort(key=lambda item: (item.created_at, item.path.name), reverse=True)
    return snapshots


def _delete_verified_snapshot(parent: Path, snapshot: _VerifiedSnapshot) -> None:
    path = _existing_directory(snapshot.path, "backup snapshot")
    if path.parent != parent or _SNAPSHOT_NAME_RE.fullmatch(path.name) is None:
        raise BackupError("backup snapshot path is unsafe")
    if _directory_identity(path, "backup snapshot") != snapshot.identity:
        raise BackupError("backup snapshot changed before deletion")
    verify_backup(path)
    if _directory_identity(path, "backup snapshot") != snapshot.identity:
        raise BackupError("backup snapshot changed before deletion")
    try:
        shutil.rmtree(path)
        _fsync_directory(parent)
    except OSError as exc:
        raise BackupError("backup snapshot could not be deleted") from exc


def _snapshot_public(snapshot: _VerifiedSnapshot) -> dict[str, object]:
    result: dict[str, object] = {
        "name": snapshot.path.name,
        "created_at": str(snapshot.manifest["created_at"]),
        "revision": str(snapshot.manifest["revision"]),
        "snapshot_kind": str(snapshot.manifest["snapshot_kind"]),
        "user_marked": bool(snapshot.manifest["user_marked"]),
    }
    if "label" in snapshot.manifest:
        result["label"] = str(snapshot.manifest["label"])
    return result


def _selected_backup_paths(include_paths: Sequence[str]) -> tuple[str, ...]:
    if isinstance(include_paths, (str, bytes)) or not 1 <= len(include_paths) <= _MAX_BACKUP_FILES:
        raise BackupError("selected backup paths are invalid")
    selected: set[str] = set()
    for value in include_paths:
        if not isinstance(value, str):
            raise BackupError("selected backup paths are invalid")
        relative = _relative_path(value)
        if relative != value or relative in selected:
            raise BackupError("selected backup paths are invalid")
        if relative not in _JSON_SCHEMA_COMPONENTS and re.fullmatch(r"data/[A-Za-z0-9_.-]+\.sqlite3", relative) is None:
            raise BackupError("selected backup path is not persistent configuration or SQLite data")
        selected.add(relative)
    return tuple(sorted(selected))


def _selected_backup_sources(root: Path, include_paths: Sequence[str]) -> list[tuple[Path, str]]:
    selected: dict[str, Path] = {}
    for relative in include_paths:
        source = _required_regular(_safe_child(root, relative), "selected backup source")
        selected[relative] = source
        if relative.endswith(".sqlite3"):
            for member, member_relative in _sqlite_set_members(source, relative):
                if member.exists() or member.is_symlink():
                    selected[member_relative] = _required_regular(member, "selected SQLite source")
    if len(selected) > _MAX_BACKUP_FILES:
        raise BackupError("selected backup file limit exceeded")
    if any(path.stat().st_size > _MAX_BACKUP_FILE_BYTES for path in selected.values()) or sum(path.stat().st_size for path in selected.values()) > _MAX_BACKUP_TOTAL_BYTES:
        raise BackupError("selected backup size limit exceeded")
    return [(path, relative) for relative, path in sorted(selected.items())]


def _backup_sources(root: Path) -> list[tuple[Path, str]]:
    environment = _required_regular(_safe_child(root, ".env"), ".env")
    sources: list[tuple[Path, str]] = [(environment, ".env")]
    data = _existing_directory(root / "data", "data directory")
    stack: list[tuple[Path, PurePosixPath, int]] = [(data, PurePosixPath("data"), 0)]
    total_bytes = environment.stat(follow_symlinks=False).st_size
    while stack:
        directory, relative_root, depth = stack.pop()
        if depth > 32:
            raise BackupError("backup directory depth limit exceeded")
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise BackupError("backup data directory cannot be inspected") from exc
        for child in children:
            if child.name == MAINTENANCE_LOCK_NAME:
                continue
            relative_path = relative_root / child.name
            relative = relative_path.as_posix()
            # Legacy releases stored the persistent Chromium profile under
            # data/. It contains cookies, history and cache, so exclude the
            # whole subtree rather than attempting to classify its files.
            if _is_missav_profile_path(relative_path):
                continue
            try:
                details = child.lstat()
            except OSError as exc:
                raise BackupError(f"backup source is unavailable: {relative}") from exc
            if _is_linklike(child, details):
                raise BackupError(f"backup source contains a link: {relative}")
            if stat.S_ISDIR(details.st_mode):
                stack.append((child, relative_path, depth + 1))
                continue
            if not stat.S_ISREG(details.st_mode):
                raise BackupError(f"backup source is not a regular file: {relative}")
            if details.st_size > _MAX_BACKUP_FILE_BYTES:
                raise BackupError(f"backup source file is too large: {relative}")
            total_bytes += details.st_size
            if total_bytes > _MAX_BACKUP_TOTAL_BYTES:
                raise BackupError("backup source size limit exceeded")
            sources.append((child, relative))
            if len(sources) > _MAX_BACKUP_FILES:
                raise BackupError("backup file limit exceeded")

    required_databases = {"web_downloads.sqlite3", "media_metadata.sqlite3"}
    databases = {
        path.name
        for path, relative in sources
        if relative.startswith("data/") and relative.endswith(".sqlite3")
    }
    if not required_databases.issubset(databases):
        raise BackupError("required SQLite databases are missing")
    required_paths = set(REQUIRED_FILES)
    available_paths = {relative for _path, relative in sources}
    if not required_paths.issubset(available_paths):
        raise BackupError("required backup files are missing")
    return sorted(sources, key=lambda item: item[1])


def _is_missav_profile_path(relative: PurePosixPath) -> bool:
    """Identify legacy sensitive browser state inside the application tree."""

    parts = relative.parts
    return (
        len(parts) >= 2
        and parts[0] == "data"
        and _MISSAV_PROFILE_DIR_RE.fullmatch(parts[1]) is not None
    )


def _copy_stable_sqlite_set(
    database: Path,
    destination_root: Path,
    relative: str,
    *,
    fault_injector: FaultInjector | None,
) -> list[BackupEntry]:
    last_error: BaseException | None = None
    for attempt in range(_SQLITE_SNAPSHOT_ATTEMPTS):
        copied_paths: set[str] = set()
        try:
            before = _sqlite_set_state(database, relative)
            entries: list[BackupEntry] = []
            for member, member_relative in _sqlite_set_members(database, relative):
                if member_relative not in before:
                    continue
                destination = _safe_child(destination_root, member_relative)
                entry = _copy_exact(member, destination, member_relative)
                if entry.sha256 != before[member_relative]["sha256"]:
                    raise BackupError(
                        f"SQLite source hash changed during backup: {member_relative}"
                    )
                entries.append(entry)
                copied_paths.add(member_relative)
                _inject_fault(fault_injector, "after_copy")
            after = _sqlite_set_state(database, relative)
            if before != after or copied_paths != set(before):
                raise BackupError(f"SQLite set changed during backup: {relative}")
            return entries
        except (BackupError, OSError, sqlite3.Error) as exc:
            last_error = exc
            for member_relative in copied_paths | {
                relative,
                f"{relative}-wal",
                f"{relative}-shm",
            }:
                _safe_child(destination_root, member_relative).unlink(missing_ok=True)
            if attempt + 1 < _SQLITE_SNAPSHOT_ATTEMPTS:
                time.sleep(_SQLITE_SNAPSHOT_RETRY_SECONDS * (attempt + 1))
    raise BackupError(f"SQLite set did not stabilize: {relative}") from last_error


def _sqlite_set_members(
    database: Path,
    relative: str,
) -> tuple[tuple[Path, str], ...]:
    return (
        (database, relative),
        (Path(f"{database}-wal"), f"{relative}-wal"),
        (Path(f"{database}-shm"), f"{relative}-shm"),
    )


def _sqlite_set_state(
    database: Path,
    relative: str,
) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    for member, member_relative in _sqlite_set_members(database, relative):
        if not member.exists() and not member.is_symlink():
            continue
        state[member_relative] = _stable_file_state(
            member,
            wal=member_relative.endswith(".sqlite3-wal"),
        )
    if relative not in state:
        raise BackupError(f"required SQLite database is missing: {relative}")
    return state


def _stable_file_state(path: Path, *, wal: bool) -> dict[str, object]:
    before = path.lstat()
    if _is_linklike(path, before) or not stat.S_ISREG(before.st_mode):
        raise BackupError("SQLite set contains an unsafe file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    header = bytearray()
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BackupError("SQLite set changed before inspection")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                if wal and len(header) < 32:
                    header.extend(chunk[: 32 - len(header)])
                digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
    )
    if identity != (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
    ):
        raise BackupError("SQLite set changed during inspection")
    result: dict[str, object] = {
        "identity": list(identity),
        "sha256": digest.hexdigest(),
    }
    if wal:
        result["wal"] = _wal_identity(bytes(header), after.st_size)
    return result


def _wal_identity(header: bytes, size: int) -> dict[str, object]:
    if size == 0:
        return {"frames": 0, "page_size": 0, "salt": ""}
    if size < 32 or len(header) != 32:
        raise BackupError("SQLite WAL header is incomplete")
    page_size = int.from_bytes(header[8:12], "big")
    if page_size == 1:
        page_size = 65_536
    if page_size < 512 or page_size > 65_536 or page_size & (page_size - 1):
        raise BackupError("SQLite WAL page size is invalid")
    frame_size = page_size + 24
    payload = size - 32
    if payload % frame_size:
        raise BackupError("SQLite WAL contains a partial frame")
    return {
        "frames": payload // frame_size,
        "page_size": page_size,
        "salt": header[16:24].hex(),
    }


def _sqlite_schema_versions(path: Path) -> dict[str, int]:
    versions: dict[str, int] = {}
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_migrations'"
        ).fetchone()
        if table is None:
            return versions
        rows = connection.execute(
            "SELECT component, MAX(version) FROM schema_migrations GROUP BY component"
        ).fetchall()
        for component, version in rows:
            if (
                not isinstance(component, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", component) is None
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
            ):
                raise BackupError("backup SQLite schema versions are invalid")
            versions[component] = version
        return versions
    finally:
        connection.close()


def _verify_copied_databases(
    root: Path,
    entries: list[BackupEntry],
) -> dict[str, object]:
    result: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="jav-pilot-backup-verify-") as tmp:
        temporary = Path(tmp)
        for entry in entries:
            if not (
                entry.path.endswith(".sqlite3")
                or entry.path.endswith(".sqlite3-wal")
                or entry.path.endswith(".sqlite3-shm")
            ):
                continue
            source = _safe_child(root, entry.path)
            destination = _safe_child(temporary, entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        for entry in entries:
            if not entry.path.endswith(".sqlite3"):
                continue
            database = _safe_child(temporary, entry.path)
            connection = sqlite3.connect(database, timeout=10.0)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                connection.close()
            if integrity != ("ok",):
                raise BackupError(f"backup SQLite integrity check failed: {entry.path}")
            result[entry.path] = {
                "integrity_check": "ok",
                "schema_versions": _sqlite_schema_versions(database),
            }
    return result


def _schema_contract(
    json_schemas: dict[str, int],
    sqlite_checks: dict[str, object],
) -> dict[str, object]:
    components: dict[str, tuple[str, int]] = {}

    def add_component(name: object, kind: str, version: object) -> None:
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            raise BackupError("backup schema versions are invalid")
        current = components.get(name)
        candidate = (kind, version)
        if current is not None and current != candidate:
            raise BackupError("backup schema component versions conflict")
        components[name] = candidate

    if not set(json_schemas).issubset(_JSON_SCHEMA_COMPONENTS):
        raise BackupError("backup JSON schema versions are invalid")
    for relative, name in _JSON_SCHEMA_COMPONENTS.items():
        if relative in json_schemas:
            add_component(name, "json", json_schemas[relative])

    for check in sqlite_checks.values():
        versions = check.get("schema_versions") if isinstance(check, dict) else None
        if not isinstance(versions, dict):
            raise BackupError("backup SQLite schema versions are invalid")
        for name, version in versions.items():
            add_component(name, "sqlite", version)

    return {
        "contract_version": _SCHEMA_CONTRACT_VERSION,
        "components": [
            {
                "component": name,
                "kind": kind,
                "current_version": version,
                "minimum_reader_version": version,
                "downgrade_requires_restore": True,
            }
            for name, (kind, version) in sorted(components.items())
        ],
    }


def _verify_manifest_metadata(
    root: Path,
    manifest: dict[str, Any],
    actual_sqlite: dict[str, object],
) -> None:
    expected_json = manifest.get("json_schemas")
    if not isinstance(expected_json, dict) or any(
        not isinstance(path, str)
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        for path, version in expected_json.items()
    ):
        raise BackupError("backup JSON schema manifest is invalid")
    actual_json = _json_schema_versions(root)
    if expected_json != actual_json:
        raise BackupError("backup JSON schema manifest does not match its files")

    expected_sqlite = manifest.get("sqlite")
    if not isinstance(expected_sqlite, dict) or expected_sqlite != actual_sqlite:
        raise BackupError("backup SQLite schema manifest does not match its files")
    schema_contract = manifest.get("schema_contract")
    _validate_schema_contract(schema_contract)
    if manifest.get(
        "format_version"
    ) == BACKUP_FORMAT_VERSION and schema_contract != _schema_contract(
        actual_json, actual_sqlite
    ):
        raise BackupError("backup schema contract does not match its files")


def _validate_schema_contract(value: object) -> None:
    if not isinstance(value, dict):
        raise BackupError("backup schema contract is invalid")
    contract_version = value.get("contract_version")
    components = value.get("components")
    if (
        isinstance(contract_version, bool)
        or not isinstance(contract_version, int)
        or contract_version < 1
        or not isinstance(components, list)
        or not components
        or len(components) > 128
    ):
        raise BackupError("backup schema contract is invalid")
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise BackupError("backup schema contract is invalid")
        name = component.get("component")
        kind = component.get("kind")
        current = component.get("current_version")
        minimum = component.get("minimum_reader_version")
        restore = component.get("downgrade_requires_restore")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
            or name in seen
            or kind not in {"json", "sqlite"}
            or isinstance(current, bool)
            or not isinstance(current, int)
            or current < 1
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or not 1 <= minimum <= current
            or not isinstance(restore, bool)
        ):
            raise BackupError("backup schema contract is invalid")
        seen.add(name)


def _json_schema_versions(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for relative in ("data/app_config.json", "data/settings.json"):
        path = _safe_child(root, relative)
        if not path.exists():
            continue
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise BackupError(f"JSON config is too large: {relative}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"JSON config is invalid: {relative}") from exc
        version = payload.get("schema_version") if isinstance(payload, dict) else None
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BackupError(f"JSON config schema is invalid: {relative}")
        result[relative] = version
    return result


def _copy_exact(
    source: Path,
    destination: Path,
    relative: str,
    *,
    restrict_permissions: bool = True,
) -> BackupEntry:
    before = source.lstat()
    if _is_linklike(source, before) or not stat.S_ISREG(before.st_mode):
        raise BackupError(f"source is not a regular file: {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    destination_fd = -1
    digest = hashlib.sha256()
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BackupError(f"source changed before backup: {relative}")
        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_fd = os.open(destination, destination_flags, 0o600)
        destination_handle = os.fdopen(destination_fd, "wb")
        destination_fd = -1
        with os.fdopen(source_fd, "rb", closefd=False) as source_handle:
            with destination_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    destination_handle.write(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
        after = os.fstat(source_fd)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise BackupError(f"source changed during backup: {relative}")
    finally:
        if destination_fd >= 0:
            with suppress(OSError):
                os.close(destination_fd)
        os.close(source_fd)
    if restrict_permissions:
        os.chmod(destination, 0o600)
    _fsync_directory(destination.parent)
    backup_mode = stat.S_IMODE(destination.stat().st_mode)
    return BackupEntry(
        relative,
        before.st_size,
        digest.hexdigest(),
        stat.S_IMODE(before.st_mode),
        backup_mode,
        int(before.st_uid),
        int(before.st_gid),
    )


def _entry_metadata(entry: BackupEntry) -> dict[str, object]:
    return {
        "size": entry.size,
        "sha256": entry.sha256,
        "source_mode": entry.source_mode,
        "backup_mode": entry.backup_mode,
        "source_uid": entry.source_uid,
        "source_gid": entry.source_gid,
    }


def _read_manifest(root: Path) -> dict[str, Any]:
    path = _required_regular(root / MANIFEST_NAME, MANIFEST_NAME)
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise BackupError("backup manifest is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise BackupError("backup format is unsupported")
    format_version = payload.get("format_version")
    if (
        isinstance(format_version, bool)
        or format_version not in SUPPORTED_BACKUP_FORMAT_VERSIONS
    ):
        raise BackupError("backup format is unsupported")
    revision = payload.get("revision")
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise BackupError("backup revision is invalid")
    _manifest_created_at(payload.get("created_at"))
    normalized = dict(payload)
    if format_version == 1:
        normalized["snapshot_kind"] = "manual"
        normalized["user_marked"] = False
        normalized.pop("label", None)
        return normalized
    normalized["snapshot_kind"] = _snapshot_kind(payload.get("snapshot_kind"))
    user_marked = payload.get("user_marked")
    if not isinstance(user_marked, bool):
        raise BackupError("backup mark is invalid")
    normalized["user_marked"] = user_marked
    clean_label = _backup_label(payload.get("label"))
    if clean_label is None:
        normalized.pop("label", None)
    else:
        normalized["label"] = clean_label
    return normalized


def _manifest_entries(manifest: dict[str, Any]) -> list[BackupEntry]:
    format_version = manifest.get("format_version")
    raw_entries = manifest.get("entries")
    if (
        not isinstance(raw_entries, list)
        or not 1 <= len(raw_entries) <= _MAX_BACKUP_FILES
    ):
        raise BackupError("backup entry list is invalid")
    result: list[BackupEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise BackupError("backup entry is invalid")
        relative = _relative_path(raw.get("path"))
        if relative in seen:
            raise BackupError("backup contains duplicate entries")
        size = raw.get("size")
        source_mode = raw.get("source_mode")
        backup_mode = raw.get("backup_mode")
        source_uid = raw.get("source_uid") if format_version in {3, 4} else None
        source_gid = raw.get("source_gid") if format_version in {3, 4} else None
        sha256 = raw.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or not _DIGEST_RE.fullmatch(sha256)
        ):
            raise BackupError("backup entry checksum is invalid")
        if (
            isinstance(source_mode, bool)
            or not isinstance(source_mode, int)
            or not 0 <= source_mode <= 0o7777
            or isinstance(backup_mode, bool)
            or not isinstance(backup_mode, int)
            or not 0 <= backup_mode <= 0o7777
        ):
            raise BackupError("backup entry permissions are invalid")
        if format_version in {3, 4} and (
            isinstance(source_uid, bool)
            or not isinstance(source_uid, int)
            or not 0 <= source_uid <= 0xFFFFFFFF
            or isinstance(source_gid, bool)
            or not isinstance(source_gid, int)
            or not 0 <= source_gid <= 0xFFFFFFFF
        ):
            raise BackupError("backup entry ownership is invalid")
        result.append(
            BackupEntry(
                relative,
                size,
                sha256,
                source_mode,
                backup_mode,
                source_uid,
                source_gid,
            )
        )
        seen.add(relative)
    return result


def _sqlite_sidecar_removals(entries: list[BackupEntry]) -> frozenset[str]:
    present = {entry.path for entry in entries}
    removals = {
        sidecar
        for entry in entries
        if entry.path.endswith(".sqlite3")
        for sidecar in (f"{entry.path}-wal", f"{entry.path}-shm")
        if sidecar not in present
    }
    return frozenset(removals)


def _restore_operation_paths(
    entries: list[BackupEntry], removals: frozenset[str]
) -> list[str]:
    paths = {entry.path for entry in entries} | set(removals)
    if len(paths) > _MAX_RESTORE_OPERATIONS:
        raise BackupError("restore operation limit exceeded")

    def order(relative: str) -> tuple[str, int, str]:
        for suffix in ("-wal", "-shm"):
            if relative in removals and relative.endswith(f".sqlite3{suffix}"):
                return relative[: -len(suffix)], 0, relative
        return relative, 1, relative

    return sorted(paths, key=order)


def _restore_plan(
    entries: list[BackupEntry],
    target: Path,
    *,
    removals: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    entries_by_path = {entry.path: entry for entry in entries}
    for relative in _restore_operation_paths(entries, removals):
        entry = entries_by_path.get(relative)
        current = _safe_child(target, relative)
        if current.exists() or current.is_symlink():
            try:
                info = current.lstat()
            except OSError as exc:
                raise BackupError(f"restore target is unavailable: {relative}") from exc
            if _is_linklike(current, info) or not stat.S_ISREG(info.st_mode):
                raise BackupError(f"restore target is unsafe: {relative}")
        current_hash = _sha256(current) if current.exists() else None
        changes.append(
            {
                "path": relative,
                "action": (
                    "delete"
                    if entry is None and current_hash is not None
                    else "unchanged"
                    if entry is None or current_hash == entry.sha256
                    else "replace"
                ),
            }
        )
    return changes


def _recover_interrupted_restores(
    target: Path, *, dry_run: bool = False,
) -> list[_FinishedRestore]:
    finished: list[_FinishedRestore] = []
    for rollback in sorted(
        target.glob(".jav-pilot-restore-rollback-*"), key=lambda path: path.name
    ):
        if _RESTORE_ROLLBACK_NAME_RE.fullmatch(rollback.name) is None:
            raise BackupError("an interrupted restore path is unsafe")
        try:
            rollback_info = rollback.lstat()
        except OSError as exc:
            raise BackupError("an interrupted restore is unavailable") from exc
        if _is_linklike(rollback, rollback_info) or not stat.S_ISDIR(
            rollback_info.st_mode
        ):
            raise BackupError("an interrupted restore path is unsafe")
        identity = (int(rollback_info.st_dev), int(rollback_info.st_ino))
        journal, journal_sha256 = _read_restore_journal(rollback)
        if _directory_identity(rollback, "restore rollback") != identity:
            raise BackupError("restore rollback changed during recovery")
        if dry_run:
            if journal["status"] not in {"completed", "rolled_back"}:
                raise BackupError("a pending restore requires --apply; dry-run did not change it")
            finished.append(_FinishedRestore(rollback, identity, journal_sha256))
            continue
        if journal.get("status") == "preparing":
            _discard_preparing_restore(target, rollback, journal)
        elif journal.get("status") in {"prepared", "applying"}:
            _rollback_restore(target, rollback, journal)
            if _directory_identity(rollback, "restore rollback") != identity:
                raise BackupError("restore rollback changed during recovery")
            journal, journal_sha256 = _read_restore_journal(rollback)
            if journal["status"] != "rolled_back":
                raise BackupError("an interrupted restore has an invalid journal")
            finished.append(_FinishedRestore(rollback, identity, journal_sha256))
        elif journal.get("status") in {"completed", "rolled_back"}:
            _remove_restore_staging(target, journal["staging"])
            if _directory_identity(rollback, "restore rollback") != identity:
                raise BackupError("restore rollback changed during recovery")
            finished.append(_FinishedRestore(rollback, identity, journal_sha256))
    return finished


def _rollback_restore(target: Path, rollback: Path, journal: dict[str, Any]) -> None:
    clean_journal = _validated_restore_journal(journal)
    journal_version = clean_journal["journal_version"]
    originals = clean_journal["originals"]
    missing = set(clean_journal["originally_missing"])
    replaced = list(clean_journal["replaced"])
    pending = clean_journal["pending"]
    if pending is not None and pending not in replaced:
        replaced.append(pending)
    for relative in reversed(replaced):
        destination = _safe_child(target, relative)
        if relative in missing:
            destination.unlink(missing_ok=True)
            _fsync_directory(destination.parent)
            continue
        source = _required_regular(
            _safe_child(rollback, relative), "restore rollback data"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.rollback-{uuid.uuid4().hex}"
        )
        try:
            copied = _copy_exact(
                source,
                temporary,
                relative,
                restrict_permissions=False,
            )
            if journal_version >= 2:
                metadata = originals[relative]
                if (
                    copied.size != metadata["size"]
                    or copied.sha256 != metadata["sha256"]
                    or copied.source_mode != metadata["backup_mode"]
                ):
                    raise BackupError(f"restore rollback data mismatch: {relative}")
                original_mode = metadata["source_mode"]
                original_uid = metadata.get("source_uid")
                original_gid = metadata.get("source_gid")
            else:
                original_mode = copied.source_mode
                original_uid = None
                original_gid = None
            _chown_durable(temporary, original_uid, original_gid)
            _chmod_durable(temporary, original_mode)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
    clean_journal["status"] = "rolled_back"
    clean_journal["pending"] = None
    persisted_journal = dict(clean_journal)
    if journal_version < 4:
        persisted_journal.pop("remove", None)
    _write_journal(rollback / "restore-journal.json", persisted_journal)
    _remove_restore_staging(target, clean_journal["staging"])


def _discard_preparing_restore(
    target: Path,
    rollback: Path,
    journal: dict[str, Any],
) -> None:
    clean_journal = _validated_restore_journal(journal)
    if (
        clean_journal["journal_version"] < 2
        or clean_journal["status"] != "preparing"
        or _RESTORE_ROLLBACK_NAME_RE.fullmatch(rollback.name) is None
        or rollback.parent.resolve(strict=True) != target.resolve(strict=True)
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    identity = _directory_identity(rollback, "restore rollback")
    _remove_restore_staging(target, clean_journal["staging"])
    if _directory_identity(rollback, "restore rollback") != identity:
        raise BackupError("restore rollback changed before cleanup")
    try:
        shutil.rmtree(rollback)
        _fsync_directory(target)
    except OSError as exc:
        raise BackupError("restore rollback could not be cleaned") from exc


def _remove_finished_restore(
    target: Path,
    finished: _FinishedRestore,
) -> None:
    rollback = finished.path
    if _RESTORE_ROLLBACK_NAME_RE.fullmatch(
        rollback.name
    ) is None or rollback.parent.resolve(strict=True) != target.resolve(strict=True):
        raise BackupError("an interrupted restore has an invalid journal")
    if _directory_identity(rollback, "restore rollback") != finished.identity:
        raise BackupError("restore rollback changed before cleanup")
    clean_journal, journal_sha256 = _read_restore_journal(rollback)
    if (
        clean_journal["status"] not in {"completed", "rolled_back"}
        or journal_sha256 != finished.journal_sha256
    ):
        raise BackupError("restore rollback changed before cleanup")
    _remove_restore_staging(target, clean_journal["staging"])
    if _directory_identity(rollback, "restore rollback") != finished.identity:
        raise BackupError("restore rollback changed before cleanup")
    clean_journal, journal_sha256 = _read_restore_journal(rollback)
    if (
        clean_journal["status"] not in {"completed", "rolled_back"}
        or journal_sha256 != finished.journal_sha256
        or _directory_identity(rollback, "restore rollback") != finished.identity
    ):
        raise BackupError("restore rollback changed before cleanup")
    try:
        shutil.rmtree(rollback)
        _fsync_directory(target)
    except OSError as exc:
        raise BackupError("restore rollback could not be cleaned") from exc


def _read_restore_journal(rollback: Path) -> tuple[dict[str, Any], str]:
    journal_path = rollback / "restore-journal.json"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(journal_path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_MANIFEST_BYTES:
            raise BackupError("an interrupted restore has an invalid journal")
        payload = bytearray()
        while len(payload) <= _MAX_MANIFEST_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_MANIFEST_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("an interrupted restore has an invalid journal") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    try:
        current = journal_path.lstat()
    except OSError as exc:
        raise BackupError("an interrupted restore has an invalid journal") from exc
    if (
        len(payload) > _MAX_MANIFEST_BYTES
        or _is_linklike(journal_path, current)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
        or after.st_size != len(payload)
        or after.st_size != current.st_size
        or after.st_mtime_ns != current.st_mtime_ns
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    journal_bytes = bytes(payload)
    try:
        raw_journal = json.loads(journal_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("an interrupted restore has an invalid journal") from exc
    return _validated_restore_journal(raw_journal), hashlib.sha256(
        journal_bytes
    ).hexdigest()


def _validated_restore_journal(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackupError("an interrupted restore has an invalid journal")
    journal_version = value.get("journal_version", 1)
    if (
        isinstance(journal_version, bool)
        or journal_version not in SUPPORTED_RESTORE_JOURNAL_VERSIONS
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    status_value = value.get("status")
    if status_value not in {
        "preparing",
        "prepared",
        "applying",
        "completed",
        "rolled_back",
    }:
        raise BackupError("an interrupted restore has an invalid journal")
    entries = _journal_paths(
        value.get("entries"), "entries", maximum=_MAX_RESTORE_OPERATIONS
    )
    replaced = _journal_paths(
        value.get("replaced"),
        "replaced",
        allow_empty=True,
        maximum=_MAX_RESTORE_OPERATIONS,
    )
    originally_missing = _journal_paths(
        value.get("originally_missing"),
        "originally missing",
        allow_empty=True,
        maximum=_MAX_RESTORE_OPERATIONS,
    )
    entry_set = set(entries)
    if not set(replaced).issubset(entry_set) or not set(originally_missing).issubset(
        entry_set
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    pending_value = value.get("pending")
    pending = None if pending_value is None else _relative_path(pending_value)
    if pending is not None and pending not in entry_set:
        raise BackupError("an interrupted restore has an invalid journal")
    if replaced != entries[: len(replaced)] or pending in set(replaced):
        raise BackupError("an interrupted restore has an invalid journal")
    if pending is not None and pending != entries[len(replaced)]:
        raise BackupError("an interrupted restore has an invalid journal")
    if status_value == "preparing" and (
        journal_version < 2 or replaced or pending is not None
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    if status_value == "prepared" and (replaced or pending is not None):
        raise BackupError("an interrupted restore has an invalid journal")
    if status_value == "completed" and (replaced != entries or pending is not None):
        raise BackupError("an interrupted restore has an invalid journal")
    if status_value == "rolled_back" and pending is not None:
        raise BackupError("an interrupted restore has an invalid journal")
    staging_value = value.get("staging")
    if staging_value is None:
        staging = None
    elif (
        not isinstance(staging_value, str)
        or _RESTORE_STAGING_NAME_RE.fullmatch(staging_value) is None
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    else:
        staging = staging_value
    backup_value = value.get("backup")
    if (
        not isinstance(backup_value, str)
        or not 1 <= len(backup_value) <= 4096
        or "\x00" in backup_value
        or "\n" in backup_value
        or "\r" in backup_value
    ):
        raise BackupError("an interrupted restore has an invalid journal")
    if journal_version >= 2:
        if staging is None:
            raise BackupError("an interrupted restore has an invalid journal")
        originals = _journal_originals(
            value.get("originals"),
            ownership=journal_version >= 3,
        )
        original_set = set(originals)
        missing_set = set(originally_missing)
        if original_set & missing_set:
            raise BackupError("an interrupted restore has an invalid journal")
        if status_value == "preparing":
            if not (original_set | missing_set).issubset(entry_set):
                raise BackupError("an interrupted restore has an invalid journal")
        elif original_set != entry_set - missing_set:
            raise BackupError("an interrupted restore has an invalid journal")
    else:
        originals = {}
    if journal_version >= 4:
        removals = _journal_paths(
            value.get("remove"),
            "remove",
            allow_empty=True,
            maximum=_MAX_RESTORE_OPERATIONS,
        )
        if not set(removals).issubset(entry_set) or any(
            not relative.endswith((".sqlite3-wal", ".sqlite3-shm"))
            for relative in removals
        ):
            raise BackupError("an interrupted restore has an invalid journal")
    else:
        if value.get("remove") is not None:
            raise BackupError("an interrupted restore has an invalid journal")
        removals = []
    clean_journal = {
        "journal_version": journal_version,
        "status": status_value,
        "backup": backup_value,
        "staging": staging,
        "entries": entries,
        "replaced": replaced,
        "originally_missing": originally_missing,
        "originals": originals,
        "pending": pending,
    }
    if journal_version >= 4:
        clean_journal["remove"] = removals
    return clean_journal


def _journal_originals(
    value: object,
    *,
    ownership: bool,
) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or len(value) > _MAX_RESTORE_OPERATIONS:
        raise BackupError("an interrupted restore has an invalid journal")
    originals: dict[str, dict[str, object]] = {}
    for raw_path, raw_metadata in value.items():
        if not isinstance(raw_path, str):
            raise BackupError("an interrupted restore has an invalid journal")
        relative = _relative_path(raw_path)
        if relative in originals or not isinstance(raw_metadata, dict):
            raise BackupError("an interrupted restore has an invalid journal")
        expected_keys = {
            "size",
            "sha256",
            "source_mode",
            "backup_mode",
        }
        if ownership:
            expected_keys.update(("source_uid", "source_gid"))
        if set(raw_metadata) != expected_keys:
            raise BackupError("an interrupted restore has an invalid journal")
        size = raw_metadata.get("size")
        sha256 = raw_metadata.get("sha256")
        source_mode = raw_metadata.get("source_mode")
        backup_mode = raw_metadata.get("backup_mode")
        source_uid = raw_metadata.get("source_uid") if ownership else None
        source_gid = raw_metadata.get("source_gid") if ownership else None
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or _DIGEST_RE.fullmatch(sha256) is None
            or isinstance(source_mode, bool)
            or not isinstance(source_mode, int)
            or not 0 <= source_mode <= 0o7777
            or isinstance(backup_mode, bool)
            or not isinstance(backup_mode, int)
            or not 0 <= backup_mode <= 0o7777
        ):
            raise BackupError("an interrupted restore has an invalid journal")
        if ownership and (
            isinstance(source_uid, bool)
            or not isinstance(source_uid, int)
            or not 0 <= source_uid <= 0xFFFFFFFF
            or isinstance(source_gid, bool)
            or not isinstance(source_gid, int)
            or not 0 <= source_gid <= 0xFFFFFFFF
        ):
            raise BackupError("an interrupted restore has an invalid journal")
        originals[relative] = {
            "size": size,
            "sha256": sha256,
            "source_mode": source_mode,
            "backup_mode": backup_mode,
        }
        if ownership:
            originals[relative]["source_uid"] = source_uid
            originals[relative]["source_gid"] = source_gid
    return originals


def _remove_restore_staging(target: Path, name: str | None) -> None:
    if name is None:
        return
    if _RESTORE_STAGING_NAME_RE.fullmatch(name) is None:
        raise BackupError("an interrupted restore has an invalid staging path")
    staging = target / name
    if not staging.exists() and not staging.is_symlink():
        return
    identity = _directory_identity(staging, "restore staging")
    if staging.parent.resolve(strict=True) != target.resolve(strict=True):
        raise BackupError("an interrupted restore staging path is unsafe")
    if _directory_identity(staging, "restore staging") != identity:
        raise BackupError("restore staging changed before cleanup")
    try:
        shutil.rmtree(staging)
        _fsync_directory(target)
    except OSError as exc:
        raise BackupError("restore staging could not be cleaned") from exc


def _journal_paths(
    value: object,
    _label: str,
    *,
    allow_empty: bool = False,
    maximum: int = _MAX_BACKUP_FILES,
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise BackupError("an interrupted restore has an invalid journal")
    paths = [_relative_path(item) for item in value]
    if len(paths) > maximum or len(paths) != len(set(paths)):
        raise BackupError("an interrupted restore has an invalid journal")
    return paths


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    restrict_permissions: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if restrict_permissions:
            _restrict_atomic_permissions(temporary)
        _fsync_atomic_file(temporary)
        os.replace(temporary, path)
        if restrict_permissions:
            _restrict_atomic_permissions(path)
        _fsync_atomic_file(path)
        _fsync_directory(path.parent)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _restrict_atomic_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fsync_atomic_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _snapshot_kind(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in SNAPSHOT_KINDS:
        raise BackupError("backup snapshot kind is invalid")
    return clean


def _backup_label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BackupError("backup label is invalid")
    clean = value.strip()
    if _LABEL_RE.fullmatch(clean) is None:
        raise BackupError("backup label is invalid")
    return clean


def _manifest_created_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise BackupError("backup creation timestamp is invalid")
    try:
        created = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise BackupError("backup creation timestamp is invalid") from exc
    if created.strftime("%Y%m%dT%H%M%SZ") != value:
        raise BackupError("backup creation timestamp is invalid")
    return created


def _inject_fault(injector: FaultInjector | None, point: str) -> None:
    if injector is None:
        return
    try:
        injector(point)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"backup operation interrupted at {point}") from exc


def _relative_path(value: object) -> str:
    clean = str(value or "").strip()
    path = PurePosixPath(clean)
    if (
        not clean
        or path.is_absolute()
        or clean != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BackupError("backup path is unsafe")
    return clean


def _safe_child(root: Path, relative: object) -> Path:
    clean = _relative_path(relative)
    parts = PurePosixPath(clean).parts
    candidate = root.joinpath(*parts)
    current = root
    for part in parts[:-1]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            info = current.lstat()
        except OSError as exc:
            raise BackupError("backup path is unavailable") from exc
        if _is_linklike(current, info) or not stat.S_ISDIR(info.st_mode):
            raise BackupError("backup path contains an unsafe directory")
    return candidate


def _required_regular(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError(f"required file is missing: {label}") from exc
    if _is_linklike(path, info) or not stat.S_ISREG(info.st_mode):
        raise BackupError(f"required file is unsafe: {label}")
    return path


def _existing_directory(path: Path, label: str) -> Path:
    return _directory(path, label, create=False)


def _directory(path: Path, label: str, *, create: bool) -> Path:
    clean = Path(path).expanduser()
    if create:
        clean.mkdir(parents=True, exist_ok=True)
    try:
        info = clean.lstat()
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc
    if _is_linklike(clean, info) or not stat.S_ISDIR(info.st_mode):
        raise BackupError(f"unsafe directory: {label}")
    try:
        return clean.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc


def _directory_identity(path: Path, label: str) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc
    if _is_linklike(path, info) or not stat.S_ISDIR(info.st_mode):
        raise BackupError(f"{label} is unsafe")
    return int(info.st_dev), int(info.st_ino)


def _is_linklike(path: Path, info: os.stat_result | None = None) -> bool:
    details = info or path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & reparse_flag
    )


def _ensure_capacity(parent: Path, sources: list[tuple[Path, str]]) -> None:
    required = sum(path.stat().st_size for path, _relative in sources)
    reserve = max(64 * 1024 * 1024, required // 20)
    if shutil.disk_usage(parent).free < required + reserve:
        raise BackupError("backup destination does not have enough free space")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod_durable(path: Path, mode: int) -> None:
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise BackupError("restore file permissions are invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if os.name != "nt" and hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(path, mode)
        applied = os.fstat(descriptor)
        current = path.lstat()
        if (
            _is_linklike(path, current)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or (applied.st_dev, applied.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_IMODE(applied.st_mode) != mode
            or stat.S_IMODE(current.st_mode) != mode
        ):
            raise BackupError("restore file permissions could not be applied")
        if os.name != "nt":
            os.fsync(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)
    _fsync_directory(path.parent)


def _chown_durable(path: Path, uid: int | None, gid: int | None) -> None:
    if uid is None and gid is None:
        return
    if (
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or not 0 <= uid <= 0xFFFFFFFF
        or isinstance(gid, bool)
        or not isinstance(gid, int)
        or not 0 <= gid <= 0xFFFFFFFF
    ):
        raise BackupError("restore file ownership is invalid")
    if os.name == "nt":  # pragma: no cover - production restore runs on Linux.
        return
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        os.fchown(descriptor, uid, gid)
        applied = os.fstat(descriptor)
        current = path.lstat()
        if (
            _is_linklike(path, current)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or (applied.st_dev, applied.st_ino) != (opened.st_dev, opened.st_ino)
            or (int(applied.st_uid), int(applied.st_gid)) != (uid, gid)
            or (int(current.st_uid), int(current.st_gid)) != (uid, gid)
        ):
            raise BackupError("restore file ownership could not be applied")
        os.fsync(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)
