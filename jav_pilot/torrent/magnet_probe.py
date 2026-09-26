from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from ..config.app_config import AppConfig
from .inputs import catalog_code_from_download_metadata
from .qbittorrent import (
    PROBE_DOWNLOAD_LIMIT_BYTES_PER_SECOND,
    PROBE_HASH_BATCH_SIZE,
    PROBE_MAX_DOWNLOADED_BYTES,
    PROBE_MAX_MAGNETS,
    PROBE_METADATA_TEXT_MAX_BYTES,
    PROBE_TAG,
    QbittorrentClient,
    resolve_probe_destination,
)
from .magnet import MagnetError, parse_magnet
from ..config.runtime_config import runtime_config_path
from ..core.storage import atomic_write_text


PROBE_JOURNAL_SCHEMA_VERSION = 1
PROBE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
INFO_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_PROBE_TIMEOUT_SECONDS = 300.0
DEFAULT_PROBE_POLL_SECONDS = 0.75
DEFAULT_MINIMUM_OBSERVATION_SECONDS = 3.0
DEFAULT_STALE_AGE_SECONDS = 300
PROBE_ADDED_ON_GRACE_SECONDS = 5
PROBE_ADDED_ON_WINDOW_SECONDS = 120
PROBE_PURPOSES = frozenset({"seed", "metadata"})
PROBE_METADATA_FILES_PER_TORRENT = 200
PROBE_METADATA_FILES_PER_JOB = 1000
PROBE_NAMESPACE_MAX_REQUESTS = 4
PROBE_MIN_HTTP_TIMEOUT_SECONDS = 0.5


class MagnetProbeError(RuntimeError):
    pass


class MagnetProbeBusyError(MagnetProbeError):
    pass


class MagnetProbeConflictError(MagnetProbeError):
    pass


class MagnetProbeNotFoundError(MagnetProbeError):
    pass


@dataclass(frozen=True)
class ProbeMagnet:
    uri: str
    info_hash: str


