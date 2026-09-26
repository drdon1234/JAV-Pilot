from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jav_pilot.config.schema import (  # noqa: E402
    APP_CONFIG_SCHEMA_VERSION,
    SETTINGS_SCHEMA_VERSION,
)
from tools.prepare_compose import browser_profile_command  # noqa: E402
from ops.nas_release import (  # noqa: E402
    REQUIRED_MOUNTS,
    CommandResult,
    NasReleaseError,
    NasReleaseManager,
    ReleasePaths,
    _extract_release_archive,
    _http_json,
    _mount_map,
    _release_environment,
    _sha256,
    _sqlite_integrity,
)
from tools.image_bundle_manifest import create_manifest, write_manifest  # noqa: E402


REPORT_FORMAT_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 40 * 60
MIN_TIMEOUT_SECONDS = 10 * 60
MAX_TIMEOUT_SECONDS = 60 * 60
COMMAND_TIMEOUT_SECONDS = 15 * 60
CLEANUP_TIMEOUT_SECONDS = 5 * 60
RESOURCE_LABEL = "com.jav-pilot.release-drill"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HEX_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPECTED_WORKER_CHECKS = frozenset(
    {
        "site_diagnostic_scheduler",
        "media_library_manager",
        "metadata_manager",
        "web_download_manager",
        "web_batch_manager",
    }
)
_EXPECTED_DATABASES = frozenset(
    {
        "media_library.sqlite3",
        "media_metadata.sqlite3",
        "site_diagnostics.sqlite3",
        "web_downloads.sqlite3",
    }
)


class ReleaseDrillError(RuntimeError):
    pass


@dataclass(frozen=True)
class DrillResources:
    token: str
    project: str
    container: str
    network: str
    repository: str
    browser_profile_volume: str

    @classmethod
    def from_run_id(cls, value: object) -> DrillResources:
        clean = str(value or "").strip()
        if _RUN_ID_RE.fullmatch(clean) is None:
            raise ReleaseDrillError("release drill run ID is invalid")
        token = hashlib.sha256(clean.encode("ascii")).hexdigest()[:16]
        prefix = f"jav-pilot-drill-{token}"
        return cls(
            token=token,
            project=prefix,
            container=prefix,
            network=f"{prefix}-net",
            repository=f"{prefix}-image",
            browser_profile_volume=f"{prefix}-browser-profile",
        )


@dataclass(frozen=True)
class DrillLayout:
    root: Path
    app: Path
    backups: Path
    state: Path
    qb_downloads: Path
    web_downloads: Path
    media: Path

    @classmethod
    def create(cls, root: Path) -> DrillLayout:
        resolved = Path(root).resolve(strict=True)
        paths = {
            "app": resolved / "app",
            "backups": resolved / "release-backups",
            "state": resolved / "release-state",
            "qb_downloads": resolved / "mounts" / "qb",
            "web_downloads": resolved / "mounts" / "web",
            "media": resolved / "mounts" / "media",
        }
        # The release extractor deliberately requires a nonexistent destination
        # so it can reject symlinks and pre-seeded content before writing.  Keep
        # the app slot reserved by its private temporary parent, but create only
        # the independent state and mount roots here.
        for name, path in paths.items():
            if name == "app":
                continue
            path.mkdir(parents=True, mode=0o700)
            _require_within(path, resolved)
        return cls(resolved, **paths)

    def expected_mounts(self) -> dict[str, str]:
        result = {
            "/app/data": str((self.app / "data").resolve(strict=True)),
            "/downloads/jav": str(self.qb_downloads.resolve(strict=True)),
            "/downloads/jav-web": str(self.web_downloads.resolve(strict=True)),
            "/media/JAV": str(self.media.resolve(strict=True)),
            "/app/maintenance-locks": str(
                (self.app / "runtime" / "maintenance-locks").resolve(strict=True)
            ),
        }
        for source in result.values():
            _require_within(Path(source), self.root)
        if set(result) != set(REQUIRED_MOUNTS) | {"/app/maintenance-locks"}:
            raise ReleaseDrillError("release drill mount contract is incomplete")
        return result


@dataclass(frozen=True)
class DrillImageBundle:
    archive: Path
    manifest: Path
    identity: DrillImageIdentity


@dataclass(frozen=True)
class DrillImageIdentity:
    image_id: str
    image_digest: str
    revision: str
    reference: str
    platform: str


@dataclass
class DrillBackupCleanup:
    image: DrillImageIdentity | None = None


class BoundedRunner:
    def __init__(
        self,
        *,
        deadline: float | None,
        command_timeout: float = COMMAND_TIMEOUT_SECONDS,
        docker_resource_token: str | None = None,
    ) -> None:
        self.deadline = deadline
        self.command_timeout = command_timeout
        self.docker_resource_token = docker_resource_token

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        timeout = self.command_timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseDrillError("release drill deadline exceeded")
            timeout = min(timeout, remaining)
        command = _label_docker_run(argv, self.docker_resource_token)
        process_environment = os.environ.copy()
        if env:
            process_environment.update(env)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=process_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=max(1.0, timeout),
            )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseDrillError("release drill command timed out") from exc
        result = CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
        if check and result.returncode != 0:
            executable = Path(command[0]).name if command else "command"
            raise ReleaseDrillError(
                f"{executable} failed with status {result.returncode}"
            )
        return result


