"""Subtitle manager lifecycle and its hooks into metadata publication."""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

from ...core.observability import emit_json_log
from ...library.filesystem import media_code_from_path
from ...subtitles.manager import (
    SubtitleConfig,
    SubtitleDisabledError,
    SubtitleManager,
    SubtitleUnavailableError,
)
from ...subtitles.store import SubtitleStore, SubtitleStoreError
from ...web_download.variant import web_download_variant_from_stem
from .. import state
from .history import require_operational_mode


def subtitle_manager(config: SubtitleConfig | None = None) -> SubtitleManager:
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            SubtitleUnavailableError("subtitles are unavailable in maintenance mode")
        )
        with state.SUBTITLES_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise SubtitleUnavailableError("server is shutting down")
            if state.SUBTITLES is not None:
                return state.SUBTITLES
            active_config = config or SubtitleConfig.from_env()
            if not active_config.enabled:
                raise SubtitleDisabledError("subtitles are disabled")
            try:
                state.SUBTITLES = SubtitleManager(
                    active_config,
                    on_written=_synchronize_library,
                )
            except (OSError, sqlite3.Error, SubtitleStoreError) as exc:
                raise SubtitleUnavailableError("subtitle storage is unavailable") from exc
            return state.SUBTITLES


def shutdown_subtitle_manager(*, timeout: float) -> bool:
    with state.SUBTITLES_LOCK:
        manager = state.SUBTITLES
    stopped = manager is None or manager.shutdown(timeout=timeout)
    if stopped:
        with state.SUBTITLES_LOCK:
            if state.SUBTITLES is manager:
                state.SUBTITLES = None
    else:
        emit_json_log(
            "subtitles",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    return bool(stopped)


def observe_subtitle_publication(relative_media_path: str) -> None:
    """Queue subtitles for media whose metadata was just published."""

    try:
        config = SubtitleConfig.from_env()
        if not config.enabled:
            return
        root = config.library_path.resolve(strict=True)
        media = root.joinpath(*PurePosixPath(relative_media_path).parts)
        code = media_code_from_path(media, root)
        if code is None:
            return
        subtitle_manager(config).observe_published(
            relative_media_path,
            code[0],
            web_download_variant_from_stem(media.name),
        )
    except Exception:
        emit_json_log(
            "subtitles",
            "publication_hook_failed",
            level="warning",
            error_code="internal",
            outcome="failed",
        )


def relocate_subtitle_references(old_relative_path: str, new_relative_path: str) -> None:
    """Follow a verified same-directory media rename in the subtitle store.

    Runs inside the metadata relocation: an exception here rolls the rename
    back, and the rollback calls this again with the paths swapped.
    """

    config = SubtitleConfig.from_env()
    if not config.database_path.exists():
        return
    SubtitleStore(config.database_path).relocate_media(
        old_relative_path,
        new_relative_path,
        replace_stale=True,
    )


def _synchronize_library(relative_media_path: str) -> None:
    from .media import synchronize_media_library

    synchronize_media_library("subtitle_publish", (relative_media_path,))