@dataclass(frozen=True)
class ProbeJournal:
    probe_id: str
    created_at: int
    category: str
    tag: str
    save_path: str
    hashes: tuple[str, ...]

    @property
    def added_after(self) -> int:
        return self.created_at - PROBE_ADDED_ON_GRACE_SECONDS

    @property
    def added_before(self) -> int:
        return self.created_at + PROBE_ADDED_ON_WINDOW_SECONDS

    def to_dict(self) -> dict[str, object]:
        _validate_journal(self)
        return {
            "schema_version": PROBE_JOURNAL_SCHEMA_VERSION,
            "probe_id": self.probe_id,
            "created_at": self.created_at,
            "category": self.category,
            "tag": self.tag,
            "save_path": self.save_path,
            "hashes": list(self.hashes),
        }

    @classmethod
    def from_dict(cls, payload: object) -> "ProbeJournal":
        if not isinstance(payload, dict):
            raise MagnetProbeError("probe journal must be an object")
        expected_keys = {
            "schema_version",
            "probe_id",
            "created_at",
            "category",
            "tag",
            "save_path",
            "hashes",
        }
        if (
            set(payload) != expected_keys
            or payload.get("schema_version") != PROBE_JOURNAL_SCHEMA_VERSION
        ):
            raise MagnetProbeError("unsupported or malformed probe journal")
        raw_hashes = payload.get("hashes")
        if not isinstance(raw_hashes, list) or any(
            not isinstance(value, str) for value in raw_hashes
        ):
            raise MagnetProbeError("probe journal hashes are invalid")
        try:
            created_at = int(payload.get("created_at"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise MagnetProbeError("probe journal timestamp is invalid") from exc
        journal = cls(
            probe_id=str(payload.get("probe_id") or ""),
            created_at=created_at,
            category=str(payload.get("category") or ""),
            tag=str(payload.get("tag") or ""),
            save_path=str(payload.get("save_path") or ""),
            hashes=tuple(raw_hashes),
        )
        _validate_journal(journal)
        return journal


class ProbeJournalStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or runtime_config_path().parent / "qb-probes"

    def path_for(self, probe_id: str) -> Path:
        if not PROBE_ID_RE.fullmatch(str(probe_id)):
            raise MagnetProbeError("invalid probe id")
        return self.root / f"{probe_id}.json"

    def write(self, journal: ProbeJournal) -> Path:
        payload = journal.to_dict()
        path = self.path_for(journal.probe_id)
        if path.exists():
            raise MagnetProbeError("probe journal already exists")
        raw = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        atomic_write_text(path, raw)
        return path

    def read(self, path: Path) -> ProbeJournal:
        try:
            if path.stat().st_size > 64 * 1024:
                raise MagnetProbeError("probe journal is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except MagnetProbeError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise MagnetProbeError("cannot read probe journal") from exc
        journal = ProbeJournal.from_dict(payload)
        if path.name != f"{journal.probe_id}.json":
            raise MagnetProbeError("probe journal filename does not match its id")
        return journal

    def paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.glob("*.json"))

    def remove(self, probe_id: str) -> None:
        try:
            self.path_for(probe_id).unlink(missing_ok=True)
        except OSError as exc:
            raise MagnetProbeError("cannot remove probe journal") from exc


def normalize_probe_magnets(
    values: list[str] | tuple[str, ...],
) -> tuple[ProbeMagnet, ...]:
    if (
        not isinstance(values, (list, tuple))
        or not values
        or len(values) > PROBE_MAX_MAGNETS
    ):
        raise MagnetProbeError(f"provide between 1 and {PROBE_MAX_MAGNETS} magnets")
    magnets: list[ProbeMagnet] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise MagnetProbeError("each probe magnet must be a string")
        uri = value.strip()
        if "\r" in uri or "\n" in uri:
            raise MagnetProbeError("probe magnet cannot contain line breaks")
        try:
            parsed = parse_magnet(uri)
        except MagnetError as exc:
            raise MagnetProbeError(str(exc)) from exc
        if parsed.info_hash not in seen:
            seen.add(parsed.info_hash)
            magnets.append(ProbeMagnet(uri=uri, info_hash=parsed.info_hash))
    if not magnets:
        raise MagnetProbeError("at least one unique probe magnet is required")
    return tuple(magnets)


def _probe_purpose(value: object) -> str:
    purpose = str(value or "").strip().lower()
    if purpose not in PROBE_PURPOSES:
        raise MagnetProbeError("probe purpose must be seed or metadata")
    return purpose


def _normalize_overlap_hashes(
    values: list[str] | tuple[str, ...],
) -> frozenset[str]:
    if (
        not isinstance(values, (list, tuple))
        or not values
        or len(values) > PROBE_MAX_MAGNETS
    ):
        raise MagnetProbeError(
            f"provide between 1 and {PROBE_MAX_MAGNETS} torrent hashes"
        )
    hashes: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise MagnetProbeError("invalid torrent hash")
        info_hash = value.strip().lower()
        if not INFO_HASH_RE.fullmatch(info_hash):
            raise MagnetProbeError("invalid torrent hash")
        hashes.add(info_hash)
    return frozenset(hashes)


class MagnetProbeService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], QbittorrentClient] | None = None,
        journal_store: ProbeJournalStore | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_PROBE_POLL_SECONDS,
        minimum_observation_seconds: float = DEFAULT_MINIMUM_OBSERVATION_SECONDS,
    ) -> None:
        self._client_factory = client_factory or (
            lambda: QbittorrentClient(AppConfig.from_env().qbittorrent)
        )
        self.journal_store = journal_store or ProbeJournalStore()
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep
        self.timeout_seconds = max(0.0, min(float(timeout_seconds), 900.0))
        self.poll_interval_seconds = max(0.05, min(float(poll_interval_seconds), 5.0))
        self.minimum_observation_seconds = max(
            0.0,
            min(float(minimum_observation_seconds), self.timeout_seconds),
        )

    def run(
        self,
        magnets: list[str] | tuple[str, ...],
        *,
        purpose: str = "seed",
        probe_id: str | None = None,
        cancel_event: threading.Event | None = None,
        on_update: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        parsed = normalize_probe_magnets(magnets)
        clean_purpose = _probe_purpose(purpose)
        clean_probe_id = probe_id or uuid.uuid4().hex
        if not PROBE_ID_RE.fullmatch(clean_probe_id):
            raise MagnetProbeError("invalid probe id")
        cancel = cancel_event or threading.Event()
        started_monotonic = self._monotonic()
        metadata_deadline = (
            started_monotonic + self.timeout_seconds
            if clean_purpose == "metadata"
            else None
        )
        created_at = max(1, int(self._wall_clock()))
        result: dict[str, object] = {
            "ok": True,
            "probe_id": clean_probe_id,
            "purpose": clean_purpose,
            "status": "running",
            "total": len(parsed),
            "progress": {
                "resolved": 0,
                "total": len(parsed),
                "elapsed_ms": 0,
                "timeout_ms": int(self.timeout_seconds * 1000),
            },
            "items": [],
            "cleanup": None,
        }
        self._emit(on_update, result)

        client: QbittorrentClient | None = None
        journal: ProbeJournal | None = None
        journal_written = False
        preexisting: dict[str, dict[str, object]] = {}
        latest: dict[str, dict[str, object]] = {}
        ever_observed: set[str] = set()
        resumed_after_metadata: set[str] = set()
        candidates: tuple[ProbeMagnet, ...] = ()
        contents: dict[str, dict[str, object]] = {}
        timed_out = False
        capabilities: dict[str, object] = {}
        add_outcome = "not_required"

        def current_items(
            *, final: bool, timed_out_now: bool = False
        ) -> list[dict[str, object]]:
            return _probe_items(
                parsed,
                preexisting,
                latest,
                journal,
                final=final,
                timed_out=timed_out_now,
                purpose=clean_purpose,
                contents=contents,
            )

        def complete_metadata_timeout() -> dict[str, object]:
            nonlocal timed_out
            timed_out = True
            _mark_probe_contents_unavailable(parsed, contents)
            result["status"] = "complete"
            result["items"] = current_items(final=True, timed_out_now=True)
            result["progress"] = _progress(
                result["items"],
                len(parsed),
                started_monotonic,
                self._monotonic(),
                self.timeout_seconds,
            )
            if not journal_written:
                result["cleanup"] = {
                    "status": "not_required",
                    "deleted": 0,
                    "skipped": 0,
                }
            return copy.deepcopy(result)

        def complete_cancelled_without_add() -> dict[str, object]:
            result["status"] = "cancelled"
            result["items"] = current_items(final=True)
            result["progress"] = _progress(
                result["items"],
                len(parsed),
                started_monotonic,
                self._monotonic(),
                self.timeout_seconds,
            )
            result["cleanup"] = {
                "status": "not_required",
                "deleted": 0,
                "skipped": 0,
            }
            return copy.deepcopy(result)

        def remove_unsent_journal() -> None:
            nonlocal journal, journal_written
            assert journal is not None
            self.journal_store.remove(journal.probe_id)
            journal_written = False
            journal = None

        try:
            client = self._client_factory()
            capabilities = client.require_probe_support()
            all_hashes = [item.info_hash for item in parsed]
            preexisting = client.probe_torrent_snapshots(all_hashes)
            _merge_probe_snapshots(latest, preexisting)
            candidates = tuple(
                item for item in parsed if item.info_hash not in preexisting
            )
            category, _, save_path = resolve_probe_destination(
                client.config, clean_probe_id
            )
            result["items"] = current_items(final=False)
            result["progress"] = _progress(
                result["items"],
                len(parsed),
                started_monotonic,
                self._monotonic(),
                self.timeout_seconds,
            )
            self._emit(on_update, result)

            if cancel.is_set():
                result["status"] = "cancelled"
                result["items"] = current_items(final=True)
                result["cleanup"] = {
                    "status": "not_required",
                    "deleted": 0,
                    "skipped": 0,
                }
                return copy.deepcopy(result)

            if clean_purpose == "seed" and not candidates:
                _enrich_unknown_trackers(client, parsed, latest, capabilities)
                result["status"] = "complete"
                result["items"] = current_items(final=True)
                result["progress"] = _progress(
                    result["items"],
                    len(parsed),
                    started_monotonic,
                    self._monotonic(),
                    self.timeout_seconds,
                )
                result["cleanup"] = {
                    "status": "not_required",
                    "deleted": 0,
                    "skipped": 0,
                }
                return copy.deepcopy(result)

            if clean_purpose == "metadata":
                assert metadata_deadline is not None
                metadata_timed_out = _collect_probe_contents(
                    client,
                    parsed,
                    preexisting,
                    latest,
                    journal,
                    contents,
                    deadline=metadata_deadline,
                    monotonic=self._monotonic,
                )
                if metadata_timed_out:
                    timed_out = True
                    result["status"] = "complete"
                    result["items"] = current_items(
                        final=True,
                        timed_out_now=True,
                    )
                    result["progress"] = _progress(
                        result["items"],
                        len(parsed),
                        started_monotonic,
                        self._monotonic(),
                        self.timeout_seconds,
                    )
                    result["cleanup"] = {
                        "status": "not_required",
                        "deleted": 0,
                        "skipped": 0,
                    }
                    return copy.deepcopy(result)

            if candidates:
                if clean_purpose == "metadata":
                    assert metadata_deadline is not None
                    remaining = metadata_deadline - self._monotonic()
                    if (
                        remaining
                        <= PROBE_NAMESPACE_MAX_REQUESTS * PROBE_MIN_HTTP_TIMEOUT_SECONDS
                    ):
                        return complete_metadata_timeout()
                    client.ensure_probe_namespace(
                        category=category,
                        tag=PROBE_TAG,
                        save_path=save_path,
                        timeout_seconds=(remaining / PROBE_NAMESPACE_MAX_REQUESTS),
                    )
                    if self._monotonic() >= metadata_deadline:
                        return complete_metadata_timeout()
                else:
                    client.ensure_probe_namespace(
                        category=category,
                        tag=PROBE_TAG,
                        save_path=save_path,
                    )

                if cancel.is_set():
                    return complete_cancelled_without_add()
                if (
                    clean_purpose == "metadata"
                    and self._monotonic() >= metadata_deadline
                ):
                    return complete_metadata_timeout()
                if cancel.is_set():
                    return complete_cancelled_without_add()
                journal = ProbeJournal(
                    probe_id=clean_probe_id,
                    created_at=created_at,
                    category=category,
                    tag=PROBE_TAG,
                    save_path=save_path,
                    hashes=tuple(item.info_hash for item in candidates),
                )
                if (
                    clean_purpose == "metadata"
                    and self._monotonic() >= metadata_deadline
                ):
                    journal = None
                    return complete_metadata_timeout()
                if cancel.is_set():
                    journal = None
                    return complete_cancelled_without_add()
                self.journal_store.write(journal)
                journal_written = True
                if cancel.is_set():
                    remove_unsent_journal()
                    return complete_cancelled_without_add()
                candidate_uris = [item.uri for item in candidates]
                if cancel.is_set():
                    remove_unsent_journal()
                    return complete_cancelled_without_add()
                if clean_purpose == "metadata":
                    remaining = metadata_deadline - self._monotonic()
                    if remaining <= PROBE_MIN_HTTP_TIMEOUT_SECONDS:
                        remove_unsent_journal()
                        return complete_metadata_timeout()
                    add_outcome = client.add_probe_magnets(
                        candidate_uris,
                        category=category,
                        tag=PROBE_TAG,
                        save_path=save_path,
                        timeout_seconds=remaining,
                    )
                    if self._monotonic() >= metadata_deadline:
                        timed_out = True
                        _mark_probe_contents_unavailable(parsed, contents)
                else:
                    add_outcome = client.add_probe_magnets(
                        candidate_uris,
                        category=category,
                        tag=PROBE_TAG,
                        save_path=save_path,
                    )

            if (
                clean_purpose == "metadata"
                and not candidates
                and _all_probe_contents_resolved(parsed, contents)
            ):
                result["status"] = "complete"
                result["items"] = current_items(final=True)
                result["progress"] = _progress(
                    result["items"],
                    len(parsed),
                    started_monotonic,
                    self._monotonic(),
                    self.timeout_seconds,
                )
                result["cleanup"] = {
                    "status": "not_required",
                    "deleted": 0,
                    "skipped": 0,
                }
                return copy.deepcopy(result)

            observation_started = self._monotonic()
            deadline = (
                metadata_deadline
                if metadata_deadline is not None
                else observation_started + self.timeout_seconds
            )
            while True:
                if cancel.is_set():
                    break
                if clean_purpose == "metadata" and self._monotonic() >= deadline:
                    _mark_probe_contents_unavailable(parsed, contents)
                    timed_out = True
                    break
                snapshot_kwargs: dict[str, float] = {}
                if clean_purpose == "metadata":
                    remaining = deadline - self._monotonic()
                    snapshot_count = len(parsed)
                    batch_count = (
                        snapshot_count + PROBE_HASH_BATCH_SIZE - 1
                    ) // PROBE_HASH_BATCH_SIZE
                    if remaining <= batch_count * 0.5:
                        _mark_probe_contents_unavailable(parsed, contents)
                        timed_out = True
                        break
                    snapshot_kwargs["timeout_seconds"] = min(
                        5.0,
                        remaining / batch_count,
                    )
                snapshots = client.probe_torrent_snapshots(
                    [
                        item.info_hash
                        for item in (
                            parsed if clean_purpose == "metadata" else candidates
                        )
                    ],
                    **snapshot_kwargs,
                )
                _merge_probe_snapshots(latest, snapshots)
                candidate_hashes = {item.info_hash for item in candidates}
                ever_observed.update(candidate_hashes.intersection(snapshots))
                if journal is not None:
                    _raise_if_probe_download_safety_violated(
                        candidates,
                        latest,
                        journal,
                        purpose=clean_purpose,
                    )
                if clean_purpose == "metadata":
                    metadata_timed_out = _collect_probe_contents(
                        client,
                        parsed,
                        preexisting,
                        latest,
                        journal,
                        contents,
                        deadline=deadline,
                        monotonic=self._monotonic,
                    )
                else:
                    metadata_timed_out = False
                now = self._monotonic()
                result["items"] = current_items(final=False)
                result["progress"] = _progress(
                    result["items"],
                    len(parsed),
                    started_monotonic,
                    now,
                    self.timeout_seconds,
                )
                self._emit(on_update, result)
                if metadata_timed_out:
                    timed_out = True
                    break
                if clean_purpose == "metadata" and _all_probe_contents_resolved(
                    parsed, contents
                ):
                    break
                if clean_purpose == "seed" and (
                    now - observation_started >= self.minimum_observation_seconds
                    and _all_candidates_have_positive_seed(candidates, latest)
                ):
                    break
                if now >= deadline:
                    timed_out = True
                    break
                if clean_purpose == "seed" and journal is not None:
                    resume_hashes = [
                        item.info_hash
                        for item in candidates
                        if item.info_hash not in resumed_after_metadata
                        and _owned_probe_needs_resume(
                            latest.get(item.info_hash), journal
                        )
                    ]
                    if resume_hashes:
                        resumed_after_metadata.update(resume_hashes)
                        client.start_owned_probe_torrents(
                            resume_hashes,
                            expected_category=journal.category,
                            expected_tag=journal.tag,
                            expected_save_path=journal.save_path,
                            added_after=journal.added_after,
                            added_before=journal.added_before,
                        )
                self._sleep(min(self.poll_interval_seconds, max(0.0, deadline - now)))

            if clean_purpose == "seed":
                _enrich_unknown_trackers(client, parsed, latest, capabilities)

            if cancel.is_set():
                result["status"] = "cancelled"
            elif add_outcome == "failed" and not ever_observed:
                result["ok"] = False
                result["status"] = "failed"
                result["error"] = "qBittorrent did not accept the probe magnets"
            else:
                result["status"] = "complete"
            result["items"] = current_items(final=True, timed_out_now=timed_out)
            result["progress"] = _progress(
                result["items"],
                len(parsed),
                started_monotonic,
                self._monotonic(),
                self.timeout_seconds,
            )
        except Exception as exc:
            result["ok"] = False
            result["status"] = "failed"
            result["error"] = _safe_error(exc)
            result["items"] = current_items(final=True, timed_out_now=timed_out)
            result["progress"] = _progress(
                result["items"],
                len(parsed),
                started_monotonic,
                self._monotonic(),
                self.timeout_seconds,
            )
        finally:
            if journal_written and journal is not None and client is not None:
                self._emit(on_update, {**result, "status": "cleaning"})
                try:
                    cleanup = client.delete_owned_probe_torrents(
                        list(journal.hashes),
                        expected_category=journal.category,
                        expected_tag=journal.tag,
                        expected_save_path=journal.save_path,
                        added_after=journal.added_after,
                        added_before=journal.added_before,
                    )
                    # A hash never observed by this run may still be an asynchronous
                    # qB add. Keep its journal so the age-gated reaper can decide later.
                    unresolved_missing = set(cleanup.get("missing", [])) - ever_observed
                    remaining = list(cleanup.get("remaining", []))
                    skipped = list(cleanup.get("skipped", []))
                    if not remaining and not unresolved_missing and not skipped:
                        self.journal_store.remove(journal.probe_id)
                        cleanup_status = "complete"
                    else:
                        cleanup_status = "incomplete"
                    result["cleanup"] = {
                        "status": cleanup_status,
                        "deleted": len(cleanup.get("deleted", [])),
                        "skipped": len(skipped),
                        "remaining": remaining,
                    }
                except Exception as exc:
                    result["cleanup"] = {
                        "status": "incomplete",
                        "deleted": 0,
                        "skipped": 0,
                        "error": _safe_error(exc),
                    }
            elif result.get("cleanup") is None:
                result["cleanup"] = {
                    "status": "not_required",
                    "deleted": 0,
                    "skipped": 0,
                }
            self._emit(on_update, result)
        return copy.deepcopy(result)

    def reap_stale(
        self, *, max_age_seconds: int = DEFAULT_STALE_AGE_SECONDS
    ) -> dict[str, object]:
        safe_age = max(60, min(int(max_age_seconds), 86_400))
        now = int(self._wall_clock())
        cleaned: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, str]] = []
        client: QbittorrentClient | None = None
        for path in self.journal_store.paths():
            try:
                journal = self.journal_store.read(path)
            except MagnetProbeError as exc:
                errors.append({"journal": path.name, "error": _safe_error(exc)})
                continue
            if now - journal.created_at < safe_age:
                skipped.append(journal.probe_id)
                continue
            try:
                if client is None:
                    client = self._client_factory()
                expected_marker = resolve_probe_destination(
                    client.config, journal.probe_id
                )
                if expected_marker != (
                    journal.category,
                    journal.tag,
                    journal.save_path,
                ):
                    raise MagnetProbeError(
                        "stale probe journal does not match the current qBittorrent configuration"
                    )
                cleanup = client.delete_owned_probe_torrents(
                    list(journal.hashes),
                    expected_category=journal.category,
                    expected_tag=journal.tag,
                    expected_save_path=journal.save_path,
                    added_after=journal.added_after,
                    added_before=journal.added_before,
                )
                if cleanup.get("remaining") or cleanup.get("skipped"):
                    raise MagnetProbeError(
                        "one or more stale probe torrents could not be safely removed"
                    )
                self.journal_store.remove(journal.probe_id)
                cleaned.append(journal.probe_id)
            except Exception as exc:
                errors.append({"journal": path.name, "error": _safe_error(exc)})
        return {"cleaned": cleaned, "skipped": skipped, "errors": errors}

    @staticmethod
    def _emit(
        callback: Callable[[dict[str, object]], None] | None,
        payload: dict[str, object],
    ) -> None:
        if callback is None:
            return
        try:
            callback(copy.deepcopy(payload))
        except Exception:
            pass


