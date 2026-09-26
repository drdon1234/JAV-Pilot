from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Mapping

from ..config.app_config import AppConfig
from .qbittorrent import (
    PROBE_MAX_MAGNETS,
    QbittorrentClient,
    resolve_download_destination,
    AddDownloadRequest,
)
from .magnet import MagnetError, parse_magnet
from .magnet_selection_store import (
    TERMINAL_CANDIDATE_STATES,
    SelectionLedgerStore,
)
from ..config.runtime_config import runtime_config_path
from ..core.storage import atomic_write_text


SELECTION_JOURNAL_SCHEMA_VERSION = 1
SELECTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SELECTION_TAG_PREFIX = "jav-pilot-smart-"
SELECTION_TAG_RE = re.compile(r"^jav-pilot-smart-[0-9a-f]{32}$")
DEFAULT_SELECTION_TIMEOUT_SECONDS = 300.0
DEFAULT_SELECTION_POLL_SECONDS = 2.0
DEFAULT_SELECTION_MINIMUM_OBSERVATION_SECONDS = 30.0
MAX_SELECTION_TIMEOUT_SECONDS = 900.0
DEFAULT_SELECTION_SCHEDULING_GRACE_SECONDS = 12.0
MAX_SELECTION_SCHEDULING_GRACE_SECONDS = 60.0
SELECTION_ADDED_ON_GRACE_SECONDS = 10
SELECTION_ADDED_ON_WINDOW_SECONDS = 1_200
SELECTION_STALE_AGE_SECONDS = 3_600

SelectionCompletion = Callable[
    [dict[str, object]], Mapping[str, object] | None
]
SelectionStart = Callable[[str], object]


class MagnetSelectionError(RuntimeError):
    pass


class MagnetSelectionBusyError(MagnetSelectionError):
    pass


class MagnetSelectionConflictError(MagnetSelectionError):
    pass


class MagnetSelectionNotFoundError(MagnetSelectionError):
    pass


@dataclass(frozen=True)
class SelectionMagnet:
    uri: str
    info_hash: str
    display_name: str | None