@contextmanager
def _managed_drill_backups(
    layout: DrillLayout,
    resources: DrillResources,
) -> Iterator[DrillBackupCleanup]:
    cleanup = DrillBackupCleanup()
    active_error: BaseException | None = None
    try:
        yield cleanup
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        if cleanup.image is not None:
            try:
                _cleanup_drill_backups(layout, resources, cleanup.image)
            except BaseException as cleanup_error:
                if active_error is None:
                    raise
                active_error.add_note(
                    "release drill backup cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )


def run_release_drill(
    repo: Path,
    *,
    run_id: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    timeout = _bounded_timeout(timeout_seconds)
    repository = _repository_root(repo)
    resources = DrillResources.from_run_id(run_id)
    deadline = time.monotonic() + timeout
    runner = BoundedRunner(
        deadline=deadline,
        docker_resource_token=resources.token,
    )
    _require_docker(runner)
    cleanup_release_drill(resources, runner=runner)
    _require_zstd(runner)

    with tempfile.TemporaryDirectory(prefix=f"{resources.project}-") as temporary:
        layout = DrillLayout.create(Path(temporary))
        archive = layout.root / "release.tar"
        head = _create_git_archive(repository, archive, runner)
        archive_hash = _sha256(archive)
        revisions = _drill_revisions(head)
        _extract_release_archive(
            archive,
            layout.app,
            expected_sha256=archive_hash,
        )
        _seed_application_root(layout.app)
        port = _loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = _drill_environment(layout, resources, port)
        for source in (
            layout.app,
            layout.backups,
            layout.state,
            layout.qb_downloads,
            layout.web_downloads,
            layout.media,
        ):
            _require_within(source, layout.root)

        with (
            _managed_drill_backups(layout, resources) as backup_cleanup,
            _temporary_environment(environment),
        ):
            paths = ReleasePaths.validated(layout.app, layout.backups, layout.state)
            manager = NasReleaseManager(
                paths,
                runner=runner,
                container_name=resources.container,
                compose_service="jav-pilot",
                browser_profile_volume=resources.browser_profile_volume,
                base_url=base_url,
            )
            expected_mounts = layout.expected_mounts()
            phases: list[dict[str, object]] = []
            initial_environment = _drill_initial_release_environment(
                layout,
                resources,
                revisions["current"],
            )
            manager._validate_compose(
                layout.app,
                initial_environment,
                allow_loopback_lan=True,
            )
            _validate_drill_compose(
                manager,
                initial_environment,
                layout,
                resources,
            )
            current_identity = _build_drill_image(
                manager,
                layout,
                resources,
                revisions["current"],
                runner,
            )
            backup_cleanup.image = current_identity
            _prepare_browser_profile_volume(
                resources,
                current_identity,
                _host_container_user(),
                runner,
            )
            manager._compose(
                ["up", "-d", "--no-build", "--force-recreate"],
                environment=initial_environment,
            )
            phases.append(
                _verify_phase(
                    manager,
                    "current",
                    revisions["current"],
                    expected_mounts,
                    layout,
                    current_identity,
                )
            )

            first_bundle = _build_drill_image_bundle(
                manager,
                layout,
                resources,
                revisions["upgrade_1"],
                runner,
            )
            first = manager.deploy(
                archive,
                revision=revisions["upgrade_1"],
                archive_sha256=archive_hash,
                image_archive_path=first_bundle.archive,
                image_manifest_path=first_bundle.manifest,
                image_archive_filename=first_bundle.archive.name,
                apply=True,
            )
            first_release = _release_id_from_result(first)
            phases.append(
                _verify_phase(
                    manager,
                    "upgrade_1",
                    revisions["upgrade_1"],
                    expected_mounts,
                    layout,
                    first_bundle.identity,
                )
            )

            second_bundle = _build_drill_image_bundle(
                manager,
                layout,
                resources,
                revisions["upgrade_2"],
                runner,
            )
            second = manager.deploy(
                archive,
                revision=revisions["upgrade_2"],
                archive_sha256=archive_hash,
                image_archive_path=second_bundle.archive,
                image_manifest_path=second_bundle.manifest,
                image_archive_filename=second_bundle.archive.name,
                apply=True,
            )
            second_release = _release_id_from_result(second)
            if second_release == first_release:
                raise ReleaseDrillError("immutable release IDs did not advance")
            phases.append(
                _verify_phase(
                    manager,
                    "upgrade_2",
                    revisions["upgrade_2"],
                    expected_mounts,
                    layout,
                    second_bundle.identity,
                )
            )

            rollback = manager.rollback(first_release, apply=True)
            if (
                rollback.get("ok") is not True
                or rollback.get("release_id") != first_release
            ):
                raise ReleaseDrillError(
                    "release rollback did not select the previous release"
                )
            phases.append(
                _verify_phase(
                    manager,
                    "rollback",
                    revisions["upgrade_1"],
                    expected_mounts,
                    layout,
                    first_bundle.identity,
                )
            )
            current = manager._load_current_manifest(required=True)
            if (
                current.get("release_id") != first_release
                or current.get("revision") != revisions["upgrade_1"]
            ):
                raise ReleaseDrillError("release state did not converge after rollback")
            return {
                "format_version": REPORT_FORMAT_VERSION,
                "ok": True,
                "run_token": resources.token,
                "archive_sha256": archive_hash,
                "revisions": revisions,
                "release_ids": {
                    "upgrade_1": first_release,
                    "upgrade_2": second_release,
                    "final": first_release,
                },
                "phases": phases,
            }


def _cleanup_drill_backups(
    layout: DrillLayout,
    resources: DrillResources,
    image: DrillImageIdentity,
) -> None:
    root = layout.root.resolve(strict=True)
    backups = layout.backups
    prefix = f"{resources.project}-"
    if (
        root.parent != Path(tempfile.gettempdir()).resolve(strict=True)
        or not root.name.startswith(prefix)
        or len(root.name) <= len(prefix)
        or backups != root / "release-backups"
        or backups.is_symlink()
        or not backups.is_dir()
        or _IMAGE_ID_RE.fullmatch(image.image_id) is None
        or _REVISION_RE.fullmatch(image.revision) is None
        or image.reference != f"{resources.repository}:{image.revision}"
        or image.platform != "linux/amd64"
    ):
        raise ReleaseDrillError("release drill backup cleanup is unsafe")
    _require_within(backups, root)
    before = backups.lstat()
    source = str(backups)
    if any(character in source for character in ("\r", "\n", ",")):
        raise ReleaseDrillError("release drill backup mount is unsafe")
    script = "\n".join(
        (
            "import shutil",
            "from pathlib import Path",
            "root = Path('/release-backups')",
            "for child in tuple(root.iterdir()):",
            "    if child.is_dir() and not child.is_symlink():",
            "        shutil.rmtree(child)",
            "    else:",
            "        child.unlink()",
            "if any(root.iterdir()):",
            "    raise RuntimeError('backup cleanup is incomplete')",
        )
    )
    runner = BoundedRunner(
        deadline=time.monotonic() + CLEANUP_TIMEOUT_SECONDS,
        command_timeout=CLEANUP_TIMEOUT_SECONDS,
    )
    _require_docker(runner)
    _stop_drill_containers(resources, runner)
    try:
        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                f"{resources.container}-backup-cleanup",
                "--label",
                f"{RESOURCE_LABEL}={resources.token}",
                "--entrypoint",
                "python",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges:true",
                "--network",
                "none",
                "--read-only",
                "--pids-limit",
                "32",
                "--memory",
                "128m",
                "--mount",
                f"type=bind,source={source},target=/release-backups",
                image.image_id,
                "-B",
                "-c",
                script,
            ]
        )
    finally:
        _stop_drill_containers(resources, runner)
    after = backups.lstat()
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or backups.is_symlink()
        or not backups.is_dir()
        or any(backups.iterdir())
    ):
        raise ReleaseDrillError("release drill backup cleanup is incomplete")


