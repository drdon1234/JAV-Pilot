"""Values derived from the process environment: store locations and build revision."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ...config.paths import default_database_path
from ...config.runtime_config import runtime_config_path
from ...config.settings import settings_path
from ...search.detail_prefetch import DetailPrefetchUnavailableError
from ...search.resources.errors import ResourceSearchUnavailableError
from ...search.session_store import MetadataSearchStoreError
from ...sites.diagnostic_store import SiteDiagnosticStoreError


def metadata_search_database_path() -> Path:
    configured = os.environ.get("JAV_PILOT_METADATA_SEARCH_DATABASE_PATH", "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else runtime_config_path().with_name("metadata_search_sessions.sqlite3")
    )
    if not path.is_absolute():
        raise MetadataSearchStoreError("metadata search database path must be absolute")
    return path


def detail_prefetch_database_path() -> Path:
    configured = os.environ.get("JAV_PILOT_DETAIL_PREFETCH_DATABASE_PATH", "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else settings_path().parent / "detail_prefetch.sqlite3"
    )
    try:
        return path.resolve()
    except OSError as exc:
        raise DetailPrefetchUnavailableError(
            "detail prefetch database path is invalid"
        ) from exc


def resource_search_database_path() -> Path:
    raw = os.environ.get("JAV_PILOT_WEB_DOWNLOAD_DATABASE_PATH", "").strip()
    path = Path(raw).expanduser() if raw else default_database_path("web_downloads.sqlite3")
    if not path.is_absolute():
        raise ResourceSearchUnavailableError("resource search database path is invalid")
    return path


def site_diagnostic_database_path() -> Path:
    configured = os.environ.get("JAV_PILOT_SITE_DIAGNOSTIC_DATABASE_PATH", "").strip()
    database_path = (
        Path(configured).expanduser()
        if configured
        else runtime_config_path().with_name("site_diagnostics.sqlite3")
    )
    if not database_path.is_absolute():
        raise SiteDiagnosticStoreError("site diagnostic database path must be absolute")
    return database_path


def app_revision() -> str:
    revision = os.environ.get("JAV_PILOT_REVISION", "unknown").strip()
    return revision if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", revision) else "unknown"
