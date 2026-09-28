"""Runs web download batches: discovery, preview, commit and job submission."""

from __future__ import annotations

import inspect
import math
import queue
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ...core.catalog_code import normalize_catalog_code
from ...missav import models as missav_models
from ...missav.browser_gate import MISSAV_BROWSER_GATE
from ..errors import WebDownloadConfigError, WebDownloadError
from ..jobs import normalize_web_download_code
from ..manager import WebDownloadManager
from ..policy import DEFAULT_EXISTING_POLICY
from ..providers import discover_exact_web_download_variant
from ..quality import discover_web_download_qualities, normalize_quality_heights
from ..variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
    MissavVariant,
    normalize_variant_priority,
    normalize_web_download_variant,
)
from .discovery import (
    BrokerMissavSeriesDiscoverer,
    SubprocessMissavSeriesDiscoverer,
    retryable_discovery_error,
)
from .errors import WebDownloadBatchConflictError, WebDownloadBatchError
from .intents import rule_selected_code_keys
from .library import (
    LibrarySnapshotChanged,
    current_media_library_revision,
    library_deduplication_snapshot,
    require_current_library_snapshot,
)
from .models import (
    DEFAULT_BATCH_PAGE_BUDGET,
    RESOURCE_SEARCH_SELECTION_PROVENANCE,
    SERIES_DISCOVERY_PROVENANCE,
    BatchRequest,
    LibraryDeduplicationSnapshot,
    SelectedBatchPlan,
)
from .store import WebDownloadBatchStore
from .validation import (
    auto_download_request_hash,
    hash_direct_queue_key,
    hash_preview_token,
    normalize_discovered_codes,
    normalize_selected_batch_items,
    optional_rule_revision,
    parse_batch_request,
    validate_batch_existing_policy,
    validate_direct_queue_hash,
    validate_intent_height,
    validate_item_quality_strategy,
    validate_max_height,
    validate_page_budget,
    validate_rule_id,
    validate_source_revision,
    validate_source_session_id,
)

MAX_QUALITY_WORKERS = 2


CLAIM_MAX_ATTEMPTS = 4
CLAIM_RETRY_BASE_SECONDS = 0.05
CLAIM_RETRY_MAX_SECONDS = 2.0
# The sequence is intentionally finite.  Each entry is one retry delay; the
# manager consumes entries in order and then surfaces the sanitized failure.
# Keep the default conservative so a transient site incident cannot keep a
# worker (or a queue row) alive forever.
DISCOVERY_RETRY_DELAYS = (1.0,)
MAX_DISCOVERY_RETRY_DELAYS = 8
QUEUED_RECOVERY_INTERVAL_SECONDS = 5.0
COMMIT_SNAPSHOT_MAX_ATTEMPTS = 3


ITEM_QUALITY_STATUSES = ("pending", "ready", "failed", "legacy")


def _quality_callable_accepts_cancel_event(discover: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(discover).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "cancel_event"
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        )
        for parameter in parameters
    )


def _quality_callable_accepts_variant(discover: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(discover).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "variant"
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        )
        for parameter in parameters
    )


