"""Prepare a private first installation on a Linux Docker host."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jav_pilot.config.schema import APP_CONFIG_SCHEMA_VERSION, SETTINGS_SCHEMA_VERSION  # noqa: E402

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_IDENTITY = re.compile(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9})\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LOCK_NAME = ".jav-pilot-maintenance.lock"


class PreparationError(RuntimeError):
    pass


def _identity(value: str) -> tuple[int, int]:
    match = _IDENTITY.fullmatch(value)
    if match is None:
        raise PreparationError("choose a non-root JAV_PILOT_CONTAINER_USER=uid:gid")
    identity = (int(match[1]), int(match[2]))
    if any(number > 2**32 - 2 for number in identity):
        raise PreparationError("container uid:gid is out of range")
    return identity


def _private_file(path: Path, content: str, uid: int, gid: int, mode: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            os.fchown(handle.fileno(), uid, gid)
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    if path.is_symlink():
        raise PreparationError("installation directories must not be symbolic links")
    if path.exists():
        details = path.stat()
        if not stat.S_ISDIR(details.st_mode) or (details.st_uid, details.st_gid) != (
            uid,
            gid,
        ):
            raise PreparationError(
                "existing directory ownership differs; review it before continuing"
            )
        return
    path.mkdir(mode=mode)
    os.chown(path, uid, gid)
    path.chmod(mode)


def prepare(root: Path, *, uid: int | None = None, gid: int | None = None) -> None:
    if os.name != "posix":
        raise PreparationError(
            "run Compose preparation on a Linux Docker host or inside WSL"
        )
    root = root.resolve(strict=True)
    if not (root / "docker-compose.yml").is_file():
        raise PreparationError("run this tool from an intact Jav Pilot source checkout")
    if (uid is None) != (gid is None):
        raise PreparationError("--uid and --gid must be provided together")
    environment = root / ".env"
    if environment.is_symlink():
        raise PreparationError(".env must not be a symbolic link")
    if not environment.exists():
        selected_uid, selected_gid = _identity(
            f"{uid if uid is not None else os.getuid()}:{gid if gid is not None else os.getgid()}"
        )
        if os.getuid() not in (0, selected_uid) or (
            os.getuid() != 0 and selected_gid not in {os.getgid(), *os.getgroups()}
        ):
            raise PreparationError(
                "the current user cannot prepare the selected uid:gid"
            )
        template = (root / ".env.example").read_text(encoding="utf-8")
        values = {
            "JAV_PILOT_CONTAINER_USER": f"{selected_uid}:{selected_gid}",
            "JAV_PILOT_AUTH_PASSWORD": secrets.token_urlsafe(24),
            "JAV_PILOT_AUTH_SECRET": secrets.token_urlsafe(48),
        }
        lines = [
            f"{line.split('=', 1)[0]}={values[line.split('=', 1)[0]]}"
            if line.split("=", 1)[0] in values
            else line
            for line in template.splitlines()
        ]
        _private_file(
            environment, "\n".join(lines) + "\n", os.getuid(), os.getgid(), 0o600
        )
    text = environment.read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    selected_uid, selected_gid = _identity(values.get("JAV_PILOT_CONTAINER_USER", ""))
    if uid is not None and (selected_uid, selected_gid) != (uid, gid):
        raise PreparationError(
            "existing .env selects another uid:gid; it was not changed"
        )
    if os.getuid() not in (0, selected_uid):
        raise PreparationError(
            "prepare as the selected service user or reviewed root operator"
        )
    if stat.S_IMODE(environment.stat().st_mode) & 0o077:
        raise PreparationError(
            ".env must be private (chmod 600 .env); it was not changed"
        )
    runtime = root / "runtime"
    _directory(runtime, uid=os.getuid(), gid=os.getgid(), mode=0o750)
    _directory(root / "data", uid=selected_uid, gid=selected_gid, mode=0o700)
    for filename, version in (
        ("app_config.json", APP_CONFIG_SCHEMA_VERSION),
        ("settings.json", SETTINGS_SCHEMA_VERSION),
    ):
        path = root / "data" / filename
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise PreparationError("existing configuration must be a regular file")
        if not path.exists():
            _private_file(
                path,
                json.dumps({"schema_version": version}) + "\n",
                selected_uid,
                selected_gid,
                0o600,
            )
    for relative in (
        "downloads",
        "downloads/jav",
        "downloads/jav-web",
        "media",
        "media/JAV",
        "backups",
    ):
        _directory(runtime / relative, uid=selected_uid, gid=selected_gid, mode=0o700)
    lock_root = runtime / "maintenance-locks"
    _directory(lock_root, uid=os.getuid(), gid=selected_gid, mode=0o770)
    if stat.S_IMODE(lock_root.stat().st_mode) != 0o770:
        raise PreparationError(
            "existing maintenance lock directory must have mode 0770"
        )
    lock = lock_root / _LOCK_NAME
    if not lock.exists() and not lock.is_symlink():
        _private_file(lock, "", os.getuid(), selected_gid, 0o660)
    details = lock.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o660
        or (details.st_uid, details.st_gid) != (os.getuid(), selected_gid)
    ):
        raise PreparationError(
            "existing shared lock identity differs; it was not changed"
        )


def browser_profile_command(image_id: str, volume_name: str, user: str) -> list[str]:
    if _IMAGE_ID.fullmatch(image_id) is None or _NAME.fullmatch(volume_name) is None:
        raise PreparationError(
            "browser initialization requires an exact local image and named volume"
        )
    uid, gid = _identity(user)
    script = "\n".join(
        (
            "import os, stat, sys",
            "from pathlib import Path",
            "path = Path('/browser-profile')",
            "uid, gid = map(int, sys.argv[1:])",
            "details = path.lstat()",
            "if not stat.S_ISDIR(details.st_mode) or path.is_symlink():",
            "    raise RuntimeError('browser volume is unsafe')",
            "if any(path.iterdir()):",
            "    if (details.st_uid, details.st_gid) != (uid, gid) or stat.S_IMODE(details.st_mode) != 0o700:",
            "        raise RuntimeError('nonempty browser volume needs a reviewed ownership migration')",
            "else:",
            "    os.chown(path, uid, gid)",
            "    os.chmod(path, 0o700)",
        )
    )
    return [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "FOWNER",
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
        f"type=volume,source={volume_name},target=/browser-profile,volume-nocopy",
        "--entrypoint",
        "python",
        image_id,
        "-B",
        "-c",
        script,
        str(uid),
        str(gid),
    ]


def _docker(
    root: Path, arguments: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if check and result.returncode:
        raise PreparationError(
            "Docker preparation failed; verify the local image, permissions, and volume ownership"
        )
    return result


def prepare_browser_volume(root: Path) -> None:
    try:
        model = json.loads(
            _docker(root, ["compose", "config", "--format", "json"]).stdout
        )
        service = model["services"]["jav-pilot"]
        user = service["user"]
        _identity(user)
        project = model["name"]
        mounts = [
            item
            for item in service["volumes"]
            if item["target"] == "/app/browser-profile"
        ]
        if (
            len(mounts) != 1
            or mounts[0].get("type") != "volume"
            or mounts[0].get("read_only") is True
        ):
            raise PreparationError(
                "browser mount must be the writable Compose named volume"
            )
        key = mounts[0]["source"]
        volume = model["volumes"][key]
        name = volume["name"]
        if (
            set(volume) != {"name"}
            or _NAME.fullmatch(name) is None
            or _NAME.fullmatch(project) is None
        ):
            raise PreparationError(
                "browser volume must be owned by this Compose project"
            )
        image_id = _docker(
            root, ["image", "inspect", "--format", "{{.Id}}", service["image"]]
        ).stdout.strip()
        command = browser_profile_command(image_id, name, user)
        inspected = _docker(root, ["volume", "inspect", name], check=False)
        if inspected.returncode:
            _docker(
                root,
                [
                    "volume",
                    "create",
                    "--label",
                    f"com.docker.compose.project={project}",
                    "--label",
                    f"com.docker.compose.volume={key}",
                    name,
                ],
            )
            inspected = _docker(root, ["volume", "inspect", name])
        details = json.loads(inspected.stdout)
        if (
            len(details) != 1
            or details[0].get("Name") != name
            or details[0].get("Driver") != "local"
            or details[0].get("Options") not in (None, {})
        ):
            raise PreparationError(
                "existing browser volume driver or options are unsafe"
            )
        labels = details[0].get("Labels") or {}
        if (
            not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.volume") != key
        ):
            raise PreparationError(
                "existing browser volume belongs to another Compose project"
            )
        _docker(root, command[1:])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreparationError(
            "Compose browser volume configuration is invalid"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument(
        "--browser-volume",
        action="store_true",
        help="initialize the exact empty browser volume after docker compose build or pull",
    )
    args = parser.parse_args(argv)
    try:
        prepare(ROOT, uid=args.uid, gid=args.gid)
        if args.browser_volume:
            prepare_browser_volume(ROOT)
        print(
            "Prepared local .env and runtime paths. View login credentials only in your trusted local editor."
        )
        if not args.browser_volume:
            print(
                "Next: docker compose build (or set JAV_PILOT_IMAGE_REFERENCE in .env and "
                "run docker compose pull); python3 tools/prepare_compose.py --browser-volume; "
                "docker compose up -d --no-build"
            )
        return 0
    except (PreparationError, OSError) as exc:
        print(f"Preparation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
