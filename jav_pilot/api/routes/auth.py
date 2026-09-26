"""Authentication endpoints."""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus

from ...config.runtime_config import (
    RuntimeConfigError,
    runtime_config_path,
    update_auth_config,
)
from ...security.auth import (
    AuthConfig,
    authenticate,
    clear_session_cookie,
    make_session_cookie,
    public_auth_status,
    revoke_session,
)
from ...security.posture import evaluate_security_posture
from .. import state
from ..base import BaseHandler
from ..request import (
    request_is_secure,
    request_peer_host,
    request_server_host,
    request_server_port,
)


def _security_posture(
    host: str,
    config: AuthConfig | None = None,
    port: int | None = None,
):
    active = config or AuthConfig.from_env()
    return evaluate_security_posture(
        host=host,
        auth_enabled=active.enabled,
        auth_configured=active.configured,
        secret_persistent=active.secret_persistent,
        plain_password=active.password,
        config_path=runtime_config_path(),
        listen_port=port,
    )


class AuthRoutes(BaseHandler):
    def _handle_auth_status(self) -> None:
        config = AuthConfig.from_env()
        authenticated = self._authenticated()
        status = {
            "enabled": config.enabled,
            "configured": config.configured,
            "authenticated": authenticated,
        }
        if authenticated:
            status.update(public_auth_status(config))
            status["security"] = _security_posture(
                request_server_host(self), config, request_server_port(self)
            ).to_dict()
        self._send_json(status)

    def _handle_auth_login(self) -> None:
        try:
            raw = self._read_body(16 * 1024)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(
                {"ok": False, "error": str(exc) or "invalid JSON body"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                {"ok": False, "error": "request body must be an object"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        config = AuthConfig.from_env()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        source = request_peer_host(self)
        if not config.enabled:
            self._send_json({"ok": True, "auth": public_auth_status(config)})
            return
        if not config.configured:
            self._send_json(
                {"ok": False, "error": "authentication is not configured"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not state.LOGIN_VERIFICATION_SLOTS.acquire(blocking=False):
            self._send_json(
                {"ok": False, "error": "登录验证繁忙，请稍后重试"},
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": "1"},
            )
            return
        try:
            retry_after = state.LOGIN_RATE_LIMITER.begin_attempt(source, username)
            if retry_after:
                self._send_json(
                    {"ok": False, "error": "登录尝试过于频繁，请稍后重试"},
                    HTTPStatus.TOO_MANY_REQUESTS,
                    headers={"Retry-After": str(retry_after)},
                )
                return
            try:
                authenticated = authenticate(username, password, config)
            except BaseException:
                state.LOGIN_RATE_LIMITER.finish_attempt(
                    source,
                    username,
                    success=False,
                )
                raise
            retry_after = state.LOGIN_RATE_LIMITER.finish_attempt(
                source,
                username,
                success=authenticated,
            )
        finally:
            state.LOGIN_VERIFICATION_SLOTS.release()
        if not authenticated:
            if retry_after:
                self._send_json(
                    {"ok": False, "error": "登录尝试过于频繁，请稍后重试"},
                    HTTPStatus.TOO_MANY_REQUESTS,
                    headers={"Retry-After": str(retry_after)},
                )
                return
            self._send_json(
                {"ok": False, "error": "账号或密码不正确"}, HTTPStatus.UNAUTHORIZED
            )
            return
        self._send_json(
            {"ok": True, "auth": public_auth_status(config)},
            headers={
                "Set-Cookie": make_session_cookie(
                    config,
                    secure=request_is_secure(self),
                )
            },
        )

    def _handle_auth_logout(self) -> None:
        revoke_session(self.headers.get("Cookie", ""))
        self._send_json(
            {"ok": True},
            headers={
                "Set-Cookie": clear_session_cookie(
                    secure=request_is_secure(self),
                )
            },
        )

    def _handle_auth_password(self) -> None:
        if not self._authenticated():
            self._send_unauthorized("/api/auth/password")
            return
        try:
            payload = self._read_json_body(16 * 1024)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        current_config = AuthConfig.from_env()
        current_password = str(payload.get("current_password") or "")
        if current_config.enabled and current_config.configured:
            if not authenticate(
                current_config.username, current_password, current_config
            ):
                self._send_json(
                    {"ok": False, "error": "当前密码不正确"}, HTTPStatus.UNAUTHORIZED
                )
                return

        username = str(
            payload.get("username") or current_config.username or "admin"
        ).strip()
        new_password = str(payload.get("new_password") or "")
        if len(new_password) < 12:
            self._send_json(
                {"ok": False, "error": "新密码至少需要 12 个字符"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if len(new_password) > 512:
            self._send_json(
                {"ok": False, "error": "新密码不能超过 512 个字符"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            update_auth_config(
                username=username,
                password=new_password,
                secret=secrets.token_urlsafe(32),
                enabled=True,
            )
        except (RuntimeConfigError, OSError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        next_config = AuthConfig.from_env()
        self._send_json(
            {"ok": True, "auth": public_auth_status(next_config)},
            headers={
                "Set-Cookie": make_session_cookie(
                    next_config,
                    secure=request_is_secure(self),
                )
            },
        )
