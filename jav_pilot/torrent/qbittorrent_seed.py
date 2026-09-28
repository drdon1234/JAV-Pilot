"""Give the bundled qBittorrent its WebUI password before its first start.

qBittorrent has no setting for an initial password: until one is saved it
logs a random temporary password on every start. The container entrypoint
(``jav_pilot.container_entry``) runs before the bundled qBittorrent starts and
writes ``QBITTORRENT_PASSWORD`` into qBittorrent.conf the way qBittorrent
stores it (PBKDF2-HMAC-SHA512). A password already present in the file is
never replaced; change it in the qBittorrent WebUI instead.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

PASSWORD_KEY = "WebUI\\Password_PBKDF2"
MIN_PASSWORD_LENGTH = 6  # qBittorrent's own minimum
_ITERATIONS = 100_000
_SALT_BYTES = 16
_KEY_BYTES = 64
# The linuxserver.io image copies its defaults only when no file exists; a
# file created here must carry them itself.
_LINUXSERVER_DEFAULTS = """[AutoRun]
enabled=false
program=

[LegalNotice]
Accepted=true

[Preferences]
Connection\\UPnP=false
Connection\\PortRangeMin=6881
Downloads\\SavePath=/downloads/
Downloads\\ScanDirsV2=@Variant(\\0\\0\\0\\x1c\\0\\0\\0\\0)
Downloads\\TempPath=/downloads/incomplete/
WebUI\\Address=*
WebUI\\ServerDomains=*
"""


class SeedError(ValueError):
    pass


def password_line(password: str, *, salt: bytes | None = None) -> str:
    salt = secrets.token_bytes(_SALT_BYTES) if salt is None else salt
    key = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, _ITERATIONS, _KEY_BYTES
    )
    encoded = (
        base64.b64encode(salt).decode("ascii")
        + ":"
        + base64.b64encode(key).decode("ascii")
    )
    return f'{PASSWORD_KEY}="@ByteArray({encoded})"'


def seed_password(path: Path, password: str) -> str:
    """Return "seeded", "kept" (a password exists) or "skipped" (none given)."""

    if not password:
        return "skipped"
    if len(password) < MIN_PASSWORD_LENGTH or any(
        ord(character) < 32 for character in password
    ):
        raise SeedError(
            f"QBITTORRENT_PASSWORD must have at least {MIN_PASSWORD_LENGTH} "
            "characters and no control characters"
        )
    if path.is_symlink():
        raise SeedError(f"{path} is a symbolic link")
    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if any(line.startswith(PASSWORD_KEY + "=") for line in lines):
            return "kept"
        try:
            section = lines.index("[Preferences]")
            lines.insert(section + 1, password_line(password))
        except ValueError:
            lines += ["", "[Preferences]", password_line(password)]
        content = "\n".join(lines) + "\n"
    else:
        content = _LINUXSERVER_DEFAULTS + password_line(password) + "\n"
    if not path.parent.is_dir():
        path.parent.mkdir(mode=0o700)
    temporary = path.with_name(f".{path.name}.seed.tmp")
    # A crash between open and replace leaves the temporary file behind; it
    # is ours to discard, otherwise O_EXCL would fail every later start.
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "seeded"

