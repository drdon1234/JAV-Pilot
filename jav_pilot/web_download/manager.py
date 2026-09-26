"""Web download queue manager: scheduling, workers, retries and archiving."""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Callable, Sequence

from .quality import validate_selected_height
from .storage import (
    StorageLease,
    StorageReservationError,
    StorageReservations,
    WebDownloadStorageReservations,
)
from .variant import DEFAULT_WEB_DOWNLOAD_VARIANT
from .archives import (
    archive_root_matches,
    archive_root_snapshot,
    archived_file_probe_in_root,
    reconcile_completed_archives,
    stable_archived_file_status,
    with_archive_statuses,
)
from .cleanup import (
    cleanup_job_artifacts,
    cleanup_orphan_browser_profiles,
    retained_staging_bytes,
)
from .config import WebDownloadConfig, normalize_idempotency_key
from .errors import (
    WebDownloadArchiveUnavailableError,
    WebDownloadConfigError,
    WebDownloadConflictError,
    WebDownloadDisabledError,
    WebDownloadDiskLowError,
    WebDownloadError,
    WebDownloadRunnerError,
)
from .job_store import WebDownloadStore
from .jobs import (
    ACTIVE_STATUSES,
    ARCHIVE_AVAILABLE,
    ARCHIVE_MISSING,
    AUTO_PROVIDER,
    AUTO_STORAGE_RESERVATIONS,
    CAPTURE_FAILURE_CODES,
    CHECKPOINT_METADATA_ALLOWANCE_BYTES,
    DISK_LOW_VOLUME_KEYS,
    MAX_CHECKPOINT_ENTRIES,
    PHASE_ORDER,
    QUEUED_STATUS,
    RETRY_WAIT_STATUS,
    TERMINAL_STATUSES,
    UNSET,
    WORKER_STATUSES,
    normalize_web_download_code,
    redact_worker_error,
    replacement_revision_timestamp,
    validate_relative_output_path,
    validate_requested_height,
    validate_web_download_variant,
)
from .runner import (
    MissavManifestProvider,
    SubprocessWebDownloadRunner,
    WebDownloadRunner,
)

LOGGER = logging.getLogger(__name__)


