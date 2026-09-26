from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .qb_paths import (
    DEFAULT_QB_APP_LIBRARY_PATH,
    DEFAULT_QB_LIBRARY_PATH,
    DEFAULT_QB_STAGING_PATH,
)
from .runtime_config import RuntimeConfigError, load_runtime_config


@dataclass(frozen=True)
class QbittorrentConfig:
    url: str = ""
    username: str = ""
    password: str = ""
    category: str = "jav"
    save_path: str = DEFAULT_QB_STAGING_PATH
    library_path: str = DEFAULT_QB_LIBRARY_PATH
    app_library_path: str = DEFAULT_QB_APP_LIBRARY_PATH
    tags: str = "jav-pilot"

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def public_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "url": _public_url(self.url),
            "username": self.username,
            "has_password": bool(self.password),
            "category": self.category,
            "save_path": self.save_path,
            "library_path": self.library_path,
            "app_library_path": self.app_library_path,
            "tags": self.tags,
        }


@dataclass(frozen=True)
class AppConfig:
    qbittorrent: QbittorrentConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        try:
            runtime = load_runtime_config()
        except RuntimeConfigError:
            runtime = {}
        qb = runtime.get("qbittorrent", {})
        if not isinstance(qb, dict):
            qb = {}
        return cls(
            qbittorrent=QbittorrentConfig(
                url=_setting(qb, "url", "JAV_PILOT_QB_URL"),
                username=_setting(qb, "username", "JAV_PILOT_QB_USERNAME"),
                password=_setting(qb, "password", "JAV_PILOT_QB_PASSWORD"),
                category=_setting(qb, "category", "JAV_PILOT_QB_CATEGORY", "jav"),
                save_path=_setting(qb, "save_path", "JAV_PILOT_QB_SAVE_PATH", DEFAULT_QB_STAGING_PATH),
                library_path=_setting(qb, "library_path", "JAV_PILOT_QB_LIBRARY_PATH", DEFAULT_QB_LIBRARY_PATH),
                app_library_path=_setting(
                    qb,
                    "app_library_path",
                    "JAV_PILOT_QB_APP_LIBRARY_PATH",
                    _env(
                        "JAV_PILOT_MEDIA_METADATA_LIBRARY_PATH",
                        DEFAULT_QB_APP_LIBRARY_PATH,
                    ),
                ),
                tags=_setting(qb, "tags", "JAV_PILOT_QB_TAGS", "jav-pilot"),
            )
        )

    def public_dict(self) -> dict[str, object]:
        return {"qbittorrent": self.qbittorrent.public_dict()}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _setting(section: dict[str, object], key: str, env_name: str, default: str = "") -> str:
    if key in section:
        return str(section.get(key) or "").strip()
    return _env(env_name, default)


def _public_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if not hostname:
            return ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return ""
