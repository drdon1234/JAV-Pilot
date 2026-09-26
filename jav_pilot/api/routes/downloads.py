"""Torrent download, import, magnet probe and organizer endpoints."""

from __future__ import annotations

import json
from http import HTTPStatus

from ...config.app_config import AppConfig
from ...config.settings import SettingsError, load_settings, normalize_settings
from ...core.guards import QueryError
from ...downloads.history import (
    DownloadHistoryError,
    lookup_download_history,
    normalize_lookup_codes,
)
from ...downloads.replacements import DownloadReplacementError
from ...torrent.inputs import DownloadInputError, parse_download_inputs
from ...torrent.magnet import parse_magnet
from ...torrent.magnet_probe import (
    MagnetProbeBusyError,
    MagnetProbeConflictError,
    MagnetProbeError,
    MagnetProbeNotFoundError,
)
from ...torrent.magnet_selection import (
    MagnetSelectionBusyError,
    MagnetSelectionConflictError,
    MagnetSelectionError,
    MagnetSelectionNotFoundError,
)
from ...torrent.organizer import preview_organizer
from ...torrent.qbittorrent import (
    TORRENT_FILTERS,
    DownloaderError,
    QbittorrentClient,
    TorrentHistoryLimitError,
    filter_torrent_history,
    normalize_torrent_history_query,
    parse_add_download_payload,
    parse_torrent_action_payload,
    torrent_history_summary,
)
from .. import state
from ..request import query_params, single_param, strict_int_param
from ..services.download_history import (
    history_library_entries,
    history_torrent_tasks,
    history_web_jobs,
)
from ..services.replacements import (
    attach_download_recovery,
    download_replacement_no_source_count,
    submit_qb_replacement,
    with_qb_reselection,
)
from ..services.torrents import (
    delete_qb_with_metadata,
    enrich_import_batch_from_probe,
    import_download_request,
    invalidate_torrent_history_cache,
    submit_qb_download,
    torrent_history_snapshot,
)
from .common import CommonErrorResponses


