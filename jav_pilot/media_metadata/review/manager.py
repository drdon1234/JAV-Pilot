"""Coordinates metadata review sessions from opening to publication."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ...web_download.variant import web_download_variant_from_stem
from ..publish import (
    MAX_NFO_BYTES,
    MetadataPublishConflict,
    MetadataPublishError,
    _fsync_directory,
    _movie_asset_names,
    _open_publish_directory,
    _publish_no_replace,
    _regular_media_file,
    _regular_root,
    build_movie_nfo,
    inspect_movie_metadata_assets,
    safe_relative_media_path,
)
from .drafts import compute_draft_digest, draft_metadata, parse_movie_nfo
from .errors import (
    MediaMetadataReviewConflict,
    MediaMetadataReviewError,
    MediaMetadataReviewNotFound,
    MediaMetadataReviewValidationError,
)
from .fields import finite_positive, hex_id, reject_sensitive_text, timestamp
from .images import ReviewImage, prepare_image
from .journal import (
    JournalDiscardablePublishConflict,
    artifact_preview,
    artifact_state,
    backup_artifact,
    cleanup_artifact_temporaries,
    cross_process_publication_lock,
    load_publish_journals,
    persist_publish_journal,
    preflight_journal_artifacts,
    read_regular_artifact,
    reject_unrecoverable_windows_journals,
    remove_publish_journal,
    replace_regular_artifact,
    require_publication_matches_journal,
    require_secure_review_publication,
    rollback_journal_artifacts,
    same_artifact_state,
    sync_artifact_directories,
    verify_journal_original_state,
)
from .models import (
    DEFAULT_PREVIEW_SECONDS,
    JournalArtifact,
    LoadedJournal,
    PreviewArtifact,
    PublishJournal,
    PublishPreview,
)
from .store import MediaMetadataReviewStore

class MediaMetadataReviewManager:
    def __init__(
        self,
        *,
        database_path: Path | str,
        library_root: Path | str,
        backup_root: Path | str,
        store: MediaMetadataReviewStore | None = None,
        clock: Callable[[], float] = time.time,
        preview_seconds: float = DEFAULT_PREVIEW_SECONDS,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.library_root = Path(library_root)
        self.backup_root = Path(backup_root)
        if not self.library_root.is_absolute() or not self.backup_root.is_absolute():
            raise MediaMetadataReviewValidationError(
                "metadata review paths must be absolute"
            )
        self.store = store or MediaMetadataReviewStore(database_path, clock=clock)
        self._clock = clock
        self._preview_seconds = finite_positive(
            preview_seconds, "preview lifetime", maximum=3600.0
        )
        self._token_factory = token_factory
        self._fault_injector = fault_injector
        self._previews: dict[str, PublishPreview] = {}
        self._lock = threading.RLock()
        self.recovered_refetch_intents = self.store.recover_refetch_intents()
        if os.name == "nt":
            reject_unrecoverable_windows_journals(self.backup_root)
        else:
            with self._publication_guard():
                self._recover_publish_journals()

    def open_review(
        self, code: object, relative_media_path: object
    ) -> dict[str, object]:
        media, root = self._media_file(relative_media_path)
        review = self.store.create_review(code, media.relative_to(root).as_posix())
        plan = inspect_movie_metadata_assets(
            library_root=root,
            media_file=media,
        )
        nfo_name, _, _ = _movie_asset_names(media, root)
        nfo_target = media.parent / nfo_name
        nfo_status = str(plan.assets[nfo_name]["status"])
        snapshot_status = "missing" if nfo_status == "missing" else "unavailable"
        if nfo_status == "conflict":
            snapshot_status = "unsafe"
        elif nfo_status == "existing":
            try:
                with _open_publish_directory(media.parent, root) as directory_fd:
                    body, _ = read_regular_artifact(
                        nfo_target,
                        directory_fd=directory_fd,
                        max_bytes=MAX_NFO_BYTES,
                    )
                fields = parse_movie_nfo(body, str(review["code"]))
                self.store.capture_source_snapshot(
                    review["review_id"],
                    "nfo",
                    fields,
                    source_digest=hashlib.sha256(body).hexdigest(),
                    deduplicate=True,
                )
                snapshot_status = "captured"
            except (MediaMetadataReviewError, MetadataPublishError):
                snapshot_status = "unsafe"
        result = self.store.get_review(review["review_id"])
        result["local_assets"] = plan.assets
        result["nfo_snapshot_status"] = snapshot_status
        return result

    def get_review(self, review_id: object) -> dict[str, object]:
        return self.store.get_review(review_id)

    def capture_source_snapshot(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self.store.capture_source_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    def capture_image_snapshot(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self.store.capture_image_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    def update_draft(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self.store.update_draft(*args, **kwargs)  # type: ignore[arg-type]

    def abandon_review(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self.store.abandon_review(*args, **kwargs)  # type: ignore[arg-type]

    def request_refetch(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self.store.request_refetch(*args, **kwargs)  # type: ignore[arg-type]

    def claim_refetch_intent(self, intent_id: object) -> dict[str, object]:
        return self.store.claim_refetch_intent(intent_id)

    def complete_refetch_intent(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self.store.complete_refetch_intent(*args, **kwargs)  # type: ignore[arg-type]

    def get_refetch_intent(self, intent_id: object) -> dict[str, object]:
        return self.store.get_refetch_intent(intent_id)

    def list_publications(self, review_id: object) -> list[dict[str, object]]:
        return self.store.list_publications(review_id)

    def preview_publish(
        self,
        review_id: object,
        *,
        include_nfo: bool,
        portrait: ReviewImage | None = None,
        landscape: ReviewImage | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        with self._publication_guard():
            return self._preview_publish_locked(
                review_id,
                include_nfo=include_nfo,
                portrait=portrait,
                landscape=landscape,
                expected_revision=expected_revision,
            )

    def _preview_publish_locked(
        self,
        review_id: object,
        *,
        include_nfo: bool,
        portrait: ReviewImage | None = None,
        landscape: ReviewImage | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        clean_id = hex_id(review_id, "review id")
        if not include_nfo and portrait is None and landscape is None:
            raise MediaMetadataReviewValidationError(
                "metadata review publish selection is empty"
            )
        review = self.store.get_review(clean_id)
        revision = int(review["revision"])
        if expected_revision is not None and expected_revision != revision:
            raise MediaMetadataReviewConflict("metadata review revision changed")
        media, root = self._media_file(review["relative_media_path"])
        directory = media.parent
        nfo_name, portrait_name, landscape_names = _movie_asset_names(media, root)
        draft = draft_metadata(review)
        artifacts: list[tuple[str, Path, bytes, str, str]] = []
        if include_nfo:
            nfo_body = build_movie_nfo(
                draft,
                variant=web_download_variant_from_stem(media.name),
            )
            reject_sensitive_text(nfo_body.decode("utf-8"), "NFO")
            artifacts.append(
                (
                    (directory / nfo_name).relative_to(root).as_posix(),
                    directory / nfo_name,
                    nfo_body,
                    "draft",
                    "nfo",
                )
            )
        if portrait is not None:
            prepared = prepare_image(portrait, "portrait")
            artifacts.append(
                (
                    (directory / portrait_name).relative_to(root).as_posix(),
                    directory / portrait_name,
                    prepared.body,
                    prepared.source_id,
                    "portrait",
                )
            )
        if landscape is not None:
            prepared = prepare_image(landscape, "landscape")
            artifacts.extend(
                (
                    (directory / name).relative_to(root).as_posix(),
                    directory / name,
                    prepared.body,
                    prepared.source_id,
                    "landscape",
                )
                for name in landscape_names
            )
        preview_artifacts: list[PreviewArtifact] = []
        with _open_publish_directory(directory, root) as directory_fd:
            for relative_path, target, body, source_id, kind in artifacts:
                state = artifact_state(target, directory_fd=directory_fd)
                preview_artifacts.append(
                    PreviewArtifact(
                        relative_path=relative_path,
                        target=target,
                        body=body,
                        source_id=source_id,
                        kind=kind,
                        original=state,
                    )
                )
        now = timestamp(self._clock())
        token = hex_id(self._token_factory(), "preview token")
        preview = PublishPreview(
            preview_token=token,
            review_id=clean_id,
            revision=revision,
            draft_digest=compute_draft_digest(review),
            created_at=now,
            expires_at=now + self._preview_seconds,
            artifacts=tuple(preview_artifacts),
        )
        with self._lock:
            self._expire_previews(now)
            if token in self._previews:
                raise MediaMetadataReviewConflict(
                    "metadata review preview identifier collided"
                )
            while len(self._previews) >= 128:
                self._previews.pop(next(iter(self._previews)))
            self._previews[token] = preview
        return {
            "ok": True,
            "preview_token": token,
            "review_id": clean_id,
            "revision": revision,
            "expires_at": preview.expires_at,
            "artifacts": [artifact_preview(item) for item in preview.artifacts],
        }

    def publish(self, preview_token: object) -> dict[str, object]:
        with self._publication_guard():
            return self._publish_locked(preview_token)

    def _publish_locked(self, preview_token: object) -> dict[str, object]:
        token = hex_id(preview_token, "preview token")
        now = timestamp(self._clock())
        with self._lock:
            preview = self._previews.pop(token, None)
        if preview is None:
            raise MediaMetadataReviewConflict(
                "metadata review preview is missing or already consumed"
            )
        if now >= preview.expires_at:
            raise MediaMetadataReviewConflict("metadata review preview expired")
        review = self.store.get_review(preview.review_id)
        if (
            int(review["revision"]) != preview.revision
            or compute_draft_digest(review) != preview.draft_digest
        ):
            raise MediaMetadataReviewConflict("metadata review changed after preview")
        media, root = self._media_file(review["relative_media_path"])
        directory = media.parent
        expected_targets = {item.target for item in preview.artifacts}
        if any(target.parent != directory for target in expected_targets):
            raise MediaMetadataReviewConflict("metadata review target changed")

        publication_id = self.store.reserve_publication_id()
        journal = PublishJournal(
            publication_id=publication_id,
            review_id=preview.review_id,
            review_revision=preview.revision + 1,
            artifacts=tuple(
                JournalArtifact(
                    relative_path=artifact.relative_path,
                    action=artifact.action,
                    original_sha256=artifact.original.sha256,
                    proposed_sha256=hashlib.sha256(artifact.body).hexdigest(),
                )
                for artifact in preview.artifacts
            ),
        )
        journal_body: bytes | None = None
        publication: dict[str, object] | None = None
        journal_resolved = False
        completed = 0
        journal_requires_recovery = False
        try:
            with _open_publish_directory(directory, root) as directory_fd:
                for artifact in preview.artifacts:
                    current = artifact_state(
                        artifact.target,
                        directory_fd=directory_fd,
                    )
                    if not same_artifact_state(current, artifact.original):
                        raise MediaMetadataReviewConflict(
                            "metadata review artifact changed after preview"
                        )

                backups: dict[str, str] = {}
                for artifact in preview.artifacts:
                    if artifact.action != "replace":
                        continue
                    backups[artifact.relative_path] = backup_artifact(
                        backup_root=self.backup_root,
                        backup_run_id=publication_id,
                        relative_path=artifact.relative_path,
                        original=artifact.original,
                    )

                public_artifacts = [
                    {
                        "relative_path": artifact.relative_path,
                        "kind": artifact.kind,
                        "source_id": artifact.source_id,
                        "action": artifact.action,
                        "sha256": hashlib.sha256(artifact.body).hexdigest(),
                        "backup_path": backups.get(artifact.relative_path),
                    }
                    for artifact in preview.artifacts
                ]
                journal_body = persist_publish_journal(self.backup_root, journal)
                self._inject_fault("after_journal_persisted")

                for artifact in preview.artifacts:
                    if artifact.action == "replace":
                        assert artifact.original.body is not None
                        assert artifact.original.identity is not None
                        replace_regular_artifact(
                            artifact.target,
                            artifact.body,
                            artifact.original.body,
                            artifact.original.identity,
                            directory,
                            directory_fd=directory_fd,
                        )
                    elif artifact.action == "create":
                        status_value = _publish_no_replace(
                            artifact.target,
                            artifact.body,
                            directory,
                            directory_fd=directory_fd,
                        )
                        if status_value != "generated":
                            raise JournalDiscardablePublishConflict(
                                "metadata review target changed before creation"
                            )
                    if artifact.action != "unchanged":
                        completed += 1
                        journal_requires_recovery = True
                        self._inject_fault(f"after_artifact_{completed}")
                    published = artifact_state(
                        artifact.target,
                        directory_fd=directory_fd,
                    )
                    if published.body != artifact.body:
                        if artifact.action == "unchanged":
                            raise JournalDiscardablePublishConflict(
                                "metadata review unchanged artifact changed"
                            )
                        raise MediaMetadataReviewError(
                            "metadata review artifact verification failed"
                        )
                _fsync_directory(directory, descriptor=directory_fd)
                publication = self.store.record_publication(
                    preview.review_id,
                    expected_revision=preview.revision,
                    draft=review,
                    artifacts=public_artifacts,
                    publication_id=publication_id,
                )
                self._inject_fault("after_database_commit")
        except BaseException as exc:
            recovered_publication: dict[str, object] | None = None
            if journal_body is not None:
                if (
                    isinstance(exc, JournalDiscardablePublishConflict)
                    and not journal_requires_recovery
                ):
                    remove_publish_journal(
                        self.backup_root,
                        journal,
                        expected_body=journal_body,
                    )
                else:
                    try:
                        recovered_publication = _recover_publish_journal(
                            store=self.store,
                            library_root=self.library_root,
                            backup_root=self.backup_root,
                            loaded=LoadedJournal(journal=journal, body=journal_body),
                        )
                    except MediaMetadataReviewConflict as recovery_exc:
                        if isinstance(exc, MetadataPublishConflict):
                            raise MediaMetadataReviewConflict(
                                "metadata review artifact changed during publish"
                            ) from recovery_exc
                        raise
                journal_resolved = True
            if recovered_publication is not None and isinstance(exc, Exception):
                publication = recovered_publication
            else:
                if isinstance(exc, MediaMetadataReviewError):
                    raise
                if isinstance(exc, MetadataPublishConflict):
                    raise MediaMetadataReviewConflict(
                        "metadata review artifact changed during publish"
                    ) from exc
                if isinstance(exc, MetadataPublishError):
                    raise MediaMetadataReviewError(
                        "metadata review artifact could not be published"
                    ) from exc
                raise
        assert publication is not None
        assert journal_body is not None
        if not journal_resolved:
            try:
                remove_publish_journal(
                    self.backup_root,
                    journal,
                    expected_body=journal_body,
                )
            except MediaMetadataReviewError:
                # A committed publication is authoritative. Startup recovery verifies
                # every target before removing a journal that could not be cleaned now.
                pass
        with self._lock:
            stale = [
                preview_id
                for preview_id, item in self._previews.items()
                if item.review_id == preview.review_id
            ]
            for preview_id in stale:
                self._previews.pop(preview_id, None)
        return {"ok": True, **publication}

    def _inject_fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @contextmanager
    def _publication_guard(self) -> Iterator[None]:
        require_secure_review_publication()
        with self._lock:
            with cross_process_publication_lock(self.backup_root):
                yield

    def _recover_publish_journals(self) -> None:
        for loaded in load_publish_journals(self.backup_root):
            _recover_publish_journal(
                store=self.store,
                library_root=self.library_root,
                backup_root=self.backup_root,
                loaded=loaded,
            )

    def _media_file(self, relative_media_path: object) -> tuple[Path, Path]:
        try:
            relative = safe_relative_media_path(relative_media_path)
        except MetadataPublishError as exc:
            raise MediaMetadataReviewValidationError(str(exc)) from exc
        try:
            root = _regular_root(self.library_root)
        except MetadataPublishError as exc:
            raise MediaMetadataReviewError(str(exc)) from exc
        try:
            media = _regular_media_file(
                root.joinpath(*PurePosixPath(relative).parts),
                root,
            )
        except MetadataPublishError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                raise MediaMetadataReviewNotFound(str(exc)) from exc
            raise MediaMetadataReviewValidationError(str(exc)) from exc
        return media, root

    def _expire_previews(self, now: float) -> None:
        expired = [
            token
            for token, preview in self._previews.items()
            if now >= preview.expires_at
        ]
        for token in expired:
            self._previews.pop(token, None)


def _recover_publish_journal(
    *,
    store: MediaMetadataReviewStore,
    library_root: Path,
    backup_root: Path,
    loaded: LoadedJournal,
) -> dict[str, object] | None:
    journal = loaded.journal
    publication = store.find_publication(journal.publication_id)
    root, recoveries = preflight_journal_artifacts(
        journal=journal,
        library_root=library_root,
        backup_root=backup_root,
        committed=publication is not None,
    )
    if publication is not None:
        require_publication_matches_journal(publication, journal)
    else:
        rollback_journal_artifacts(recoveries, root)
        verify_journal_original_state(recoveries, root)
    sync_artifact_directories(recoveries, root)
    cleanup_artifact_temporaries(recoveries, root)
    remove_publish_journal(
        backup_root,
        journal,
        expected_body=loaded.body,
    )
    return publication