def _stop_drill_containers(
    resources: DrillResources,
    runner: BoundedRunner,
) -> None:
    filters = (
        f"label=com.docker.compose.project={resources.project}",
        f"label={RESOURCE_LABEL}={resources.token}",
    )
    containers = [resources.container]
    for value in filters:
        containers.extend(
            _docker_lines(runner, ["docker", "ps", "-aq", "--filter", value])
        )
    for container in dict.fromkeys(containers):
        runner.run(["docker", "rm", "-f", container], check=False)
    if any(
        _docker_lines(runner, ["docker", "ps", "-aq", "--filter", value])
        for value in filters
    ):
        raise ReleaseDrillError("release drill container cleanup is incomplete")


def cleanup_release_drill(
    resources: DrillResources,
    *,
    runner: BoundedRunner | None = None,
) -> dict[str, object]:
    active_runner = runner or BoundedRunner(
        deadline=time.monotonic() + CLEANUP_TIMEOUT_SECONDS,
        command_timeout=60.0,
    )
    _require_docker(active_runner)
    container_ids = _docker_lines(
        active_runner,
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={resources.project}",
        ],
    )
    container_ids.extend(
        _docker_lines(
            active_runner,
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={RESOURCE_LABEL}={resources.token}",
            ],
        )
    )
    container_ids.append(resources.container)
    for container in dict.fromkeys(container_ids):
        active_runner.run(["docker", "rm", "-f", container], check=False)
    remaining_named_containers = _docker_lines(
        active_runner,
        [
            "docker",
            "ps",
            "-a",
            "--format",
            "{{.Names}}",
            "--filter",
            f"name={resources.container}",
        ],
    )
    if resources.container in remaining_named_containers:
        raise ReleaseDrillError("release drill container cleanup is incomplete")

    volume_ids = _docker_lines(
        active_runner,
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={resources.project}",
        ],
    )
    volume_ids.extend(
        _docker_lines(
            active_runner,
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label={RESOURCE_LABEL}={resources.token}",
            ],
        )
    )
    volume_ids.append(resources.browser_profile_volume)
    for volume in dict.fromkeys(volume_ids):
        active_runner.run(["docker", "volume", "rm", "-f", volume], check=False)
    remaining_named_volumes = _docker_lines(
        active_runner,
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"name={resources.browser_profile_volume}",
        ],
    )
    if resources.browser_profile_volume in remaining_named_volumes:
        raise ReleaseDrillError("release drill browser volume cleanup is incomplete")

    network_ids = _docker_lines(
        active_runner,
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label={RESOURCE_LABEL}={resources.token}",
        ],
    )
    network_ids.append(resources.network)
    for network in dict.fromkeys(network_ids):
        active_runner.run(["docker", "network", "rm", network], check=False)
    remaining_named_networks = _docker_lines(
        active_runner,
        [
            "docker",
            "network",
            "ls",
            "--format",
            "{{.Name}}",
            "--filter",
            f"name={resources.network}",
        ],
    )
    if resources.network in remaining_named_networks:
        raise ReleaseDrillError("release drill network cleanup is incomplete")

    image_refs = _docker_lines(
        active_runner,
        [
            "docker",
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
            resources.repository,
        ],
    )
    for image in image_refs:
        if image.startswith(f"{resources.repository}:"):
            active_runner.run(["docker", "image", "rm", "-f", image], check=False)

    leftover_containers = _docker_lines(
        active_runner,
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={resources.project}",
        ],
    )
    leftover_containers.extend(
        _docker_lines(
            active_runner,
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={RESOURCE_LABEL}={resources.token}",
            ],
        )
    )
    leftover_volumes = _docker_lines(
        active_runner,
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={resources.project}",
        ],
    )
    leftover_volumes.extend(
        _docker_lines(
            active_runner,
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label={RESOURCE_LABEL}={resources.token}",
            ],
        )
    )
    leftovers = {
        "containers": list(dict.fromkeys(leftover_containers)),
        "volumes": list(dict.fromkeys(leftover_volumes)),
        "networks": _docker_lines(
            active_runner,
            [
                "docker",
                "network",
                "ls",
                "-q",
                "--filter",
                f"label={RESOURCE_LABEL}={resources.token}",
            ],
        ),
        "images": _docker_lines(
            active_runner,
            [
                "docker",
                "image",
                "ls",
                "-q",
                resources.repository,
            ],
        ),
    }
    if any(leftovers.values()):
        raise ReleaseDrillError("release drill Docker cleanup is incomplete")
    return {"ok": True, "run_token": resources.token}


