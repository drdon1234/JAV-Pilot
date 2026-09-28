"""Container entrypoint: prepare the mounted directories, then drop root.

The Docker Compose deployments start the container as root with ``PUID`` and
``PGID`` set. This entrypoint then

- hands directories Docker has just created (owned by root and empty) to
  ``PUID:PGID``. It never changes directories that already hold files, so an
  existing media library keeps its ownership. Only these fixed mount points
  are touched as root, never anything inside them, and a symlink in place
  of one is left alone;
- switches to ``PUID:PGID``, which clears every capability (``PUID`` 0 is
  refused, since it would keep them);
- writes ``QBITTORRENT_PASSWORD`` into the bundled qBittorrent's
  configuration before qBittorrent first starts (qBittorrent waits for this
  container to be healthy), see ``torrent.qbittorrent_seed``. This runs
  unprivileged because qBittorrent itself can write to that directory;
- hands ``JAV_PILOT_PROXY`` to the libraries that only read ``HTTP_PROXY``
  and friends, and starts a virtual display for the headful MissAV browser;
- runs the command.

Started as any other user, or as root without ``PUID``/``PGID``, it skips the
first two steps.
"""

from __future__ import annotations

import errno
import os
import select
import shutil
import subprocess
import sys
from pathlib import Path

from .torrent.qbittorrent_seed import SeedError, seed_password

# (path, mode, private): a private directory not owned by PUID:PGID stops the
# start; a shared one (media, downloads) only warns, since it may belong to
# another account on a NAS.
_CLAIMED_DIRECTORIES = (
    (Path("/app/data"), 0o700, True),
    (Path("/app/browser-profile"), 0o700, True),
    (Path("/downloads/jav"), 0o750, False),
    (Path("/downloads/jav-web"), 0o750, False),
    (Path("/media/JAV"), 0o755, False),
    (Path("/qbittorrent"), 0o700, False),
)
QBITTORRENT_CONFIG = Path("/qbittorrent/qBittorrent/qBittorrent.conf")
# Hosts reached inside the Compose network or on the Docker host itself.
_NO_PROXY = (
    "127.0.0.1,localhost,jav-pilot,jav-pilot-qbittorrent,jav-pilot-jackett,"
    "host.docker.internal"
)
_DISPLAY_START_SECONDS = 15


class EntryError(RuntimeError):
    pass


def claim_directory(path: Path, mode: int, private: bool, owner: tuple[int, int]) -> None:
    # Work on one descriptor opened without following a symlink, so the entry
    # that is checked is the one that is changed.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return  # not mounted, or not a real directory: leave it alone
        raise
    try:
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid) == owner:
            return
        current = f"{info.st_uid}:{info.st_gid}"
        wanted = f"{owner[0]}:{owner[1]}"
        if not os.listdir(fd):
            os.fchown(fd, *owner)
            os.fchmod(fd, mode)
            print(f"prepared {path} for {wanted}")
        elif private:
            raise EntryError(
                f"{path} belongs to {current}, not {wanted}; fix its ownership or PUID/PGID"
            )
        else:
            print(
                f"warning: {path} already holds files owned by {current}; "
                f"make sure {wanted} can write to it",
                file=sys.stderr,
            )
    finally:
        os.close(fd)


def claim_directories(owner: tuple[int, int]) -> None:
    for path, mode, private in _CLAIMED_DIRECTORIES:
        claim_directory(path, mode, private, owner)


def seed_qbittorrent_password() -> None:
    # Never as root: qBittorrent can write to this directory.
    if os.geteuid() != 0 and QBITTORRENT_CONFIG.parent.parent.is_dir():
        outcome = seed_password(
            QBITTORRENT_CONFIG, os.environ.get("QBITTORRENT_PASSWORD", "")
        )
        print(
            {
                "seeded": "qBittorrent password: set from QBITTORRENT_PASSWORD",
                "kept": "qBittorrent password: already set, left unchanged",
                "skipped": "qBittorrent password: QBITTORRENT_PASSWORD is empty, "
                "qBittorrent keeps its temporary password",
            }[outcome]
        )


def export_proxy_environment() -> None:
    # urllib, curl and Chromium read HTTP(S)_PROXY, not JAV_PILOT_PROXY. A
    # value set explicitly for the container wins.
    proxy = os.environ.get("JAV_PILOT_PROXY", "").strip()
    if not proxy:
        return
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        if not os.environ.get(key, "").strip():
            os.environ[key] = proxy
    if not os.environ.get("NO_PROXY", "").strip():
        os.environ["NO_PROXY"] = _NO_PROXY


def start_virtual_display(command: list[str]) -> None:
    """Start Xvfb in the background and point DISPLAY at it.

    Wrapping the service in ``xvfb-run`` would put a shell between the init
    process and the service; that shell exits on the stop signal without
    passing it on, so the service would be killed instead of shutting down.
    Older compose files still run ``xvfb-run`` themselves and are left alone.
    """
    if os.environ.get("DISPLAY") or command[0] == "xvfb-run" or not shutil.which("Xvfb"):
        return
    read_fd, write_fd = os.pipe()
    try:
        subprocess.Popen(
            [
                "Xvfb", "-displayfd", str(write_fd),
                "-screen", "0", "1280x1024x24", "-nolisten", "tcp",
            ],
            pass_fds=(write_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        os.close(write_fd)
    # Xvfb writes the display number once it accepts connections.
    with os.fdopen(read_fd) as reader:
        ready, _, _ = select.select([reader], [], [], _DISPLAY_START_SECONDS)
        number = reader.readline().strip() if ready else ""
    if not number.isdigit():
        raise EntryError("the virtual display did not start")
    os.environ["DISPLAY"] = f":{number}"


def _owner_from_env() -> tuple[int, int] | None:
    uid, gid = os.environ.get("PUID", "").strip(), os.environ.get("PGID", "").strip()
    if not uid and not gid:
        return None
    try:
        owner = int(uid), int(gid)
    except ValueError:
        raise EntryError("PUID and PGID must both be numbers") from None
    if min(owner) < 0:
        raise EntryError("PUID and PGID must not be negative")
    if owner[0] == 0:
        # Staying root would keep every added capability.
        raise EntryError("PUID must not be 0 (root); use the account that owns the media")
    return owner


def main(argv: list[str] | None = None) -> int:
    command = sys.argv[1:] if argv is None else argv
    if not command:
        print("usage: python -m jav_pilot.container_entry COMMAND...", file=sys.stderr)
        return 2
    try:
        if os.geteuid() == 0:
            owner = _owner_from_env()
            if owner is not None:
                claim_directories(owner)
                os.setgroups([])
                os.setgid(owner[1])
                os.setuid(owner[0])
    except (OSError, EntryError) as exc:
        print(f"jav-pilot: {exc}", file=sys.stderr)
        return 1
    try:
        seed_qbittorrent_password()
    except (OSError, SeedError) as exc:
        # qBittorrent then keeps its temporary password; JAV Pilot still starts.
        print(f"warning: qBittorrent password not set: {exc}", file=sys.stderr)
    export_proxy_environment()
    try:
        start_virtual_display(command)
    except (OSError, EntryError) as exc:
        print(f"jav-pilot: {exc}", file=sys.stderr)
        return 1
    sys.stdout.flush()
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"jav-pilot: cannot run {command[0]}: {exc}", file=sys.stderr)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
