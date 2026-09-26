"""Media metadata and metadata review endpoints."""

from __future__ import annotations

import base64
import binascii
import secrets
import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...media_metadata.manager import (
    MediaMetadataConfig,
    MediaMetadataDisabledError,
    MediaMetadataError,
    MediaMetadataUnavailableError,
)
from ...media_metadata.review.errors import (
    MediaMetadataReviewConflict,
    MediaMetadataReviewError,
    MediaMetadataReviewNotFound,
    MediaMetadataReviewValidationError,
)
from ...media_metadata.review.images import ReviewImage, prepare_review_image
from ...media_metadata.sources import MediaMetadataSourceError
from ...media_metadata.store import (
    MediaMetadataConflictError,
    MediaMetadataNotFoundError,
    MediaMetadataStoreError,
)
from .. import state
from ..base import BaseHandler
from ..request import query_params, single_param, strict_int_param
from ..services.media import (
    media_metadata_manager,
    media_metadata_review_manager,
    refetch_media_metadata_review,
    review_cached_image,
    synchronize_media_library,
)


class MediaMetadataRoutes(BaseHandler):
    def _handle_media_metadata(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            config = MediaMetadataConfig.from_env()
            limit = strict_int_param(params, "limit", 100)
            offset = strict_int_param(params, "offset", 0)
            status_filter = single_param(params, "filter") or "all"
            query = single_param(params, "q") or None
            if limit < 1 or limit > 500 or offset < 0 or offset > 10_000_000:
                raise QueryError("metadata pagination is invalid")
            if not config.enabled:
                self._send_json(
                    {
                        "ok": False,
                        "enabled": False,
                        "available": False,
                        "library_path": config.library_path.as_posix(),
                        "jobs": [],
                        "count": 0,
                        "offset": offset,
                        "limit": limit,
                        "has_more": False,
                        "summary": {
                            "total": 0,
                            "waiting": 0,
                            "running": 0,
                            "completed": 0,
                            "failed": 0,
                        },
                        "error": "Media metadata is disabled",
                    }
                )
                return
            manager = media_metadata_manager(config)
            jobs = manager.list(
                limit=limit,
                offset=offset,
                status_filter=status_filter,
                query=query,
            )
            count = manager.count(status_filter=status_filter, query=query)
            summary = manager.summary()
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error, MediaMetadataUnavailableError):
            self._send_media_metadata_unavailable()
            return
        except (MediaMetadataError, MediaMetadataStoreError) as exc:
            self._send_media_metadata_error(exc)
            return
        self._send_json(
            {
                "ok": True,
                "enabled": True,
                "available": True,
                "library_path": config.library_path.as_posix(),
                "jobs": jobs,
                "count": count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(jobs) < count,
                "summary": summary,
            }
        )

    def _handle_media_metadata_scan(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            code = payload.get("code")
            jobs = media_metadata_manager().scan(code if code is not None else None)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error, MediaMetadataUnavailableError):
            self._send_media_metadata_unavailable()
            return
        except (MediaMetadataError, MediaMetadataStoreError) as exc:
            self._send_media_metadata_error(exc)
            return
        self._send_json(
            {"ok": True, "queued": len(jobs), "jobs": jobs},
            HTTPStatus.ACCEPTED,
        )

    def _handle_media_metadata_migrate_titles(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            action = str(payload.get("action") or "").strip().lower()
            manager = media_metadata_manager()
            if action == "preview":
                code = payload.get("code")
                report = manager.preview_nfo_title_migration(
                    code if code is not None else None
                )
            elif action == "migrate":
                report = manager.migrate_nfo_titles(payload.get("preview_id"))
            else:
                raise ValueError("unsupported NFO migration action")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error, MediaMetadataUnavailableError):
            self._send_media_metadata_unavailable()
            return
        except (MediaMetadataError, MediaMetadataStoreError) as exc:
            self._send_media_metadata_error(exc)
            return
        self._send_json({"ok": True, **report})

    def _handle_media_metadata_action(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            job_id = str(payload.get("job_id") or "").strip()
            action = str(payload.get("action") or "").strip().lower()
            if action == "complete_all":
                if set(payload) != {"action"}:
                    raise ValueError("unsupported media metadata action")
                report = media_metadata_manager().complete_all()
                self._send_json({"ok": True, **report})
                return
            if not job_id:
                raise ValueError("job_id is required")
            if action != "retry":
                raise ValueError("unsupported media metadata action")
            job = media_metadata_manager().retry(job_id)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error, MediaMetadataUnavailableError):
            self._send_media_metadata_unavailable()
            return
        except (MediaMetadataError, MediaMetadataStoreError) as exc:
            self._send_media_metadata_error(exc)
            return
        self._send_json({"ok": True, "job": job})

    def _handle_media_metadata_review(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            review_id = single_param(params, "id").strip()
            if not review_id:
                raise ValueError("metadata review id is required")
            manager = media_metadata_review_manager()
            review = manager.get_review(review_id)
            publications = manager.list_publications(review_id)
        except (QueryError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MediaMetadataReviewError as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json({"ok": True, "review": review, "publications": publications})

    def _handle_media_metadata_review_open(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            review = media_metadata_review_manager().open_review(
                payload.get("code"), payload.get("relative_media_path")
            )
        except (ValueError, MediaMetadataReviewError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json({"ok": True, "review": review})

    def _handle_media_metadata_review_draft(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            review = media_metadata_review_manager().update_draft(
                payload.get("review_id"),
                manual_values=payload.get("manual_values"),
                source_choices=payload.get("source_choices"),
                locks=payload.get("locks"),
                clear_manual=payload.get("clear_manual") or (),
                expected_revision=payload.get("expected_revision"),
            )
        except (ValueError, MediaMetadataReviewError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json({"ok": True, "review": review})

    def _handle_media_metadata_review_abandon(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            review = media_metadata_review_manager().abandon_review(
                payload.get("review_id"),
                expected_revision=payload.get("expected_revision"),
            )
        except (ValueError, MediaMetadataReviewError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json({"ok": True, "review": review})

    def _handle_media_metadata_review_refetch(self) -> None:
        try:
            payload = self._read_json_body(32 * 1024)
            if set(payload) != {
                "review_id",
                "sources",
                "fields",
                "images",
                "expected_revision",
            }:
                raise MediaMetadataReviewValidationError(
                    "metadata review refetch request is invalid"
                )
            result = refetch_media_metadata_review(
                payload.get("review_id"),
                sources=payload.get("sources") or (),
                fields=payload.get("fields") or (),
                images=payload.get("images") or (),
                expected_revision=payload.get("expected_revision"),
            )
        except (ValueError, MediaMetadataReviewError, MediaMetadataSourceError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json({"ok": True, **result})

    def _handle_media_metadata_review_image(self) -> None:
        try:
            payload = self._read_json_body(17 * 1024 * 1024)
            if set(payload) != {
                "review_id",
                "kind",
                "body_base64",
                "expected_revision",
            }:
                raise MediaMetadataReviewValidationError(
                    "metadata review image request is invalid"
                )
            encoded = str(payload.get("body_base64") or "")
            try:
                body = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise MediaMetadataReviewValidationError(
                    "metadata review image encoding is invalid"
                ) from exc
            if len(body) > 12 * 1024 * 1024:
                raise MediaMetadataReviewValidationError(
                    "metadata review image is too large"
                )
            manager = media_metadata_review_manager()
            kind = payload.get("kind")
            image, details = prepare_review_image(
                ReviewImage(body=body, source_id="manual"), kind
            )
            image_ref = secrets.token_hex(16)
            manager.capture_image_snapshot(
                payload.get("review_id"),
                kind=details["kind"],
                source_id=details["source_id"],
                sha256=details["sha256"],
                width=details["width"],
                height=details["height"],
                expected_revision=payload.get("expected_revision"),
            )
            review = manager.get_review(payload.get("review_id"))
            state.REVIEW_IMAGE_CACHE.set(image_ref, image)
        except (ValueError, MediaMetadataReviewError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json(
            {
                "ok": True,
                "review": review,
                "image_ref": image_ref,
                "image": details,
            }
        )

    def _handle_media_metadata_review_preview(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            include_nfo = payload.get("include_nfo")
            if not isinstance(include_nfo, bool):
                raise MediaMetadataReviewValidationError(
                    "metadata review NFO selection is invalid"
                )
            portrait = review_cached_image(payload.get("portrait_ref"))
            landscape = review_cached_image(payload.get("landscape_ref"))
            preview = media_metadata_review_manager().preview_publish(
                payload.get("review_id"),
                include_nfo=include_nfo,
                portrait=portrait,
                landscape=landscape,
                expected_revision=payload.get("expected_revision"),
            )
        except (ValueError, MediaMetadataReviewError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json(preview)

    def _handle_media_metadata_review_publish(self) -> None:
        try:
            payload = self._read_json_body(4096)
            result = media_metadata_review_manager().publish(
                payload.get("preview_token")
            )
            artifacts = result.get("artifacts")
            refresh_paths = (
                tuple(
                    str(item.get("relative_path") or "")
                    for item in artifacts
                    if isinstance(item, dict) and item.get("relative_path")
                )
                if isinstance(artifacts, list)
                else ()
            )
            synchronize_media_library("metadata_publish", refresh_paths)
        except (ValueError, MediaMetadataReviewError) as exc:
            self._send_media_metadata_review_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_media_metadata_unavailable()
            return
        self._send_json({"ok": True, **result})

    def _send_media_metadata_review_error(self, error: Exception) -> None:
        if isinstance(error, MediaMetadataReviewNotFound):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(error, MediaMetadataReviewConflict):
            status = HTTPStatus.CONFLICT
        elif isinstance(
            error,
            (MediaMetadataReviewValidationError, ValueError),
        ):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
        elif isinstance(error, MediaMetadataSourceError):
            status = HTTPStatus.BAD_GATEWAY
        else:
            status = HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json({"ok": False, "error": str(error)}, status)

    def _send_media_metadata_error(
        self, error: MediaMetadataError | MediaMetadataStoreError
    ) -> None:
        if isinstance(error, MediaMetadataNotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(error, MediaMetadataConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(
            error, (MediaMetadataDisabledError, MediaMetadataUnavailableError)
        ):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_json({"ok": False, "error": str(error)}, status)

    def _send_media_metadata_unavailable(self) -> None:
        self._send_json(
            {"ok": False, "error": "Media metadata storage is unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