class MagnetProbeManager:
    def __init__(
        self,
        service: MagnetProbeService,
        *,
        completed_ttl_seconds: int = 600,
        max_jobs: int = 24,
    ) -> None:
        self.service = service
        self.completed_ttl_seconds = max(30, min(int(completed_ttl_seconds), 3600))
        self.max_jobs = max(1, min(int(max_jobs), 100))
        self._jobs: dict[str, dict[str, object]] = {}
        self._completed_at: dict[str, float] = {}
        self._controls: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._requested_hashes: dict[str, frozenset[str]] = {}
        self._download_reservations: dict[str, frozenset[str]] = {}
        self._external_reservations: dict[str, frozenset[str]] = {}
        self._active_id: str | None = None
        self._stopping = False
        self._lock = threading.RLock()

    def start(
        self,
        magnets: list[str] | tuple[str, ...],
        *,
        purpose: str = "seed",
    ) -> dict[str, object]:
        parsed = normalize_probe_magnets(magnets)
        clean_purpose = _probe_purpose(purpose)
        uris = tuple(item.uri for item in parsed)
        requested_hashes = frozenset(item.info_hash for item in parsed)
        with self._lock:
            self._purge_locked()
            if self._stopping:
                raise MagnetProbeError("magnet probe manager is shutting down")
            reserved_hashes = (
                set().union(*self._download_reservations.values())
                if self._download_reservations
                else set()
            )
            if requested_hashes.intersection(reserved_hashes):
                raise MagnetProbeConflictError(
                    "a normal download for this hash is being submitted"
                )
            self._require_no_probe_overlap_locked(requested_hashes)
            if self._active_id is not None:
                raise MagnetProbeBusyError("another magnet probe is already running")
            probe_id = uuid.uuid4().hex
            control = threading.Event()
            initial: dict[str, object] = {
                "ok": True,
                "probe_id": probe_id,
                "purpose": clean_purpose,
                "status": "queued",
                "total": len(parsed),
                "progress": {
                    "resolved": 0,
                    "total": len(parsed),
                    "elapsed_ms": 0,
                    "timeout_ms": (
                        int(self.service.timeout_seconds * 1000)
                        if hasattr(self.service, "timeout_seconds")
                        else 0
                    ),
                },
                "items": [],
                "cleanup": None,
            }
            self._jobs[probe_id] = initial
            self._controls[probe_id] = control
            self._requested_hashes[probe_id] = requested_hashes
            self._active_id = probe_id
            thread = threading.Thread(
                target=self._run_job,
                args=(probe_id, uris, control, clean_purpose),
                name=f"jav-magnet-probe-{probe_id[:8]}",
                daemon=True,
            )
            self._threads[probe_id] = thread
            try:
                thread.start()
            except Exception as exc:
                self._threads.pop(probe_id, None)
                self._controls.pop(probe_id, None)
                self._jobs.pop(probe_id, None)
                self._requested_hashes.pop(probe_id, None)
                if self._active_id == probe_id:
                    self._active_id = None
                raise MagnetProbeError(_safe_error(exc)) from exc
            return copy.deepcopy(initial)

    def require_no_hash_overlap(
        self,
        hashes: list[str] | tuple[str, ...],
    ) -> None:
        requested = _normalize_overlap_hashes(hashes)
        with self._lock:
            self._require_no_probe_overlap_locked(requested)

    def reserve_external_hashes(
        self,
        hashes: list[str] | tuple[str, ...],
    ) -> str:
        """Reserve hashes for a manager whose work lives outside this manager.

        The reservation is acquired under the same lock used by normal probes
        and download submissions, so callers can safely hold it across an
        asynchronous operation without a check-then-submit race.
        """
        requested = _normalize_overlap_hashes(hashes)
        token = uuid.uuid4().hex
        with self._lock:
            self._purge_locked()
            if self._stopping:
                raise MagnetProbeError("magnet probe manager is shutting down")
            self._require_no_probe_overlap_locked(requested)
            reserved_hashes = (
                set().union(*self._download_reservations.values())
                if self._download_reservations
                else set()
            )
            if requested.intersection(reserved_hashes):
                raise MagnetProbeConflictError(
                    "a normal download for this hash is being submitted"
                )
            self._external_reservations[token] = requested
        return token

    def release_external_hashes(self, token: object) -> None:
        clean_token = str(token or "").strip()
        if not clean_token:
            return
        with self._lock:
            self._external_reservations.pop(clean_token, None)

    @contextmanager
    def reserve_download_hashes(
        self,
        hashes: list[str] | tuple[str, ...],
    ) -> Iterator[None]:
        requested = _normalize_overlap_hashes(hashes)
        token = uuid.uuid4().hex
        with self._lock:
            self._require_no_probe_overlap_locked(requested)
            reserved_hashes = (
                set().union(*self._download_reservations.values())
                if self._download_reservations
                else set()
            )
            if requested.intersection(reserved_hashes):
                raise MagnetProbeConflictError(
                    "a normal download for this hash is already being submitted"
                )
            self._download_reservations[token] = requested
        try:
            yield
        finally:
            with self._lock:
                self._download_reservations.pop(token, None)

    def _require_no_probe_overlap_locked(
        self,
        requested: frozenset[str],
    ) -> None:
        active_hashes = (
            set().union(*self._requested_hashes.values())
            if self._requested_hashes
            else set()
        )
        if requested.intersection(active_hashes):
            raise MagnetProbeConflictError(
                "a temporary magnet probe for this hash is still active"
            )
        external_hashes = (
            set().union(*self._external_reservations.values())
            if self._external_reservations
            else set()
        )
        if requested.intersection(external_hashes):
            raise MagnetProbeConflictError(
                "a smart magnet selection for this hash is still active"
            )
        self._require_no_journal_overlap_locked(requested)

    def _require_no_journal_overlap_locked(
        self,
        requested: frozenset[str],
    ) -> None:
        store = getattr(self.service, "journal_store", None)
        if store is None:
            return
        try:
            paths = store.paths()
            journals = [store.read(path) for path in paths]
        except Exception as exc:
            raise MagnetProbeConflictError(
                "cannot verify temporary magnet probe cleanup"
            ) from exc
        if any(requested.intersection(journal.hashes) for journal in journals):
            raise MagnetProbeConflictError(
                "a temporary magnet probe for this hash has not been cleaned"
            )

    def get(self, probe_id: str) -> dict[str, object]:
        clean_probe_id = str(probe_id or "")
        if not PROBE_ID_RE.fullmatch(clean_probe_id):
            raise MagnetProbeError("invalid probe id")
        with self._lock:
            self._purge_locked()
            job = self._jobs.get(clean_probe_id)
            if job is None:
                raise MagnetProbeNotFoundError("magnet probe was not found")
            return copy.deepcopy(job)

    def cancel(self, probe_id: str) -> bool:
        clean_probe_id = str(probe_id or "")
        if not PROBE_ID_RE.fullmatch(clean_probe_id):
            raise MagnetProbeError("invalid probe id")
        with self._lock:
            control = self._controls.get(clean_probe_id)
            if control is None:
                if clean_probe_id in self._jobs:
                    return False
                raise MagnetProbeNotFoundError("magnet probe was not found")
            control.set()
            job = self._jobs.get(clean_probe_id)
            if job and job.get("status") in {"queued", "running"}:
                job["status"] = "cancelling"
            return True

    def reap_stale(
        self, *, max_age_seconds: int = DEFAULT_STALE_AGE_SECONDS
    ) -> dict[str, object]:
        return self.service.reap_stale(max_age_seconds=max_age_seconds)

    def shutdown(self, *, timeout: float = 35.0) -> bool:
        with self._lock:
            self._stopping = True
            controls = list(self._controls.values())
            threads = list(self._threads.values())
            for control in controls:
                control.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in threads)

    def _run_job(
        self,
        probe_id: str,
        magnets: tuple[str, ...],
        control: threading.Event,
        purpose: str,
    ) -> None:
        try:
            result = self.service.run(
                magnets,
                purpose=purpose,
                probe_id=probe_id,
                cancel_event=control,
                on_update=lambda payload: self._update_job(probe_id, payload),
            )
        except Exception as exc:
            result = {
                "ok": False,
                "probe_id": probe_id,
                "purpose": purpose,
                "status": "failed",
                "total": len(magnets),
                "error": _safe_error(exc),
                "items": [],
                "cleanup": {"status": "unknown"},
            }
        result["purpose"] = purpose
        with self._lock:
            self._jobs[probe_id] = copy.deepcopy(result)
            self._completed_at[probe_id] = time.monotonic()
            self._controls.pop(probe_id, None)
            self._threads.pop(probe_id, None)
            self._requested_hashes.pop(probe_id, None)
            if self._active_id == probe_id:
                self._active_id = None
            self._purge_locked()

    def _update_job(self, probe_id: str, payload: dict[str, object]) -> None:
        with self._lock:
            if probe_id in self._jobs:
                updated = copy.deepcopy(payload)
                if (
                    self._jobs[probe_id].get("status") == "cancelling"
                    and updated.get("status") in {"queued", "running"}
                ):
                    updated["status"] = "cancelling"
                self._jobs[probe_id] = updated

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [
            probe_id
            for probe_id, completed_at in self._completed_at.items()
            if completed_at + self.completed_ttl_seconds <= now
        ]
        for probe_id in expired:
            self._jobs.pop(probe_id, None)
            self._completed_at.pop(probe_id, None)
        while len(self._jobs) > self.max_jobs:
            removable = next(
                (probe_id for probe_id in self._jobs if probe_id in self._completed_at),
                None,
            )
            if removable is None:
                break
            self._jobs.pop(removable, None)
            self._completed_at.pop(removable, None)