def _verify_phase(
    manager: NasReleaseManager,
    name: str,
    revision: str,
    expected_mounts: dict[str, str],
    layout: DrillLayout,
    expected_image: DrillImageIdentity,
) -> dict[str, object]:
    acceptance = manager._acceptance(
        revision,
        expected_mounts=expected_mounts,
        expected_container_user=_host_container_user(),
        expected_image_id=expected_image.image_id,
        expected_image_digest=expected_image.image_digest,
        expected_oci_revision=expected_image.revision,
        expected_image_reference=expected_image.reference,
        expected_image_platform=expected_image.platform,
    )
    if acceptance.get("ok") is not True or acceptance.get("revision") != revision:
        raise ReleaseDrillError("release phase acceptance failed")
    for key in (
        "mounts_ok",
        "init",
        "restart_count_zero",
        "browser",
        "logs_clean",
        "processes_clean",
    ):
        if acceptance.get(key) is not True:
            raise ReleaseDrillError("release phase acceptance is incomplete")
    checks = acceptance.get("readiness_checks")
    if not isinstance(checks, dict) or not _EXPECTED_WORKER_CHECKS.issubset(checks):
        raise ReleaseDrillError("release phase worker checks are incomplete")
    if any(checks[key] is not True for key in _EXPECTED_WORKER_CHECKS):
        raise ReleaseDrillError("release phase worker is unavailable")
    mounts = acceptance.get("mounts")
    if not isinstance(mounts, dict):
        raise ReleaseDrillError("release phase mount report is invalid")
    for destination, source in expected_mounts.items():
        if mounts.get(destination) != source:
            raise ReleaseDrillError("release phase mount source changed")
        _require_within(Path(source), layout.root)
    sqlite_checks = _sqlite_integrity(layout.app / "data")
    if not _EXPECTED_DATABASES.issubset(sqlite_checks):
        raise ReleaseDrillError("release phase SQLite set is incomplete")
    if any(sqlite_checks[name] != "ok" for name in _EXPECTED_DATABASES):
        raise ReleaseDrillError("release phase SQLite integrity failed")
    container = manager._container_state(required=True)
    inspected_mounts = _mount_map(container)
    if any(
        inspected_mounts.get(key) != value for key, value in expected_mounts.items()
    ):
        raise ReleaseDrillError("release phase Docker mount inspection failed")
    health = _http_json(f"{manager.base_url}/healthz")
    readiness = _http_json(f"{manager.base_url}/readyz")
    if health.get("revision") != revision or readiness.get("ok") is not True:
        raise ReleaseDrillError("release phase endpoint verification failed")
    return {
        "name": name,
        "revision": revision,
        "healthy": acceptance.get("health") is True,
        "ready": acceptance.get("readiness") is True,
        "mounts": {key: True for key in sorted(expected_mounts)},
        "sqlite": {name: sqlite_checks[name] for name in sorted(_EXPECTED_DATABASES)},
        "workers": {key: checks[key] for key in sorted(_EXPECTED_WORKER_CHECKS)},
        "browser": True,
        "processes_clean": True,
        "image_id": acceptance.get("image_id"),
        "image_digest": acceptance.get("image_digest"),
        "image_reference": acceptance.get("image_reference"),
        "image_platform": acceptance.get("image_platform"),
    }


