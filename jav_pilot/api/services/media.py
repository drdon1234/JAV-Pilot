"""Media library, media metadata and metadata review manager lifecycle."""

from __future__ import annotations

import re
import secrets
import sqlite3

from ...config.settings import load_settings
from ...config.source_catalog import METADATA_PROFILES
from ...core.observability import emit_json_log
from ...library.errors import (
    MediaLibraryConflictError,
    MediaLibraryError,
    MediaLibraryUnavailableError,
)
from ...library.worker import MediaLibraryConfig, MediaLibraryManager
from ...media_metadata.images import MetadataImageError, select_metadata_artwork
from ...media_metadata.manager import (
    MediaMetadataConfig,
    MediaMetadataDisabledError,
    MediaMetadataManager,
    MediaMetadataUnavailableError,
    discover_optional_description,
)
from ...media_metadata.review.errors import (
    MediaMetadataReviewConflict,
    MediaMetadataReviewError,
    MediaMetadataReviewValidationError,
)
from ...media_metadata.review.images import ReviewImage, prepare_review_image
from ...media_metadata.review.manager import MediaMetadataReviewManager
from ...media_metadata.review.models import REFETCH_CONFLICT_ERROR_CODE
from ...media_metadata.sources import (
    MediaMetadataSourceError,
    resolve_media_metadata_snapshots,
)
from ...media_metadata.store import MediaMetadataStoreError
from ...web_download.config import WebDownloadConfig
from ...web_download.job_store import WebDownloadStore
from .. import state
from .history import require_operational_mode
from .notifications import ensure_notification_outbox_registered


def media_library_manager(
    config: MediaLibraryConfig | None = None,
) -> MediaLibraryManager:
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            MediaLibraryUnavailableError(
                "media library is unavailable in maintenance mode"
            )
        )
        if state.SERVER_STOPPING.is_set():
            raise MediaLibraryUnavailableError("server is shutting down")
        active_config = config or MediaLibraryConfig.from_env()
        if not active_config.enabled:
            raise MediaLibraryUnavailableError("media library index is disabled")
        with state.MEDIA_LIBRARY_LOCK:
            if state.MEDIA_LIBRARY is not None:
                return state.MEDIA_LIBRARY
            manager = MediaLibraryManager(
                active_config, on_changed=_auto_complete_new_library_media
            )
            manager.start()
            state.MEDIA_LIBRARY = manager
            return manager


def _auto_complete_new_library_media(_report: object) -> None:
    """Queue metadata for media that appeared in the library, e.g. by hand."""

    try:
        defaults = load_settings().get("workflow_defaults") or {}
        if isinstance(defaults, dict) and defaults.get("metadata_auto_complete") is False:
            return
        config = MediaMetadataConfig.from_env()
        if not config.enabled:
            return
        media_metadata_manager(config).scan(retry_existing=False)
    except Exception:  # noqa: BLE001 - indexing continues; the user can run 一键补全.
        emit_json_log(
            "media_metadata",
            "auto_complete_unavailable",
            level="warning",
            error_code="scan_failed",
        )


def synchronize_media_library(
    source: str,
    refresh_paths: tuple[str, ...] = (),
) -> bool:
    clean_source = (
        source
        if source in {"web_archive", "qb_organizer", "metadata_publish"}
        else "unknown"
    )
    manager: MediaLibraryManager | None = None
    try:
        manager = media_library_manager()
        if manager.status()["state"] == "initializing":
            manager.request_reconcile(refresh_paths)
            emit_json_log(
                "media_library",
                "synchronous_update_deferred",
                level="warning",
                source=clean_source,
                error_code="initializing",
                outcome="deferred",
            )
            return False
        report = manager.reconcile_now(refresh_paths=refresh_paths)
    except MediaLibraryConflictError:
        if manager is not None:
            manager.request_reconcile(refresh_paths)
        emit_json_log(
            "media_library",
            "synchronous_update_deferred",
            level="warning",
            source=clean_source,
            error_code="busy",
            outcome="deferred",
        )
        return False
    except (MediaLibraryError, OSError, sqlite3.Error):
        emit_json_log(
            "media_library",
            "synchronous_update_failed",
            level="warning",
            source=clean_source,
            error_code="unavailable",
            outcome="failed",
        )
        return False
    except Exception:
        emit_json_log(
            "media_library",
            "synchronous_update_failed",
            level="warning",
            source=clean_source,
            error_code="internal",
            outcome="failed",
        )
        return False
    emit_json_log(
        "media_library",
        "synchronous_update_completed",
        source=clean_source,
        changed=bool(report.changed),
        outcome="succeeded",
    )
    return True


def synchronize_qb_media_library() -> None:
    synchronize_media_library("qb_organizer")


def observe_web_archive(
    job: dict[str, object],
    metadata_manager: MediaMetadataManager | None,
) -> None:
    if metadata_manager is not None:
        try:
            metadata_manager.web_completed(job)
        except Exception:
            emit_json_log(
                "media_metadata",
                "web_registration_failed",
                level="warning",
                error_code="internal",
                outcome="failed",
            )
    synchronize_media_library("web_archive")


