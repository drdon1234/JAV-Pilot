from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Protocol, Sequence


MANIFEST_FORMAT_VERSION = 3
SUPPORTED_MANIFEST_FORMAT_VERSIONS = frozenset({1, 2, MANIFEST_FORMAT_VERSION})
SERVICE_NAME = "jav-pilot"
DEFAULT_BASE_URL = "http://127.0.0.1:8766"
REQUIRED_MOUNTS = (
    "/app/data",
    "/downloads/jav",
    "/downloads/jav-web",
    "/media/JAV",
)
BROWSER_PROFILE_MOUNT = "/app/browser-profile"
BROWSER_PROFILE_VOLUME = "jav-pilot-browser-profile"
BACKUP_MOUNT = "/app/backups"
MAINTENANCE_LOCK_MOUNT = "/app/maintenance-locks"
HOST_MOUNT_ENV_BY_DESTINATION = {
    "/downloads/jav": "JAV_PILOT_QB_STAGING_HOST_PATH",
    "/downloads/jav-web": "JAV_PILOT_WEB_DOWNLOAD_HOST_PATH",
    "/media/JAV": "JAV_PILOT_LIBRARY_HOST_PATH",
}
PROTECTED_TOP_LEVEL = frozenset({".env", "data", "downloads", "media", "runtime"})
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMMUTABLE_IMAGE_REFERENCE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._:-]*"
    r"(?:/[a-z0-9][a-z0-9._-]*)+)@sha256:(?P<digest>[0-9a-f]{64})$"
)
_SHA_TAG_REFERENCE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._:-]*"
    r"(?:/[a-z0-9][a-z0-9._-]*)*):(?P<tag>[0-9a-f]{40})$"
)
_IMAGE_PLATFORM_RE = re.compile(
    r"^(?P<os>[a-z0-9]+(?:[._-][a-z0-9]+)*)/"
    r"(?P<architecture>[a-z0-9]+(?:[._-][a-z0-9]+)*)"
    r"(?:/(?P<variant>[a-z0-9]+(?:[._-][a-z0-9]+)*))?$"
)
_IMAGE_ARCHIVE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "revision",
        "sha_tag",
        "image_id",
        "platform",
        "archive_filename",
        "archive_sha256",
    }
)
_RELEASE_ID_RE = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{12}$")
_DOCKER_RESOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LOOPBACK_ORIGIN_RE = re.compile(
    r"^http://(?:127\.0\.0\.1|localhost):(?:[1-9][0-9]{0,4})$"
)
_MAX_ARCHIVE_FILES = 20_000
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MAINTENANCE_LOCK_NAME = ".jav-pilot-maintenance.lock"
_RELEASE_JOURNAL_VERSION = 2
_LEGACY_RELEASE_JOURNAL_VERSION = 1
_SOURCE_EXCLUDED_PREFIXES = (
    ".jav-pilot-restore-staging-",
    ".jav-pilot-restore-rollback-",
)
_LEGACY_BROWSER_PAGES = (
    ("/", "\u4f5c\u54c1\u641c\u7d22"),
    ("/downloads", "\u4e0b\u8f7d\u4efb\u52a1"),
    ("/metadata", "\u5143\u6570\u636e\u8865\u5168"),
    ("/sites", "\u7ad9\u70b9\u4e0e\u89e3\u6790"),
    ("/settings", "\u7cfb\u7edf\u8bbe\u7f6e"),
)
_COMPOSE_NETWORK_KEY = "jav-pilot"
_COMPOSE_TMPFS = "/run/jav-pilot-missav:rw,nosuid,nodev,noexec,mode=0700"
_RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_COMPOSE_BUILD_ARGS = frozenset(
    {
        "VCS_REF",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
)
_LEGACY_COMPOSE_BUILD_ARGS = frozenset(
    {
        "VCS_REF",
        "JAV_PILOT_BUILD_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    }
)
_COMPOSE_FORBIDDEN_SERVICE_FIELDS = (
    "cgroup",
    "cgroup_parent",
    "credential_spec",
    "deploy",
    "develop",
    "device_cgroup_rules",
    "devices",
    "dns",
    "dns_opt",
    "dns_search",
    "external_links",
    "gpus",
    "group_add",
    "ipc",
    "isolation",
    "links",
    "network_mode",
    "oom_kill_disable",
    "pid",
    "post_start",
    "pre_stop",
    "privileged",
    "runtime",
    "sysctls",
    "use_api_socket",
    "userns_mode",
    "uts",
    "volumes_from",
)
_COMPOSE_FORBIDDEN_BUILD_FIELDS = (
    "additional_contexts",
    "cache_to",
    "dockerfile_inline",
    "entitlements",
    "extra_hosts",
    "isolation",
    "output",
    "outputs",
    "privileged",
    "secrets",
    "ssh",
    "tags",
)


class NasReleaseError(RuntimeError):
    pass


class DeploymentBusyError(NasReleaseError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class ImageDeploymentSource:
    kind: str
    registry_digest_reference: str | None
    compose_reference: str
    platform: str
    expected_image_id: str | None = None
    archive_path: Path | None = None
    archive_filename: str | None = None
    archive_sha256: str | None = None


@dataclass(frozen=True)
class ComposeValidation:
    rendered_sha256: str
    service_user: str


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class SystemRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        process_environment = os.environ.copy()
        if env:
            process_environment.update(env)
        completed = subprocess.run(
            [str(item) for item in argv],
            cwd=cwd,
            env=process_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode != 0:
            executable = Path(str(argv[0])).name if argv else "command"
            raise NasReleaseError(
                f"{executable} failed with status {result.returncode}"
            )
        return result


class BackupService(Protocol):
    def create_and_verify(
        self,
        app_root: Path,
        backup_root: Path,
        *,
        revision: str,
        image_id: str,
        release_source: Path,
        include_paths: Sequence[str],
    ) -> Path: ...

    def restore(
        self,
        backup_path: Path,
        app_root: Path,
        *,
        image_id: str,
        release_source: Path,
    ) -> None: ...


class ContainerBackupService:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def create_and_verify(
        self,
        app_root: Path,
        backup_root: Path,
        *,
        revision: str,
        image_id: str,
        release_source: Path,
        include_paths: Sequence[str],
    ) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        script = (
            "import json,sys; from pathlib import Path; "
            "sys.path.insert(0, '/release'); "
            "from jav_pilot.backup_restore import create_backup, verify_backup; "
            "path=create_backup(Path('/target'),Path('/backups'),revision=sys.argv[1],"
            "snapshot_kind='deployment',maintenance_lock_held=True,"
            "include_paths=json.loads(sys.argv[2])); "
            "verify_backup(path); print(json.dumps({'name':path.name}))"
        )
        result = self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                "-v",
                f"{release_source}:/release:ro",
                "-v",
                f"{app_root}:/target",
                "-v",
                f"{backup_root}:/backups",
                image_id,
                "-c",
                script,
                revision,
                json.dumps(list(include_paths)),
            ]
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("backup tool returned invalid JSON") from exc
        name = str(payload.get("name") or "") if isinstance(payload, dict) else ""
        if not name or name != Path(name).name:
            raise NasReleaseError("backup tool returned an unsafe path")
        backup = backup_root / name
        if not backup.is_dir() or backup.is_symlink():
            raise NasReleaseError("verified backup is unavailable on the host")
        return backup

    def restore(
        self,
        backup_path: Path,
        app_root: Path,
        *,
        image_id: str,
        release_source: Path,
    ) -> None:
        script = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, '/release'); "
            "from jav_pilot.backup_restore import verify_backup, restore_backup; "
            "backup=Path('/backups')/sys.argv[1]; verify_backup(backup); "
            "restore_backup(backup,Path('/target'),dry_run=False,"
            "maintenance_lock_held=True)"
        )
        self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                "-v",
                f"{release_source}:/release:ro",
                "-v",
                f"{app_root}:/target",
                "-v",
                f"{backup_path.parent}:/backups",
                image_id,
                "-c",
                script,
                backup_path.name,
            ]
        )


