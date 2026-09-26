"""qBittorrent submission helpers and torrent history caching."""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Callable

from ...config.app_config import AppConfig, QbittorrentConfig
from ...config.settings import load_settings
from ...core.catalog_code import canonical_catalog_code
from ...media_metadata.manager import MediaMetadataConfig, MediaMetadataError
from ...media_metadata.store import MediaMetadataStoreError
from ...torrent.inputs import (
    DownloadInputError,
    ParsedDownloadBatch,
    ParsedDownloadInput,
    enrich_download_inputs_from_probe,
)
from ...torrent.magnet_probe import MagnetProbeConflictError
from ...torrent.organizer import apply_organizer
from ...torrent.qbittorrent import (
    TORRENT_HISTORY_MAX_TASKS,
    AddDownloadRequest,
    QbittorrentClient,
)
from .. import state
from .media import media_metadata_manager


def _torrent_history_cache_key(
    config: QbittorrentConfig,
) -> tuple[str, str, str, str, str]:
    public = config.public_dict()
    credential_fingerprint = hmac.digest(
        state.TORRENT_HISTORY_CACHE_CREDENTIAL_SALT,
        f"{config.username}\0{config.password}".encode("utf-8"),
        "sha256",
    ).hex()
    return (
        "qb-history-v1",
        str(public.get("url") or ""),
        str(public.get("username") or ""),
        str(public.get("category") or ""),
        credential_fingerprint,
    )


def torrent_history_snapshot(
    config: QbittorrentConfig,
) -> tuple[dict[str, object], ...]:
    key = _torrent_history_cache_key(config)
    cached = state.TORRENT_HISTORY_CACHE.get(key)
    if cached is not None:
        return cached
    with state.TORRENT_HISTORY_CACHE_LOCK:
        cached = state.TORRENT_HISTORY_CACHE.get(key)
        if cached is not None:
            return cached
        tasks = tuple(
            QbittorrentClient(config).list_torrent_history(
                category=config.category,
                max_tasks=TORRENT_HISTORY_MAX_TASKS,
            )
        )
        state.TORRENT_HISTORY_CACHE.set(key, tasks)
        return tasks


def invalidate_torrent_history_cache() -> None:
    with state.TORRENT_HISTORY_CACHE_LOCK:
        state.TORRENT_HISTORY_CACHE.clear()


def download_metadata_code(result: dict[str, object]) -> object | None:
    for key in ("code", "canonical_code"):
        value = result.get(key)
        if canonical_catalog_code(value, max_length=40) is not None:
            return value
    return None


def import_download_request(item: ParsedDownloadInput) -> AddDownloadRequest:
    display_name = (
        item.torrent_name
        or item.magnet.display_name
        or item.catalog_code
        or item.magnet.info_hash
    )
    result: dict[str, object] = {
        "title": display_name,
        "sources": [],
    }
    if item.catalog_code is not None:
        result["code"] = item.catalog_code
        result["canonical_code"] = item.catalog_code
    magnet_info = item.magnet.to_dict()
    magnet_info["source_id"] = "manual-import"
    return AddDownloadRequest(
        magnet=item.magnet.uri,
        name=display_name,
        auto_organize=True,
        result=result,
        magnet_info=magnet_info,
    )


def enrich_import_batch_from_probe(
    batch: ParsedDownloadBatch,
    probe: dict[str, object],
) -> ParsedDownloadBatch:
    if probe.get("purpose") != "metadata":
        raise DownloadInputError("probe does not belong to a metadata inspection")
    status = str(probe.get("status") or "")
    if status in {"queued", "running", "cleaning", "cancelling"}:
        raise MagnetProbeConflictError("metadata inspection is not finished")
    if status not in {"complete", "failed", "cancelled"}:
        raise DownloadInputError("metadata inspection status is invalid")
    probe_items = probe.get("items")
    if not isinstance(probe_items, list):
        raise DownloadInputError("metadata inspection result is invalid")
    batch_hashes = {item.magnet.info_hash for item in batch.items}
    probe_hashes = {
        str(item.get("info_hash") or "").strip().lower()
        for item in probe_items
        if isinstance(item, dict)
    }
    if probe_hashes != batch_hashes:
        raise DownloadInputError(
            "metadata inspection does not match the download input"
        )
    cleanup = probe.get("cleanup")
    if not isinstance(cleanup, dict) or not isinstance(cleanup.get("status"), str):
        raise DownloadInputError("metadata inspection cleanup result is invalid")
    if cleanup["status"] not in {"complete", "not_required"}:
        raise MagnetProbeConflictError("metadata inspection cleanup is incomplete")
    return enrich_download_inputs_from_probe(batch, probe_items)


def submit_qb_download(
    request: AddDownloadRequest,
    *,
    client: QbittorrentClient | None = None,
    settings: dict[str, object] | None = None,
    register_metadata: bool = True,
) -> dict[str, object]:
    organize_match = None
    if request.auto_organize:
        request, organize_match = apply_organizer(
            request,
            load_settings() if settings is None else settings,
        )
    metadata_code = download_metadata_code(request.result)
    before_submit: Callable[[str], None] | None = None
    metadata_registered: bool | None = None
    if metadata_code is not None and register_metadata:

        def persist_metadata(info_hash: str) -> None:
            nonlocal metadata_registered
            metadata_registered = enqueue_qb_metadata(info_hash, metadata_code)

        before_submit = persist_metadata
    downloader = client or QbittorrentClient(AppConfig.from_env().qbittorrent)
    result = downloader.add_magnet(request, before_submit=before_submit)
    invalidate_torrent_history_cache()
    if organize_match:
        result["organize"] = organize_match
    if (
        metadata_code is not None
        and register_metadata
        and metadata_registered is not True
    ):
        result["metadata_warning"] = (
            "Automatic metadata registration failed; scan the media library "
            "after the download completes"
        )
    return result


def enqueue_qb_metadata(info_hash: str, code: object) -> bool:
    try:
        media_metadata_manager().enqueue_qb(info_hash, code)
    except (
        MediaMetadataError,
        MediaMetadataStoreError,
        OSError,
        sqlite3.Error,
    ):
        return False
    return True


def discard_incomplete_qb_metadata(info_hash: str) -> bool:
    try:
        config = MediaMetadataConfig.from_env()
        if not config.enabled:
            return True
        media_metadata_manager(config).discard_qb(
            (info_hash,),
            delete_files=False,
        )
    except (
        MediaMetadataError,
        MediaMetadataStoreError,
        OSError,
        sqlite3.Error,
    ):
        return False
    return True


def delete_qb_with_metadata(
    client: QbittorrentClient,
    info_hashes: tuple[str, ...],
    *,
    delete_files: bool,
) -> tuple[dict[str, object], int | None]:
    try:
        config = MediaMetadataConfig.from_env()
        if not config.enabled:
            return (
                client.torrent_action(
                    "delete",
                    info_hashes,
                    delete_files=delete_files,
                ),
                0,
            )
        manager = media_metadata_manager(config)
    except (
        MediaMetadataError,
        MediaMetadataStoreError,
        OSError,
        sqlite3.Error,
    ):
        return (
            client.torrent_action(
                "delete",
                info_hashes,
                delete_files=delete_files,
            ),
            None,
        )
    return manager.delete_qb(
        client,
        info_hashes,
        delete_files=delete_files,
    )