def _validate_journal(journal: ProbeJournal) -> None:
    if not PROBE_ID_RE.fullmatch(journal.probe_id):
        raise MagnetProbeError("probe journal id is invalid")
    if journal.created_at <= 0:
        raise MagnetProbeError("probe journal timestamp is invalid")
    if (
        not journal.category.endswith("-probe")
        or not journal.category
        or len(journal.category) > 120
    ):
        raise MagnetProbeError("probe journal category is invalid")
    if any(ord(character) < 32 for character in journal.category):
        raise MagnetProbeError("probe journal category is invalid")
    if journal.tag != PROBE_TAG:
        raise MagnetProbeError("probe journal tag is invalid")
    path = PurePosixPath(journal.save_path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or "\\" in journal.save_path
        or len(path.parts) < 3
        or path.parts[-2:] != (".probe", journal.probe_id)
    ):
        raise MagnetProbeError("probe journal path is invalid")
    if not journal.hashes or len(journal.hashes) > PROBE_MAX_MAGNETS:
        raise MagnetProbeError("probe journal hash count is invalid")
    if len(set(journal.hashes)) != len(journal.hashes) or any(
        not INFO_HASH_RE.fullmatch(info_hash) for info_hash in journal.hashes
    ):
        raise MagnetProbeError("probe journal hashes are invalid")