def _drill_environment(
    layout: DrillLayout,
    resources: DrillResources,
    port: int,
) -> dict[str, str]:
    return {
        "COMPOSE_PROJECT_NAME": resources.project,
        "JAV_PILOT_COMPOSE_CONTAINER_NAME": resources.container,
        "JAV_PILOT_COMPOSE_HOST_BIND": "127.0.0.1",
        "JAV_PILOT_COMPOSE_LAN_BIND": "127.0.0.2",
        "JAV_PILOT_COMPOSE_HOST_PORT": str(port),
        "JAV_PILOT_COMPOSE_NETWORK": resources.network,
        "JAV_PILOT_COMPOSE_BROWSER_PROFILE_VOLUME": (
            resources.browser_profile_volume
        ),
        "JAV_PILOT_IMAGE_REPOSITORY": resources.repository,
        "JAV_PILOT_QB_STAGING_HOST_PATH": str(layout.qb_downloads),
        "JAV_PILOT_WEB_DOWNLOAD_HOST_PATH": str(layout.web_downloads),
        "JAV_PILOT_LIBRARY_HOST_PATH": str(layout.media),
        "JAV_PILOT_HISTORY_BACKUP_HOST_PATH": str(layout.backups),
        "JAV_PILOT_AUTH_ENABLED": "0",
        "JAV_PILOT_ALLOW_INSECURE_REMOTE": "1",
        "JAV_PILOT_AUTH_USERNAME": "",
        "JAV_PILOT_AUTH_PASSWORD": "",
        "JAV_PILOT_AUTH_SECRET": "",
        "JAV_PILOT_WEB_DOWNLOAD_ENABLED": "1",
        "JAV_PILOT_MEDIA_METADATA_ENABLED": "1",
        "JAV_PILOT_MEDIA_LIBRARY_ENABLED": "1",
        "JAV_PILOT_WEB_DOWNLOAD_MIN_FREE_BYTES": str(64 * 1024 * 1024),
        "JAV_PILOT_WEB_DOWNLOAD_INITIAL_MEDIA_BYTES": str(16 * 1024 * 1024),
        "JAV_PILOT_WEB_DOWNLOAD_MAX_FILE_BYTES": str(1024 * 1024 * 1024),
        "JAV_PILOT_READINESS_MIN_FREE_BYTES": str(16 * 1024 * 1024),
        "JAV_PILOT_SITE_DIAGNOSTIC_INTERVAL_SECONDS": "86400",
        "JAV_PILOT_SITE_DIAGNOSTIC_MAX_INTERVAL_SECONDS": "86400",
        "JAV_PILOT_HISTORY_BACKUP_ROOT": "/app/backups",
        "JAV_PILOT_HISTORY_BACKUP_PATH": "",
        "JAV_PILOT_CPU_LIMIT": "2.0",
        "JAV_PILOT_MEMORY_LIMIT": "3g",
        "JAV_PILOT_PIDS_LIMIT": "512",
        "JAV_PILOT_CONTAINER_USER": _host_container_user(),
        "JAV_PILOT_QB_URL": "",
        "JAV_PILOT_QB_USERNAME": "",
        "JAV_PILOT_QB_PASSWORD": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
        "ALL_PROXY": os.environ.get("ALL_PROXY", ""),
    }


def _host_container_user() -> str:
    uid_getter = getattr(os, "getuid", None)
    gid_getter = getattr(os, "getgid", None)
    if callable(uid_getter) and callable(gid_getter):
        uid = int(uid_getter())
        gid = int(gid_getter())
        if uid > 0 and gid > 0:
            return f"{uid}:{gid}"
    uid_text = str(os.environ.get("UID") or "1000").strip()
    if re.fullmatch(r"[1-9][0-9]{0,9}", uid_text) is None:
        uid_text = "1000"
    return f"{uid_text}:1000"


def _drill_initial_release_environment(
    layout: DrillLayout,
    resources: DrillResources,
    revision: str,
) -> dict[str, str]:
    return _release_environment(
        revision,
        mounts=layout.expected_mounts(),
        backup_root=layout.backups,
        image_repository=resources.repository,
    )


def _build_drill_image(
    manager: NasReleaseManager,
    layout: DrillLayout,
    resources: DrillResources,
    revision: str,
    runner: BoundedRunner,
) -> DrillImageIdentity:
    if _REVISION_RE.fullmatch(revision) is None:
        raise ReleaseDrillError("release drill image revision is invalid")
    environment = _drill_initial_release_environment(
        layout,
        resources,
        revision,
    )
    manager._validate_compose(
        layout.app,
        environment,
        allow_loopback_lan=True,
    )
    _validate_drill_compose(manager, environment, layout, resources)
    manager._compose(["build"], environment=environment)
    return _inspect_drill_image(
        runner,
        f"{resources.repository}:{revision}",
        revision,
    )