def shutdown_media_library_manager(*, timeout: float) -> bool:
    with state.MEDIA_LIBRARY_LOCK:
        manager = state.MEDIA_LIBRARY
    stopped = manager is None or manager.stop(timeout=timeout)
    if stopped:
        with state.MEDIA_LIBRARY_LOCK:
            if state.MEDIA_LIBRARY is manager:
                state.MEDIA_LIBRARY = None
    if not stopped:
        emit_json_log(
            "media_library",
            "shutdown_deadline_exceeded",
            level="warning",
            outcome="pending",
        )
    return bool(stopped)


def media_metadata_manager(
    config: MediaMetadataConfig | None = None,
    *,
    _register_notifications: bool = True,
) -> MediaMetadataManager:
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            MediaMetadataUnavailableError(
                "media metadata is unavailable in maintenance mode"
            )
        )
        with state.MEDIA_METADATA_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise MediaMetadataUnavailableError("server is shutting down")
            if state.MEDIA_METADATA is not None:
                manager = state.MEDIA_METADATA
            else:
                active_config = config or MediaMetadataConfig.from_env()
                if not active_config.enabled:
                    raise MediaMetadataDisabledError("media metadata is disabled")
                try:
                    state.MEDIA_METADATA = MediaMetadataManager(
                        active_config,
                        on_published=lambda relative_path: synchronize_media_library(
                            "metadata_publish", (relative_path,)
                        ),
                        on_archive_relocated=_synchronize_archive_reference,
                    )
                except (OSError, sqlite3.Error, MediaMetadataStoreError) as exc:
                    raise MediaMetadataUnavailableError(
                        "media metadata storage is unavailable"
                    ) from exc
                manager = state.MEDIA_METADATA
    if _register_notifications:
        ensure_notification_outbox_registered(
            manager.store.path,
            component="media_metadata",
        )
    return manager


def _synchronize_archive_reference(
    kind: str,
    download_key: str,
    old_relative_path: str,
    new_relative_path: str,
) -> None:
    if kind != "web":
        return
    config = WebDownloadConfig.from_env()
    WebDownloadStore(config.database_path).compare_and_swap_completed_output_path(
        download_key,
        old_relative_path,
        new_relative_path,
    )


def media_metadata_review_manager() -> MediaMetadataReviewManager:
    with state.OPERATIONAL_MANAGER_LOCK:
        require_operational_mode(
            MediaMetadataReviewConflict(
                "metadata review is unavailable in maintenance mode"
            )
        )
        with state.MEDIA_METADATA_REVIEW_LOCK:
            if state.SERVER_STOPPING.is_set():
                raise MediaMetadataReviewConflict("server is shutting down")
            if state.MEDIA_METADATA_REVIEW is not None:
                return state.MEDIA_METADATA_REVIEW
            config = MediaMetadataConfig.from_env()
            if not config.enabled:
                raise MediaMetadataReviewValidationError("media metadata is disabled")
            state.MEDIA_METADATA_REVIEW = MediaMetadataReviewManager(
                database_path=config.database_path,
                library_root=config.library_path,
                backup_root=config.backup_path / "review",
            )
            return state.MEDIA_METADATA_REVIEW


