from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .. import __version__
from ..config.app_config import QbittorrentConfig
from ..core.catalog_code import normalize_catalog_code
from .magnet import MagnetError, parse_magnet
from ..core.models import MagnetInfo
from ..config.qb_paths import (
    QbPathError,
    normalize_qb_path,
    resolve_qb_download_destination,
    validate_qb_staging_path,
)


class DownloaderError(RuntimeError):
    pass


class DownloaderHttpError(DownloaderError):
    def __init__(self, status_code: int) -> None:
        self.status_code = int(status_code)
        super().__init__(f"qBittorrent request failed with HTTP {self.status_code}")


class TorrentHistoryLimitError(DownloaderError):
    def __init__(self, max_tasks: int) -> None:
        self.max_tasks = int(max_tasks)
        super().__init__(
            f"qBittorrent JAV history exceeds the bounded limit of {self.max_tasks} tasks"
        )


TORRENT_FILTERS = {
    "all",
    "downloading",
    "completed",
    "paused",
    "active",
    "inactive",
    "resumed",
    "stalled",
    "stalled_uploading",
    "stalled_downloading",
    "errored",
}
TORRENT_ACTIONS = {
    "pause": "api/v2/torrents/pause",
    "resume": "api/v2/torrents/resume",
    "recheck": "api/v2/torrents/recheck",
    "delete": "api/v2/torrents/delete",
}
QB5_TORRENT_ACTIONS = {
    "pause": "api/v2/torrents/stop",
    "resume": "api/v2/torrents/start",
}
INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
QB_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")
PROBE_MIN_QB_VERSION = (4, 5, 0)
PROBE_MAX_MAGNETS = 100
PROBE_HASH_BATCH_SIZE = 50
PROBE_TAG = "jav-pilot-probe"
SMART_SELECTION_TAG_RE = re.compile(r"^jav-pilot-smart-[0-9a-f]{32}$")
PROBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
PROBE_DOWNLOAD_LIMIT_BYTES_PER_SECOND = 1
PROBE_MAX_DOWNLOADED_BYTES = 1024 * 1024
PROBE_METADATA_TEXT_MAX_BYTES = 2048
SMART_SELECTION_FALLBACK_BATCH_SIZE = 4
SMART_SELECTION_MAX_BATCH_SIZE = PROBE_MAX_MAGNETS
TORRENT_HISTORY_PAGE_SIZE = 200
TORRENT_HISTORY_MAX_TASKS = 5_000
TORRENT_HISTORY_QUERY_MAX_LENGTH = 40


@dataclass(frozen=True)
class AddDownloadRequest:
    magnet: str
    name: str = ""
    category: str = ""
    save_path: str = ""
    tags: str = ""
    paused: bool = False
    auto_organize: bool = True
    result: dict[str, Any] = field(default_factory=dict)
    magnet_info: dict[str, Any] = field(default_factory=dict)
    replacement_id: str | None = None
    idempotency_key: str | None = None


def resolve_download_destination(
    request: AddDownloadRequest,
    config: QbittorrentConfig,
) -> tuple[str, str]:
    try:
        return resolve_qb_download_destination(
            configured_category=config.category,
            configured_save_path=config.save_path,
            requested_category=request.category,
            requested_save_path=request.save_path,
        )
    except QbPathError as exc:
        raise DownloaderError(str(exc)) from exc


