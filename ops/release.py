from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_IMMUTABLE_IMAGE_REFERENCE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._:-]*"
    r"(?:/[a-z0-9][a-z0-9._-]*)+)@sha256:(?P<digest>[0-9a-f]{64})$"
)
_IMAGE_PLATFORM_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)?$"
)
_SHA_TAG_REFERENCE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._:-]*"
    r"(?:/[a-z0-9][a-z0-9._-]*)*):(?P<tag>[0-9a-f]{40})$"
)
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_REMOTE_ARCHIVE_RE = re.compile(
    r"^/tmp/jav-pilot-[0-9a-f]{40}\.[A-Za-z0-9]{6,32}\.tar$"
)
_REMOTE_IMAGE_ARCHIVE_RE = re.compile(
    r"^/tmp/jav-pilot-image-[0-9a-f]{40}\.[A-Za-z0-9]{6,32}\.tar$"
)
_REMOTE_IMAGE_MANIFEST_RE = re.compile(
    r"^/tmp/jav-pilot-image-[0-9a-f]{40}\.[A-Za-z0-9]{6,32}\.json$"
)
_IMAGE_MANIFEST_KEYS = frozenset(
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
_MAX_IMAGE_MANIFEST_BYTES = 64 * 1024
_MAX_REMOTE_ERROR_BYTES = 4096
_MAX_REMOTE_ERROR_CHARACTERS = 1024


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            [str(item) for item in argv],
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        result = CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
        if check and result.returncode != 0:
            executable = Path(str(argv[0])).name if argv else "command"
            raise ReleaseError(f"{executable} failed with status {result.returncode}")
        return result


@dataclass(frozen=True)
class ReleaseArtifact:
    revision: str
    archive: bytes
    archive_sha256: str
    remote_script: bytes


@dataclass(frozen=True)
class ImageArchiveArtifact:
    archive_path: Path
    manifest_bytes: bytes
    archive_sha256: str
    archive_filename: str
    sha_tag: str
    image_id: str
    platform: str


class GitReleaseBuilder:
    def __init__(self, repo: Path, runner: Runner | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.runner = runner or SubprocessRunner()

    def build(self, revision: object) -> ReleaseArtifact:
        clean_revision = _revision(revision)
        self.runner.run(
            ["git", "-C", str(self.repo), "fetch", "--quiet", "origin", "main"]
        )
        resolved = _text(
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "rev-parse",
                    "--verify",
                    f"{clean_revision}^{{commit}}",
                ]
            ).stdout
        )
        if resolved != clean_revision:
            raise ReleaseError(
                "release revision does not resolve to the requested commit"
            )
        remote_head = _text(
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "rev-parse",
                    "--verify",
                    "refs/remotes/origin/main",
                ]
            ).stdout
        )
        if not _REVISION_RE.fullmatch(remote_head):
            raise ReleaseError("origin/main did not resolve to a full revision")
        ancestor = self.runner.run(
            [
                "git",
                "-C",
                str(self.repo),
                "merge-base",
                "--is-ancestor",
                clean_revision,
                remote_head,
            ],
            check=False,
        )
        if ancestor.returncode != 0:
            raise ReleaseError("release revision is not present on origin/main")
        archive = self.runner.run(
            [
                "git",
                "-C",
                str(self.repo),
                "archive",
                "--format=tar",
                clean_revision,
            ]
        ).stdout
        if not archive:
            raise ReleaseError("git archive produced no release content")
        remote_script = self.runner.run(
            [
                "git",
                "-C",
                str(self.repo),
                "show",
                f"{clean_revision}:ops/nas_release.py",
            ]
        ).stdout
        if not remote_script:
            raise ReleaseError("release does not contain the remote deployment tool")
        return ReleaseArtifact(
            revision=clean_revision,
            archive=archive,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            remote_script=remote_script,
        )