def _build_drill_image_bundle(
    manager: NasReleaseManager,
    layout: DrillLayout,
    resources: DrillResources,
    revision: str,
    runner: BoundedRunner,
) -> DrillImageBundle:
    image_reference = f"{resources.repository}:{revision}"
    identity = _build_drill_image(manager, layout, resources, revision, runner)

    bundle_root = layout.root / "image-bundles" / revision
    bundle_root.mkdir(parents=True, mode=0o700)
    _require_within(bundle_root, layout.root)
    raw_archive = bundle_root / "image.tar"
    archive = bundle_root / f"jav-pilot-{revision}-linux-amd64.tar.zst"
    manifest = bundle_root / "manifest.json"
    runner.run(
        [
            "docker",
            "image",
            "save",
            "--output",
            str(raw_archive),
            image_reference,
        ]
    )
    if (
        raw_archive.is_symlink()
        or not raw_archive.is_file()
        or raw_archive.stat().st_size <= 0
    ):
        raise ReleaseDrillError("release drill image archive is invalid")
    runner.run(
        [
            "zstd",
            "--quiet",
            "--force",
            "--threads=0",
            "-10",
            "-o",
            str(archive),
            str(raw_archive),
        ]
    )
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size <= 0:
        raise ReleaseDrillError("release drill compressed image archive is invalid")
    write_manifest(
        manifest,
        create_manifest(
            revision=revision,
            sha_tag=image_reference,
            image_id=identity.image_id,
            platform=identity.platform,
            archive=archive,
        ),
    )
    runner.run(["docker", "image", "rm", image_reference])
    return DrillImageBundle(archive=archive, manifest=manifest, identity=identity)


