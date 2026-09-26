from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from ..search.detail_prefetch import (
    DETAIL_PREFETCH_SCHEMA_COMPONENT,
    DETAIL_PREFETCH_SCHEMA_VERSION,
)
from ..downloads.replacements import (
    SCHEMA_COMPONENT as DOWNLOAD_REPLACEMENT_SCHEMA_COMPONENT,
)
from ..downloads.replacements import SCHEMA_VERSION as DOWNLOAD_REPLACEMENT_SCHEMA_VERSION
from ..media_metadata.store import (
    CURRENT_SCHEMA_VERSION as MEDIA_METADATA_SCHEMA_VERSION,
)
from ..media_metadata.review.models import (
    CURRENT_SCHEMA_VERSION as MEDIA_METADATA_REVIEW_SCHEMA_VERSION,
)
from ..library.models import CURRENT_SCHEMA_VERSION as MEDIA_LIBRARY_SCHEMA_VERSION
from ..search.session_store import (
    METADATA_SEARCH_SCHEMA_COMPONENT,
    METADATA_SEARCH_SCHEMA_VERSION,
)
from ..torrent.magnet_selection_store import (
    SCHEMA_COMPONENT as MAGNET_SELECTION_SCHEMA_COMPONENT,
    SCHEMA_VERSION as MAGNET_SELECTION_SCHEMA_VERSION,
)
from ..notifications.outbox import NOTIFICATION_SCHEMA_VERSION
from ..search.resources.models import RESOURCE_SEARCH_SCHEMA_VERSION
from ..config.runtime_config import CURRENT_SCHEMA_VERSION as APP_CONFIG_SCHEMA_VERSION
from ..config.settings import CURRENT_SETTINGS_SCHEMA_VERSION
from ..sites.diagnostic_store import SITE_DIAGNOSTIC_SCHEMA_VERSION
from ..web_download.batches.schema import BATCH_SCHEMA_VERSION
from ..web_download.jobs import WEB_DOWNLOAD_SCHEMA_VERSION


SCHEMA_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class SchemaComponent:
    component: str
    kind: str
    current_version: int
    minimum_reader_version: int
    downgrade_requires_restore: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


SCHEMA_COMPONENTS = (
    SchemaComponent(
        "app_config",
        "json",
        APP_CONFIG_SCHEMA_VERSION,
        APP_CONFIG_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "settings",
        "json",
        CURRENT_SETTINGS_SCHEMA_VERSION,
        CURRENT_SETTINGS_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "web_downloads",
        "sqlite",
        WEB_DOWNLOAD_SCHEMA_VERSION,
        WEB_DOWNLOAD_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "web_download_batches",
        "sqlite",
        BATCH_SCHEMA_VERSION,
        BATCH_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        DOWNLOAD_REPLACEMENT_SCHEMA_COMPONENT,
        "sqlite",
        DOWNLOAD_REPLACEMENT_SCHEMA_VERSION,
        DOWNLOAD_REPLACEMENT_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "resource_search",
        "sqlite",
        RESOURCE_SEARCH_SCHEMA_VERSION,
        RESOURCE_SEARCH_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        MAGNET_SELECTION_SCHEMA_COMPONENT,
        "sqlite",
        MAGNET_SELECTION_SCHEMA_VERSION,
        MAGNET_SELECTION_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        METADATA_SEARCH_SCHEMA_COMPONENT,
        "sqlite",
        METADATA_SEARCH_SCHEMA_VERSION,
        METADATA_SEARCH_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        DETAIL_PREFETCH_SCHEMA_COMPONENT,
        "sqlite",
        DETAIL_PREFETCH_SCHEMA_VERSION,
        DETAIL_PREFETCH_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "media_metadata",
        "sqlite",
        MEDIA_METADATA_SCHEMA_VERSION,
        MEDIA_METADATA_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "media_metadata_review",
        "sqlite",
        MEDIA_METADATA_REVIEW_SCHEMA_VERSION,
        MEDIA_METADATA_REVIEW_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "notifications",
        "sqlite",
        NOTIFICATION_SCHEMA_VERSION,
        NOTIFICATION_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "site_diagnostics",
        "sqlite",
        SITE_DIAGNOSTIC_SCHEMA_VERSION,
        SITE_DIAGNOSTIC_SCHEMA_VERSION,
        True,
    ),
    SchemaComponent(
        "media_library",
        "sqlite",
        MEDIA_LIBRARY_SCHEMA_VERSION,
        MEDIA_LIBRARY_SCHEMA_VERSION,
        True,
    ),
)


def runtime_schema_contract() -> dict[str, object]:
    return {
        "contract_version": SCHEMA_CONTRACT_VERSION,
        "components": [component.to_dict() for component in SCHEMA_COMPONENTS],
    }


def sqlite_schema_versions(path: Path) -> dict[str, int]:
    versions: dict[str, int] = {}
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_migrations'"
        ).fetchone()
        if table is None:
            return versions
        rows = connection.execute(
            "SELECT component, MAX(version) FROM schema_migrations GROUP BY component"
        ).fetchall()
        for component, version in rows:
            if isinstance(component, str) and isinstance(version, int):
                versions[component] = version
        return versions
    finally:
        connection.close()