class QbittorrentClient:
    def __init__(self, config: QbittorrentConfig) -> None:
        self.config = config
        self._cookies = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookies))
        self._logged_in = False
        self._probe_capabilities: dict[str, object] | None = None

    def status(self, *, timeout_seconds: float | None = None) -> dict[str, object]:
        self._require_config()
        try:
            if timeout_seconds is None:
                self._login_if_needed()
                version = self._request_text("api/v2/app/version")
            else:
                request_count = (
                    2 if (self.config.username or self.config.password) else 1
                )
                request_timeout = max(0.5, float(timeout_seconds) / request_count)
                self._login_if_needed(timeout_seconds=request_timeout)
                version = self._request_text(
                    "api/v2/app/version",
                    max_bytes=256,
                    timeout_seconds=request_timeout,
                )
        except DownloaderError as exc:
            return {"configured": True, "ok": False, "error": str(exc)}
        return {"configured": True, "ok": True, "version": version}

    def add_magnet(
        self,
        request: AddDownloadRequest,
        *,
        before_submit: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        self._require_config()
        try:
            parsed = parse_magnet(request.magnet)
        except MagnetError as exc:
            raise DownloaderError(str(exc)) from exc
        _require_supported_qb_magnet(parsed)

        category, save_path = resolve_download_destination(request, self.config)

        self._login_if_needed()
        tags = request.tags or self.config.tags
        form: dict[str, str] = {
            "urls": request.magnet,
            "paused": "true" if request.paused else "false",
        }
        if category:
            form["category"] = category
        if save_path:
            form["savepath"] = save_path
        if tags:
            form["tags"] = tags

        if before_submit is not None:
            before_submit(parsed.info_hash)
        outcome = self._request_add_torrents(form, (parsed.info_hash,))
        if outcome == "failed":
            raise DownloaderError("qBittorrent did not accept the magnet")

        return {
            "ok": True,
            "info_hash": parsed.info_hash,
            "display_name": request.name or parsed.display_name,
            "category": category,
            "save_path": save_path,
            "tags": tags,
        }

    def list_torrents(
        self,
        *,
        filter_name: str = "all",
        category: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        self._require_config()
        self._login_if_needed()
        safe_filter = filter_name if filter_name in TORRENT_FILTERS else "all"
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, min(int(offset), 1_000_000))
        query: dict[str, str] = {
            "filter": safe_filter,
            "sort": "added_on",
            "reverse": "true",
            "limit": str(safe_limit),
            "offset": str(safe_offset),
        }
        if category:
            query["category"] = str(category).strip()[:120]
        raw = self._request_text(
            f"api/v2/torrents/info?{urlencode(query)}",
            max_bytes=4 * 1024 * 1024,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DownloaderError("qBittorrent returned an invalid task list") from exc
        if not isinstance(payload, list):
            raise DownloaderError("qBittorrent returned an invalid task list")
        return [
            _torrent_payload(item)
            for item in payload[:safe_limit]
            if isinstance(item, dict)
        ]

    def list_torrent_history(
        self,
        *,
        category: str,
        max_tasks: int = TORRENT_HISTORY_MAX_TASKS,
        page_size: int = TORRENT_HISTORY_PAGE_SIZE,
    ) -> list[dict[str, object]]:
        """Read one bounded, category-scoped history snapshot from qBittorrent."""

        clean_category = str(category or "").strip()
        if not clean_category:
            raise DownloaderError("qBittorrent category is required for history")
        safe_max = int(max_tasks)
        safe_page_size = int(page_size)
        if safe_max < 1 or safe_max > 50_000:
            raise DownloaderError("qBittorrent history limit is invalid")
        if (
            safe_page_size < 1
            or safe_page_size > TORRENT_HISTORY_PAGE_SIZE
            or (safe_max > 1 and safe_page_size < 2)
        ):
            raise DownloaderError("qBittorrent history page size is invalid")

        tasks: list[dict[str, object]] = []
        seen_hashes: set[str] = set()
        while len(tasks) < safe_max:
            overlap = 1 if tasks else 0
            request_limit = min(
                safe_page_size,
                safe_max - len(tasks) + overlap,
            )
            request_offset = max(0, len(tasks) - overlap)
            page = self.list_torrents(
                filter_name="all",
                category=clean_category,
                limit=request_limit,
                offset=request_offset,
            )
            if overlap:
                expected_hash = str(tasks[-1].get("hash") or "").strip().lower()
                overlap_hash = (
                    str(page[0].get("hash") or "").strip().lower() if page else ""
                )
                if overlap_hash != expected_hash:
                    raise DownloaderError(
                        "qBittorrent task history changed while it was being read; retry"
                    )
            new_tasks = page[overlap:]
            for task in new_tasks:
                info_hash = str(task.get("hash") or "").strip().lower()
                if not INFO_HASH_RE.fullmatch(info_hash):
                    raise DownloaderError(
                        "qBittorrent returned an invalid task history"
                    )
                if info_hash in seen_hashes:
                    raise DownloaderError(
                        "qBittorrent task history changed while it was being read; retry"
                    )
                seen_hashes.add(info_hash)
                tasks.append(task)
            if len(page) < request_limit:
                return tasks

        overflow = self.list_torrents(
            filter_name="all",
            category=clean_category,
            limit=2,
            offset=max(0, safe_max - 1),
        )
        expected_hash = str(tasks[-1].get("hash") or "").strip().lower()
        overlap_hash = (
            str(overflow[0].get("hash") or "").strip().lower() if overflow else ""
        )
        if overlap_hash != expected_hash:
            raise DownloaderError(
                "qBittorrent task history changed while it was being read; retry"
            )
        if len(overflow) > 1:
            raise TorrentHistoryLimitError(safe_max)
        return tasks

    def torrent_snapshot(self, info_hash: str) -> dict[str, object] | None:
        clean_hash = _normalize_hashes((info_hash,))[0]
        self._require_config()
        self._login_if_needed()
        query = urlencode({"hashes": clean_hash, "limit": "2"})
        raw = self._request_text(
            f"api/v2/torrents/info?{query}",
            max_bytes=512 * 1024,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DownloaderError(
                "qBittorrent returned invalid torrent metadata"
            ) from exc
        if not isinstance(payload, list) or len(payload) > 2:
            raise DownloaderError("qBittorrent returned invalid torrent metadata")
        matches = [
            item
            for item in payload
            if isinstance(item, dict)
            and str(item.get("hash") or "").strip().lower() == clean_hash
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise DownloaderError("qBittorrent returned ambiguous torrent metadata")
        item = matches[0]
        return {
            **_torrent_payload(item),
            "content_path": str(item.get("content_path") or ""),
        }

    def torrent_files(
        self,
        info_hash: str,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, object], ...]:
        clean_hash = _normalize_hashes((info_hash,))[0]
        self._require_config()
        self._login_if_needed()
        query = urlencode({"hash": clean_hash})
        path = f"api/v2/torrents/files?{query}"
        if timeout_seconds is None:
            raw = self._request_text(path, max_bytes=8 * 1024 * 1024)
        else:
            raw = self._request_text(
                path,
                max_bytes=8 * 1024 * 1024,
                timeout_seconds=timeout_seconds,
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DownloaderError(
                "qBittorrent returned an invalid torrent file list"
            ) from exc
        if not isinstance(payload, list) or len(payload) > 20_000:
            raise DownloaderError("qBittorrent returned an invalid torrent file list")
        files: list[dict[str, object]] = []
        seen_indexes: set[int] = set()
        seen_names: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                raise DownloaderError(
                    "qBittorrent returned an invalid torrent file list"
                )
            name = _relative_torrent_file_name(item.get("name"))
            index = _torrent_file_index(item.get("index"))
            if index in seen_indexes:
                raise DownloaderError(
                    "qBittorrent returned an invalid torrent file list"
                )
            if name in seen_names:
                raise DownloaderError(
                    "qBittorrent returned an invalid torrent file list"
                )
            seen_indexes.add(index)
            seen_names.add(name)
            files.append(
                {
                    "index": index,
                    "name": name,
                    "size": _torrent_file_size(item.get("size")),
                    "progress": _torrent_file_progress(item.get("progress")),
                    "priority": _torrent_file_priority(item.get("priority")),
                }
            )
        return tuple(files)

    def require_probe_support(self) -> dict[str, object]:
        if self._probe_capabilities is not None:
            return dict(self._probe_capabilities)
        self._require_config()
        self._login_if_needed()
        raw_version = self._request_text(
            "api/v2/app/version",
            max_bytes=256,
            timeout_seconds=5.0,
        ).strip()
        version = _parse_qb_version(
            raw_version,
            "cannot verify qBittorrent 4.5+ support required for safe magnet probing",
        )
        if version < PROBE_MIN_QB_VERSION:
            raise DownloaderError(
                "qBittorrent 4.5 or newer is required for safe magnet probing"
            )
        self._probe_capabilities = {
            "version": raw_version,
            "version_tuple": version,
            "include_trackers": version >= (5, 1, 0),
        }
        return dict(self._probe_capabilities)

    def ensure_probe_namespace(
        self,
        *,
        category: str,
        tag: str,
        save_path: str,
        timeout_seconds: float | None = None,
    ) -> None:
        clean_category, clean_tag, clean_path = _validated_probe_destination(
            self.config,
            category,
            tag,
            save_path,
        )
        self.require_probe_support()
        self._login_if_needed()
        request_timeout = _probe_request_timeout(timeout_seconds, default=5.0)

        raw_categories = self._request_text(
            "api/v2/torrents/categories",
            max_bytes=1024 * 1024,
            timeout_seconds=request_timeout,
        )
        try:
            categories = json.loads(raw_categories)
        except json.JSONDecodeError as exc:
            raise DownloaderError("qBittorrent returned invalid categories") from exc
        if not isinstance(categories, dict):
            raise DownloaderError("qBittorrent returned invalid categories")
        if clean_category not in categories:
            response = self._request_text(
                "api/v2/torrents/createCategory",
                data={
                    "category": clean_category,
                    "savePath": PurePosixPath(clean_path).parent.as_posix(),
                },
                timeout_seconds=request_timeout,
            )
            _require_ok_response(
                response, "qBittorrent could not create the probe category"
            )

        raw_tags = self._request_text(
            "api/v2/torrents/tags",
            max_bytes=1024 * 1024,
            timeout_seconds=request_timeout,
        )
        try:
            tags = json.loads(raw_tags)
        except json.JSONDecodeError as exc:
            raise DownloaderError("qBittorrent returned invalid tags") from exc
        if not isinstance(tags, list) or any(
            not isinstance(value, str) for value in tags
        ):
            raise DownloaderError("qBittorrent returned invalid tags")
        if clean_tag not in tags:
            response = self._request_text(
                "api/v2/torrents/createTags",
                data={"tags": clean_tag},
                timeout_seconds=request_timeout,
            )
            _require_ok_response(response, "qBittorrent could not create the probe tag")

    def probe_torrent_snapshots(
        self,
        hashes: list[str] | tuple[str, ...],
        *,
        include_trackers: bool = False,
        timeout_seconds: float = 5.0,
    ) -> dict[str, dict[str, object]]:
        clean_hashes = _normalize_probe_hashes(hashes, allow_empty=True)
        if not clean_hashes:
            return {}
        self._require_config()
        self._login_if_needed()
        snapshots: dict[str, dict[str, object]] = {}
        for start in range(0, len(clean_hashes), PROBE_HASH_BATCH_SIZE):
            batch = clean_hashes[start : start + PROBE_HASH_BATCH_SIZE]
            query: dict[str, str] = {"hashes": "|".join(batch)}
            if include_trackers:
                query["includeTrackers"] = "true"
            raw = self._request_text(
                f"api/v2/torrents/info?{urlencode(query)}",
                max_bytes=4 * 1024 * 1024,
                timeout_seconds=timeout_seconds,
            )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DownloaderError(
                    "qBittorrent returned an invalid probe snapshot"
                ) from exc
            if not isinstance(payload, list) or any(
                not isinstance(item, dict) for item in payload
            ):
                raise DownloaderError("qBittorrent returned an invalid probe snapshot")
            requested = set(batch)
            for item in payload:
                info_hash = str(item.get("hash") or "").strip().lower()
                if info_hash in requested:
                    snapshots[info_hash] = _probe_torrent_payload(item)
        return snapshots

    def add_probe_magnets(
        self,
        magnets: list[str] | tuple[str, ...],
        *,
        category: str,
        tag: str,
        save_path: str,
        timeout_seconds: float | None = None,
    ) -> str:
        clean_category, clean_tag, clean_path = _validated_probe_destination(
            self.config,
            category,
            tag,
            save_path,
        )
        uris = _normalize_probe_magnets(magnets)
        self.require_probe_support()
        self._login_if_needed()
        expected_hashes = tuple(parse_magnet(uri).info_hash for uri in uris)
        return self._request_add_torrents(
            {
                "urls": "\n".join(uris),
                "category": clean_category,
                "tags": clean_tag,
                "savepath": clean_path,
                "paused": "false",
                "stopped": "false",
                "forced": "true",
                "addToTopOfQueue": "true",
                "stopCondition": "MetadataReceived",
                "dlLimit": str(PROBE_DOWNLOAD_LIMIT_BYTES_PER_SECOND),
                "autoTMM": "false",
            },
            expected_hashes,
            timeout_seconds=_probe_request_timeout(timeout_seconds, default=10.0),
        )

    def ensure_smart_selection_tag(
        self,
        tag: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> str:
        clean_tag = _smart_selection_tag(tag)
        self._require_config()
        self._login_if_needed(timeout_seconds=timeout_seconds)
        raw_tags = self._request_text(
            "api/v2/torrents/tags",
            max_bytes=1024 * 1024,
            timeout_seconds=timeout_seconds,
        )
        try:
            tags = json.loads(raw_tags)
        except json.JSONDecodeError as exc:
            raise DownloaderError("qBittorrent returned invalid tags") from exc
        if not isinstance(tags, list) or any(
            not isinstance(value, str) for value in tags
        ):
            raise DownloaderError("qBittorrent returned invalid tags")
        if clean_tag not in tags:
            response = self._request_text(
                "api/v2/torrents/createTags",
                data={"tags": clean_tag},
                timeout_seconds=timeout_seconds,
            )
            _require_ok_response(
                response,
                "qBittorrent could not create the smart selection tag",
            )
        return clean_tag

    def smart_selection_batch_limit(
        self,
        candidate_count: int,
        *,
        timeout_seconds: float = 5.0,
    ) -> int:
        """Return a conservative batch size from qBittorrent's queue limits.

        qBittorrent may still have fewer free slots because unrelated torrents
        are active. The selection service handles that residual uncertainty by
        observing the batch and recursively splitting candidates that were not
        actually scheduled.
        """

        try:
            requested = int(candidate_count)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DownloaderError("smart selection candidate count is invalid") from exc
        if requested < 1 or requested > PROBE_MAX_MAGNETS:
            raise DownloaderError("smart selection candidate count is invalid")
        self._require_config()
        try:
            self._login_if_needed(timeout_seconds=timeout_seconds)
            raw = self._request_text(
                "api/v2/app/preferences",
                max_bytes=256 * 1024,
                timeout_seconds=timeout_seconds,
            )
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DownloaderError("qBittorrent returned invalid preferences") from exc
        except DownloaderError:
            raise
        except (TypeError, ValueError) as exc:
            raise DownloaderError("qBittorrent preferences are unavailable") from exc
        if not isinstance(payload, dict):
            raise DownloaderError("qBittorrent returned invalid preferences")

        limits: list[int] = []
        for key in ("max_active_downloads", "max_active_torrents"):
            value = payload.get(key)
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if parsed >= 0:
                limits.append(parsed)
        if limits:
            configured = min(limits)
        else:
            configured = SMART_SELECTION_FALLBACK_BATCH_SIZE
        return max(
            1,
            min(requested, configured, SMART_SELECTION_MAX_BATCH_SIZE),
        )

    def add_smart_selection_magnets(
        self,
        magnets: list[str] | tuple[str, ...],
        *,
        tag: str,
        timeout_seconds: float | None = None,
    ) -> str:
        clean_tag = _smart_selection_tag(tag)
        uris = _normalize_probe_magnets(magnets)
        category = str(self.config.category or "").strip()
        if not category:
            raise DownloaderError(
                "normal qBittorrent category is required for smart selection"
            )
        try:
            save_path = validate_qb_staging_path(self.config.save_path).as_posix()
        except QbPathError as exc:
            raise DownloaderError(str(exc)) from exc
        self._require_config()
        self._login_if_needed(timeout_seconds=timeout_seconds)
        expected_hashes = tuple(parse_magnet(uri).info_hash for uri in uris)
        configured_tags = [
            value.strip()
            for value in str(self.config.tags or "").split(",")
            if value.strip() and value.strip() != clean_tag
        ]
        configured_tags.append(clean_tag)
        outcome = self._request_add_torrents(
            {
                "urls": "\n".join(uris),
                "category": category,
                "tags": ",".join(dict.fromkeys(configured_tags)),
                "savepath": save_path,
                "paused": "false",
                "stopped": "false",
                # Keep these candidates auto-managed so qBittorrent's active
                # download limit is observable and the service can split a
                # queued batch instead of bypassing the scheduler.
                "forced": "false",
                "addToTopOfQueue": "true",
                "autoTMM": "false",
            },
            expected_hashes,
            timeout_seconds=_probe_request_timeout(timeout_seconds, default=15.0),
        )
        if outcome == "failed":
            raise DownloaderError("qBittorrent did not accept the smart selection magnet")
        return outcome

    def remove_torrent_tags(
        self,
        hashes: list[str] | tuple[str, ...],
        *,
        tag: str,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        clean_hashes = _normalize_probe_hashes(hashes, allow_empty=True)
        clean_tag = _smart_selection_tag(tag)
        if not clean_hashes:
            return {"ok": True, "hashes": [], "tag": clean_tag}
        self._require_config()
        self._login_if_needed(timeout_seconds=timeout_seconds)
        response = self._request_text(
            "api/v2/torrents/removeTags",
            data={"hashes": "|".join(clean_hashes), "tags": clean_tag},
            timeout_seconds=timeout_seconds,
        )
        _require_ok_response(response, "qBittorrent could not remove the selection tag")
        return {"ok": True, "hashes": list(clean_hashes), "tag": clean_tag}

    def delete_owned_smart_selection_torrents(
        self,
        hashes: list[str] | tuple[str, ...],
        *,
        tag: str,
        added_after: int,
        added_before: int,
        timeout_seconds: float = 5.0,
    ) -> dict[str, list[str]]:
        clean_hashes = _normalize_probe_hashes(hashes, allow_empty=True)
        clean_tag = _smart_selection_tag(tag)
        category = str(self.config.category or "").strip()
        if not category:
            raise DownloaderError(
                "normal qBittorrent category is required for smart selection cleanup"
            )
        try:
            save_path = validate_qb_staging_path(self.config.save_path).as_posix()
        except QbPathError as exc:
            raise DownloaderError(str(exc)) from exc
        earliest = int(added_after)
        latest = int(added_before)
        if earliest <= 0 or latest < earliest or latest - earliest > 3_600:
            raise DownloaderError("invalid smart selection ownership time window")

        if not clean_hashes:
            return {
                "requested": [],
                "deleted": [],
                "skipped": [],
                "missing": [],
                "remaining": [],
            }

        before = self.probe_torrent_snapshots(
            clean_hashes,
            timeout_seconds=timeout_seconds,
        )
        deletable: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        for info_hash in clean_hashes:
            snapshot = before.get(info_hash)
            if snapshot is None:
                missing.append(info_hash)
            elif _snapshot_matches_smart_selection(
                snapshot,
                category=category,
                tag=clean_tag,
                save_path=save_path,
                added_after=earliest,
                added_before=latest,
            ):
                deletable.append(info_hash)
            else:
                skipped.append(info_hash)

        if deletable:
            self._login_if_needed(timeout_seconds=timeout_seconds)
            response = self._request_text(
                "api/v2/torrents/delete",
                data={"hashes": "|".join(deletable), "deleteFiles": "true"},
                timeout_seconds=timeout_seconds,
            )
            _require_ok_response(
                response,
                "qBittorrent could not remove smart selection torrents",
            )
        remaining_snapshot = self.probe_torrent_snapshots(
            deletable,
            timeout_seconds=timeout_seconds,
        )
        remaining = [
            info_hash for info_hash in deletable if info_hash in remaining_snapshot
        ]
        deleted = [
            info_hash for info_hash in deletable if info_hash not in remaining_snapshot
        ]
        return {
            "requested": list(clean_hashes),
            "deleted": deleted,
            "skipped": skipped,
            "missing": missing,
            "remaining": remaining,
        }

    def _request_add_torrents(
        self,
        form: dict[str, str],
        expected_hashes: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        try:
            if timeout_seconds is None:
                response = self._request_text("api/v2/torrents/add", data=form)
            else:
                response = self._request_text(
                    "api/v2/torrents/add",
                    data=form,
                    timeout_seconds=timeout_seconds,
                )
        except DownloaderHttpError as exc:
            if exc.status_code == 409:
                return "failed"
            raise DownloaderError("qBittorrent add request failed") from exc
        except DownloaderError as exc:
            raise DownloaderError("qBittorrent add request failed") from exc
        return _parse_add_response(response, expected_hashes)

    def start_owned_probe_torrents(
        self,
        hashes: list[str] | tuple[str, ...],
        *,
        expected_category: str,
        expected_tag: str,
        expected_save_path: str,
        added_after: int,
        added_before: int,
    ) -> dict[str, list[str]]:
        clean_hashes = _normalize_probe_hashes(hashes)
        clean_category, clean_tag, clean_path = _validated_probe_destination(
            self.config,
            expected_category,
            expected_tag,
            expected_save_path,
        )
        earliest = int(added_after)
        latest = int(added_before)
        if earliest <= 0 or latest < earliest or latest - earliest > 600:
            raise DownloaderError("invalid probe ownership time window")

        before = self.probe_torrent_snapshots(clean_hashes)
        startable: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        for info_hash in clean_hashes:
            snapshot = before.get(info_hash)
            if snapshot is None:
                missing.append(info_hash)
            elif _snapshot_matches_probe(
                snapshot,
                category=clean_category,
                tag=clean_tag,
                save_path=clean_path,
                added_after=earliest,
                added_before=latest,
            ) and snapshot.get("state") in {"stoppedDL", "pausedDL"}:
                startable.append(info_hash)
            else:
                skipped.append(info_hash)

        if startable:
            capabilities = self.require_probe_support()
            version = capabilities.get("version_tuple")
            if not isinstance(version, tuple) or any(
                not isinstance(part, int) for part in version
            ):
                raise DownloaderError(
                    "cannot verify qBittorrent version for safe probe resume"
                )
            endpoint = "start" if version >= (5, 0, 0) else "resume"
            response = self._request_text(
                "api/v2/torrents/setDownloadLimit",
                data={
                    "hashes": "|".join(startable),
                    "limit": str(PROBE_DOWNLOAD_LIMIT_BYTES_PER_SECOND),
                },
                timeout_seconds=5.0,
            )
            _require_ok_response(response, "qBittorrent could not limit probe torrents")
            response = self._request_text(
                f"api/v2/torrents/{endpoint}",
                data={"hashes": "|".join(startable)},
                timeout_seconds=5.0,
            )
            _require_ok_response(
                response, "qBittorrent could not resume probe torrents"
            )
        return {
            "requested": list(clean_hashes),
            "started": startable,
            "skipped": skipped,
            "missing": missing,
        }

    def stop_owned_probe_torrents(
        self,
        hashes: list[str] | tuple[str, ...],
        *,
        expected_category: str,
        expected_tag: str,
        expected_save_path: str,
        added_after: int,
        added_before: int,
        timeout_seconds: float = 5.0,
    ) -> dict[str, list[str]]:
        clean_hashes = _normalize_probe_hashes(hashes)
        clean_category, clean_tag, clean_path = _validated_probe_destination(
            self.config,
            expected_category,
            expected_tag,
            expected_save_path,
        )
        earliest = int(added_after)
        latest = int(added_before)
        if earliest <= 0 or latest < earliest or latest - earliest > 600:
            raise DownloaderError("invalid probe ownership time window")

        before = self.probe_torrent_snapshots(
            clean_hashes,
            timeout_seconds=timeout_seconds,
        )
        owned: list[str] = []
        stoppable: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        for info_hash in clean_hashes:
            snapshot = before.get(info_hash)
            if snapshot is None:
                missing.append(info_hash)
            elif _snapshot_matches_probe(
                snapshot,
                category=clean_category,
                tag=clean_tag,
                save_path=clean_path,
                added_after=earliest,
                added_before=latest,
            ):
                owned.append(info_hash)
                if snapshot.get("state") not in {"stoppedDL", "pausedDL"}:
                    stoppable.append(info_hash)
            else:
                skipped.append(info_hash)

        if stoppable:
            capabilities = self.require_probe_support()
            version = capabilities.get("version_tuple")
            if not isinstance(version, tuple) or any(
                not isinstance(part, int) for part in version
            ):
                raise DownloaderError(
                    "cannot verify qBittorrent version for safe probe stop"
                )
            endpoint = "stop" if version >= (5, 0, 0) else "pause"
            response = self._request_text(
                f"api/v2/torrents/{endpoint}",
                data={"hashes": "|".join(stoppable)},
                timeout_seconds=timeout_seconds,
            )
            _require_ok_response(response, "qBittorrent could not stop probe torrents")

        after = self.probe_torrent_snapshots(
            owned,
            timeout_seconds=timeout_seconds,
        )
        verified: list[str] = []
        remaining: list[str] = []
        for info_hash in owned:
            snapshot = after.get(info_hash)
            if (
                snapshot is not None
                and _snapshot_matches_probe(
                    snapshot,
                    category=clean_category,
                    tag=clean_tag,
                    save_path=clean_path,
                    added_after=earliest,
                    added_before=latest,
                )
                and snapshot.get("state") in {"stoppedDL", "pausedDL"}
                and snapshot.get("downloaded") == 0
            ):
                verified.append(info_hash)
            else:
                remaining.append(info_hash)
        return {
            "requested": list(clean_hashes),
            "stopped": stoppable,
            "verified": verified,
            "skipped": skipped,
            "missing": missing,
            "remaining": remaining,
        }

    def delete_owned_probe_torrents(
        self,
        hashes: list[str] | tuple[str, ...],
        *,
        expected_category: str,
        expected_tag: str,
        expected_save_path: str,
        added_after: int,
        added_before: int,
    ) -> dict[str, list[str]]:
        clean_hashes = _normalize_probe_hashes(hashes)
        clean_category, clean_tag, clean_path = _validated_probe_destination(
            self.config,
            expected_category,
            expected_tag,
            expected_save_path,
        )
        earliest = int(added_after)
        latest = int(added_before)
        if earliest <= 0 or latest < earliest or latest - earliest > 600:
            raise DownloaderError("invalid probe ownership time window")

        before = self.probe_torrent_snapshots(clean_hashes)
        deletable: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        # The journal candidate set is necessary but not sufficient: qB state must
        # still carry every probe marker immediately before deletion.
        for info_hash in clean_hashes:
            snapshot = before.get(info_hash)
            if snapshot is None:
                missing.append(info_hash)
            elif _snapshot_matches_probe(
                snapshot,
                category=clean_category,
                tag=clean_tag,
                save_path=clean_path,
                added_after=earliest,
                added_before=latest,
            ):
                deletable.append(info_hash)
            else:
                skipped.append(info_hash)

        if deletable:
            self._login_if_needed()
            response = self._request_text(
                "api/v2/torrents/delete",
                data={"hashes": "|".join(deletable), "deleteFiles": "true"},
                timeout_seconds=5.0,
            )
            _require_ok_response(
                response, "qBittorrent could not remove probe torrents"
            )
        remaining_snapshot = self.probe_torrent_snapshots(deletable)
        remaining = [
            info_hash for info_hash in deletable if info_hash in remaining_snapshot
        ]
        deleted = [
            info_hash for info_hash in deletable if info_hash not in remaining_snapshot
        ]
        return {
            "requested": list(clean_hashes),
            "deleted": deleted,
            "skipped": skipped,
            "missing": missing,
            "remaining": remaining,
        }

    def torrent_action(
        self,
        action: str,
        hashes: list[str] | tuple[str, ...],
        *,
        delete_files: bool = False,
    ) -> dict[str, object]:
        self._require_config()
        clean_action = str(action).strip().lower()
        path = TORRENT_ACTIONS.get(clean_action)
        if not path:
            raise DownloaderError("unsupported torrent action")
        clean_hashes = _normalize_hashes(hashes)
        configured_category = self.config.category.strip()
        if not configured_category:
            raise DownloaderError(
                "qBittorrent category must be configured before managing downloads"
            )
        self._login_if_needed()
        self._require_torrents_in_category(clean_hashes, configured_category)
        if clean_action in QB5_TORRENT_ACTIONS:
            raw_version = self._request_text(
                "api/v2/app/version",
                max_bytes=256,
                timeout_seconds=5.0,
            ).strip()
            version = _parse_qb_version(
                raw_version,
                "cannot verify qBittorrent version for task action",
            )
            if version >= (5, 0, 0):
                path = QB5_TORRENT_ACTIONS[clean_action]
        form = {"hashes": "|".join(clean_hashes)}
        if clean_action == "delete":
            form["deleteFiles"] = "true" if delete_files else "false"
        response = self._request_text(path, data=form)
        if response.strip().lower() not in {"", "ok."}:
            raise DownloaderError(response.strip())
        return {
            "ok": True,
            "action": clean_action,
            "hashes": list(clean_hashes),
            "delete_files": bool(delete_files) if clean_action == "delete" else False,
        }

    def set_location(
        self,
        hashes: list[str] | tuple[str, ...],
        location: str,
    ) -> dict[str, object]:
        self._require_config()
        clean_hashes = _normalize_hashes(hashes)
        try:
            clean_location = normalize_qb_path(location)
        except QbPathError as exc:
            raise DownloaderError(str(exc)) from exc
        self._login_if_needed()
        response = self._request_text(
            "api/v2/torrents/setLocation",
            data={"hashes": "|".join(clean_hashes), "location": clean_location},
        )
        if response.strip().lower() not in {"", "ok."}:
            raise DownloaderError(response.strip())
        return {
            "ok": True,
            "hashes": list(clean_hashes),
            "location": clean_location,
        }

    def rename_torrent_file(
        self,
        info_hash: str,
        old_path: str,
        new_path: str,
    ) -> dict[str, object]:
        self._require_config()
        clean_hash = _normalize_hashes((info_hash,))[0]
        clean_old = _relative_torrent_file_name(old_path)
        clean_new = _relative_torrent_file_name(new_path)
        if clean_old == clean_new:
            raise DownloaderError("torrent file paths must be different")
        category = str(self.config.category or "").strip()
        if not category:
            raise DownloaderError(
                "qBittorrent category is required for torrent file changes"
            )
        self._login_if_needed()
        files = self.torrent_files(clean_hash)
        if sum(1 for item in files if item["name"] == clean_old) != 1:
            raise DownloaderError("torrent file rename source was not found")
        if any(item["name"] == clean_new for item in files):
            raise DownloaderError("torrent file rename target is already in use")
        self._require_torrents_in_category((clean_hash,), category)
        response = self._request_text(
            "api/v2/torrents/renameFile",
            data={
                "hash": clean_hash,
                "oldPath": clean_old,
                "newPath": clean_new,
            },
        )
        _require_ok_response(response, "qBittorrent could not rename the torrent file")
        return {
            "ok": True,
            "hash": clean_hash,
            "old_path": clean_old,
            "new_path": clean_new,
        }

    def rename_torrent_folder(
        self,
        info_hash: str,
        old_path: str,
        new_path: str,
    ) -> dict[str, object]:
        self._require_config()
        clean_hash = _normalize_hashes((info_hash,))[0]
        clean_old = _relative_torrent_file_name(old_path)
        clean_new = _relative_torrent_file_name(new_path)
        old_folder = PurePosixPath(clean_old)
        new_folder = PurePosixPath(clean_new)
        if old_folder == new_folder:
            raise DownloaderError("torrent folder paths must be different")
        if old_folder.parent != new_folder.parent:
            raise DownloaderError("torrent folders must remain in the same parent")
        category = str(self.config.category or "").strip()
        if not category:
            raise DownloaderError(
                "qBittorrent category is required for torrent folder changes"
            )
        self._login_if_needed()
        files = self.torrent_files(clean_hash)
        tracked_paths = tuple(PurePosixPath(str(item["name"])) for item in files)
        if not any(old_folder in path.parents for path in tracked_paths):
            raise DownloaderError("torrent folder rename source was not found")
        if any(
            new_folder == path or new_folder in path.parents for path in tracked_paths
        ):
            raise DownloaderError("torrent folder rename target is already in use")
        self._require_torrents_in_category((clean_hash,), category)
        response = self._request_text(
            "api/v2/torrents/renameFolder",
            data={
                "hash": clean_hash,
                "oldPath": clean_old,
                "newPath": clean_new,
            },
        )
        _require_ok_response(
            response, "qBittorrent could not rename the torrent folder"
        )
        return {
            "ok": True,
            "hash": clean_hash,
            "old_path": clean_old,
            "new_path": clean_new,
        }

    def set_torrent_file_priority(
        self,
        info_hash: str,
        indexes: list[int] | tuple[int, ...],
        priority: int,
    ) -> dict[str, object]:
        self._require_config()
        clean_hash = _normalize_hashes((info_hash,))[0]
        clean_indexes = _normalize_file_indexes(indexes)
        if isinstance(priority, bool) or priority not in {0, 1, 6, 7}:
            raise DownloaderError("invalid torrent file priority")
        category = str(self.config.category or "").strip()
        if not category:
            raise DownloaderError(
                "qBittorrent category is required for torrent file changes"
            )
        self._login_if_needed()
        available_indexes = {
            int(item["index"]) for item in self.torrent_files(clean_hash)
        }
        if not set(clean_indexes).issubset(available_indexes):
            raise DownloaderError("one or more torrent file indexes were not found")
        self._require_torrents_in_category((clean_hash,), category)
        response = self._request_text(
            "api/v2/torrents/filePrio",
            data={
                "hash": clean_hash,
                "id": "|".join(str(index) for index in clean_indexes),
                "priority": str(priority),
            },
        )
        _require_ok_response(
            response,
            "qBittorrent could not change the torrent file priority",
        )
        return {
            "ok": True,
            "hash": clean_hash,
            "indexes": list(clean_indexes),
            "priority": priority,
        }

    def _require_torrents_in_category(
        self, hashes: tuple[str, ...], category: str
    ) -> None:
        query = urlencode({"hashes": "|".join(hashes)})
        raw = self._request_text(
            f"api/v2/torrents/info?{query}",
            max_bytes=1024 * 1024,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DownloaderError("qBittorrent returned an invalid task list") from exc
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise DownloaderError("qBittorrent returned an invalid task list")

        returned = {
            str(item.get("hash") or "").strip().lower(): str(item.get("category") or "")
            for item in payload
        }
        if set(returned) != set(hashes) or any(
            value != category for value in returned.values()
        ):
            raise DownloaderError(
                "one or more torrents are missing or outside the configured category"
            )

    def _login_if_needed(self, *, timeout_seconds: float | None = None) -> None:
        if self._logged_in or (not self.config.username and not self.config.password):
            return
        data = {"username": self.config.username, "password": self.config.password}
        if timeout_seconds is None:
            response = self._request_text("api/v2/auth/login", data=data)
        else:
            response = self._request_text(
                "api/v2/auth/login",
                data=data,
                max_bytes=256,
                timeout_seconds=timeout_seconds,
            )
        if response.strip().lower() not in {"", "ok."}:
            raise DownloaderError("qBittorrent login failed")
        self._logged_in = True

    def _request_text(
        self,
        path: str,
        data: dict[str, str] | None = None,
        *,
        max_bytes: int = 1024 * 1024,
        timeout_seconds: float = 15.0,
        retry_auth: bool = True,
    ) -> str:
        url = urljoin(self.config.url.rstrip("/") + "/", path)
        body = urlencode(data).encode("utf-8") if data is not None else None
        headers = {
            "User-Agent": f"jav-pilot/{__version__}",
            "Accept": "application/json, text/plain, */*",
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(
            url,
            data=body,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with self._opener.open(
                request, timeout=max(0.5, min(float(timeout_seconds), 30.0))
            ) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise DownloaderError(
                        f"qBittorrent response exceeded {max_bytes} bytes"
                    )
        except HTTPError as exc:
            exc.read(4096)
            if (
                retry_auth
                and exc.code in {401, 403}
                and path != "api/v2/auth/login"
                and (self.config.username or self.config.password)
            ):
                self._logged_in = False
                self._login_if_needed(timeout_seconds=timeout_seconds)
                return self._request_text(
                    path,
                    data=data,
                    max_bytes=max_bytes,
                    timeout_seconds=timeout_seconds,
                    retry_auth=False,
                )
            raise DownloaderHttpError(exc.code) from exc
        except URLError as exc:
            raise DownloaderError(str(exc.reason)) from exc
        except TimeoutError as exc:
            raise DownloaderError("request timed out") from exc
        return raw.decode("utf-8", errors="replace")

    def _require_config(self) -> None:
        if not self.config.configured:
            raise DownloaderError("qBittorrent is not configured")


def parse_add_download_payload(raw: bytes) -> AddDownloadRequest:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DownloaderError("invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise DownloaderError("request body must be an object")
    magnet = str(payload.get("magnet") or "").strip()
    if not magnet:
        raise DownloaderError("magnet is required")
    replacement_id = _optional_submission_identity(
        payload.get("replacement_id"),
        field="replacement_id",
    )
    idempotency_key = _optional_submission_identity(
        payload.get("idempotency_key"),
        field="idempotency_key",
    )
    if (replacement_id is None) != (idempotency_key is None):
        raise DownloaderError(
            "replacement_id and idempotency_key must be provided together"
        )
    return AddDownloadRequest(
        magnet=magnet,
        name=str(payload.get("name") or "").strip(),
        category=str(payload.get("category") or "").strip(),
        save_path=str(payload.get("save_path") or "").strip(),
        tags=str(payload.get("tags") or "").strip(),
        paused=_json_bool(payload, "paused", False),
        auto_organize=_json_bool(payload, "auto_organize", True),
        result=payload.get("result") if isinstance(payload.get("result"), dict) else {},
        magnet_info=(
            payload.get("magnet_info")
            if isinstance(payload.get("magnet_info"), dict)
            else {}
        ),
        replacement_id=replacement_id,
        idempotency_key=idempotency_key,
    )


def _optional_submission_identity(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DownloaderError(f"{field} is invalid")
    clean = value.strip()
    if (
        value != clean
        or not clean.isascii()
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", clean) is None
    ):
        raise DownloaderError(f"{field} is invalid")
    return clean


def parse_torrent_action_payload(raw: bytes) -> tuple[str, tuple[str, ...], bool]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DownloaderError("invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise DownloaderError("request body must be an object")
    action = str(payload.get("action") or "").strip().lower()
    if action not in TORRENT_ACTIONS:
        raise DownloaderError("unsupported torrent action")
    raw_hashes = payload.get("hashes")
    if isinstance(raw_hashes, str):
        raw_hashes = [raw_hashes]
    if not isinstance(raw_hashes, list):
        raise DownloaderError("hashes must be a list")
    hashes = _normalize_hashes([str(value) for value in raw_hashes])
    return action, hashes, _json_bool(payload, "delete_files", False)


def _normalize_hashes(hashes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not hashes or len(hashes) > 50:
        raise DownloaderError("provide between 1 and 50 torrent hashes")
    clean: list[str] = []
    seen: set[str] = set()
    for value in hashes:
        info_hash = str(value).strip().lower()
        if not INFO_HASH_RE.fullmatch(info_hash):
            raise DownloaderError("invalid torrent hash")
        if info_hash not in seen:
            seen.add(info_hash)
            clean.append(info_hash)
    if not clean:
        raise DownloaderError("at least one torrent hash is required")
    return tuple(clean)


def _relative_torrent_file_name(value: object) -> str:
    if not isinstance(value, str):
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    raw = value.strip()
    if (
        not raw
        or len(raw.encode("utf-8")) > PROBE_METADATA_TEXT_MAX_BYTES
        or raw.startswith(("/", "\\"))
        or "\\" in raw
        or "\x00" in raw
        or any(ord(character) < 32 for character in raw)
    ):
        raise DownloaderError("qBittorrent returned an unsafe torrent file path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
    ):
        raise DownloaderError("qBittorrent returned an unsafe torrent file path")
    return raw


def _torrent_file_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    if value < 0 or value > 20_000:
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    return value


def _torrent_file_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 2**63 - 1
    ):
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    return value


def _torrent_file_progress(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    progress = float(value)
    if not math.isfinite(progress) or progress < 0.0 or progress > 1.0:
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    return progress


def _torrent_file_priority(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in {0, 1, 6, 7}
    ):
        raise DownloaderError("qBittorrent returned an invalid torrent file list")
    return value


def _normalize_file_indexes(
    indexes: list[int] | tuple[int, ...],
) -> tuple[int, ...]:
    if not isinstance(indexes, (list, tuple)) or not indexes or len(indexes) > 20_000:
        raise DownloaderError("provide between 1 and 20000 torrent file indexes")
    clean: list[int] = []
    seen: set[int] = set()
    for value in indexes:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DownloaderError("invalid torrent file index")
        index = value
        if index < 0 or index > 20_000 or index in seen:
            raise DownloaderError("invalid torrent file index")
        seen.add(index)
        clean.append(index)
    return tuple(sorted(clean))


def _normalize_probe_hashes(
    hashes: list[str] | tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not hashes:
        if allow_empty:
            return ()
        raise DownloaderError("at least one probe torrent hash is required")
    if len(hashes) > PROBE_MAX_MAGNETS:
        raise DownloaderError(
            f"probe supports at most {PROBE_MAX_MAGNETS} torrent hashes"
        )
    clean: list[str] = []
    seen: set[str] = set()
    for value in hashes:
        info_hash = str(value).strip().lower()
        if not INFO_HASH_RE.fullmatch(info_hash):
            raise DownloaderError("invalid probe torrent hash")
        if info_hash not in seen:
            seen.add(info_hash)
            clean.append(info_hash)
    return tuple(clean)


def _normalize_probe_magnets(magnets: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not magnets or len(magnets) > PROBE_MAX_MAGNETS:
        raise DownloaderError(
            f"provide between 1 and {PROBE_MAX_MAGNETS} probe magnets"
        )
    uris: list[str] = []
    seen: set[str] = set()
    for value in magnets:
        if not isinstance(value, str):
            raise DownloaderError("each probe magnet must be a string")
        uri = value.strip()
        if "\r" in uri or "\n" in uri:
            raise DownloaderError("probe magnet cannot contain line breaks")
        try:
            parsed = parse_magnet(uri)
        except MagnetError as exc:
            raise DownloaderError(str(exc)) from exc
        _require_supported_qb_magnet(parsed)
        if parsed.info_hash not in seen:
            seen.add(parsed.info_hash)
            uris.append(uri)
    if not uris:
        raise DownloaderError("at least one unique probe magnet is required")
    return tuple(uris)


def _require_supported_qb_magnet(magnet: MagnetInfo) -> None:
    xt_values = magnet.params.get("xt", [])
    if any(str(value).lower().startswith("urn:btmh:") for value in xt_values):
        raise DownloaderError("hybrid v1/v2 magnets are not supported")


def _parse_add_response(response: str, expected_hashes: tuple[str, ...]) -> str:
    detail = str(response or "").strip()
    if detail.lower() in {"", "ok."}:
        return "ok"
    if detail.lower() == "fails.":
        return "failed"

    try:
        payload = json.loads(detail)
    except json.JSONDecodeError as exc:
        raise DownloaderError("qBittorrent returned an invalid add response") from exc

    required_keys = {
        "success_count",
        "failure_count",
        "pending_count",
        "added_torrent_ids",
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise DownloaderError("qBittorrent returned an invalid add response")

    counts: dict[str, int] = {}
    for key in ("success_count", "failure_count", "pending_count"):
        value = payload.get(key)
        if type(value) is not int or value < 0:
            raise DownloaderError("qBittorrent returned an invalid add response")
        counts[key] = value

    raw_ids = payload.get("added_torrent_ids")
    if not isinstance(raw_ids, list) or any(
        not isinstance(value, str) for value in raw_ids
    ):
        raise DownloaderError("qBittorrent returned an invalid add response")
    added_ids = tuple(value.strip().lower() for value in raw_ids)
    if (
        len(added_ids) != counts["success_count"]
        or len(set(added_ids)) != len(added_ids)
        or any(not INFO_HASH_RE.fullmatch(value) for value in added_ids)
        or not set(added_ids).issubset(expected_hashes)
        or sum(counts.values()) != len(expected_hashes)
    ):
        raise DownloaderError("qBittorrent returned an invalid add response")

    accepted_count = counts["success_count"] + counts["pending_count"]
    if accepted_count == 0:
        return "failed"
    if counts["failure_count"] > 0:
        return "partial"
    return "ok"


def _validated_probe_destination(
    config: QbittorrentConfig,
    category: str,
    tag: str,
    save_path: str,
) -> tuple[str, str, str]:
    clean_category = _probe_category(category)
    clean_tag = _probe_tag(tag)
    clean_path = _probe_path(save_path)
    expected_category, expected_tag, expected_path = resolve_probe_destination(
        config,
        PurePosixPath(clean_path).name,
    )
    if (clean_category, clean_tag, clean_path) != (
        expected_category,
        expected_tag,
        expected_path,
    ):
        raise DownloaderError(
            "probe marker does not match the current qBittorrent configuration"
        )
    return clean_category, clean_tag, clean_path


def _probe_request_timeout(value: float | None, *, default: float) -> float:
    if value is None:
        return default
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise DownloaderError("probe request timeout must be positive") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise DownloaderError("probe request timeout must be positive")
    return min(default, timeout)


def resolve_probe_destination(
    config: QbittorrentConfig,
    probe_id: str,
) -> tuple[str, str, str]:
    clean_probe_id = str(probe_id or "").strip()
    if not PROBE_ID_RE.fullmatch(clean_probe_id):
        raise DownloaderError("invalid probe id")
    normal_category = str(config.category or "").strip()
    if not normal_category:
        raise DownloaderError("normal qBittorrent category is required before probing")
    suffix = "-probe"
    category = f"{normal_category[: 120 - len(suffix)]}{suffix}"
    try:
        staging = validate_qb_staging_path(config.save_path)
    except QbPathError as exc:
        raise DownloaderError(str(exc)) from exc
    save_path = (staging / ".probe" / clean_probe_id).as_posix()
    return category, PROBE_TAG, save_path


def _probe_category(value: str) -> str:
    category = str(value or "").strip()
    if (
        not category
        or len(category) > 120
        or not category.endswith("-probe")
        or any(ord(character) < 32 for character in category)
    ):
        raise DownloaderError("invalid isolated probe category")
    return category


def _probe_tag(value: str) -> str:
    tag = str(value or "").strip()
    if tag != PROBE_TAG:
        raise DownloaderError("invalid isolated probe tag")
    return tag


def _probe_path(value: str) -> str:
    try:
        path = PurePosixPath(normalize_qb_path(value))
    except QbPathError as exc:
        raise DownloaderError(str(exc)) from exc
    parts = path.parts
    try:
        marker_index = parts.index(".probe")
    except ValueError as exc:
        raise DownloaderError(
            "probe path must use an isolated .probe directory"
        ) from exc
    if marker_index < 1 or marker_index != len(parts) - 2 or not parts[-1]:
        raise DownloaderError("probe path must end with .probe/<probe_id>")
    return path.as_posix()


def _snapshot_matches_probe(
    snapshot: dict[str, object],
    *,
    category: str,
    tag: str,
    save_path: str,
    added_after: int,
    added_before: int,
) -> bool:
    tags = snapshot.get("tags")
    added_on = snapshot.get("added_on")
    return bool(
        snapshot.get("category") == category
        and isinstance(tags, tuple)
        and tag in tags
        and snapshot.get("save_path") == save_path
        and isinstance(added_on, int)
        and added_after <= added_on <= added_before
    )


def _snapshot_matches_smart_selection(
    snapshot: dict[str, object],
    *,
    category: str,
    tag: str,
    save_path: str,
    added_after: int,
    added_before: int,
) -> bool:
    tags = snapshot.get("tags")
    added_on = snapshot.get("added_on")
    return bool(
        snapshot.get("category") == category
        and isinstance(tags, tuple)
        and tag in tags
        and snapshot.get("save_path") == save_path
        and isinstance(added_on, int)
        and added_after <= added_on <= added_before
    )


def _smart_selection_tag(value: object) -> str:
    tag = str(value or "").strip().lower()
    if not SMART_SELECTION_TAG_RE.fullmatch(tag):
        raise DownloaderError("invalid smart selection tag")
    return tag


def _require_ok_response(response: str, message: str) -> None:
    detail = str(response or "").strip()
    if detail.lower() not in {"", "ok."}:
        raise DownloaderError(detail or message)


def _parse_qb_version(raw_version: object, error_message: str) -> tuple[int, int, int]:
    match = QB_VERSION_RE.search(str(raw_version or ""))
    if not match:
        raise DownloaderError(error_message)
    return tuple(int(part or 0) for part in match.groups())


def _json_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if not isinstance(value, bool):
        raise DownloaderError(f"{key} must be a boolean")
    return value


def _probe_torrent_payload(item: dict[str, Any]) -> dict[str, object]:
    trackers: list[dict[str, int | None]] = []
    raw_trackers = item.get("trackers")
    if isinstance(raw_trackers, list):
        for tracker in raw_trackers:
            if not isinstance(tracker, dict):
                continue
            trackers.append(
                {
                    "status": _nullable_nonnegative_int(tracker.get("status")),
                    "seeders": _nullable_nonnegative_int(tracker.get("num_seeds")),
                    "leechers": _nullable_nonnegative_int(tracker.get("num_leeches")),
                }
            )
    state = str(item.get("state") or "unknown")
    total_size = _nullable_nonnegative_int(item.get("total_size"))
    progress = _nullable_nonnegative_float(item.get("progress"))
    return {
        "hash": str(item.get("hash") or "").strip().lower(),
        "name": _safe_probe_torrent_name(item.get("name")),
        "category": str(item.get("category") or ""),
        "tags": tuple(
            tag.strip() for tag in str(item.get("tags") or "").split(",") if tag.strip()
        ),
        "save_path": str(item.get("save_path") or ""),
        "added_on": _nullable_nonnegative_int(item.get("added_on")),
        "state": state,
        "seeders": _nullable_nonnegative_int(item.get("num_complete")),
        "connected_seeders": _nullable_nonnegative_int(item.get("num_seeds")),
        "leechers": _nullable_nonnegative_int(item.get("num_incomplete")),
        "connected_leechers": _nullable_nonnegative_int(item.get("num_leechs")),
        "availability": _nullable_nonnegative_float(item.get("availability")),
        "downloaded": _nullable_nonnegative_int(item.get("downloaded")),
        "download_speed": _nullable_nonnegative_int(item.get("dlspeed")),
        "upload_speed": _nullable_nonnegative_int(item.get("upspeed")),
        "peak_download_speed": _nullable_nonnegative_int(item.get("dlspeed")),
        "progress": progress,
        "amount_left": _nullable_nonnegative_int(item.get("amount_left")),
        "size": _nullable_nonnegative_int(item.get("size")),
        "dl_limit": _nullable_nonnegative_int(item.get("dl_limit")),
        "total_size": total_size,
        "_metadata_received": bool(
            total_size is not None
            and total_size > 0
            and state not in {"forcedMetaDL", "metaDL"}
        ),
        "trackers": tuple(trackers),
    }


def _safe_probe_torrent_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if (
        not name
        or len(name.encode("utf-8", errors="replace")) > PROBE_METADATA_TEXT_MAX_BYTES
        or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        return None
    return name


def normalize_torrent_history_query(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    ascii_query = unicodedata.normalize("NFKC", raw).strip().upper()
    if (
        not ascii_query.isascii()
        or len(ascii_query) > TORRENT_HISTORY_QUERY_MAX_LENGTH
        or re.fullmatch(r"[A-Z0-9._-]+", ascii_query) is None
    ):
        raise DownloaderError("download query must be a valid catalog code or prefix")
    normalized = normalize_catalog_code(
        ascii_query,
        max_length=TORRENT_HISTORY_QUERY_MAX_LENGTH,
    )
    if normalized is not None:
        return normalized[1]
    if re.fullmatch(r"[A-Z]{2,12}", ascii_query):
        return ascii_query
    raise DownloaderError("download query must be a valid catalog code or prefix")


def filter_torrent_history(
    tasks: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    filter_name: str,
    catalog_query: str = "",
) -> list[dict[str, object]]:
    if filter_name not in TORRENT_FILTERS:
        raise DownloaderError("unsupported torrent filter")
    return [
        task
        for task in tasks
        if _torrent_matches_filter(task, filter_name)
        and _torrent_matches_catalog_query(task, catalog_query)
    ]


def torrent_history_summary(
    tasks: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    category: str,
) -> dict[str, object]:
    return {
        "scope": "category",
        "category": str(category),
        "total": len(tasks),
        "downloading": sum(task.get("stage") == "downloading" for task in tasks),
        "completed": sum(task.get("stage") == "completed" for task in tasks),
        "errors": sum(
            task.get("stage") == "error" or bool(task.get("issue")) for task in tasks
        ),
        "speed": sum(_safe_int(task.get("dlspeed")) for task in tasks),
    }


def _torrent_matches_catalog_query(task: dict[str, object], catalog_query: str) -> bool:
    if not catalog_query:
        return True
    normalized_name = unicodedata.normalize("NFKC", str(task.get("name") or "")).upper()
    searchable_name = "".join(
        character
        for character in normalized_name
        if character.isascii() and character.isalnum()
    )
    return catalog_query in searchable_name


def _torrent_matches_filter(task: dict[str, object], filter_name: str) -> bool:
    if filter_name == "all":
        return True
    stage = str(task.get("stage") or "")
    state = str(task.get("state") or "")
    if filter_name == "downloading":
        return stage == "downloading"
    if filter_name == "completed":
        return stage == "completed"
    if filter_name == "paused":
        return state in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
    if filter_name == "errored":
        return stage == "error"
    if filter_name == "active":
        return _safe_int(task.get("dlspeed")) > 0 or _safe_int(task.get("upspeed")) > 0
    if filter_name == "inactive":
        return (
            _safe_int(task.get("dlspeed")) == 0 and _safe_int(task.get("upspeed")) == 0
        )
    if filter_name == "resumed":
        return state not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
    if filter_name == "stalled":
        return state in {"stalledDL", "stalledUP"}
    if filter_name == "stalled_uploading":
        return state == "stalledUP"
    if filter_name == "stalled_downloading":
        return state == "stalledDL"
    return False


def _torrent_payload(item: dict[str, Any]) -> dict[str, object]:
    state = str(item.get("state") or "unknown")
    progress = _safe_float(item.get("progress"), minimum=0.0, maximum=1.0)
    stage = _torrent_stage(state, progress)
    eta = _safe_int(item.get("eta"))
    if eta >= 8_640_000:
        eta = -1
    return {
        "hash": str(item.get("hash") or "").lower(),
        "name": str(item.get("name") or ""),
        "state": state,
        "stage": stage,
        "progress": progress,
        "size": _safe_int(item.get("size")),
        "downloaded": _safe_int(item.get("downloaded")),
        "amount_left": _safe_int(item.get("amount_left")),
        "dlspeed": _safe_int(item.get("dlspeed")),
        "upspeed": _safe_int(item.get("upspeed")),
        "eta": eta,
        "ratio": _safe_float(item.get("ratio"), minimum=0.0, maximum=1_000_000.0),
        "category": str(item.get("category") or ""),
        "tags": str(item.get("tags") or ""),
        "save_path": str(item.get("save_path") or ""),
        "added_on": _safe_int(item.get("added_on")),
        "completion_on": _safe_int(item.get("completion_on")),
        "issue": _torrent_issue(state),
        "can_pause": stage in {"queued", "downloading", "checking"},
        "can_resume": stage == "paused",
        "complete": progress >= 1.0 or stage == "completed",
    }


def _torrent_stage(state: str, progress: float) -> str:
    if state in {"error", "missingFiles", "unknown"}:
        return "error"
    if state in {"checkingDL", "checkingUP", "checkingResumeData"}:
        return "checking"
    if progress >= 1.0 or state in {
        "uploading",
        "stalledUP",
        "queuedUP",
        "forcedUP",
        "pausedUP",
        "stoppedUP",
    }:
        return "completed"
    if state in {"pausedDL", "stoppedDL"}:
        return "paused"
    if state in {"downloading", "metaDL", "stalledDL", "forcedDL", "allocating"}:
        return "downloading"
    return "queued"


def _torrent_issue(state: str) -> str:
    return {
        "error": "qBittorrent reported a task error",
        "missingFiles": "downloaded files are missing",
        "stalledDL": "download has no active peers",
        "metaDL": "waiting for magnet metadata",
        "unknown": "qBittorrent returned an unknown state",
    }.get(state, "")


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_float(value: object, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return minimum
    if not math.isfinite(parsed):
        return minimum
    return max(minimum, min(parsed, maximum))


def _nullable_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _nullable_nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed
