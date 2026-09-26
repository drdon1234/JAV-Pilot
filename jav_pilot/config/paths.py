"""Defaults for paths on the application host, resolved when configuration is read."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_data_dir() -> Path:
    configured_app = os.environ.get("JAV_PILOT_APP_CONFIG_PATH", "").strip()
    if configured_app:
        return Path(configured_app).expanduser().absolute().parent
    configured_data = os.environ.get("JAV_PILOT_DATA_DIR", "").strip()
    if configured_data:
        return Path(configured_data).expanduser().absolute()
    configured_settings = os.environ.get("JAV_PILOT_SETTINGS_PATH", "").strip()
    if configured_settings:
        return Path(configured_settings).expanduser().absolute().parent
    return Path.cwd() / "data"


def default_database_path(filename: str) -> Path:
    return runtime_data_dir() / filename


def default_library_path() -> Path:
    return runtime_data_dir() / "media" / "JAV"


def default_staging_path() -> Path:
    return runtime_data_dir() / "downloads" / "jav-web"


def default_nfo_backup_path() -> Path:
    return runtime_data_dir() / "media_metadata_nfo_backups"


def default_history_backup_root() -> Path:
    data = runtime_data_dir()
    return data.with_name(data.name + "-backups")
