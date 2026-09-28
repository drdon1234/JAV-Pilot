"""Failed download replacement and archive endpoints."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from http import HTTPStatus

from ...core.catalog_code import normalize_catalog_code
from ...core.guards import QueryError
from ...downloads.replacements import (
    DownloadReplacementConflictError,
    DownloadReplacementError,
    DownloadReplacementNotFoundError,
)
from ...torrent.magnet_selection import (
    MagnetSelectionBusyError,
    MagnetSelectionConflictError,
    MagnetSelectionError,
)
from ...torrent.qbittorrent import DownloaderError
from ...web_download.errors import WebDownloadError
from .. import state
from ..request import query_params, single_param, strict_int_param
from ..services.replacements import (
    complete_download_replacement_smart_selection,
    dispose_download_replacement_candidates,
    download_replacement_magnet_uris,
    download_replacement_source,
    download_replacement_store,
    download_resource_recovery_manager,
    recover_download_replacement_cleanup,
)
from .common import CommonErrorResponses


class DownloadReplacementRoutes(CommonErrorResponses):
    def _handle_download_replacement_get(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            if set(params) != {"id"} or len(params["id"]) != 1:
                raise QueryError("download replacement query is invalid")
            replacement = download_replacement_store().get(single_param(params, "id"))
            replacement = recover_download_replacement_cleanup(replacement)
            if (
                str(replacement.get("status") or "") == "open"
                and str(replacement.get("discovery_status") or "") == "queued"
            ):
                replacement = download_resource_recovery_manager().resume(
                    replacement["replacement_id"]
                )
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DownloadReplacementError as exc:
            self._send_download_replacement_error(exc)
            return
        except (DownloaderError, WebDownloadError, OSError, sqlite3.Error) as exc:
            self._send_download_replacement_error(exc)
            return
        self._send_json({"ok": True, "replacement": replacement})

    def _handle_failed_download_archive_get(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            if set(params) - {"limit", "offset"}:
                raise QueryError("failed download archive query is invalid")
            limit = strict_int_param(params, "limit", 50)
            offset = strict_int_param(params, "offset", 0)
            archive = download_replacement_store().failed_archive_page(
                limit=limit,
                offset=offset,
            )
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DownloadReplacementError as exc:
            self._send_download_replacement_error(exc)
            return
        except (OSError, sqlite3.Error) as exc:
            self._send_download_replacement_error(exc)
            return
        self._send_json({"ok": True, **archive})

    def _handle_failed_download_archive_action(self) -> None:
        try:
            payload = self._read_json_body(8 * 1024)
            action = str(payload.get("action") or "").strip().lower()
            store = download_replacement_store()
            if action == "delete" and set(payload) == {"action", "code"}:
                removed = 1 if store.delete_failed_archive(payload.get("code")) else 0
            else:
                raise ValueError("failed download archive action is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DownloadReplacementError as exc:
            self._send_download_replacement_error(exc)
            return
        except (OSError, sqlite3.Error) as exc:
            self._send_download_replacement_error(exc)
            return
        self._send_json({"ok": True, "removed": removed})

    def _handle_download_replacement_create(self) -> None:
        try:
            payload = self._read_json_body(8 * 1024)
            if set(payload) != {"source_kind", "source_id", "mode"}:
                raise ValueError("download replacement request is invalid")
            source = download_replacement_source(
                payload.get("source_kind"), payload.get("source_id")
            )
            replacement = download_replacement_store().create_or_reuse(**source)
            replacement = recover_download_replacement_cleanup(replacement)
            if str(replacement.get("status") or "") == "open":
                replacement = download_resource_recovery_manager().enqueue(
                    replacement["replacement_id"],
                    mode=payload.get("mode"),
                )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DownloadReplacementError as exc:
            self._send_download_replacement_error(exc)
            return
        except (DownloaderError, WebDownloadError, OSError, sqlite3.Error) as exc:
            self._send_download_replacement_error(exc)
            return
        self._send_json({"ok": True, "replacement": replacement})

    def _handle_download_replacement_smart_selection_start(self) -> None:
        try:
            payload = self._read_json_body(8 * 1024)
            if set(payload) != {"replacement_id"}:
                raise ValueError("download replacement smart selection request is invalid")
            store = download_replacement_store()
            replacement = store.get(payload.get("replacement_id"))
            replacement = recover_download_replacement_cleanup(replacement)
            if str(replacement.get("status") or "") != "open":
                raise DownloadReplacementConflictError(
                    "download replacement is no longer open"
                )
            if (
                str(replacement.get("recovery_mode") or "") != "smart_magnet"
                or str(replacement.get("discovery_status") or "") != "available"
                or str(replacement.get("magnet_status") or "") != "available"
            ):
                raise DownloadReplacementConflictError(
                    "download replacement has no confirmed smart-selection magnets"
                )
            source = download_replacement_source(
                replacement["source_kind"], replacement["source_id"]
            )
            if (
                str(source["source_revision"]) != str(replacement["source_revision"])
                or normalize_catalog_code(source["code"], max_length=40)[1]
                != normalize_catalog_code(replacement["code"], max_length=40)[1]
            ):
                raise DownloadReplacementConflictError(
                    "download failure changed before smart selection"
                )
            magnets = download_replacement_magnet_uris(replacement)
            selection_id = hashlib.sha256(
                (
                    "download-replacement-selection\0"
                    f"{replacement['replacement_id']}\0"
                    f"{replacement.get('discovery_finished_at')}"
                ).encode("ascii")
            ).hexdigest()[:32]

            selection_claimed = False

            def persist_selection_start(_selection_id: str) -> None:
                nonlocal selection_claimed
                store.begin_smart_selection(
                    replacement["replacement_id"],
                    selection_id=selection_id,
                    expected_source_revision=replacement["source_revision"],
                )
                selection_claimed = True

            def complete_selection(
                selection_payload: dict[str, object],
            ) -> dict[str, object] | None:
                return complete_download_replacement_smart_selection(
                    str(replacement["replacement_id"]),
                    selection_payload,
                    selection_id=selection_id,
                    expected_source_revision=str(replacement["source_revision"]),
                )

            try:
                result = state.MAGNET_SELECTIONS.start(
                    magnets,
                    selection_id=selection_id,
                    on_start=persist_selection_start,
                    on_complete=complete_selection,
                )
            except MagnetSelectionError:
                if selection_claimed:
                    try:
                        store.finish_smart_selection(
                            replacement["replacement_id"],
                            selection_id=selection_id,
                            expected_source_revision=replacement["source_revision"],
                            outcome="failed",
                            cleanup_status="not_required",
                        )
                    except DownloadReplacementError:
                        pass
                raise
            if (
                result.get("status") in {"complete", "failed", "cancelled"}
                and not isinstance(result.get("replacement"), Mapping)
            ):
                result = dict(result)
                outcome = complete_selection(dict(result))
                if outcome is not None:
                    result["replacement"] = outcome
                result.pop("replacement_error", None)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MagnetSelectionBusyError as exc:
            self._send_json(
                {"ok": False, "error": str(exc)}, HTTPStatus.TOO_MANY_REQUESTS
            )
            return
        except (MagnetSelectionConflictError, DownloadReplacementConflictError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except DownloadReplacementNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (MagnetSelectionError, DownloadReplacementError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (DownloaderError, WebDownloadError, OSError, sqlite3.Error) as exc:
            self._send_download_replacement_error(exc)
            return
        self._send_json(result, HTTPStatus.ACCEPTED)

    def _handle_download_replacement_cleanup_no_sources(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if payload:
                raise ValueError("resource cleanup request is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._dispose_download_replacement_candidates(
            disposition="delete",
            no_sources_only=True,
        )

    def _handle_download_replacement_disposition_preview(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if payload:
                raise ValueError("failed download disposition preview is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if not state.DOWNLOAD_REPLACEMENT_CLEANUP_LOCK.acquire(blocking=False):
            self._send_json(
                {"ok": False, "error": "resource cleanup is already running"},
                HTTPStatus.CONFLICT,
            )
            return
        try:
            store = download_replacement_store()
            store.recover_interrupted_dispositions()
            candidates = store.disposable_failure_snapshot()
            snapshot_token = state.DOWNLOAD_DISPOSITION_SNAPSHOTS.create(candidates)
            source_counts = {
                "bt": sum(
                    1 for candidate in candidates if candidate.get("source_kind") == "qb"
                ),
                "web": sum(
                    1 for candidate in candidates if candidate.get("source_kind") != "qb"
                ),
            }
        except (DownloadReplacementError, OSError, sqlite3.Error) as exc:
            self._send_download_replacement_error(exc)
            return
        finally:
            state.DOWNLOAD_REPLACEMENT_CLEANUP_LOCK.release()
        self._send_json(
            {
                "ok": True,
                "snapshot_token": snapshot_token,
                "count": len(candidates),
                "source_counts": source_counts,
                "expires_in_seconds": int(
                    state.DOWNLOAD_DISPOSITION_SNAPSHOTS.ttl_seconds
                ),
            }
        )

    def _handle_download_replacement_dispose(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if set(payload) != {"disposition", "snapshot_token"}:
                raise ValueError("failed download disposition request is invalid")
            disposition = str(payload.get("disposition") or "").strip().lower()
            if disposition not in {"archive", "delete"}:
                raise ValueError("failed download disposition is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._dispose_download_replacement_candidates(
            disposition=disposition,
            no_sources_only=False,
            snapshot_token=payload.get("snapshot_token"),
        )

    def _dispose_download_replacement_candidates(
        self,
        *,
        disposition: str,
        no_sources_only: bool,
        snapshot_token: object | None = None,
    ) -> None:
        if not state.DOWNLOAD_REPLACEMENT_CLEANUP_LOCK.acquire(blocking=False):
            self._send_json(
                {"ok": False, "error": "resource cleanup is already running"},
                HTTPStatus.CONFLICT,
            )
            return
        snapshot_started = False
        try:
            store = download_replacement_store()
            cached_result: dict[str, object] | None = None
            if snapshot_token is None:
                store.recover_interrupted_dispositions()
                candidates = store.disposable_failure_snapshot(
                    no_sources_only=no_sources_only
                )
            else:
                candidates, cached_result = state.DOWNLOAD_DISPOSITION_SNAPSHOTS.begin(
                    snapshot_token,
                    disposition=disposition,
                )
                snapshot_started = cached_result is None
            result = (
                cached_result
                if cached_result is not None
                else dispose_download_replacement_candidates(
                    store,
                    candidates,
                    disposition=disposition,
                )
            )
            if snapshot_started:
                state.DOWNLOAD_DISPOSITION_SNAPSHOTS.complete(
                    snapshot_token,
                    disposition=disposition,
                    result=result,
                )
        except (DownloadReplacementError, OSError, sqlite3.Error) as exc:
            if snapshot_started:
                state.DOWNLOAD_DISPOSITION_SNAPSHOTS.discard(snapshot_token)
            self._send_download_replacement_error(exc)
            return
        except Exception:
            if snapshot_started:
                state.DOWNLOAD_DISPOSITION_SNAPSHOTS.discard(snapshot_token)
            raise
        finally:
            state.DOWNLOAD_REPLACEMENT_CLEANUP_LOCK.release()
        self._send_json({"ok": True, **result})
