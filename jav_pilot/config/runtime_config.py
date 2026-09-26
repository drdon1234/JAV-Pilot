from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import APP_CONFIG_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION
from ..security.passwords import PasswordHashError, hash_password, validate_password_hash
from ..notifications.adapters import (
    GotifyConfig,
    NasNotificationConfig,
    NotificationConfig,
    TelegramConfig,
    WebhookConfig,
)
from ..core.migrations import JSONMigration, MigrationError, migrate_json
from .qb_paths import (
    DEFAULT_QB_APP_LIBRARY_PATH,
    DEFAULT_QB_LIBRARY_PATH,
    DEFAULT_QB_STAGING_PATH,
    QbPathError,
    is_legacy_shared_staging_path,
    normalize_qb_path,
    validate_qb_library_mapping,
    validate_qb_staging_path,
    validate_qb_roots,
)
from .settings import (
    SettingsError,
    load_settings,
    validate_organizer_destinations,
)
from ..security.posture import (
    SecurityPostureError,
    validate_new_password,
    validate_session_secret,
)
from ..core.storage import atomic_write_text, backup_file
from .paths import runtime_data_dir

_P = ParamSpec("_P")
_R = TypeVar("_R")
_RUNTIME_CONFIG_LOCK = threading.RLock()
_RUNTIME_CONFIG_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

_HISTORY_RETENTION_TYPES = frozenset({"web", "batch", "metadata"})
_HISTORY_RETENTION_FIELDS = frozenset(
    {*_HISTORY_RETENTION_TYPES, "auto_enabled", "timezone", "hour"}
)
DEFAULT_HISTORY_RETENTION_TIMEZONE = "Asia/Shanghai"
DEFAULT_HISTORY_RETENTION_HOUR = 3
_IANA_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")

_NOTIFICATION_CHANNEL_FIELDS = {
    "webhook": frozenset(
        {"endpoint", "signing_secret", "private_origin", "pinned_addresses"}
    ),
    "gotify": frozenset(
        {"origin", "app_token", "private_origin", "pinned_addresses", "priority"}
    ),
    "telegram": frozenset(
        {
            "bot_token",
            "chat_id",
            "api_origin",
            "private_origin",
            "pinned_addresses",
        }
    ),
    "nas": frozenset(
        {"endpoint", "signing_secret", "private_origin", "pinned_addresses"}
    ),
}


class RuntimeConfigError(RuntimeError):
    pass


@contextmanager
def runtime_config_transaction() -> Iterator[None]:
    with _RUNTIME_CONFIG_LOCK:
        yield


def _runtime_config_locked(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with runtime_config_transaction():
            return function(*args, **kwargs)

    return locked


def runtime_config_path() -> Path:
    configured = os.environ.get("JAV_PILOT_APP_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return runtime_data_dir() / "app_config.json"


@_runtime_config_locked
def load_runtime_config(
    path: Path | None = None,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    path = path or runtime_config_path()
    if not path.exists():
        _RUNTIME_CONFIG_CACHE.pop(str(path.absolute()), None)
        return {"schema_version": CURRENT_SCHEMA_VERSION}
    cache_key = str(path.absolute())
    if fault_injector is None:
        stat_result = path.stat()
        signature = (int(stat_result.st_mtime_ns), int(stat_result.st_size))
        cached = _RUNTIME_CONFIG_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return copy.deepcopy(cached[1])
    payload = _read_runtime_config_payload(path)
    config, migrated = _normalize_runtime_config(
        payload,
        fault_injector=fault_injector,
    )
    if not migrated:
        result = config
    else:
        try:
            result = save_runtime_config(config, path)
        except OSError:
            # A read remains usable even when a read-time migration cannot be
            # persisted (for example a temporarily read-only volume).  Save
            # endpoints still report write failures explicitly.
            result = config
    if fault_injector is None:
        stat_result = path.stat()
        _RUNTIME_CONFIG_CACHE[cache_key] = (
            (int(stat_result.st_mtime_ns), int(stat_result.st_size)),
            copy.deepcopy(result),
        )
    return result


@_runtime_config_locked
def save_runtime_config(
    payload: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    path = path or runtime_config_path()
    config = normalize_runtime_config(payload)
    if path.exists():
        existing = _read_runtime_config_payload(path)
        _check_runtime_config_fields(existing)
        if _schema_version(existing) > CURRENT_SCHEMA_VERSION:
            raise RuntimeConfigError(
                "app config schema is newer than this application supports"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    raw = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, raw)
    stat_result = path.stat()
    _RUNTIME_CONFIG_CACHE[str(path.absolute())] = (
        (int(stat_result.st_mtime_ns), int(stat_result.st_size)),
        copy.deepcopy(config),
    )
    return config


@_runtime_config_locked
def update_qbittorrent_config(
    fields: dict[str, Any],
    *,
    password_action: str = "keep",
    settings: dict[str, Any] | None = None,
    path: Path | None = None,
    expected_app_library_root: str | None = None,
    require_accessible_mapping: bool = False,
) -> dict[str, Any]:
    path = path or runtime_config_path()
    if path.exists():
        config = _normalize_runtime_config(_read_runtime_config_payload(path))[0]
    else:
        config = {"schema_version": CURRENT_SCHEMA_VERSION}
    qb = copy.deepcopy(config.get("qbittorrent", {}))
    if not isinstance(qb, dict):
        qb = {}

    for key in (
        "url",
        "username",
        "category",
        "save_path",
        "library_path",
        "app_library_path",
        "tags",
    ):
        if key in fields:
            qb[key] = fields.get(key)

    action = str(password_action or "keep").strip().lower()
    if action == "set":
        password = _safe_text(fields.get("password"), 512)
        if not password:
            raise RuntimeConfigError(
                "password cannot be empty when setting qB password"
            )
        qb["password"] = password
    elif action == "clear":
        qb["password"] = ""
    elif action != "keep":
        raise RuntimeConfigError("invalid qB password action")

    if "category" not in qb:
        qb["category"] = "jav"
    if "save_path" not in qb:
        qb["save_path"] = DEFAULT_QB_STAGING_PATH
    if "library_path" not in qb:
        qb["library_path"] = DEFAULT_QB_LIBRARY_PATH
    if "app_library_path" not in qb:
        qb["app_library_path"] = DEFAULT_QB_APP_LIBRARY_PATH

    config["qbittorrent"] = qb
    prospective = normalize_runtime_config(config)
    prospective_qb = prospective.get("qbittorrent", {})
    try:
        validate_organizer_destinations(
            settings if settings is not None else load_settings(),
            category=str(prospective_qb.get("category") or ""),
            save_path=str(prospective_qb.get("save_path") or ""),
        )
    except SettingsError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    try:
        validate_qb_library_mapping(
            str(prospective_qb.get("library_path") or ""),
            str(prospective_qb.get("app_library_path") or ""),
            expected_app_root=expected_app_library_root,
            require_accessible=require_accessible_mapping,
        )
    except QbPathError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    return save_runtime_config(prospective, path)


@_runtime_config_locked
def update_auth_config(
    *,
    username: str,
    password: str,
    secret: str,
    enabled: bool = True,
    path: Path | None = None,
) -> dict[str, Any]:
    try:
        clean_password = validate_new_password(password, username)
        clean_secret = validate_session_secret(secret, clean_password)
    except SecurityPostureError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    config = load_runtime_config(path)
    config["auth"] = {
        "enabled": bool(enabled),
        "username": username,
        "password_hash": hash_password(clean_password),
        "secret": clean_secret,
    }
    return save_runtime_config(config, path)


@_runtime_config_locked
def update_notification_config(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
    validator: Callable[[NotificationConfig], None] | None = None,
) -> dict[str, Any]:
    prospective, candidate = prepare_notification_config(payload, path=path)
    if validator is not None:
        validator(candidate)
    return save_prepared_notification_config(prospective, path=path)


@_runtime_config_locked
def prepare_notification_config(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
) -> tuple[dict[str, Any], NotificationConfig]:
    if not isinstance(payload, dict) or not set(payload).issubset(
        _NOTIFICATION_CHANNEL_FIELDS
    ):
        raise RuntimeConfigError("notification config contains an invalid channel")
    config = load_runtime_config(path)
    notifications = copy.deepcopy(config.get("notifications", {}))
    if not isinstance(notifications, dict):
        notifications = {}
    for channel_name, raw_update in payload.items():
        if not isinstance(raw_update, dict):
            raise RuntimeConfigError("notification channel config must be an object")
        allowed_fields = _NOTIFICATION_CHANNEL_FIELDS[channel_name]
        allowed_input = {*allowed_fields, "enabled", "clear_fields"}
        if not set(raw_update).issubset(allowed_input):
            raise RuntimeConfigError("notification channel config has an invalid field")
        current = notifications.get(channel_name, {})
        channel = dict(current) if isinstance(current, dict) else {}
        if "enabled" in raw_update:
            if not isinstance(raw_update["enabled"], bool):
                raise RuntimeConfigError("notification enabled state must be boolean")
            channel["enabled"] = raw_update["enabled"]
        clear_fields = raw_update.get("clear_fields", [])
        if not isinstance(clear_fields, list) or any(
            not isinstance(field, str) or field not in allowed_fields
            for field in clear_fields
        ):
            raise RuntimeConfigError("notification clear fields are invalid")
        for field in clear_fields:
            channel.pop(field, None)
        for field in allowed_fields:
            if field not in raw_update:
                continue
            value = raw_update[field]
            if field == "priority" or field == "pinned_addresses":
                channel[field] = value
                continue
            text = _notification_text(
                value,
                2048
                if field in {"private_origin", "origin", "api_origin", "endpoint"}
                else 1024,
            )
            if text:
                channel[field] = text
        notifications[channel_name] = channel
    config["notifications"] = notifications
    prospective = normalize_runtime_config(config)
    return prospective, _notification_config_from_runtime(prospective)


@_runtime_config_locked
def save_prepared_notification_config(
    prospective: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    return save_runtime_config(prospective, path)


@_runtime_config_locked
def update_history_retention_config(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, object]:
    config = load_runtime_config(path)
    current = _history_retention_from_runtime(config)
    if not isinstance(payload, dict):
        raise RuntimeConfigError("history retention config is invalid")
    config["history_retention"] = _normalize_history_retention({**current, **payload})
    saved = save_runtime_config(config, path)
    return _history_retention_from_runtime(saved)


def history_retention_config(path: Path | None = None) -> dict[str, object]:
    return _history_retention_from_runtime(load_runtime_config(path))


def _history_retention_from_runtime(
    config: dict[str, Any],
) -> dict[str, object]:
    raw = config.get("history_retention", {})
    return _normalize_history_retention(raw)


def history_retention_policy(path: Path | None = None) -> dict[str, int | None]:
    config = history_retention_config(path)
    return {
        task_type: config[task_type]  # type: ignore[dict-item]
        for task_type in sorted(_HISTORY_RETENTION_TYPES)
    }


def notification_config(path: Path | None = None) -> NotificationConfig:
    return _notification_config_from_runtime(load_runtime_config(path))


def _notification_config_from_runtime(config: dict[str, Any]) -> NotificationConfig:
    raw = config.get("notifications", {})
    channels = raw if isinstance(raw, dict) else {}
    webhook = _notification_channel(channels, "webhook")
    gotify = _notification_channel(channels, "gotify")
    telegram = _notification_channel(channels, "telegram")
    nas = _notification_channel(channels, "nas")
    return NotificationConfig(
        webhook=WebhookConfig(
            enabled=bool(webhook.get("enabled", False)),
            endpoint=str(webhook.get("endpoint") or ""),
            signing_secret=str(webhook.get("signing_secret") or "") or None,
            private_origin=str(webhook.get("private_origin") or "") or None,
            pinned_addresses=tuple(webhook.get("pinned_addresses") or ()),
        ),
        gotify=GotifyConfig(
            enabled=bool(gotify.get("enabled", False)),
            origin=str(gotify.get("origin") or ""),
            app_token=str(gotify.get("app_token") or ""),
            private_origin=str(gotify.get("private_origin") or "") or None,
            pinned_addresses=tuple(gotify.get("pinned_addresses") or ()),
            priority=int(gotify.get("priority", 5)),
        ),
        telegram=TelegramConfig(
            enabled=bool(telegram.get("enabled", False)),
            bot_token=str(telegram.get("bot_token") or ""),
            chat_id=str(telegram.get("chat_id") or ""),
            api_origin=str(telegram.get("api_origin") or "https://api.telegram.org"),
            private_origin=str(telegram.get("private_origin") or "") or None,
            pinned_addresses=tuple(telegram.get("pinned_addresses") or ()),
        ),
        nas=NasNotificationConfig(
            enabled=bool(nas.get("enabled", False)),
            endpoint=str(nas.get("endpoint") or ""),
            signing_secret=str(nas.get("signing_secret") or "") or None,
            private_origin=str(nas.get("private_origin") or "") or None,
            pinned_addresses=tuple(nas.get("pinned_addresses") or ()),
        ),
    )


def notification_public_config(path: Path | None = None) -> dict[str, object]:
    return notification_public_config_for(notification_config(path))


def notification_public_config_for(config: NotificationConfig) -> dict[str, object]:
    return {
        "webhook": {
            "enabled": config.webhook.enabled,
            "target_configured": bool(config.webhook.endpoint),
            "credential_configured": bool(config.webhook.signing_secret),
            "private_target": bool(config.webhook.private_origin),
            "pinned_address_count": len(config.webhook.pinned_addresses),
        },
        "gotify": {
            "enabled": config.gotify.enabled,
            "target_configured": bool(config.gotify.origin),
            "credential_configured": bool(config.gotify.app_token),
            "private_target": bool(config.gotify.private_origin),
            "pinned_address_count": len(config.gotify.pinned_addresses),
            "priority": config.gotify.priority,
        },
        "telegram": {
            "enabled": config.telegram.enabled,
            "target_configured": bool(config.telegram.api_origin),
            "credential_configured": bool(
                config.telegram.bot_token and config.telegram.chat_id
            ),
            "private_target": bool(config.telegram.private_origin),
            "pinned_address_count": len(config.telegram.pinned_addresses),
        },
        "nas": {
            "enabled": config.nas.enabled,
            "target_configured": bool(config.nas.endpoint),
            "credential_configured": bool(config.nas.signing_secret),
            "private_target": bool(config.nas.private_origin),
            "pinned_address_count": len(config.nas.pinned_addresses),
        },
    }


def normalize_runtime_config(
    payload: dict[str, Any],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    return _normalize_runtime_config(payload, fault_injector=fault_injector)[0]


def _normalize_runtime_config(
    payload: dict[str, Any],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise RuntimeConfigError("app config must be an object")
    try:
        migrated_payload, version_migrated = migrate_json(
            payload,
            component="app_config",
            current_version=CURRENT_SCHEMA_VERSION,
            migrations=_app_config_migrations(),
            fault_injector=fault_injector,
        )
    except MigrationError as exc:
        raise RuntimeConfigError(str(exc).replace("app_config", "app config")) from exc

    normalized = _normalize_current_runtime_config(migrated_payload)
    return normalized, version_migrated or normalized != migrated_payload


def _normalize_current_runtime_config(payload: dict[str, Any]) -> dict[str, Any]:
    _check_runtime_config_fields(payload)
    normalized: dict[str, Any] = {"schema_version": CURRENT_SCHEMA_VERSION}
    if "qbittorrent" in payload:
        normalized["qbittorrent"] = _normalize_qbittorrent(payload.get("qbittorrent"))
    if "auth" in payload:
        normalized_auth, _auth_migrated = _normalize_auth(payload.get("auth"))
        normalized["auth"] = normalized_auth
    if "notifications" in payload:
        normalized["notifications"] = _normalize_notifications(
            payload.get("notifications")
        )
    if "history_retention" in payload:
        normalized["history_retention"] = _normalize_history_retention(
            payload.get("history_retention")
        )
    return normalized


def _check_runtime_config_fields(payload: dict[str, Any]) -> None:
    if set(payload) - {"schema_version", "qbittorrent", "auth", "notifications", "history_retention"}:
        raise RuntimeConfigError("app config contains unrecognized fields; original file was preserved")


def _app_config_migrations() -> tuple[JSONMigration, ...]:
    return (
        JSONMigration(1, _migrate_app_config_v1),
        JSONMigration(2, _migrate_app_config_v2, _verify_app_config_v2),
        JSONMigration(3, _migrate_app_config_v3, _verify_app_config_v3),
        JSONMigration(4, _migrate_app_config_v4, _verify_app_config_v4),
        JSONMigration(5, _migrate_app_config_v5, _verify_app_config_v5),
        JSONMigration(6, _migrate_app_config_v6, _verify_app_config_v6),
        JSONMigration(7, _migrate_app_config_v7, _verify_app_config_v7),
        JSONMigration(8, _migrate_app_config_v8, _verify_app_config_v8),
    )


def _migrate_app_config_v1(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _migrate_app_config_v2(payload: dict[str, Any]) -> dict[str, Any]:
    if "auth" in payload:
        payload["auth"] = _normalize_auth(payload.get("auth"))[0]
    return payload


def _migrate_app_config_v3(payload: dict[str, Any]) -> dict[str, Any]:
    qb = payload.get("qbittorrent")
    if qb is None:
        return payload
    had_app_library_path = isinstance(qb, dict) and "app_library_path" in qb
    normalized = _normalize_qbittorrent(
        qb,
        allow_legacy_staging=True,
        migrate_required_destination=True,
    )
    save_path = normalized.get("save_path")
    if not save_path or is_legacy_shared_staging_path(save_path):
        normalized["save_path"] = DEFAULT_QB_STAGING_PATH
    if not normalized.get("library_path"):
        normalized["library_path"] = DEFAULT_QB_LIBRARY_PATH
    try:
        validate_qb_roots(normalized["save_path"], normalized["library_path"])
    except QbPathError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    if not had_app_library_path:
        normalized.pop("app_library_path", None)
    payload["qbittorrent"] = normalized
    return payload


def _migrate_app_config_v4(payload: dict[str, Any]) -> dict[str, Any]:
    qb = payload.get("qbittorrent")
    if qb is None:
        return payload
    had_app_library_path = isinstance(qb, dict) and "app_library_path" in qb
    normalized = _normalize_qbittorrent(
        qb,
        migrate_required_destination=True,
    )
    if not had_app_library_path:
        normalized.pop("app_library_path", None)
    payload["qbittorrent"] = normalized
    return payload


def _migrate_app_config_v5(payload: dict[str, Any]) -> dict[str, Any]:
    if "qbittorrent" in payload:
        payload["qbittorrent"] = _normalize_qbittorrent(payload.get("qbittorrent"))
    return payload


def _migrate_app_config_v6(payload: dict[str, Any]) -> dict[str, Any]:
    if "notifications" in payload:
        payload["notifications"] = _normalize_notifications(
            payload.get("notifications")
        )
    return payload


def _migrate_app_config_v7(payload: dict[str, Any]) -> dict[str, Any]:
    if "history_retention" in payload:
        payload["history_retention"] = _normalize_history_retention(
            payload.get("history_retention")
        )
    return payload


def _migrate_app_config_v8(payload: dict[str, Any]) -> dict[str, Any]:
    if "history_retention" in payload:
        payload["history_retention"] = _normalize_history_retention(
            payload.get("history_retention")
        )
    return payload


def _verify_app_config_v2(payload: dict[str, Any]) -> None:
    auth = payload.get("auth")
    if auth is not None and (not isinstance(auth, dict) or "password" in auth):
        raise RuntimeConfigError("app config v2 authentication migration is incomplete")


def _verify_app_config_v3(payload: dict[str, Any]) -> None:
    qb = payload.get("qbittorrent")
    if qb is None:
        return
    if not isinstance(qb, dict):
        raise RuntimeConfigError("app config v3 qBittorrent migration is incomplete")
    try:
        validate_qb_staging_path(str(qb.get("save_path") or ""))
        if str(qb.get("library_path") or "").strip():
            validate_qb_roots(
                str(qb.get("save_path") or ""),
                str(qb.get("library_path") or ""),
            )
    except QbPathError as exc:
        raise RuntimeConfigError(str(exc)) from exc


def _verify_app_config_v4(payload: dict[str, Any]) -> None:
    _verify_app_config_v3(payload)
    qb = payload.get("qbittorrent")
    if isinstance(qb, dict) and (
        not str(qb.get("category") or "").strip()
        or not str(qb.get("save_path") or "").strip()
    ):
        raise RuntimeConfigError("app config v4 qBittorrent destination is incomplete")


def _verify_app_config_v5(payload: dict[str, Any]) -> None:
    _verify_app_config_v4(payload)
    qb = payload.get("qbittorrent")
    if isinstance(qb, dict) and not str(qb.get("app_library_path") or "").strip():
        raise RuntimeConfigError("app config v5 qBittorrent path mapping is incomplete")


def _verify_app_config_v6(payload: dict[str, Any]) -> None:
    if "notifications" in payload:
        _normalize_notifications(payload.get("notifications"))


def _verify_app_config_v7(payload: dict[str, Any]) -> None:
    if "history_retention" in payload:
        _normalize_history_retention(payload.get("history_retention"))


def _verify_app_config_v8(payload: dict[str, Any]) -> None:
    _verify_app_config_v7(payload)


def _normalize_history_retention(value: Any) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value).issubset(
        _HISTORY_RETENTION_FIELDS
    ):
        raise RuntimeConfigError("history retention config is invalid")
    normalized: dict[str, object] = {}
    for task_type in sorted(_HISTORY_RETENTION_TYPES):
        raw = value.get(task_type)
        if raw is None or raw is False or raw == 0 or raw == "0":
            normalized[task_type] = None
            continue
        if isinstance(raw, bool):
            raise RuntimeConfigError("history retention days are invalid")
        try:
            days = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeConfigError("history retention days are invalid") from exc
        if days < 1 or days > 36_500:
            raise RuntimeConfigError("history retention days are invalid")
        normalized[task_type] = days
    auto_enabled = value.get("auto_enabled", False)
    if not isinstance(auto_enabled, bool):
        raise RuntimeConfigError("history retention automatic state is invalid")
    timezone = str(
        value.get("timezone", DEFAULT_HISTORY_RETENTION_TIMEZONE) or ""
    ).strip()
    if (
        not timezone
        or len(timezone) > 128
        or _IANA_TIMEZONE_RE.fullmatch(timezone) is None
        or any(part in {".", ".."} for part in timezone.split("/"))
    ):
        raise RuntimeConfigError("history retention timezone is invalid")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeConfigError("history retention timezone is invalid") from exc
    hour = value.get("hour", DEFAULT_HISTORY_RETENTION_HOUR)
    if isinstance(hour, bool):
        raise RuntimeConfigError("history retention hour is invalid")
    try:
        clean_hour = int(hour)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeConfigError("history retention hour is invalid") from exc
    if clean_hour < 0 or clean_hour > 23 or str(hour).strip() != str(clean_hour):
        raise RuntimeConfigError("history retention hour is invalid")
    normalized.update(
        {
            "auto_enabled": auto_enabled,
            "timezone": timezone,
            "hour": clean_hour,
        }
    )
    if auto_enabled and not any(
        normalized[task_type] is not None for task_type in _HISTORY_RETENTION_TYPES
    ):
        raise RuntimeConfigError(
            "automatic history retention requires a retention period"
        )
    return normalized


def _normalize_notifications(value: Any) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value).issubset(
        _NOTIFICATION_CHANNEL_FIELDS
    ):
        raise RuntimeConfigError("notification config contains an invalid channel")
    normalized: dict[str, object] = {}
    for name, allowed_fields in _NOTIFICATION_CHANNEL_FIELDS.items():
        if name not in value:
            continue
        raw = value[name]
        if not isinstance(raw, dict) or not set(raw).issubset(
            {*allowed_fields, "enabled"}
        ):
            raise RuntimeConfigError("notification channel config has an invalid field")
        channel: dict[str, object] = {"enabled": bool(raw.get("enabled", False))}
        for field in allowed_fields:
            if field not in raw:
                continue
            if field == "priority":
                priority = _bounded_int(raw[field], minimum=-10, maximum=10)
                channel[field] = priority
            elif field == "pinned_addresses":
                channel[field] = _notification_pins(raw[field])
            elif field == "private_origin":
                text = _notification_text(raw[field], 2048)
                channel[field] = _notification_origin(text) if text else ""
            elif field in {"origin", "api_origin"}:
                text = _notification_text(raw[field], 2048)
                channel[field] = _notification_origin(text) if text else ""
            elif field == "endpoint":
                text = _notification_text(raw[field], 2048)
                channel[field] = _notification_endpoint(text) if text else ""
            else:
                channel[field] = _notification_text(raw[field], 1024)
        normalized[name] = channel
    return normalized


def _notification_channel(channels: dict[str, object], name: str) -> dict[str, object]:
    value = channels.get(name, {})
    return value if isinstance(value, dict) else {}


def _notification_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise RuntimeConfigError("notification endpoint is invalid")
    return value


def _notification_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeConfigError("notification origin is invalid")
    return value.rstrip("/")


def _notification_pins(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise RuntimeConfigError("notification address pins are invalid")
    try:
        pins = sorted({ipaddress.ip_address(str(item)).compressed for item in value})
    except ValueError as exc:
        raise RuntimeConfigError("notification address pins are invalid") from exc
    return pins


def _notification_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise RuntimeConfigError("notification text config is invalid")
    return text


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RuntimeConfigError("notification numeric config is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeConfigError("notification numeric config is invalid") from exc
    if not minimum <= parsed <= maximum:
        raise RuntimeConfigError("notification numeric config is invalid")
    return parsed


def _normalize_qbittorrent(
    qb: Any,
    *,
    allow_legacy_staging: bool = False,
    migrate_required_destination: bool = False,
) -> dict[str, str]:
    if not isinstance(qb, dict):
        raise RuntimeConfigError("qbittorrent config must be an object")
    normalized: dict[str, str] = {}
    if "url" in qb:
        url = _safe_text(qb.get("url"), 260).rstrip("/")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise RuntimeConfigError(
                    "qBittorrent URL must be a valid http:// or https:// URL"
                )
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise RuntimeConfigError(
                    "qBittorrent URL cannot contain credentials, query, or fragment"
                )
        normalized["url"] = url
    for key, limit in (
        ("username", 160),
        ("password", 512),
        ("category", 120),
        ("save_path", 260),
        ("library_path", 260),
        ("app_library_path", 260),
        ("tags", 180),
    ):
        if key in qb:
            normalized[key] = _safe_text(qb.get(key), limit)
    for key in ("save_path", "library_path", "app_library_path"):
        if normalized.get(key):
            try:
                normalized[key] = normalize_qb_path(normalized[key])
            except QbPathError as exc:
                raise RuntimeConfigError(str(exc)) from exc
    if migrate_required_destination:
        if not normalized.get("category"):
            normalized["category"] = "jav"
        if not normalized.get("save_path"):
            normalized["save_path"] = DEFAULT_QB_STAGING_PATH
    if not normalized.get("category"):
        raise RuntimeConfigError("qBittorrent category is required")
    if not normalized.get("save_path"):
        raise RuntimeConfigError("qBittorrent staging path is required")
    if not normalized.get("app_library_path"):
        normalized["app_library_path"] = DEFAULT_QB_APP_LIBRARY_PATH
    if not allow_legacy_staging:
        try:
            validate_qb_staging_path(normalized["save_path"])
        except QbPathError as exc:
            raise RuntimeConfigError(str(exc)) from exc
    if (
        normalized.get("save_path")
        and normalized.get("library_path")
        and not allow_legacy_staging
    ):
        try:
            validate_qb_roots(normalized["save_path"], normalized["library_path"])
        except QbPathError as exc:
            raise RuntimeConfigError(str(exc)) from exc
    try:
        validate_qb_library_mapping(
            normalized.get("library_path", ""),
            normalized["app_library_path"],
        )
    except QbPathError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    return normalized


def _read_runtime_config_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(f"cannot read app config: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeConfigError("app config must be an object")
    return payload


def _schema_version(payload: dict[str, Any]) -> int:
    raw = payload.get("schema_version")
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise RuntimeConfigError("app config schema version is invalid")
    return raw


def _normalize_auth(auth: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(auth, dict):
        raise RuntimeConfigError("auth config must be an object")
    username = _safe_text(auth.get("username"), 80) or "admin"
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", username):
        raise RuntimeConfigError("username may only contain letters, numbers, _ . @ -")
    password_hash = _safe_text(auth.get("password_hash"), 1024)
    legacy_password = _safe_text(auth.get("password"), 512)
    migrated = False
    if password_hash:
        try:
            password_hash = validate_password_hash(password_hash)
        except PasswordHashError as exc:
            raise RuntimeConfigError(str(exc)) from exc
    elif legacy_password:
        password_hash = hash_password(legacy_password)
        migrated = True

    return {
        "enabled": bool(auth.get("enabled", True)),
        "username": username,
        "password_hash": password_hash,
        "secret": _safe_text(auth.get("secret"), 512),
    }, migrated or "password" in auth


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r", "").strip()[:limit]
