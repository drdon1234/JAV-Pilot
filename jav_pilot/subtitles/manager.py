"""Background worker that finds, validates and writes Chinese subtitles."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..config.paths import default_database_path, default_library_path
from ..library.errors import MediaLibraryError
from ..library.filesystem import safe_relative_path
from ..library.media_probe import LocalMediaProbeSafetyError, probe_local_video_duration_ms
from ..library.models import PART_SUFFIX_RE
from .models import MAX_SUBTITLE_BYTES, PROVIDER_IDS, SCRIPTS, SubtitleCandidate
from .providers import MIN_INTERVAL_SECONDS, build_providers
from .providers.base import ProviderError, RequestThrottle, SubtitleProvider
from .scoring import MatchFacts, ScoredCandidate, rank
from .sidecars import subtitle_file_name, subtitle_sidecars
from .store import (
    SubtitleFileRecord,
    SubtitleJobNotFound,
    SubtitleStore,
    SubtitleStoreConflict,
    SubtitleStoreError,
)
from .text import SubtitleTextError, convert_script, encode_subtitle, is_chinese, parse_subtitle

LOGGER = logging.getLogger(__name__)
HARDSUB_VARIANT = "chinese_subtitle"
RETRY_DELAYS_SECONDS = (300.0, 1800.0, 7200.0)
TIMELINE_GRACE_MS = 60_000
REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_POLL_SECONDS = 2.0
MAX_BATCH_ITEMS = 500
_CANDIDATE_FIELDS = (
    "provider",
    "file_name",
    "download_url",
    "format",
    "declared_script",
    "duration_ms",
    "machine_translated",
)


class SubtitleError(RuntimeError):
    code = "subtitle_failed"


class SubtitleDisabledError(SubtitleError):
    code = "subtitle_disabled"


class SubtitleUnavailableError(SubtitleError):
    code = "subtitle_unavailable"


class SubtitleJobNotFoundError(SubtitleError):
    code = "subtitle_job_not_found"


class SubtitleJobBusyError(SubtitleError):
    code = "subtitle_job_busy"


class SubtitleNotPresentError(SubtitleError):
    code = "subtitle_not_present"


class SubtitleMediaMissingError(SubtitleError):
    code = "subtitle_media_missing"


class SubtitleMultipartError(SubtitleError):
    code = "subtitle_multipart_unsupported"


class SubtitleCandidateNotFoundError(SubtitleError):
    code = "subtitle_candidate_not_found"


class SubtitleCandidateInvalidError(SubtitleError):
    code = "subtitle_candidate_invalid"


class SubtitleFileConflictError(SubtitleError):
    code = "subtitle_file_conflict"


class SubtitleFileModifiedError(SubtitleError):
    code = "subtitle_file_modified"


class SubtitleProviderUnavailableError(SubtitleError):
    code = "subtitle_provider_unavailable"


class _CandidateRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SubtitleConfig:
    enabled: bool
    database_path: Path
    library_path: Path
    poll_seconds: float = DEFAULT_POLL_SECONDS

    @classmethod
    def from_env(cls) -> "SubtitleConfig":
        enabled = os.environ.get("JAV_PILOT_SUBTITLES_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        database_path = Path(
            os.environ.get("JAV_PILOT_SUBTITLES_DATABASE_PATH", "").strip()
            or default_database_path("subtitles.sqlite3")
        ).expanduser()
        library_path = Path(
            os.environ.get("JAV_PILOT_MEDIA_METADATA_LIBRARY_PATH", "").strip()
            or os.environ.get("JAV_PILOT_WEB_DOWNLOAD_LIBRARY_PATH", "").strip()
            or os.environ.get("JAV_PILOT_QB_APP_LIBRARY_PATH", "").strip()
            or default_library_path()
        ).expanduser()
        if not database_path.is_absolute() or not library_path.is_absolute():
            raise SubtitleUnavailableError("subtitle paths must be absolute")
        return cls(enabled=enabled, database_path=database_path, library_path=library_path)


@dataclass(frozen=True, slots=True)
class SubtitlePreferences:
    auto_fetch: bool = True
    script: str = "zh-CN"
    allow_machine_translated: bool = True

    @classmethod
    def from_settings(cls, settings: object) -> "SubtitlePreferences":
        defaults = settings.get("workflow_defaults") if isinstance(settings, dict) else None
        section = defaults.get("subtitles") if isinstance(defaults, dict) else None
        if not isinstance(section, dict):
            return cls()
        script = section.get("script")
        return cls(
            auto_fetch=section.get("auto_fetch") is not False,
            script=script if script in SCRIPTS else "zh-CN",
            allow_machine_translated=section.get("allow_machine_translated") is not False,
        )


ProvidersLoader = Callable[[], Sequence[SubtitleProvider]]
PreferencesLoader = Callable[[], SubtitlePreferences]
DurationProbe = Callable[[Path, Path], "int | None"]
WrittenObserver = Callable[[str], None]


class SubtitleManager:
    def __init__(
        self,
        config: SubtitleConfig | None = None,
        *,
        store: SubtitleStore | None = None,
        providers_loader: ProvidersLoader | None = None,
        preferences_loader: PreferencesLoader | None = None,
        duration_probe: DurationProbe | None = None,
        on_written: WrittenObserver | None = None,
        start_worker: bool = True,
    ) -> None:
        self.config = config or SubtitleConfig.from_env()
        self.store = store or SubtitleStore(self.config.database_path)
        self._throttles = {
            provider_id: RequestThrottle(MIN_INTERVAL_SECONDS[provider_id])
            for provider_id in PROVIDER_IDS
        }
        self._providers_loader = providers_loader or self._configured_providers
        self._preferences_loader = preferences_loader or _configured_preferences
        self._duration_probe = duration_probe or _probe_duration
        self._on_written = on_written
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.RLock()
        self._stopping = False
        self.store.recover_running()
        self._worker = threading.Thread(
            target=self._dispatch,
            name="jav-subtitles",
            daemon=True,
        )
        if start_worker:
            self._worker.start()

    # ---- public operations ----------------------------------------------

    def observe_published(
        self,
        relative_media_path: str,
        code: str,
        variant: str | None,
    ) -> dict[str, object] | None:
        """Queue a media file whose metadata was just published, if enabled."""

        if not self.config.enabled or not self._preferences_loader().auto_fetch:
            return None
        try:
            media = self._media(relative_media_path)
            media_stat = media.stat()
        except (SubtitleError, OSError):
            return None
        job = self.store.enqueue_auto(
            relative_media_path,
            code,
            variant,
            media_size=int(media_stat.st_size),
            media_mtime_ns=int(media_stat.st_mtime_ns),
        )
        if job is not None:
            self._wake()
        return job

    def request(
        self,
        relative_media_path: str,
        code: str,
        variant: str | None,
    ) -> dict[str, object]:
        self._require_enabled()
        media = self._media(relative_media_path)
        if PART_SUFFIX_RE.search(media.stem):
            raise SubtitleMultipartError("multi-part media is not supported")
        job = self.store.enqueue_request(relative_media_path, code, variant, origin="manual")
        if job is None:  # pragma: no cover - manual requests are always queued.
            raise SubtitleUnavailableError("subtitle request was not queued")
        self._wake()
        return job

    def request_batch(self, items: Sequence[tuple[str, str, str | None]]) -> int:
        self._require_enabled()
        queued = 0
        for relative_media_path, code, variant in list(items)[:MAX_BATCH_ITEMS]:
            try:
                media = self._media(relative_media_path)
            except SubtitleMediaMissingError:
                continue
            if PART_SUFFIX_RE.search(media.stem):
                continue
            job = self.store.enqueue_request(relative_media_path, code, variant, origin="batch")
            if job is not None and job["status"] == "queued":
                queued += 1
        if queued:
            self._wake()
        return queued

    def retry(self, job_id: object) -> dict[str, object]:
        job = self.get(job_id)
        return self.request(
            str(job["relative_media_path"]),
            str(job["code"]),
            job.get("variant"),  # type: ignore[arg-type]
        )

    def candidates(self, job_id: object) -> dict[str, object]:
        job = self.get(job_id)
        cached = self.store.load_candidates(job["job_id"])
        if cached is None:
            return {"searched_at": None, "candidates": []}
        searched_at, records = cached
        selected = job.get("selected_candidate_id")
        return {
            "searched_at": searched_at,
            "candidates": [_public_candidate(record, selected) for record in records],
        }

    def select(self, job_id: object, candidate_id: object) -> dict[str, object]:
        """Download one cached candidate now and make it the media's subtitle."""

        self._require_enabled()
        job = self.get(job_id)
        clean_id = str(job["job_id"])
        cached = self.store.load_candidates(clean_id)
        record = next(
            (item for item in (cached[1] if cached else []) if item.get("candidate_id") == candidate_id),
            None,
        )
        if record is None:
            raise SubtitleCandidateNotFoundError("subtitle candidate was not found")
        try:
            candidate = SubtitleCandidate.from_record({key: record.get(key) for key in _CANDIDATE_FIELDS})
        except ValueError as exc:
            raise SubtitleCandidateNotFoundError("subtitle candidate was not found") from exc
        if candidate.format is None:
            raise SubtitleCandidateInvalidError("format_unsupported")
        provider = next(
            (item for item in self._providers_loader() if item.provider_id == candidate.provider),
            None,
        )
        if provider is None:
            raise SubtitleProviderUnavailableError("subtitle source is disabled")
        try:
            previous = self.store.lease(clean_id)
        except SubtitleStoreConflict as exc:
            raise SubtitleJobBusyError("subtitle job is running") from exc
        try:
            root = self._root()
            media = self._media(str(previous["relative_media_path"]), root)
            try:
                raw = provider.fetch(candidate, timeout=REQUEST_TIMEOUT_SECONDS)
            except ProviderError as exc:
                raise SubtitleProviderUnavailableError("subtitle source is unavailable") from exc
            preferences = self._preferences_loader()
            try:
                written = self._write_candidate(
                    clean_id,
                    media,
                    candidate,
                    raw,
                    preferences.script,
                    self._duration_probe(media, root),
                )
            except _CandidateRejected as exc:
                raise SubtitleCandidateInvalidError(exc.reason) from exc
        except BaseException:
            self.store.release(clean_id, previous)
            raise
        updated = self.store.set_completed(
            clean_id,
            provider=candidate.provider,
            candidate_id=candidate.candidate_id,
            file=written,
        )
        self._notify_written(str(updated["relative_media_path"]))
        return updated

    def remove(self, job_id: object) -> dict[str, object]:
        self._require_enabled()
        job = self.get(job_id)
        clean_id = str(job["job_id"])
        if job["status"] == "running":
            raise SubtitleJobBusyError("subtitle job is running")
        if job["status"] != "completed":
            raise SubtitleNotPresentError("no subtitle was written for this media")
        with self._write_lock:
            record = self.store.file_record(clean_id)
            if record is not None:
                path = _join(self._root(), record.relative_path)
                if _exists(path):
                    if not _matches(path, record):
                        raise SubtitleFileModifiedError("subtitle file was modified")
                    path.unlink()
                    _fsync_directory(path.parent)
            try:
                updated = self.store.mark_removed(clean_id)
            except SubtitleStoreConflict as exc:
                raise SubtitleJobBusyError("subtitle job state changed") from exc
        self._notify_written(str(updated["relative_media_path"]))
        return updated

    def forget(self, job_id: object) -> None:
        job = self.get(job_id)
        try:
            self.store.forget(job["job_id"])
        except SubtitleStoreConflict as exc:
            raise SubtitleJobBusyError("subtitle job is running") from exc

    def get(self, job_id: object) -> dict[str, object]:
        try:
            return self.store.get(job_id)
        except SubtitleJobNotFound as exc:
            raise SubtitleJobNotFoundError("subtitle job was not found") from exc

    def list(self, **kwargs: object) -> list[dict[str, object]]:
        return self.store.list(**kwargs)  # type: ignore[arg-type]

    def count(self, **kwargs: object) -> int:
        return self.store.count(**kwargs)  # type: ignore[arg-type]

    def summary(self) -> dict[str, int]:
        return self.store.summary()

    # ---- worker ---------------------------------------------------------

    def process_once(self) -> bool:
        job = self.store.claim_ready()
        if job is None:
            return False
        try:
            self._process(job)
        except Exception:
            LOGGER.exception("subtitle job failed unexpectedly")
            try:
                self._retry_or_fail(job, "internal_error")
            except Exception:
                LOGGER.exception("subtitle job state could not be recorded")
        return True

    def shutdown(self, *, timeout: float = 30.0) -> bool:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._worker.ident is not None:
            self._worker.join(timeout=max(0.0, timeout))
        return not self._worker.is_alive()

    def _dispatch(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                worked = self.process_once()
            except (OSError, sqlite3.Error, SubtitleStoreError):
                worked = False
            except Exception:
                LOGGER.exception("unexpected subtitle worker failure")
                worked = False
            if not worked:
                with self._condition:
                    if self._stopping:
                        return
                    self._condition.wait(timeout=self.config.poll_seconds)

    def _wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _process(self, job: Mapping[str, object]) -> None:
        job_id = str(job["job_id"])
        origin = str(job["origin"])
        try:
            root = self._root()
            media = self._media(str(job["relative_media_path"]), root)
        except SubtitleUnavailableError:
            self._retry_or_fail(job, "library_unavailable")
            return
        except SubtitleMediaMissingError:
            self.store.set_outcome(job_id, "failed", "media_missing")
            return
        if PART_SUFFIX_RE.search(media.stem):
            self.store.set_outcome(job_id, "skipped", "multipart")
            return
        if origin != "manual" and job.get("variant") == HARDSUB_VARIANT:
            self.store.set_outcome(job_id, "skipped", "hardsub")
            return
        if origin != "manual" and self._has_foreign_subtitle(job_id, media):
            self.store.set_outcome(job_id, "existing", "external_subtitle")
            return
        providers = list(self._providers_loader())
        if not providers:
            self.store.set_outcome(job_id, "failed", "no_provider")
            return

        preferences = self._preferences_loader()
        duration = self._duration_probe(media, root)
        code = str(job["code"])
        found: list[SubtitleCandidate] = []
        failed_providers = 0
        for provider in providers:
            try:
                found.extend(provider.search(code, timeout=REQUEST_TIMEOUT_SECONDS))
            except ProviderError:
                failed_providers += 1
        ranked = rank(
            found,
            MatchFacts(code, preferences.script, duration, preferences.allow_machine_translated),
        )
        by_provider = {provider.provider_id: provider for provider in providers}
        outcomes: dict[str, str] = {}
        download_failed = False
        written: tuple[SubtitleCandidate, SubtitleFileRecord] | None = None
        try:
            for scored in ranked:
                if scored.rejected is not None:
                    continue
                candidate = scored.candidate
                try:
                    raw = by_provider[candidate.provider].fetch(
                        candidate, timeout=REQUEST_TIMEOUT_SECONDS
                    )
                except ProviderError:
                    download_failed = True
                    outcomes[candidate.candidate_id] = "download_failed"
                    continue
                try:
                    record = self._write_candidate(
                        job_id, media, candidate, raw, preferences.script, duration
                    )
                except _CandidateRejected as exc:
                    outcomes[candidate.candidate_id] = exc.reason
                    continue
                written = (candidate, record)
                break
        except (SubtitleFileConflictError, SubtitleFileModifiedError) as exc:
            self.store.save_candidates(job_id, _candidate_records(ranked, outcomes))
            reason = "file_conflict" if isinstance(exc, SubtitleFileConflictError) else "file_modified"
            self.store.set_outcome(job_id, "failed", reason)
            return
        self.store.save_candidates(job_id, _candidate_records(ranked, outcomes))
        if written is not None:
            candidate, record = written
            self.store.set_completed(
                job_id,
                provider=candidate.provider,
                candidate_id=candidate.candidate_id,
                file=record,
            )
            self._notify_written(str(job["relative_media_path"]))
            return
        if (not found and failed_providers) or download_failed:
            self._retry_or_fail(job, "provider_unavailable")
            return
        self.store.set_not_found(job_id, "no_candidates" if not found else "no_usable_candidate")

    def _retry_or_fail(self, job: Mapping[str, object], reason: str) -> None:
        job_id = str(job["job_id"])
        attempts = int(job.get("attempts") or 1)  # type: ignore[arg-type]
        if attempts > len(RETRY_DELAYS_SECONDS):
            self.store.set_outcome(job_id, "failed", reason)
            return
        self.store.set_retry(
            job_id,
            reason,
            self.store.now() + RETRY_DELAYS_SECONDS[max(0, attempts - 1)],
        )

    # ---- files ----------------------------------------------------------

    def _write_candidate(
        self,
        job_id: str,
        media: Path,
        candidate: SubtitleCandidate,
        raw: bytes,
        target_script: str,
        duration_ms: int | None,
    ) -> SubtitleFileRecord:
        try:
            parsed = parse_subtitle(raw)
        except SubtitleTextError as exc:
            raise _CandidateRejected(exc.reason) from exc
        if not is_chinese(parsed.dialogue):
            raise _CandidateRejected("not_chinese")
        if duration_ms is not None and parsed.last_end_ms > duration_ms + TIMELINE_GRACE_MS:
            raise _CandidateRejected("timeline_too_long")
        text, script = convert_script(parsed, target_script)
        return self._publish(
            job_id,
            media,
            encode_subtitle(text),
            script,
            parsed.format,
            candidate.provider,
        )

    def _publish(
        self,
        job_id: str,
        media: Path,
        body: bytes,
        script: str,
        subtitle_format: str,
        provider: str,
    ) -> SubtitleFileRecord:
        with self._write_lock:
            root = self._root()
            target = media.with_name(subtitle_file_name(media.stem, script, subtitle_format))
            target_relative = target.relative_to(root).as_posix()
            owned = self.store.file_record(job_id)
            owned_path = _join(root, owned.relative_path) if owned is not None else None
            if owned is not None and owned_path is not None and _exists(owned_path):
                if not _matches(owned_path, owned):
                    raise SubtitleFileModifiedError("subtitle file was modified")
            replace = owned is not None and owned.relative_path == target_relative and _exists(target)
            if not replace and _exists(target):
                raise SubtitleFileConflictError("subtitle target is occupied")
            if replace:
                temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.part")
                try:
                    _write_exclusive(temporary, body)
                    os.replace(temporary, target)
                finally:
                    if _exists(temporary):
                        temporary.unlink()
            else:
                try:
                    _write_exclusive(target, body)
                except FileExistsError as exc:
                    raise SubtitleFileConflictError("subtitle target is occupied") from exc
            _fsync_directory(target.parent)
            if owned_path is not None and owned_path != target and _exists(owned_path):
                owned_path.unlink()
                _fsync_directory(owned_path.parent)
            target_stat = target.stat()
            media_stat = media.stat()
            return SubtitleFileRecord(
                relative_path=target_relative,
                sha256=hashlib.sha256(body).hexdigest(),
                size=int(target_stat.st_size),
                mtime_ns=int(target_stat.st_mtime_ns),
                script=script,
                provider=provider,
                media_size=int(media_stat.st_size),
                media_mtime_ns=int(media_stat.st_mtime_ns),
            )

    def _has_foreign_subtitle(self, job_id: str, media: Path) -> bool:
        owned = self.store.file_record(job_id)
        owned_name = PurePosixPath(owned.relative_path).name if owned is not None else None
        try:
            names = os.listdir(media.parent)
        except OSError:
            return False
        return any(name != owned_name for name in subtitle_sidecars(media.stem, names))

    def _root(self) -> Path:
        try:
            root = self.config.library_path.resolve(strict=True)
        except OSError as exc:
            raise SubtitleUnavailableError("media library is unavailable") from exc
        if not root.is_dir():
            raise SubtitleUnavailableError("media library is unavailable")
        return root

    def _media(self, relative_media_path: object, root: Path | None = None) -> Path:
        active_root = root or self._root()
        try:
            clean = safe_relative_path(relative_media_path, allow_root=False)
        except MediaLibraryError as exc:
            raise SubtitleMediaMissingError("media path is invalid") from exc
        media = active_root.joinpath(*PurePosixPath(clean).parts)
        try:
            media_stat = media.lstat()
            resolved = media.resolve(strict=True)
        except OSError as exc:
            raise SubtitleMediaMissingError("media file is missing") from exc
        if (
            stat.S_ISLNK(media_stat.st_mode)
            or not stat.S_ISREG(media_stat.st_mode)
            or not resolved.is_relative_to(active_root)
        ):
            raise SubtitleMediaMissingError("media file is unsafe")
        return media

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise SubtitleDisabledError("subtitles are disabled")

    def _configured_providers(self) -> list[SubtitleProvider]:
        from ..config.settings import SettingsError, load_settings

        try:
            sites = load_settings().get("sites")
        except (OSError, SettingsError):
            LOGGER.warning("subtitle sources could not be loaded from settings")
            return []
        return build_providers(sites, self._throttles)

    def _notify_written(self, relative_media_path: str) -> None:
        if self._on_written is None:
            return
        try:
            self._on_written(relative_media_path)
        except Exception:
            LOGGER.warning("subtitle publication observer failed")


def _configured_preferences() -> SubtitlePreferences:
    from ..config.settings import SettingsError, load_settings

    try:
        return SubtitlePreferences.from_settings(load_settings())
    except (OSError, SettingsError):
        return SubtitlePreferences()


def _probe_duration(media: Path, root: Path) -> int | None:
    try:
        return probe_local_video_duration_ms(media, root)
    except (LocalMediaProbeSafetyError, OSError):
        return None


def _candidate_records(
    ranked: Sequence[ScoredCandidate],
    outcomes: Mapping[str, str],
) -> list[dict[str, object]]:
    return [
        {
            **scored.candidate.to_record(),
            "candidate_id": scored.candidate.candidate_id,
            "score": scored.score,
            "rejected": outcomes.get(scored.candidate.candidate_id, scored.rejected),
            "duration_delta_ms": scored.duration_delta_ms,
        }
        for scored in ranked
    ]


def _public_candidate(record: Mapping[str, object], selected: object) -> dict[str, object]:
    return {
        "candidate_id": record.get("candidate_id"),
        "provider": record.get("provider"),
        "file_name": record.get("file_name"),
        "format": record.get("format"),
        "declared_script": record.get("declared_script"),
        "duration_ms": record.get("duration_ms"),
        "duration_delta_ms": record.get("duration_delta_ms"),
        "machine_translated": bool(record.get("machine_translated")),
        "score": int(record.get("score") or 0),  # type: ignore[arg-type]
        "rejected": record.get("rejected"),
        "selected": selected is not None and record.get("candidate_id") == selected,
    }


def _join(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _matches(path: Path, record: SubtitleFileRecord) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return False
    if path_stat.st_size != record.size or path_stat.st_size > MAX_SUBTITLE_BYTES:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == record.sha256


def _write_exclusive(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as writer:
            writer.write(body)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