def load_image_archive_artifact(
    archive_path: Path,
    manifest_path: Path,
    *,
    revision: object,
) -> ImageArchiveArtifact:
    clean_revision = _revision(revision)
    archive = Path(archive_path).absolute()
    manifest = Path(manifest_path).absolute()
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("image archive is unavailable")
    if (
        manifest.is_symlink()
        or not manifest.is_file()
        or manifest.stat().st_size > _MAX_IMAGE_MANIFEST_BYTES
    ):
        raise ReleaseError("image manifest is unavailable or unsafe")
    try:
        manifest_bytes = manifest.read_bytes()
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("image manifest is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _IMAGE_MANIFEST_KEYS:
        raise ReleaseError("image manifest schema is invalid")
    if payload.get("schema_version") != 2:
        raise ReleaseError("image manifest version is unsupported")
    if _revision(payload.get("revision")) != clean_revision:
        raise ReleaseError("image manifest revision does not match the release")
    sha_tag = _sha_tag_reference(payload.get("sha_tag"), clean_revision)
    image_id = str(payload.get("image_id") or "").strip()
    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise ReleaseError("image manifest image ID is invalid")
    platform = _image_platform(payload.get("platform"))
    archive_filename = str(payload.get("archive_filename") or "").strip()
    expected_archive_filename = (
        f"jav-pilot-{clean_revision}-{platform.replace('/', '-')}.tar.zst"
    )
    if (
        not archive_filename
        or archive_filename != Path(archive_filename).name
        or archive_filename != expected_archive_filename
        or archive.name != archive_filename
        or any(character in archive_filename for character in ("\x00", "\n", "\r"))
    ):
        raise ReleaseError("image manifest archive filename is invalid")
    expected_sha256 = _sha256_value(
        payload.get("archive_sha256"), "image archive SHA-256"
    )
    actual_sha256 = _sha256_file(archive)
    if actual_sha256 != expected_sha256:
        raise ReleaseError("image archive SHA-256 does not match its manifest")
    return ImageArchiveArtifact(
        archive_path=archive,
        manifest_bytes=manifest_bytes,
        archive_sha256=expected_sha256,
        archive_filename=archive_filename,
        sha_tag=sha_tag,
        image_id=image_id,
        platform=platform,
    )


class SshRemote:
    def __init__(self, alias: str, runner: Runner | None = None) -> None:
        if not _SSH_ALIAS_RE.fullmatch(str(alias or "")):
            raise ReleaseError("SSH alias is invalid")
        self.alias = alias
        self.runner = runner or SubprocessRunner()

    def deploy(
        self,
        artifact: ReleaseArtifact,
        *,
        image_reference: str | None = None,
        image_platform: str | None = None,
        image_archive: ImageArchiveArtifact | None = None,
        app_root: str,
        backup_root: str,
        state_root: str,
        apply: bool,
    ) -> dict[str, object]:
        if (image_reference is None) == (image_archive is None):
            raise ReleaseError(
                "exactly one image source is required: digest reference or archive"
            )
        clean_image_reference: str | None = None
        clean_image_platform: str | None = None
        if image_reference is not None:
            clean_image_reference = _immutable_image_reference(image_reference)
            clean_image_platform = _image_platform(image_platform or "linux/amd64")
        elif image_platform is not None:
            raise ReleaseError("image platform is supplied by the image manifest")
        remote_archive = self._create_remote_archive(artifact.revision)
        remote_image_archive: str | None = None
        remote_image_manifest: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="jav-pilot-release-") as tmp:
                local_archive = Path(tmp) / "release.tar"
                local_archive.write_bytes(artifact.archive)
                self.runner.run(
                    ["scp", str(local_archive), f"{self.alias}:{remote_archive}"]
                )
                if image_archive is not None:
                    remote_image_archive = self._create_remote_image_archive(
                        artifact.revision
                    )
                    remote_image_manifest = self._create_remote_image_manifest(
                        artifact.revision
                    )
                    local_manifest = Path(tmp) / "image-manifest.json"
                    local_manifest.write_bytes(image_archive.manifest_bytes)
                    self.runner.run(
                        [
                            "scp",
                            str(image_archive.archive_path),
                            f"{self.alias}:{remote_image_archive}",
                        ]
                    )
                    self.runner.run(
                        [
                            "scp",
                            str(local_manifest),
                            f"{self.alias}:{remote_image_manifest}",
                        ]
                    )
                    if (
                        _sha256_file(image_archive.archive_path)
                        != image_archive.archive_sha256
                    ):
                        raise ReleaseError(
                            "image archive changed while it was transferred"
                        )
            arguments = [
                "--app-root",
                app_root,
                "--backup-root",
                backup_root,
                "--state-root",
                state_root,
                "deploy",
                "--archive",
                remote_archive,
                "--archive-sha256",
                artifact.archive_sha256,
                "--revision",
                artifact.revision,
            ]
            if clean_image_reference is not None:
                arguments.extend(
                    [
                        "--image-reference",
                        clean_image_reference,
                        "--image-platform",
                        str(clean_image_platform),
                    ]
                )
            else:
                assert image_archive is not None
                assert remote_image_archive is not None
                assert remote_image_manifest is not None
                arguments.extend(
                    [
                        "--image-archive",
                        remote_image_archive,
                        "--image-manifest",
                        remote_image_manifest,
                        "--image-archive-filename",
                        image_archive.archive_filename,
                    ]
                )
            if apply:
                arguments.append("--apply")
            return self._invoke(artifact.remote_script, arguments)
        finally:
            for remote_path in (
                remote_image_manifest,
                remote_image_archive,
                remote_archive,
            ):
                if remote_path is not None:
                    self.runner.run(
                        [
                            "ssh",
                            self.alias,
                            f"rm -f -- {shlex.quote(remote_path)}",
                        ],
                        check=False,
                    )

    def _create_remote_archive(self, revision: str) -> str:
        template = f"/tmp/jav-pilot-{revision}.XXXXXXXX.tar"
        result = self.runner.run(
            ["ssh", self.alias, f"mktemp -- {shlex.quote(template)}"]
        )
        remote_archive = _text(result.stdout)
        if not _REMOTE_ARCHIVE_RE.fullmatch(remote_archive):
            raise ReleaseError("remote temporary archive path is invalid")
        return remote_archive

    def _create_remote_image_archive(self, revision: str) -> str:
        return self._create_remote_file(
            f"/tmp/jav-pilot-image-{revision}.XXXXXXXX.tar",
            _REMOTE_IMAGE_ARCHIVE_RE,
        )

    def _create_remote_image_manifest(self, revision: str) -> str:
        return self._create_remote_file(
            f"/tmp/jav-pilot-image-{revision}.XXXXXXXX.json",
            _REMOTE_IMAGE_MANIFEST_RE,
        )

    def _create_remote_file(self, template: str, pattern: re.Pattern[str]) -> str:
        result = self.runner.run(
            ["ssh", self.alias, f"mktemp -- {shlex.quote(template)}"]
        )
        remote_path = _text(result.stdout)
        if not pattern.fullmatch(remote_path):
            raise ReleaseError("remote temporary image path is invalid")
        return remote_path

    def rollback(
        self,
        remote_script: bytes,
        *,
        release_id: str,
        app_root: str,
        backup_root: str,
        state_root: str,
        apply: bool,
    ) -> dict[str, object]:
        arguments = [
            "--app-root",
            app_root,
            "--backup-root",
            backup_root,
            "--state-root",
            state_root,
            "rollback",
            "--release-id",
            release_id,
        ]
        if apply:
            arguments.append("--apply")
        return self._invoke(remote_script, arguments)

    def _invoke(
        self,
        remote_script: bytes,
        arguments: Sequence[str],
    ) -> dict[str, object]:
        remote_command = " ".join(
            ["python3", "-", *(shlex.quote(argument) for argument in arguments)]
        )
        result = self.runner.run(
            ["ssh", self.alias, remote_command],
            input_bytes=remote_script,
            check=False,
        )
        if result.returncode != 0:
            remote_error = _structured_remote_error(result.stderr)
            if remote_error is not None:
                raise ReleaseError(remote_error)
            raise ReleaseError(f"ssh failed with status {result.returncode}")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("remote release tool returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ReleaseError("remote release tool returned an invalid result")
        if payload.get("ok") is not True:
            raise ReleaseError(str(payload.get("error") or "remote release failed"))
        return payload


def _revision(value: object) -> str:
    clean = str(value or "").strip()
    if not _REVISION_RE.fullmatch(clean):
        raise ReleaseError("revision must be a full lowercase Git SHA")
    return clean


def _immutable_image_reference(value: object) -> str:
    clean = str(value or "").strip()
    if (
        not _IMMUTABLE_IMAGE_REFERENCE_RE.fullmatch(clean)
        or len(clean) > 320
        or "//" in clean
    ):
        raise ReleaseError(
            "image reference must be a full registry reference pinned by sha256 digest"
        )
    return clean


def _sha_tag_reference(value: object, revision: object) -> str:
    clean = str(value or "").strip()
    match = _SHA_TAG_REFERENCE_RE.fullmatch(clean)
    if match is None or match.group("tag") != _revision(revision):
        raise ReleaseError("image SHA tag must end with the full release revision")
    return clean


def _image_platform(value: object) -> str:
    clean = str(value or "").strip()
    if not _IMAGE_PLATFORM_RE.fullmatch(clean):
        raise ReleaseError("image platform must use os/architecture[/variant] format")
    return clean


def _sha256_value(value: object, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        raise ReleaseError(f"{label} is invalid")
    return clean


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseError("image archive is unavailable") from exc
    return digest.hexdigest()


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ReleaseError("command returned invalid text") from exc


def _structured_remote_error(value: bytes) -> str | None:
    if not isinstance(value, bytes) or not 1 <= len(value) <= _MAX_REMOTE_ERROR_BYTES:
        return None
    try:
        payload = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"ok", "error"}:
        return None
    error = payload.get("error")
    if payload.get("ok") is not False or not isinstance(error, str):
        return None
    clean = error.strip()
    if not 1 <= len(clean) <= _MAX_REMOTE_ERROR_CHARACTERS or any(
        character in clean for character in ("\x00", "\n", "\r")
    ):
        return None
    return clean


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy immutable JAV Pilot releases")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", required=True)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--state-root", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    deploy = subparsers.add_parser("deploy")
    deploy.add_argument("--revision", required=True)
    image_source = deploy.add_mutually_exclusive_group(required=True)
    image_source.add_argument("--image-reference")
    image_source.add_argument("--image-archive", type=Path)
    deploy.add_argument("--image-manifest", type=Path)
    deploy.add_argument("--image-platform")
    deploy.add_argument("--apply", action="store_true")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--tool-revision", required=True)
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        artifact = GitReleaseBuilder(args.repo).build(
            args.revision if args.command == "deploy" else args.tool_revision
        )
        remote = SshRemote(args.remote)
        if args.command == "deploy":
            image_archive = None
            if args.image_archive is not None:
                if args.image_manifest is None:
                    raise ReleaseError(
                        "--image-manifest is required with --image-archive"
                    )
                image_archive = load_image_archive_artifact(
                    args.image_archive,
                    args.image_manifest,
                    revision=artifact.revision,
                )
            elif args.image_manifest is not None:
                raise ReleaseError(
                    "--image-manifest is only valid with --image-archive"
                )
            payload = remote.deploy(
                artifact,
                image_reference=args.image_reference,
                image_platform=args.image_platform,
                image_archive=image_archive,
                app_root=args.app_root,
                backup_root=args.backup_root,
                state_root=args.state_root,
                apply=args.apply,
            )
        else:
            payload = remote.rollback(
                artifact.remote_script,
                release_id=args.release_id,
                app_root=args.app_root,
                backup_root=args.backup_root,
                state_root=args.state_root,
                apply=args.apply,
            )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ReleaseError, subprocess.SubprocessError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