class _ManagerOwnership:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WebDownloadConfigError("web download manager lock path is unsafe")
        handle = None
        try:
            handle = path.open("a+b")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            raise WebDownloadConflictError(
                "another web download manager already owns this database"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class WebDownloadManager:
    def __init__(
        self,
        config: WebDownloadConfig | None = None,
        *,
        runner: WebDownloadRunner | None = None,
        manifest_provider: MissavManifestProvider | None = None,
        store: WebDownloadStore | None = None,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        on_completed: Callable[[dict[str, object]], None] | None = None,
        start_workers: bool = True,
        storage_reservations: StorageReservations | None | object = (
            AUTO_STORAGE_RESERVATIONS
        ),
    ) -> None:
        self.config = config or WebDownloadConfig.from_env()
        self.store = store or WebDownloadStore(self.config.database_path)
        self.runner = runner or SubprocessWebDownloadRunner(
            manifest_provider=manifest_provider
        )
        if storage_reservations is AUTO_STORAGE_RESERVATIONS:
            if isinstance(self.runner, SubprocessWebDownloadRunner):
                try:
                    self._storage_reservations: StorageReservations | None = (
                        WebDownloadStorageReservations(
                            self.config.staging_path,
                            self.config.library_path,
                            min_free_bytes=self.config.min_free_bytes,
                            initial_media_bytes=(self.config.initial_media_bytes),
                            on_low_space=self.store.record_disk_low,
                        )
                    )
                except StorageReservationError as exc:
                    raise WebDownloadConfigError(str(exc)) from exc
            else:
                self._storage_reservations = None
        else:
            self._storage_reservations = storage_reservations  # type: ignore[assignment]
        self._id_factory = id_factory
        self._on_completed = on_completed
        self._condition = threading.Condition(threading.RLock())
        self._active_cancels: dict[str, threading.Event] = {}
        self._archive_summary_cache: tuple[float, int, int] | None = None
        self._stopping = False
        self._workers_started = False
        self._workers: tuple[threading.Thread, ...] = ()
        self._ownership = _ManagerOwnership(
            self.store.path.with_name(f"{self.store.path.name}.manager.lock")
        )
        try:
            cleaned_profiles = cleanup_orphan_browser_profiles(
                Path(self.config.staging_path),
                self.store.browser_profile_cleanup_job_ids(),
            )
            if cleaned_profiles:
                LOGGER.info(
                    "removed %d orphaned MissAV browser profile(s)",
                    cleaned_profiles,
                )
            self.store.recover_interrupted()
            control = self.store.control(hard_limit=self.config.max_concurrency)
            self._configure_runner_bandwidth(int(control["bandwidth_limit"]))
            reconcile_completed_archives(self.store, Path(self.config.library_path))
            for recovered_job in self.store.artifact_cleanup_candidates():
                cleanup_job_artifacts(
                    self.config,
                    str(recovered_job["job_id"]),
                    str(recovered_job["code"]),
                    recovered_job.get("output_path"),
                    recovered_job.get("incumbent_output_path"),
                )
            self._workers = tuple(
                threading.Thread(
                    target=self._dispatch,
                    name=f"jav-web-download-{index + 1}",
                    daemon=True,
                )
                for index in range(self.config.max_concurrency)
            )
            if start_workers:
                self.start_workers()
        except BaseException:
            if not any(worker.is_alive() for worker in self._workers):
                self._ownership.release()
                close_bandwidth = getattr(self.runner, "close_bandwidth", None)
                if callable(close_bandwidth):
                    close_bandwidth()
            raise

    def start_workers(self) -> None:
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            if self._workers_started:
                return
            self._workers_started = True
            try:
                for worker in self._workers:
                    worker.start()
            except BaseException:
                self._stopping = True
                for cancel_event in tuple(self._active_cancels.values()):
                    cancel_event.set()
                self._condition.notify_all()
                raise

    def start(
        self,
        code: object,
        idempotency_key: str | None = None,
        requested_height: object = UNSET,
        variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
        *,
        job_id: str | None = None,
        replaces_job_id: str | None = None,
    ) -> dict[str, object]:
        if not self.config.enabled:
            raise WebDownloadDisabledError("web downloads are disabled")
        display_code, code_key = normalize_web_download_code(code)
        clean_variant = validate_web_download_variant(variant)
        clean_key = normalize_idempotency_key(idempotency_key)
        # Omitting the optional height is the immediate-submit path.  Keep an
        # explicitly supplied ``None`` invalid for callers that use this API
        # for strict quality selection, while the HTTP endpoint can omit the
        # field and enqueue a legacy capture immediately.
        clean_height = (
            None
            if requested_height is UNSET
            else validate_requested_height(requested_height)
        )
        # Immediate Web-download submissions do not perform a foreground
        # quality probe.  Without an explicit height the worker must capture
        # the site's default stream (legacy strategy); ``selected`` is only
        # valid when a concrete height was supplied by a quality-selection or
        # batch workflow.
        quality_strategy = "selected" if clean_height is not None else "legacy"
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            job = self.store.create_or_get(
                provider=AUTO_PROVIDER,
                code=display_code,
                code_key=code_key,
                variant=clean_variant,
                idempotency_key=clean_key,
                job_id=job_id or self._id_factory(),
                requested_height=clean_height,
                quality_strategy=quality_strategy,
                replaces_job_id=replaces_job_id,
            )
            self._condition.notify_all()
            return self._with_archive_status(job)

    def notify_queued_jobs(self) -> None:
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            self._condition.notify_all()

    def list(
        self,
        *,
        status_filter: str = "all",
        limit: int = 100,
        offset: int = 0,
        code: object | None = None,
        query: object | None = None,
    ) -> list[dict[str, object]]:
        code_key = normalize_web_download_code(code)[1] if code is not None else None
        query_key = _normalize_web_download_history_query(query)
        jobs = self.store.list(
            status_filter=status_filter,
            limit=limit,
            offset=offset,
            code_key=code_key,
            query_key=query_key,
        )
        return with_archive_statuses(jobs, Path(self.config.library_path))

    def count(
        self,
        *,
        status_filter: str = "all",
        code: object | None = None,
        query: object | None = None,
    ) -> int:
        code_key = normalize_web_download_code(code)[1] if code is not None else None
        return self.store.count(
            status_filter=status_filter,
            code_key=code_key,
            query_key=_normalize_web_download_history_query(query),
        )

    def summary(self) -> dict[str, int | float]:
        now = time.monotonic()
        summary = self.store.summary()
        with self._condition:
            cached = self._archive_summary_cache
            if cached is not None and now - cached[0] < 15.0:
                summary["completed"] = cached[1]
                summary["missing"] = cached[2]
                return summary
        completed = with_archive_statuses(
            self.store.completed_metadata_candidates(),
            Path(self.config.library_path),
        )
        available_count = sum(
            job.get("archive_status") == ARCHIVE_AVAILABLE for job in completed
        )
        missing_count = sum(
            job.get("archive_status") == ARCHIVE_MISSING for job in completed
        )
        summary["completed"] = available_count
        summary["missing"] = missing_count
        with self._condition:
            self._archive_summary_cache = (now, available_count, missing_count)
        return summary

    def get(self, job_id: str) -> dict[str, object]:
        return self._with_archive_status(self.store.get(job_id))

    def completed_metadata_candidates(self) -> list[dict[str, object]]:
        return self.store.completed_metadata_candidates()

    def action(self, job_id: str, action: str) -> dict[str, object]:
        clean_action = str(action or "").strip().lower()
        if clean_action == "cancel":
            return self.cancel(job_id)
        if clean_action == "pause":
            return self.pause(job_id)
        if clean_action == "resume":
            return self.resume(job_id)
        if clean_action == "retry":
            return self.retry(job_id)
        if clean_action == "restart":
            return self.restart(job_id)
        if clean_action == "remove":
            return self.remove(job_id)
        raise WebDownloadError("unsupported web download action")

    def control(self) -> dict[str, object]:
        return self.store.control(hard_limit=self.config.max_concurrency)

    def update_control(
        self,
        *,
        target_concurrency: object = UNSET,
        bandwidth_limit: object = UNSET,
        timezone: object = UNSET,
        schedule: object = UNSET,
    ) -> dict[str, object]:
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            control = self.store.update_control(
                hard_limit=self.config.max_concurrency,
                target_concurrency=target_concurrency,
                bandwidth_limit=bandwidth_limit,
                timezone=timezone,
                schedule=schedule,
            )
            self._configure_runner_bandwidth(int(control["bandwidth_limit"]))
            self._condition.notify_all()
            return control

    def pause(self, job_id: str) -> dict[str, object]:
        with self._condition:
            job = self.store.pause(job_id)
            if str(job["status"]) == "pausing":
                cancel_event = self._active_cancels.get(str(job_id))
                if cancel_event is not None:
                    cancel_event.set()
            self._condition.notify_all()
            return self._with_archive_status(job)

    def resume(self, job_id: str) -> dict[str, object]:
        if not self.config.enabled:
            raise WebDownloadDisabledError("web downloads are disabled")
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            if str(job_id) in self._active_cancels:
                raise WebDownloadConflictError("web download has not finished pausing")
            job = self.store.resume(job_id)
            self._condition.notify_all()
            return self._with_archive_status(job)

    def global_pause(self) -> dict[str, object]:
        with self._condition:
            control, pausing_ids = self.store.set_global_paused(
                True, hard_limit=self.config.max_concurrency
            )
            for job_id in pausing_ids:
                cancel_event = self._active_cancels.get(job_id)
                if cancel_event is not None:
                    cancel_event.set()
            self._condition.notify_all()
            return control

    def global_resume(self) -> dict[str, object]:
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            control, _pausing_ids = self.store.set_global_paused(
                False, hard_limit=self.config.max_concurrency
            )
            self._condition.notify_all()
            return control

    def set_priority(
        self,
        job_id: str,
        priority: object,
        *,
        expected_revision: object | None = None,
    ) -> dict[str, object]:
        with self._condition:
            job = self.store.set_priority(
                job_id,
                priority,
                expected_revision=expected_revision,
            )
            self._condition.notify_all()
            return self._with_archive_status(job)

    def reorder(
        self, job_ids: Sequence[str], *, expected_revision: object
    ) -> dict[str, object]:
        with self._condition:
            control = self.store.reorder(
                job_ids,
                expected_revision=expected_revision,
            )
            self._condition.notify_all()
            return control

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._condition:
            job = self.store.cancel(job_id)
            cancel_event = self._active_cancels.get(str(job_id))
            if cancel_event is not None:
                cancel_event.set()
            self._condition.notify_all()
            return self._with_archive_status(job)

    def retry(self, job_id: str) -> dict[str, object]:
        if not self.config.enabled:
            raise WebDownloadDisabledError("web downloads are disabled")
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            job = self.store.retry(job_id)
            self._condition.notify_all()
            return self._with_archive_status(job)

    def restart(self, job_id: str) -> dict[str, object]:
        """Discard retained artifacts and explicitly restart a terminal job."""

        if not self.config.enabled:
            raise WebDownloadDisabledError("web downloads are disabled")
        with self._condition:
            if self._stopping:
                raise WebDownloadError("web download manager is shutting down")
            job = self.store.get(job_id)
            if str(job["status"]) not in {"failed", "cancelled"}:
                raise WebDownloadConflictError("web download is not restartable")
            if not cleanup_job_artifacts(
                self.config,
                str(job["job_id"]),
                str(job["code"]),
                job.get("output_path"),
                job.get("incumbent_output_path"),
            ):
                raise WebDownloadConflictError(
                    "web download checkpoint could not be discarded"
                )
            restarted = self.store.retry(job_id, reset_checkpoint=True)
            self._condition.notify_all()
            return self._with_archive_status(restarted)

    def remove(self, job_id: str) -> dict[str, object]:
        with self._condition:
            job = self.store.get(job_id)
            if str(job["status"]) not in TERMINAL_STATUSES:
                raise WebDownloadConflictError("active web downloads cannot be removed")
            if not cleanup_job_artifacts(
                self.config,
                str(job["job_id"]),
                str(job["code"]),
                job.get("output_path"),
                job.get("incumbent_output_path"),
            ):
                raise WebDownloadConflictError(
                    "web download artifacts could not be cleaned; retry removal"
                )
            result = self.store.remove(job_id)
            self._condition.notify_all()
            return result

    def remove_failed_replacement(
        self,
        job_id: str,
        *,
        expected_updated_at: object,
    ) -> dict[str, object]:
        with self._condition:
            job = self.store.get(job_id)
            expected = replacement_revision_timestamp(
                expected_updated_at,
                "web download replacement revision",
            )
            if str(job["status"]) not in {"failed", "cancelled"} or float(job["updated_at"]) != expected:
                raise WebDownloadConflictError(
                    "failed web download changed before replacement cleanup"
                )
            if not cleanup_job_artifacts(
                self.config,
                str(job["job_id"]),
                str(job["code"]),
                job.get("output_path"),
                job.get("incumbent_output_path"),
            ):
                raise WebDownloadConflictError(
                    "web download artifacts could not be cleaned; retry replacement cleanup"
                )
            result = self.store.remove_failed_if_unchanged(
                job_id,
                expected_updated_at=expected,
            )
            self._condition.notify_all()
            return result

    def cleanup_missing(self) -> dict[str, object]:
        with self._condition:
            candidates: list[dict[str, str]] = []
            library_root = Path(self.config.library_path)
            root_snapshot = archive_root_snapshot(library_root)
            if root_snapshot is None:
                raise WebDownloadArchiveUnavailableError(
                    "archive storage could not be verified"
                )
            for job in self.store.completed_metadata_candidates():
                output_path = job.get("output_path")
                if (
                    archived_file_probe_in_root(root_snapshot[0], output_path)[0]
                    != ARCHIVE_MISSING
                ):
                    continue
                if not archive_root_matches(library_root, root_snapshot):
                    raise WebDownloadArchiveUnavailableError(
                        "archive storage changed during cleanup"
                    )
                candidates.append(
                    {
                        "job_id": str(job["job_id"]),
                        "output_path": str(output_path),
                    }
                )

            removed_ids = self.store.remove_completed_archives_if_missing(
                candidates,
                lambda output_path: stable_archived_file_status(
                    library_root,
                    root_snapshot,
                    output_path,
                ),
                lambda: archive_root_matches(library_root, root_snapshot),
            )
            self._condition.notify_all()
            return {"removed": len(removed_ids), "job_ids": removed_ids}

    def _with_archive_status(self, job: dict[str, object]) -> dict[str, object]:
        return with_archive_statuses([job], Path(self.config.library_path))[0]

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            self._stopping = True
            for cancel_event in tuple(self._active_cancels.values()):
                cancel_event.set()
            self._condition.notify_all()
        for worker in self._workers:
            if worker.ident is not None:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        stopped = not any(worker.is_alive() for worker in self._workers)
        if stopped:
            self._ownership.release()
            close_bandwidth = getattr(self.runner, "close_bandwidth", None)
            if callable(close_bandwidth):
                close_bandwidth()
        return stopped

    def _configure_runner_bandwidth(self, limit: int) -> None:
        configure = getattr(self.runner, "configure_bandwidth", None)
        if callable(configure):
            configure(limit)

    def _dispatch(self) -> None:
        while True:
            reservation = None
            with self._condition:
                if self._stopping:
                    return
                try:
                    has_runnable = self.config.enabled and self.store.has_runnable()
                except (OSError, sqlite3.Error):
                    self._condition.wait(timeout=1.0)
                    continue
                if not has_runnable:
                    self._condition.wait(timeout=1.0)
                    continue
                try:
                    admission_open = self.store.claim_admission_open(
                        hard_limit=self.config.max_concurrency
                    )
                except (OSError, sqlite3.Error, WebDownloadError):
                    self._condition.wait(timeout=1.0)
                    continue
                if not admission_open:
                    self._condition.wait(timeout=1.0)
                    continue
                try:
                    candidate = self.store.peek_next_runnable()
                except (OSError, sqlite3.Error):
                    self._condition.wait(timeout=1.0)
                    continue
                if candidate is None:
                    self._condition.wait(timeout=1.0)
                    continue
                candidate_id = str(candidate["job_id"])

            # Directory inspection can walk a large retained checkpoint.  It
            # must not hold the manager condition, which guards every queue
            # API and the other dispatch workers.
            if self._storage_reservations is not None:
                try:
                    retained_staging_bytes(
                        Path(self.config.staging_path),
                        candidate_id,
                        max_entries=MAX_CHECKPOINT_ENTRIES,
                        max_bytes=(
                            3 * self.config.max_file_bytes
                            + CHECKPOINT_METADATA_ALLOWANCE_BYTES
                        ),
                    )
                    reservation = self._storage_reservations.try_reserve()
                except WebDownloadRunnerError as exc:
                    with self._condition:
                        try:
                            rejected = self.store.claim_next_runnable(
                                candidate_id,
                                hard_limit=self.config.max_concurrency,
                            )
                            if rejected is not None:
                                self.store.update(
                                    candidate_id,
                                    status="failed",
                                    speed=0,
                                    eta=None,
                                    error=redact_worker_error(exc),
                                    failure_stage="worker",
                                    failure_code="checkpoint_invalid",
                                )
                        except (OSError, sqlite3.Error, WebDownloadError):
                            pass
                        self._condition.wait(timeout=1.0)
                    continue
                except StorageReservationError:
                    with self._condition:
                        self._condition.wait(timeout=1.0)
                    continue
                if reservation is None:
                    with self._condition:
                        self._condition.wait(timeout=1.0)
                    continue

            with self._condition:
                if self._stopping:
                    if reservation is not None:
                        reservation.release()
                    return
                try:
                    job = self.store.claim_next_runnable(
                        candidate_id,
                        hard_limit=self.config.max_concurrency,
                    )
                except (OSError, sqlite3.Error):
                    if reservation is not None:
                        reservation.release()
                    self._condition.wait(timeout=1.0)
                    continue
                except BaseException:
                    if reservation is not None:
                        reservation.release()
                    raise
                if job is None:
                    if reservation is not None:
                        reservation.release()
                    self._condition.wait(timeout=1.0)
                    continue
                job_id = str(job["job_id"])
                cancel_event = threading.Event()
                self._active_cancels[job_id] = cancel_event
            try:
                self._run_job(job, cancel_event, reservation)
            finally:
                with self._condition:
                    self._active_cancels.pop(job_id, None)
                    if reservation is not None:
                        reservation.release()
                    try:
                        current = self.store.get(job_id)
                        if str(current["status"]) == "pausing":
                            self.store.finish_pausing(job_id)
                        elif str(current["status"]) == "cancelling":
                            self.store.update(
                                job_id,
                                status="cancelled",
                                speed=0,
                                eta=None,
                                error=None,
                            )
                    except (OSError, sqlite3.Error, WebDownloadError):
                        pass
                    self._condition.notify_all()

    def _run_job(
        self,
        job: dict[str, object],
        cancel_event: threading.Event,
        reservation: StorageLease | None,
    ) -> None:
        job_id = str(job["job_id"])
        try:
            self.runner.run(
                job=job,
                config=self.config,
                on_event=lambda event: self._apply_event(
                    job_id,
                    event,
                    reservation,
                ),
                cancel_event=cancel_event,
            )
            with self._condition:
                current = self.store.get(job_id)
                if str(current["status"]) in ACTIVE_STATUSES:
                    if current["status"] == "cancelling":
                        pass
                    elif current["status"] == "pausing":
                        pass
                    elif current["status"] in {
                        QUEUED_STATUS,
                        RETRY_WAIT_STATUS,
                        "paused",
                    }:
                        pass
                    elif self._stopping:
                        if current["status"] in WORKER_STATUSES:
                            self.store.requeue_interrupted(job_id)
                    elif cancel_event.is_set():
                        self.store.update(
                            job_id,
                            status="cancelled",
                            speed=0,
                            eta=None,
                            error=None,
                        )
                    else:
                        self.store.update(
                            job_id,
                            status="failed",
                            speed=0,
                            eta=None,
                            error="Download worker stopped before completion",
                        )
        except Exception as exc:
            try:
                with self._condition:
                    current = self.store.get(job_id)
                    if str(current["status"]) not in TERMINAL_STATUSES:
                        if current["status"] == "cancelling":
                            pass
                        elif current["status"] == "pausing":
                            pass
                        elif current["status"] in {
                            QUEUED_STATUS,
                            RETRY_WAIT_STATUS,
                            "paused",
                        }:
                            pass
                        elif self._stopping:
                            if current["status"] in WORKER_STATUSES:
                                self.store.requeue_interrupted(job_id)
                        elif cancel_event.is_set():
                            self.store.update(
                                job_id,
                                status="cancelled",
                                speed=0,
                                eta=None,
                                error=None,
                            )
                        elif isinstance(exc, WebDownloadDiskLowError):
                            self.store.update(
                                job_id,
                                disk_low_volume_key=exc.volume_key,
                                status="failed",
                                speed=0,
                                eta=None,
                                error=redact_worker_error(exc),
                                failure_stage="worker",
                                failure_code="disk_low",
                                output_path=None,
                            )
                        else:
                            self.store.update(
                                job_id,
                                status="failed",
                                speed=0,
                                eta=None,
                                error=redact_worker_error(exc),
                            )
            except (OSError, sqlite3.Error, WebDownloadError):
                pass
        finally:
            try:
                current = self.store.get(job_id)
                if str(current["status"]) == "completed":
                    cleanup_job_artifacts(
                        self.config,
                        job_id,
                        str(job["code"]),
                        current.get("output_path"),
                        current.get("incumbent_output_path"),
                    )
            except (OSError, sqlite3.Error, WebDownloadError):
                pass

    def _apply_event(
        self,
        job_id: str,
        event: dict[str, object],
        reservation: StorageLease | None = None,
    ) -> None:
        completed_job: dict[str, object] | None = None
        with self._condition:
            current = self.store.get(job_id)
            event_type = (
                str(event.get("type") or event.get("event") or "").strip().lower()
            )
            checkpoint_reset = (
                event_type == "progress" and event.get("checkpoint_reset") is True
            )
            checkpoint_reconcile = (
                event_type == "progress" and event.get("checkpoint_reconcile") is True
            )
            checkpoint_convergence = checkpoint_reset or checkpoint_reconcile
            if (
                str(current["status"]) == "pausing"
                and event_type != "completed"
                and not checkpoint_convergence
            ):
                return
            if str(current["status"]) == "cancelling" and event_type == "cancelled":
                return
            if str(current["status"]) == RETRY_WAIT_STATUS:
                return
            if checkpoint_convergence:
                updated = self._apply_event_locked(job_id, event, current)
                self._resize_storage_reservation(event, reservation)
            else:
                self._resize_storage_reservation(event, reservation)
                updated = self._apply_event_locked(job_id, event, current)
            if event_type == "completed" and str(updated["status"]) == "completed":
                completed_job = updated
        if completed_job is not None and self._on_completed is not None:
            try:
                self._on_completed(dict(completed_job))
            except Exception:
                LOGGER.warning("web download completion callback failed")

    def _resize_storage_reservation(
        self,
        event: dict[str, object],
        reservation: StorageLease | None,
    ) -> None:
        if (
            str(event.get("type") or event.get("event") or "").strip().lower()
            != "progress"
        ):
            return
        downloaded_bytes = (
            _non_negative_int(event["downloaded_bytes"], "downloaded bytes")
            if "downloaded_bytes" in event
            else 0
        )
        raw_total = event.get("total_bytes")
        total_bytes = (
            _non_negative_int(raw_total, "total bytes") if raw_total is not None else 0
        )
        raw_estimate = event.get("storage_estimate_bytes")
        storage_estimate_bytes = (
            _non_negative_int(raw_estimate, "storage estimate bytes")
            if raw_estimate is not None
            else 0
        )
        if (
            downloaded_bytes > self.config.max_file_bytes
            or total_bytes > self.config.max_file_bytes
        ):
            raise WebDownloadRunnerError(
                "download worker media exceeds the configured size limit"
            )
        if total_bytes > 0:
            media_bytes = max(total_bytes, downloaded_bytes)
            exact = True
        elif storage_estimate_bytes > 0:
            media_bytes = max(
                min(storage_estimate_bytes, self.config.max_file_bytes),
                downloaded_bytes,
            )
            exact = False
        else:
            media_bytes = min(
                downloaded_bytes + self.config.initial_media_bytes,
                self.config.max_file_bytes,
            )
            exact = False
        if reservation is None:
            return
        try:
            resized = reservation.try_resize(
                media_bytes,
                downloaded_bytes,
                exact=exact,
            )
        except StorageReservationError as exc:
            raise WebDownloadDiskLowError(
                self._reservation_volume_key("staging")
            ) from exc
        if not resized:
            raise WebDownloadDiskLowError(self._reservation_volume_key("staging"))

    def _reservation_volume_key(self, volume_key: str) -> str:
        normalizer = getattr(self._storage_reservations, "notification_volume_key", None)
        normalized = str(normalizer(volume_key)) if callable(normalizer) else volume_key
        return normalized if normalized in DISK_LOW_VOLUME_KEYS else volume_key

    def _apply_event_locked(
        self,
        job_id: str,
        event: dict[str, object],
        current: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not isinstance(event, dict):
            raise WebDownloadRunnerError("download worker event is invalid")
        if current is None:
            current = self.store.get(job_id)
        current_status = str(current["status"])
        event_type = str(event.get("type") or event.get("event") or "").strip().lower()
        checkpoint_reconcile_event = (
            event_type == "progress" and event.get("checkpoint_reconcile") is True
        )
        checkpoint_reset_event = (
            event_type == "progress" and event.get("checkpoint_reset") is True
        )
        checkpoint_convergence_event = (
            checkpoint_reset_event or checkpoint_reconcile_event
        )
        if current_status in TERMINAL_STATUSES:
            return current
        if current_status == RETRY_WAIT_STATUS:
            return current
        if (
            self._stopping
            and current_status in WORKER_STATUSES
            and event_type in {"cancelled", "failed"}
        ):
            return self.store.requeue_interrupted(job_id)
        if (
            current_status == "pausing"
            and event_type != "completed"
            and not checkpoint_convergence_event
        ):
            return current
        if current_status == "cancelling" and event_type == "cancelled":
            return current
        if (
            current_status == "cancelling"
            and event_type
            not in {
                "completed",
                "cancelled",
            }
            and not checkpoint_convergence_event
        ):
            return current
        if event_type == "status":
            requested = str(event.get("status") or "").strip().lower()
            if requested not in WORKER_STATUSES:
                raise WebDownloadRunnerError("download worker status is invalid")
            if (
                current_status in PHASE_ORDER
                and PHASE_ORDER[requested] < PHASE_ORDER[current_status]
            ):
                raise WebDownloadRunnerError("download worker status moved backwards")
            fields = {"status": requested, **_progress_fields(event, current)}
            return self.store.update(job_id, **fields)
        if event_type == "source":
            provider = str(event.get("provider") or "").strip().lower()
            if provider not in {"missav", "jable", "supjav"}:
                raise WebDownloadRunnerError("download source provider is invalid")
            return self.store.update(job_id, resolved_provider=provider)
        if event_type == "progress":
            checkpoint_fields = _checkpoint_fields(
                event,
                max_checkpoint_bytes=self.config.max_file_bytes,
            )
            checkpoint_reset = bool(checkpoint_fields.pop("checkpoint_reset", False))
            checkpoint_reconcile = bool(
                checkpoint_fields.pop("checkpoint_reconcile", False)
            )
            if (checkpoint_reset or checkpoint_reconcile) and (
                "progress" not in event or "downloaded_bytes" not in event
            ):
                raise WebDownloadRunnerError(
                    "download worker checkpoint convergence is incomplete"
                )
            fields = {
                **_progress_fields(
                    event,
                    current,
                    allow_reset=(checkpoint_reset or checkpoint_reconcile),
                    preserve_total=checkpoint_reconcile,
                ),
                **checkpoint_fields,
            }
            if (checkpoint_reset or checkpoint_reconcile) and (
                int(fields["downloaded_bytes"]) != int(fields["checkpoint_bytes"])
            ):
                raise WebDownloadRunnerError(
                    "download worker checkpoint convergence is invalid"
                )
            requested = str(event.get("status") or "").strip().lower()
            if requested:
                if requested not in WORKER_STATUSES:
                    raise WebDownloadRunnerError("download worker status is invalid")
                if (
                    current_status in PHASE_ORDER
                    and PHASE_ORDER[requested] < PHASE_ORDER[current_status]
                ):
                    raise WebDownloadRunnerError(
                        "download worker status moved backwards"
                    )
                if current_status not in {"pausing", "cancelling"} or not (
                    checkpoint_reset or checkpoint_reconcile
                ):
                    fields["status"] = requested
            if not fields:
                raise WebDownloadRunnerError("download worker progress event is empty")
            return self.store.update(
                job_id,
                checkpoint_reset=checkpoint_reset,
                checkpoint_reconcile=checkpoint_reconcile,
                **fields,
            )
        if event_type == "quality":
            raw_height = event.get("selected_height")
            if isinstance(raw_height, bool) or not isinstance(raw_height, int):
                raise WebDownloadRunnerError("selected video quality is invalid")
            try:
                selected_height = validate_selected_height(raw_height)
            except ValueError as exc:
                raise WebDownloadRunnerError(
                    "selected video quality is invalid"
                ) from exc
            return self.store.update(job_id, selected_height=selected_height)
        if event_type == "completed":
            output_path = _validate_output_path(
                event.get("output_path"), library_root=self.config.library_path
            )
            verified_height = event.get("verified_height")
            publication_outcome = event.get("publication_outcome")
            return self.store.update(
                job_id,
                status="completed",
                progress=100.0,
                speed=0,
                eta=0,
                error=None,
                output_path=output_path,
                verified_height=verified_height,
                publication_outcome=publication_outcome,
                **_completion_byte_fields(event, current),
            )
        if event_type == "cancelled":
            return self.store.update(
                job_id,
                status="cancelled",
                speed=0,
                eta=None,
                error=None,
                output_path=None,
            )
        if event_type == "failed":
            error_code = event.get("error_code")
            if error_code == "media_transport_transient":
                checkpoint = _checkpoint_fields(
                    event,
                    max_checkpoint_bytes=self.config.max_file_bytes,
                )
                checkpoint_reset = bool(checkpoint.pop("checkpoint_reset", False))
                return self.store.defer_transient_failure(
                    job_id,
                    event.get("error") or "Media connection was interrupted",
                    checkpoint_bytes=(
                        int(checkpoint["checkpoint_bytes"]) if checkpoint else None
                    ),
                    checkpoint_fragments=(
                        int(checkpoint["checkpoint_fragments"]) if checkpoint else None
                    ),
                    max_checkpoint_bytes=self.config.max_file_bytes,
                    checkpoint_reset=checkpoint_reset,
                )
            if isinstance(error_code, str) and error_code in CAPTURE_FAILURE_CODES:
                if bool(event.get("retryable")):
                    return self.store.defer_transient_failure(
                        job_id,
                        event.get("error")
                        or "Web source discovery is temporarily unavailable",
                        checkpoint_bytes=None,
                        checkpoint_fragments=None,
                        max_checkpoint_bytes=self.config.max_file_bytes,
                        failure_stage="capture",
                        failure_code=error_code,
                    )
                else:
                    return self.store.update(
                        job_id,
                        status="failed",
                        speed=0,
                        eta=None,
                        error=redact_worker_error(
                            event.get("error") or "Web source discovery failed"
                        ),
                        failure_stage="capture",
                        failure_code=error_code,
                        output_path=None,
                    )
            disk_low_volume_key: str | None = None
            if error_code is not None:
                if error_code != "disk_low":
                    raise WebDownloadRunnerError(
                        "download worker error code is invalid"
                    )
                raw_volume_key = event.get("volume_key")
                if raw_volume_key not in {"staging", "library"}:
                    raise WebDownloadRunnerError(
                        "download worker disk volume is invalid"
                    )
                disk_low_volume_key = str(raw_volume_key)
                volume_normalizer = getattr(
                    getattr(self, "_storage_reservations", None),
                    "notification_volume_key",
                    None,
                )
                if callable(volume_normalizer):
                    disk_low_volume_key = str(volume_normalizer(disk_low_volume_key))
                if disk_low_volume_key not in DISK_LOW_VOLUME_KEYS:
                    raise WebDownloadRunnerError(
                        "download worker disk volume is invalid"
                    )
            return self.store.update(
                job_id,
                disk_low_volume_key=disk_low_volume_key,
                status="failed",
                speed=0,
                eta=None,
                error=redact_worker_error(event.get("error") or "Download worker failed"),
                failure_stage=(
                    "capture"
                    if isinstance(error_code, str) and error_code.startswith("missav_")
                    else "worker"
                ),
                failure_code=(
                    str(error_code) if error_code is not None else "download_failed"
                ),
                output_path=None,
            )
        raise WebDownloadRunnerError("download worker event type is invalid")


def _normalize_web_download_history_query(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value)).strip().upper()
    if not normalized:
        return None
    if (
        len(normalized) > 40
        or not normalized.isascii()
        or re.fullmatch(r"[A-Z0-9._ -]+", normalized) is None
    ):
        raise WebDownloadError("web download history query is invalid")
    query_key = re.sub(r"[^A-Z0-9]", "", normalized)
    if not query_key:
        raise WebDownloadError("web download history query is invalid")
    return query_key


def _progress_fields(
    event: dict[str, object],
    current: dict[str, object],
    *,
    allow_reset: bool = False,
    preserve_total: bool = False,
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if "progress" in event:
        progress = _finite_number(event["progress"], "progress")
        bounded_progress = min(progress, 99.9)
        fields["progress"] = (
            bounded_progress
            if allow_reset
            else max(float(current["progress"]), bounded_progress)
        )
    if "downloaded_bytes" in event:
        downloaded = _non_negative_int(event["downloaded_bytes"], "downloaded bytes")
        fields["downloaded_bytes"] = (
            downloaded
            if allow_reset
            else max(int(current["downloaded_bytes"]), downloaded)
        )
    if "total_bytes" in event:
        raw_total = event["total_bytes"]
        if raw_total is None and allow_reset and not preserve_total:
            fields["total_bytes"] = None
        elif raw_total is not None:
            total = _non_negative_int(raw_total, "total bytes")
            fields["total_bytes"] = max(int(current["total_bytes"] or 0), total)
    if "speed" in event:
        fields["speed"] = (
            0.0 if event["speed"] is None else _finite_number(event["speed"], "speed")
        )
    if "eta" in event:
        fields["eta"] = (
            None
            if event["eta"] is None
            else math.ceil(_finite_number(event["eta"], "eta"))
        )
    if allow_reset:
        if not preserve_total:
            fields.setdefault("total_bytes", None)
        fields.setdefault("speed", 0.0)
        fields.setdefault("eta", None)
    return fields


def _checkpoint_fields(
    event: dict[str, object],
    *,
    required: bool = False,
    max_checkpoint_bytes: int | None = None,
) -> dict[str, object]:
    names = {"checkpoint_bytes", "checkpoint_fragments"}
    present = names.intersection(event)
    reset_present = "checkpoint_reset" in event
    reconcile_present = "checkpoint_reconcile" in event
    if not present:
        if reset_present or reconcile_present:
            raise WebDownloadRunnerError(
                "download worker checkpoint convergence is invalid"
            )
        if required:
            raise WebDownloadRunnerError(
                "download worker checkpoint progress is missing"
            )
        return {}
    if present != names:
        raise WebDownloadRunnerError(
            "download worker checkpoint progress is incomplete"
        )
    checkpoint_reset = event.get("checkpoint_reset", False)
    if not isinstance(checkpoint_reset, bool):
        raise WebDownloadRunnerError("download worker checkpoint reset is invalid")
    checkpoint_reconcile = event.get("checkpoint_reconcile", False)
    if not isinstance(checkpoint_reconcile, bool):
        raise WebDownloadRunnerError(
            "download worker checkpoint reconciliation is invalid"
        )
    if checkpoint_reset and checkpoint_reconcile:
        raise WebDownloadRunnerError(
            "download worker checkpoint progress mode is invalid"
        )
    checkpoint_bytes = _non_negative_int(event["checkpoint_bytes"], "checkpoint bytes")
    checkpoint_fragments = _non_negative_int(
        event["checkpoint_fragments"], "checkpoint fragments"
    )
    if checkpoint_fragments > MAX_CHECKPOINT_ENTRIES or (
        max_checkpoint_bytes is not None and checkpoint_bytes > max_checkpoint_bytes
    ):
        raise WebDownloadRunnerError(
            "download worker checkpoint fragment count is invalid"
        )
    if "downloaded_bytes" in event:
        downloaded_bytes = _non_negative_int(
            event["downloaded_bytes"], "downloaded bytes"
        )
        if checkpoint_bytes > downloaded_bytes:
            raise WebDownloadRunnerError(
                "download worker checkpoint progress is invalid"
            )
    fields: dict[str, object] = {
        "checkpoint_bytes": checkpoint_bytes,
        "checkpoint_fragments": checkpoint_fragments,
    }
    if checkpoint_reset:
        fields["checkpoint_reset"] = True
    if checkpoint_reconcile:
        fields["checkpoint_reconcile"] = True
    return fields


def _completion_byte_fields(
    event: dict[str, object], current: dict[str, object]
) -> dict[str, object]:
    del current
    if "downloaded_bytes" not in event or event.get("total_bytes") is None:
        raise WebDownloadRunnerError(
            "download worker completed event is missing archived file size"
        )
    downloaded = _non_negative_int(event["downloaded_bytes"], "downloaded bytes")
    total = _non_negative_int(event["total_bytes"], "total bytes")
    if downloaded != total:
        raise WebDownloadRunnerError(
            "download worker completed event has inconsistent archived file size"
        )
    return {"downloaded_bytes": downloaded, "total_bytes": total}


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise WebDownloadRunnerError(f"download worker {name} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebDownloadRunnerError(f"download worker {name} is invalid") from exc
    if not math.isfinite(number) or number < 0:
        raise WebDownloadRunnerError(f"download worker {name} is invalid")
    return number


def _non_negative_int(value: object, name: str) -> int:
    number = _finite_number(value, name)
    if not number.is_integer():
        raise WebDownloadRunnerError(f"download worker {name} is invalid")
    return int(number)


def _validate_output_path(value: object, *, library_root: str) -> str:
    raw = str(value or "").strip()
    root = Path(library_root)
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            raw = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise WebDownloadRunnerError(
                "download worker output path is invalid"
            ) from exc
    return validate_relative_output_path(raw)
