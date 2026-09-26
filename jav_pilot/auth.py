"""Stable import path for deployment tooling.

``ops/nas_release.py`` imports ``jav_pilot.auth`` inside release images of
any version, so this path must keep working across layout changes. The
implementation lives in :mod:`jav_pilot.security.auth`.
"""

from .security.auth import (
    SESSION_COOKIE,
    AuthConfig,
    authenticate,
    clear_session_cookie,
    make_session_cookie,
    public_auth_status,
    revoke_session,
    verify_session,
)

__all__ = [
    "SESSION_COOKIE",
    "AuthConfig",
    "authenticate",
    "clear_session_cookie",
    "make_session_cookie",
    "public_auth_status",
    "revoke_session",
    "verify_session",
]