def review_cached_image(reference: object) -> ReviewImage | None:
    value = str(reference or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise MediaMetadataReviewValidationError(
            "metadata review image reference is invalid"
        )
    image = state.REVIEW_IMAGE_CACHE.get(value)
    if image is None:
        raise MediaMetadataReviewConflict("metadata review image reference expired")
    return image


def _cache_review_artwork(
    manager: MediaMetadataReviewManager,
    review_id: str,
    image: object,
    kind: str,
    *,
    expected_revision: int,
) -> tuple[str, dict[str, object]]:
    body = getattr(image, "body", None)
    profile = str(getattr(image, "parser_profile", "") or "")
    if not isinstance(body, bytes) or profile not in METADATA_PROFILES:
        raise MediaMetadataReviewValidationError(
            "metadata review image result is invalid"
        )
    normalized, details = prepare_review_image(
        ReviewImage(body=body, source_id=profile), kind
    )
    reference = secrets.token_hex(16)
    state.REVIEW_IMAGE_CACHE.set(reference, normalized)
    manager.capture_image_snapshot(
        review_id,
        kind=details["kind"],
        source_id=details["source_id"],
        sha256=details["sha256"],
        width=details["width"],
        height=details["height"],
        expected_revision=expected_revision,
    )
    return reference, details


def refetch_media_metadata_review(
    review_id: object,
    *,
    sources: object,
    fields: object,
    images: object,
    expected_revision: object,
) -> dict[str, object]:
    if (
        not isinstance(sources, (list, tuple))
        or not isinstance(fields, (list, tuple))
        or not isinstance(images, (list, tuple))
    ):
        raise MediaMetadataReviewValidationError(
            "metadata review refetch selection is invalid"
        )
    manager = media_metadata_review_manager()
    intent = manager.request_refetch(
        review_id,
        sources=sources,
        fields=fields,
        images=images,
        expected_revision=expected_revision,
    )
    intent = manager.claim_refetch_intent(intent["intent_id"])
    try:
        return _execute_media_metadata_review_refetch(manager, intent)
    except Exception as exc:
        try:
            manager.complete_refetch_intent(
                intent["intent_id"],
                status="failed",
                error_code=(
                    REFETCH_CONFLICT_ERROR_CODE
                    if isinstance(exc, MediaMetadataReviewConflict)
                    else "execution_failed"
                ),
            )
        except (MediaMetadataReviewError, OSError, sqlite3.Error):
            pass
        raise


def _execute_media_metadata_review_refetch(
    manager: MediaMetadataReviewManager,
    intent: dict[str, object],
) -> dict[str, object]:
    review = manager.get_review(intent["review_id"])
    current_revision = int(review["revision"])
    current_abandon_generation = int(review.get("abandon_generation", 0))
    if current_revision != int(
        intent["base_revision"]
    ) or current_abandon_generation != int(intent["base_abandon_generation"]):
        raise MediaMetadataReviewConflict(
            "metadata review changed before refetch execution"
        )
    requested_sources = {str(item) for item in intent["sources"]}
    requested_fields = {str(item) for item in intent["fields"]}
    requested_images = {str(item) for item in intent["images"]}
    satisfied_fields: set[str] = set()
    image_refs: dict[str, str] = {}
    image_details: dict[str, dict[str, object]] = {}
    public_sources: list[dict[str, object]] = []
    candidates: list[object] = []

    if requested_sources & METADATA_PROFILES:
        try:
            snapshots = resolve_media_metadata_snapshots(review["code"])
        except MediaMetadataSourceError:
            snapshots = ()
        for snapshot in snapshots:
            source = snapshot.parser_profile
            if source not in requested_sources:
                continue
            available = snapshot.review_fields()
            selected = {
                name: value
                for name, value in available.items()
                if name in requested_fields
            }
            if selected:
                manager.capture_source_snapshot(
                    review["review_id"],
                    source,
                    selected,
                    deduplicate=True,
                    expected_revision=current_revision,
                )
                review = manager.get_review(review["review_id"])
                current_revision = int(review["revision"])
                current_abandon_generation = int(review.get("abandon_generation", 0))
                satisfied_fields.update(selected)
            if requested_images:
                candidates.extend(snapshot.image_candidates)
            public_sources.append(snapshot.public_dict())

    if "missav" in requested_sources and "description" in requested_fields:
        description = discover_optional_description(str(review["code"]))
        if description:
            manager.capture_source_snapshot(
                review["review_id"],
                "missav",
                {"description": description},
                deduplicate=True,
                expected_revision=current_revision,
            )
            review = manager.get_review(review["review_id"])
            current_revision = int(review["revision"])
            current_abandon_generation = int(review.get("abandon_generation", 0))
            satisfied_fields.add("description")

    if requested_images and candidates:
        try:
            artwork = select_metadata_artwork(
                candidates,
                need_portrait="portrait" in requested_images,
                need_landscape="landscape" in requested_images,
            )
        except MetadataImageError:
            artwork = None
        if artwork is not None:
            for kind in ("portrait", "landscape"):
                selected_image = getattr(artwork, kind)
                if kind not in requested_images or selected_image is None:
                    continue
                reference, details = _cache_review_artwork(
                    manager,
                    str(review["review_id"]),
                    selected_image,
                    kind,
                    expected_revision=current_revision,
                )
                review = manager.get_review(review["review_id"])
                current_revision = int(review["revision"])
                current_abandon_generation = int(review.get("abandon_generation", 0))
                image_refs[kind] = reference
                image_details[kind] = details

    complete = requested_fields.issubset(
        satisfied_fields
    ) and requested_images.issubset(image_refs)
    intent = manager.complete_refetch_intent(
        intent["intent_id"],
        status="completed" if complete else "failed",
        error_code=None if complete else "source_not_found",
        expected_revision=current_revision,
        expected_abandon_generation=current_abandon_generation,
    )
    return {
        "intent": intent,
        "review": manager.get_review(review["review_id"]),
        "sources": public_sources,
        "image_refs": image_refs,
        "images": image_details,
    }


def shutdown_media_metadata_manager(*, timeout: float) -> bool:
    with state.MEDIA_METADATA_LOCK:
        manager = state.MEDIA_METADATA
    stopped = True
    if manager is not None:
        stopped = manager.shutdown(timeout=timeout)
        if not stopped:
            emit_json_log(
                "media_metadata",
                "shutdown_deadline_exceeded",
                level="warning",
                outcome="pending",
            )
    if stopped:
        with state.MEDIA_METADATA_LOCK:
            if state.MEDIA_METADATA is manager:
                state.MEDIA_METADATA = None
    with state.MEDIA_METADATA_REVIEW_LOCK:
        state.MEDIA_METADATA_REVIEW = None
    state.REVIEW_IMAGE_CACHE.clear()
    return bool(stopped)