def _snapshot_owned(snapshot: dict[str, object], journal: ProbeJournal) -> bool:
    tags = snapshot.get("tags")
    added_on = snapshot.get("added_on")
    return bool(
        snapshot.get("category") == journal.category
        and isinstance(tags, tuple)
        and journal.tag in tags
        and snapshot.get("save_path") == journal.save_path
        and isinstance(added_on, int)
        and journal.added_after <= added_on <= journal.added_before
    )


def _all_candidates_have_positive_seed(
    candidates: tuple[ProbeMagnet, ...],
    snapshots: dict[str, dict[str, object]],
) -> bool:
    if not candidates or any(item.info_hash not in snapshots for item in candidates):
        return False
    for item in candidates:
        if not _snapshot_has_positive_seed(snapshots[item.info_hash]):
            return False
    return True


def _merge_probe_snapshots(
    observations: dict[str, dict[str, object]],
    snapshots: dict[str, dict[str, object]],
) -> None:
    for info_hash, snapshot in snapshots.items():
        previous = observations.get(info_hash, {})
        merged = dict(previous)
        merged.update(snapshot)
        current_name = _optional_probe_text(snapshot.get("name"))
        previous_name = _optional_probe_text(previous.get("name"))
        merged["name"] = current_name or previous_name
        for field, tracker_field in (("seeders", "seeders"), ("leechers", "leechers")):
            merged[field] = _maximum_optional_int(
                _reported_count(previous, field, tracker_field),
                _reported_count(snapshot, field, tracker_field),
            )
        for field in (
            "connected_seeders",
            "connected_leechers",
            "downloaded",
            "total_size",
        ):
            merged[field] = _maximum_optional_int(
                previous.get(field), snapshot.get(field)
            )
        merged["availability"] = _maximum_optional_float(
            previous.get("availability"),
            snapshot.get("availability"),
        )
        merged["download_speed"] = _latest_optional_int(
            snapshot.get("download_speed"), previous.get("download_speed")
        )
        merged["upload_speed"] = _latest_optional_int(
            snapshot.get("upload_speed"), previous.get("upload_speed")
        )
        merged["peak_download_speed"] = _maximum_optional_int(
            previous.get("peak_download_speed"),
            snapshot.get("peak_download_speed"),
            snapshot.get("download_speed"),
        )
        merged["progress"] = _latest_optional_float(
            snapshot.get("progress"), previous.get("progress")
        )
        merged["trackers"] = _merge_tracker_observations(
            previous.get("trackers"),
            snapshot.get("trackers"),
        )
        merged["_metadata_received"] = bool(
            previous.get("_metadata_received")
        ) or _snapshot_metadata_received(snapshot)
        observations[info_hash] = merged