@dataclass(frozen=True)
class SelectionJournal:
    selection_id: str
    created_at: int
    category: str
    tag: str
    save_path: str
    hashes: tuple[str, ...]
    selected_hash: str | None = None

    @property
    def added_after(self) -> int:
        return self.created_at - SELECTION_ADDED_ON_GRACE_SECONDS

    @property
    def added_before(self) -> int:
        return self.created_at + SELECTION_ADDED_ON_WINDOW_SECONDS

    def to_dict(self) -> dict[str, object]:
        _validate_journal(self)
        return {
            "schema_version": SELECTION_JOURNAL_SCHEMA_VERSION,
            "selection_id": self.selection_id,
            "created_at": self.created_at,
            "category": self.category,
            "tag": self.tag,
            "save_path": self.save_path,
            "hashes": list(self.hashes),
            "selected_hash": self.selected_hash,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "SelectionJournal":
        if not isinstance(payload, dict):
            raise MagnetSelectionError("smart selection journal must be an object")
        expected_keys = {
            "schema_version",
            "selection_id",
            "created_at",
            "category",
            "tag",
            "save_path",
            "hashes",
            "selected_hash",
        }
        if (
            set(payload) != expected_keys
            or payload.get("schema_version") != SELECTION_JOURNAL_SCHEMA_VERSION
        ):
            raise MagnetSelectionError("unsupported or malformed smart selection journal")
        raw_hashes = payload.get("hashes")
        if not isinstance(raw_hashes, list) or any(
            not isinstance(value, str) for value in raw_hashes
        ):
            raise MagnetSelectionError("smart selection journal hashes are invalid")
        try:
            created_at = int(payload.get("created_at"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise MagnetSelectionError("smart selection journal timestamp is invalid") from exc
        raw_selected = payload.get("selected_hash")
        selected_hash = None if raw_selected is None else str(raw_selected).strip().lower()
        journal = cls(
            selection_id=str(payload.get("selection_id") or ""),
            created_at=created_at,
            category=str(payload.get("category") or ""),
            tag=str(payload.get("tag") or ""),
            save_path=str(payload.get("save_path") or ""),
            hashes=tuple(raw_hashes),
            selected_hash=selected_hash,
        )
        _validate_journal(journal)
        return journal


class SelectionJournalStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or runtime_config_path().parent / "qb-smart-selections"

    def path_for(self, selection_id: str) -> Path:
        if not SELECTION_ID_RE.fullmatch(str(selection_id)):
            raise MagnetSelectionError("invalid smart selection id")
        return self.root / f"{selection_id}.json"

    def write(self, journal: SelectionJournal, *, replace: bool = False) -> Path:
        payload = journal.to_dict()
        path = self.path_for(journal.selection_id)
        if path.exists() and not replace:
            raise MagnetSelectionError("smart selection journal already exists")
        raw = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        atomic_write_text(path, raw)
        return path

    def read(self, path: Path) -> SelectionJournal:
        try:
            if path.stat().st_size > 64 * 1024:
                raise MagnetSelectionError("smart selection journal is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except MagnetSelectionError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise MagnetSelectionError("cannot read smart selection journal") from exc
        journal = SelectionJournal.from_dict(payload)
        if path.name != f"{journal.selection_id}.json":
            raise MagnetSelectionError("smart selection journal filename does not match its id")
        return journal

    def paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.glob("*.json"))

    def remove(self, selection_id: str) -> None:
        try:
            self.path_for(selection_id).unlink(missing_ok=True)
        except OSError as exc:
            raise MagnetSelectionError("cannot remove smart selection journal") from exc


def normalize_selection_magnets(
    values: list[str] | tuple[str, ...],
) -> tuple[SelectionMagnet, ...]:
    if (
        not isinstance(values, (list, tuple))
        or not values
        or len(values) > PROBE_MAX_MAGNETS
    ):
        raise MagnetSelectionError(
            f"provide between 1 and {PROBE_MAX_MAGNETS} magnets"
        )
    magnets: list[SelectionMagnet] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise MagnetSelectionError("each smart selection magnet must be a string")
        uri = value.strip()
        if "\r" in uri or "\n" in uri:
            raise MagnetSelectionError("smart selection magnet cannot contain line breaks")
        try:
            parsed = parse_magnet(uri)
        except MagnetError as exc:
            raise MagnetSelectionError(str(exc)) from exc
        if parsed.info_hash in seen:
            continue
        seen.add(parsed.info_hash)
        magnets.append(
            SelectionMagnet(
                uri=uri,
                info_hash=parsed.info_hash,
                display_name=parsed.display_name,
            )
        )
    if not magnets:
        raise MagnetSelectionError("at least one unique smart selection magnet is required")
    return tuple(magnets)


class MagnetSelectionService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], QbittorrentClient] | None = None,
        journal_store: SelectionJournalStore | None = None,
        ledger_store: SelectionLedgerStore | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = DEFAULT_SELECTION_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_SELECTION_POLL_SECONDS,
        minimum_observation_seconds: float = DEFAULT_SELECTION_MINIMUM_OBSERVATION_SECONDS,
        scheduling_grace_seconds: float = DEFAULT_SELECTION_SCHEDULING_GRACE_SECONDS,
        on_selected: Callable[[str, str], None] | None = None,
    ) -> None:
        self._client_factory = client_factory or (
            lambda: QbittorrentClient(AppConfig.from_env().qbittorrent)
        )
        supplied_journal_store = journal_store is not None
        self.journal_store = journal_store or SelectionJournalStore()
        if ledger_store is None:
            ledger_path = (
                self.journal_store.root / "candidate-ledger.sqlite3"
                if supplied_journal_store
                else runtime_config_path().parent / "qb-smart-selections.sqlite3"
            )
            ledger_store = SelectionLedgerStore(ledger_path, clock=wall_clock)
        self.ledger_store = ledger_store
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep
        self.timeout_seconds = max(
            1.0,
            min(float(timeout_seconds), MAX_SELECTION_TIMEOUT_SECONDS),
        )
        self.poll_interval_seconds = max(0.25, min(float(poll_interval_seconds), 10.0))
        self.minimum_observation_seconds = max(
            0.0,
            min(float(minimum_observation_seconds), self.timeout_seconds),
        )
        self.scheduling_grace_seconds = max(
            1.0,
            min(float(scheduling_grace_seconds), MAX_SELECTION_SCHEDULING_GRACE_SECONDS),
        )
        self._on_selected = on_selected

    def run(
        self,
        magnets: list[str] | tuple[str, ...],
        *,
        selection_id: str | None = None,
        cancel_event: threading.Event | None = None,
        on_update: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        parsed = normalize_selection_magnets(magnets)
        clean_id = selection_id or uuid.uuid4().hex
        if not SELECTION_ID_RE.fullmatch(clean_id):
            raise MagnetSelectionError("invalid smart selection id")
        cancel = cancel_event or threading.Event()
        started = self._monotonic()
        result: dict[str, object] = {
            "ok": True,
            "selection_id": clean_id,
            "purpose": "smart",
            "status": "running",
            "total": len(parsed),
            "progress": {
                "resolved": 0,
                "total": len(parsed),
                "elapsed_ms": 0,
                "timeout_ms": int(self.timeout_seconds * 1000),
            },
            "items": [],
            "selection": {
                "status": "pending",
                "selected_info_hash": None,
                "selected_name": None,
                "selected_quality": None,
            },
            "cleanup": None,
        }
        self._emit(on_update, result)

        client: QbittorrentClient | None = None
        journal: SelectionJournal | None = None
        journal_written = False
        ledger_created = False
        preexisting: dict[str, dict[str, object]] = {}
        observations: dict[str, dict[str, object]] = {}
        selected_hash: str | None = None
        timed_out = False
        inconclusive = False
        candidates: tuple[SelectionMagnet, ...] = ()
        deleted_hashes: set[str] = set()
        retained_hashes: set[str] = set()

        def update_view(*, final: bool = False) -> None:
            result["items"] = _selection_items(
                parsed,
                preexisting,
                observations,
                selected_hash=selected_hash,
                final=final,
            )
            result["progress"] = _selection_progress(
                result["items"],
                len(parsed),
                started,
                self._monotonic(),
                self.timeout_seconds,
            )

        try:
            client = self._client_factory()
            all_hashes = [item.info_hash for item in parsed]
            preexisting = client.probe_torrent_snapshots(all_hashes)
            _merge_selection_snapshots(observations, preexisting)
            candidates = tuple(item for item in parsed if item.info_hash not in preexisting)

            category, save_path = resolve_download_destination(
                AddDownloadRequest(magnet=parsed[0].uri),
                client.config,
            )
            tag = f"{SELECTION_TAG_PREFIX}{clean_id}"
            created_at = max(1, int(self._wall_clock()))
            ledger_candidates = tuple(
                {
                    "info_hash": item.info_hash,
                    "display_name": item.display_name,
                    "origin": "preexisting" if item.info_hash in preexisting else "temporary",
                }
                for item in parsed
            )
            self.ledger_store.create(
                clean_id,
                created_at=created_at,
                category=category,
                tag=tag,
                save_path=save_path,
                candidates=ledger_candidates,
            )
            ledger_created = True
            if preexisting:
                self.ledger_store.record_snapshots(clean_id, preexisting)
            update_view()
            self._emit(on_update, result)

            if cancel.is_set():
                result["status"] = "cancelled"
                return self._finish_result(result, update_view, final=True)
            if not candidates and not preexisting:
                result["ok"] = False
                result["status"] = "failed"
                result["failure_kind"] = "selection"
                result["error"] = "no candidate magnet was added to qBittorrent"
                result["selection"] = _selection_failure_payload(
                    timed_out=False,
                    inconclusive=False,
                )
                return self._finish_result(result, update_view, final=True)

            if candidates:
                journal = SelectionJournal(
                    selection_id=clean_id,
                    created_at=created_at,
                    category=category,
                    tag=tag,
                    save_path=save_path,
                    hashes=tuple(item.info_hash for item in candidates),
                )
                self.journal_store.write(journal)
                journal_written = True
                client.ensure_smart_selection_tag(tag)
                try:
                    raw_batch_limit = client.smart_selection_batch_limit(len(candidates))
                    batch_limit = int(raw_batch_limit)
                except Exception:
                    batch_limit = min(len(candidates), 4)
                batch_limit = max(1, min(len(candidates), batch_limit))
                pending_batches: list[list[SelectionMagnet]] = [
                    list(candidates[start : start + batch_limit])
                    for start in range(0, len(candidates), batch_limit)
                ]
                batch_no = 0
                while pending_batches and not cancel.is_set():
                    batch = pending_batches.pop(0)
                    batch_no += 1
                    batch_hashes = tuple(item.info_hash for item in batch)
                    journal = SelectionJournal(
                        **{
                            **journal.__dict__,
                            "created_at": max(1, int(self._wall_clock())),
                        }
                    )
                    self.journal_store.write(journal, replace=True)
                    self.ledger_store.begin_batch(
                        clean_id,
                        batch_hashes,
                        batch_no=batch_no,
                    )
                    client.add_smart_selection_magnets(
                        [item.uri for item in batch],
                        tag=tag,
                    )
                    batch_started = self._monotonic()
                    deadline = batch_started + self.timeout_seconds
                    while True:
                        if cancel.is_set():
                            result["status"] = "cancelled"
                            break
                        snapshots = client.probe_torrent_snapshots(
                            list(batch_hashes),
                            timeout_seconds=min(5.0, max(0.5, deadline - self._monotonic())),
                        )
                        _merge_selection_snapshots(observations, snapshots)
                        self.ledger_store.record_snapshots(clean_id, snapshots)
                        _merge_ledger_observations(
                            observations,
                            self.ledger_store.records(clean_id),
                        )
                        update_view()
                        self._emit(on_update, result)
                        now = self._monotonic()
                        processed, pending = _partition_batch_candidates(
                            batch_hashes,
                            observations,
                        )
                        active_metadata = [
                            info_hash
                            for info_hash in processed
                            if _snapshot_active_metadata(observations.get(info_hash, {}))
                        ]
                        elapsed = now - batch_started
                        if (
                            not active_metadata
                            and not pending
                            and elapsed >= self.minimum_observation_seconds
                        ):
                            break
                        if (
                            pending
                            and not active_metadata
                            and elapsed >= self.scheduling_grace_seconds
                            and len(batch) > 1
                        ):
                            break
                        if now >= deadline:
                            timed_out = True
                            break
                        self._sleep(
                            min(self.poll_interval_seconds, max(0.0, deadline - now))
                        )

                    if result.get("status") == "cancelled":
                        break
                    final_states: dict[str, str] = {}
                    deferred: list[SelectionMagnet] = []
                    for item in batch:
                        snapshot = observations.get(item.info_hash, {})
                        if bool(snapshot.get("_metadata_received")) and not _snapshot_deferred(
                            snapshot
                        ):
                            final_states[item.info_hash] = (
                                "observed" if _selection_usable(snapshot) else "unavailable"
                            )
                        elif _snapshot_was_attempted(snapshot):
                            final_states[item.info_hash] = "unavailable"
                        else:
                            final_states[item.info_hash] = "deferred"
                            deferred.append(item)
                    self.ledger_store.mark_candidates(clean_id, final_states)
                    if deferred:
                        if len(batch) > 1 or len(deferred) > 1:
                            middle = max(1, len(deferred) // 2)
                            if deferred[middle:]:
                                pending_batches.insert(0, deferred[middle:])
                            if deferred[:middle]:
                                pending_batches.insert(0, deferred[:middle])
                        else:
                            inconclusive = True

                    # Keep the final fully observed batch in qBittorrent. The
                    # final cleanup can then remove only losers, and a winner
                    # from an earlier batch is re-added once after comparison.
                    retain_batch = not deferred and not pending_batches
                    if retain_batch:
                        retained_hashes.update(batch_hashes)
                        cleanup = {"deleted": [], "skipped": [], "remaining": []}
                    else:
                        cleanup = client.delete_owned_smart_selection_torrents(
                            list(batch_hashes),
                            tag=tag,
                            added_after=journal.added_after,
                            added_before=journal.added_before,
                        )
                        for info_hash in cleanup.get("deleted", []):
                            clean_hash = str(info_hash or "").strip().lower()
                            if clean_hash:
                                deleted_hashes.add(clean_hash)
                        if cleanup.get("remaining") or cleanup.get("skipped"):
                            raise MagnetSelectionError(
                                "one or more smart selection batch torrents could not be safely removed"
                            )
                    update_view()
                    self._emit(on_update, result)

                if cancel.is_set():
                    result["status"] = "cancelled"
                elif pending_batches or inconclusive:
                    result["ok"] = False
                    result["status"] = "failed"
                    result["failure_kind"] = "selection"
                    result["error"] = "smart selection could not schedule every candidate"
                    result["selection"] = _selection_failure_payload(
                        timed_out=timed_out,
                        inconclusive=True,
                    )

            # A preexisting torrent is never force-started. It is allowed to
            # remain unresolved, which is reported as inconclusive instead of
            # being silently treated as an unavailable source.
            unresolved_preexisting = [
                item.info_hash
                for item in parsed
                if item.info_hash in preexisting
                and (
                    not bool(observations.get(item.info_hash, {}).get("_metadata_received"))
                    or _snapshot_deferred(observations.get(item.info_hash, {}))
                )
            ]
            preexisting_needs_observation = any(
                not bool(observations.get(info_hash, {}).get("_metadata_received"))
                or not _selection_usable(observations.get(info_hash, {}))
                for info_hash in preexisting
            )
            if (
                preexisting
                and preexisting_needs_observation
                and result.get("status") not in {"cancelled", "failed"}
            ):
                preexisting_started = self._monotonic()
                preexisting_deadline = preexisting_started + self.timeout_seconds
                while not cancel.is_set():
                    snapshots = client.probe_torrent_snapshots(
                        list(preexisting),
                        timeout_seconds=min(5.0, max(0.5, preexisting_deadline - self._monotonic())),
                    )
                    _merge_selection_snapshots(observations, snapshots)
                    self.ledger_store.record_snapshots(clean_id, snapshots)
                    _merge_ledger_observations(observations, self.ledger_store.records(clean_id))
                    unresolved_preexisting = [
                        info_hash
                        for info_hash in unresolved_preexisting
                        if (
                            not bool(observations.get(info_hash, {}).get("_metadata_received"))
                            or _snapshot_deferred(observations.get(info_hash, {}))
                        )
                    ]
                    update_view()
                    self._emit(on_update, result)
                    elapsed = self._monotonic() - preexisting_started
                    if (
                        (not unresolved_preexisting and elapsed >= self.minimum_observation_seconds)
                        or self._monotonic() >= preexisting_deadline
                    ):
                        break
                    self._sleep(
                        min(
                            self.poll_interval_seconds,
                            max(0.0, preexisting_deadline - self._monotonic()),
                        )
                    )
                if cancel.is_set():
                    result["status"] = "cancelled"
                if unresolved_preexisting and self._monotonic() >= preexisting_deadline:
                    timed_out = True
            preexisting_states: dict[str, str] = {}
            for item in parsed:
                if item.info_hash not in preexisting:
                    continue
                snapshot = observations.get(item.info_hash, {})
                if bool(snapshot.get("_metadata_received")) and not _snapshot_deferred(
                    snapshot
                ):
                    preexisting_states[item.info_hash] = (
                        "observed" if _selection_usable(snapshot) else "unavailable"
                    )
                elif _snapshot_was_attempted(snapshot):
                    preexisting_states[item.info_hash] = "unavailable"
                else:
                    preexisting_states[item.info_hash] = "deferred"
                    inconclusive = True
            self.ledger_store.mark_candidates(clean_id, preexisting_states)

            _merge_ledger_observations(observations, self.ledger_store.records(clean_id))
            ledger_records = self.ledger_store.records(clean_id)
            unresolved = [
                record
                for record in ledger_records.values()
                if str(record.get("ledger_state") or "") not in TERMINAL_CANDIDATE_STATES
            ]
            if cancel.is_set() and result.get("status") not in {"cancelled", "failed"}:
                result["status"] = "cancelled"
            if result.get("status") == "cancelled":
                pass
            elif unresolved or inconclusive:
                result["ok"] = False
                result["status"] = "failed"
                result["failure_kind"] = "selection"
                result["error"] = "smart selection could not compare every candidate"
                result["selection"] = _selection_failure_payload(
                    timed_out=timed_out,
                    inconclusive=True,
                )
            else:
                selected_hash = _select_best_hash(parsed, observations)
                if selected_hash is not None:
                    selected = observations.get(selected_hash, {})
                    selected_quality = _quality_label(str(selected.get("name") or "")) or _quality_label(
                        next(
                            (item.display_name or "" for item in parsed if item.info_hash == selected_hash),
                            "",
                        )
                    )
                    result["selection"] = {
                        "status": "selected",
                        "selected_info_hash": selected_hash,
                        "selected_name": selected.get("name"),
                        "selected_quality": selected_quality,
                        "timed_out": timed_out,
                    }
                    self.ledger_store.mark_candidates(
                        clean_id,
                        {
                            item.info_hash: (
                                "selected"
                                if item.info_hash == selected_hash
                                else "discarded"
                                if _selection_usable(observations.get(item.info_hash, {}))
                                else str(ledger_records.get(item.info_hash, {}).get("ledger_state") or "unavailable")
                            )
                            for item in parsed
                        },
                    )
                    self.ledger_store.mark_selection(
                        clean_id,
                        status="complete",
                        selected_hash=selected_hash,
                    )
                    if (
                        selected_hash in {item.info_hash for item in candidates}
                        and selected_hash not in retained_hashes
                    ):
                        selected_uri = next(
                            item.uri for item in candidates if item.info_hash == selected_hash
                        )
                        # Re-add the winner only after every probe batch has
                        # been deleted. This is the single persistent task
                        # left for the normal download path.
                        client.add_smart_selection_magnets([selected_uri], tag=tag)
                    if journal is not None:
                        next_journal = SelectionJournal(
                            **{
                                **journal.__dict__,
                                "selected_hash": selected_hash,
                            }
                        )
                        self.journal_store.write(next_journal, replace=True)
                        journal = next_journal
                    if self._on_selected is not None:
                        try:
                            self._on_selected(selected_hash, selected_quality or "")
                        except Exception:
                            pass
                    result["status"] = "complete"
                else:
                    result["ok"] = False
                    result["status"] = "failed"
                    result["failure_kind"] = "selection"
                    result["error"] = "no usable smart selection source was observed"
                    result["selection"] = _selection_failure_payload(
                        timed_out=timed_out,
                        inconclusive=False,
                    )
                    self.ledger_store.mark_selection(clean_id, status="failed")
            update_view(final=True)
        except Exception as exc:
            result["ok"] = False
            result["status"] = "failed"
            result["failure_kind"] = "infrastructure"
            result["error"] = _safe_error(exc)
            try:
                if ledger_created:
                    self.ledger_store.mark_selection(clean_id, status="failed")
            except Exception:
                pass
            update_view(final=True)
        finally:
            if ledger_created:
                try:
                    self.ledger_store.mark_selection(
                        clean_id,
                        status="cleaning" if result.get("status") != "cancelled" else "cancelled",
                        selected_hash=journal.selected_hash if journal is not None else selected_hash,
                    )
                except Exception:
                    pass
            if journal_written and journal is not None and client is not None:
                self._emit(on_update, {**result, "status": "cleaning"})
                cleanup_hashes = [
                    info_hash
                    for info_hash in journal.hashes
                    if info_hash != journal.selected_hash
                ]
                try:
                    cleanup = (
                        client.delete_owned_smart_selection_torrents(
                            cleanup_hashes,
                            tag=journal.tag,
                            added_after=journal.added_after,
                            added_before=journal.added_before,
                        )
                        if cleanup_hashes
                        else {
                            "deleted": [],
                            "skipped": [],
                            "remaining": [],
                        }
                    )
                    for info_hash in cleanup.get("deleted", []):
                        clean_hash = str(info_hash or "").strip().lower()
                        if clean_hash:
                            deleted_hashes.add(clean_hash)
                    if journal.selected_hash and journal.selected_hash in journal.hashes:
                        try:
                            client.remove_torrent_tags(
                                [journal.selected_hash],
                                tag=journal.tag,
                            )
                        except Exception as exc:
                            cleanup["tag_error"] = _safe_error(exc)
                    remaining = list(cleanup.get("remaining", []))
                    skipped = list(cleanup.get("skipped", []))
                    tag_error = cleanup.get("tag_error")
                    if not remaining and not skipped and not tag_error:
                        if ledger_created:
                            self.ledger_store.remove(journal.selection_id)
                        self.journal_store.remove(journal.selection_id)
                        cleanup_status = "complete"
                    else:
                        cleanup_status = "incomplete"
                    result["cleanup"] = {
                        "status": cleanup_status,
                        "deleted": len(deleted_hashes),
                        "skipped": len(skipped),
                        "remaining": remaining,
                    }
                    if tag_error:
                        result["cleanup"]["error"] = tag_error
                except Exception as exc:
                    result["cleanup"] = {
                        "status": "incomplete",
                        "deleted": len(deleted_hashes),
                        "skipped": 0,
                        "error": _safe_error(exc),
                    }
            elif result.get("cleanup") is None:
                result["cleanup"] = {
                    "status": "not_required",
                    "deleted": 0,
                    "skipped": 0,
                }
            if ledger_created and not journal_written:
                try:
                    self.ledger_store.remove(clean_id)
                except Exception:
                    pass
            update_view(final=True)
            self._emit(on_update, result)
        if result.get("status") == "cancelled":
            result["failure_kind"] = "cancelled"
        return copy.deepcopy(result)

    def _finish_result(
        self,
        result: dict[str, object],
        update_view: Callable[..., None],
        *,
        final: bool,
    ) -> dict[str, object]:
        if result.get("status") == "cancelled":
            result["failure_kind"] = "cancelled"
        update_view(final=final)
        result["cleanup"] = {
            "status": "not_required",
            "deleted": 0,
            "skipped": 0,
        }
        self._emit(None, result)
        return copy.deepcopy(result)

    def reap_stale(self, *, max_age_seconds: int = SELECTION_STALE_AGE_SECONDS) -> dict[str, object]:
        safe_age = max(300, min(int(max_age_seconds), 86_400))
        now = int(self._wall_clock())
        cleaned: list[str] = []
        skipped: list[str] = []
        ledger_cleaned: list[str] = []
        errors: list[dict[str, str]] = []
        client: QbittorrentClient | None = None
        journal_ids: set[str] = set()
        for path in self.journal_store.paths():
            try:
                journal = self.journal_store.read(path)
            except MagnetSelectionError as exc:
                errors.append({"journal": path.name, "error": _safe_error(exc)})
                continue
            journal_ids.add(journal.selection_id)
            if now - journal.created_at < safe_age:
                skipped.append(journal.selection_id)
                continue
            try:
                if client is None:
                    client = self._client_factory()
                cleanup_hashes = [
                    info_hash
                    for info_hash in journal.hashes
                    if info_hash != journal.selected_hash
                ]
                cleanup = client.delete_owned_smart_selection_torrents(
                    cleanup_hashes,
                    tag=journal.tag,
                    added_after=journal.added_after,
                    added_before=journal.added_before,
                )
                if cleanup.get("remaining") or cleanup.get("skipped"):
                    raise MagnetSelectionError(
                        "one or more stale smart selection torrents could not be safely removed"
                    )
                if journal.selected_hash and journal.selected_hash in journal.hashes:
                    client.remove_torrent_tags([journal.selected_hash], tag=journal.tag)
                self.ledger_store.remove(journal.selection_id)
                self.journal_store.remove(journal.selection_id)
                cleaned.append(journal.selection_id)
            except Exception as exc:
                errors.append({"journal": path.name, "error": _safe_error(exc)})
        try:
            cutoff = float(now - safe_age)
            for run in self.ledger_store.stale_runs(older_than=cutoff):
                selection_id = str(run.get("selection_id") or "")
                if not selection_id or selection_id in journal_ids:
                    continue
                try:
                    records = self.ledger_store.records(selection_id)
                    selected_hash = str(run.get("selected_hash") or "").strip().lower()
                    cleanup_hashes = [
                        info_hash
                        for info_hash in records
                        if info_hash != selected_hash
                    ]
                    if cleanup_hashes or selected_hash:
                        if client is None:
                            client = self._client_factory()
                        created_at = max(1, int(run.get("created_at") or 0))
                        cleanup = client.delete_owned_smart_selection_torrents(
                            cleanup_hashes,
                            tag=str(run.get("tag") or ""),
                            added_after=max(1, created_at - SELECTION_ADDED_ON_GRACE_SECONDS),
                            added_before=created_at + 3_590,
                        )
                        if cleanup.get("remaining") or cleanup.get("skipped"):
                            raise MagnetSelectionError(
                                "one or more stale smart selection torrents could not be safely removed"
                            )
                        if selected_hash and selected_hash in records:
                            client.remove_torrent_tags(
                                [selected_hash],
                                tag=str(run.get("tag") or ""),
                            )
                    self.ledger_store.remove(selection_id)
                    ledger_cleaned.append(selection_id)
                except Exception as exc:
                    errors.append({"ledger": selection_id, "error": _safe_error(exc)})
        except Exception as exc:
            errors.append({"ledger": "smart-selection", "error": _safe_error(exc)})
        return {
            "cleaned": cleaned,
            "ledger_cleaned": ledger_cleaned,
            "skipped": skipped,
            "errors": errors,
        }

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


class MagnetSelectionManager:
    def __init__(
        self,
        service: MagnetSelectionService,
        *,
        completed_ttl_seconds: int = 1_800,
        max_jobs: int = 24,
        reserve_hashes: Callable[[tuple[str, ...]], object] | None = None,
        release_hashes: Callable[[object], None] | None = None,
    ) -> None:
        if (reserve_hashes is None) != (release_hashes is None):
            raise ValueError(
                "reserve_hashes and release_hashes must be supplied together"
            )
        self.service = service
        self.completed_ttl_seconds = max(60, min(int(completed_ttl_seconds), 7_200))
        self.max_jobs = max(1, min(int(max_jobs), 100))
        self._reserve_hashes = reserve_hashes
        self._release_hashes = release_hashes
        self._jobs: dict[str, dict[str, object]] = {}
        self._completed_at: dict[str, float] = {}
        self._controls: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._requested_hashes: dict[str, frozenset[str]] = {}
        self._job_hashes: dict[str, frozenset[str]] = {}
        self._completion_callbacks: dict[str, SelectionCompletion] = {}
        self._reservations: dict[str, object] = {}
        self._active_id: str | None = None
        self._stopping = False
        self._lock = threading.RLock()

    def start(
        self,
        magnets: list[str] | tuple[str, ...],
        *,
        selection_id: str | None = None,
        on_start: SelectionStart | None = None,
        on_complete: SelectionCompletion | None = None,
    ) -> dict[str, object]:
        parsed = normalize_selection_magnets(magnets)
        clean_selection_id = str(selection_id or uuid.uuid4().hex).strip().lower()
        if not SELECTION_ID_RE.fullmatch(clean_selection_id):
            raise MagnetSelectionError("invalid smart selection id")
        requested_hashes = frozenset(item.info_hash for item in parsed)
        with self._lock:
            self._purge_locked()
            if self._stopping:
                raise MagnetSelectionError("smart selection manager is shutting down")
            existing = self._jobs.get(clean_selection_id)
            if existing is not None:
                if self._job_hashes.get(clean_selection_id) != requested_hashes:
                    raise MagnetSelectionConflictError(
                        "smart selection identity was already used for different magnets"
                    )
                if (
                    on_complete is not None
                    and clean_selection_id in self._controls
                    and clean_selection_id not in self._completion_callbacks
                ):
                    self._completion_callbacks[clean_selection_id] = on_complete
                return copy.deepcopy(existing)
            if self._active_id is not None:
                raise MagnetSelectionBusyError("another smart selection is already running")
            reservation: object | None = None
            if self._reserve_hashes is not None:
                try:
                    reservation = self._reserve_hashes(
                        tuple(item.info_hash for item in parsed)
                    )
                except MagnetSelectionConflictError:
                    raise
                except Exception as exc:
                    raise MagnetSelectionConflictError(
                        "another magnet operation is using one of these hashes"
                    ) from exc
            if on_start is not None:
                try:
                    on_start(clean_selection_id)
                except Exception:
                    if reservation is not None:
                        self._release_reservation(reservation)
                    raise
            selection_id = clean_selection_id
            control = threading.Event()
            initial: dict[str, object] = {
                "ok": True,
                "selection_id": selection_id,
                "purpose": "smart",
                "status": "queued",
                "total": len(parsed),
                "progress": {
                    "resolved": 0,
                    "total": len(parsed),
                    "elapsed_ms": 0,
                    "timeout_ms": int(self.service.timeout_seconds * 1000),
                },
                "items": [],
                "selection": {
                    "status": "pending",
                    "selected_info_hash": None,
                    "selected_name": None,
                    "selected_quality": None,
                },
                "cleanup": None,
            }
            self._jobs[selection_id] = initial
            self._controls[selection_id] = control
            self._requested_hashes[selection_id] = frozenset(
                item.info_hash for item in parsed
            )
            self._job_hashes[selection_id] = requested_hashes
            if on_complete is not None:
                self._completion_callbacks[selection_id] = on_complete
            if reservation is not None:
                self._reservations[selection_id] = reservation
            self._active_id = selection_id
            thread = threading.Thread(
                target=self._run_job,
                args=(selection_id, tuple(item.uri for item in parsed), control),
                name=f"jav-smart-selection-{selection_id[:8]}",
                daemon=True,
            )
            self._threads[selection_id] = thread
            try:
                thread.start()
            except Exception as exc:
                self._threads.pop(selection_id, None)
                self._controls.pop(selection_id, None)
                self._jobs.pop(selection_id, None)
                self._requested_hashes.pop(selection_id, None)
                self._job_hashes.pop(selection_id, None)
                self._completion_callbacks.pop(selection_id, None)
                reservation = self._reservations.pop(selection_id, None)
                if self._active_id == selection_id:
                    self._active_id = None
                if reservation is not None:
                    self._release_reservation(reservation)
                raise MagnetSelectionError(_safe_error(exc)) from exc
            return copy.deepcopy(initial)

    def get(self, selection_id: str) -> dict[str, object]:
        clean_id = str(selection_id or "")
        if not SELECTION_ID_RE.fullmatch(clean_id):
            raise MagnetSelectionError("invalid smart selection id")
        with self._lock:
            self._purge_locked()
            job = self._jobs.get(clean_id)
            if job is None:
                raise MagnetSelectionNotFoundError("smart selection was not found")
            return copy.deepcopy(job)

    def conflicts(self, hashes: list[str] | tuple[str, ...]) -> bool:
        requested = {
            str(value or "").strip().lower()
            for value in hashes
            if str(value or "").strip()
        }
        if not requested:
            return False
        with self._lock:
            self._purge_locked()
            return any(
                requested.intersection(active_hashes)
                for active_hashes in self._requested_hashes.values()
            )

    def cancel(self, selection_id: str) -> bool:
        clean_id = str(selection_id or "")
        if not SELECTION_ID_RE.fullmatch(clean_id):
            raise MagnetSelectionError("invalid smart selection id")
        with self._lock:
            control = self._controls.get(clean_id)
            if control is None:
                if clean_id in self._jobs:
                    return False
                raise MagnetSelectionNotFoundError("smart selection was not found")
            control.set()
            job = self._jobs.get(clean_id)
            if job and job.get("status") in {"queued", "running"}:
                job["status"] = "cancelling"
            return True

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

    def reap_stale(self, *, max_age_seconds: int = SELECTION_STALE_AGE_SECONDS) -> dict[str, object]:
        return self.service.reap_stale(max_age_seconds=max_age_seconds)

    def _run_job(
        self,
        selection_id: str,
        magnets: tuple[str, ...],
        control: threading.Event,
    ) -> None:
        try:
            result = self.service.run(
                magnets,
                selection_id=selection_id,
                cancel_event=control,
                on_update=lambda payload: self._update_job(selection_id, payload),
            )
        except Exception as exc:
            result = {
                "ok": False,
                "selection_id": selection_id,
                "purpose": "smart",
                "status": "failed",
                "failure_kind": "infrastructure",
                "total": len(magnets),
                "error": _safe_error(exc),
                "items": [],
                "cleanup": {"status": "unknown"},
            }
        with self._lock:
            completion = self._completion_callbacks.get(selection_id)
        if completion is not None:
            try:
                outcome = completion(copy.deepcopy(result))
                if outcome is not None:
                    result["replacement"] = dict(outcome)
            except Exception as exc:
                result["replacement_error"] = _safe_error(exc)
        with self._lock:
            reservation = self._reservations.pop(selection_id, None)
            self._completion_callbacks.pop(selection_id, None)
            self._jobs[selection_id] = copy.deepcopy(result)
            self._completed_at[selection_id] = time.monotonic()
            self._controls.pop(selection_id, None)
            self._threads.pop(selection_id, None)
            self._requested_hashes.pop(selection_id, None)
            if self._active_id == selection_id:
                self._active_id = None
            self._purge_locked()
        if reservation is not None:
            self._release_reservation(reservation)

    def _release_reservation(self, reservation: object) -> None:
        if self._release_hashes is None:
            return
        try:
            self._release_hashes(reservation)
        except Exception:
            pass

    def _update_job(self, selection_id: str, payload: dict[str, object]) -> None:
        with self._lock:
            if selection_id in self._jobs:
                updated = copy.deepcopy(payload)
                if (
                    selection_id in self._completion_callbacks
                    and updated.get("status") in {"complete", "failed", "cancelled"}
                ):
                    updated["status"] = "cleaning"
                if (
                    self._jobs[selection_id].get("status") == "cancelling"
                    and updated.get("status") in {"queued", "running"}
                ):
                    updated["status"] = "cancelling"
                self._jobs[selection_id] = updated

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [
            selection_id
            for selection_id, completed_at in self._completed_at.items()
            if completed_at + self.completed_ttl_seconds <= now
        ]
        for selection_id in expired:
            self._jobs.pop(selection_id, None)
            self._completed_at.pop(selection_id, None)
            self._requested_hashes.pop(selection_id, None)
            self._job_hashes.pop(selection_id, None)
            self._completion_callbacks.pop(selection_id, None)
        while len(self._jobs) > self.max_jobs:
            removable = next(
                (selection_id for selection_id in self._jobs if selection_id in self._completed_at),
                None,
            )
            if removable is None:
                break
            self._jobs.pop(removable, None)
            self._completed_at.pop(removable, None)
            self._requested_hashes.pop(removable, None)
            self._job_hashes.pop(removable, None)
            self._completion_callbacks.pop(removable, None)


def _selection_failure_payload(
    *,
    timed_out: bool,
    inconclusive: bool,
) -> dict[str, object]:
    return {
        "status": "inconclusive" if inconclusive else "not_found",
        "selected_info_hash": None,
        "selected_name": None,
        "selected_quality": None,
        "timed_out": timed_out,
    }


def _partition_batch_candidates(
    hashes: tuple[str, ...],
    observations: Mapping[str, Mapping[str, object]],
) -> tuple[list[str], list[str]]:
    processed: list[str] = []
    pending: list[str] = []
    for info_hash in hashes:
        if _snapshot_was_attempted(observations.get(info_hash, {})):
            processed.append(info_hash)
        else:
            pending.append(info_hash)
    return processed, pending


def _snapshot_was_attempted(snapshot: Mapping[str, object]) -> bool:
    if bool(snapshot.get("attempted")):
        return True
    state = str(snapshot.get("state") or "")
    if state in {
        "metaDL",
        "forcedMetaDL",
        "downloadingMetadata",
        "downloading",
        "forcedDL",
        "stalledDL",
        "stalledUP",
        "allocating",
        "checkingDL",
        "checkingUP",
        "checkingResumeData",
        "moving",
        "error",
        "missingFiles",
    }:
        return True
    return bool(snapshot.get("_metadata_received")) and not _snapshot_deferred(snapshot)


def _snapshot_deferred(snapshot: Mapping[str, object]) -> bool:
    """Return whether qB has metadata but has not actually scheduled this candidate."""

    if bool(snapshot.get("attempted")) or _selection_usable(dict(snapshot)):
        return False
    return str(snapshot.get("state") or "") in {
        "queuedDL",
        "queuedUP",
        "pausedDL",
        "pausedUP",
        "stoppedDL",
        "stoppedUP",
        "unknown",
    }


def _snapshot_active_metadata(snapshot: Mapping[str, object]) -> bool:
    if bool(snapshot.get("_metadata_received")):
        return False
    return str(snapshot.get("state") or "") in {
        "metaDL",
        "forcedMetaDL",
        "downloadingMetadata",
        "downloading",
        "forcedDL",
        "stalledDL",
        "stalledUP",
        "allocating",
        "checkingDL",
        "checkingUP",
        "checkingResumeData",
        "moving",
    }


def _merge_ledger_observations(
    observations: dict[str, dict[str, object]],
    records: Mapping[str, Mapping[str, object]],
) -> None:
    """Overlay durable states without discarding richer qB snapshots."""

    for info_hash, record in records.items():
        previous = observations.get(info_hash, {})
        merged = dict(previous)
        merged["ledger_state"] = record.get("ledger_state")
        merged["attempted"] = bool(record.get("attempted"))
        merged["_metadata_received"] = bool(
            previous.get("_metadata_received")
            or record.get("_metadata_received")
        )
        if not merged.get("state") and record.get("state"):
            merged["state"] = record.get("state")
        if not merged.get("name") and record.get("name"):
            merged["name"] = record.get("name")
        for field in (
            "seeders",
            "connected_seeders",
            "leechers",
            "availability",
            "download_speed",
            "peak_download_speed",
            "total_size",
        ):
            if merged.get(field) is None and record.get(field) is not None:
                merged[field] = record.get(field)
        observations[info_hash] = merged


def _select_best_hash(
    magnets: tuple[SelectionMagnet, ...],
    observations: dict[str, dict[str, object]],
) -> str | None:
    ranked: list[tuple[tuple[object, ...], str]] = []
    for magnet in magnets:
        snapshot = observations.get(magnet.info_hash, {})
        if not _selection_usable(snapshot):
            continue
        name = str(snapshot.get("name") or magnet.display_name or "")
        quality = _quality_rank(name)
        seeders = max(
            _nonnegative_int(snapshot.get("seeders")),
            _nonnegative_int(snapshot.get("connected_seeders")),
        )
        availability = _nonnegative_float(snapshot.get("availability"))
        speed = max(
            _nonnegative_int(snapshot.get("download_speed")),
            _nonnegative_int(snapshot.get("peak_download_speed")),
        )
        ranked.append(
            (
                (
                    quality[0],
                    quality[1],
                    seeders,
                    _nonnegative_int(snapshot.get("connected_seeders")),
                    availability,
                    speed,
                    _nonnegative_int(snapshot.get("total_size")),
                    name.casefold(),
                    magnet.info_hash,
                ),
                magnet.info_hash,
            )
        )
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def _selection_usable(snapshot: dict[str, object]) -> bool:
    if not snapshot:
        return False
    if _nonnegative_int(snapshot.get("seeders")) > 0:
        return True
    if _nonnegative_int(snapshot.get("connected_seeders")) > 0:
        return True
    if _nonnegative_float(snapshot.get("availability")) >= 1.0:
        return True
    return _nonnegative_int(snapshot.get("download_speed")) > 0


def _selection_items(
    magnets: tuple[SelectionMagnet, ...],
    preexisting: dict[str, dict[str, object]],
    observations: dict[str, dict[str, object]],
    *,
    selected_hash: str | None,
    final: bool,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for magnet in magnets:
        snapshot = observations.get(magnet.info_hash)
        origin = "preexisting" if magnet.info_hash in preexisting else "temporary"
        if snapshot is None:
            item: dict[str, object] = {
                "info_hash": magnet.info_hash,
                "origin": origin,
                "state": "timeout" if final else "waiting",
                "seeders": None,
                "connected_seeders": None,
                "leechers": None,
                "availability": None,
                "availability_status": "unknown",
                "metadata_received": False,
                "ledger_state": "pending",
                "download_speed": None,
                "peak_download_speed": None,
                "quality_rank": _quality_rank(magnet.display_name or "")[0],
                "quality_label": _quality_label(magnet.display_name or ""),
                "selected": False,
            }
            items.append(item)
            continue
        name = str(snapshot.get("name") or magnet.display_name or "")
        availability = _nonnegative_float(snapshot.get("availability"))
        ledger_state = str(snapshot.get("ledger_state") or "")
        display_state = (
            ledger_state
            if ledger_state in {"deferred", "unavailable", "observed", "selected", "discarded"}
            else str(snapshot.get("state") or "observed")
        )
        item = {
            "info_hash": magnet.info_hash,
            "origin": origin,
            "state": display_state,
            "name": snapshot.get("name") or magnet.display_name,
            "seeders": _optional(snapshot.get("seeders")),
            "connected_seeders": _optional(snapshot.get("connected_seeders")),
            "leechers": _optional(snapshot.get("leechers")),
            "availability": availability,
            "availability_status": (
                "complete_copy"
                if availability >= 1.0
                else "partial"
                if availability > 0
                else "none"
                if availability == 0
                else "unknown"
            ),
            "metadata_received": bool(snapshot.get("_metadata_received")),
            "ledger_state": ledger_state or None,
            "download_speed": _optional(snapshot.get("download_speed")),
            "peak_download_speed": _optional(snapshot.get("peak_download_speed")),
            "progress": _nonnegative_float(snapshot.get("progress")),
            "quality_rank": _quality_rank(name)[0],
            "quality_label": _quality_label(name),
            "selected": magnet.info_hash == selected_hash,
        }
        items.append(item)
    return items


def _selection_progress(
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
            (
                bool(item.get("metadata_received"))
                and not _snapshot_deferred(item)
            )
            or item.get("ledger_state") in TERMINAL_CANDIDATE_STATES
        )
    )
    return {
        "resolved": resolved,
        "total": total,
        "elapsed_ms": max(0, int((now - started) * 1000)),
        "timeout_ms": int(timeout_seconds * 1000),
    }


def _merge_selection_snapshots(
    observations: dict[str, dict[str, object]],
    snapshots: dict[str, dict[str, object]],
) -> None:
    for info_hash, snapshot in snapshots.items():
        previous = observations.get(info_hash, {})
        merged = dict(previous)
        merged.update(snapshot)
        merged["name"] = snapshot.get("name") or previous.get("name")
        for field in (
            "seeders",
            "connected_seeders",
            "leechers",
            "connected_leechers",
            "downloaded",
            "total_size",
            "peak_download_speed",
        ):
            values = [
                _optional_nonnegative_int(previous.get(field)),
                _optional_nonnegative_int(snapshot.get(field)),
            ]
            available = [value for value in values if value is not None]
            merged[field] = max(available) if available else None
        merged["download_speed"] = _optional_nonnegative_int(
            snapshot.get("download_speed")
        )
        if merged["download_speed"] is None:
            merged["download_speed"] = _optional_nonnegative_int(
                previous.get("download_speed")
            )
        merged["peak_download_speed"] = max(
            _nonnegative_int(previous.get("peak_download_speed")),
            _nonnegative_int(snapshot.get("peak_download_speed")),
            _nonnegative_int(snapshot.get("download_speed")),
        )
        merged["upload_speed"] = _optional_nonnegative_int(snapshot.get("upload_speed"))
        merged["progress"] = _nonnegative_float(snapshot.get("progress"))
        availability_values = [
            _optional_nonnegative_float(previous.get("availability")),
            _optional_nonnegative_float(snapshot.get("availability")),
        ]
        merged["availability"] = max(
            value for value in availability_values if value is not None
        ) if any(value is not None for value in availability_values) else None
        merged["_metadata_received"] = bool(previous.get("_metadata_received")) or bool(
            snapshot.get("_metadata_received")
        )
        observations[info_hash] = merged


def _quality_rank(value: str) -> tuple[int, int]:
    text = str(value or "").upper()
    resolution = 0
    for pattern, rank in (
        (r"(?<!\d)(?:4320|8K)(?:P)?(?!\d)", 6),
        (r"(?<!\d)(?:2160|4K)(?:P)?(?!\d)", 5),
        (r"(?<!\d)(?:1440|2K)(?:P)?(?!\d)", 4),
        (r"(?<!\d)1080(?:P|I)?(?!\d)|FHD", 3),
        (r"(?<!\d)720P?(?!\d)|\bHD\b", 2),
        (r"(?<!\d)(?:576|540|480|360)P?(?!\d)|\bSD\b", 1),
    ):
        if re.search(pattern, text):
            resolution = rank
            break
    codec = 0
    if re.search(r"\b(?:AV1|HEVC|X265|H265)\b", text):
        codec = 2
    elif re.search(r"\bX264\b|H264|AVC", text):
        codec = 1
    return resolution, codec


def _quality_label(value: str) -> str | None:
    resolution, codec = _quality_rank(value)
    labels = {6: "8K", 5: "4K", 4: "2K", 3: "1080p", 2: "720p", 1: "SD"}
    label = labels.get(resolution)
    if label and codec == 2:
        return f"{label} HEVC/AV1"
    if label:
        return label
    if codec == 2:
        return "HEVC/AV1"
    if codec == 1:
        return "H.264"
    return None


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_int(value: object) -> int:
    return _optional_nonnegative_int(value) or 0


def _optional_nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_float(value: object) -> float:
    return _optional_nonnegative_float(value) or 0.0


def _optional(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) and value >= 0 else None


def _validate_journal(journal: SelectionJournal) -> None:
    if not SELECTION_ID_RE.fullmatch(journal.selection_id):
        raise MagnetSelectionError("smart selection journal id is invalid")
    if journal.created_at <= 0:
        raise MagnetSelectionError("smart selection journal timestamp is invalid")
    if not journal.category or len(journal.category) > 120 or any(
        ord(character) < 32 for character in journal.category
    ):
        raise MagnetSelectionError("smart selection journal category is invalid")
    if not SELECTION_TAG_RE.fullmatch(journal.tag):
        raise MagnetSelectionError("smart selection journal tag is invalid")
    if not journal.save_path.startswith("/") or "\\" in journal.save_path or ".." in journal.save_path.split("/"):
        raise MagnetSelectionError("smart selection journal path is invalid")
    if not journal.hashes or len(journal.hashes) > PROBE_MAX_MAGNETS:
        raise MagnetSelectionError("smart selection journal hash count is invalid")
    if len(set(journal.hashes)) != len(journal.hashes) or any(
        not re.fullmatch(r"[0-9a-f]{40}", info_hash) for info_hash in journal.hashes
    ):
        raise MagnetSelectionError("smart selection journal hashes are invalid")
    if journal.selected_hash is not None and not re.fullmatch(
        r"[0-9a-f]{40}", journal.selected_hash
    ):
        raise MagnetSelectionError("smart selection journal selected hash is invalid")


def _safe_error(exc: BaseException) -> str:
    text = str(exc or "smart magnet selection failed")
    text = re.sub(r"magnet:\?[^\s]+", "[magnet]", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://[^\s]+", "[url]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])",
        "[info_hash]",
        text,
    )
    return text.replace("\r", " ").replace("\n", " ")[:300] or "smart magnet selection failed"
