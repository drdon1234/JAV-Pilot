"""Configuration and settings endpoints."""

from __future__ import annotations

import os
from http import HTTPStatus

from ... import __version__
from ...config.app_config import AppConfig
from ...config.runtime_config import (
    RuntimeConfigError,
    notification_public_config,
    runtime_config_transaction,
    update_qbittorrent_config,
)
from ...config.settings import (
    SettingsConflictError,
    SettingsError,
    load_settings,
    load_settings_snapshot,
    normalize_settings,
    prepare_settings_update,
    public_settings,
    save_settings_if_revision,
    validate_organizer_destinations,
)
from ...media_metadata.manager import MediaMetadataConfig, MediaMetadataError
from ...torrent.qbittorrent import QbittorrentClient
from .. import state
from ..base import BaseHandler
from ..services.readiness import invalidate_readiness_probes
from ..services.torrents import invalidate_torrent_history_cache
from ..services.web_downloads import web_download_public_status


class SettingsRoutes(BaseHandler):
    def _handle_config(self) -> None:
        config = AppConfig.from_env()
        settings = load_settings()
        self._send_json(
            {
                "app": "jav-pilot",
                "version": __version__,
                "config": config.public_dict(),
                "cache": state.SEARCH_CACHE.stats(),
                "search_continuations": state.SEARCH_CONTINUATIONS.stats(),
                "javdb_fetcher": os.environ.get("JAV_PILOT_JAVDB_FETCHER", "auto"),
                "web_downloads": web_download_public_status(settings),
                "notifications": notification_public_config(),
                "settings": public_settings(settings),
            }
        )

    def _handle_settings(self) -> None:
        self._send_json(load_settings_snapshot())

    def _handle_save_settings(self) -> None:
        try:
            payload = self._read_json_body(512 * 1024)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("settings"), dict
            ):
                raise SettingsError("settings body must contain a settings object")
            with runtime_config_transaction():
                settings = normalize_settings(prepare_settings_update(payload["settings"]))
                qbittorrent = AppConfig.from_env().qbittorrent
                validate_organizer_destinations(
                    settings,
                    category=qbittorrent.category,
                    save_path=qbittorrent.save_path,
                )
                snapshot = save_settings_if_revision(
                    settings, payload.get("expected_revision")
                )
        except SettingsConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except (ValueError, SettingsError, OSError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        state.SEARCH_CACHE.clear()
        state.SEARCH_CONTINUATIONS.clear()
        state.METADATA_SEARCH_CONTINUATIONS.clear()
        state.WORK_CACHE.clear()
        self._send_json({"ok": True, **snapshot})

    def _handle_validate_settings(self) -> None:
        try:
            payload = self._read_json_body(512 * 1024)
            if "settings" in payload:
                payload = payload["settings"]
            if not isinstance(payload, dict):
                raise SettingsError("settings body must be an object")
            settings = normalize_settings(prepare_settings_update(payload))
            qbittorrent = AppConfig.from_env().qbittorrent
            validate_organizer_destinations(
                settings,
                category=qbittorrent.category,
                save_path=qbittorrent.save_path,
            )
        except (ValueError, SettingsError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "settings": public_settings(settings)})

    def _handle_save_qb_config(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            password_action = str(payload.get("password_action") or "keep")
            fields = {
                key: payload.get(key)
                for key in (
                    "url",
                    "username",
                    "password",
                    "category",
                    "save_path",
                    "library_path",
                    "app_library_path",
                    "tags",
                )
                if key in payload
            }
            metadata_config = MediaMetadataConfig.from_env()
            update_qbittorrent_config(
                fields,
                password_action=password_action,
                settings=load_settings(),
                expected_app_library_root=metadata_config.library_path.as_posix(),
                require_accessible_mapping=metadata_config.enabled,
            )
            invalidate_torrent_history_cache()
            invalidate_readiness_probes()
        except (ValueError, RuntimeConfigError, MediaMetadataError, OSError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "config": AppConfig.from_env().public_dict()})

    def _handle_downloader_status(self) -> None:
        config = AppConfig.from_env()
        if not config.qbittorrent.configured:
            self._send_json(
                {
                    "configured": False,
                    "ok": False,
                    "error": "qBittorrent is not configured",
                }
            )
            return
        self._send_json(QbittorrentClient(config.qbittorrent).status())