def _snapshot_metadata_received(snapshot: dict[str, object]) -> bool:
    explicit = snapshot.get("_metadata_received")
    if isinstance(explicit, bool):
        return explicit
    return bool(
        (total_size := _optional_nonnegative_int(snapshot.get("total_size")))
        is not None
        and total_size > 0
        and str(snapshot.get("state") or "") not in {"forcedMetaDL", "metaDL"}
    )


def _snapshot_has_positive_seed(snapshot: dict[str, object]) -> bool:
    return any(
        value is not None and value > 0
        for value in (
            _reported_count(snapshot, "seeders", "seeders"),
            _optional_nonnegative_int(snapshot.get("connected_seeders")),
        )
    )


def _owned_probe_needs_resume(
    snapshot: dict[str, object] | None,
    journal: ProbeJournal,
) -> bool:
    return bool(
        snapshot is not None
        and _snapshot_owned(snapshot, journal)
        and snapshot.get("state") in {"stoppedDL", "pausedDL"}
        and not _snapshot_has_positive_seed(snapshot)
    )


def _raise_if_probe_download_safety_violated(
    candidates: tuple[ProbeMagnet, ...],
    observations: dict[str, dict[str, object]],
    journal: ProbeJournal,
    *,
    purpose: str = "seed",
) -> None:
    for item in candidates:
        snapshot = observations.get(item.info_hash)
        if snapshot is None or not _snapshot_owned(snapshot, journal):
            continue
        downloaded = _optional_nonnegative_int(snapshot.get("downloaded"))
        if purpose == "metadata" and downloaded is not None and downloaded > 0:
            raise MagnetProbeError("metadata probe downloaded torrent content")
        if (
            purpose == "seed"
            and downloaded is not None
            and downloaded >= PROBE_MAX_DOWNLOADED_BYTES
        ):
            raise MagnetProbeError("probe download safety limit exceeded")
        state = str(snapshot.get("state") or "")
        if state not in {"forcedMetaDL", "metaDL", "stoppedDL", "pausedDL"}:
            download_limit = _optional_nonnegative_int(snapshot.get("dl_limit"))
            if download_limit != PROBE_DOWNLOAD_LIMIT_BYTES_PER_SECOND:
                raise MagnetProbeError("probe download rate safety limit violated")


def _merge_tracker_observations(
    previous: object, current: object
) -> tuple[dict[str, object], ...]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for collection in (previous, current):
        if not isinstance(collection, tuple):
            continue
        for tracker in collection:
            if not isinstance(tracker, dict):
                continue
            marker = (
                tracker.get("status"),
                tracker.get("seeders"),
                tracker.get("leechers"),
            )
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(dict(tracker))
    return tuple(merged)


def _maximum_optional_int(*values: object) -> int | None:
    parsed = [
        value
        for value in (_optional_nonnegative_int(candidate) for candidate in values)
        if value is not None
    ]
    return max(parsed) if parsed else None


def _maximum_optional_float(*values: object) -> float | None:
    parsed = [
        value
        for value in (_optional_nonnegative_float(candidate) for candidate in values)
        if value is not None
    ]
    return max(parsed) if parsed else None


def _latest_optional_int(*values: object) -> int | None:
    for value in values:
        parsed = _optional_nonnegative_int(value)
        if parsed is not None:
            return parsed
    return None


def _latest_optional_float(*values: object) -> float | None:
    for value in values:
        parsed = _optional_nonnegative_float(value)
        if parsed is not None:
            return parsed
    return None


def _enrich_unknown_trackers(
    client: QbittorrentClient,
    magnets: tuple[ProbeMagnet, ...],
    snapshots: dict[str, dict[str, object]],
    capabilities: dict[str, object],
) -> None:
    if not capabilities.get("include_trackers"):
        return
    unknown_hashes = [
        item.info_hash
        for item in magnets
        if item.info_hash in snapshots
        and not _snapshot_has_positive_seed(snapshots[item.info_hash])
    ]
    if not unknown_hashes:
        return
    try:
        _merge_probe_snapshots(
            snapshots,
            client.probe_torrent_snapshots(unknown_hashes, include_trackers=True),
        )
    except Exception:
        # Tracker detail is optional enrichment; the base qB observation remains usable.
        return


