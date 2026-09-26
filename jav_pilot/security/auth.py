from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from .passwords import verify_password_hash
from ..config.runtime_config import RuntimeConfigError, load_runtime_config


SESSION_COOKIE = "jav_pilot_session"
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)
_REVOKED_SESSION_LIMIT = 4096
_REVOKED_SESSIONS: dict[str, int] = {}
_REVOKED_SESSIONS_LOCK = threading.Lock()


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    username: str
    password: str
    secret: str
    password_hash: str = ""
    session_seconds: int = 7 * 24 * 60 * 60
    secret_persistent: bool = True

    @classmethod
    def from_env(cls) -> "AuthConfig":
        runtime_error = False
        try:
            runtime = load_runtime_config()
        except RuntimeConfigError:
            runtime = {}
            runtime_error = True
        auth = runtime.get("auth", {})
        if not isinstance(auth, dict):
            auth = {}

        password_hash = str(auth.get("password_hash") or "").strip()
        password = "" if password_hash else os.environ.get("JAV_PILOT_AUTH_PASSWORD", "").strip()
        explicit_enabled = os.environ.get("JAV_PILOT_AUTH_ENABLED", "").strip().lower()
        if runtime_error:
            enabled = True
        elif "enabled" in auth:
            enabled = bool(auth.get("enabled"))
        else:
            enabled = explicit_enabled in {"1", "true", "yes", "on"} if explicit_enabled else bool(password)
        username = _setting(auth, "username", "JAV_PILOT_AUTH_USERNAME", "admin") or "admin"
        configured_secret = _setting(auth, "secret", "JAV_PILOT_AUTH_SECRET")
        secret = configured_secret or _EPHEMERAL_SECRET
        return cls(
            enabled=enabled,
            username=username,
            password=password,
            password_hash=password_hash,
            secret=secret,
            secret_persistent=bool(configured_secret),
        )

    @property
    def configured(self) -> bool:
        return bool(self.password_hash or self.password)


def public_auth_status(config: AuthConfig | None = None) -> dict[str, Any]:
    config = config or AuthConfig.from_env()
    return {
        "enabled": config.enabled,
        "configured": config.configured,
        "secret_configured": config.secret_persistent,
        "username": config.username if config.enabled else "",
    }


def authenticate(username: str, password: str, config: AuthConfig | None = None) -> bool:
    config = config or AuthConfig.from_env()
    if not config.enabled:
        return True
    if not config.configured:
        return False
    if not _constant_time_text_equal(username, config.username):
        return False
    if config.password_hash:
        return verify_password_hash(password, config.password_hash)
    return _constant_time_text_equal(password, config.password)


def make_session_cookie(
    config: AuthConfig | None = None,
    now: int | None = None,
    *,
    secure: bool = False,
) -> str:
    config = config or AuthConfig.from_env()
    now = int(now or time.time())
    payload = {
        "u": config.username,
        "iat": now,
        "exp": now + config.session_seconds,
        "jti": secrets.token_urlsafe(18),
    }
    token = _encode(payload, config.secret)
    cookie = (
        f"{SESSION_COOKIE}={token}; Path=/; Max-Age={config.session_seconds}; "
        "HttpOnly; SameSite=Lax"
    )
    return f"{cookie}; Secure" if secure else cookie


def clear_session_cookie(*, secure: bool = False) -> str:
    cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
    return f"{cookie}; Secure" if secure else cookie


def revoke_session(
    cookie_header: str,
    config: AuthConfig | None = None,
    now: int | None = None,
) -> bool:
    """Revoke the presented signed session until its original expiry."""

    config = config or AuthConfig.from_env()
    payload = _session_payload(cookie_header, config)
    if not payload:
        return False
    current = int(now or time.time())
    expiry = int(payload.get("exp") or 0)
    session_id = str(payload.get("jti") or "")
    if not session_id or expiry <= current:
        return False
    with _REVOKED_SESSIONS_LOCK:
        _prune_revoked_sessions_locked(current)
        _REVOKED_SESSIONS[_session_revocation_key(session_id)] = expiry
        if len(_REVOKED_SESSIONS) > _REVOKED_SESSION_LIMIT:
            for key, _expiry in sorted(
                _REVOKED_SESSIONS.items(), key=lambda item: item[1]
            )[: len(_REVOKED_SESSIONS) - _REVOKED_SESSION_LIMIT]:
                _REVOKED_SESSIONS.pop(key, None)
    return True


def verify_session(cookie_header: str, config: AuthConfig | None = None, now: int | None = None) -> bool:
    config = config or AuthConfig.from_env()
    if not config.enabled:
        return True
    if not config.configured:
        return False
    payload = _session_payload(cookie_header, config)
    if not payload:
        return False
    if payload.get("u") != config.username:
        return False
    current = int(now or time.time())
    if int(payload.get("exp") or 0) <= current:
        return False
    session_id = str(payload.get("jti") or "")
    if session_id:
        with _REVOKED_SESSIONS_LOCK:
            _prune_revoked_sessions_locked(current)
            if _session_revocation_key(session_id) in _REVOKED_SESSIONS:
                return False
    return True


def _session_payload(cookie_header: str, config: AuthConfig) -> dict[str, Any] | None:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header or "")
    except Exception:  # noqa: BLE001 - malformed cookies are simply unauthenticated.
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return _decode(morsel.value, config.secret) if morsel else None


def _constant_time_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _session_revocation_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _prune_revoked_sessions_locked(now: int) -> None:
    for key in [key for key, expiry in _REVOKED_SESSIONS.items() if expiry <= now]:
        _REVOKED_SESSIONS.pop(key, None)


def _encode(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = _sign(body, secret)
    return f"{body}.{signature}"


def _decode(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = _sign(body, secret)
    if not hmac.compare_digest(signature, expected):
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sign(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _setting(section: dict[str, object], key: str, env_name: str, default: str = "") -> str:
    if key in section:
        return str(section.get(key) or "").strip()
    return os.environ.get(env_name, default).strip()