class WebDownloadBatchManager:
    def __init__(
        self,
        download_manager: WebDownloadManager,
        *,
        store: WebDownloadBatchStore | None = None,
        discover: Callable[..., object] | None = None,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        library_snapshot: Callable[[], LibraryDeduplicationSnapshot] | None = None,
        discovery_retry_delays: Sequence[float] = DISCOVERY_RETRY_DELAYS,
        queued_recovery_interval: float = QUEUED_RECOVERY_INTERVAL_SECONDS,
        page_budget: object = DEFAULT_BATCH_PAGE_BUDGET,
        quality_discover: Callable[..., Sequence[object]] | None = None,
        exact_auto_discover: Callable[..., MissavVariant] | None = None,
    ) -> None:
        self.download_manager = download_manager
        self.store = store or WebDownloadBatchStore(
            download_manager.config.database_path
        )
        production_discovery = discover is None or isinstance(
            discover, BrokerMissavSeriesDiscoverer
        )
        self._discover = discover or SubprocessMissavSeriesDiscoverer()
        self._quality_discover = (
            discover_web_download_qualities
            if production_discovery and quality_discover is None
            else quality_discover
        )
        self._exact_auto_discover = (
            discover_exact_web_download_variant
            if production_discovery and exact_auto_discover is None
            else exact_auto_discover
        )
        self._quality_discover_accepts_cancel = bool(
            self._quality_discover is not None
            and _quality_callable_accepts_cancel_event(self._quality_discover)
        )
        self._quality_discover_accepts_variant = bool(
            self._quality_discover is not None
            and _quality_callable_accepts_variant(self._quality_discover)
        )
        self._page_budget = validate_page_budget(page_budget)
        self._id_factory = id_factory
        self._token_factory = token_factory
        try:
            retry_delays = tuple(float(delay) for delay in discovery_retry_delays)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadConfigError(
                "MissAV batch discovery retry delays are invalid"
            ) from exc
        if (
            any(
                not 0.0 <= delay <= 60.0 or not math.isfinite(delay)
                for delay in retry_delays
            )
            or len(retry_delays) > MAX_DISCOVERY_RETRY_DELAYS
        ):
            raise WebDownloadConfigError(
                "MissAV batch discovery retry delays are invalid"
            )
        self._discovery_retry_delays = retry_delays
        try:
            recovery_interval = float(queued_recovery_interval)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadConfigError(
                "queued batch recovery interval is invalid"
            ) from exc
        if not math.isfinite(recovery_interval) or recovery_interval <= 0:
            raise WebDownloadConfigError("queued batch recovery interval is invalid")
        self._queued_recovery_interval = recovery_interval
        self._library_snapshot = library_snapshot or (
            lambda: library_deduplication_snapshot(
                Path(download_manager.config.library_path),
                Path(download_manager.config.database_path),
            )
        )
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._active: dict[str, threading.Event] = {}
        # ``_retry_attempted`` is retained as a compatibility/debugging view
        # (and means that a batch has retry state).  The count below is the
        # authoritative cursor into the configured delay sequence.
        self._retry_attempted: set[str] = set()
        self._retry_attempts: dict[str, int] = {}
        self._retry_requeue_inflight: set[str] = set()
        self._retry_timers: dict[str, threading.Timer] = {}
        self._quality_futures: set[Future[object]] = set()
        self._stopping = False
        self._quality_executor = ThreadPoolExecutor(
            max_workers=MAX_QUALITY_WORKERS,
            thread_name_prefix="jav-web-download-batch-quality",
        )
        self._worker = threading.Thread(
            target=self._run,
            name="jav-web-download-batch-discovery",
            daemon=True,
        )
        recovered_auto_ids = self.store.pending_auto_commit_ids()
        self._worker.start()
        for batch_id in recovered_auto_ids:
            self._queue.put(batch_id)

    def preview(
        self,
        code_or_prefix: object,
        start: object | None = None,
        end: object | None = None,
        max_height: object | None = 2160,
        existing_policy: object = DEFAULT_EXISTING_POLICY,
        variant_priority: object = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        *,
        rule_id: object | None = None,
        rule_revision: object | None = None,
    ) -> tuple[dict[str, object], str]:
        request = parse_batch_request(
            code_or_prefix,
            start,
            end,
            max_height,
            existing_policy,
            variant_priority,
        )
        token = self._token_factory()
        token_hash = hash_preview_token(token)
        clean_rule_id = None if rule_id is None else validate_rule_id(rule_id)
        clean_rule_revision = optional_rule_revision(rule_revision)
        library_revision = current_media_library_revision(
            Path(self.download_manager.config.library_path),
            Path(self.download_manager.config.database_path),
        )
        with self._lock:
            if self._stopping:
                raise WebDownloadBatchError(
                    "web download batch manager is shutting down"
                )
            batch = self.store.create(
                request,
                batch_id=self._id_factory(),
                token_hash=token_hash,
                page_budget=self._page_budget,
                library_revision=library_revision,
                rule_id=clean_rule_id,
                rule_revision=clean_rule_revision,
            )
            self._queue.put(str(batch["batch_id"]))
        return batch, token

    def queue_auto(
        self,
        code: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        normalized_code = normalize_catalog_code(code, max_length=40)
        if normalized_code is None:
            raise WebDownloadBatchError("automatic download catalog code is invalid")
        display_code, code_key = normalize_web_download_code(normalized_code[0])
        parsed = parse_batch_request(
            display_code,
            max_height=2160,
            existing_policy=DEFAULT_EXISTING_POLICY,
            variant_priority=DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        )
        if parsed.mode != "exact":
            raise WebDownloadBatchError(
                "automatic download requires an exact catalog code"
            )
        request = BatchRequest(
            mode=parsed.mode,
            code_or_prefix=display_code,
            prefix=parsed.prefix,
            suffix_width=parsed.suffix_width,
            start=parsed.start,
            end=parsed.end,
            max_height=parsed.max_height,
            existing_policy=parsed.existing_policy,
            variant_priority=parsed.variant_priority,
            provenance_type=SERIES_DISCOVERY_PROVENANCE,
            auto_commit=True,
        )
        request_hash = auto_download_request_hash(request, code_key)
        with self._lock:
            if self._stopping:
                raise WebDownloadBatchError(
                    "web download batch manager is shutting down"
                )
            batch, created = self.store.create_or_reuse_auto(
                request,
                batch_id=self._id_factory(),
                token_hash=hash_preview_token(self._token_factory()),
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if created:
                self._queue.put(str(batch["batch_id"]))
            return batch

    def latest_auto_intent(self, code: object) -> dict[str, object] | None:
        return self.store.latest_auto_intent(code)

    def auto_intents(
        self,
        *,
        status_filter: str = "all",
        query: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return self.store.list_auto_intents(
            status_filter=status_filter,
            query=query,
            limit=limit,
        )

    def count_auto_intents(
        self,
        *,
        status_filter: str = "all",
        query: str | None = None,
    ) -> int:
        return self.store.count_auto_intents(
            status_filter=status_filter,
            query=query,
        )

    def _selected_batch_plan(
        self,
        items: Sequence[Mapping[str, object]],
        source_session_id: object,
        source_revision: object,
        max_height: object | None = 2160,
        existing_policy: object = DEFAULT_EXISTING_POLICY,
        variant_priority: object = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        *,
        default_quality_strategy: object = "highest",
        default_height: object | None = None,
        rule_id: object | None = None,
        rule_revision: object | None = None,
    ) -> SelectedBatchPlan:
        clean_rule_id = None if rule_id is None else validate_rule_id(rule_id)
        clean_rule_revision = optional_rule_revision(rule_revision)
        if (clean_rule_id is None) != (clean_rule_revision is None):
            raise WebDownloadBatchError(
                "batch rule identity and revision must be provided together"
            )
        rule: dict[str, object] | None = None
        if clean_rule_id is not None:
            rule = self.store.get_rule(clean_rule_id)
            if int(rule["revision"]) != clean_rule_revision:
                raise WebDownloadBatchConflictError(
                    "batch rule changed before preview completion"
                )
            max_height = rule["max_height"]
            existing_policy = rule["existing_policy"]
            variant_priority = rule["variant_priority"]
            default_quality_strategy = rule["default_quality_strategy"]
            default_height = rule["default_height"]
        clean_max_height = validate_max_height(max_height)
        clean_existing_policy = validate_batch_existing_policy(existing_policy)
        clean_quality_strategy = validate_item_quality_strategy(
            default_quality_strategy
        )
        clean_default_height = (
            clean_max_height
            if clean_quality_strategy == "highest"
            else validate_intent_height(
                default_height,
                max_height=clean_max_height,
            )
        )
        try:
            clean_priority = normalize_variant_priority(variant_priority)
        except ValueError as exc:
            raise WebDownloadBatchError(str(exc)) from exc
        clean_items = normalize_selected_batch_items(items, clean_priority)
        clean_session_id = validate_source_session_id(source_session_id)
        clean_revision = validate_source_revision(source_revision)
        selected_code_keys: frozenset[str] | None = None
        if rule is not None and rule["selection_mode"] != "all":
            snapshot = self._library_snapshot()
            require_current_library_snapshot(snapshot)
            selected_code_keys = rule_selected_code_keys(
                clean_items,
                selection_mode=rule["selection_mode"],
                quality_strategy=clean_quality_strategy,
                requested_height=clean_default_height,
                snapshot=snapshot,
            )
            library_revision = snapshot.index_revision
        else:
            library_revision = current_media_library_revision(
                Path(self.download_manager.config.library_path),
                Path(self.download_manager.config.database_path),
            )
        return SelectedBatchPlan(
            items=clean_items,
            source_session_id=clean_session_id,
            source_revision=clean_revision,
            max_height=clean_max_height,
            existing_policy=clean_existing_policy,
            variant_priority=clean_priority,
            quality_strategy=clean_quality_strategy,
            default_height=clean_default_height,
            selected_code_keys=selected_code_keys,
            library_revision=library_revision,
            rule_id=clean_rule_id,
            rule_revision=clean_rule_revision,
        )

    def replay_selected_submission(
        self,
        idempotency_key: object,
        request_hash: object,
    ) -> dict[str, object] | None:
        with self._lock:
            return self.store.replay_direct_submission(
                idempotency_key,
                request_hash,
            )

    def queue_selected(
        self,
        items: Sequence[Mapping[str, object]],
        source_session_id: object,
        source_revision: object,
        max_height: object | None = 2160,
        existing_policy: object = DEFAULT_EXISTING_POLICY,
        variant_priority: object = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        *,
        default_quality_strategy: object = "highest",
        default_height: object | None = None,
        rule_id: object | None = None,
        rule_revision: object | None = None,
        idempotency_key: object,
        request_hash: object,
    ) -> dict[str, object]:
        key_hash = hash_direct_queue_key(idempotency_key)
        clean_request_hash = validate_direct_queue_hash(
            request_hash,
            "direct queue request",
        )
        with self._lock:
            replayed = self.store.replay_direct_submission(
                idempotency_key,
                clean_request_hash,
            )
            if replayed is not None:
                return replayed
            if self._stopping:
                raise WebDownloadBatchError(
                    "web download batch manager is shutting down"
                )
        # Snapshot and media-library scans are intentionally outside the
        # manager lock; create_selected revalidates the captured revisions.
        plan = self._selected_batch_plan(
            items,
            source_session_id,
            source_revision,
            max_height,
            existing_policy,
            variant_priority,
            default_quality_strategy=default_quality_strategy,
            default_height=default_height,
            rule_id=rule_id,
            rule_revision=rule_revision,
        )
        token = self._token_factory()
        batch_id = self._id_factory()
        with self._lock:
            replayed = self.store.replay_direct_submission(
                idempotency_key, clean_request_hash
            )
            if replayed is not None:
                return replayed
            if self._stopping:
                raise WebDownloadBatchError(
                    "web download batch manager is shutting down"
                )
            try:
                self.store.create_selected(
                    plan.items,
                    batch_id=batch_id,
                    token_hash=hash_preview_token(token),
                    source_session_id=plan.source_session_id,
                    source_revision=plan.source_revision,
                    max_height=plan.max_height,
                    existing_policy=plan.existing_policy,
                    variant_priority=plan.variant_priority,
                    quality_pending=False,
                    default_quality_strategy=plan.quality_strategy,
                    default_height=plan.default_height,
                    selected_code_keys=plan.selected_code_keys,
                    library_revision=plan.library_revision,
                    rule_id=plan.rule_id,
                    rule_revision=plan.rule_revision,
                    direct_queue_key_hash=key_hash,
                    direct_queue_request_hash=clean_request_hash,
                )
                committed = self.action(
                    batch_id,
                    "commit",
                    token,
                )
                if str(committed["status"]) != "committed":
                    raise WebDownloadBatchConflictError(
                        "web download batch could not be committed"
                    )
                return committed
            except BaseException:
                try:
                    self.store.discard_direct_submission(
                        idempotency_key,
                        clean_request_hash,
                        batch_id=batch_id,
                    )
                except Exception:
                    pass
                raise

    def get(self, batch_id: str) -> dict[str, object]:
        batch = self.store.get(batch_id)
        if str(batch["status"]) == "expired":
            self._cancel_active_batches((str(batch["batch_id"]),))
        return batch

    def action(
        self,
        batch_id: str,
        action: object,
        preview_token: object | None = None,
        selected_codes: Sequence[object] | None = None,
        item_intents: Sequence[Mapping[str, object]] | None = None,
        commit_guard: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        clean_action = str(action or "").strip().lower()
        if clean_action == "cancel":
            with self._lock:
                batch = self.store.cancel(batch_id)
                event = self._active.get(str(batch_id))
                if event is not None:
                    event.set()
                timer = self._retry_timers.pop(str(batch_id), None)
                if timer is not None:
                    timer.cancel()
                self._retry_attempted.discard(str(batch_id))
                self._retry_attempts.pop(str(batch_id), None)
                self._retry_requeue_inflight.discard(str(batch_id))
                return batch
        if clean_action == "commit":
            with self._lock:
                if self._stopping:
                    raise WebDownloadBatchError(
                        "web download batch manager is shutting down"
                    )
                if commit_guard is not None:
                    current = self.store.get(batch_id)
                    if str(current["status"]) != "committed":
                        commit_guard()
            requires_snapshot = self.store.commit_requires_library_snapshot(
                batch_id,
                preview_token,
                selected_codes,
                item_intents,
            )
            attempts = COMMIT_SNAPSHOT_MAX_ATTEMPTS if requires_snapshot else 1
            for attempt in range(attempts):
                try:
                    snapshot = (
                        self._library_snapshot()
                        if requires_snapshot
                        else LibraryDeduplicationSnapshot(frozenset(), frozenset())
                    )
                    require_current_library_snapshot(snapshot)
                except LibrarySnapshotChanged as exc:
                    if attempt + 1 >= attempts:
                        raise WebDownloadBatchConflictError(
                            "media library changed repeatedly; retry the batch commit"
                        ) from exc
                    continue
                with self._lock:
                    if self._stopping:
                        raise WebDownloadBatchError(
                            "web download batch manager is shutting down"
                        )
                    try:
                        batch, changed = self.store.commit(
                            batch_id,
                            preview_token,
                            selected_codes=selected_codes,
                            item_intents=item_intents,
                            library_snapshot=snapshot,
                        )
                    except LibrarySnapshotChanged as exc:
                        if attempt + 1 >= attempts:
                            raise WebDownloadBatchConflictError(
                                "media library changed repeatedly; retry the batch commit"
                            ) from exc
                        continue
                    if changed and int(batch["created_count"]) > 0:
                        try:
                            self.download_manager.notify_queued_jobs()
                        except Exception:
                            # The jobs are already committed durably. Download workers
                            # also poll, so a failed wake-up must not turn success into
                            # a misleading API failure.
                            pass
                    return batch
            raise WebDownloadBatchConflictError(
                "web download batch could not obtain a stable library snapshot"
            )
        if clean_action == "continue":
            library_revision = current_media_library_revision(
                Path(self.download_manager.config.library_path),
                Path(self.download_manager.config.database_path),
            )
            with self._lock:
                if self._stopping:
                    raise WebDownloadBatchError(
                        "web download batch manager is shutting down"
                    )
                batch, changed = self.store.create_continuation(
                    batch_id,
                    preview_token,
                    continuation_id=self._id_factory(),
                    library_revision=library_revision,
                )
                if changed:
                    self._queue.put(str(batch["batch_id"]))
                return batch
        if clean_action == "retry":
            with self._lock:
                if self._stopping:
                    raise WebDownloadBatchError(
                        "web download batch manager is shutting down"
                    )
                batch, changed = self.store.retry_failed(batch_id, preview_token)
                if changed:
                    self._clear_retry_state_locked(str(batch_id))
                    self._queue.put(str(batch["batch_id"]))
                return batch
        if clean_action == "remove":
            removed = self.store.remove(batch_id)
            self._cancel_active_batches((str(batch_id),))
            return removed
        raise WebDownloadBatchError("unsupported web download batch action")

    def remove_failed_replacement(
        self,
        batch_id: str,
        *,
        expected_updated_at: object,
    ) -> dict[str, object]:
        with self._lock:
            result = self.store.remove_failed_auto_if_unchanged(
                batch_id,
                expected_updated_at=expected_updated_at,
            )
            self._cancel_active_batches((str(batch_id),))
            return result

    def list_chains(self, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        self._expire_ready_and_cancel()
        return self.store.list_chains(limit=limit, offset=offset)

    def get_chain(
        self,
        root_chain_id: str,
        *,
        page_limit: int = DEFAULT_BATCH_PAGE_BUDGET,
        page_offset: int = 0,
    ) -> dict[str, object]:
        self._expire_ready_and_cancel()
        return self.store.get_chain(
            root_chain_id,
            page_limit=page_limit,
            page_offset=page_offset,
        )

    def cancel_chain(self, root_chain_id: str) -> dict[str, object]:
        with self._lock:
            summary, cancelled = self.store.cancel_chain(root_chain_id)
            for batch_id in cancelled:
                event = self._active.get(batch_id)
                if event is not None:
                    event.set()
                timer = self._retry_timers.pop(batch_id, None)
                if timer is not None:
                    timer.cancel()
                self._retry_attempted.discard(batch_id)
                self._retry_attempts.pop(batch_id, None)
                self._retry_requeue_inflight.discard(batch_id)
        return summary

    def export_chain(self, root_chain_id: str) -> dict[str, object]:
        return self.store.export_chain(root_chain_id)

    def save_rule(
        self,
        name: object,
        code_or_prefix: object,
        start: object | None = None,
        end: object | None = None,
        max_height: object | None = 2160,
        existing_policy: object = DEFAULT_EXISTING_POLICY,
        variant_priority: object = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY,
        *,
        rule_id: object | None = None,
        default_quality_strategy: object = "highest",
        default_height: object | None = None,
        selection_mode: object = "all",
        expected_revision: object | None = None,
    ) -> dict[str, object]:
        request = parse_batch_request(
            code_or_prefix,
            start,
            end,
            max_height,
            existing_policy,
            variant_priority,
        )
        clean_rule_id = (
            validate_rule_id(rule_id) if rule_id is not None else self._id_factory()
        )
        with self._lock:
            return self.store.save_rule(
                request,
                rule_id=clean_rule_id,
                name=name,
                default_quality_strategy=default_quality_strategy,
                default_height=default_height,
                selection_mode=selection_mode,
                expected_revision=expected_revision,
            )

    def list_rules(self) -> list[dict[str, object]]:
        return self.store.list_rules()

    def get_rule(self, rule_id: str) -> dict[str, object]:
        return self.store.get_rule(rule_id)

    def remove_rule(
        self,
        rule_id: str,
        *,
        expected_revision: object,
    ) -> dict[str, object]:
        with self._lock:
            return self.store.remove_rule(
                rule_id,
                expected_revision=expected_revision,
            )

    def _expire_ready_and_cancel(self) -> tuple[str, ...]:
        expired = self.store.expire_ready()
        self._cancel_active_batches(expired)
        return expired

    def _cancel_active_batches(self, batch_ids: Sequence[str]) -> None:
        if not batch_ids:
            return
        with self._lock:
            for batch_id in batch_ids:
                event = self._active.get(batch_id)
                if event is not None:
                    event.set()
                timer = self._retry_timers.pop(batch_id, None)
                if timer is not None:
                    timer.cancel()
                self._retry_attempted.discard(batch_id)
                self._retry_attempts.pop(batch_id, None)
                self._retry_requeue_inflight.discard(batch_id)

    def preview_rule(self, rule_id: str) -> tuple[dict[str, object], str]:
        rule = self.store.get_rule(rule_id)
        return self.preview(
            rule["code_or_prefix"],
            rule["start"],
            rule["end"],
            rule["max_height"],
            rule["existing_policy"],
            rule["variant_priority"],
            rule_id=rule["rule_id"],
            rule_revision=rule["revision"],
        )

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            if not self._stopping:
                self._stopping = True
                for event in self._active.values():
                    event.set()
                for future in tuple(self._quality_futures):
                    future.cancel()
                for timer in self._retry_timers.values():
                    timer.cancel()
                self._retry_timers.clear()
                self._retry_attempted.clear()
                self._retry_attempts.clear()
                self._retry_requeue_inflight.clear()
                for attempt in range(CLAIM_MAX_ATTEMPTS):
                    try:
                        self.store.fail_interrupted()
                        break
                    except (OSError, sqlite3.Error, WebDownloadError):
                        if attempt + 1 >= CLAIM_MAX_ATTEMPTS:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(
                            min(
                                CLAIM_RETRY_BASE_SECONDS * (2**attempt),
                                remaining,
                            )
                        )
                self._queue.put(None)
        if self._worker.ident is not None:
            self._worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self._quality_executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            pending = tuple(
                future for future in self._quality_futures if not future.done()
            )
        if pending:
            wait(pending, timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            quality_released = all(future.done() for future in self._quality_futures)
        return not self._worker.is_alive() and quality_released

    def _run(self) -> None:
        while True:
            try:
                batch_id = self._queue.get(timeout=self._queued_recovery_interval)
            except queue.Empty:
                self._recover_queued_auto_batches()
                continue
            if batch_id is None:
                return
            cancel_event = threading.Event()
            with self._lock:
                # Queue recovery and retry timers can legitimately enqueue the
                # same id more than once.  Never replace an existing active
                # cancellation token; doing so would allow two discoverers to
                # operate on one batch and could create duplicate jobs.
                if (
                    self._stopping
                    or batch_id in self._active
                    or batch_id in self._retry_timers
                ):
                    continue
                self._active[batch_id] = cancel_event
            request = self._claim_with_retry(batch_id, cancel_event)
            if request is None:
                with self._lock:
                    self._active.pop(batch_id, None)
                    self._clear_retry_state_locked(batch_id)
                continue
            retry_scheduled = False
            try:
                if request.provenance_type == RESOURCE_SEARCH_SELECTION_PROVENANCE:
                    self.store.prepare_selected(batch_id)
                elif request.provenance_type == SERIES_DISCOVERY_PROVENANCE:
                    result = self._discover_once(request, cancel_event)
                    codes = normalize_discovered_codes(
                        getattr(result, "codes", ()), request
                    )
                    complete = getattr(result, "complete", None)
                    if not isinstance(complete, bool):
                        raise WebDownloadBatchError("batch discovery result is invalid")
                    raw_variants = getattr(result, "variants_by_code", None)
                    variants_by_code = (
                        None if raw_variants is None else dict(raw_variants)
                    )
                    finished = self._finish_discovery_with_retry(
                        batch_id,
                        codes=codes,
                        variants_by_code=variants_by_code,
                        complete=complete,
                        quality_pending=(
                            self._quality_discover is not None
                            and not request.auto_commit
                        ),
                    )
                else:
                    raise WebDownloadBatchError("batch provenance is invalid")
                if request.auto_commit and finished:
                    current = self.store.get(batch_id)
                    if str(current["status"]) == "ready":
                        self._commit_auto_batch(batch_id, cancel_event)
                elif self._quality_discover is not None:
                    self._resolve_batch_qualities(batch_id, cancel_event)
            except Exception as exc:
                if retryable_discovery_error(exc):
                    retry_scheduled = self._requeue_transient_discovery(
                        batch_id,
                        cancel_event,
                    )
                if not retry_scheduled:
                    self._mark_failed_with_retry(
                        batch_id,
                        claim_failure=False,
                        cancel_event=cancel_event,
                        failure=exc,
                    )
            finally:
                with self._lock:
                    self._active.pop(batch_id, None)
                    if not retry_scheduled:
                        self._clear_retry_state_locked(batch_id)

    def _recover_queued_auto_batches(self) -> None:
        with self._lock:
            if self._stopping:
                return
        try:
            pending = self.store.pending_auto_commit_ids()
        except (OSError, sqlite3.Error, WebDownloadError):
            return
        for batch_id in pending:
            with self._lock:
                if self._stopping:
                    return
                if batch_id in self._active or batch_id in self._retry_timers:
                    continue
            self._queue.put(batch_id)

    def _commit_auto_batch(
        self,
        batch_id: str,
        cancel_event: threading.Event,
    ) -> None:
        for attempt in range(COMMIT_SNAPSHOT_MAX_ATTEMPTS):
            if cancel_event.is_set():
                return
            try:
                snapshot = self._library_snapshot()
                require_current_library_snapshot(snapshot)
                batch, changed = self.store.commit_auto(
                    batch_id,
                    library_snapshot=snapshot,
                )
            except LibrarySnapshotChanged:
                if attempt + 1 >= COMMIT_SNAPSHOT_MAX_ATTEMPTS:
                    raise WebDownloadBatchConflictError(
                        "media library changed repeatedly; retry the automatic download"
                    ) from None
                continue
            if changed and int(batch["created_count"]) > 0:
                try:
                    self.download_manager.notify_queued_jobs()
                except Exception:
                    pass
            return

    def _discover_once(
        self,
        request: BatchRequest,
        cancel_event: threading.Event,
    ) -> object:
        if cancel_event.is_set():
            raise WebDownloadBatchError("batch discovery was cancelled")
        if (
            request.auto_commit
            and request.mode == "exact"
            and self._exact_auto_discover is not None
        ):
            variant = normalize_web_download_variant(
                self._exact_auto_discover(
                    request.code_or_prefix,
                    variant_priority=request.variant_priority,
                    timeout_seconds=30.0,
                    cancel_event=cancel_event,
                )
            )
            return missav_models.MissavSeriesDiscovery(
                codes=(request.code_or_prefix,),
                complete=True,
                variants_by_code=((request.code_or_prefix, (variant,)),),
            )
        permit = MISSAV_BROWSER_GATE.acquire_background(
            blocking=True,
            cancel_event=cancel_event,
        )
        if permit is None:
            raise WebDownloadBatchError("batch discovery was cancelled")
        with permit:
            return self._discover(
                request.prefix,
                suffix_width=request.suffix_width,
                start=(int(request.start) if request.start is not None else None),
                end=(int(request.end) if request.end is not None else None),
                timeout_seconds=120,
                cancel_event=cancel_event,
            )

    def _resolve_batch_qualities(
        self,
        batch_id: str,
        cancel_event: threading.Event,
    ) -> None:
        max_height, requests = self.store.quality_requests(batch_id)
        if not requests or self._quality_discover is None:
            return
        futures: dict[Future[object], str] = {}
        for code, code_key, variant in requests:
            if cancel_event.is_set():
                return
            future = self._quality_executor.submit(
                self._discover_item_qualities,
                code,
                variant,
                max_height,
                cancel_event,
            )
            with self._lock:
                self._quality_futures.add(future)
            future.add_done_callback(self._forget_quality_future)
            futures[future] = code_key
        results: dict[str, Sequence[object] | None] = {}
        pending = set(futures)
        try:
            while pending:
                if cancel_event.is_set():
                    for future in pending:
                        future.cancel()
                    return
                completed, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    code_key = futures[future]
                    try:
                        results[code_key] = future.result()
                    except Exception:
                        results[code_key] = None
        finally:
            if cancel_event.is_set():
                for future in futures:
                    future.cancel()
        if len(results) == len(requests) and not cancel_event.is_set():
            batch = self.store.get(batch_id)
            rule_id = batch.get("rule_id")
            if rule_id is None:
                try:
                    self.store.finish_quality_resolution(batch_id, results)
                except (
                    OSError,
                    sqlite3.Error,
                    WebDownloadBatchError,
                    WebDownloadError,
                ):
                    self.store.fail_quality_resolution(batch_id)
                return
            try:
                rule = self.store.get_rule(str(rule_id))
                if batch.get("rule_revision") is None or int(rule["revision"]) != int(
                    batch["rule_revision"]
                ):
                    raise WebDownloadBatchConflictError(
                        "batch rule changed before preview completion"
                    )
                snapshot = self._library_snapshot()
                require_current_library_snapshot(snapshot)
                self.store.finish_quality_resolution(
                    batch_id,
                    results,
                    rule=rule,
                    library_snapshot=snapshot,
                )
            except (
                OSError,
                sqlite3.Error,
                WebDownloadBatchError,
                WebDownloadError,
            ):
                self.store.fail_quality_resolution(batch_id)

    def _discover_item_qualities(
        self,
        code: str,
        variant: MissavVariant,
        max_height: int,
        cancel_event: threading.Event,
    ) -> tuple[int, ...] | None:
        if cancel_event.is_set() or self._quality_discover is None:
            return None
        if (
            variant != DEFAULT_WEB_DOWNLOAD_VARIANT
            and not self._quality_discover_accepts_variant
        ):
            return None
        permit = MISSAV_BROWSER_GATE.acquire_background(
            blocking=True,
            cancel_event=cancel_event,
        )
        if permit is None:
            return None
        with permit:
            if cancel_event.is_set():
                return None
            try:
                kwargs: dict[str, object] = {}
                if self._quality_discover_accepts_cancel:
                    kwargs["cancel_event"] = cancel_event
                if self._quality_discover_accepts_variant:
                    kwargs["variant"] = variant
                discovered = self._quality_discover(code, **kwargs)
                if cancel_event.is_set():
                    return None
                heights = normalize_quality_heights(discovered)
            except Exception:
                return None
        eligible = tuple(height for height in heights if height <= max_height)
        return eligible

    def _forget_quality_future(self, future: Future[object]) -> None:
        with self._lock:
            self._quality_futures.discard(future)

    def _requeue_transient_discovery(
        self,
        batch_id: str,
        cancel_event: threading.Event,
    ) -> bool:
        """Move a transiently failed discovery back to the queue once per delay.

        A retry *attempt* is represented by one entry in
        ``_discovery_retry_delays``.  The old implementation used a set and
        therefore consumed only the first delay, silently converting every
        later transient failure into a terminal error.  Keep the reservation
        and database transition idempotent so a timer, queue recovery pass,
        or a duplicate worker callback cannot schedule two retries for the
        same batch.
        """
        with self._lock:
            if (
                self._stopping
                or cancel_event.is_set()
                or not self._discovery_retry_delays
                or batch_id in self._retry_timers
                or batch_id in self._retry_requeue_inflight
            ):
                return False
            retry_index = self._retry_attempts.get(batch_id, 0)
            if retry_index >= len(self._discovery_retry_delays):
                return False
            self._retry_requeue_inflight.add(batch_id)
        for attempt in range(CLAIM_MAX_ATTEMPTS):
            try:
                changed = self.store.requeue_discovery(batch_id)
                break
            except (OSError, sqlite3.Error, WebDownloadError):
                if attempt + 1 >= CLAIM_MAX_ATTEMPTS:
                    with self._lock:
                        self._retry_requeue_inflight.discard(batch_id)
                    return False
                if cancel_event.wait(CLAIM_RETRY_BASE_SECONDS * (2**attempt)):
                    with self._lock:
                        self._retry_requeue_inflight.discard(batch_id)
                    return False
        if not changed:
            with self._lock:
                self._retry_requeue_inflight.discard(batch_id)
            return False
        with self._lock:
            self._retry_requeue_inflight.discard(batch_id)
            if self._stopping or cancel_event.is_set():
                return False
            # A concurrent cancellation/retry may have advanced the state
            # while SQLite was busy.  Re-check the cursor before scheduling.
            retry_index = self._retry_attempts.get(batch_id, 0)
            if retry_index >= len(self._discovery_retry_delays):
                return False
            self._retry_attempts[batch_id] = retry_index + 1
            self._retry_attempted.add(batch_id)
            delay = self._discovery_retry_delays[retry_index]
            if delay == 0.0:
                self._queue.put(batch_id)
                return True
            timer = threading.Timer(delay, self._enqueue_due_retry, args=(batch_id,))
            timer.daemon = True
            self._retry_timers[batch_id] = timer
            try:
                timer.start()
            except RuntimeError:
                self._retry_timers.pop(batch_id, None)
                self._retry_attempted.discard(batch_id)
                self._retry_attempts.pop(batch_id, None)
                return False
            return True

    def _enqueue_due_retry(self, batch_id: str) -> None:
        with self._lock:
            self._retry_timers.pop(batch_id, None)
            if self._stopping or batch_id not in self._retry_attempted:
                return
            self._queue.put(batch_id)

    def _clear_retry_state_locked(self, batch_id: str) -> None:
        timer = self._retry_timers.pop(batch_id, None)
        if timer is not None:
            timer.cancel()
        self._retry_attempted.discard(batch_id)
        self._retry_attempts.pop(batch_id, None)
        self._retry_requeue_inflight.discard(batch_id)

    def _claim_with_retry(
        self,
        batch_id: str,
        cancel_event: threading.Event,
    ) -> BatchRequest | None:
        for attempt in range(CLAIM_MAX_ATTEMPTS):
            with self._lock:
                if self._stopping:
                    return None
            try:
                return self.store.claim(batch_id)
            except WebDownloadError:
                break
            except (OSError, sqlite3.Error):
                if attempt + 1 >= CLAIM_MAX_ATTEMPTS:
                    break
                if cancel_event.wait(CLAIM_RETRY_BASE_SECONDS * (2**attempt)):
                    return None
        self._mark_failed_with_retry(
            batch_id,
            claim_failure=True,
            cancel_event=cancel_event,
            failure=None,
        )
        return None

    def _finish_discovery_with_retry(
        self,
        batch_id: str,
        *,
        codes: Sequence[str],
        variants_by_code: Mapping[str, Sequence[object]] | None,
        complete: bool,
        quality_pending: bool,
    ) -> bool:
        for attempt in range(CLAIM_MAX_ATTEMPTS):
            try:
                return self.store.finish_discovery(
                    batch_id,
                    codes=codes,
                    variants_by_code=variants_by_code,
                    complete=complete,
                    quality_pending=quality_pending,
                )
            except (OSError, sqlite3.Error):
                if attempt + 1 >= CLAIM_MAX_ATTEMPTS:
                    raise
                time.sleep(CLAIM_RETRY_BASE_SECONDS * (2**attempt))
        return False

    def _mark_failed_with_retry(
        self,
        batch_id: str,
        *,
        claim_failure: bool,
        cancel_event: threading.Event,
        failure: BaseException | None,
    ) -> None:
        mark_failed = (
            self.store.fail_claim if claim_failure else self.store.fail_discovery
        )
        attempt = 0
        while True:
            with self._lock:
                if self._stopping:
                    return
            try:
                if claim_failure:
                    mark_failed(batch_id)
                else:
                    mark_failed(
                        batch_id,
                        failure_code=getattr(failure, "failure_code", "queue_failed"),
                    )
                return
            except (OSError, sqlite3.Error, WebDownloadError):
                delay = min(
                    CLAIM_RETRY_BASE_SECONDS * (2 ** min(attempt, 8)),
                    CLAIM_RETRY_MAX_SECONDS,
                )
                attempt += 1
                if cancel_event.wait(delay):
                    return