def _collect_probe_contents(
    client: QbittorrentClient,
    magnets: tuple[ProbeMagnet, ...],
    preexisting: dict[str, dict[str, object]],
    snapshots: dict[str, dict[str, object]],
    journal: ProbeJournal | None,
    contents: dict[str, dict[str, object]],
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> bool:
    if monotonic() >= deadline:
        _mark_probe_contents_unavailable(magnets, contents)
        return True
    configured_category = str(client.config.category or "").strip()
    owned_to_stop: list[str] = []
    if journal is not None:
        for magnet in magnets:
            previous = contents.get(magnet.info_hash, {})
            snapshot = snapshots.get(magnet.info_hash)
            if (
                previous.get("metadata_status")
                not in {"ready", "restricted", "unavailable"}
                and magnet.info_hash not in preexisting
                and snapshot is not None
                and _snapshot_owned(snapshot, journal)
                and bool(snapshot.get("_metadata_received"))
                and snapshot.get("downloaded") == 0
            ):
                owned_to_stop.append(magnet.info_hash)

    verified_owned: set[str] = set()
    if owned_to_stop:
        remaining = deadline - monotonic()
        snapshot_batches = (
            len(owned_to_stop) + PROBE_HASH_BATCH_SIZE - 1
        ) // PROBE_HASH_BATCH_SIZE
        request_count = snapshot_batches * 2 + 1
        if remaining <= request_count * 0.5:
            _mark_probe_contents_unavailable(magnets, contents)
            return True
        request_timeout = min(5.0, remaining / request_count)
        try:
            stopped = client.stop_owned_probe_torrents(
                owned_to_stop,
                expected_category=journal.category,
                expected_tag=journal.tag,
                expected_save_path=journal.save_path,
                added_after=journal.added_after,
                added_before=journal.added_before,
                timeout_seconds=request_timeout,
            )
            raw_verified = stopped.get("verified", [])
            if not isinstance(raw_verified, list):
                raise MagnetProbeError(
                    "qBittorrent returned an invalid probe stop result"
                )
            verified_owned = {
                info_hash
                for info_hash in raw_verified
                if isinstance(info_hash, str) and info_hash in owned_to_stop
            }
        except Exception as exc:
            error = _safe_error(exc)
            for info_hash in owned_to_stop:
                snapshot = snapshots.get(info_hash, {})
                pending = _pending_probe_content(
                    snapshot,
                    contents.get(info_hash, {}),
                )
                pending["content_error"] = error
                contents[info_hash] = pending
        if monotonic() >= deadline:
            _mark_probe_contents_unavailable(magnets, contents)
            return True

    displayed_count = sum(
        len(files)
        for content in contents.values()
        if isinstance((files := content.get("files")), list)
    )
    for magnet in magnets:
        previous = contents.get(magnet.info_hash, {})
        if previous.get("metadata_status") in {
            "ready",
            "restricted",
            "unavailable",
        }:
            continue
        if monotonic() >= deadline:
            _mark_probe_contents_unavailable(magnets, contents)
            return True
        snapshot = snapshots.get(magnet.info_hash)
        if snapshot is None:
            continue

        if magnet.info_hash in preexisting:
            readable = bool(
                configured_category and snapshot.get("category") == configured_category
            )
        else:
            readable = bool(journal is not None and _snapshot_owned(snapshot, journal))
        if not readable:
            contents[magnet.info_hash] = {"metadata_status": "restricted"}
            continue

        pending = _pending_probe_content(snapshot, previous)
        contents[magnet.info_hash] = pending
        if not bool(snapshot.get("_metadata_received")):
            continue
        if magnet.info_hash not in preexisting and (
            snapshot.get("downloaded") != 0 or magnet.info_hash not in verified_owned
        ):
            continue

        remaining = deadline - monotonic()
        if remaining <= 0:
            _mark_probe_contents_unavailable(magnets, contents)
            return True
        try:
            raw_files = client.torrent_files(
                magnet.info_hash,
                timeout_seconds=min(15.0, remaining),
            )
            if (
                not isinstance(raw_files, (list, tuple))
                or not raw_files
                or len(raw_files) > 20_000
            ):
                raise MagnetProbeError(
                    "qBittorrent returned an invalid torrent file list"
                )
            remaining = max(0, PROBE_METADATA_FILES_PER_JOB - displayed_count)
            display_limit = min(PROBE_METADATA_FILES_PER_TORRENT, remaining)
            files = [_probe_content_file(item) for item in raw_files[:display_limit]]
        except Exception as exc:
            pending["content_error"] = _safe_error(exc)
            contents[magnet.info_hash] = pending
            if monotonic() >= deadline:
                _mark_probe_contents_unavailable(magnets, contents)
                return True
            continue

        if monotonic() >= deadline:
            _mark_probe_contents_unavailable(magnets, contents)
            return True
        remaining = deadline - monotonic()
        try:
            fresh = client.probe_torrent_snapshots(
                [magnet.info_hash],
                timeout_seconds=min(5.0, remaining),
            )
        except Exception as exc:
            pending["content_error"] = _safe_error(exc)
            contents[magnet.info_hash] = pending
            if monotonic() >= deadline:
                _mark_probe_contents_unavailable(magnets, contents)
                return True
            continue
        if monotonic() >= deadline:
            _mark_probe_contents_unavailable(magnets, contents)
            return True
        fresh_snapshot = fresh.get(magnet.info_hash)
        if magnet.info_hash in preexisting:
            still_readable = bool(
                fresh_snapshot is not None
                and configured_category
                and fresh_snapshot.get("category") == configured_category
            )
        else:
            still_readable = bool(
                fresh_snapshot is not None
                and journal is not None
                and _snapshot_owned(fresh_snapshot, journal)
                and fresh_snapshot.get("state") in {"stoppedDL", "pausedDL"}
                and fresh_snapshot.get("downloaded") == 0
            )
        if not still_readable:
            contents[magnet.info_hash] = {"metadata_status": "restricted"}
            continue

        _merge_probe_snapshots(snapshots, fresh)
        verified_snapshot = snapshots[magnet.info_hash]
        pending = _pending_probe_content(verified_snapshot, pending)
        torrent_name = _optional_probe_text(verified_snapshot.get("name"))

        file_count = len(raw_files)
        displayed_count += len(files)
        catalog_code = catalog_code_from_download_metadata(
            torrent_name,
            [str(item.get("name") or "") for item in raw_files],
        )
        ready = {
            **pending,
            "metadata_status": "ready",
            "file_count": file_count,
            "files": files,
            "files_truncated": len(files) < file_count,
            "catalog_code": catalog_code,
            "requires_confirmation": catalog_code is None,
        }
        ready.pop("content_error", None)
        contents[magnet.info_hash] = ready
    return False


def _pending_probe_content(
    snapshot: dict[str, object],
    previous: dict[str, object],
) -> dict[str, object]:
    pending: dict[str, object] = {"metadata_status": "pending"}
    torrent_name = _optional_probe_text(snapshot.get("name"))
    total_size = _optional_nonnegative_int(snapshot.get("total_size"))
    if torrent_name is not None:
        pending["torrent_name"] = torrent_name
    if total_size is not None:
        pending["total_size"] = total_size
    previous_error = previous.get("content_error")
    if isinstance(previous_error, str) and previous_error:
        pending["content_error"] = previous_error
    return pending


def _mark_probe_contents_unavailable(
    magnets: tuple[ProbeMagnet, ...],
    contents: dict[str, dict[str, object]],
) -> None:
    for magnet in magnets:
        current = contents.get(magnet.info_hash, {})
        if current.get("metadata_status") in {"ready", "restricted"}:
            continue
        unavailable = dict(current)
        unavailable["metadata_status"] = "unavailable"
        unavailable.pop("catalog_code", None)
        unavailable.pop("requires_confirmation", None)
        contents[magnet.info_hash] = unavailable


def _all_probe_contents_resolved(
    magnets: tuple[ProbeMagnet, ...],
    contents: dict[str, dict[str, object]],
) -> bool:
    return bool(magnets) and all(
        contents.get(item.info_hash, {}).get("metadata_status")
        in {"ready", "restricted", "unavailable"}
        for item in magnets
    )


def _probe_content_file(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        raise MagnetProbeError("qBittorrent returned an invalid torrent file list")
    index = item.get("index")
    size = item.get("size")
    name = item.get("name")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index > 20_000
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > 2**63 - 1
        or not isinstance(name, str)
    ):
        raise MagnetProbeError("qBittorrent returned an invalid torrent file list")
    clean_name = name.strip()
    path = PurePosixPath(clean_name)
    if (
        not clean_name
        or len(clean_name.encode("utf-8", errors="replace"))
        > PROBE_METADATA_TEXT_MAX_BYTES
        or clean_name.startswith(("/", "\\"))
        or "\\" in clean_name
        or "\x00" in clean_name
        or any(ord(character) < 32 or ord(character) == 127 for character in clean_name)
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != clean_name
    ):
        raise MagnetProbeError("qBittorrent returned an unsafe torrent file path")
    return {"index": index, "name": clean_name, "size": size}


def _optional_probe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text.encode("utf-8", errors="replace")) > PROBE_METADATA_TEXT_MAX_BYTES
        or "\x00" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    return text


