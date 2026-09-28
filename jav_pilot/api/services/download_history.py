"""Sources consulted when looking up previous downloads."""

from __future__ import annotations

from collections.abc import Callable

from ...config.app_config import AppConfig
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
        manager = media_library_manager()
        result = manager.index.store.list_entries(
            root_key=manager.index.root_key, query=code, limit=20
        )
        return list(result.get("items") or [])

    return entries
