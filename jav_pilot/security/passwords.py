from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


SCHEME = "scrypt"
DEFAULT_N = 2**14
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32


class PasswordHashError(ValueError):
    pass


def hash_password(password: str) -> str:
    if not password:
        raise PasswordHashError("password cannot be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=DEFAULT_N,
        r=DEFAULT_R,
        p=DEFAULT_P,
        dklen=HASH_BYTES,
    )
    return "$".join(
        (
            SCHEME,
            str(DEFAULT_N),
            str(DEFAULT_R),
            str(DEFAULT_P),
            _encode(salt),
            _encode(digest),
        )
    )


def verify_password_hash(password: str, encoded: str) -> bool:
    try:
        n, r, p, salt, expected = _parse(encoded)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (PasswordHashError, ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def validate_password_hash(encoded: str) -> str:
    _parse(encoded)
    return encoded


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PasswordHashError("invalid password hash") from exc

    if scheme != SCHEME or n < 2**12 or n > 2**18 or n & (n - 1):
        raise PasswordHashError("unsupported password hash parameters")
    if not 1 <= r <= 16 or not 1 <= p <= 8:
        raise PasswordHashError("unsupported password hash parameters")

    salt = _decode(raw_salt)
    digest = _decode(raw_digest)
    if not 8 <= len(salt) <= 64 or not 16 <= len(digest) <= 64:
        raise PasswordHashError("invalid password hash payload")
    return n, r, p, salt, digest


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise PasswordHashError("invalid password hash encoding") from exc