def _probe_content_fields(
    info_hash: str,
    contents: dict[str, dict[str, object]],
    *,
    final: bool,
) -> dict[str, object]:
    fields = copy.deepcopy(contents.get(info_hash, {}))
    status = fields.get("metadata_status")
    if status not in {"pending", "ready", "unavailable", "restricted"}:
        status = "pending"
    if final and status == "pending":
        status = "unavailable"
    fields["metadata_status"] = status
    catalog_code = fields.get("catalog_code")
    if not isinstance(catalog_code, str) or not catalog_code:
        catalog_code = None
    fields["catalog_code"] = catalog_code
    fields["requires_confirmation"] = catalog_code is None
    return fields


def _probe_items(
    magnets: tuple[ProbeMagnet, ...],
    preexisting: dict[str, dict[str, object]],
    snapshots: dict[str, dict[str, object]],
    journal: ProbeJournal | None,
    *,
    final: bool,
    timed_out: bool = False,
    purpose: str = "seed",
    contents: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    safe_contents = contents or {}
    items: list[dict[str, object]] = []
    for magnet in magnets:
        current = snapshots.get(magnet.info_hash)
        if magnet.info_hash in preexisting:
            origin = "preexisting"
        elif (
            current is not None
            and journal is not None
            and _snapshot_owned(current, journal)
        ):
            origin = "temporary"
        elif current is not None:
            origin = "external"
        else:
            origin = "temporary"

        if current is None:
            item: dict[str, object] = {
                "info_hash": magnet.info_hash,
                "origin": origin,
                "state": "timeout" if final and timed_out else "waiting",
                "seed_status": "unknown",
                "seeders": None,
                "connected_seeders": None,
                "leechers": None,
                "availability": None,
                "availability_status": "unknown",
                "metadata_received": False,
                "download_speed": None,
                "peak_download_speed": None,
            }
            if purpose == "metadata":
                item.update(
                    _probe_content_fields(
                        magnet.info_hash,
                        safe_contents,
                        final=final,
                    )
                )
            items.append(item)
            continue

        raw_state = str(current.get("state") or "")
        seeders = _reported_count(current, "seeders", "seeders")
        leechers = _reported_count(current, "leechers", "leechers")
        connected_seeders = _optional_nonnegative_int(current.get("connected_seeders"))
        availability = _optional_nonnegative_float(current.get("availability"))
        positive_seed_observation = any(
            value is not None and value > 0 for value in (seeders, connected_seeders)
        )
        if positive_seed_observation:
            seed_status = "available"
        elif (
            final
            and timed_out
            and _working_trackers_report_zero(current)
            and raw_state not in {"forcedMetaDL", "metaDL", "stoppedDL", "pausedDL"}
        ):
            seed_status = "none_observed"
        else:
            seed_status = "unknown"
        if availability is None:
            availability_status = "unknown"
        elif availability >= 1.0:
            availability_status = "complete_copy"
        elif availability > 0:
            availability_status = "partial"
        else:
            availability_status = "none"
        item = {
            "info_hash": magnet.info_hash,
            "origin": origin,
            "state": "conflict" if origin == "external" else "observed",
            "seed_status": seed_status,
            "seeders": seeders,
            "connected_seeders": connected_seeders,
            "leechers": leechers,
            "availability": availability,
            "availability_status": availability_status,
            "metadata_received": bool(current.get("_metadata_received")),
            "download_speed": _optional_nonnegative_int(
                current.get("download_speed")
            ),
            "peak_download_speed": _optional_nonnegative_int(
                current.get("peak_download_speed")
            ),
            "progress": _optional_nonnegative_float(current.get("progress")),
        }
        if purpose == "metadata":
            item.update(
                _probe_content_fields(
                    magnet.info_hash,
                    safe_contents,
                    final=final,
                )
            )
        items.append(item)
    return items


def _reported_count(
    snapshot: dict[str, object], field: str, tracker_field: str
) -> int | None:
    values: list[int] = []
    direct = _optional_nonnegative_int(snapshot.get(field))
    if direct is not None:
        values.append(direct)
    trackers = snapshot.get("trackers")
    if isinstance(trackers, tuple):
        for tracker in trackers:
            if not isinstance(tracker, dict) or tracker.get("status") != 2:
                continue
            value = _optional_nonnegative_int(tracker.get(tracker_field))
            if value is not None:
                values.append(value)
    return max(values) if values else None


def _working_trackers_report_zero(snapshot: dict[str, object]) -> bool:
    trackers = snapshot.get("trackers")
    if not isinstance(trackers, tuple):
        return False
    reported = [
        value
        for tracker in trackers
        if isinstance(tracker, dict) and tracker.get("status") == 2
        if (value := _optional_nonnegative_int(tracker.get("seeders"))) is not None
    ]
    return bool(reported) and max(reported) == 0


def _progress(
    items: object,
    total: int,
    started: float,
    now: float,
    timeout_seconds: float,
) -> dict[str, int]:
    safe_items = items if isinstance(items, list) else []
    resolved = sum(
        1
        for item in safe_items
        if isinstance(item, dict)
        and (
            item.get("metadata_status") in {"ready", "unavailable", "restricted"}
            if "metadata_status" in item
            else item.get("state") != "waiting"
        )
    )
    return {
        "resolved": resolved,
        "total": total,
        "elapsed_ms": max(0, int((now - started) * 1000)),
        "timeout_ms": int(timeout_seconds * 1000),
    }


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _safe_error(exc: BaseException) -> str:
    text = str(exc or "magnet probe failed")
    text = re.sub(r"magnet:\?[^\s]+", "[magnet]", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://[^\s]+", "[url]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])",
        "[info_hash]",
        text,
    )
    return text.replace("\r", " ").replace("\n", " ")[:300] or "magnet probe failed"