class DeploymentLock(AbstractContextManager["DeploymentLock"]):
    def __init__(self, path: Path, *, shared: bool = False) -> None:
        self.path = path
        self.shared = shared
        self.descriptor = -1

    def __enter__(self) -> "DeploymentLock":
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - remote tool is Linux-only.
            raise NasReleaseError("remote deployment requires POSIX flock") from exc
        if not self.shared:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_before = self.path.parent.lstat()
        if self.path.parent.is_symlink() or not stat.S_ISDIR(parent_before.st_mode):
            raise NasReleaseError("release lock root is unsafe")
        if self.shared and stat.S_IMODE(parent_before.st_mode) & 0o007:
            raise NasReleaseError("shared release lock root must exclude other users")
        flags = (
            os.O_RDWR
            | (0 if self.shared else os.O_CREAT)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise NasReleaseError("release lock is not a regular file")
            # Only provisioning may change the shared lock inode metadata.
            if not self.shared:
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    os.fchown(
                        descriptor,
                        int(parent_before.st_uid),
                        int(parent_before.st_gid),
                    )
                elif int(opened.st_uid) == int(parent_before.st_uid) and int(
                    opened.st_gid
                ) != int(parent_before.st_gid):
                    os.fchown(descriptor, -1, int(parent_before.st_gid))
                os.fchmod(descriptor, 0o600)
            current = self.path.lstat()
            parent_after = self.path.parent.lstat()
            if (
                self.path.is_symlink()
                or self.path.parent.is_symlink()
                or not stat.S_ISDIR(parent_after.st_mode)
                or (parent_after.st_dev, parent_after.st_ino)
                != (parent_before.st_dev, parent_before.st_ino)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or stat.S_IMODE(current.st_mode) != (0o660 if self.shared else 0o600)
                or stat.S_IMODE(os.fstat(descriptor).st_mode)
                != (0o660 if self.shared else 0o600)
                or (int(current.st_uid), int(current.st_gid))
                != (int(parent_before.st_uid), int(parent_before.st_gid))
            ):
                raise NasReleaseError("release lock identity is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            after = self.path.lstat()
            if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                raise NasReleaseError("release lock identity changed")
            self.descriptor = descriptor
        except BlockingIOError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise DeploymentBusyError(
                "another deployment holds the release lock"
            ) from exc
        except BaseException:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        return self

    def __exit__(self, *_args: object) -> bool:
        if self.descriptor >= 0:
            import fcntl

            descriptor = self.descriptor
            self.descriptor = -1
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        return False


@dataclass(frozen=True)
class ReleasePaths:
    app_root: Path
    backup_root: Path
    state_root: Path

    @classmethod
    def validated(
        cls,
        app_root: Path,
        backup_root: Path,
        state_root: Path,
    ) -> "ReleasePaths":
        app = _directory(app_root, "application root", create=False)
        backup = _directory(backup_root, "backup root", create=True)
        state = _directory(state_root, "release state root", create=True)
        roots = (("application", app), ("backup", backup), ("release state", state))
        for index, (left_label, left) in enumerate(roots):
            for right_label, right in roots[index + 1 :]:
                if _paths_overlap(left, right):
                    raise NasReleaseError(
                        f"{left_label} and {right_label} roots must be independent"
                    )
        return cls(app, backup, state)


class NasReleaseManager:
    def __init__(
        self,
        paths: ReleasePaths,
        *,
        runner: Runner | None = None,
        backup_service: BackupService | None = None,
        lock_factory: Callable[[Path], AbstractContextManager[object]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        http_json: Callable[[str], dict[str, object]] | None = None,
        web_download_queue_control: (
            Callable[[str | None], Mapping[str, object]] | None
        ) = None,
        compose_service: str = SERVICE_NAME,
        container_name: str = SERVICE_NAME,
        browser_profile_volume: str = BROWSER_PROFILE_VOLUME,
        base_url: str = DEFAULT_BASE_URL,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths
        self.runner = runner or SystemRunner()
        self.backup_service = backup_service or ContainerBackupService(self.runner)
        self.lock_factory = lock_factory or (
            lambda path: DeploymentLock(path, shared=True)
        )
        self.now = now
        self.sleep = sleep
        self.http_json = http_json or _http_json
        self.web_download_queue_control = (
            web_download_queue_control or self._container_web_download_queue_control
        )
        self.compose_service = _docker_resource_name(compose_service, "Compose service")
        self.container_name = _docker_resource_name(container_name, "container")
        self.browser_profile_volume = _docker_resource_name(
            browser_profile_volume,
            "browser profile volume",
        )
        self.base_url = _loopback_origin(base_url)
        self.fault_hook = fault_hook or (lambda _point: None)

    def deploy(
        self,
        archive_path: Path,
        *,
        revision: object,
        archive_sha256: object,
        image_reference: object | None = None,
        image_platform: object | None = None,
        image_archive_path: Path | None = None,
        image_manifest_path: Path | None = None,
        image_archive_filename: object | None = None,
        apply: bool = False,
    ) -> dict[str, object]:
        clean_revision = _revision(revision)
        clean_archive_hash = _sha256_value(archive_sha256, "archive SHA-256")
        image_source = _deployment_image_source(
            revision=clean_revision,
            image_reference=image_reference,
            image_platform=image_platform,
            image_archive_path=image_archive_path,
            image_manifest_path=image_manifest_path,
            image_archive_filename=image_archive_filename,
        )
        release_id = f"{clean_revision[:12]}-{clean_archive_hash[:12]}"
        with self._release_locks():
            self._prepare_release_operation(apply=apply)
            with tempfile.TemporaryDirectory(
                prefix=f".{release_id}-",
                dir=self.paths.state_root,
            ) as tmp:
                staging = Path(tmp) / "release"
                _extract_release_archive(
                    archive_path,
                    staging,
                    expected_sha256=clean_archive_hash,
                )
                compose = _required_release_file(staging, "docker-compose.yml")
                compose_hash = _sha256(compose)
                source_tree_hash = _source_tree_digest(staging)
                current = self._load_current_manifest()
                same_source_release = bool(
                    current
                    and current.get("revision") == clean_revision
                    and current.get("archive_sha256") == clean_archive_hash
                )
                image_identity_matches = bool(
                    current
                    and current.get("image_source") == image_source.kind
                    and (
                        (
                            image_source.kind == "pull"
                            and current.get("registry_digest_reference")
                            == image_source.registry_digest_reference
                        )
                        or (
                            image_source.kind == "archive"
                            and current.get("image_archive_sha256")
                            == image_source.archive_sha256
                            and current.get("image_id")
                            == image_source.expected_image_id
                            and current.get("image_reference")
                            == image_source.compose_reference
                            and current.get("image_platform") == image_source.platform
                        )
                    )
                )
                if same_source_release and not image_identity_matches:
                    identity_digest = hashlib.sha256(
                        "\0".join(
                            (
                                clean_archive_hash,
                                image_source.kind,
                                str(image_source.registry_digest_reference or ""),
                                image_source.compose_reference,
                                str(image_source.expected_image_id or ""),
                                str(image_source.archive_sha256 or ""),
                                image_source.platform,
                            )
                        ).encode("utf-8")
                    ).hexdigest()
                    release_id = f"{clean_revision[:12]}-{identity_digest[:12]}"
                if not (same_source_release and image_identity_matches):
                    release_id = self._available_release_occurrence_id(
                        release_id,
                        current_release_id=(
                            current.get("release_id") if current else None
                        ),
                    )
                previous_container = self._container_state(required=True)
                previous_mounts = _mount_map(previous_container)
                environment = _release_environment(
                    clean_revision,
                    mounts=previous_mounts,
                    backup_root=self.paths.backup_root,
                    image_reference=image_source.compose_reference,
                )
                target_compose = self._validate_compose(staging, environment)
                if same_source_release and image_identity_matches:
                    acceptance = self._acceptance(
                        clean_revision,
                        expected_mounts=current.get("mounts"),
                        expected_container_user=target_compose.service_user,
                        expected_image_id=current.get("image_id"),
                        expected_image_digest=current.get("runtime_image_digest")
                        or current.get("image_digest"),
                        expected_oci_revision=current.get("revision"),
                        expected_image_reference=current.get("image_reference"),
                        expected_image_platform=current.get("image_platform"),
                    )
                    if acceptance["ok"]:
                        current_release_id = _release_id(current.get("release_id"))
                        return {
                            "ok": True,
                            "dry_run": not apply,
                            "idempotent": True,
                            "release_id": current_release_id,
                            "manifest": str(self._manifest_path(current_release_id)),
                        }
                previous_revision = _container_revision(previous_container)
                previous_image_id = str(previous_container.get("Image") or "")
                previous_image_metadata = self._image_metadata(
                    previous_image_id,
                    expected_digest=(
                        current.get("runtime_image_digest")
                        or current.get("image_digest")
                        if current
                        else None
                    ),
                )
                if previous_image_metadata["revision"] != previous_revision:
                    raise NasReleaseError(
                        "running image revision label does not match the container"
                    )
                previous_image_reference = _container_image_reference(
                    previous_container
                )
                previous_image_repository = _image_repository(previous_image_reference)
                previous_immutable_reference = (
                    previous_image_reference
                    if _is_immutable_image_reference(previous_image_reference)
                    else None
                )
                previous_image_tag = f"rollback-{previous_revision}"
                previous_contract = self._container_schema_contract()
                target_contract = self._image_schema_contract(previous_image_id, source=staging)
                backup_selection = _deployment_backup_paths(
                    self.paths.app_root, previous_contract, target_contract,
                    database_schema_reader=self._container_database_components,
                )
                previous_environment = _release_environment(
                    previous_revision,
                    mounts=previous_mounts,
                    backup_root=self.paths.backup_root,
                    image_reference=previous_immutable_reference,
                    image_tag=(
                        None
                        if previous_immutable_reference is not None
                        else _image_reference_parts(previous_image_reference)[1]
                    ),
                    image_repository=previous_image_repository,
                )
                previous_compose_hash = _sha256(
                    _required_release_file(self.paths.app_root, "docker-compose.yml")
                )
                previous_allows_legacy_topology = (
                    not current or _allows_legacy_manifest_acceptance(current)
                )
                previous_allows_legacy_ports = (
                    not current or _allows_legacy_port_acceptance(current)
                )
                previous_compose = self._validate_compose(
                    self.paths.app_root,
                    previous_environment,
                    recorded_mounts=previous_mounts,
                    allow_legacy_topology=previous_allows_legacy_topology,
                    allow_legacy_ports=previous_allows_legacy_ports,
                )
                plan = {
                    "release_id": release_id,
                    "revision": clean_revision,
                    "archive_sha256": clean_archive_hash,
                    "compose_sha256": compose_hash,
                    "rendered_compose_sha256": target_compose.rendered_sha256,
                    "compose_service_user": target_compose.service_user,
                    "source_tree_sha256": source_tree_hash,
                    "image_source": image_source.kind,
                    "registry_digest_reference": (
                        image_source.registry_digest_reference
                    ),
                    "image_reference": image_source.compose_reference,
                    "image_platform": image_source.platform,
                    "image_archive_sha256": image_source.archive_sha256,
                    "previous_revision": previous_revision,
                    "will_bootstrap_previous": not bool(current),
                    "will_stop": True,
                    "will_backup": bool(backup_selection),
                    "backup_paths": list(backup_selection),
                    "backup_reason": "persistent_schema_change" if backup_selection else "schema_unchanged",
                    "protected_paths": sorted(PROTECTED_TOP_LEVEL),
                }
                if not apply:
                    return {"ok": True, "dry_run": True, "plan": plan}

                prior_queue_paused = self._web_download_queue_paused()
                source_snapshot = self.paths.state_root / "sources" / release_id
                if source_snapshot.exists():
                    raise NasReleaseError("release source snapshot already exists")
                source_snapshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                recovery_tool_source = (
                    self.paths.state_root / "recovery-tools" / release_id / "source"
                )
                if (
                    recovery_tool_source.parent.exists()
                    or recovery_tool_source.parent.is_symlink()
                ):
                    raise NasReleaseError(
                        "release recovery tool artifact already exists"
                    )
                backup_path: Path | None = None
                source_saved = False
                stopped = False
                replacement_attempted = False
                deployed_contract: dict[str, object] | None = None
                journal: dict[str, object] = {
                    "journal_version": _RELEASE_JOURNAL_VERSION,
                    "operation": "deploy",
                    "status": "initialized",
                    "release_id": release_id,
                    "source_snapshot": str(source_snapshot),
                    "recovery_tool_source": str(recovery_tool_source),
                    "recovery_tool_sha256": source_tree_hash,
                    "backup_path": None,
                    "backup_selection": list(backup_selection),
                    "target_schema_contract": target_contract,
                    "target_image": {
                        "revision": clean_revision,
                        "source": image_source.kind,
                        "registry_digest_reference": (
                            image_source.registry_digest_reference
                        ),
                        "compose_reference": image_source.compose_reference,
                        "platform": image_source.platform,
                        "expected_image_id": image_source.expected_image_id,
                        "archive_sha256": image_source.archive_sha256,
                        "compose_service_user": target_compose.service_user,
                    },
                    "previous": {
                        "release_id": current.get("release_id") if current else None,
                        "revision": previous_revision,
                        "schema_contract": previous_contract,
                        "image_id": previous_image_id,
                        "image_digest": previous_image_metadata["digest"],
                        "image_repository": previous_image_repository,
                        "image_tag": previous_image_tag,
                        "image_reference": previous_immutable_reference,
                        "mounts": previous_mounts,
                        "compose_service_user": previous_compose.service_user,
                        "allow_legacy_topology": previous_allows_legacy_topology,
                        "allow_legacy_ports": previous_allows_legacy_ports,
                    },
                    "web_download_queue": {
                        "prior_global_paused": prior_queue_paused,
                    },
                    "manifest": None,
                }
                try:
                    self._write_release_journal(journal)
                    self._release_fault("after_deploy_journal")
                    self._set_web_download_queue_paused(True)
                    self._advance_release_journal(journal, "queue_quiesced")
                    self._release_fault("after_queue_quiesced")
                    recovery_tool_hash = _publish_source_snapshot(
                        staging,
                        recovery_tool_source,
                    )
                    if recovery_tool_hash != source_tree_hash:
                        raise NasReleaseError(
                            "release recovery tool artifact checksum mismatch"
                        )
                    self._validated_deploy_recovery_tool(journal)
                    self._advance_release_journal(journal, "tool_published")
                    self._release_fault("after_recovery_tool_publication")
                    self._advance_release_journal(journal, "stopping")
                    stopped = True
                    self._stop()
                    self._advance_release_journal(journal, "stopped")
                    recovery_tool_source = self._validated_deploy_recovery_tool(journal)
                    if backup_selection:
                        backup_path = self.backup_service.create_and_verify(
                            self.paths.app_root,
                            self.paths.backup_root,
                            revision=previous_revision,
                            image_id=previous_image_id,
                            release_source=recovery_tool_source,
                            include_paths=backup_selection,
                        )
                    journal["backup_path"] = str(backup_path) if backup_path is not None else None
                    self._advance_release_journal(journal, "prepared")
                    self.runner.run(
                        [
                            "docker",
                            "image",
                            "tag",
                            previous_image_id,
                            f"{previous_image_repository}:{previous_image_tag}",
                        ]
                    )
                    self._advance_release_journal(journal, "image_materializing")
                    target_image_metadata = self._materialize_target_image(
                        image_source,
                        revision=clean_revision,
                    )
                    image_contract = self._image_schema_contract(target_image_metadata["image_id"])
                    if _contract_versions(image_contract) != _contract_versions(target_contract):
                        raise NasReleaseError("target image schema contract does not match the release source")
                    target_journal = journal["target_image"]
                    assert isinstance(target_journal, dict)
                    target_journal["image_id"] = target_image_metadata["image_id"]
                    target_journal["runtime_digest"] = target_image_metadata["digest"]
                    self._advance_release_journal(journal, "image_ready")
                    previous_source_tree_hash = _publish_source_snapshot(
                        self.paths.app_root,
                        source_snapshot,
                    )
                    source_saved = True
                    self._advance_release_journal(journal, "source_published")
                    if not current:
                        baseline_seed = (
                            f"{previous_revision[:12]}-{previous_source_tree_hash[:12]}"
                        )
                        baseline_manifest: dict[str, object] = {
                            "format_version": MANIFEST_FORMAT_VERSION,
                            "release_id": baseline_seed,
                            "created_at": str(
                                previous_container.get("Created")
                                or self.now().astimezone(UTC).isoformat()
                            ),
                            "revision": previous_revision,
                            "archive_sha256": previous_source_tree_hash,
                            "compose_sha256": previous_compose_hash,
                            "rendered_compose_sha256": (
                                previous_compose.rendered_sha256
                            ),
                            "compose_service_user": previous_compose.service_user,
                            "compose_port_topology": "legacy_single_bind",
                            "source_tree_sha256": previous_source_tree_hash,
                            "image_source": "legacy",
                            "image_identity": {
                                "kind": "legacy_image",
                                "image_id": previous_image_id,
                                "revision": previous_revision,
                            },
                            "registry_digest_reference": (previous_immutable_reference),
                            "image_reference": previous_immutable_reference,
                            "image_platform": previous_image_metadata.get("platform"),
                            "image_archive_sha256": None,
                            "image_id": previous_image_id,
                            "image_digest": previous_image_metadata["digest"],
                            "runtime_image_digest": previous_image_metadata["digest"],
                            "image_repository": previous_image_repository,
                            "image_tag": previous_image_tag,
                            "schema_contract": previous_contract,
                            "compatibility": {
                                "bootstrap_predecessor": True,
                            },
                            "backup_path": None,
                            "mounts": previous_mounts,
                            "acceptance": {
                                "bootstrap_predecessor": True,
                                "revision": previous_revision,
                            },
                            "previous": {"release_id": None},
                        }
                        baseline_id = self._available_release_occurrence_id(
                            baseline_seed,
                            current_release_id=None,
                            reusable_manifest=baseline_manifest,
                        )
                        baseline_manifest["release_id"] = baseline_id
                        self._publish_manifest_file(baseline_manifest)
                        journal_previous = journal["previous"]
                        assert isinstance(journal_previous, dict)
                        journal_previous["release_id"] = baseline_id
                        self._write_release_journal(journal)
                        self._write_current_pointer(baseline_manifest)
                        current = baseline_manifest
                    self._release_fault("after_source_snapshot")
                    self._advance_release_journal(journal, "replacing_source")
                    _sync_source_tree(staging, self.paths.app_root)
                    if _source_tree_digest(self.paths.app_root) != source_tree_hash:
                        raise NasReleaseError("deployed source tree digest mismatch")
                    self._advance_release_journal(journal, "source_replaced")
                    deployed_compose = self._validate_compose(
                        self.paths.app_root,
                        environment,
                    )
                    if deployed_compose != target_compose:
                        raise NasReleaseError("deployed Compose rendering changed")
                    self._pause_persisted_web_download_queue()
                    replacement_attempted = True
                    self._advance_release_journal(journal, "target_starting")
                    self._compose(
                        [
                            "up",
                            "-d",
                            "--no-build",
                            "--pull",
                            "never",
                            "--force-recreate",
                        ],
                        environment=environment,
                    )
                    self._advance_release_journal(journal, "target_started")
                    deployed_contract = self._container_schema_contract()
                    container = self._container_state(required=True)
                    image_id = str(container.get("Image") or "")
                    image_metadata = self._image_metadata(
                        image_id,
                        expected_digest=target_image_metadata["digest"],
                    )
                    acceptance = self._acceptance(
                        clean_revision,
                        expected_mounts={
                            **previous_mounts,
                            MAINTENANCE_LOCK_MOUNT: str(self._maintenance_lock_root()),
                        },
                        expected_container_user=deployed_compose.service_user,
                        expected_image_id=target_image_metadata["image_id"],
                        expected_image_digest=target_image_metadata["digest"],
                        expected_oci_revision=clean_revision,
                        expected_image_reference=image_source.compose_reference,
                        expected_image_platform=image_source.platform,
                    )
                    if not acceptance["ok"]:
                        raise NasReleaseError("deployment acceptance failed")
                    image_digest = str(image_metadata["digest"])
                    schema_contract = deployed_contract
                    image_repository = _image_repository(
                        _container_image_reference(container)
                    )
                    rollback_requires_data_restore = not _contract_can_read(
                        previous_contract,
                        _contract_versions(schema_contract),
                    )
                    image_identity = (
                        {
                            "kind": "registry_digest",
                            "registry_digest_reference": (
                                image_source.registry_digest_reference
                            ),
                        }
                        if image_source.kind == "pull"
                        else {
                            "kind": "archive",
                            "archive_sha256": image_source.archive_sha256,
                            "image_id": image_id,
                            "revision": clean_revision,
                            "platform": image_source.platform,
                            "sha_tag": image_source.compose_reference,
                        }
                    )
                    manifest = {
                        "format_version": MANIFEST_FORMAT_VERSION,
                        "release_id": release_id,
                        "created_at": self.now().astimezone(UTC).isoformat(),
                        "revision": clean_revision,
                        "archive_sha256": clean_archive_hash,
                        "compose_sha256": compose_hash,
                        "rendered_compose_sha256": (
                            deployed_compose.rendered_sha256
                        ),
                        "compose_service_user": deployed_compose.service_user,
                        "compose_port_topology": "dual_bind",
                        "source_tree_sha256": source_tree_hash,
                        "image_source": image_source.kind,
                        "image_identity": image_identity,
                        "registry_digest_reference": (
                            image_source.registry_digest_reference
                        ),
                        "image_reference": image_source.compose_reference,
                        "image_platform": image_source.platform,
                        "image_archive_sha256": image_source.archive_sha256,
                        "image_id": image_id,
                        "image_digest": image_digest,
                        "runtime_image_digest": image_digest,
                        "image_repository": image_repository,
                        "image_tag": clean_revision,
                        "schema_contract": schema_contract,
                        "compatibility": {
                            "previous_can_read_current_data": (
                                not rollback_requires_data_restore
                            ),
                            "rollback_requires_data_restore": (
                                rollback_requires_data_restore
                            ),
                        },
                        "backup_path": str(backup_path) if backup_path is not None else None,
                        "mounts": acceptance["mounts"],
                        "acceptance": acceptance,
                        "previous": {
                            "release_id": current.get("release_id")
                            if current
                            else None,
                            "revision": previous_revision,
                            "image_id": previous_image_id,
                            "image_repository": previous_image_repository,
                            "image_tag": previous_image_tag,
                            "image_reference": previous_immutable_reference,
                            "image_digest": previous_image_metadata["digest"],
                            "image_platform": previous_image_metadata.get("platform"),
                            "source_snapshot": str(source_snapshot),
                            "source_tree_sha256": previous_source_tree_hash,
                            "compose_sha256": previous_compose_hash,
                            "rendered_compose_sha256": (
                                previous_compose.rendered_sha256
                            ),
                            "compose_service_user": previous_compose.service_user,
                            "schema_contract": previous_contract,
                        },
                    }
                    journal["manifest"] = manifest
                    self._advance_release_journal(journal, "accepted")
                    self._release_fault("after_runtime_acceptance")
                    manifest_path = self._publish_manifest_file(manifest)
                    self._advance_release_journal(journal, "manifest_published")
                    self._release_fault("after_manifest_publication")
                    self._write_current_pointer(manifest)
                    self._advance_release_journal(journal, "current_published")
                    self._release_fault("after_current_publication")
                    self._advance_release_journal(journal, "queue_restoring")
                    self._restore_web_download_queue(journal)
                    self._advance_release_journal(journal, "queue_restored")
                    self._release_fault("after_queue_restore")
                    self._cleanup_deploy_recovery_tool(journal)
                    self._clear_release_journal()
                    return {
                        "ok": True,
                        "dry_run": False,
                        "idempotent": False,
                        "release_id": release_id,
                        "manifest": str(manifest_path),
                        "backup": str(backup_path) if backup_path is not None else None,
                    }
                except BaseException:
                    if journal is not None and journal.get("status") in {
                        "accepted",
                        "manifest_published",
                        "current_published",
                        "queue_restoring",
                        "queue_restored",
                    }:
                        self._recover_release_journal()
                        recovered = self._load_current_manifest()
                        if recovered.get("release_id") == release_id:
                            return {
                                "ok": True,
                                "dry_run": False,
                                "idempotent": False,
                                "release_id": release_id,
                                "manifest": str(self._manifest_path(release_id)),
                                "backup": str(backup_path) if backup_path is not None else None,
                                "recovered_publication": True,
                            }
                        raise
                    try:
                        if stopped and replacement_attempted:
                            self._stop()
                        if source_saved:
                            _sync_source_tree(source_snapshot, self.paths.app_root)
                        requires_data_restore = replacement_attempted and (
                            deployed_contract is None
                            or not _contract_can_read(
                                previous_contract,
                                _contract_versions(deployed_contract),
                            )
                        )
                        if backup_path is not None and requires_data_restore:
                            recovery_tool_source = self._validated_deploy_recovery_tool(
                                journal
                            )
                            self.backup_service.restore(
                                backup_path,
                                self.paths.app_root,
                                image_id=previous_image_id,
                                release_source=recovery_tool_source,
                            )
                        recovery_container_user = previous_compose.service_user
                        if stopped:
                            self._pause_persisted_web_download_queue()
                            if replacement_attempted:
                                recovery_environment = _release_environment(
                                    previous_revision,
                                    mounts=previous_mounts,
                                    backup_root=self.paths.backup_root,
                                    image_reference=previous_immutable_reference,
                                    image_tag=(
                                        None
                                        if previous_immutable_reference is not None
                                        else previous_image_tag
                                    ),
                                    image_repository=previous_image_repository,
                                )
                                recovery_compose = self._validate_compose(
                                    self.paths.app_root,
                                    recovery_environment,
                                    recorded_mounts=previous_mounts,
                                    allow_legacy_topology=(
                                        previous_allows_legacy_topology
                                    ),
                                    allow_legacy_ports=previous_allows_legacy_ports,
                                )
                                if (
                                    recovery_compose.service_user
                                    != previous_compose.service_user
                                ):
                                    raise NasReleaseError(
                                        "rollback Compose service user changed"
                                    )
                                recovery_container_user = (
                                    recovery_compose.service_user
                                )
                                self._compose(
                                    [
                                        "up",
                                        "-d",
                                        "--no-build",
                                        "--pull",
                                        "never",
                                        "--force-recreate",
                                    ],
                                    environment=recovery_environment,
                                )
                            else:
                                self.runner.run(
                                    ["docker", "start", self.container_name]
                                )
                            recovery = self._acceptance(
                                previous_revision,
                                expected_mounts=previous_mounts,
                                expected_container_user=recovery_container_user,
                                expected_image_id=previous_image_id,
                                expected_image_digest=previous_image_metadata["digest"],
                                expected_oci_revision=previous_revision,
                                allow_legacy_runtime=True,
                            )
                            if not recovery["ok"]:
                                raise NasReleaseError(
                                    "automatic rollback acceptance failed"
                                )
                        self._restore_web_download_queue(journal)
                        if source_saved:
                            shutil.rmtree(source_snapshot)
                            _fsync_directory(source_snapshot.parent)
                        self._cleanup_deploy_recovery_tool(journal)
                        self._clear_release_journal()
                    except BaseException as rollback_error:
                        raise NasReleaseError(
                            "deployment failed and automatic rollback did not complete"
                        ) from rollback_error
                    raise

    def rollback(self, release_id: object, *, apply: bool = False) -> dict[str, object]:
        target_id = _release_id(release_id)
        with self._release_locks():
            self._prepare_release_operation(apply=apply)
            current = self._load_current_manifest(required=True)
            target = self._load_manifest(target_id)
            previous = current.get("previous")
            if (
                not isinstance(previous, dict)
                or previous.get("release_id") != target_id
            ):
                raise NasReleaseError("rollback target is not the previous release")
            target_revision = _revision(target.get("revision"))
            if _revision(previous.get("revision")) != target_revision:
                raise NasReleaseError(
                    "rollback previous revision does not match target"
                )
            target_manifest_image = str(target.get("image_id") or "")
            previous_image = str(previous.get("image_id") or "")
            if target_manifest_image and previous_image != target_manifest_image:
                raise NasReleaseError("rollback previous image does not match target")
            source_snapshot = _safe_existing_directory(
                previous.get("source_snapshot"),
                self.paths.state_root,
                "rollback source snapshot",
            )
            expected_source_hash = _sha256_value(
                previous.get("source_tree_sha256"),
                "rollback source tree SHA-256",
            )
            if _source_tree_digest(source_snapshot) != expected_source_hash:
                raise NasReleaseError("rollback source snapshot checksum mismatch")
            snapshot_compose_hash = _sha256(
                _required_release_file(source_snapshot, "docker-compose.yml")
            )
            expected_compose_hash = _sha256_value(
                previous.get("compose_sha256"),
                "rollback Compose SHA-256",
            )
            if snapshot_compose_hash != expected_compose_hash:
                raise NasReleaseError("rollback source Compose checksum mismatch")
            target_compose_hash = target.get("compose_sha256")
            if (
                target_compose_hash is not None
                and _sha256_value(target_compose_hash, "target Compose SHA-256")
                != snapshot_compose_hash
            ):
                raise NasReleaseError("rollback target Compose checksum mismatch")
            current_revision = _revision(current.get("revision"))
            current_container = self._container_state(required=True)
            if _container_revision(current_container) != current_revision:
                raise NasReleaseError("running revision does not match release state")
            current_image = str(current_container.get("Image") or "")
            expected_current_image = str(current.get("image_id") or "")
            if not current_image or (
                expected_current_image and current_image != expected_current_image
            ):
                raise NasReleaseError("running image does not match release state")
            current_image_metadata = self._image_metadata(
                current_image,
                expected_digest=current.get("runtime_image_digest")
                or current.get("image_digest"),
            )
            expected_current_digest = current.get("image_digest")
            if (
                expected_current_digest is not None
                and current_image_metadata["digest"] != expected_current_digest
            ):
                raise NasReleaseError(
                    "running image digest does not match release state"
                )
            if current_image_metadata["revision"] != current_revision:
                raise NasReleaseError(
                    "running image label does not match release state"
                )
            current_contract = self._container_schema_contract()
            requires_data_restore = not _contract_can_read(
                target.get("schema_contract"),
                _contract_versions(current_contract),
            )
            backup_path = (
                _safe_existing_directory(current.get("backup_path"), self.paths.backup_root, "rollback backup")
                if requires_data_restore else None
            )
            recovery_selection = _deployment_backup_paths(
                self.paths.app_root, current_contract, target.get("schema_contract"),
                database_schema_reader=self._container_database_components,
            ) if requires_data_restore else ()
            target_image = str(target.get("image_id") or previous.get("image_id") or "")
            target_image_metadata = self._image_metadata(
                target_image,
                expected_digest=target.get("runtime_image_digest")
                or target.get("image_digest"),
            )
            expected_target_digest = target.get("image_digest")
            if (
                expected_target_digest is not None
                and target_image_metadata["digest"] != expected_target_digest
            ):
                raise NasReleaseError("rollback image digest does not match target")
            if target_image_metadata["revision"] != target_revision:
                raise NasReleaseError("rollback image label does not match target")
            target_tag = str(target.get("image_tag") or target_revision)
            target_repository = str(
                target.get("image_repository") or previous.get("image_repository") or ""
            )
            target_reference = target.get("image_reference")
            target_mounts = target.get("mounts")
            environment = _release_environment(
                target_revision,
                mounts=target_mounts,
                backup_root=self.paths.backup_root,
                image_reference=(
                    str(target_reference) if target_reference is not None else None
                ),
                image_tag=None if target_reference is not None else target_tag,
                image_repository=target_repository,
            )
            target_compose = self._validate_compose(
                source_snapshot,
                environment,
                recorded_mounts=target_mounts,
                allow_legacy_topology=_allows_legacy_manifest_acceptance(target),
                allow_legacy_ports=_allows_legacy_port_acceptance(target),
            )
            plan = {
                "target_release_id": target_id,
                "target_revision": target.get("revision"),
                "target_image_id": target_image,
                "target_image_reference": target_reference,
                "compose_service_user": target_compose.service_user,
                "requires_data_restore": requires_data_restore,
                "will_backup_current_data": bool(recovery_selection),
                "backup_paths": list(recovery_selection),
                "source_snapshot": str(source_snapshot),
                "backup_path": str(backup_path) if requires_data_restore else None,
            }
            if not apply:
                return {"ok": True, "dry_run": True, "plan": plan}

            prior_queue_paused = self._web_download_queue_paused()
            current_mounts = _mount_map(current_container)
            current_repository = str(current.get("image_repository") or "")
            current_tag = str(current.get("image_tag") or current_revision)
            current_reference = current.get("image_reference")
            current_environment = _release_environment(
                current_revision,
                mounts=current_mounts,
                backup_root=self.paths.backup_root,
                image_reference=(
                    str(current_reference) if current_reference is not None else None
                ),
                image_tag=None if current_reference is not None else current_tag,
                image_repository=current_repository,
            )
            current_compose = self._validate_compose(
                self.paths.app_root,
                current_environment,
                recorded_mounts=current_mounts,
                allow_legacy_topology=_allows_legacy_manifest_acceptance(current),
                allow_legacy_ports=_allows_legacy_port_acceptance(current),
            )
            with self._rollback_recovery_workspace(target_id) as current_source:
                current_backup: Path | None = None
                stopped = False
                source_saved = False
                replacement_attempted = False
                event_path: Path | None = None
                journal: dict[str, object] = {
                    "journal_version": _RELEASE_JOURNAL_VERSION,
                    "operation": "rollback",
                    "status": "prepared",
                    "release_id": target_id,
                    "source_snapshot": str(source_snapshot),
                    "recovery_source": str(current_source),
                    "recovery_backup": None,
                    "current": current,
                    "target": target,
                    "current_image_digest": current_image_metadata["digest"],
                    "target_image_digest": target_image_metadata["digest"],
                    "current_compose_service_user": current_compose.service_user,
                    "target_compose_service_user": target_compose.service_user,
                    "web_download_queue": {
                        "prior_global_paused": prior_queue_paused,
                    },
                    "event": None,
                    "event_path": None,
                }
                self._write_release_journal(journal)
                self._release_fault("after_rollback_journal")
                try:
                    self._set_web_download_queue_paused(True)
                    self._advance_release_journal(journal, "queue_quiesced")
                    self._release_fault("after_rollback_queue_quiesced")
                    stopped = True
                    self._stop()
                    if recovery_selection:
                        current_backup = self.backup_service.create_and_verify(
                            self.paths.app_root,
                            self.paths.backup_root,
                            revision=current_revision,
                            image_id=current_image,
                            release_source=self.paths.app_root,
                            include_paths=recovery_selection,
                        )
                    journal["recovery_backup"] = str(current_backup) if current_backup is not None else None
                    self._advance_release_journal(journal, "current_backed_up")
                    _copy_source_tree(self.paths.app_root, current_source)
                    source_saved = True
                    self._advance_release_journal(journal, "current_source_saved")
                    self._advance_release_journal(journal, "applying_target")
                    if requires_data_restore:
                        assert backup_path is not None
                        self.backup_service.restore(
                            backup_path,
                            self.paths.app_root,
                            image_id=target_image,
                            release_source=self.paths.app_root,
                        )
                    _sync_source_tree(source_snapshot, self.paths.app_root)
                    deployed_compose = self._validate_compose(
                        self.paths.app_root,
                        environment,
                        recorded_mounts=target_mounts,
                        allow_legacy_topology=(
                            _allows_legacy_manifest_acceptance(target)
                        ),
                        allow_legacy_ports=_allows_legacy_port_acceptance(target),
                    )
                    if deployed_compose != target_compose:
                        raise NasReleaseError("rollback Compose rendering changed")
                    self._pause_persisted_web_download_queue()
                    replacement_attempted = True
                    self._compose(
                        [
                            "up",
                            "-d",
                            "--no-build",
                            "--pull",
                            "never",
                            "--force-recreate",
                        ],
                        environment=environment,
                    )
                    self._advance_release_journal(journal, "target_started")
                    acceptance = self._acceptance(
                        target_revision,
                        expected_mounts=target.get("mounts"),
                        expected_container_user=deployed_compose.service_user,
                        expected_image_id=target_image,
                        expected_image_digest=target_image_metadata["digest"],
                        expected_oci_revision=target_revision,
                        expected_image_reference=target.get("image_reference"),
                        expected_image_platform=target.get("image_platform"),
                        allow_legacy_runtime=_allows_legacy_manifest_acceptance(target),
                    )
                    if not acceptance["ok"]:
                        raise NasReleaseError("rollback acceptance failed")
                    event = {
                        "format_version": 1,
                        "rolled_back_at": self.now().astimezone(UTC).isoformat(),
                        "from_release_id": current.get("release_id"),
                        "to_release_id": target_id,
                        "data_restored": requires_data_restore,
                        "recovery_backup_path": str(current_backup) if current_backup is not None else None,
                        "schema_contract_before": current_contract,
                        "acceptance": acceptance,
                    }
                    event_path = (
                        self.paths.state_root
                        / "rollbacks"
                        / (
                            f"{self.now().astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
                            f"{target_id}-{uuid.uuid4().hex[:12]}.json"
                        )
                    )
                    journal["event"] = event
                    journal["event_path"] = str(event_path)
                    self._advance_release_journal(journal, "accepted")
                    self._release_fault("after_rollback_acceptance")
                    _write_json_exclusive(event_path, event)
                    self._advance_release_journal(journal, "event_published")
                    self._release_fault("after_rollback_event")
                    self._write_current_pointer(target)
                    self._advance_release_journal(journal, "current_published")
                    self._release_fault("after_rollback_pointer")
                    self._advance_release_journal(journal, "queue_restoring")
                    self._restore_web_download_queue(journal)
                    self._advance_release_journal(journal, "queue_restored")
                    self._release_fault("after_rollback_queue_restore")
                    self._clear_release_journal()
                    return {
                        "ok": True,
                        "dry_run": False,
                        "release_id": target_id,
                        "data_restored": requires_data_restore,
                        "recovery_backup": str(current_backup) if current_backup is not None else None,
                        "event": str(event_path),
                    }
                except BaseException:
                    if journal.get("status") in {
                        "accepted",
                        "event_published",
                        "current_published",
                        "queue_restoring",
                        "queue_restored",
                    }:
                        self._recover_release_journal()
                        recovered = self._load_current_manifest(required=True)
                        if recovered.get("release_id") == target_id:
                            return {
                                "ok": True,
                                "dry_run": False,
                                "release_id": target_id,
                                "data_restored": requires_data_restore,
                                "recovery_backup": str(current_backup) if current_backup is not None else None,
                                "event": str(event_path),
                                "recovered_publication": True,
                            }
                        raise
                    try:
                        if stopped and replacement_attempted:
                            self._stop()
                        if source_saved:
                            _sync_source_tree(current_source, self.paths.app_root)
                        if current_backup is not None:
                            self.backup_service.restore(
                                current_backup,
                                self.paths.app_root,
                                image_id=current_image,
                                release_source=(
                                    current_source
                                    if source_saved
                                    else self.paths.app_root
                                ),
                            )
                        recovery_container_user = current_compose.service_user
                        if stopped:
                            self._pause_persisted_web_download_queue()
                            if replacement_attempted:
                                recovery_compose = self._validate_compose(
                                    self.paths.app_root,
                                    current_environment,
                                    recorded_mounts=current_mounts,
                                    allow_legacy_topology=(
                                        _allows_legacy_manifest_acceptance(current)
                                    ),
                                    allow_legacy_ports=_allows_legacy_port_acceptance(
                                        current
                                    ),
                                )
                                if (
                                    recovery_compose.service_user
                                    != current_compose.service_user
                                ):
                                    raise NasReleaseError(
                                        "recovery Compose service user changed"
                                    )
                                recovery_container_user = (
                                    recovery_compose.service_user
                                )
                                self._compose(
                                    [
                                        "up",
                                        "-d",
                                        "--no-build",
                                        "--pull",
                                        "never",
                                        "--force-recreate",
                                    ],
                                    environment=current_environment,
                                )
                            else:
                                self.runner.run(
                                    ["docker", "start", self.container_name]
                                )
                            recovery = self._acceptance(
                                current_revision,
                                expected_mounts=current_mounts,
                                expected_container_user=recovery_container_user,
                                expected_image_id=current_image,
                                expected_image_digest=current_image_metadata["digest"],
                                expected_oci_revision=current_revision,
                            )
                            if not recovery["ok"]:
                                raise NasReleaseError(
                                    "rollback recovery acceptance failed"
                                )
                        self._write_current_pointer(current)
                        self._restore_web_download_queue(journal)
                        if event_path is not None:
                            _durable_unlink_if_present(event_path)
                        self._clear_release_journal()
                    except BaseException as recovery_error:
                        raise NasReleaseError(
                            "rollback failed and current release recovery did not complete"
                        ) from recovery_error
                    raise

    def _validate_compose(
        self,
        source_root: Path,
        environment: dict[str, str],
        *,
        allow_legacy_topology: bool = False,
        allow_legacy_ports: bool = False,
        allow_loopback_lan: bool = False,
        recorded_mounts: Mapping[str, object] | None = None,
    ) -> ComposeValidation:
        compose = _required_release_file(source_root, "docker-compose.yml")
        base = [
            "docker",
            "compose",
            "--project-directory",
            str(self.paths.app_root),
            "--env-file",
            str(self.paths.app_root / ".env"),
            "-f",
            str(compose),
        ]
        compose_environment = self._compose_environment(environment)
        rendered = self.runner.run(
            [*base, "config", "--format", "json"],
            env=compose_environment,
        ).stdout
        if not rendered:
            raise NasReleaseError("docker compose config returned no content")
        try:
            model = json.loads(rendered.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("docker compose config returned invalid JSON") from exc
        service_user = self._validate_compose_model(
            model,
            compose_environment,
            allow_legacy_topology=allow_legacy_topology,
            allow_legacy_ports=allow_legacy_ports,
            allow_loopback_lan=allow_loopback_lan,
            recorded_mounts=recorded_mounts,
        )
        return ComposeValidation(
            rendered_sha256=hashlib.sha256(rendered).hexdigest(),
            service_user=service_user,
        )

    def _validate_compose_model(
        self,
        model: object,
        environment: Mapping[str, str],
        *,
        allow_legacy_topology: bool,
        allow_legacy_ports: bool,
        allow_loopback_lan: bool,
        recorded_mounts: Mapping[str, object] | None = None,
    ) -> str:
        if not isinstance(allow_legacy_topology, bool):
            raise NasReleaseError("legacy Compose topology mode is invalid")
        if not isinstance(allow_legacy_ports, bool):
            raise NasReleaseError("legacy Compose port mode is invalid")
        if not isinstance(allow_loopback_lan, bool):
            raise NasReleaseError("loopback Compose port mode is invalid")
        if not isinstance(model, dict):
            raise NasReleaseError("docker compose model is invalid")
        services = model.get("services")
        if not isinstance(services, dict) or set(services) != {self.compose_service}:
            raise NasReleaseError("docker compose service set is invalid")
        service = services.get(self.compose_service)
        if not isinstance(service, dict):
            raise NasReleaseError("docker compose service is invalid")
        if (
            service.get("configs")
            or service.get("secrets")
            or model.get("configs")
            or model.get("secrets")
        ):
            raise NasReleaseError("docker compose configs or secrets are forbidden")
        if service.get("container_name") != self.container_name:
            raise NasReleaseError("docker compose container name is invalid")
        platform = service.get("platform")
        if platform != "linux/amd64" and not (
            allow_legacy_topology and platform is None
        ):
            raise NasReleaseError("docker compose platform is invalid")
        expected_image = _compose_image_reference(environment)
        if service.get("image") != expected_image:
            raise NasReleaseError("docker compose image reference is invalid")
        if any(
            _compose_value_present(service.get(field))
            for field in _COMPOSE_FORBIDDEN_SERVICE_FIELDS
        ):
            raise NasReleaseError("docker compose service isolation is invalid")
        service_read_only = service.get("read_only")
        if service.get("init") is not True or not (
            service_read_only is None or service_read_only is False
        ):
            raise NasReleaseError("docker compose service isolation is invalid")
        cap_add = service.get("cap_add")
        cap_drop = service.get("cap_drop")
        security_opt = service.get("security_opt")
        if (
            not isinstance(cap_add, list)
            or len(cap_add) != 3
            or any(not isinstance(item, str) for item in cap_add)
            or set(cap_add) != {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
            or cap_drop != ["ALL"]
            or security_opt != ["no-new-privileges:true"]
            or service.get("extra_hosts")
            != ["host.docker.internal=host-gateway"]
        ):
            raise NasReleaseError("docker compose service privileges are invalid")
        user = _compose_service_user(service.get("user"))

        build = service.get("build")
        if not isinstance(build, dict):
            raise NasReleaseError("docker compose build contract is invalid")
        expected_context = str(self.paths.app_root.resolve(strict=True))
        build_args = build.get("args")
        allowed_build_args = (
            {_COMPOSE_BUILD_ARGS, _LEGACY_COMPOSE_BUILD_ARGS}
            if allow_legacy_topology
            else {_COMPOSE_BUILD_ARGS}
        )
        actual_build_args = (
            frozenset(build_args) if isinstance(build_args, dict) else frozenset()
        )
        proxy_args_match = bool(
            isinstance(build_args, dict)
            and actual_build_args == _COMPOSE_BUILD_ARGS
            and all(isinstance(value, str) for value in build_args.values())
            and len(
                {
                    build_args[key]
                    for key in _COMPOSE_BUILD_ARGS
                    if key != "VCS_REF"
                }
            )
            == 1
        )
        if (
            build.get("context") != expected_context
            or build.get("dockerfile") != "Dockerfile"
            or build.get("network") != "host"
            or not isinstance(build_args, dict)
            or actual_build_args not in allowed_build_args
            or build_args.get("VCS_REF")
            != environment.get("JAV_PILOT_REVISION")
            or any(
                not isinstance(value, str)
                or len(value) > 4096
                or any(character in value for character in ("\x00", "\n", "\r"))
                for value in build_args.values()
            )
            or (
                actual_build_args == _COMPOSE_BUILD_ARGS
                and not proxy_args_match
            )
            or any(
                _compose_value_present(build.get(field))
                for field in _COMPOSE_FORBIDDEN_BUILD_FIELDS
            )
        ):
            raise NasReleaseError("docker compose build contract is invalid")

        tmpfs = service.get("tmpfs")
        if tmpfs != [_COMPOSE_TMPFS]:
            raise NasReleaseError("docker compose tmpfs contract is invalid")
        ports = service.get("ports")
        allowed_port_counts = {1, 2} if allow_legacy_ports else {2}
        if not isinstance(ports, list) or len(ports) not in allowed_port_counts:
            raise NasReleaseError("docker compose port contract is invalid")
        legacy_single_port = allow_legacy_ports and len(ports) == 1
        normalized_ports: set[tuple[str, str, int]] = set()
        published_ports: set[str] = set()
        host_ips: list[str] = []
        for port in ports:
            if not isinstance(port, dict):
                raise NasReleaseError("docker compose port contract is invalid")
            host_ip = port.get("host_ip")
            if not isinstance(host_ip, str):
                raise NasReleaseError("docker compose port contract is invalid")
            try:
                parsed_host_ip = ipaddress.ip_address(host_ip)
            except ValueError as exc:
                raise NasReleaseError("docker compose port contract is invalid") from exc
            published = port.get("published")
            if (
                port.get("target") != 8766
                or port.get("protocol") != "tcp"
                or port.get("mode") != "ingress"
                or not isinstance(published, str)
                or not published.isdigit()
                or not 1 <= int(published) <= 65535
            ):
                raise NasReleaseError("docker compose port contract is invalid")
            if legacy_single_port:
                if host_ip not in {"127.0.0.1", "0.0.0.0"}:
                    raise NasReleaseError("docker compose port contract is invalid")
            elif host_ip != "127.0.0.1" and not (
                _is_rfc1918_ipv4(parsed_host_ip)
                or (
                    allow_loopback_lan
                    and parsed_host_ip.version == 4
                    and parsed_host_ip.is_loopback
                )
            ):
                raise NasReleaseError("docker compose port contract is invalid")
            host_ips.append(host_ip)
            published_ports.add(published)
            normalized_ports.add((host_ip, published, int(port["target"])))
        if len(normalized_ports) != len(ports):
            raise NasReleaseError("docker compose port contract is invalid")
        if len(published_ports) != 1:
            raise NasReleaseError("docker compose port contract is invalid")
        if not legacy_single_port:
            rfc1918_count = sum(
                _is_rfc1918_ipv4(ipaddress.ip_address(host_ip))
                for host_ip in host_ips
            )
            loopback_lan_count = sum(
                ipaddress.ip_address(host_ip).version == 4
                and ipaddress.ip_address(host_ip).is_loopback
                and host_ip != "127.0.0.1"
                for host_ip in host_ips
            )
            if host_ips.count("127.0.0.1") != 1 or not (
                rfc1918_count == 1
                or (allow_loopback_lan and loopback_lan_count == 1)
            ):
                raise NasReleaseError("docker compose port contract is invalid")

        service_networks = service.get("networks")
        networks = model.get("networks")
        if (
            not isinstance(service_networks, dict)
            or set(service_networks) != {_COMPOSE_NETWORK_KEY}
            or service_networks[_COMPOSE_NETWORK_KEY] is not None
            or not isinstance(networks, dict)
            or set(networks) != {_COMPOSE_NETWORK_KEY}
        ):
            raise NasReleaseError("docker compose network contract is invalid")
        network = networks[_COMPOSE_NETWORK_KEY]
        network_ipam = network.get("ipam") if isinstance(network, dict) else None
        if (
            not isinstance(network, dict)
            or not set(network) <= {"driver", "external", "ipam", "name"}
            or network.get("driver") != "bridge"
            or network.get("external") not in (None, False)
            or _DOCKER_RESOURCE_RE.fullmatch(str(network.get("name") or "")) is None
            or (network_ipam is not None and network_ipam != {})
        ):
            raise NasReleaseError("docker compose network contract is invalid")

        raw_mounts = service.get("volumes")
        if not isinstance(raw_mounts, list):
            raise NasReleaseError("docker compose mounts are invalid")
        mounts: dict[str, dict[str, object]] = {}
        for raw_mount in raw_mounts:
            if not isinstance(raw_mount, dict):
                raise NasReleaseError("docker compose mount is invalid")
            target = raw_mount.get("target")
            if not isinstance(target, str) or target in mounts:
                raise NasReleaseError("docker compose mount target is invalid")
            mounts[target] = raw_mount

        required_targets = set(REQUIRED_MOUNTS)
        # A new deployment must use the current lock mount. Recovery validates
        # the exact recorded topology instead of modifying the saved release.
        if recorded_mounts is None or MAINTENANCE_LOCK_MOUNT in recorded_mounts:
            required_targets.add(MAINTENANCE_LOCK_MOUNT)
            service_environment = service.get("environment")
            if not isinstance(service_environment, dict) or service_environment.get(
                "JAV_PILOT_MAINTENANCE_LOCK_ROOT"
            ) != MAINTENANCE_LOCK_MOUNT:
                raise NasReleaseError(
                    "docker compose maintenance lock environment is invalid"
                )
        optional_legacy_targets = {BACKUP_MOUNT, BROWSER_PROFILE_MOUNT}
        expected_targets = required_targets | optional_legacy_targets
        actual_targets = set(mounts)
        if recorded_mounts is not None and actual_targets != set(recorded_mounts):
            raise NasReleaseError(
                "docker compose mount targets differ from recorded topology"
            )
        if allow_legacy_topology:
            if not (
                required_targets <= actual_targets <= expected_targets
                and (
                    BROWSER_PROFILE_MOUNT not in actual_targets
                    or BACKUP_MOUNT in actual_targets
                )
            ):
                raise NasReleaseError("docker compose mount target set is invalid")
        elif actual_targets != expected_targets:
            raise NasReleaseError("docker compose mount target set is invalid")

        data_root = self.paths.app_root / "data"
        if data_root.is_symlink() or not data_root.is_dir():
            raise NasReleaseError("application data mount source is unsafe")
        expected_binds = {
            "/app/data": str(data_root.resolve(strict=True)),
            MAINTENANCE_LOCK_MOUNT: str(self._maintenance_lock_root()),
            **{
                destination: str(environment.get(variable) or "")
                for destination, variable in HOST_MOUNT_ENV_BY_DESTINATION.items()
            },
            BACKUP_MOUNT: str(
                environment.get("JAV_PILOT_HISTORY_BACKUP_HOST_PATH") or ""
            ),
        }
        for target in sorted(required_targets | ({BACKUP_MOUNT} & actual_targets)):
            mount = mounts.get(target, {})
            raw_read_only = mount.get("read_only")
            bind_options = mount.get("bind")
            read_only_ok = (
                raw_read_only is True
                if target == BACKUP_MOUNT
                else raw_read_only is None or raw_read_only is False
            )
            if (
                mount.get("type") != "bind"
                or mount.get("source") != expected_binds[target]
                or not isinstance(bind_options, dict)
                # Compose v2.20 renders the explicit default as
                # ``{"create_host_path": true}``, while newer Compose
                # releases normalize the same bind to ``{}``.  The source,
                # target, type, and read-only checks above remain mandatory.
                or bind_options not in ({}, {"create_host_path": True})
                or not read_only_ok
            ):
                raise NasReleaseError("docker compose bind mount is invalid")

        volumes = model.get("volumes")
        if BROWSER_PROFILE_MOUNT in actual_targets:
            profile_mount = mounts[BROWSER_PROFILE_MOUNT]
            profile_source = profile_mount.get("source")
            profile_read_only = profile_mount.get("read_only")
            profile_options = profile_mount.get("volume")
            if (
                profile_mount.get("type") != "volume"
                or not (
                    profile_read_only is None or profile_read_only is False
                )
                or profile_options != {}
                or not isinstance(profile_source, str)
                or not isinstance(volumes, dict)
                or set(volumes) != {profile_source}
            ):
                raise NasReleaseError("docker compose browser volume is invalid")
            volume = volumes.get(profile_source)
            if (
                not isinstance(volume, dict)
                or set(volume) != {"name"}
                or volume.get("name") != self.browser_profile_volume
            ):
                raise NasReleaseError("docker compose browser volume is invalid")
        elif volumes:
            raise NasReleaseError("docker compose volumes are invalid")
        return user

    def _release_locks(self) -> ExitStack:
        stack = ExitStack()
        try:
            root = self._maintenance_lock_root()
            stack.enter_context(self.lock_factory(root / _MAINTENANCE_LOCK_NAME))
        except BaseException:
            stack.close()
            raise
        return stack

    def _maintenance_lock_root(self) -> Path:
        root = self.paths.app_root / "runtime" / "maintenance-locks"
        if root.parent.is_symlink() or root.is_symlink() or not root.is_dir():
            raise NasReleaseError(
                "provision the shared maintenance lock directory before deployment"
            )
        return root.resolve(strict=True)

    def _compose(
        self,
        arguments: Sequence[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        compose = _required_release_file(self.paths.app_root, "docker-compose.yml")
        return self.runner.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(self.paths.app_root),
                "--env-file",
                str(self.paths.app_root / ".env"),
                "-f",
                str(compose),
                *arguments,
            ],
            env=self._compose_environment(environment),
        )

    def _compose_environment(
        self,
        environment: Mapping[str, str] | None,
    ) -> dict[str, str]:
        merged = dict(environment or {})
        merged["JAV_PILOT_COMPOSE_BROWSER_PROFILE_VOLUME"] = self.browser_profile_volume
        return merged

    def _stop(self) -> None:
        self._compose(["stop", self.compose_service])

    def _web_download_queue_paused(self) -> bool:
        try:
            payload = self.web_download_queue_control(None)
            return _web_download_queue_paused(payload)
        except (OSError, ValueError, NasReleaseError) as exc:
            raise NasReleaseError("web download queue control is unavailable") from exc

    def _set_web_download_queue_paused(self, paused: bool) -> None:
        action = "pause" if paused else "resume"
        try:
            payload = self.web_download_queue_control(action)
            applied = _web_download_queue_paused(payload)
        except (OSError, ValueError, NasReleaseError) as exc:
            raise NasReleaseError("web download queue control update failed") from exc
        if applied is not paused:
            raise NasReleaseError("web download queue control update was not applied")

    def _container_web_download_queue_control(
        self,
        action: str | None,
    ) -> dict[str, object]:
        if action not in {None, "pause", "resume"}:
            raise NasReleaseError("web download queue control action is invalid")
        script = "\n".join(
            (
                "import json,sys,urllib.request",
                "from http.cookies import SimpleCookie",
                "from jav_pilot.auth import AuthConfig,SESSION_COOKIE,make_session_cookie",
                "action=sys.argv[1]",
                "origin=sys.argv[2]",
                "headers={'Accept':'application/json'}",
                "config=AuthConfig.from_env()",
                "if config.enabled:",
                "    if not config.configured or not config.secret_persistent:",
                "        raise RuntimeError('authentication is not ready')",
                "    cookies=SimpleCookie()",
                "    cookies.load(make_session_cookie(config))",
                "    session=cookies.get(SESSION_COOKIE)",
                "    if session is None:",
                "        raise RuntimeError('maintenance session is unavailable')",
                "    headers['Cookie']=SESSION_COOKIE+'='+session.value",
                "data=None",
                "method='GET'",
                "if action!='status':",
                "    data=json.dumps({'action':action},separators=(',',':')).encode('ascii')",
                "    headers['Content-Type']='application/json'",
                "    method='POST'",
                "request=urllib.request.Request(origin+'/api/web-downloads/control',data=data,headers=headers,method=method)",
                "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))",
                "with opener.open(request,timeout=3) as response:",
                "    raw=response.read(1048577)",
                "if len(raw)>1048576:",
                "    raise RuntimeError('queue response is too large')",
                "payload=json.loads(raw.decode('utf-8'))",
                "if not isinstance(payload,dict):",
                "    raise RuntimeError('queue response is invalid')",
                "print(json.dumps(payload,ensure_ascii=True,separators=(',',':')))",
            )
        )
        result = self.runner.run(
            [
                "docker",
                "exec",
                self.container_name,
                "python",
                "-c",
                script,
                action or "status",
                # This request executes inside the service container; the
                # host-facing port may be an ephemeral mapping in a drill.
                DEFAULT_BASE_URL,
            ],
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
            raise NasReleaseError("web download queue control request failed")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError(
                "web download queue control response is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise NasReleaseError("web download queue control response is invalid")
        return payload

    def _pause_persisted_web_download_queue(self) -> None:
        data_root = self.paths.app_root / "data"
        database = data_root / "web_downloads.sqlite3"
        if (
            data_root.is_symlink()
            or not data_root.is_dir()
            or database.is_symlink()
            or not database.is_file()
        ):
            raise NasReleaseError("web download queue database is unavailable")
        mount_source = str(data_root)
        if any(character in mount_source for character in (",", "\x00", "\n", "\r")):
            raise NasReleaseError("web download queue data path is unsafe")
        container = self._container_state(required=True)
        image_id = _image_id_value(container.get("Image"))
        script = "\n".join(
            (
                "import json,sqlite3,sys",
                "database=sys.argv[1]",
                "updated_at=float(sys.argv[2])",
                "connection=sqlite3.connect(database,timeout=5)",
                "try:",
                "    connection.execute('PRAGMA busy_timeout=5000')",
                "    connection.execute('BEGIN IMMEDIATE')",
                "    columns={str(row[1]) for row in connection.execute('PRAGMA table_info(web_download_control)').fetchall() if len(row)>1}",
                "    if not {'id','global_paused','updated_at'}.issubset(columns):",
                "        raise RuntimeError('queue control schema is unavailable')",
                "    changed=connection.execute('UPDATE web_download_control SET global_paused=1,updated_at=? WHERE id=1',(updated_at,)).rowcount",
                "    row=connection.execute('SELECT global_paused FROM web_download_control WHERE id=1').fetchone()",
                "    if changed!=1 or row!=(1,):",
                "        raise RuntimeError('queue control row is unavailable')",
                "    connection.commit()",
                "    checkpoint=connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()",
                "    if checkpoint is None or checkpoint[0]!=0:",
                "        raise RuntimeError('queue checkpoint failed')",
                "finally:",
                "    connection.close()",
                "print(json.dumps({'ok':True,'global_paused':True},separators=(',',':')))",
            )
        )
        result = self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "32",
                "--memory",
                "128m",
                "--cpus",
                "1",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--mount",
                f"type=bind,source={mount_source},target=/app/data",
                "--entrypoint",
                "python",
                image_id,
                "-c",
                script,
                "/app/data/web_downloads.sqlite3",
                str(self.now().timestamp()),
            ],
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 64 * 1024:
            raise NasReleaseError("web download queue could not be quiesced")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("web download queue could not be quiesced") from exc
        if payload != {"ok": True, "global_paused": True}:
            raise NasReleaseError("web download queue could not be quiesced")

    def _restore_web_download_queue(self, journal: Mapping[str, object]) -> None:
        prior_paused = _release_journal_queue_prior_state(journal)
        if prior_paused is False:
            self._set_web_download_queue_paused(False)

    def _container_state(self, *, required: bool) -> dict[str, object]:
        result = self.runner.run(
            ["docker", "inspect", self.container_name],
            check=required,
        )
        if result.returncode != 0:
            return {}
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("docker inspect returned invalid JSON") from exc
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
        ):
            raise NasReleaseError("docker inspect returned an invalid container")
        return payload[0]

    def _image_schema_contract(self, image_id: str, *, source: Path | None = None) -> dict[str, object]:
        script = ["import json", "import sys", "from pathlib import Path"]
        command = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            # The release staging directory is private to the NAS operator.
            # Root without DAC override cannot read another user's mode-0700
            # source; imports also need a writable, isolated temporary root.
            "--user", f"{os.geteuid()}:{os.getegid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
            "--env", "TMPDIR=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint", "python",
        ]
        if source is not None:
            _required_release_file(source, "jav_pilot/schema_contract.py")
            command.extend(["-v", f"{source}:/release:ro"])
            script.append("sys.path.insert(0, '/release')")
        script.append("import jav_pilot.schema_contract as schema")
        if source is not None:
            script.extend([
                "if Path(schema.__file__).resolve() != Path('/release/jav_pilot/schema_contract.py'):",
                "    raise RuntimeError('schema contract was not loaded from release source')",
            ])
        script.append("print(json.dumps(schema.runtime_schema_contract()))")
        result = self.runner.run([*command, image_id, "-c", "\n".join(script)])
        try:
            contract = json.loads(result.stdout.decode("utf-8"))
            if not _contract_versions(contract):
                raise ValueError("empty schema contract")
        except (ValueError, UnicodeError) as exc:
            raise NasReleaseError("target image schema contract is invalid") from exc
        return contract

    def _container_schema_contract(self) -> dict[str, object]:
        script = "\n".join(
            (
                "import json",
                "try:",
                "    from jav_pilot.schema_contract import runtime_schema_contract",
                "except ModuleNotFoundError as exc:",
                "    if exc.name != 'jav_pilot.schema_contract':",
                "        raise",
                "    contract = {",
                "        'contract_version': 1,",
                "        'components': [],",
                "        'legacy_unavailable': True,",
                "    }",
                "else:",
                "    contract = runtime_schema_contract()",
                "print(json.dumps(contract, sort_keys=True))",
            )
        )
        result = self.runner.run(
            ["docker", "exec", self.container_name, "python", "-c", script]
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("container schema contract is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("components"), list
        ):
            raise NasReleaseError("container schema contract is incomplete")
        return payload

    def _container_database_components(
        self, filenames: Sequence[str],
    ) -> dict[str, list[str]]:
        if not filenames:
            return {}
        if any(re.fullmatch(r"[A-Za-z0-9_.-]+\.sqlite3", name) is None for name in filenames):
            raise NasReleaseError("persistent database filename is invalid")
        # Reading a WAL database may need to initialize its shared-memory
        # sidecar even with mode=ro. Use the running service's filesystem
        # identity rather than the host operator, without changing permissions
        # or ignoring committed WAL pages via immutable=1.
        script = "\n".join((
            "import json, sqlite3, sys",
            "from pathlib import Path",
            "root = Path(sys.argv[1])",
            "result = {}",
            "for name in json.loads(sys.argv[2]):",
            "    path = root / name",
            "    if path.is_symlink() or not path.is_file():",
            "        raise RuntimeError('persistent database path is unsafe')",
            "    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)",
            "    try:",
            "        connection.execute('PRAGMA query_only = ON')",
            "        present = connection.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'\").fetchone()",
            "        result[name] = sorted({str(row[0]) for row in connection.execute('SELECT DISTINCT component FROM schema_migrations')}) if present else []",
            "    finally:",
            "        connection.close()",
            "print(json.dumps(result, sort_keys=True))",
        ))
        result = self.runner.run([
            "docker", "exec", self.container_name, "python", "-c", script,
            "/app/data", json.dumps(list(filenames)),
        ])
        try:
            if len(result.stdout) > 1024 * 1024:
                raise ValueError("schema result is too large")
            payload = json.loads(result.stdout.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != set(filenames):
                raise ValueError("schema result does not match requested databases")
            for components in payload.values():
                if not isinstance(components, list) or any(
                    not isinstance(name, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
                    for name in components
                ):
                    raise ValueError("schema component is invalid")
        except (ValueError, UnicodeError) as exc:
            raise NasReleaseError("container database schema result is invalid") from exc
        return payload

    def _container_release_capabilities(self) -> dict[str, bool]:
        script = "\n".join(
            (
                "import importlib.util",
                "import json",
                "capabilities = {",
                "    'browser_acceptance': importlib.util.find_spec(",
                "        'jav_pilot.browser_acceptance'",
                "    ) is not None,",
                "    'schema_contract': importlib.util.find_spec(",
                "        'jav_pilot.schema_contract'",
                "    ) is not None,",
                "}",
                "print(json.dumps(",
                "    {'release_capabilities': capabilities}, sort_keys=True",
                "))",
            )
        )
        result = self.runner.run(
            ["docker", "exec", self.container_name, "python", "-c", script]
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("container release capabilities are invalid") from exc
        capabilities = (
            payload.get("release_capabilities") if isinstance(payload, dict) else None
        )
        if not isinstance(capabilities, dict) or set(capabilities) != {
            "browser_acceptance",
            "schema_contract",
        }:
            raise NasReleaseError("container release capabilities are incomplete")
        if any(not isinstance(value, bool) for value in capabilities.values()):
            raise NasReleaseError("container release capabilities are invalid")
        return {
            "browser_acceptance": capabilities["browser_acceptance"],
            "schema_contract": capabilities["schema_contract"],
        }

    def _legacy_browser_acceptance(self) -> tuple[bool, dict[str, object]]:
        pages_json = json.dumps(_LEGACY_BROWSER_PAGES, ensure_ascii=True)
        script = "\n".join(
            (
                "import json",
                "import sys",
                "from http.cookies import SimpleCookie",
                "from urllib.parse import urlsplit",
                f"PAGES = {pages_json}",
                "result = {",
                "    'ok': False,",
                "    'pages': [],",
                "    'browser_errors': 0,",
                "    'post_requests': 0,",
                "}",
                "try:",
                "    parsed = urlsplit(str(sys.argv[1]).strip().rstrip('/'))",
                "    port = parsed.port",
                "    if (",
                "        parsed.scheme != 'http'",
                "        or parsed.hostname not in {'127.0.0.1', 'localhost'}",
                "        or port is None",
                "        or not 1 <= port <= 65535",
                "        or parsed.username is not None",
                "        or parsed.password is not None",
                "        or parsed.path not in {'', '/'}",
                "        or parsed.query",
                "        or parsed.fragment",
                "    ):",
                "        raise RuntimeError('invalid legacy acceptance origin')",
                "    origin = f'http://{parsed.hostname}:{port}'",
                "    from jav_pilot.auth import (",
                "        AuthConfig,",
                "        SESSION_COOKIE,",
                "        make_session_cookie,",
                "    )",
                "    from playwright.sync_api import sync_playwright",
                "    with sync_playwright() as playwright:",
                "        browser = playwright.chromium.launch(headless=True)",
                "        try:",
                "            context = browser.new_context(",
                "                viewport={'width': 1440, 'height': 960},",
                "                locale='zh-CN',",
                "                reduced_motion='reduce',",
                "            )",
                "            try:",
                "                config = AuthConfig.from_env()",
                "                if config.enabled:",
                "                    if not config.configured:",
                "                        raise RuntimeError('authentication is not ready')",
                "                    cookie = SimpleCookie()",
                "                    cookie.load(make_session_cookie(config))",
                "                    session = cookie.get(SESSION_COOKIE)",
                "                    if session is None:",
                "                        raise RuntimeError('session is unavailable')",
                "                    context.add_cookies([{",
                "                        'name': SESSION_COOKIE,",
                "                        'value': session.value,",
                "                        'url': origin,",
                "                        'httpOnly': True,",
                "                        'sameSite': 'Lax',",
                "                    }])",
                "                page = context.new_page()",
                "                def record_console(message):",
                "                    if message.type == 'error':",
                "                        result['browser_errors'] += 1",
                "                def record_page_error(_error):",
                "                    result['browser_errors'] += 1",
                "                def record_request(request):",
                "                    if request.method.upper() == 'POST':",
                "                        result['post_requests'] += 1",
                "                page.on('console', record_console)",
                "                page.on('pageerror', record_page_error)",
                "                page.on('request', record_request)",
                "                for path, heading in PAGES:",
                "                    response = page.goto(",
                "                        f'{origin}{path}',",
                "                        wait_until='domcontentloaded',",
                "                        timeout=30000,",
                "                    )",
                "                    status = response.status if response else 0",
                "                    if status != 200:",
                "                        raise RuntimeError('page is unavailable')",
                "                    page.locator('h1').first.wait_for(",
                "                        state='visible', timeout=15000",
                "                    )",
                "                    headings = [",
                "                        value.strip()",
                "                        for value in page.locator('h1').all_inner_texts()",
                "                    ]",
                "                    if headings != [heading]:",
                "                        raise RuntimeError('page heading is invalid')",
                "                    page.wait_for_timeout(250)",
                "                    overflow = not page.evaluate(",
                "                        'document.documentElement.scrollWidth <= '",
                "                        'document.documentElement.clientWidth'",
                "                    )",
                "                    result['pages'].append({",
                "                        'path': path,",
                "                        'status': status,",
                "                        'heading': heading,",
                "                        'horizontal_overflow': overflow,",
                "                    })",
                "                    if overflow:",
                "                        raise RuntimeError('page overflow detected')",
                "                if result['browser_errors'] or result['post_requests']:",
                "                    raise RuntimeError('browser safety check failed')",
                "                result['ok'] = True",
                "            finally:",
                "                context.close()",
                "        finally:",
                "            browser.close()",
                "except Exception:",
                "    result['ok'] = False",
                "payload = {'legacy_browser_acceptance': result}",
                "print(json.dumps(",
                "    payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')",
                "))",
                "raise SystemExit(0 if result['ok'] else 1)",
            )
        )
        command_result = self.runner.run(
            [
                "docker",
                "exec",
                self.container_name,
                "python",
                "-c",
                script,
                DEFAULT_BASE_URL,
            ],
            check=False,
        )
        try:
            payload = json.loads(command_result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, {}
        expected_pages = [
            {
                "path": path,
                "status": 200,
                "heading": heading,
                "horizontal_overflow": False,
            }
            for path, heading in _LEGACY_BROWSER_PAGES
        ]
        expected_payload = {
            "legacy_browser_acceptance": {
                "ok": True,
                "pages": expected_pages,
                "browser_errors": 0,
                "post_requests": 0,
            }
        }
        payload_ok = isinstance(payload, dict) and _canonical_json(
            payload
        ) == _canonical_json(expected_payload)
        return command_result.returncode == 0 and payload_ok, (
            payload if isinstance(payload, dict) else {}
        )

    def _image_digest(self, image_id: str) -> str:
        return str(self._image_metadata(image_id)["digest"])

    def _materialize_target_image(
        self,
        source: ImageDeploymentSource,
        *,
        revision: str,
    ) -> dict[str, str]:
        if source.kind == "pull":
            if source.registry_digest_reference is None:
                raise NasReleaseError("registry image source is incomplete")
            self.runner.run(
                [
                    "docker",
                    "image",
                    "pull",
                    "--platform",
                    source.platform,
                    source.registry_digest_reference,
                ]
            )
        elif source.kind == "archive":
            if source.archive_path is None or source.archive_sha256 is None:
                raise NasReleaseError("image archive source is incomplete")
            if _sha256(source.archive_path) != source.archive_sha256:
                raise NasReleaseError(
                    "image archive SHA-256 does not match its manifest"
                )
            load_result = self.runner.run(
                [
                    "docker",
                    "image",
                    "load",
                    "--input",
                    str(source.archive_path),
                ]
            )
            _require_loaded_image_reference(
                load_result,
                source.compose_reference,
            )
            if _sha256(source.archive_path) != source.archive_sha256:
                raise NasReleaseError("image archive changed while it was loaded")
        else:  # pragma: no cover - constructed only by _deployment_image_source.
            raise NasReleaseError("image deployment source is invalid")
        metadata = self._image_metadata(source.compose_reference)
        if (
            source.expected_image_id is not None
            and metadata["image_id"] != source.expected_image_id
        ):
            raise NasReleaseError("loaded image ID does not match its manifest")
        if metadata["revision"] != revision:
            raise NasReleaseError("target image OCI revision does not match release")
        if metadata["platform"] != source.platform:
            raise NasReleaseError("target image platform does not match release")
        if (
            source.kind == "pull"
            and metadata["digest"] != source.registry_digest_reference
        ):
            raise NasReleaseError("pulled image digest does not match release")
        return metadata

    def _image_metadata(
        self,
        image_reference: str,
        *,
        expected_digest: object | None = None,
    ) -> dict[str, str]:
        clean_reference = str(image_reference or "").strip()
        is_image_id = _IMAGE_ID_RE.fullmatch(clean_reference) is not None
        is_digest_reference = (
            _IMMUTABLE_IMAGE_REFERENCE_RE.fullmatch(clean_reference) is not None
        )
        is_sha_tag = _SHA_TAG_REFERENCE_RE.fullmatch(clean_reference) is not None
        if not (is_image_id or is_digest_reference or is_sha_tag):
            raise NasReleaseError("container image reference is invalid")
        result = self.runner.run(["docker", "image", "inspect", clean_reference])
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NasReleaseError("docker image inspect returned invalid JSON") from exc
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
        ):
            raise NasReleaseError("docker image inspect returned an invalid image")
        image = payload[0]
        inspected_id = _image_id_value(image.get("Id"))
        if is_image_id and inspected_id != clean_reference:
            raise NasReleaseError("docker image identity does not match the container")
        repo_digests = (
            {
                digest
                for digest in image.get("RepoDigests", [])
                if isinstance(digest, str)
                and _IMMUTABLE_IMAGE_REFERENCE_RE.fullmatch(digest) is not None
            }
            if isinstance(image.get("RepoDigests"), list)
            else set()
        )
        if is_digest_reference and clean_reference not in repo_digests:
            raise NasReleaseError("docker image digest does not match the reference")
        preferred_digest = str(expected_digest or "").strip()
        digest_value: str | None = None
        if is_digest_reference:
            digest_value = clean_reference
        elif preferred_digest == inspected_id or preferred_digest in repo_digests:
            digest_value = preferred_digest
        elif repo_digests:
            digest_value = sorted(repo_digests)[0]
        else:
            digest_value = inspected_id
        config = image.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        revision = (
            labels.get("org.opencontainers.image.revision")
            if isinstance(labels, dict)
            else None
        )
        clean_revision = _revision(revision)
        os_name = str(image.get("Os") or "").strip().lower()
        architecture = str(image.get("Architecture") or "").strip().lower()
        variant = str(image.get("Variant") or "").strip().lower()
        platform = (
            f"{os_name}/{architecture}{f'/{variant}' if variant else ''}"
            if os_name and architecture
            else ""
        )
        return {
            "image_id": inspected_id,
            "digest": digest_value,
            "revision": clean_revision,
            "platform": platform,
        }

    def _acceptance(
        self,
        revision: str,
        *,
        expected_mounts: object,
        expected_container_user: object,
        expected_image_id: object | None = None,
        expected_image_digest: object | None = None,
        expected_oci_revision: object | None = None,
        expected_image_reference: object | None = None,
        expected_image_platform: object | None = None,
        allow_legacy_runtime: bool = False,
        timeout: float = 180.0,
    ) -> dict[str, object]:
        if not isinstance(allow_legacy_runtime, bool):
            raise NasReleaseError("legacy runtime acceptance mode is invalid")
        expected_user = _compose_service_user(expected_container_user)
        expected = expected_mounts if isinstance(expected_mounts, dict) else {}
        deadline = time.monotonic() + timeout
        container: dict[str, object] = {}
        health: dict[str, object] = {}
        readiness: dict[str, object] = {}
        while time.monotonic() < deadline:
            container = self._container_state(required=True)
            state = container.get("State")
            status = state.get("Health") if isinstance(state, dict) else None
            health_status = status.get("Status") if isinstance(status, dict) else None
            if health_status == "healthy":
                try:
                    health = self.http_json(f"{self.base_url}/healthz")
                    readiness = self.http_json(f"{self.base_url}/readyz")
                except (OSError, ValueError):
                    pass
                if health.get("revision") == revision and readiness.get("ok") is True:
                    break
            self.sleep(2.0)
        mounts = _mount_map(container)
        mount_details = _mount_details(container)
        image_id = str(container.get("Image") or "")
        image_metadata: dict[str, str] = {}
        try:
            image_metadata = self._image_metadata(
                image_id,
                expected_digest=expected_image_digest,
            )
        except NasReleaseError:
            image_metadata = {}
        image_id_ok = (
            isinstance(expected_image_id, str)
            and bool(expected_image_id)
            and image_id == expected_image_id
        )
        image_digest_ok = (
            isinstance(expected_image_digest, str)
            and bool(expected_image_digest)
            and image_metadata.get("digest") == expected_image_digest
        )
        image_revision_ok = (
            isinstance(expected_oci_revision, str)
            and image_metadata.get("revision") == expected_oci_revision == revision
        )
        image_reference = ""
        try:
            image_reference = _container_image_reference(container)
        except NasReleaseError:
            pass
        image_reference_ok = expected_image_reference is None or (
            isinstance(expected_image_reference, str)
            and bool(expected_image_reference)
            and image_reference == expected_image_reference
        )
        image_platform_ok = expected_image_platform is None or (
            isinstance(expected_image_platform, str)
            and bool(expected_image_platform)
            and image_metadata.get("platform") == expected_image_platform
        )
        config = container.get("Config")
        container_user = config.get("User") if isinstance(config, dict) else None
        container_user_ok = container_user == expected_user
        mount_ok = all(
            destination in mount_details
            and mount_details[destination]["type"] == "bind"
            and mount_details[destination]["rw"] is True
            and (
                destination not in expected
                or (
                    expected[destination] == mounts[destination]
                    if isinstance(expected[destination], str)
                    else isinstance(expected[destination], dict)
                    and expected[destination].get("source") == mounts[destination]
                )
            )
            for destination in REQUIRED_MOUNTS
        )
        if MAINTENANCE_LOCK_MOUNT in expected:
            lock_mount = mount_details.get(MAINTENANCE_LOCK_MOUNT, {})
            lock_source = expected[MAINTENANCE_LOCK_MOUNT]
            if isinstance(lock_source, dict):
                lock_source = lock_source.get("source")
            mount_ok = mount_ok and (
                lock_mount.get("type") == "bind"
                and lock_mount.get("rw") is True
                and mounts.get(MAINTENANCE_LOCK_MOUNT) == lock_source
                and lock_source == str(self._maintenance_lock_root())
            )
        browser_mount_contract_present = (
            BROWSER_PROFILE_MOUNT in mount_details or BROWSER_PROFILE_MOUNT in expected
        )
        backup_mount_contract_present = (
            BACKUP_MOUNT in mount_details or BACKUP_MOUNT in expected
        )
        browser_mount_required = (
            not allow_legacy_runtime or browser_mount_contract_present
        )
        backup_mount_required = (
            not allow_legacy_runtime
            or browser_mount_contract_present
            or backup_mount_contract_present
        )
        if mount_ok and browser_mount_required:
            browser_profile = mount_details.get(BROWSER_PROFILE_MOUNT, {})
            mount_ok = (
                browser_profile.get("type") == "volume"
                and browser_profile.get("rw") is True
                and browser_profile.get("name") == self.browser_profile_volume
            )
        if mount_ok and backup_mount_required:
            backup = mount_details.get(BACKUP_MOUNT, {})
            mount_ok = (
                backup.get("type") == "bind"
                and backup.get("rw") is False
                and (
                    BACKUP_MOUNT not in expected
                    or (
                        expected[BACKUP_MOUNT] == mounts.get(BACKUP_MOUNT)
                        if isinstance(expected[BACKUP_MOUNT], str)
                        else isinstance(expected[BACKUP_MOUNT], dict)
                        and expected[BACKUP_MOUNT].get("source")
                        == mounts.get(BACKUP_MOUNT)
                    )
                )
            )
        host_config = container.get("HostConfig")
        init_ok = isinstance(host_config, dict) and host_config.get("Init") is True
        restart_ok = int(container.get("RestartCount") or 0) == 0
        sqlite_checks = self._container_sqlite_integrity()
        runtime_capabilities: dict[str, bool] = {}
        legacy_runtime = False
        capabilities_ok = True
        browser_mode = "modern"
        if allow_legacy_runtime:
            runtime_capabilities = self._container_release_capabilities()
            browser_available = runtime_capabilities["browser_acceptance"]
            schema_available = runtime_capabilities["schema_contract"]
            legacy_runtime = not browser_available and not schema_available
            capabilities_ok = legacy_runtime or (browser_available and schema_available)
            if legacy_runtime:
                browser_mode = "legacy"
            elif not capabilities_ok:
                browser_mode = "unsupported"
        browser_ok = False
        legacy_browser_payload: dict[str, object] = {}
        if browser_mode == "legacy":
            browser_ok, legacy_browser_payload = self._legacy_browser_acceptance()
        elif browser_mode == "modern":
            browser_result = self.runner.run(
                [
                    "docker",
                    "exec",
                    self.container_name,
                    "python",
                    "-m",
                    "jav_pilot.cli",
                    "acceptance-browser",
                    "--base-url",
                    # The host-facing port may be an ephemeral mapping (the
                    # isolated release drill uses one).  Browser acceptance
                    # runs inside the service container, so it must connect
                    # to the container's fixed Compose target port instead of
                    # looping back to the host mapping.
                    DEFAULT_BASE_URL,
                ],
                check=False,
            )
            try:
                browser_payload = json.loads(browser_result.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                browser_payload = {}
            browser_ok = (
                browser_result.returncode == 0
                and isinstance(browser_payload, dict)
                and browser_payload.get("ok") is True
                and browser_payload.get("download_posts") == 0
                and browser_payload.get("browser_errors") == 0
                and browser_payload.get("detail_cases")
                == {"with_magnets": True, "without_magnets": True}
            )
        logs = self.runner.run(
            ["docker", "logs", "--since", "10m", self.container_name],
            check=False,
        )
        logs_clean, structured_log_errors = _logs_are_clean(
            logs.stdout + b"\n" + logs.stderr
        )
        logs_clean = logs.returncode == 0 and logs_clean
        process_result = self.runner.run(
            ["docker", "top", self.container_name, "-eo", "pid,stat,comm,args"],
            check=False,
        )
        processes_clean = process_result.returncode == 0 and _processes_clean(
            process_result.stdout
        )
        staging_clean = self._web_staging_clean()
        checks = readiness.get("checks")
        readiness_checks_ok = isinstance(checks, dict) and all(
            value is True for value in checks.values()
        )
        ok = all(
            (
                health.get("ok") is True,
                health.get("revision") == revision,
                readiness.get("ok") is True,
                readiness_checks_ok,
                image_id_ok,
                image_digest_ok,
                image_revision_ok,
                image_reference_ok,
                image_platform_ok,
                container_user_ok,
                mount_ok,
                init_ok,
                restart_ok,
                all(value == "ok" for value in sqlite_checks.values()),
                capabilities_ok,
                browser_ok,
                logs_clean,
                processes_clean,
                staging_clean,
            )
        )
        return {
            "ok": ok,
            "health": health.get("ok") is True,
            "readiness": readiness.get("ok") is True,
            "readiness_checks": checks if isinstance(checks, dict) else {},
            "revision": health.get("revision"),
            "image_id": image_id,
            "image_id_ok": image_id_ok,
            "image_digest": image_metadata.get("digest"),
            "image_digest_ok": image_digest_ok,
            "image_revision": image_metadata.get("revision"),
            "image_revision_ok": image_revision_ok,
            "image_reference": image_reference,
            "image_reference_ok": image_reference_ok,
            "image_platform": image_metadata.get("platform"),
            "image_platform_ok": image_platform_ok,
            "expected_container_user": expected_user,
            "container_user": container_user,
            "container_user_ok": container_user_ok,
            "mounts": mounts,
            "mount_details": mount_details,
            "mounts_ok": mount_ok,
            "init": init_ok,
            "restart_count_zero": restart_ok,
            "sqlite": sqlite_checks,
            "browser": browser_ok,
            "browser_mode": browser_mode,
            "legacy_browser": legacy_browser_payload,
            "legacy_runtime": legacy_runtime,
            "runtime_capabilities": runtime_capabilities,
            "logs_clean": logs_clean,
            "structured_log_errors": structured_log_errors,
            "processes_clean": processes_clean,
            "web_staging_clean": staging_clean,
        }

    def _container_sqlite_integrity(self) -> dict[str, str]:
        script = "\n".join(
            (
                "import json,sqlite3",
                "from pathlib import Path",
                "root=Path('/app/data')",
                "checks={}",
                "if root.is_symlink() or not root.is_dir():",
                "    checks['data']='unavailable'",
                "else:",
                "    databases=sorted(root.glob('*.sqlite3'),key=lambda item:item.name)",
                "    if not databases:",
                "        checks['data']='missing'",
                "    for database in databases:",
                "        if database.is_symlink() or not database.is_file():",
                "            checks[database.name]='unsafe'",
                "            continue",
                "        row=None",
                "        try:",
                "            connection=sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True,timeout=5)",
                "            try:",
                "                row=connection.execute('PRAGMA integrity_check').fetchone()",
                "            finally:",
                "                connection.close()",
                "        except sqlite3.Error:",
                "            pass",
                "        checks[database.name]='ok' if row==('ok',) else 'failed'",
                "print(json.dumps({'sqlite_integrity':checks},sort_keys=True))",
            )
        )
        result = self.runner.run(
            ["docker", "exec", self.container_name, "python", "-c", script],
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
            return {"data": "failed"}
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"data": "failed"}
        checks = payload.get("sqlite_integrity") if isinstance(payload, dict) else None
        if not isinstance(checks, dict) or not checks:
            return {"data": "failed"}
        validated: dict[str, str] = {}
        for raw_name, raw_status in checks.items():
            if not isinstance(raw_name, str) or not isinstance(raw_status, str):
                return {"data": "failed"}
            if raw_name == "data":
                if raw_status not in {"missing", "unavailable"}:
                    return {"data": "failed"}
            elif (
                Path(raw_name).name != raw_name
                or not raw_name.endswith(".sqlite3")
                or raw_status not in {"ok", "failed", "unsafe"}
            ):
                return {"data": "failed"}
            validated[raw_name] = raw_status
        return dict(sorted(validated.items()))

    def _web_staging_clean(self) -> bool:
        script = "\n".join(
            (
                "import json,re,time",
                "from pathlib import Path",
                "root=Path('/downloads/jav-web')",
                "pattern=re.compile(r'^(?:[.]jav-pilot-(?:test|acceptance|smoke)-|acceptance-test[._-]|playwright-test[._-]|test-residue[._-])',re.I)",
                "bad=[]",
                "timed_out=False",
                "ok=root.is_dir() and not root.is_symlink()",
                "deadline=time.monotonic()+10.0",
                "if ok:",
                "    for path in root.rglob('*'):",
                "        if time.monotonic()>deadline:",
                "            timed_out=True",
                "            break",
                "        if path.is_symlink() or pattern.search(path.name):",
                "            bad.append(path.relative_to(root).as_posix())",
                "            if len(bad)>=20:",
                "                break",
                "ok=ok and not timed_out and not bad",
                "print(json.dumps({'ok':ok,'test_residue':bad,'timed_out':timed_out},sort_keys=True))",
            )
        )
        result = self.runner.run(
            ["docker", "exec", self.container_name, "python", "-c", script],
            check=False,
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return (
            result.returncode == 0
            and isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("test_residue") == []
        )

    def _manifest_path(self, release_id: str) -> Path:
        return self.paths.state_root / "manifests" / f"{release_id}.json"

    def _available_release_occurrence_id(
        self,
        release_seed: object,
        *,
        current_release_id: object | None,
        reusable_manifest: dict[str, object] | None = None,
    ) -> str:
        clean_seed = _release_id(release_seed)
        revision_prefix = clean_seed.partition("-")[0]
        current_identity = (
            _release_id(current_release_id)
            if current_release_id is not None
            else "bootstrap"
        )
        candidate = clean_seed
        occurrence = 0
        while True:
            manifest_path = self._manifest_path(candidate)
            source_path = self.paths.state_root / "sources" / candidate
            manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
            source_exists = source_path.exists() or source_path.is_symlink()
            if not source_exists and not manifest_exists:
                return candidate
            if not source_exists and reusable_manifest is not None:
                expected_manifest = {**reusable_manifest, "release_id": candidate}
                try:
                    existing_manifest = _read_json(manifest_path)
                except NasReleaseError:
                    pass
                else:
                    if _canonical_json(existing_manifest) == _canonical_json(
                        expected_manifest
                    ):
                        return candidate
            occurrence += 1
            if occurrence > 1000:
                raise NasReleaseError("release occurrence identity space is exhausted")
            occurrence_digest = hashlib.sha256(
                f"{clean_seed}\0{current_identity}\0{occurrence}".encode("utf-8")
            ).hexdigest()
            candidate = f"{revision_prefix}-{occurrence_digest[:12]}"

    def _publish_manifest(self, manifest: dict[str, object]) -> Path:
        path = self._publish_manifest_file(manifest)
        self._write_current_pointer(manifest)
        return path

    def _publish_manifest_file(self, manifest: dict[str, object]) -> Path:
        release_id = _release_id(manifest.get("release_id"))
        path = self._manifest_path(release_id)
        if path.exists():
            existing = _read_json(path)
            if _canonical_json(existing) != _canonical_json(manifest):
                raise NasReleaseError("immutable release manifest already exists")
        else:
            _write_json_exclusive(path, manifest)
        return path

    def _release_journal_path(self) -> Path:
        return self.paths.state_root / "release-operation.json"

    def _write_release_journal(self, journal: dict[str, object]) -> None:
        _validate_release_journal(journal)
        _atomic_json(self._release_journal_path(), journal)

    def _advance_release_journal(
        self,
        journal: dict[str, object],
        status: str,
    ) -> None:
        journal["status"] = status
        self._write_release_journal(journal)

    def _clear_release_journal(self) -> None:
        path = self._release_journal_path()
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)

    def _release_fault(self, point: str) -> None:
        self.fault_hook(point)

    def _prepare_release_operation(self, *, apply: bool) -> None:
        path = self._release_journal_path()
        if path.exists() or path.is_symlink():
            if not apply:
                raise NasReleaseError("a pending release operation requires recovery with --apply; dry-run did not change it")
            self._recover_release_journal()

    def _recover_release_journal(self) -> None:
        path = self._release_journal_path()
        if not path.exists() and not path.is_symlink():
            return
        journal = _validate_release_journal(_read_json(path))
        if journal["operation"] == "rollback":
            self._recover_interrupted_rollback(journal)
            return
        status = str(journal["status"])
        if status == "queue_restored":
            self._cleanup_deploy_recovery_tool(journal)
            self._clear_release_journal()
            return
        if status == "queue_restoring":
            manifest = journal.get("manifest")
            current = self._load_current_manifest(required=True)
            if not isinstance(manifest, dict) or current.get(
                "release_id"
            ) != manifest.get("release_id"):
                raise NasReleaseError("published release journal state is inconsistent")
            self._restore_web_download_queue(journal)
            self._advance_release_journal(journal, "queue_restored")
            self._cleanup_deploy_recovery_tool(journal)
            self._clear_release_journal()
            return
        if status in {"accepted", "manifest_published", "current_published"}:
            manifest = journal.get("manifest")
            if not isinstance(manifest, dict):
                raise NasReleaseError("accepted release journal has no manifest")
            revision = _revision(manifest.get("revision"))
            acceptance = self._acceptance(
                revision,
                expected_mounts=manifest.get("mounts"),
                expected_container_user=(
                    manifest.get("compose_service_user")
                    if manifest.get("compose_service_user") is not None
                    else _container_config_user(self._container_state(required=True))
                ),
                expected_image_id=manifest.get("image_id"),
                expected_image_digest=manifest.get("runtime_image_digest")
                or manifest.get("image_digest"),
                expected_oci_revision=revision,
                expected_image_reference=manifest.get("image_reference"),
                expected_image_platform=manifest.get("image_platform"),
            )
            if acceptance["ok"]:
                self._publish_manifest_file(manifest)
                journal["status"] = "manifest_published"
                self._write_release_journal(journal)
                self._write_current_pointer(manifest)
                journal["status"] = "current_published"
                self._write_release_journal(journal)
                journal["status"] = "queue_restoring"
                self._write_release_journal(journal)
                self._restore_web_download_queue(journal)
                journal["status"] = "queue_restored"
                self._write_release_journal(journal)
                self._cleanup_deploy_recovery_tool(journal)
                self._clear_release_journal()
                return
        self._recover_interrupted_deploy(journal)

    def _recover_interrupted_deploy(self, journal: dict[str, object]) -> None:
        previous = journal.get("previous")
        if not isinstance(previous, dict):
            raise NasReleaseError("interrupted deployment previous state is invalid")
        revision = _revision(previous.get("revision"))
        image_id = str(previous.get("image_id") or "")
        image_digest = str(previous.get("image_digest") or "")
        repository = str(previous.get("image_repository") or "")
        image_tag = str(previous.get("image_tag") or "")
        image_reference_value = previous.get("image_reference")
        image_reference = (
            _immutable_image_reference(image_reference_value)
            if image_reference_value is not None
            else None
        )
        mounts = previous.get("mounts")
        legacy_target_started = (
            journal.get("journal_version") == _LEGACY_RELEASE_JOURNAL_VERSION
            and journal.get("status") == "target_started"
        )
        previous_user_value = previous.get("compose_service_user")
        previous_container_user = (
            None
            if legacy_target_started or previous_user_value is None
            else _compose_service_user(previous_user_value)
        )
        allow_legacy_value = previous.get("allow_legacy_topology")
        allow_legacy_topology = (
            True if legacy_target_started or allow_legacy_value is None else allow_legacy_value
        )
        if not isinstance(allow_legacy_topology, bool):
            raise NasReleaseError("interrupted deployment Compose mode is invalid")
        allow_legacy_ports_value = previous.get("allow_legacy_ports")
        allow_legacy_ports = (
            True
            if legacy_target_started or allow_legacy_ports_value is None
            else allow_legacy_ports_value
        )
        if not isinstance(allow_legacy_ports, bool):
            raise NasReleaseError("interrupted deployment Compose port mode is invalid")
        status = str(journal.get("status") or "")
        source_value = journal.get("source_snapshot")
        source_snapshot = Path(str(source_value or ""))
        expected_source = (
            self.paths.state_root / "sources" / _release_id(journal.get("release_id"))
        )
        if source_snapshot != expected_source:
            raise NasReleaseError("interrupted deployment source path is unsafe")
        source_available = source_snapshot.exists() and not source_snapshot.is_symlink()
        recovery_environment = _release_environment(
            revision,
            mounts=mounts,
            backup_root=self.paths.backup_root,
            image_reference=image_reference,
            image_tag=None if image_reference is not None else image_tag,
            image_repository=repository,
        )
        replacement_statuses = {
            "replacing_source",
            "source_replaced",
            "target_starting",
            "target_started",
            "accepted",
            "manifest_published",
            "current_published",
        }
        if status in replacement_statuses:
            recovery_tool_source = self._validated_deploy_recovery_tool(journal)
            source_snapshot = _safe_existing_directory(
                source_value,
                self.paths.state_root,
                "interrupted deployment source snapshot",
            )
            self._stop()
            _sync_source_tree(source_snapshot, self.paths.app_root)
            if journal.get("backup_path") is not None:
                backup = _safe_existing_directory(
                    journal.get("backup_path"), self.paths.backup_root,
                    "interrupted deployment backup",
                )
                self.backup_service.restore(
                    backup, self.paths.app_root, image_id=image_id,
                    release_source=recovery_tool_source,
                )
            elif not _journal_preserves_data(journal):
                raise NasReleaseError("interrupted deployment requires a data backup")
            recovery_compose = self._validate_compose(
                self.paths.app_root,
                recovery_environment,
                recorded_mounts=mounts,
                allow_legacy_topology=allow_legacy_topology,
                allow_legacy_ports=allow_legacy_ports,
            )
            if previous_container_user is not None and (
                recovery_compose.service_user != previous_container_user
            ):
                raise NasReleaseError("recovery Compose service user changed")
            recovery_container_user = recovery_compose.service_user
            self._pause_persisted_web_download_queue()
            self._compose(
                [
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "--force-recreate",
                ],
                environment=recovery_environment,
            )
        else:
            recovery_container_user = previous_container_user or _container_config_user(
                self._container_state(required=True)
            )
            self._stop()
            self._pause_persisted_web_download_queue()
            self.runner.run(["docker", "start", self.container_name])
        recovery = self._acceptance(
            revision,
            expected_mounts=mounts,
            expected_container_user=recovery_container_user,
            expected_image_id=image_id,
            expected_image_digest=image_digest,
            expected_oci_revision=revision,
            allow_legacy_runtime=True,
        )
        if not recovery["ok"]:
            raise NasReleaseError("interrupted deployment recovery acceptance failed")
        previous_release_id = previous.get("release_id")
        if previous_release_id is None:
            pointer = self.paths.state_root / "current.json"
            if pointer.exists():
                current_pointer = _read_json(pointer)
                if current_pointer.get("release_id") == journal.get("release_id"):
                    pointer.unlink()
                    _fsync_directory(pointer.parent)
        else:
            self._write_current_pointer(
                self._load_manifest(_release_id(previous_release_id))
            )
        self._restore_web_download_queue(journal)
        if source_available:
            shutil.rmtree(source_snapshot)
            _fsync_directory(source_snapshot.parent)
        self._cleanup_deploy_recovery_tool(journal)
        self._clear_release_journal()

    def _validated_deploy_recovery_tool(
        self,
        journal: dict[str, object],
    ) -> Path:
        candidate, _root = self._deploy_recovery_tool_paths(journal)
        source = _safe_existing_directory(
            candidate,
            self.paths.state_root,
            "deployment recovery tool artifact",
        )
        expected_hash = _sha256_value(
            journal.get("recovery_tool_sha256"),
            "deployment recovery tool SHA-256",
        )
        if _source_tree_digest(source) != expected_hash:
            raise NasReleaseError("deployment recovery tool artifact checksum mismatch")
        _required_release_file(source, "jav_pilot/backup_restore.py")
        return source

    def _cleanup_deploy_recovery_tool(
        self,
        journal: dict[str, object],
    ) -> None:
        _candidate, root = self._deploy_recovery_tool_paths(journal)
        if not root.exists() and not root.is_symlink():
            return
        safe_root = _safe_existing_directory(
            root,
            self.paths.state_root,
            "deployment recovery tool root",
        )
        shutil.rmtree(safe_root)
        _fsync_directory(safe_root.parent)

    def _deploy_recovery_tool_paths(
        self,
        journal: dict[str, object],
    ) -> tuple[Path, Path]:
        release_id = _release_id(journal.get("release_id"))
        expected_root = self.paths.state_root / "recovery-tools" / release_id
        expected_source = expected_root / "source"
        candidate = Path(str(journal.get("recovery_tool_source") or ""))
        if candidate != expected_source:
            raise NasReleaseError("deployment recovery tool path is unsafe")
        _sha256_value(
            journal.get("recovery_tool_sha256"),
            "deployment recovery tool SHA-256",
        )
        return candidate, expected_root

    def _recover_interrupted_rollback(self, journal: dict[str, object]) -> None:
        current = journal.get("current")
        target = journal.get("target")
        if not isinstance(current, dict) or not isinstance(target, dict):
            raise NasReleaseError("interrupted rollback manifests are invalid")
        status = str(journal.get("status") or "")
        target_revision = _revision(target.get("revision"))
        target_digest = _image_digest_value(journal.get("target_image_digest"))
        target_user_value = journal.get("target_compose_service_user")
        target_container_user = (
            _compose_service_user(target_user_value)
            if target_user_value is not None
            else None
        )
        if status == "queue_restored":
            self._cleanup_rollback_recovery_source(journal)
            self._clear_release_journal()
            return
        if status == "queue_restoring":
            current_pointer = self._load_current_manifest(required=True)
            if current_pointer.get("release_id") != target.get("release_id"):
                raise NasReleaseError(
                    "published rollback journal state is inconsistent"
                )
            self._restore_web_download_queue(journal)
            journal["status"] = "queue_restored"
            self._write_release_journal(journal)
            self._cleanup_rollback_recovery_source(journal)
            self._clear_release_journal()
            return
        committed_statuses = {"accepted", "event_published", "current_published"}
        if status in committed_statuses:
            acceptance = self._acceptance(
                target_revision,
                expected_mounts=target.get("mounts"),
                expected_container_user=target_container_user
                or _container_config_user(self._container_state(required=True)),
                expected_image_id=target.get("image_id"),
                expected_image_digest=target_digest,
                expected_oci_revision=target_revision,
                expected_image_reference=target.get("image_reference"),
                expected_image_platform=target.get("image_platform"),
                allow_legacy_runtime=_allows_legacy_manifest_acceptance(target),
            )
            event = journal.get("event")
            event_path = self._validated_rollback_event_path(journal)
            if acceptance["ok"] and isinstance(event, dict):
                if event_path.exists():
                    if _canonical_json(_read_json(event_path)) != _canonical_json(
                        event
                    ):
                        raise NasReleaseError("immutable rollback event already exists")
                else:
                    _write_json_exclusive(event_path, event)
                journal["status"] = "event_published"
                self._write_release_journal(journal)
                self._write_current_pointer(target)
                journal["status"] = "current_published"
                self._write_release_journal(journal)
                journal["status"] = "queue_restoring"
                self._write_release_journal(journal)
                self._restore_web_download_queue(journal)
                journal["status"] = "queue_restored"
                self._write_release_journal(journal)
                self._cleanup_rollback_recovery_source(journal)
                self._clear_release_journal()
                return

        current_revision = _revision(current.get("revision"))
        current_image = str(current.get("image_id") or "")
        current_digest = _image_digest_value(journal.get("current_image_digest"))
        current_user_value = journal.get("current_compose_service_user")
        current_container_user = (
            _compose_service_user(current_user_value)
            if current_user_value is not None
            else None
        )
        current_environment = _release_environment(
            current_revision,
            mounts=current.get("mounts"),
            backup_root=self.paths.backup_root,
            image_reference=(
                str(current["image_reference"])
                if current.get("image_reference") is not None
                else None
            ),
            image_tag=(
                None
                if current.get("image_reference") is not None
                else str(current.get("image_tag") or current_revision)
            ),
            image_repository=str(current.get("image_repository") or ""),
        )
        source_statuses = {
            "current_source_saved",
            "applying_target",
            "target_started",
            "accepted",
            "event_published",
            "current_published",
        }
        if status in source_statuses:
            recovery_source = self._validated_rollback_recovery_source(journal)
            self._stop()
            _sync_source_tree(recovery_source, self.paths.app_root)
            if journal.get("recovery_backup") is not None:
                backup = _safe_existing_directory(
                    journal.get("recovery_backup"), self.paths.backup_root,
                    "interrupted rollback recovery backup",
                )
                self.backup_service.restore(
                    backup, self.paths.app_root, image_id=current_image,
                    release_source=recovery_source,
                )
            elif not _contract_can_read(target.get("schema_contract"), _contract_versions(current.get("schema_contract"))):
                raise NasReleaseError("interrupted rollback requires a data backup")
            recovery_compose = self._validate_compose(
                self.paths.app_root,
                current_environment,
                recorded_mounts=current.get("mounts"),
                allow_legacy_topology=_allows_legacy_manifest_acceptance(current),
                allow_legacy_ports=_allows_legacy_port_acceptance(current),
            )
            if (
                current_container_user is not None
                and recovery_compose.service_user != current_container_user
            ):
                raise NasReleaseError("recovery Compose service user changed")
            recovery_container_user = recovery_compose.service_user
            self._pause_persisted_web_download_queue()
            self._compose(
                [
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "--force-recreate",
                ],
                environment=current_environment,
            )
        else:
            recovery_container_user = current_container_user or _container_config_user(
                self._container_state(required=True)
            )
            self._stop()
            self._pause_persisted_web_download_queue()
            self.runner.run(["docker", "start", self.container_name])
        recovery = self._acceptance(
            current_revision,
            expected_mounts=current.get("mounts"),
            expected_container_user=recovery_container_user,
            expected_image_id=current_image,
            expected_image_digest=current_digest,
            expected_oci_revision=current_revision,
            expected_image_reference=current.get("image_reference"),
            expected_image_platform=current.get("image_platform"),
        )
        if not recovery["ok"]:
            raise NasReleaseError("interrupted rollback recovery acceptance failed")
        self._write_current_pointer(current)
        self._restore_web_download_queue(journal)
        event_path_value = journal.get("event_path")
        if isinstance(event_path_value, str) and event_path_value:
            event_path = self._validated_rollback_event_path(journal)
            _durable_unlink_if_present(event_path)
        self._cleanup_rollback_recovery_source(journal)
        self._clear_release_journal()

    def _validated_rollback_recovery_source(
        self,
        journal: dict[str, object],
    ) -> Path:
        candidate = Path(str(journal.get("recovery_source") or ""))
        parent = candidate.parent
        expected_prefix = f".rollback-{_release_id(journal.get('release_id'))}-"
        try:
            parent.resolve(strict=True).relative_to(self.paths.state_root)
        except (OSError, ValueError) as exc:
            raise NasReleaseError("rollback recovery source is unsafe") from exc
        if candidate.name != "current-source" or not parent.name.startswith(
            expected_prefix
        ):
            raise NasReleaseError("rollback recovery source is unsafe")
        return _safe_existing_directory(
            candidate,
            self.paths.state_root,
            "rollback recovery source",
        )

    @contextmanager
    def _rollback_recovery_workspace(self, release_id: str) -> Iterator[Path]:
        parent = Path(tempfile.mkdtemp(
            prefix=f".rollback-{release_id}-", dir=self.paths.state_root,
        ))
        source = parent / "current-source"
        try:
            yield source
        finally:
            # A failed automatic recovery must retain the source referenced by
            # its durable journal so the next --apply can finish the recovery.
            journal = self._release_journal_path()
            if not journal.exists() and not journal.is_symlink():
                self._cleanup_rollback_recovery_source({
                    "release_id": release_id, "recovery_source": str(source),
                })

    def _cleanup_rollback_recovery_source(
        self,
        journal: dict[str, object],
    ) -> None:
        candidate = Path(str(journal.get("recovery_source") or ""))
        parent = candidate.parent
        if not candidate.exists() and not parent.exists():
            return
        expected_prefix = f".rollback-{_release_id(journal.get('release_id'))}-"
        try:
            parent.resolve(strict=True).relative_to(self.paths.state_root)
        except (OSError, ValueError) as exc:
            raise NasReleaseError("rollback recovery source is unsafe") from exc
        if candidate.name != "current-source" or not parent.name.startswith(
            expected_prefix
        ):
            raise NasReleaseError("rollback recovery source is unsafe")
        if candidate.exists():
            self._validated_rollback_recovery_source(journal)
        shutil.rmtree(parent)
        _fsync_directory(parent.parent)

    def _validated_rollback_event_path(
        self,
        journal: dict[str, object],
    ) -> Path:
        candidate = Path(str(journal.get("event_path") or ""))
        root = self.paths.state_root / "rollbacks"
        if candidate.parent != root or candidate.suffix != ".json":
            raise NasReleaseError("rollback event path is unsafe")
        return candidate

    def _write_current_pointer(self, manifest: dict[str, object]) -> None:
        release_id = _release_id(manifest.get("release_id"))
        path = self._manifest_path(release_id)
        pointer = {
            "release_id": release_id,
            "manifest_sha256": _sha256(path),
        }
        _atomic_json(self.paths.state_root / "current.json", pointer)

    def _load_manifest(self, release_id: str) -> dict[str, object]:
        payload = _read_json(self._manifest_path(release_id))
        if payload.get("format_version") not in SUPPORTED_MANIFEST_FORMAT_VERSIONS:
            raise NasReleaseError("release manifest version is unsupported")
        if payload.get("release_id") != release_id:
            raise NasReleaseError("release manifest identity is invalid")
        _revision(payload.get("revision"))
        _sha256_value(payload.get("archive_sha256"), "release archive SHA-256")
        return payload

    def _load_current_manifest(
        self,
        *,
        required: bool = False,
    ) -> dict[str, object]:
        pointer_path = self.paths.state_root / "current.json"
        if not pointer_path.exists():
            if required:
                raise NasReleaseError("current release manifest is unavailable")
            return {}
        pointer = _read_json(pointer_path)
        release_id = _release_id(pointer.get("release_id"))
        manifest_path = self._manifest_path(release_id)
        expected_hash = _sha256_value(
            pointer.get("manifest_sha256"),
            "current manifest SHA-256",
        )
        if _sha256(manifest_path) != expected_hash:
            raise NasReleaseError("current release manifest checksum mismatch")
        return self._load_manifest(release_id)


def _allows_legacy_manifest_acceptance(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    compatibility = manifest.get("compatibility")
    return (
        isinstance(compatibility, dict)
        and compatibility.get("bootstrap_predecessor") is True
    )


def _allows_legacy_port_acceptance(manifest: object) -> bool:
    """Allow the pre-dual-bind Compose only while migrating old manifests."""
    if not isinstance(manifest, dict):
        return True
    return manifest.get("compose_port_topology") != "dual_bind"


def _is_rfc1918_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return address.version == 4 and any(address in network for network in _RFC1918_NETWORKS)


def _release_environment(
    revision: str,
    *,
    mounts: object,
    backup_root: Path,
    image_reference: str | None = None,
    image_tag: str | None = None,
    image_repository: str | None = None,
) -> dict[str, str]:
    environment = {"JAV_PILOT_REVISION": revision}
    if image_reference is not None:
        environment["JAV_PILOT_IMAGE_REFERENCE"] = _deployment_image_reference(
            image_reference,
            revision=revision,
        )
    else:
        environment["JAV_PILOT_IMAGE_REFERENCE"] = ""
        tag = str(image_tag or revision).strip()
        if not tag or tag.lower() == "latest":
            raise NasReleaseError("production image tag must be immutable")
        environment["JAV_PILOT_IMAGE_TAG"] = tag
    if image_repository is not None and image_reference is None:
        repository = str(image_repository).strip()
        if not repository or any(character.isspace() for character in repository):
            raise NasReleaseError("image repository is invalid")
        environment["JAV_PILOT_IMAGE_REPOSITORY"] = repository
    environment.update(_host_mount_environment(mounts, backup_root=backup_root))
    return environment


def _compose_image_reference(environment: Mapping[str, str]) -> str:
    revision = _revision(environment.get("JAV_PILOT_REVISION"))
    reference = str(environment.get("JAV_PILOT_IMAGE_REFERENCE") or "").strip()
    if reference:
        return _deployment_image_reference(reference, revision=revision)
    repository = str(environment.get("JAV_PILOT_IMAGE_REPOSITORY") or "").strip()
    tag = str(environment.get("JAV_PILOT_IMAGE_TAG") or "").strip()
    if (
        not repository
        or not tag
        or tag.lower() == "latest"
        or any(character.isspace() for character in repository)
        or any(character.isspace() for character in tag)
        or any(character in repository + tag for character in ("\x00", "\n", "\r"))
    ):
        raise NasReleaseError("docker compose image environment is invalid")
    expected = f"{repository}:{tag}"
    if _image_reference_parts(expected) != (repository, tag):
        raise NasReleaseError("docker compose image environment is invalid")
    return expected


def _compose_value_present(value: object) -> bool:
    return value is not None and value is not False and value != [] and value != {}


def _compose_service_user(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{1,10}:[0-9]{1,10}", value
    ) is None:
        raise NasReleaseError("docker compose service user is invalid")
    return value


def _container_config_user(container: Mapping[str, object]) -> str:
    config = container.get("Config")
    if not isinstance(config, dict):
        raise NasReleaseError("container configuration is invalid")
    return _compose_service_user(config.get("User"))


def _host_mount_environment(
    mounts: object,
    *,
    backup_root: Path,
) -> dict[str, str]:
    if not isinstance(mounts, Mapping):
        raise NasReleaseError("release mount manifest is invalid")
    environment: dict[str, str] = {}
    for destination, variable in HOST_MOUNT_ENV_BY_DESTINATION.items():
        value = mounts.get(destination)
        if isinstance(value, Mapping):
            value = value.get("source")
        source = _posix_host_path(value, f"mount source for {destination}")
        environment[variable] = source
    backup = str(backup_root)
    if not backup or "\0" in backup or "\n" in backup or "\r" in backup:
        raise NasReleaseError("backup host path is invalid")
    environment["JAV_PILOT_HISTORY_BACKUP_HOST_PATH"] = backup
    return environment


def _posix_host_path(value: object, label: str) -> str:
    clean = str(value or "").strip()
    path = PurePosixPath(clean)
    if (
        not clean
        or not path.is_absolute()
        or clean != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\0" in clean
        or "\n" in clean
        or "\r" in clean
    ):
        raise NasReleaseError(f"{label} is invalid")
    return clean


def _deployment_image_source(
    *,
    revision: str,
    image_reference: object | None,
    image_platform: object | None,
    image_archive_path: Path | None,
    image_manifest_path: Path | None,
    image_archive_filename: object | None,
) -> ImageDeploymentSource:
    archive_mode = any(
        value is not None
        for value in (
            image_archive_path,
            image_manifest_path,
            image_archive_filename,
        )
    )
    if (image_reference is None) == (not archive_mode):
        raise NasReleaseError(
            "exactly one image source is required: digest reference or archive"
        )
    if image_reference is not None:
        if archive_mode:
            raise NasReleaseError("image sources are mutually exclusive")
        reference = _immutable_image_reference(image_reference)
        return ImageDeploymentSource(
            kind="pull",
            registry_digest_reference=reference,
            compose_reference=reference,
            platform=_image_platform(image_platform or "linux/amd64"),
        )
    if image_platform is not None:
        raise NasReleaseError("image platform is supplied by the image manifest")
    if (
        image_archive_path is None
        or image_manifest_path is None
        or image_archive_filename is None
    ):
        raise NasReleaseError(
            "image archive, manifest, and original filename are all required"
        )
    archive = Path(image_archive_path).absolute()
    manifest_path = Path(image_manifest_path).absolute()
    if archive.is_symlink() or not archive.is_file():
        raise NasReleaseError("image archive is unavailable or unsafe")
    manifest = _read_json(manifest_path)
    if set(manifest) != _IMAGE_ARCHIVE_MANIFEST_KEYS:
        raise NasReleaseError("image archive manifest schema is invalid")
    if manifest.get("schema_version") != 2:
        raise NasReleaseError("image archive manifest version is unsupported")
    if _revision(manifest.get("revision")) != revision:
        raise NasReleaseError("image archive revision does not match the release")
    sha_tag = _sha_tag_reference(manifest.get("sha_tag"), revision)
    image_id = _image_id_value(manifest.get("image_id"))
    platform = _image_platform(manifest.get("platform"))
    filename = str(manifest.get("archive_filename") or "").strip()
    supplied_filename = str(image_archive_filename or "").strip()
    expected_filename = f"jav-pilot-{revision}-{platform.replace('/', '-')}.tar.zst"
    if (
        not filename
        or filename != Path(filename).name
        or filename != expected_filename
        or filename != supplied_filename
        or any(character in filename for character in ("\x00", "\n", "\r"))
    ):
        raise NasReleaseError("image archive filename is invalid")
    archive_sha256 = _sha256_value(
        manifest.get("archive_sha256"), "image archive SHA-256"
    )
    if _sha256(archive) != archive_sha256:
        raise NasReleaseError("image archive SHA-256 does not match its manifest")
    return ImageDeploymentSource(
        kind="archive",
        registry_digest_reference=None,
        compose_reference=sha_tag,
        platform=platform,
        expected_image_id=image_id,
        archive_path=archive,
        archive_filename=filename,
        archive_sha256=archive_sha256,
    )


def _immutable_image_reference(value: object) -> str:
    clean = str(value or "").strip()
    if (
        not _IMMUTABLE_IMAGE_REFERENCE_RE.fullmatch(clean)
        or len(clean) > 320
        or "//" in clean
    ):
        raise NasReleaseError(
            "image reference must be a full registry reference pinned by sha256 digest"
        )
    return clean


def _is_immutable_image_reference(value: object) -> bool:
    try:
        _immutable_image_reference(value)
    except NasReleaseError:
        return False
    return True


def _sha_tag_reference(value: object, revision: object) -> str:
    clean = str(value or "").strip()
    match = _SHA_TAG_REFERENCE_RE.fullmatch(clean)
    if match is None or match.group("tag") != _revision(revision):
        raise NasReleaseError("image SHA tag must end with the full release revision")
    return clean


def _deployment_image_reference(value: object, *, revision: object) -> str:
    clean = str(value or "").strip()
    if _IMMUTABLE_IMAGE_REFERENCE_RE.fullmatch(clean):
        return _immutable_image_reference(clean)
    return _sha_tag_reference(clean, revision)


def _image_repository(value: object) -> str:
    clean = str(value or "").strip()
    digest_match = _IMMUTABLE_IMAGE_REFERENCE_RE.fullmatch(clean)
    if digest_match is not None:
        return digest_match.group("repository")
    tag_match = _SHA_TAG_REFERENCE_RE.fullmatch(clean)
    if tag_match is not None:
        return tag_match.group("repository")
    return _image_reference_parts(clean)[0]


def _image_platform(value: object) -> str:
    clean = str(value or "").strip()
    if not _IMAGE_PLATFORM_RE.fullmatch(clean):
        raise NasReleaseError(
            "image platform must use os/architecture[/variant] format"
        )
    return clean


def _image_id_value(value: object) -> str:
    clean = str(value or "").strip()
    if not _IMAGE_ID_RE.fullmatch(clean):
        raise NasReleaseError("image ID is invalid")
    return clean


def _require_loaded_image_reference(
    result: CommandResult,
    expected_reference: str,
) -> None:
    raw = result.stdout + b"\n" + result.stderr
    if len(raw) > 1024 * 1024:
        raise NasReleaseError("docker image load output is invalid")
    try:
        lines = {line.strip() for line in raw.decode("utf-8").splitlines()}
    except UnicodeDecodeError as exc:
        raise NasReleaseError("docker image load output is invalid") from exc
    if f"Loaded image: {expected_reference}" not in lines:
        raise NasReleaseError("image archive did not load its manifest SHA tag")


def _image_reference_parts(value: object) -> tuple[str, str]:
    clean = str(value or "").strip()
    if not clean or any(character.isspace() for character in clean):
        raise NasReleaseError("container image reference is invalid")
    without_digest = clean.split("@", 1)[0]
    slash = without_digest.rfind("/")
    colon = without_digest.rfind(":")
    if colon > slash:
        repository = without_digest[:colon]
        tag = without_digest[colon + 1 :]
    else:
        repository = without_digest
        tag = "latest"
    if not repository or not tag:
        raise NasReleaseError("container image reference is invalid")
    return repository, tag


def _extract_release_archive(
    archive_path: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    archive = Path(archive_path)
    if archive.is_symlink() or not archive.is_file():
        raise NasReleaseError("release archive is unavailable")
    if _sha256(archive) != expected_sha256:
        raise NasReleaseError("release archive checksum mismatch")
    destination.mkdir(parents=True, mode=0o700)
    seen: set[str] = set()
    total_bytes = 0
    try:
        handle = tarfile.open(archive, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise NasReleaseError("release archive is invalid") from exc
    with handle:
        members = handle.getmembers()
        if not 1 <= len(members) <= _MAX_ARCHIVE_FILES:
            raise NasReleaseError("release archive file count is invalid")
        for member in members:
            relative = _relative_source_path(member.name)
            if relative in seen:
                raise NasReleaseError("release archive contains duplicate paths")
            seen.add(relative)
            first = PurePosixPath(relative).parts[0]
            if first in PROTECTED_TOP_LEVEL:
                raise NasReleaseError("release archive contains a protected path")
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise NasReleaseError("release archive contains an unsafe entry")
            total_bytes += member.size
            if total_bytes > _MAX_ARCHIVE_BYTES:
                raise NasReleaseError("release archive is too large")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise NasReleaseError("release archive entry cannot be read")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(target, (stat.S_IMODE(member.mode) & 0o755) or 0o600)


def _copy_source_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if _source_entry_excluded(child.name):
            continue
        _copy_safe(child, destination / child.name)
    _fsync_tree(destination)


def _publish_source_snapshot(source: Path, destination: Path) -> str:
    if destination.exists() or destination.is_symlink():
        raise NasReleaseError("release source snapshot already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    try:
        _copy_source_tree(source, temporary)
        digest = _source_tree_digest(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        if _source_tree_digest(destination) != digest:
            raise NasReleaseError("release source snapshot changed during publication")
        return digest
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)


def _sync_source_tree(source: Path, destination: Path) -> None:
    for child in sorted(destination.iterdir(), key=lambda item: item.name):
        if _source_entry_excluded(child.name):
            continue
        _remove_safe(child)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child.name in PROTECTED_TOP_LEVEL:
            raise NasReleaseError("release source contains a protected path")
        if _source_entry_excluded(child.name):
            continue
        _copy_safe(child, destination / child.name)
    _fsync_tree(destination)


def _copy_safe(source: Path, destination: Path) -> None:
    info = source.lstat()
    if source.is_symlink():
        raise NasReleaseError("release source contains a symbolic link")
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=(stat.S_IMODE(info.st_mode) & 0o755) or 0o700)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_safe(child, destination / child.name)
        return
    if not stat.S_ISREG(info.st_mode):
        raise NasReleaseError("release source contains a special file")
    shutil.copy2(source, destination, follow_symlinks=False)
    if os.name == "nt":  # pragma: no cover - deployment runs on Linux.
        return
    descriptor = os.open(
        destination,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_safe(path: Path) -> None:
    if path.is_symlink():
        raise NasReleaseError("application source contains a symbolic link")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()
    else:
        raise NasReleaseError("application source contains a special file")


def _source_entry_excluded(name: str) -> bool:
    return (
        name in PROTECTED_TOP_LEVEL
        or name == _MAINTENANCE_LOCK_NAME
        or any(name.startswith(prefix) for prefix in _SOURCE_EXCLUDED_PREFIXES)
    )


def _source_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath("."))]
    while stack:
        directory, relative_root = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise NasReleaseError("release source tree cannot be inspected") from exc
        directories: list[tuple[Path, PurePosixPath]] = []
        for child in children:
            if relative_root == PurePosixPath(".") and _source_entry_excluded(
                child.name
            ):
                continue
            info = child.lstat()
            if child.is_symlink():
                raise NasReleaseError("release source contains a symbolic link")
            relative = (
                PurePosixPath(child.name)
                if relative_root == PurePosixPath(".")
                else relative_root / child.name
            )
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                digest.update(f"D\0{relative.as_posix()}\0{mode:o}\n".encode("utf-8"))
                directories.append((child, relative))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise NasReleaseError("release source contains a special file")
            file_count += 1
            total_bytes += info.st_size
            if file_count > _MAX_ARCHIVE_FILES or total_bytes > _MAX_ARCHIVE_BYTES:
                raise NasReleaseError("release source tree exceeds safety limits")
            digest.update(
                (
                    f"F\0{relative.as_posix()}\0{mode:o}\0{info.st_size}\0"
                    f"{_sha256(child)}\n"
                ).encode("utf-8")
            )
        stack.extend(reversed(directories))
    return digest.hexdigest()


def _fsync_tree(root: Path) -> None:
    if os.name == "nt":  # pragma: no cover - deployment runs on Linux.
        return
    directories: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        directories.append(directory)
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if directory == root and _source_entry_excluded(child.name):
                continue
            info = child.lstat()
            if child.is_symlink():
                raise NasReleaseError("release source contains a symbolic link")
            if stat.S_ISDIR(info.st_mode):
                stack.append(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise NasReleaseError("release source contains a special file")
            descriptor = os.open(
                child,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _container_revision(container: dict[str, object]) -> str:
    config = container.get("Config")
    environment = config.get("Env") if isinstance(config, dict) else None
    if isinstance(environment, list):
        for item in environment:
            if isinstance(item, str) and item.startswith("JAV_PILOT_REVISION="):
                return _revision(item.partition("=")[2])
    raise NasReleaseError("running container has no immutable revision")


def _container_image_reference(container: dict[str, object]) -> str:
    config = container.get("Config")
    if not isinstance(config, dict):
        raise NasReleaseError("running container configuration is invalid")
    image = config.get("Image")
    if not isinstance(image, str) or not image.strip():
        raise NasReleaseError("running container has no image reference")
    return image.strip()


def _mount_map(container: dict[str, object]) -> dict[str, str]:
    mounts = container.get("Mounts")
    result: dict[str, str] = {}
    if not isinstance(mounts, list):
        return result
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        destination = mount.get("Destination")
        source = mount.get("Source")
        if isinstance(destination, str) and isinstance(source, str):
            result[destination] = source
    return result


def _mount_details(container: dict[str, object]) -> dict[str, dict[str, object]]:
    mounts = container.get("Mounts")
    result: dict[str, dict[str, object]] = {}
    if not isinstance(mounts, list):
        return result
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        destination = mount.get("Destination")
        source = mount.get("Source")
        mount_type = mount.get("Type")
        read_write = mount.get("RW")
        name = mount.get("Name")
        if (
            isinstance(destination, str)
            and isinstance(source, str)
            and isinstance(mount_type, str)
            and isinstance(read_write, bool)
        ):
            detail: dict[str, object] = {
                "source": source,
                "type": mount_type,
                "rw": read_write,
            }
            if isinstance(name, str):
                detail["name"] = name
            result[destination] = detail
    return result


def _http_json(url: str) -> dict[str, object]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("health response is invalid")
    return payload


def _web_download_queue_paused(payload: Mapping[str, object]) -> bool:
    control = payload.get("control")
    paused = control.get("global_paused") if isinstance(control, dict) else None
    if payload.get("ok") is not True or not isinstance(paused, bool):
        raise ValueError("web download queue response is invalid")
    return paused


def _release_journal_queue_prior_state(
    journal: Mapping[str, object],
) -> bool | None:
    queue = journal.get("web_download_queue")
    if (
        queue is None
        and journal.get("journal_version") == _LEGACY_RELEASE_JOURNAL_VERSION
    ):
        return None
    if not isinstance(queue, dict) or set(queue) != {"prior_global_paused"}:
        raise NasReleaseError("release operation journal queue state is invalid")
    prior_paused = queue.get("prior_global_paused")
    if not isinstance(prior_paused, bool):
        raise NasReleaseError("release operation journal queue state is invalid")
    return prior_paused


def _sqlite_integrity(data_root: Path) -> dict[str, str]:
    if data_root.is_symlink() or not data_root.is_dir():
        return {"data": "unavailable"}
    result: dict[str, str] = {}
    databases = sorted(data_root.glob("*.sqlite3"), key=lambda item: item.name)
    if not databases:
        return {"data": "missing"}
    for database in databases:
        if database.is_symlink() or not database.is_file():
            result[database.name] = "unsafe"
            continue
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            connection.close()
        result[database.name] = "ok" if row == ("ok",) else "failed"
    return result


def _processes_clean(raw: bytes) -> bool:
    """Return whether a successful ``docker top`` snapshot is safe.

    The service deliberately keeps a virtual display (``xvfb-run``/``Xvfb``)
    and may have a Chromium process alive while the acceptance probe is
    running.  Those are expected *running* processes and must not make a
    deployment fail.  What the release gate needs to catch is a process that
    has exited without being reaped: Linux reports that as a ``Z`` state and
    commonly appends ``<defunct>`` to the command.  A malformed or empty
    snapshot is also unsafe because it cannot establish that the process
    table was inspected successfully.
    """
    if not isinstance(raw, (bytes, bytearray)):
        return False
    try:
        text = bytes(raw).decode("utf-8", errors="replace")
    except (TypeError, ValueError):
        return False
    lines = text.splitlines()
    if not lines:
        return False

    header = lines[0].strip().split()
    if len(header) < 2 or [item.casefold() for item in header[:2]] != [
        "pid",
        "stat",
    ]:
        return False

    saw_process = False
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split(maxsplit=3)
        if len(fields) < 2:
            return False
        pid, state = fields[0], fields[1]
        if not pid.isdecimal() or int(pid) <= 0 or not state:
            return False
        lowered = line.casefold()
        if "z" in state.casefold() or "<defunct>" in lowered:
            return False
        # Some ps implementations spell the state out instead of exposing
        # the one-letter STAT flag.  Treat only an explicit marker as a
        # zombie; active browser/Xvfb command names are otherwise harmless.
        if "defunct" in lowered or "(zombie)" in lowered:
            return False
        saw_process = True
    return saw_process


def _logs_are_clean(raw: bytes) -> tuple[bool, int]:
    text = raw.decode("utf-8", errors="replace")
    lowered = text.lower()
    textual_error = any(
        marker in lowered for marker in ("traceback", "fatal", "uncaught")
    )
    structured_errors = 0
    for line in text.splitlines():
        clean = line.strip()
        if not clean.startswith("{") or len(clean) > 1024 * 1024:
            continue
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and str(payload.get("level") or "").lower() in {
            "error",
            "critical",
        }:
            structured_errors += 1
    return not textual_error and structured_errors == 0, structured_errors


def _deployment_backup_paths(
    app_root: Path, previous_contract: object, target_contract: object, *,
    database_schema_reader: Callable[[Sequence[str]], Mapping[str, Sequence[str]]],
) -> tuple[str, ...]:
    previous = _contract_versions(previous_contract)
    target = _contract_versions(target_contract)
    if previous and previous == target:
        return ()
    changed = {name for name in previous.keys() | target.keys() if previous.get(name) != target.get(name)}
    kinds: dict[str, str] = {}
    for contract in (previous_contract, target_contract):
        if not isinstance(contract, dict):
            raise NasReleaseError("schema contract is invalid")
        for item in contract.get("components", []):
            if not isinstance(item, dict) or item.get("kind") not in {"json", "sqlite"}:
                raise NasReleaseError("schema contract kind is invalid")
            kinds[str(item["component"])] = str(item["kind"])
    json_paths = {"app_config": "data/app_config.json", "settings": "data/settings.json"}
    selected = {json_paths[name] for name in changed if name in json_paths}
    if any(kinds.get(name) == "json" and name not in json_paths for name in changed):
        raise NasReleaseError("JSON schema component has no persistent path")
    database_components = {name for name in changed if kinds.get(name) == "sqlite"}
    data_root = app_root / "data"
    if data_root.is_symlink() or not data_root.is_dir():
        raise NasReleaseError("persistent data root is unavailable")
    databases = sorted(data_root.glob("*.sqlite3"))
    found: set[str] = set()
    for database in databases:
        if database.is_symlink() or not database.is_file():
            raise NasReleaseError("persistent database path is unsafe")
    database_schemas = database_schema_reader([path.name for path in databases]) if database_components else {}
    if database_components and set(database_schemas) != {path.name for path in databases}:
        raise NasReleaseError("persistent database schema inspection is incomplete")
    for database in databases:
        names = set(database_schemas.get(database.name, ()))
        affected = names & database_components
        if affected:
            selected.add(f"data/{database.name}")
            found.update(affected)
    # A newly introduced component has no current schema row identifying its
    # database. Preserve the existing SQLite set rather than guessing ownership.
    if database_components - found:
        selected.update(f"data/{database.name}" for database in databases)
    for relative in selected:
        path = app_root / relative
        if path.is_symlink() or not path.is_file():
            raise NasReleaseError("migration backup source is unavailable")
    if changed and not selected:
        raise NasReleaseError("migration has no recoverable persistent baseline")
    return tuple(sorted(selected))


def _journal_preserves_data(journal: Mapping[str, object]) -> bool:
    previous = journal.get("previous")
    if not isinstance(previous, dict) or journal.get("backup_selection") != []:
        return False
    try:
        before = _contract_versions(previous.get("schema_contract"))
        after = _contract_versions(journal.get("target_schema_contract"))
    except NasReleaseError:
        return False
    return bool(before) and before == after


def _contract_versions(contract: object) -> dict[str, int]:
    if not isinstance(contract, dict) or not isinstance(
        contract.get("components"), list
    ):
        raise NasReleaseError("schema contract is invalid")
    result: dict[str, int] = {}
    for item in contract["components"]:
        if not isinstance(item, dict):
            raise NasReleaseError("schema contract component is invalid")
        component = item.get("component")
        version = item.get("current_version")
        if (
            not isinstance(component, str)
            or isinstance(version, bool)
            or not isinstance(version, int)
        ):
            raise NasReleaseError("schema contract component is invalid")
        result[component] = version
    return result


def _contract_can_read(contract: object, data_versions: dict[str, int]) -> bool:
    try:
        readers = _contract_versions(contract)
    except NasReleaseError:
        return False
    return bool(data_versions) and all(
        component in readers and readers[component] >= version
        for component, version in data_versions.items()
    )


def _relative_source_path(value: object) -> str:
    clean = str(value or "").strip().rstrip("/")
    path = PurePosixPath(clean)
    if (
        not clean
        or path.is_absolute()
        or clean != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise NasReleaseError("release archive path is unsafe")
    return clean


def _required_release_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise NasReleaseError(f"release is missing {relative}")
    return path


def _safe_existing_directory(value: object, root: Path, label: str) -> Path:
    path = Path(str(value or ""))
    if path.is_symlink():
        raise NasReleaseError(f"{label} is unsafe")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise NasReleaseError(f"{label} is unsafe") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise NasReleaseError(f"{label} is unsafe")
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _directory(path: Path, label: str, *, create: bool) -> Path:
    clean = Path(path).expanduser()
    if clean.is_symlink():
        raise NasReleaseError(f"{label} is unsafe")
    if create:
        clean.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        resolved = clean.resolve(strict=True)
    except OSError as exc:
        raise NasReleaseError(f"{label} is unavailable") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise NasReleaseError(f"{label} is unsafe")
    return resolved


def _docker_resource_name(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if _DOCKER_RESOURCE_RE.fullmatch(clean) is None:
        raise NasReleaseError(f"{label} name is invalid")
    return clean


def _loopback_origin(value: object) -> str:
    clean = str(value or "").strip().rstrip("/")
    if _LOOPBACK_ORIGIN_RE.fullmatch(clean) is None:
        raise NasReleaseError("acceptance base URL must be a loopback origin")
    port = int(clean.rsplit(":", 1)[1])
    if port > 65_535:
        raise NasReleaseError("acceptance base URL must be a loopback origin")
    return clean


def _revision(value: object) -> str:
    clean = str(value or "").strip()
    if not _REVISION_RE.fullmatch(clean):
        raise NasReleaseError("revision must be a full lowercase Git SHA")
    return clean


def _release_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _RELEASE_ID_RE.fullmatch(clean):
        raise NasReleaseError("release ID is invalid")
    return clean


def _sha256_value(value: object, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise NasReleaseError(f"{label} is invalid")
    return clean


def _image_digest_value(value: object) -> str:
    clean = str(value or "").strip()
    digest = clean.rsplit("@", 1)[-1]
    if not digest.startswith("sha256:") or _SHA256_RE.fullmatch(digest[7:]) is None:
        raise NasReleaseError("image digest is invalid")
    if "@" in clean and not clean.partition("@")[0]:
        raise NasReleaseError("image digest is invalid")
    return clean


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise NasReleaseError("release state file is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NasReleaseError("release state file is invalid") from exc
    if not isinstance(payload, dict):
        raise NasReleaseError("release state file is invalid")
    return payload


def _canonical_json(payload: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NasReleaseError("release state payload is invalid") from exc


def _validate_release_journal(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise NasReleaseError("release operation journal is invalid")
    operation = value.get("operation")
    if operation not in {"deploy", "rollback"}:
        raise NasReleaseError("release operation journal is invalid")
    status = value.get("status")
    # The production incident left exactly this v1 shape. Its missing prior queue
    # state is recovered fail-closed; no other v1 operation remains supported.
    legacy_target_started = (
        value.get("journal_version") == _LEGACY_RELEASE_JOURNAL_VERSION
        and operation == "deploy"
        and status == "target_started"
        and "web_download_queue" not in value
    )
    if (
        value.get("journal_version") != _RELEASE_JOURNAL_VERSION
        and not legacy_target_started
    ):
        raise NasReleaseError("release operation journal version is unsupported")
    deploy_statuses = {
        "initialized",
        "queue_quiesced",
        "tool_published",
        "stopping",
        "stopped",
        "prepared",
        "image_materializing",
        "image_ready",
        "source_published",
        "replacing_source",
        "source_replaced",
        "target_starting",
        "target_started",
        "accepted",
        "manifest_published",
        "current_published",
        "queue_restoring",
        "queue_restored",
    }
    rollback_statuses = {
        "prepared",
        "queue_quiesced",
        "current_backed_up",
        "current_source_saved",
        "applying_target",
        "target_started",
        "accepted",
        "event_published",
        "current_published",
        "queue_restoring",
        "queue_restored",
    }
    allowed_statuses = deploy_statuses if operation == "deploy" else rollback_statuses
    if status not in allowed_statuses:
        raise NasReleaseError("release operation journal status is invalid")
    if not legacy_target_started:
        _release_journal_queue_prior_state(value)
    release_id = _release_id(value.get("release_id"))
    path_keys = (
        ("source_snapshot", "recovery_tool_source")
        if operation == "deploy"
        else ("source_snapshot", "recovery_source")
    )
    for key in path_keys:
        path = value.get(key)
        if (
            not isinstance(path, str)
            or not 1 <= len(path) <= 4096
            or any(character in path for character in ("\x00", "\n", "\r"))
        ):
            raise NasReleaseError("release operation journal path is invalid")
    if operation == "deploy":
        _sha256_value(
            value.get("recovery_tool_sha256"),
            "deployment recovery tool SHA-256",
        )
        backup_path = value.get("backup_path")
        if status in {
            "initialized",
            "queue_quiesced",
            "tool_published",
            "stopping",
            "stopped",
        }:
            if backup_path is not None:
                raise NasReleaseError("release operation journal backup is premature")
        elif backup_path is None:
            if not _journal_preserves_data(value):
                raise NasReleaseError("release operation journal is missing a required migration backup")
        elif (
            not isinstance(backup_path, str)
            or not 1 <= len(backup_path) <= 4096
            or any(character in backup_path for character in ("\x00", "\n", "\r"))
        ):
            raise NasReleaseError("release operation journal path is invalid")
        previous = value.get("previous")
        if not isinstance(previous, dict):
            raise NasReleaseError("release operation journal previous state is invalid")
        _revision(previous.get("revision"))
        _image_digest_value(previous.get("image_digest"))
        if not isinstance(previous.get("image_id"), str):
            raise NasReleaseError("release operation journal image is invalid")
        previous_reference = previous.get("image_reference")
        if previous_reference is not None:
            _immutable_image_reference(previous_reference)
        target_image = value.get("target_image")
        if target_image is not None:
            if not isinstance(target_image, dict) or target_image.get("source") not in {
                "pull",
                "archive",
            }:
                raise NasReleaseError(
                    "release operation journal target image is invalid"
                )
            source_kind = str(target_image["source"])
            target_revision = _revision(target_image.get("revision"))
            compose_reference = _deployment_image_reference(
                target_image.get("compose_reference"),
                revision=target_revision,
            )
            _image_platform(target_image.get("platform"))
            if source_kind == "archive":
                if target_image.get("registry_digest_reference") is not None:
                    raise NasReleaseError(
                        "release operation journal target image is invalid"
                    )
                _sha_tag_reference(compose_reference, target_revision)
                _image_id_value(target_image.get("expected_image_id"))
                _sha256_value(
                    target_image.get("archive_sha256"),
                    "image archive SHA-256",
                )
            else:
                registry_reference = _immutable_image_reference(
                    target_image.get("registry_digest_reference")
                )
                if compose_reference != registry_reference or (
                    target_image.get("expected_image_id") is not None
                    or target_image.get("archive_sha256") is not None
                ):
                    raise NasReleaseError(
                        "release operation journal target image is invalid"
                    )
            image_ready_statuses = deploy_statuses - {
                "initialized",
                "queue_quiesced",
                "tool_published",
                "stopping",
                "stopped",
                "prepared",
                "image_materializing",
            }
            if status in image_ready_statuses:
                _image_id_value(target_image.get("image_id"))
                _image_digest_value(target_image.get("runtime_digest"))
        manifest = value.get("manifest")
        if status in {
            "accepted",
            "manifest_published",
            "current_published",
            "queue_restoring",
            "queue_restored",
        }:
            if (
                not isinstance(manifest, dict)
                or manifest.get("release_id") != release_id
            ):
                raise NasReleaseError("release operation journal manifest is invalid")
        elif manifest is not None:
            raise NasReleaseError("release operation journal manifest is premature")
    else:
        current = value.get("current")
        target = value.get("target")
        if not isinstance(current, dict) or not isinstance(target, dict):
            raise NasReleaseError("release operation journal manifests are invalid")
        _release_id(current.get("release_id"))
        if target.get("release_id") != release_id:
            raise NasReleaseError("release operation journal target is invalid")
        _revision(current.get("revision"))
        _revision(target.get("revision"))
        _image_digest_value(value.get("current_image_digest"))
        _image_digest_value(value.get("target_image_digest"))
        recovery_backup = value.get("recovery_backup")
        if recovery_backup is not None and (
            not isinstance(recovery_backup, str)
            or not 1 <= len(recovery_backup) <= 4096
            or any(character in recovery_backup for character in ("\x00", "\n", "\r"))
        ):
            raise NasReleaseError("release operation journal path is invalid")
        event = value.get("event")
        event_path = value.get("event_path")
        if status in {
            "accepted",
            "event_published",
            "current_published",
            "queue_restoring",
            "queue_restored",
        }:
            if not isinstance(event, dict) or not isinstance(event_path, str):
                raise NasReleaseError("release operation journal event is invalid")
        elif event is not None or event_path is not None:
            raise NasReleaseError("release operation journal event is premature")
    _canonical_json(value)
    return dict(value)


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    completed = False
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
    finally:
        os.close(descriptor)
        if not completed:
            path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_json_exclusive(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - deployment runs on Linux.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_unlink_if_present(path: Path) -> bool:
    if os.name == "nt":  # pragma: no cover - deployment runs on Linux.
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except FileNotFoundError:
        return False
    try:
        try:
            os.unlink(path.name, dir_fd=descriptor)
        except FileNotFoundError:
            removed = False
        else:
            removed = True
        os.fsync(descriptor)
        return removed
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply JAV Pilot releases on a NAS")
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    deploy = subparsers.add_parser("deploy")
    deploy.add_argument("--archive", type=Path, required=True)
    deploy.add_argument("--archive-sha256", required=True)
    deploy.add_argument("--revision", required=True)
    image_source = deploy.add_mutually_exclusive_group(required=True)
    image_source.add_argument("--image-reference")
    image_source.add_argument("--image-archive", type=Path)
    deploy.add_argument("--image-manifest", type=Path)
    deploy.add_argument("--image-archive-filename")
    deploy.add_argument("--image-platform")
    deploy.add_argument("--apply", action="store_true")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        manager = NasReleaseManager(
            ReleasePaths.validated(args.app_root, args.backup_root, args.state_root)
        )
        if args.command == "deploy":
            payload = manager.deploy(
                args.archive,
                revision=args.revision,
                archive_sha256=args.archive_sha256,
                image_reference=args.image_reference,
                image_platform=args.image_platform,
                image_archive_path=args.image_archive,
                image_manifest_path=args.image_manifest,
                image_archive_filename=args.image_archive_filename,
                apply=args.apply,
            )
        else:
            payload = manager.rollback(args.release_id, apply=args.apply)
        print(json.dumps(payload, sort_keys=True))
        return 0
    except (NasReleaseError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