class DownloadRoutes(CommonErrorResponses):
    def _handle_downloads(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            filter_name = single_param(params, "filter") or "all"
            if filter_name not in TORRENT_FILTERS:
                raise QueryError("download filter is invalid")
            catalog_query = normalize_torrent_history_query(single_param(params, "q"))
            limit = strict_int_param(params, "limit", 100)
            offset = strict_int_param(params, "offset", 0)
        except (QueryError, DownloaderError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if limit < 1 or limit > 199 or offset < 0 or offset > 1_000_000:
            self._send_json(
                {
                    "configured": False,
                    "ok": False,
                    "tasks": [],
                    "error": "download pagination is invalid",
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        config = AppConfig.from_env()
        if not config.qbittorrent.configured:
            self._send_json(
                {
                    "configured": False,
                    "ok": False,
                    "tasks": [],
                    "offset": offset,
                    "limit": limit,
                    "has_more": False,
                    "count": 0,
                    "error": "qBittorrent is not configured",
                }
            )
            return
        category = config.qbittorrent.category.strip()
        if not category:
            self._send_json(
                {
                    "configured": True,
                    "ok": False,
                    "tasks": [],
                    "offset": offset,
                    "limit": limit,
                    "has_more": False,
                    "count": 0,
                    "error": "qBittorrent category must be configured before listing downloads",
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        requested_categories = params.get("category", [])
        if requested_categories and any(
            value != category for value in requested_categories
        ):
            self._send_json(
                {
                    "configured": True,
                    "ok": False,
                    "tasks": [],
                    "error": "requested category must match the configured qBittorrent category",
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            snapshot = torrent_history_snapshot(config.qbittorrent)
            filtered_tasks = filter_torrent_history(
                snapshot,
                filter_name=filter_name,
                catalog_query=catalog_query,
            )
        except TorrentHistoryLimitError as exc:
            self._send_json(
                {
                    "configured": True,
                    "ok": False,
                    "tasks": [],
                    "count": 0,
                    "offset": offset,
                    "limit": limit,
                    "has_more": False,
                    "error_code": "history_limit_exceeded",
                    "snapshot_limit": exc.max_tasks,
                    "error": str(exc),
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except DownloaderError as exc:
            self._send_json(
                {"configured": True, "ok": False, "tasks": [], "error": str(exc)},
                HTTPStatus.BAD_GATEWAY,
            )
            return
        count = len(filtered_tasks)
        tasks = [
            with_qb_reselection(task, category=category)
            for task in filtered_tasks[offset : offset + limit]
        ]
        tasks = attach_download_recovery(tasks)
        no_source_failure_count = download_replacement_no_source_count()
        self._send_json(
            {
                "configured": True,
                "ok": True,
                "tasks": tasks,
                "count": count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(tasks) < count,
                "category": category,
                "query": catalog_query,
                "summary": torrent_history_summary(snapshot, category=category),
                "no_source_failure_count": no_source_failure_count,
            }
        )

    def _handle_download_action(self) -> None:
        try:
            raw = self._read_body(64 * 1024)
            action, hashes, delete_files = parse_torrent_action_payload(raw)
            config = AppConfig.from_env()
            client = QbittorrentClient(config.qbittorrent)
            if action == "delete":
                result, removed = delete_qb_with_metadata(
                    client,
                    hashes,
                    delete_files=delete_files,
                )
            else:
                result = client.torrent_action(
                    action,
                    hashes,
                    delete_files=delete_files,
                )
                removed = 0
        except (ValueError, DownloaderError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if action == "delete":
            if removed is None:
                result["metadata_warning"] = (
                    "The torrent was deleted, but its pending metadata task "
                    "could not be cleaned up"
                )
            else:
                result["metadata_jobs_removed"] = removed
        invalidate_torrent_history_cache()
        self._send_json(result)

    def _handle_magnet_probe_start(self) -> None:
        try:
            payload = self._read_json_body(256 * 1024)
            result = state.MAGNET_PROBES.start(payload.get("magnets"))
        except MagnetProbeBusyError as exc:
            self._send_json(
                {"ok": False, "error": str(exc)}, HTTPStatus.TOO_MANY_REQUESTS
            )
            return
        except MagnetProbeConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except (ValueError, MagnetProbeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result, HTTPStatus.ACCEPTED)

    def _handle_magnet_probe_get(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            probe_id = single_param(params, "probe_id").strip()
            if not probe_id:
                raise ValueError("probe_id is required")
            result = state.MAGNET_PROBES.get(probe_id)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MagnetProbeNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (ValueError, MagnetProbeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def _handle_magnet_selection_start(self) -> None:
        try:
            payload = self._read_json_body(256 * 1024)
            result = state.MAGNET_SELECTIONS.start(payload.get("magnets"))
        except MagnetSelectionBusyError as exc:
            self._send_json(
                {"ok": False, "error": str(exc)}, HTTPStatus.TOO_MANY_REQUESTS
            )
            return
        except MagnetSelectionConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except (ValueError, MagnetSelectionError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result, HTTPStatus.ACCEPTED)

    def _handle_magnet_selection_get(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            selection_id = single_param(params, "selection_id").strip()
            if not selection_id:
                raise ValueError("selection_id is required")
            result = state.MAGNET_SELECTIONS.get(selection_id)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MagnetSelectionNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (ValueError, MagnetSelectionError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def _handle_download(self) -> None:
        try:
            raw = self._read_body(512 * 1024)
            request = parse_add_download_payload(raw)
            info_hash = parse_magnet(request.magnet).info_hash
            if state.MAGNET_SELECTIONS.conflicts([info_hash]):
                raise MagnetSelectionConflictError(
                    "a smart magnet selection for this hash is still active"
                )
            with state.MAGNET_PROBES.reserve_download_hashes([info_hash]):
                result = (
                    submit_qb_replacement(request, info_hash)
                    if request.replacement_id is not None
                    else submit_qb_download(request)
                )
        except MagnetProbeConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except MagnetSelectionConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except DownloadReplacementError as exc:
            self._send_download_replacement_error(exc)
            return
        except (ValueError, DownloaderError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def _handle_download_history_lookup(self) -> None:
        try:
            payload = self._read_json_body(32 * 1024)
            if set(payload) != {"codes"}:
                raise DownloadHistoryError("download history lookup is invalid")
            codes = normalize_lookup_codes(payload.get("codes"))
        except (ValueError, DownloadHistoryError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        items, unavailable = lookup_download_history(
            codes,
            torrent_tasks=history_torrent_tasks(),
            web_jobs=history_web_jobs(),
            library_entries=history_library_entries(),
        )
        self._send_json({"ok": True, "items": items, "unavailable": unavailable})

    def _handle_download_import_preview(self) -> None:
        try:
            payload = self._read_json_body(256 * 1024)
            batch = parse_download_inputs(payload.get("input"))
        except (ValueError, DownloadInputError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, **batch.to_dict()})

    def _handle_download_import_inspect(self) -> None:
        try:
            payload = self._read_json_body(256 * 1024)
            batch = parse_download_inputs(payload.get("input"))
            if not batch.items:
                raise DownloadInputError("no valid BT inputs were found")
            probe = state.MAGNET_PROBES.start(
                [item.magnet.uri for item in batch.items],
                purpose="metadata",
            )
        except MagnetProbeBusyError as exc:
            self._send_json(
                {"ok": False, "error": str(exc)}, HTTPStatus.TOO_MANY_REQUESTS
            )
            return
        except MagnetProbeConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except (ValueError, DownloadInputError, MagnetProbeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {"ok": True, **batch.to_dict(), "probe": probe},
            HTTPStatus.ACCEPTED,
        )

    def _handle_download_import(self) -> None:
        try:
            payload = self._read_json_body(256 * 1024)
            confirmation = payload.get("confirm_unrecognized", False)
            if type(confirmation) is not bool:
                raise ValueError("confirm_unrecognized must be a boolean")
            batch = parse_download_inputs(payload.get("input"))
            if not batch.items:
                raise DownloadInputError("no valid BT inputs were found")
            probe_id_value = payload.get("probe_id")
            if probe_id_value is not None:
                if not isinstance(probe_id_value, str) or not probe_id_value.strip():
                    raise ValueError("probe_id is invalid")
                probe = state.MAGNET_PROBES.get(probe_id_value.strip())
                batch = enrich_import_batch_from_probe(batch, probe)
            if batch.requires_confirmation and not confirmation:
                self._send_json(
                    {
                        "ok": False,
                        "error": "one or more inputs were not recognized as JAV",
                        **batch.to_dict(),
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            results: list[dict[str, object]] = []
            added_count = 0
            failed_count = 0
            batch_hashes = [item.magnet.info_hash for item in batch.items]
            with state.MAGNET_PROBES.reserve_download_hashes(batch_hashes):
                client = QbittorrentClient(AppConfig.from_env().qbittorrent)
                settings = load_settings()
                for item in batch.items:
                    try:
                        result = submit_qb_download(
                            import_download_request(item),
                            client=client,
                            settings=settings,
                        )
                    except (ValueError, DownloaderError) as exc:
                        failed_count += 1
                        results.append(
                            {
                                **item.to_dict(),
                                "status": "failed",
                                "error": str(exc),
                            }
                        )
                        continue
                    added_count += 1
                    response_item = {
                        **item.to_dict(),
                        "status": "added",
                        "display_name": result.get("display_name")
                        or item.magnet.display_name,
                    }
                    metadata_warning = result.get("metadata_warning")
                    if isinstance(metadata_warning, str) and metadata_warning:
                        response_item["metadata_warning"] = metadata_warning
                    results.append(response_item)
        except MagnetProbeConflictError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except MagnetProbeNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except (
            ValueError,
            DownloadInputError,
            DownloaderError,
            MagnetProbeError,
        ) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json(
            {
                "ok": failed_count == 0,
                "added_count": added_count,
                "failed_count": failed_count,
                "invalid_count": len(batch.errors),
                "items": results,
            }
        )

    def _handle_organizer_preview(self) -> None:
        try:
            raw = self._read_body(256 * 1024)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            preview_settings = load_settings()
            if isinstance(payload.get("settings"), dict):
                preview_settings = normalize_settings(payload["settings"])
            sample = (
                payload.get("sample")
                if isinstance(payload.get("sample"), dict)
                else payload
            )
            preview = preview_organizer(
                sample,
                preview_settings,
                AppConfig.from_env().qbittorrent,
            )
        except (
            json.JSONDecodeError,
            ValueError,
            SettingsError,
            DownloaderError,
        ) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, **preview})
