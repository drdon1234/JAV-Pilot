"""Sources consulted when looking up previous downloads."""

from __future__ import annotations

from collections.abc import Callable

from ...config.app_config import AppConfig
from ...downloads.history import code_identity
from ...library.errors import MediaLibraryError
from ...library.worker import MediaLibraryConfig
from ...web_download.config import WebDownloadConfig
from ...web_download.errors import WebDownloadError
from .media import media_library_manager
from .torrents import torrent_history_snapshot
from .web_downloads import web_download_manager


def history_torrent_tasks() -> Callable[[], tuple[dict[str, object], ...]] | None:
    qbittorrent = AppConfig.from_env().qbittorrent
    if not qbittorrent.configured or not qbittorrent.category.strip():
        return None
    return lambda: torrent_history_snapshot(qbittorrent)


def history_web_jobs() -> Callable[[str], list[dict[str, object]]] | None:
    try:
        if not WebDownloadConfig.from_env().enabled:
            return None
    except (WebDownloadError, ValueError):
        return None
    return lambda code: web_download_manager().list(code=code, limit=20)


def history_library_entries() -> Callable[[str], list[dict[str, object]]] | None:
    try:
        if not MediaLibraryConfig.from_env().enabled:
            return None
    except (MediaLibraryError, ValueError):
        return None

    def entries(code: str) -> list[dict[str, object]]:
        identity = code_identity(code)
        if identity is None:
            return []
        manager = media_library_manager()
        rows: list[dict[str, object]] = []
        while True:
            result = manager.index.store.list_entries(
                root_key=manager.index.root_key, query=code,
                limit=500, offset=len(rows),
            )
            page = list(result.get("items") or [])
            rows.extend(page)
            if len(page) < 500:
                break
        return [row for row in rows if code_identity(row.get("code") or row.get("code_key")) == identity]

    return entries