def _inspect_drill_image(
    runner: BoundedRunner,
    image_reference: str,
    revision: str,
) -> DrillImageIdentity:
    inspected = runner.run(["docker", "image", "inspect", image_reference]).stdout
    try:
        payload = json.loads(inspected.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseDrillError("release drill image inspection is invalid") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise ReleaseDrillError("release drill image inspection is invalid")
    image = payload[0]
    image_id = image.get("Id")
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    platform = _drill_image_platform(image)
    if (
        not isinstance(image_id, str)
        or _IMAGE_ID_RE.fullmatch(image_id) is None
        or platform != "linux/amd64"
        or not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != revision
    ):
        raise ReleaseDrillError("release drill image identity is invalid")
    return DrillImageIdentity(
        image_id=image_id,
        # A drill image is transferred through ``docker save``/``load`` and
        # has no stable registry identity.  Local RepoDigests are optional and
        # are not preserved by Docker across that boundary, while the content
        # addressed image ID is.  Use the latter for every phase comparison.
        image_digest=image_id,
        revision=revision,
        reference=image_reference,
        platform=platform,
    )


def _drill_image_platform(image: dict[str, object]) -> str:
    os_name = str(image.get("Os") or "").strip().lower()
    architecture = str(image.get("Architecture") or "").strip().lower()
    variant = str(image.get("Variant") or "").strip().lower()
    if not os_name or not architecture:
        return ""
    return f"{os_name}/{architecture}{f'/{variant}' if variant else ''}"


def _prepare_browser_profile_volume(
    resources: DrillResources,
    image: DrillImageIdentity,
    user: str,
    runner: BoundedRunner,
) -> None:
    if re.fullmatch(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", user) is None:
        raise ReleaseDrillError("release drill container user is invalid")
    runner.run(
        [
            "docker",
            "volume",
            "create",
            "--label",
            f"{RESOURCE_LABEL}={resources.token}",
            resources.browser_profile_volume,
        ]
    )
    result = runner.run(
        [
            "docker",
            "volume",
            "inspect",
            resources.browser_profile_volume,
        ]
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseDrillError("release drill browser volume is invalid") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise ReleaseDrillError("release drill browser volume is invalid")
    volume = payload[0]
    labels = volume.get("Labels")
    if (
        volume.get("Name") != resources.browser_profile_volume
        or not isinstance(labels, dict)
        or labels.get(RESOURCE_LABEL) != resources.token
    ):
        raise ReleaseDrillError("release drill browser volume escaped isolation")
    runner.run(browser_profile_command(image.image_id, resources.browser_profile_volume, user))


def _validate_drill_compose(
    manager: NasReleaseManager,
    environment: dict[str, str],
    layout: DrillLayout,
    resources: DrillResources,
) -> None:
    result = manager._compose(
        ["config", "--format", "json"],
        environment=environment,
    )
    try:
        model = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseDrillError("release drill Compose model is invalid") from exc
    expected_port = int(manager.base_url.rsplit(":", 1)[1])
    _validate_drill_compose_model(
        model,
        layout,
        resources,
        expected_port=expected_port,
    )


def _validate_drill_compose_model(
    model: object,
    layout: DrillLayout,
    resources: DrillResources,
    *,
    expected_port: int,
) -> None:
    if not isinstance(model, dict):
        raise ReleaseDrillError("release drill Compose model is invalid")
    services = model.get("services")
    if not isinstance(services, dict) or set(services) != {"jav-pilot"}:
        raise ReleaseDrillError("release drill Compose service set is invalid")
    service = services.get("jav-pilot") if isinstance(services, dict) else None
    if (
        not isinstance(service, dict)
        or service.get("configs")
        or service.get("secrets")
    ):
        raise ReleaseDrillError("release drill Compose service mounts are invalid")
    ports = service.get("ports")
    if not isinstance(ports, list) or len(ports) != 2:
        raise ReleaseDrillError("release drill Compose port is invalid")
    normalized_ports: set[tuple[str, str, int]] = set()
    for port in ports:
        if (
            not isinstance(port, dict)
            or port.get("host_ip") not in {"127.0.0.1", "127.0.0.2"}
            or port.get("published") != str(expected_port)
            or port.get("target") != 8766
            or port.get("protocol") != "tcp"
            or port.get("mode") != "ingress"
        ):
            raise ReleaseDrillError("release drill Compose port escaped isolation")
        normalized_ports.add(
            (str(port["host_ip"]), str(port["published"]), int(port["target"]))
        )
    if len(normalized_ports) != len(ports):
        raise ReleaseDrillError("release drill Compose port is invalid")
    if {port["host_ip"] for port in ports} != {"127.0.0.1", "127.0.0.2"}:
        raise ReleaseDrillError("release drill Compose port is invalid")
    service_networks = service.get("networks")
    networks = model.get("networks")
    network = (
        networks.get("jav-pilot") if isinstance(networks, dict) else None
    )
    if (
        not isinstance(service_networks, dict)
        or service_networks != {"jav-pilot": None}
        or not isinstance(networks, dict)
        or set(networks) != {"jav-pilot"}
        or not isinstance(network, dict)
        or network.get("driver") != "bridge"
        or network.get("external") not in (None, False)
        or network.get("name") != resources.network
    ):
        raise ReleaseDrillError("release drill Compose network escaped isolation")
    raw_mounts = service.get("volumes") if isinstance(service, dict) else None
    if not isinstance(raw_mounts, list):
        raise ReleaseDrillError("release drill Compose mounts are invalid")
    mounts: dict[str, dict[str, object]] = {}
    for raw_mount in raw_mounts:
        if not isinstance(raw_mount, dict):
            raise ReleaseDrillError("release drill Compose mount is invalid")
        target = raw_mount.get("target")
        if not isinstance(target, str) or target in mounts:
            raise ReleaseDrillError("release drill Compose mount target is invalid")
        mounts[target] = raw_mount

    expected_binds = {
        **layout.expected_mounts(),
        "/app/backups": str(layout.backups.resolve(strict=True)),
    }
    expected_targets = {*expected_binds, "/app/browser-profile"}
    if set(mounts) != expected_targets:
        raise ReleaseDrillError("release drill Compose mount target set is invalid")
    for target, expected_source in expected_binds.items():
        mount = mounts.get(target, {})
        read_only = mount.get("read_only") is True
        if (
            mount.get("type") != "bind"
            or mount.get("source") != expected_source
            or read_only != (target == "/app/backups")
        ):
            raise ReleaseDrillError("release drill Compose bind mount escaped isolation")
        _require_within(Path(expected_source), layout.root)

    profile_mount = mounts.get("/app/browser-profile", {})
    profile_source = profile_mount.get("source")
    if (
        profile_mount.get("type") != "volume"
        or profile_mount.get("read_only") is True
        or not isinstance(profile_source, str)
    ):
        raise ReleaseDrillError("release drill browser volume mount is invalid")
    volumes = model.get("volumes")
    volume = volumes.get(profile_source) if isinstance(volumes, dict) else None
    if isinstance(volumes, dict) and any(
        isinstance(item, dict)
        and item.get("name") == "jav-pilot-browser-profile"
        for item in volumes.values()
    ):
        raise ReleaseDrillError("release drill Compose references production volume")
    if (
        not isinstance(volume, dict)
        or volume.get("name") != resources.browser_profile_volume
        or resources.browser_profile_volume == "jav-pilot-browser-profile"
    ):
        raise ReleaseDrillError("release drill browser volume escaped isolation")


def _seed_application_root(app: Path) -> None:
    data = app / "data"
    data.mkdir(mode=0o700)
    lock_root = app / "runtime" / "maintenance-locks"
    lock_root.mkdir(parents=True, mode=0o770)
    lock_root.chmod(0o770)
    lock = lock_root / ".jav-pilot-maintenance.lock"
    lock.touch(mode=0o660, exist_ok=False)
    lock.chmod(0o660)
    (app / ".env").write_text(
        "JAV_PILOT_AUTH_ENABLED=0\nJAV_PILOT_ALLOW_INSECURE_REMOTE=1\n",
        encoding="ascii",
        newline="\n",
    )
    # The application intentionally treats absent JSON config files as
    # defaults and does not persist them during startup.  Release backups,
    # however, require both files to be present and schema-versioned.  Seed
    # only the version markers so the drill remains credential-free while the
    # normal runtime loaders expand them to their defaults.
    for filename, schema_version in (
        ("app_config.json", APP_CONFIG_SCHEMA_VERSION),
        ("settings.json", SETTINGS_SCHEMA_VERSION),
    ):
        (data / filename).write_text(
            json.dumps({"schema_version": schema_version}, indent=2) + "\n",
            encoding="ascii",
            newline="\n",
        )


def _create_git_archive(repo: Path, destination: Path, runner: BoundedRunner) -> str:
    revision_result = runner.run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    try:
        revision = revision_result.stdout.decode("ascii").strip().lower()
    except UnicodeDecodeError as exc:
        raise ReleaseDrillError("Git revision output is invalid") from exc
    if _REVISION_RE.fullmatch(revision) is None:
        raise ReleaseDrillError("Git revision output is invalid")
    archive_result = runner.run(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"]
    )
    if not archive_result.stdout:
        raise ReleaseDrillError("Git release archive is empty")
    destination.write_bytes(archive_result.stdout)
    return revision


def _drill_revisions(current: str) -> dict[str, str]:
    revisions = {
        "current": current,
        "upgrade_1": hashlib.sha256(f"{current}:upgrade:1".encode()).hexdigest()[:40],
        "upgrade_2": hashlib.sha256(f"{current}:upgrade:2".encode()).hexdigest()[:40],
    }
    if len(set(revisions.values())) != len(revisions):
        raise ReleaseDrillError("release drill revisions are not unique")
    return revisions


def _release_id_from_result(value: object) -> str:
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ReleaseDrillError("release deployment did not complete")
    release_id = str(value.get("release_id") or "")
    if _HEX_ID_RE.fullmatch(release_id.replace("-", "")) is None:
        raise ReleaseDrillError("release deployment returned an invalid ID")
    return release_id


def _repository_root(value: Path) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except OSError as exc:
        raise ReleaseDrillError("release drill repository is unavailable") from exc
    for relative in ("Dockerfile", "docker-compose.yml", "ops/nas_release.py"):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ReleaseDrillError("release drill repository is incomplete")
    return root


def _require_within(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ReleaseDrillError(
            "release drill path escaped its temporary root"
        ) from exc


def _require_docker(runner: BoundedRunner) -> None:
    result = runner.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ReleaseDrillError(
            "a real Docker daemon is required for the release drill"
        )


def _require_zstd(runner: BoundedRunner) -> None:
    result = runner.run(["zstd", "--version"], check=False)
    if result.returncode != 0:
        raise ReleaseDrillError("zstd is required for the release drill")


def _docker_lines(runner: BoundedRunner, argv: Sequence[str]) -> list[str]:
    result = runner.run(argv, check=False)
    if result.returncode != 0:
        raise ReleaseDrillError("Docker resource query failed")
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReleaseDrillError("Docker returned invalid text") from exc
    return [line.strip() for line in lines if line.strip()]


def _label_docker_run(
    argv: Sequence[str],
    resource_token: str | None,
) -> list[str]:
    command = [str(item) for item in argv]
    if resource_token is None or command[:2] != ["docker", "run"]:
        return command
    if _HEX_ID_RE.fullmatch(resource_token) is None:
        raise ReleaseDrillError("Docker resource token is invalid")
    return [
        "docker",
        "run",
        "--label",
        f"{RESOURCE_LABEL}={resource_token}",
        *command[2:],
    ]


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    if port == 8766 or not 1 <= port <= 65_535:
        return _loopback_port()
    return port


def _bounded_timeout(value: object) -> int:
    if isinstance(value, bool):
        raise ReleaseDrillError("release drill timeout is invalid")
    try:
        timeout = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReleaseDrillError("release drill timeout is invalid") from exc
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ReleaseDrillError("release drill timeout is invalid")
    return timeout


@contextmanager
def _temporary_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, destination)


def _error_report(resources: DrillResources, error_code: str) -> dict[str, object]:
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "ok": False,
        "run_token": resources.token,
        "error_code": error_code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an isolated Docker release drill")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--repo", type=Path, default=ROOT)
    run.add_argument("--run-id", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    resources = DrillResources.from_run_id(args.run_id)
    if args.command == "cleanup":
        try:
            payload = cleanup_release_drill(resources)
        except (OSError, ReleaseDrillError, subprocess.SubprocessError):
            print(
                json.dumps(_error_report(resources, "cleanup_failed"), sort_keys=True)
            )
            return 2
        print(json.dumps(payload, sort_keys=True))
        return 0

    report: dict[str, object] | None = None
    result = 0
    unexpected: BaseException | None = None
    unexpected_traceback: TracebackType | None = None
    try:
        try:
            report = run_release_drill(
                args.repo,
                run_id=args.run_id,
                timeout_seconds=args.timeout_seconds,
            )
        except (
            OSError,
            sqlite3.Error,
            NasReleaseError,
            ReleaseDrillError,
            subprocess.SubprocessError,
        ):
            report = _error_report(resources, "release_drill_failed")
            result = 2
    except BaseException as exc:
        unexpected = exc
        unexpected_traceback = exc.__traceback__
    finally:
        cleanup_ok = False
        try:
            cleanup_release_drill(resources)
            cleanup_ok = True
        except (
            OSError,
            ReleaseDrillError,
            subprocess.SubprocessError,
        ) as cleanup_error:
            result = 2
            if unexpected is not None and hasattr(unexpected, "add_note"):
                unexpected.add_note(
                    f"release drill cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
        except BaseException as cleanup_error:
            result = 2
            if unexpected is None:
                unexpected = cleanup_error
                unexpected_traceback = cleanup_error.__traceback__
            elif hasattr(unexpected, "add_note"):
                unexpected.add_note(
                    f"release drill cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
    if unexpected is not None:
        raise unexpected.with_traceback(unexpected_traceback)
    if report is None:
        raise ReleaseDrillError("release drill produced no report")
    report["cleanup"] = cleanup_ok
    if not cleanup_ok:
        report["ok"] = False
        report["error_code"] = "cleanup_failed"
    _atomic_json(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
