"""Failed download replacement: discovery, smart selection and disposal."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from ...config.app_config import AppConfig
from ...config.settings import BUILTIN_WEB_DOWNLOAD_SITE_IDS, load_settings
from ...core.catalog_code import normalize_catalog_code
from ...core.guards import contains_sensitive_transport_text
from ...core.models import SearchBounds
from ...downloads.replacements import (
    DownloadReplacementConflictError,
    DownloadReplacementError,
    DownloadReplacementNotFoundError,
    DownloadReplacementStore,
)
from ...downloads.resource_recovery import (
    DownloadResourceRecoveryManager,
    MagnetDiscovery,
    WebDiscovery,
)
from ...media_metadata.manager import MediaMetadataConfig, MediaMetadataError
from ...media_metadata.store import MediaMetadataStore, MediaMetadataStoreError
from ...missav.browser_runtime import get_missav_browser_runtime
from ...search.engine import default_indexers, search
from ...torrent.inputs import catalog_code_from_download_metadata
from ...torrent.magnet import parse_magnet
from ...torrent.qbittorrent import (
    AddDownloadRequest,
    DownloaderError,
    QbittorrentClient,
)
from ...web_download.batches.errors import WebDownloadBatchNotFoundError
from ...web_download.batches.store import WebDownloadBatchStore
from ...web_download.config import WebDownloadConfig
from ...web_download.errors import WebDownloadError, WebDownloadNotFoundError
from ...web_download.job_store import WebDownloadStore
from ...web_download.jobs import normalize_web_download_code
from ...web_download.manager import WebDownloadManager
from ...web_download.providers import (
    PriorityWebDownloadProvider,
    WebDownloadProviderError,
    WebDownloadProviderNotFound,
)
from ...web_download.variant import DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY
from .. import state
from .history import require_operational_mode
from .torrents import (
    delete_qb_with_metadata,
    discard_incomplete_qb_metadata,
    download_metadata_code,
    enqueue_qb_metadata,
    submit_qb_download,
)
from .web_downloads import (
    web_download_batch_manager,
    web_download_manager,
    web_download_site_availability,
)

QB_REPLACEMENT_FAILURE_STATES = frozenset(
    {"error", "missingFiles", "unknown", "stalledDL"}
)


def download_replacement_store(
    config: WebDownloadConfig | None = None,
) -> DownloadReplacementStore:
    active_config = config or WebDownloadConfig.from_env()
    database_path = Path(active_config.database_path)
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            DownloadReplacementError(
                "Download replacements are unavailable in maintenance mode"
            )
        )
        with state.DOWNLOAD_REPLACEMENTS_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise DownloadReplacementError("server is shutting down")
            if (
                state.DOWNLOAD_REPLACEMENTS is None
                or state.DOWNLOAD_REPLACEMENTS.path != database_path
            ):
                state.DOWNLOAD_REPLACEMENTS = DownloadReplacementStore(database_path)
            return state.DOWNLOAD_REPLACEMENTS


def download_resource_recovery_manager(
    config: WebDownloadConfig | None = None,
) -> DownloadResourceRecoveryManager:
    active_config = config or WebDownloadConfig.from_env()
    store = download_replacement_store(active_config)
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            DownloadReplacementError(
                "Download resource recovery is unavailable in maintenance mode"
            )
        )
        with state.DOWNLOAD_RESOURCE_RECOVERY_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise DownloadReplacementError("server is shutting down")
            if state.DOWNLOAD_RESOURCE_RECOVERY is None:
                state.DOWNLOAD_RESOURCE_RECOVERY = DownloadResourceRecoveryManager(
                    store,
                    probe_magnets=_probe_replacement_magnets,
                    probe_web=_probe_replacement_web,
                    worker_count=2,
                )
            elif state.DOWNLOAD_RESOURCE_RECOVERY.store.path != store.path:
                previous = state.DOWNLOAD_RESOURCE_RECOVERY
                state.DOWNLOAD_RESOURCE_RECOVERY = DownloadResourceRecoveryManager(
                    store,
                    probe_magnets=_probe_replacement_magnets,
                    probe_web=_probe_replacement_web,
                    worker_count=2,
                )
                previous.shutdown(timeout=5.0)
            return state.DOWNLOAD_RESOURCE_RECOVERY


def _probe_replacement_magnets(
    code: str,
    excluded_info_hashes: tuple[str, ...],
    cancel_event: threading.Event,
) -> MagnetDiscovery:
    if cancel_event.is_set():
        return MagnetDiscovery("unavailable", error_code="cancelled")
    settings = load_settings()
    indexers = default_indexers(settings)
    if not indexers:
        return MagnetDiscovery("unavailable", error_code="configuration")
    try:
        response = search(
            code,
            sources=tuple(indexers),
            bounds=SearchBounds(
                limit=20,
                page=1,
                max_pages=1,
                result_limit=20,
                fetch_magnets=True,
                detail_limit=1,
                timeout_seconds=10.0,
                match="exact",
                search_kind="code",
            ),
            indexers=indexers,
            cancelled=cancel_event.is_set,
        )
    except Exception as exc:
        return MagnetDiscovery("unavailable", error_code=_discovery_error_code(exc))
    if cancel_event.is_set():
        return MagnetDiscovery("unavailable", error_code="cancelled")
    excluded = {str(value).strip().lower() for value in excluded_info_hashes}
    magnets_by_hash = {
        magnet.info_hash: magnet.to_dict()
        for result in response.results
        for magnet in result.magnets
        if magnet.info_hash and magnet.info_hash.lower() not in excluded
    }
    magnets = tuple(magnets_by_hash.values())
    count = len(magnets)
    if count:
        return MagnetDiscovery("available", count=min(count, 999), magnets=magnets[:20])
    if response.errors or any(
        source.parse_status == "error"
        or source.magnet_error
        for result in response.results
        for source in result.sources
    ):
        return MagnetDiscovery("unavailable", error_code="source_unavailable")
    if response.results:
        return MagnetDiscovery("not_found", error_code="no_magnets")
    return MagnetDiscovery("not_found", error_code="no_result")


def _probe_replacement_web(
    code: str,
    cancel_event: threading.Event,
) -> WebDiscovery:
    if cancel_event.is_set():
        return WebDiscovery("unavailable", error_code="cancelled")
    try:
        available, _reason, _providers = web_download_site_availability()
    except Exception as exc:
        return WebDiscovery("unavailable", error_code=_discovery_error_code(exc))
    if not available:
        return WebDiscovery("unavailable", error_code="configuration")
    try:
        provider = PriorityWebDownloadProvider(get_missav_browser_runtime())
        config = WebDownloadConfig.from_env()
        first_error: str | None = None
        for variant in DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY:
            if cancel_event.is_set():
                return WebDiscovery("unavailable", error_code="cancelled")
            try:
                manifest = provider.capture_manifest(
                    code,
                    provider="auto",
                    variant=variant,
                    timeout_seconds=config.capture_timeout_seconds,
                    requested_height=None,
                    quality_strategy="legacy",
                    cancel_event=cancel_event,
                )
                provider_id = str(getattr(manifest, "provider", "") or "").strip().lower()
                if provider_id not in BUILTIN_WEB_DOWNLOAD_SITE_IDS:
                    return WebDiscovery("unavailable", error_code="provider_invalid")
                return WebDiscovery(
                    "available",
                    provider_ids=(provider_id,),
                    variant=str(variant),
                )
            except WebDownloadProviderNotFound:
                continue
            except WebDownloadProviderError as exc:
                first_error = _discovery_error_code(exc)
                break
            except Exception as exc:  # provider boundary keeps transport details out of storage
                first_error = _discovery_error_code(exc)
                break
        if first_error is not None:
            return WebDiscovery("unavailable", error_code=first_error)
        return WebDiscovery("not_found")
    except Exception as exc:
        return WebDiscovery("unavailable", error_code=_discovery_error_code(exc))


def _discovery_error_code(error: BaseException) -> str:
    value = str(getattr(error, "code", "") or "discovery_failed").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value or "") is None:
        return "discovery_failed"
    return value[:64]


def _download_replacement_magnet_sources(
    replacement: Mapping[str, object],
) -> dict[str, str]:
    excluded = {
        str(replacement.get("source_id") or "").strip().lower()
    } if str(replacement.get("source_kind") or "") == "qb" else set()
    result: dict[str, str] = {}
    snapshots = replacement.get("magnets")
    if not isinstance(snapshots, list):
        snapshots = []
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        info_hash = str(snapshot.get("info_hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", info_hash) or info_hash in excluded:
            continue
        refs = snapshot.get("source_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            uri = str(ref.get("uri") or "").strip()
            try:
                parsed = parse_magnet(uri)
            except ValueError:
                continue
            if parsed.info_hash == info_hash:
                result.setdefault(info_hash, uri)
                break
    if not result:
        raise DownloadReplacementConflictError(
            "download replacement has no eligible magnet source"
        )
    return result


def download_replacement_magnet_uris(
    replacement: Mapping[str, object],
) -> tuple[str, ...]:
    return tuple(_download_replacement_magnet_sources(replacement).values())


def complete_download_replacement_smart_selection(
    replacement_id: str,
    selection: Mapping[str, object],
    *,
    selection_id: str,
    expected_source_revision: str,
) -> dict[str, object] | None:
    selection_result = selection.get("selection")
    selection_status = (
        str(selection_result.get("status") or "").strip().lower()
        if isinstance(selection_result, Mapping)
        else ""
    )
    selected_hash = str(
        selection_result.get("selected_info_hash") or ""
        if isinstance(selection_result, Mapping)
        else ""
    ).strip().lower()
    result_status = str(selection.get("status") or "").strip().lower()
    failure_kind = str(selection.get("failure_kind") or "").strip().lower()
    if result_status == "cancelled":
        outcome = "cancelled"
    elif (
        result_status == "failed"
        and failure_kind == "selection"
        and selection_status in {"not_found", "inconclusive"}
    ):
        outcome = selection_status
    elif result_status == "complete" and selection_status == "selected" and re.fullmatch(
        r"[0-9a-f]{40}", selected_hash
    ):
        outcome = "selected"
    else:
        outcome = "failed"
    cleanup = selection.get("cleanup")
    cleanup_status = (
        str(cleanup.get("status") or "unknown").strip().lower()
        if isinstance(cleanup, Mapping)
        else "unknown"
    )
    if cleanup_status not in {
        "complete",
        "not_required",
        "incomplete",
        "unknown",
    }:
        cleanup_status = "unknown"
    store = download_replacement_store()
    replacement = store.finish_smart_selection(
        replacement_id,
        selection_id=selection_id,
        expected_source_revision=expected_source_revision,
        outcome=outcome,
        cleanup_status=cleanup_status,
    )
    if outcome != "selected":
        return None
    sources = _download_replacement_magnet_sources(replacement)
    uri = sources.get(selected_hash)
    if uri is None:
        raise DownloadReplacementConflictError(
            "smart selection winner is outside the confirmed source snapshot"
        )
    display_name = str(replacement.get("code") or "")
    snapshots = replacement.get("magnets")
    if isinstance(snapshots, list):
        selected = next(
            (
                item
                for item in snapshots
                if isinstance(item, Mapping)
                and str(item.get("info_hash") or "").strip().lower()
                == selected_hash
            ),
            None,
        )
        if selected is not None:
            display_name = str(selected.get("display_name") or display_name)
    request = AddDownloadRequest(
        magnet=uri,
        name=display_name,
        auto_organize=True,
        result={"code": replacement["code"]},
        replacement_id=str(replacement["replacement_id"]),
        idempotency_key=str(replacement["idempotency_key"]),
    )
    submitted = submit_qb_replacement(request, selected_hash)
    outcome = submitted.get("replacement")
    if not isinstance(outcome, Mapping):
        raise DownloadReplacementError(
            "smart selection replacement outcome is missing"
        )
    return dict(outcome)


def download_replacement_source(
    source_kind: object,
    source_id: object,
) -> dict[str, object]:
    kind = str(source_kind or "").strip().lower()
    raw_id = str(source_id or "").strip()
    if kind == "qb":
        clean_id = raw_id.lower()
        if re.fullmatch(r"[0-9a-f]{40}", clean_id) is None:
            raise DownloadReplacementError(
                "download replacement source identity is invalid"
            )
        qb_config = AppConfig.from_env().qbittorrent
        if not qb_config.configured or not qb_config.category.strip():
            raise DownloaderError("qBittorrent is not configured")
        snapshot = QbittorrentClient(qb_config).torrent_snapshot(clean_id)
        if snapshot is None:
            raise DownloadReplacementNotFoundError(
                "failed qBittorrent download was not found"
            )
        if not _is_replaceable_qb_failure(snapshot, qb_config.category):
            raise DownloadReplacementConflictError(
                "qBittorrent download is no longer a replaceable failure"
            )
        code = _qb_failure_catalog_code(clean_id, snapshot)
        if code is None:
            raise DownloadReplacementConflictError(
                "qBittorrent download has no reliable catalog code"
            )
        return {
            "source_kind": kind,
            "source_id": clean_id,
            "source_revision": _qb_failure_revision(snapshot),
            "code": code,
        }

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{15,79}", raw_id) is None:
        raise DownloadReplacementError(
            "download replacement source identity is invalid"
        )
    config = WebDownloadConfig.from_env()
    if kind == "web_job":
        try:
            job = WebDownloadStore(config.database_path).get(raw_id)
        except WebDownloadNotFoundError as exc:
            raise DownloadReplacementNotFoundError(
                "failed web download was not found"
            ) from exc
        if not _web_job_can_reselect(job):
            raise DownloadReplacementConflictError(
                "web download is no longer a replaceable failure"
            )
        return {
            "source_kind": kind,
            "source_id": raw_id,
            "source_revision": _replacement_timestamp_revision(job["updated_at"]),
            "code": job["code"],
        }
    if kind == "web_intent":
        try:
            batch = WebDownloadBatchStore(config.database_path).get(raw_id)
        except WebDownloadBatchNotFoundError as exc:
            raise DownloadReplacementNotFoundError(
                "failed automatic web download was not found"
            ) from exc
        items = batch.get("items")
        has_attached_job = isinstance(items, list) and any(
            isinstance(item, dict) and item.get("job_id") is not None for item in items
        )
        if (
            not bool(batch.get("auto_commit"))
            or not _web_intent_can_reselect(batch)
            or has_attached_job
        ):
            raise DownloadReplacementConflictError(
                "automatic web download is no longer a replaceable failure"
            )
        return {
            "source_kind": kind,
            "source_id": raw_id,
            "source_revision": _replacement_timestamp_revision(batch["updated_at"]),
            "code": batch["code_or_prefix"],
        }
    raise DownloadReplacementError("download replacement source kind is invalid")


def _replacement_timestamp_revision(value: object) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DownloadReplacementError(
            "download replacement source revision is invalid"
        ) from exc
    if not 0 <= timestamp < float("inf"):
        raise DownloadReplacementError(
            "download replacement source revision is invalid"
        )
    return format(timestamp, ".17g")


def _is_replaceable_qb_failure(
    snapshot: Mapping[str, object], category: object
) -> bool:
    try:
        progress = float(snapshot.get("progress") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        str(snapshot.get("category") or "") == str(category or "").strip()
        and progress < 1.0
        and str(snapshot.get("state") or "") in QB_REPLACEMENT_FAILURE_STATES
    )


def _qb_failure_revision(snapshot: Mapping[str, object]) -> str:
    payload = {
        key: snapshot.get(key)
        for key in (
            "hash",
            "name",
            "state",
            "progress",
            "category",
            "save_path",
            "added_on",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _qb_failure_catalog_code(
    info_hash: str,
    snapshot: Mapping[str, object],
) -> str | None:
    metadata_job: dict[str, object] | None = None
    try:
        with state.MEDIA_METADATA_LOCK:
            manager = state.MEDIA_METADATA
        if manager is not None:
            metadata_job = manager.store.get_by_download("qb", info_hash)
        else:
            config = MediaMetadataConfig.from_env()
            if config.database_path.is_file():
                metadata_job = MediaMetadataStore(config.database_path).get_by_download(
                    "qb", info_hash
                )
    except (MediaMetadataError, MediaMetadataStoreError, OSError, sqlite3.Error):
        metadata_job = None
    candidates = (
        metadata_job.get("code") if metadata_job is not None else None,
        catalog_code_from_download_metadata(snapshot.get("name")),
    )
    for candidate in candidates:
        normalized = normalize_catalog_code(candidate, max_length=40)
        if normalized is not None:
            return normalized[0]
    return None


def with_qb_reselection(
    task: Mapping[str, object], *, category: str
) -> dict[str, object]:
    result = dict(task)
    try:
        info_hash = str(task.get("hash") or "").strip().lower()
        if not _is_replaceable_qb_failure(task, category):
            return result
        code = _qb_failure_catalog_code(info_hash, task)
        if code is not None:
            result["reselection"] = {
                "source_kind": "qb",
                "source_id": info_hash,
                "code": code,
            }
    except (DownloaderError, OSError, ValueError):
        pass
    return result


def attach_download_recovery(
    items: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    sources = []
    for item in items:
        reselection = item.get("reselection")
        if isinstance(reselection, Mapping):
            sources.append(
                (reselection.get("source_kind"), reselection.get("source_id"))
            )
    try:
        recoveries = download_replacement_store().get_for_sources(sources)
    except (DownloadReplacementError, OSError, sqlite3.Error):
        recoveries = {}
    result: list[dict[str, object]] = []
    for item in items:
        current = dict(item)
        reselection = current.get("reselection")
        if isinstance(reselection, Mapping):
            key = (
                str(reselection.get("source_kind") or ""),
                str(reselection.get("source_id") or ""),
            )
            recovery = recoveries.get(key)
            if recovery is not None and _download_recovery_matches_item(
                current, reselection, recovery
            ):
                current["reselection"] = {
                    **dict(reselection),
                    "recovery": recovery,
                }
        result.append(current)
    return result


def _download_recovery_matches_item(
    item: Mapping[str, object],
    reselection: Mapping[str, object],
    recovery: Mapping[str, object],
) -> bool:
    source_kind = str(reselection.get("source_kind") or "")
    try:
        if source_kind == "qb":
            source_revision = _qb_failure_revision(item)
        elif source_kind in {"web_job", "web_intent"}:
            source_revision = _replacement_timestamp_revision(item.get("updated_at"))
        else:
            return False
        current_code = normalize_catalog_code(
            reselection.get("code"), max_length=40
        )
        recovery_code = normalize_catalog_code(recovery.get("code"), max_length=40)
    except (DownloadReplacementError, TypeError, ValueError, OverflowError):
        return False
    return bool(
        current_code is not None
        and recovery_code is not None
        and current_code[1] == recovery_code[1]
        and source_revision == str(recovery.get("source_revision") or "")
    )


def download_replacement_no_source_count() -> int:
    try:
        return download_replacement_store().count_no_sources()
    except (DownloadReplacementError, OSError, sqlite3.Error):
        return 0


_WEB_DOWNLOAD_RESELECTION_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "retry_wait",
        "locating",
        "validating",
        "downloading",
        "verifying",
        "archiving",
        "pausing",
        "paused",
        "cancelling",
    }
)
_WEB_INTENT_RESELECTION_ACTIVE_STATUSES = frozenset(
    {"queued", "discovering", "ready"}
)


def _has_download_error(item: Mapping[str, object]) -> bool:
    value = item.get("error")
    return isinstance(value, str) and bool(value.strip())


def _web_job_can_reselect(job: Mapping[str, object]) -> bool:
    status = str(job.get("status") or "").strip().lower()
    if status in _WEB_DOWNLOAD_RESELECTION_ACTIVE_STATUSES:
        return False
    return status in {"failed", "cancelled"} or _has_download_error(job)


def _web_intent_can_reselect(intent: Mapping[str, object]) -> bool:
    status = str(intent.get("status") or "").strip().lower()
    if status in _WEB_INTENT_RESELECTION_ACTIVE_STATUSES:
        return False
    return (
        status in {"failed", "incomplete", "too_many", "cancelled"}
        or _has_download_error(intent)
    )


def with_web_job_reselection(job: Mapping[str, object]) -> dict[str, object]:
    result = dict(job)
    if _web_job_can_reselect(job):
        result["reselection"] = {
            "source_kind": "web_job",
            "source_id": str(job.get("job_id") or ""),
            "code": str(job.get("code") or ""),
        }
    return result


def with_web_intent_reselection(intent: Mapping[str, object]) -> dict[str, object]:
    result = dict(intent)
    items = intent.get("items")
    has_attached_job = isinstance(items, list) and any(
        isinstance(item, dict) and item.get("job_id") is not None for item in items
    )
    if (
        bool(intent.get("auto_commit"))
        and _web_intent_can_reselect(intent)
        and not has_attached_job
    ):
        result["reselection"] = {
            "source_kind": "web_intent",
            "source_id": str(intent.get("batch_id") or ""),
            "code": str(intent.get("code_or_prefix") or ""),
        }
    return result


def requires_web_download_restart(error: object) -> bool:
    return str(error or "").strip().lower() == "resume checkpoint is invalid"


def sanitized_bulk_retry_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    if not message or contains_sensitive_transport_text(message):
        return "retry could not be queued"
    if len(message) > 160:
        return "retry could not be queued"
    return message


def submit_qb_replacement(
    request: AddDownloadRequest,
    info_hash: str,
) -> dict[str, object]:
    replacement_id = request.replacement_id
    idempotency_key = request.idempotency_key
    if replacement_id is None or idempotency_key is None:
        raise DownloadReplacementError("download replacement identity is invalid")
    request_code = download_metadata_code(request.result)
    if request_code is None:
        raise DownloadReplacementConflictError(
            "replacement download has no reliable catalog code"
        )
    store = download_replacement_store()
    replacement = store.get(replacement_id)
    if (
        str(replacement.get("recovery_mode") or "")
        not in {"smart_magnet", "manual_magnet"}
        or str(replacement.get("discovery_status") or "") != "available"
        or str(replacement.get("magnet_status") or "") != "available"
    ):
        raise DownloadReplacementConflictError(
            "download replacement has no confirmed magnet source"
        )
    if info_hash not in _download_replacement_magnet_sources(replacement):
        raise DownloadReplacementConflictError(
            "replacement magnet is not part of the confirmed source snapshot"
        )
    claimed = store.begin_submission(
        replacement_id,
        idempotency_key=idempotency_key,
        target_kind="qb",
        target_id=info_hash,
        code=request_code,
    )
    phase = str(claimed["submission_phase"])
    if phase == "complete":
        result = _replacement_download_result(request, info_hash)
        result["replacement"] = _replacement_download_outcome(
            claimed,
            replayed=True,
        )
        return result
    lease_token = str(claimed.get("lease_token") or "")
    client = QbittorrentClient(AppConfig.from_env().qbittorrent)
    if phase == "cleanup":
        if not _replacement_target_exists(client, info_hash):
            final = store.finish_cleanup(
                replacement_id,
                removed=False,
                cleanup_error="replacement_target_missing",
                lease_token=lease_token,
            )
        else:
            final = _finish_download_replacement_cleanup(
                claimed, lease_token=lease_token, client=client
            )
        result = _replacement_download_result(request, info_hash)
        result["replacement"] = _replacement_download_outcome(
            final,
            replayed=True,
        )
        if str(final["status"]) != "completed":
            result["replacement_warning"] = (
                "The new task exists, but the old failed item could not be cleaned up"
            )
        return result

    try:
        current_source = download_replacement_source(
            claimed["source_kind"], claimed["source_id"]
        )
        if (
            str(current_source["source_revision"]) != str(claimed["source_revision"])
            or normalize_catalog_code(current_source["code"], max_length=40)[1]
            != normalize_catalog_code(claimed["code"], max_length=40)[1]
        ):
            raise DownloadReplacementConflictError(
                "download failure changed before replacement submission"
            )
    except (DownloadReplacementError, DownloaderError, WebDownloadError):
        _release_download_replacement_submission(store, replacement_id, lease_token)
        raise

    submitted = False
    try:
        result = submit_qb_download(
            request,
            client=client,
            register_metadata=False,
        )
        submitted = True
    except DownloaderError:
        try:
            target_exists = _replacement_target_exists(client, info_hash)
        except DownloaderError as confirmation_error:
            raise DownloadReplacementConflictError(
                "replacement task outcome could not be confirmed; retry the same selection"
            ) from confirmation_error
        if not target_exists:
            _release_download_replacement_submission(store, replacement_id, lease_token)
            raise
        result = _replacement_download_result(request, info_hash)
    if submitted and not _replacement_target_exists(client, info_hash):
        raise DownloadReplacementConflictError(
            "replacement task was accepted but could not be confirmed; retry the same selection"
        )

    store.mark_target_created(replacement_id, lease_token)
    metadata_registered = enqueue_qb_metadata(info_hash, request_code)
    final = _finish_download_replacement_cleanup(
        store.get(replacement_id), lease_token=lease_token, client=client
    )
    result["replacement"] = _replacement_download_outcome(
        final,
        replayed=not submitted,
    )
    if not metadata_registered:
        result["metadata_warning"] = (
            "Automatic metadata registration failed; scan the media library "
            "after the download completes"
        )
    if str(final["status"]) != "completed":
        result["replacement_warning"] = (
            "The new task was created, but the old failed item could not be cleaned up"
        )
    return result


def _replacement_download_result(
    request: AddDownloadRequest, info_hash: str
) -> dict[str, object]:
    config = AppConfig.from_env().qbittorrent
    parsed = parse_magnet(request.magnet)
    return {
        "ok": True,
        "info_hash": info_hash,
        "display_name": request.name or parsed.display_name,
        "category": config.category,
        "save_path": request.save_path or config.save_path,
        "tags": request.tags or config.tags,
    }


def submit_web_replacement(
    manager: WebDownloadManager,
    *,
    code: object,
    idempotency_key: object,
    replacement_id: object,
    variant: object | None,
) -> dict[str, object]:
    clean_code, _normalized_key = normalize_web_download_code(code)
    clean_replacement_id = str(replacement_id or "").strip().lower()
    clean_key = str(idempotency_key or "").strip()
    if (
        not re.fullmatch(r"[0-9a-f]{32}", clean_replacement_id)
        or clean_key != clean_replacement_id
    ):
        raise DownloadReplacementError("download replacement identity is invalid")
    store = download_replacement_store()
    replacement = store.get(clean_replacement_id)
    if (
        str(replacement.get("recovery_mode") or "") != "web"
        or str(replacement.get("discovery_status") or "") != "available"
    ):
        raise DownloadReplacementConflictError(
            "download replacement has no confirmed Web source"
        )
    clean_variant = str(
        variant or replacement.get("web_variant") or "original"
    ).strip().lower()
    if clean_variant != str(replacement.get("web_variant") or clean_variant):
        raise DownloadReplacementConflictError(
            "selected Web source variant does not match discovery"
        )
    if str(replacement.get("web_status") or "") != "available":
        raise DownloadReplacementConflictError(
            "download replacement has no confirmed Web source"
        )
    target_id = clean_replacement_id
    claimed = store.begin_submission(
        clean_replacement_id,
        idempotency_key=clean_key,
        target_kind="web_job",
        target_id=target_id,
        code=clean_code,
    )
    phase = str(claimed.get("submission_phase"))
    if phase == "complete":
        result = manager.get(target_id)
        result["replacement"] = _replacement_download_outcome(
            claimed,
            replayed=True,
        )
        return result
    lease_token = str(claimed.get("lease_token") or "")
    if not lease_token:
        raise DownloadReplacementError("download replacement submission lease is invalid")
    if phase == "cleanup":
        try:
            result = manager.get(target_id)
        except WebDownloadNotFoundError:
            final = store.finish_cleanup(
                clean_replacement_id,
                removed=False,
                cleanup_error="replacement_target_missing",
                lease_token=lease_token,
            )
            raise DownloadReplacementConflictError(
                "replacement Web task could not be confirmed"
            ) from None
        final = _finish_download_replacement_cleanup(
            claimed,
            lease_token=lease_token,
        )
        result["replacement"] = _replacement_download_outcome(final, replayed=True)
        if str(final["status"]) != "completed":
            result["replacement_warning"] = (
                "The new Web task exists, but the old failed item could not be cleaned up"
            )
        return result
    try:
        current_source = download_replacement_source(
            claimed["source_kind"], claimed["source_id"]
        )
        if (
            str(current_source["source_revision"])
            != str(claimed["source_revision"])
            or normalize_catalog_code(current_source["code"], max_length=40)[1]
            != normalize_catalog_code(claimed["code"], max_length=40)[1]
        ):
            raise DownloadReplacementConflictError(
                "download failure changed before replacement submission"
            )
    except (DownloadReplacementError, DownloaderError, WebDownloadError):
        _release_download_replacement_submission(store, clean_replacement_id, lease_token)
        raise
    submitted = False
    try:
        result = manager.start(
            clean_code,
            clean_key,
            variant=clean_variant,
            job_id=target_id,
            replaces_job_id=(
                str(claimed["source_id"])
                if str(claimed["source_kind"]) == "web_job"
                else None
            ),
        )
        submitted = True
    except WebDownloadError:
        try:
            result = manager.get(target_id)
        except WebDownloadNotFoundError:
            _release_download_replacement_submission(store, clean_replacement_id, lease_token)
            raise
    store.mark_target_created(clean_replacement_id, lease_token)
    final = _finish_download_replacement_cleanup(
        store.get(clean_replacement_id),
        lease_token=lease_token,
    )
    result["replacement"] = _replacement_download_outcome(
        final,
        replayed=not submitted,
    )
    if str(final["status"]) != "completed":
        result["replacement_warning"] = (
            "The new Web task was created, but the old failed item could not be cleaned up"
        )
    return result


def _replacement_download_outcome(
    replacement: Mapping[str, object],
    *,
    replayed: bool,
) -> dict[str, object]:
    status = str(replacement.get("status") or "")
    if status not in {"completed", "cleanup_failed"}:
        raise DownloadReplacementError(
            "download replacement outcome is not terminal"
        )
    cleanup_error = replacement.get("cleanup_error")
    return {
        "replacement_id": str(replacement.get("replacement_id") or ""),
        "status": status,
        "old_failure_removed": status == "completed",
        "replayed": bool(replayed),
        "cleanup_error": (
            str(cleanup_error) if cleanup_error is not None else None
        ),
    }


def _replacement_target_exists(client: QbittorrentClient, info_hash: str) -> bool:
    snapshot = client.torrent_snapshot(info_hash)
    if snapshot is None:
        return False
    raw_category = getattr(getattr(client, "config", None), "category", None)
    category = (
        raw_category.strip()
        if isinstance(raw_category, str)
        else AppConfig.from_env().qbittorrent.category.strip()
    )
    if not category or str(snapshot.get("category") or "") != category:
        return False
    return True


def _release_download_replacement_submission(
    store: DownloadReplacementStore,
    replacement_id: str,
    lease_token: str,
) -> None:
    try:
        store.release_submission(replacement_id, lease_token)
    except DownloadReplacementError:
        pass


def _finish_download_replacement_cleanup(
    replacement: Mapping[str, object],
    *,
    lease_token: str,
    client: QbittorrentClient | None = None,
) -> dict[str, object]:
    store = download_replacement_store()
    try:
        _remove_download_replacement_source(
            replacement,
            client=client,
        )
    except (
        DownloadReplacementError,
        DownloaderError,
        WebDownloadError,
        OSError,
        sqlite3.Error,
    ) as exc:
        return store.finish_cleanup(
            replacement["replacement_id"],
            removed=False,
            cleanup_error=_replacement_cleanup_error_code(exc),
            lease_token=lease_token,
        )
    return store.finish_cleanup(
        replacement["replacement_id"],
        removed=True,
        lease_token=lease_token,
    )


def _remove_download_replacement_source(
    replacement: Mapping[str, object],
    *,
    client: QbittorrentClient | None = None,
) -> None:
    source_kind = str(replacement["source_kind"])
    source_id = str(replacement["source_id"])
    source_revision = str(replacement["source_revision"])
    if source_kind == "qb":
        qb_client = client or QbittorrentClient(AppConfig.from_env().qbittorrent)
        snapshot = qb_client.torrent_snapshot(source_id)
        if snapshot is not None:
            category = AppConfig.from_env().qbittorrent.category
            if (
                not _is_replaceable_qb_failure(snapshot, category)
                or _qb_failure_revision(snapshot) != source_revision
            ):
                raise DownloadReplacementConflictError(
                    "failed qBittorrent download changed before cleanup"
                )
            _result, metadata_removed = delete_qb_with_metadata(
                qb_client,
                (source_id,),
                delete_files=False,
            )
            if metadata_removed is None:
                raise DownloaderError("failed qBittorrent metadata could not be cleaned")
        elif not discard_incomplete_qb_metadata(source_id):
            raise DownloaderError("failed qBittorrent metadata could not be cleaned")
        return
    if source_kind == "web_job":
        try:
            web_download_manager().remove_failed_replacement(
                source_id,
                expected_updated_at=source_revision,
            )
        except WebDownloadNotFoundError:
            pass
        return
    if source_kind == "web_intent":
        try:
            web_download_batch_manager().remove_failed_replacement(
                source_id,
                expected_updated_at=source_revision,
            )
        except WebDownloadBatchNotFoundError:
            pass
        return
    raise DownloadReplacementError("download replacement source kind is invalid")


def dispose_download_replacement_candidates(
    store: DownloadReplacementStore,
    candidates: Sequence[Mapping[str, object]],
    *,
    disposition: str,
) -> dict[str, object]:
    archived = 0
    deleted = 0
    failed = 0
    failures: list[dict[str, object]] = []
    for candidate in candidates:
        claimed = store.begin_disposition(
            candidate["replacement_id"],
            disposition=disposition,
        )
        if claimed is None:
            continue
        lease_token = str(claimed.get("lease_token") or "")
        try:
            try:
                current = download_replacement_source(
                    claimed["source_kind"], claimed["source_id"]
                )
            except DownloadReplacementNotFoundError:
                current = None
            source_changed = current is not None and (
                str(current["source_revision"]) != str(claimed["source_revision"])
                or normalize_catalog_code(current["code"], max_length=40)[1]
                != normalize_catalog_code(claimed["code"], max_length=40)[1]
            )
            if current is not None and not source_changed:
                _remove_download_replacement_source(claimed)
            final = store.finish_disposition(
                claimed["replacement_id"],
                lease_token,
                removed=True,
            )
            if final.get("disposition") == "archive":
                archived += 1
            else:
                deleted += 1
        except (
            DownloadReplacementError,
            DownloaderError,
            WebDownloadError,
            OSError,
            sqlite3.Error,
        ) as exc:
            failed += 1
            try:
                store.finish_disposition(
                    claimed["replacement_id"],
                    lease_token,
                    removed=False,
                    error_code=_replacement_cleanup_error_code(exc),
                )
            except DownloadReplacementError:
                pass
            if len(failures) < 50:
                failures.append(
                    {
                        "replacement_id": str(candidate["replacement_id"]),
                        "code": str(candidate["code"]),
                        "error": _replacement_cleanup_error_code(exc),
                    }
                )
    return {
        "archived": archived,
        "deleted": deleted,
        "removed": archived + deleted,
        "failed": failed,
        "failures": failures,
        "truncated": failed > len(failures),
    }


def _replacement_cleanup_error_code(error: Exception) -> str:
    if isinstance(error, DownloadReplacementConflictError):
        return "source_changed"
    if isinstance(error, DownloaderError):
        return "qb_cleanup_failed"
    if isinstance(error, WebDownloadError):
        return "web_cleanup_failed"
    if isinstance(error, (OSError, sqlite3.Error)):
        return "cleanup_storage_unavailable"
    return "cleanup_failed"


def recover_download_replacement_cleanup(
    replacement: Mapping[str, object],
) -> dict[str, object]:
    if str(replacement.get("status") or "") not in {
        "replacement_created",
        "cleanup_failed",
    }:
        return dict(replacement)
    target_kind = replacement.get("target_kind")
    target_id = replacement.get("target_id")
    if not isinstance(target_kind, str) or not isinstance(target_id, str):
        return dict(replacement)
    store = download_replacement_store()
    try:
        claimed = store.begin_submission(
            replacement["replacement_id"],
            idempotency_key=replacement["idempotency_key"],
            target_kind=target_kind,
            target_id=target_id,
            code=replacement["code"],
        )
    except DownloadReplacementConflictError:
        return store.get(replacement["replacement_id"])
    if str(claimed.get("submission_phase")) == "complete":
        return claimed
    lease_token = str(claimed.get("lease_token") or "")
    if str(claimed.get("submission_phase")) != "cleanup" or not lease_token:
        return claimed
    cleanup_client: QbittorrentClient | None = None
    try:
        if target_kind == "qb":
            cleanup_client = QbittorrentClient(AppConfig.from_env().qbittorrent)
            target_exists = _replacement_target_exists(cleanup_client, target_id)
        elif target_kind == "web_job":
            web_download_manager().get(target_id)
            target_exists = True
        else:
            target_exists = False
    except (DownloaderError, WebDownloadError):
        target_exists = False
    if not target_exists:
        return store.finish_cleanup(
            replacement["replacement_id"],
            removed=False,
            cleanup_error="replacement_target_missing",
            lease_token=lease_token,
        )
    return _finish_download_replacement_cleanup(
        claimed, lease_token=lease_token, client=cleanup_client
    )
