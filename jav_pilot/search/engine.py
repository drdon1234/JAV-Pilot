from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field, replace
from datetime import date
from functools import cmp_to_key
from threading import Event, Lock, Thread
from typing import Any

from ..core.catalog_code import (
    CodePattern,
    canonical_catalog_code,
    code_matches_pattern,
    query_code_pattern,
)
from ..core.guards import looks_like_catalog_code, normalize_query
from ..indexers import Fc2Indexer, Indexer, JavBusIndexer, JavDbIndexer
from ..indexers.fc2 import fc2_product_id
from ..indexers.fanza import FanzaIndexer
from ..indexers.mgs import MgsIndexer
from ..indexers.avbase import AvBaseIndexer
from ..indexers.fc2db import Fc2DbIndexer
from ..indexers.javten import JavTenIndexer
from ..indexers.torznab import TorznabConfig, TorznabIndexer
from ..core.models import (
    MagnetGroup,
    MagnetInfo,
    MagnetSourceRef,
    SearchBounds,
    SearchContinuation,
    SearchContinuationSource,
    SearchPageUpdate,
    SearchResponse,
    SearchResult,
    SearchSort,
    SourceImage,
    WorkResult,
    WorkSource,
    normalize_source_images,
)
from ..config.settings import filter_defaults, load_settings, search_sites


NATURAL_TOKEN_RE = re.compile(r"\d+|\D+")
MAX_UPSTREAM_PAGE_RESULTS = 50
MAX_UPSTREAM_PAGES = 3
MAX_RESULT_SCAN_PAGES = 100
SOURCE_ERROR_URL_RE = re.compile(r"(?:https?|wss?|ftp)://\S+", flags=re.IGNORECASE)
SOURCE_ERROR_SENSITIVE_CONTEXT_RE = re.compile(
    r"\b(?:authorization|proxy-authorization|bearer|cookie|set-cookie|password|"
    r"secret|token|api[_-]?key|credential|session(?:id)?)\b",
    flags=re.IGNORECASE,
)
SENSITIVE_SOURCE_ERROR = "upstream request failed (sensitive details redacted)"
_METADATA_INDEXERS = {
    "fanza": FanzaIndexer, "mgs": MgsIndexer, "avbase": AvBaseIndexer,
    "fc2db": Fc2DbIndexer, "javten": JavTenIndexer,
}


def default_indexers(settings: dict | None = None) -> dict[str, Indexer]:
    settings = settings or load_settings()
    registry: dict[str, Indexer] = {}
    for site in search_sites(settings):
        site_id = str(site.get("id") or "")
        profile = str(site.get("parser_profile") or site_id)
        base_url = str(site.get("base_url") or "").strip()
        template = str(site.get("search", {}).get("url_template") or "").strip()
        defaults = filter_defaults(site)
        parser_rules = (
            site.get("parser_rules")
            if site.get("parser_rules_mode") == "custom"
            and isinstance(site.get("parser_rules"), dict)
            else None
        )
        if profile == "javbus":
            registry[site_id] = JavBusIndexer(
                base_url=base_url,
                search_template=template,
                default_filters=defaults,
                source_id=site_id,
                parser_rules=parser_rules,
            )
        elif profile == "javdb":
            registry[site_id] = JavDbIndexer(
                base_url=base_url,
                search_template=template,
                default_filters=defaults,
                source_id=site_id,
                parser_rules=parser_rules,
            )
        elif profile == "fc2":
            registry[site_id] = Fc2Indexer(
                base_url=base_url,
                search_template=template,
                default_filters=defaults,
                source_id=site_id,
            )
        elif profile in _METADATA_INDEXERS:
            registry[site_id] = _METADATA_INDEXERS[profile](
                base_url=base_url, source_id=site_id,
            )
        elif profile == "torznab":
            config = site.get("torznab", {})
            registry[site_id] = TorznabIndexer(TorznabConfig(
                endpoint=config["endpoint"], api_key=config["api_key"],
                source_id=site_id, display_name=site.get("name", site_id),
                pinned_addresses=tuple(config.get("pinned_addresses", [])),
                categories=tuple(config.get("categories", [])),
            ))
    return registry


def aggregate_results(
    records: Iterable[SearchResult],
    *,
    sort: str = "relevance",
) -> tuple[WorkResult, ...]:
    normalized_sort = SearchBounds(sort=sort).normalized().sort
    source_records = [(record.source, record) for record in records]
    works = _aggregate_records(source_records)
    return tuple(_sort_works(works, normalized_sort))


def merge_work_records(
    work: WorkResult,
    records: Iterable[tuple[str, SearchResult]],
) -> WorkResult:
    """Rebuild one known work from per-source records without re-grouping.

    Stream enrichment replaces individual sources of an existing work. Those
    records already belong to ``work``; grouping them again by code would
    split a source whose detail page reports a variant spelling and silently
    drop every group but the first.
    """

    builder = _WorkBuilder(
        work_id=work.work_id,
        canonical_code=work.canonical_code,
        code=work.code,
        title=work.title,
        primary_identity=True,
    )
    for source_id, record in records:
        builder.add(source_id, record)
    return builder.build()


def search(
    raw_query: str,
    *,
    sources: Iterable[str] = ("all",),
    bounds: SearchBounds | None = None,
    indexers: dict[str, Indexer] | None = None,
    on_source: Callable[[str, SearchResponse, int, int], None] | None = None,
    on_page: Callable[[SearchPageUpdate], None] | None = None,
    on_checkpoint: Callable[[SearchResponse], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    continuation: SearchContinuation | None = None,
    include_torrent_sources: bool = True,
) -> SearchResponse:
    """Search the selected sources and aggregate them into works.

    Torrent indexers only supply magnets: in an aggregate search their records
    merge into works found by metadata sources and never decide a work's title,
    first source, or a result slot ahead of metadata results. When magnets are
    not requested (``include_torrent_sources=False``) they are skipped entirely
    unless they are the only selected source.
    """

    query = normalize_query(raw_query)
    bounds = (bounds or SearchBounds()).normalized()
    registry = indexers or default_indexers()

    selected = list(sources)
    if selected == ["all"] or not selected:
        selected = list(registry.keys())

    errors: dict[str, str] = {}
    skipped: dict[str, str] = {}
    jobs: dict[str, Indexer] = {}
    torrent_only = all(
        isinstance(registry.get(source), TorznabIndexer) for source in selected
    )

    for source in selected:
        indexer = registry.get(source)
        if not indexer:
            errors[source] = "unknown source"
            continue
        reason = source_skip_reason(indexer, query, bounds)
        if (
            reason is None
            and not include_torrent_sources
            and not torrent_only
            and isinstance(indexer, TorznabIndexer)
        ):
            reason = "未开启解析磁链"
        if reason is not None:
            skipped[source] = reason
            continue
        jobs[source] = indexer

    if not jobs:
        return SearchResponse(
            query=query,
            results=(),
            errors=errors,
            skipped=skipped,
            sort=bounds.sort,
            match=bounds.match,
            result_limit=bounds.result_limit,
            found_count=0,
            pages_total=0,
        )
    supplementary = frozenset(
        source
        for source, indexer in jobs.items()
        if isinstance(indexer, TorznabIndexer)
    )
    if supplementary == frozenset(jobs):
        supplementary = frozenset()

    source_bounds = bounds
    clean_continuation = _validated_search_continuation(
        continuation,
        sources=tuple(jobs),
        result_limit=bounds.result_limit,
    )
    if clean_continuation is not None:
        errors.update(clean_continuation.errors)
    source_outputs: dict[str, tuple[SearchResult, ...]] = {}
    incremental_limit = bounds.result_limit
    if on_page is not None and incremental_limit is None:
        incremental_limit = min(bounds.page * bounds.limit, 999)
    accumulator = (
        _IncrementalSearchAccumulator(
            query,
            bounds,
            incremental_limit,
            tuple(jobs),
            supplementary=supplementary,
        )
        if incremental_limit is not None
        else None
    )
    progress_lock = Lock()
    checkpoint_delivery_lock = Lock()
    delivered_checkpoint_generation = 0
    limit_reached = Event()
    incremental_pages_scanned = (
        clean_continuation.pages_scanned if clean_continuation is not None else 0
    )
    continuation_records: dict[str, list[tuple[SearchResult, ...]]] = {
        source_id: [] for source_id in jobs
    }
    continuation_next_pages: dict[str, int | None] = {
        source_id: 1 for source_id in jobs
    }
    if clean_continuation is not None:
        for source in clean_continuation.sources:
            if source.records:
                continuation_records[source.source_id].append(source.records)
            continuation_next_pages[source.source_id] = source.next_page
            if accumulator is not None:
                accumulator.add(
                    source.source_id,
                    source.records,
                    source_done=source.next_page is None,
                )
        if (
            accumulator is not None
            and accumulator.found_count >= accumulator.result_limit
        ):
            if bounds.result_limit == clean_continuation.result_limit:
                raise ValueError("continued search result limit must increase")
            limit_reached.set()

    def page_completed(
        source_id: str,
        upstream_page: int,
        page_results: tuple[SearchResult, ...],
        source_done: bool,
        page_failed: bool,
    ) -> bool:
        nonlocal incremental_pages_scanned, delivered_checkpoint_generation
        if accumulator is None:
            return False
        if client_cancelled():
            return True
        checkpoint_snapshot: (
            tuple[
                int,
                tuple[WorkResult, ...],
                dict[str, tuple[tuple[SearchResult, ...], ...]],
                dict[str, int | None],
                dict[str, str],
                int,
            ]
            | None
        ) = None
        with progress_lock:
            if client_cancelled():
                return True
            incremental_pages_scanned += 1
            if not page_failed:
                if page_results:
                    continuation_records[source_id].append(page_results)
                continuation_next_pages[source_id] = (
                    None if source_done else upstream_page + 1
                )
            # A failed source cannot consume its reserved share for the rest of
            # this response.  Treat the failed run as terminal for quota
            # allocation while retaining its continuation cursor for a later
            # retry.  Otherwise healthy sources remain stranded in ``pending``
            # and a requested result limit can return only half full.
            delta = accumulator.add(
                source_id,
                page_results,
                source_done=source_done or page_failed,
            )
            found_count = accumulator.found_count
            if found_count >= accumulator.result_limit:
                limit_reached.set()
            update = SearchPageUpdate(
                source_id=source_id,
                upstream_page=upstream_page,
                delta=delta,
                pages_scanned=incremental_pages_scanned,
                pages_total=None,
                found_count=found_count,
                result_limit=accumulator.result_limit,
            )
            if on_checkpoint is not None:
                checkpoint_snapshot = (
                    incremental_pages_scanned,
                    accumulator.snapshot_results(),
                    {
                        source: tuple(chunks)
                        for source, chunks in continuation_records.items()
                    },
                    dict(continuation_next_pages),
                    dict(errors),
                    found_count,
                )
            if on_page is not None:
                if client_cancelled():
                    return True
                on_page(update)
        if checkpoint_snapshot is not None and not client_cancelled():
            (
                generation,
                snapshot_results,
                snapshot_records,
                snapshot_next_pages,
                snapshot_errors,
                snapshot_found_count,
            ) = checkpoint_snapshot
            continuation = _search_continuation(
                sources=tuple(jobs),
                records=snapshot_records,
                next_pages=snapshot_next_pages,
                pages_scanned=generation,
                result_limit=accumulator.result_limit,
                found_count=snapshot_found_count,
                errors=snapshot_errors,
            )
            checkpoint = _response_from_results(
                query,
                bounds,
                snapshot_results,
                snapshot_errors,
                generation,
                result_limit=accumulator.result_limit,
                continuation=continuation,
                skipped=skipped,
            )
            with checkpoint_delivery_lock:
                if (
                    generation > delivered_checkpoint_generation
                    and not client_cancelled()
                ):
                    on_checkpoint(checkpoint)
                    delivered_checkpoint_generation = generation
        return limit_reached.is_set()

    def client_cancelled() -> bool:
        return bool(cancelled and cancelled())

    active_jobs = {
        source_id: indexer
        for source_id, indexer in jobs.items()
        if continuation_next_pages[source_id] is not None
    }
    if limit_reached.is_set() and clean_continuation is not None:
        active_jobs = {}

    if active_jobs:
        futures: dict[Future[_SourceSearchOutput], str] = {}
        threads: list[Thread] = []

        def run_source_job(
            future: Future[_SourceSearchOutput],
            source: str,
            indexer: Indexer,
        ) -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                output = _search_indexer_pages(
                    source,
                    indexer,
                    query,
                    source_bounds,
                    page_completed if accumulator is not None else None,
                    limit_reached.is_set,
                    client_cancelled,
                    start_page=continuation_next_pages[source] or 1,
                )
            except Exception as exc:  # noqa: BLE001 - delivered through the Future below.
                future.set_exception(exc)
            else:
                future.set_result(output)

        try:
            for source, indexer in active_jobs.items():
                future: Future[_SourceSearchOutput] = Future()
                futures[future] = source
                try:
                    thread = Thread(
                        target=run_source_job,
                        args=(future, source, indexer),
                        name=f"search-source-{source}",
                        daemon=True,
                    )
                    thread.start()
                except (OSError, RuntimeError):
                    run_source_job(future, source, indexer)
                else:
                    threads.append(thread)
            completed = 0
            legacy_pages_scanned = 0
            pending = set(futures)
            while pending:
                if client_cancelled():
                    for future in pending:
                        future.cancel()
                    break
                done, pending = wait(
                    pending,
                    timeout=0.05,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    if client_cancelled():
                        future.cancel()
                        continue
                    source = futures[future]
                    with progress_lock:
                        errors.pop(source, None)
                    try:
                        output = future.result()
                        source_outputs[source] = output.results
                        legacy_pages_scanned += output.pages_scanned
                    except Exception as exc:  # noqa: BLE001 - source failures should not fail the whole search.
                        with progress_lock:
                            errors[source] = sanitize_source_error(exc)
                    completed += 1
                    if on_source is not None:
                        if accumulator is not None:
                            with progress_lock:
                                partial_response = _response_from_accumulator(
                                    query,
                                    bounds,
                                    accumulator,
                                    errors,
                                    incremental_pages_scanned,
                                    continuation=None,
                                    skipped=skipped,
                                )
                        else:
                            partial_response = _response_from_source_outputs(
                                query,
                                bounds,
                                tuple(jobs),
                                source_outputs,
                                errors,
                                supplementary=supplementary,
                                skipped=skipped,
                            )
                        if client_cancelled():
                            continue
                        on_source(
                            source,
                            partial_response,
                            completed,
                            len(active_jobs),
                        )
        finally:
            for thread in threads:
                thread.join()
    else:
        legacy_pages_scanned = 0

    if accumulator is not None:
        next_continuation = _search_continuation(
            sources=tuple(jobs),
            records=continuation_records,
            next_pages=continuation_next_pages,
            pages_scanned=incremental_pages_scanned,
            result_limit=accumulator.result_limit,
            found_count=accumulator.found_count,
            errors=errors,
        )
        return _response_from_accumulator(
            query,
            bounds,
            accumulator,
            errors,
            incremental_pages_scanned,
            continuation=next_continuation,
            skipped=skipped,
        )
    return _response_from_source_outputs(
        query,
        bounds,
        tuple(jobs),
        source_outputs,
        errors,
        pages_scanned=legacy_pages_scanned,
        supplementary=supplementary,
        skipped=skipped,
    )


def source_skip_reason(
    indexer: object, query: str, bounds: SearchBounds
) -> str | None:
    """Why a source cannot answer this query, as a short user-facing reason."""

    if getattr(indexer, "catalog_family", None) == "fc2" and fc2_product_id(query) is None:
        return "仅支持 FC2 番号"
    if not getattr(indexer, "supports_keyword_search", True) and not looks_like_catalog_code(
        query
    ):
        return "仅支持完整番号"
    reason = getattr(indexer, "skip_reason", None)
    return reason(query, bounds) if callable(reason) else None


def _response_from_source_outputs(
    query: str,
    bounds: SearchBounds,
    sources: tuple[str, ...],
    source_outputs: dict[str, tuple[SearchResult, ...]],
    errors: dict[str, str],
    *,
    pages_scanned: int = 0,
    supplementary: frozenset[str] = frozenset(),
    skipped: dict[str, str] | None = None,
) -> SearchResponse:
    records = _round_robin_records(sources, source_outputs)
    works = _aggregate_records(records, supplementary=supplementary)
    if supplementary:
        works = [
            work
            for work in works
            if any(source.source_id not in supplementary for source in work.sources)
        ] + [
            work
            for work in works
            if work.code
            and all(source.source_id in supplementary for source in work.sources)
        ]
    works = _filter_works(works, bounds, query)
    ordered = _sort_works(works, bounds.sort)
    start = (bounds.page - 1) * bounds.limit
    return SearchResponse(
        query=query,
        results=tuple(ordered[start : start + bounds.limit]),
        errors=errors,
        skipped=dict(skipped or {}),
        sort=bounds.sort,
        match=bounds.match,
        found_count=len(ordered),
        pages_scanned=pages_scanned,
        pages_total=pages_scanned,
    )


def _response_from_accumulator(
    query: str,
    bounds: SearchBounds,
    accumulator: "_IncrementalSearchAccumulator",
    errors: dict[str, str],
    pages_scanned: int,
    *,
    continuation: SearchContinuation | None,
    skipped: dict[str, str] | None = None,
) -> SearchResponse:
    results = accumulator.results()
    return SearchResponse(
        query=query,
        results=results,
        errors=dict(errors),
        skipped=dict(skipped or {}),
        sort=bounds.sort,
        match=bounds.match,
        result_limit=accumulator.result_limit,
        found_count=len(results),
        pages_scanned=pages_scanned,
        pages_total=pages_scanned if continuation is None else None,
        continuation=continuation,
    )


def _response_from_results(
    query: str,
    bounds: SearchBounds,
    results: tuple[WorkResult, ...],
    errors: dict[str, str],
    pages_scanned: int,
    *,
    result_limit: int,
    continuation: SearchContinuation | None,
    skipped: dict[str, str] | None = None,
) -> SearchResponse:
    ordered = tuple(_sort_works(results, bounds.sort))
    return SearchResponse(
        query=query,
        results=ordered,
        errors=errors,
        skipped=dict(skipped or {}),
        sort=bounds.sort,
        match=bounds.match,
        result_limit=result_limit,
        found_count=len(ordered),
        pages_scanned=pages_scanned,
        pages_total=pages_scanned if continuation is None else None,
        continuation=continuation,
    )


@dataclass(frozen=True)
class _SourceSearchOutput:
    results: tuple[SearchResult, ...]
    pages_scanned: int


class _IncrementalSearchAccumulator:
    def __init__(
        self,
        query: str,
        bounds: SearchBounds,
        result_limit: int,
        sources: tuple[str, ...],
        *,
        supplementary: frozenset[str] = frozenset(),
    ) -> None:
        self.query = query
        self.bounds = bounds
        self.result_limit = result_limit
        self._supplementary = frozenset(supplementary).intersection(sources)
        primary = tuple(
            source_id for source_id in sources if source_id not in self._supplementary
        )
        if not primary:
            primary, self._supplementary = sources, frozenset()
        self._primary = primary
        # Primary sources are promoted first so that a torrent indexer's
        # records can only take slots metadata sources left unused.
        self._sources = (
            *primary,
            *(source_id for source_id in sources if source_id in self._supplementary),
        )
        self._builders: dict[tuple[str, ...], _WorkBuilder] = {}
        self._results: dict[tuple[str, ...], WorkResult] = {}
        base_quota, remainder = divmod(result_limit, len(primary))
        self._source_quotas = {
            source_id: base_quota + (index < remainder)
            for index, source_id in enumerate(primary)
        }
        self._source_quotas.update(dict.fromkeys(self._supplementary, 0))
        self._source_quota_used = dict.fromkeys(sources, 0)
        self._completed_sources: set[str] = set()
        self._shared_slots = 0
        self._pending: dict[str, deque[tuple[int, SearchResult]]] = {
            source_id: deque() for source_id in sources
        }
        # Supplementary records wait here, keyed by work identity, until a
        # primary source creates that work or every primary source finished.
        self._supplementary_pending: dict[
            tuple[str, ...], list[tuple[str, int, SearchResult]]
        ] = {}
        self._positions = 0

    @property
    def found_count(self) -> int:
        return len(self._builders)

    def add(
        self,
        source_id: str,
        records: Iterable[SearchResult],
        *,
        source_done: bool,
    ) -> tuple[WorkResult, ...]:
        changed: list[tuple[str, ...]] = []
        changed_keys: set[tuple[str, ...]] = set()
        supplementary = source_id in self._supplementary
        for result in records:
            position = self._positions
            self._positions += 1
            if not _search_result_matches(result, self.bounds, self.query):
                continue
            if supplementary:
                self._add_supplementary(
                    source_id, position, result, changed, changed_keys
                )
                continue
            if not self._apply_record(
                source_id,
                position,
                result,
                changed,
                changed_keys,
            ):
                pending = self._pending[source_id]
                if len(pending) < self.result_limit:
                    pending.append((position, result))
        if source_done and source_id not in self._completed_sources:
            self._completed_sources.add(source_id)
            self._shared_slots += max(
                0,
                self._source_quotas[source_id] - self._source_quota_used[source_id],
            )
            self._promote_pending(changed, changed_keys)
        return tuple(self._builders[key].build() for key in changed)

    def _apply_record(
        self,
        source_id: str,
        position: int,
        result: SearchResult,
        changed: list[tuple[str, ...]],
        changed_keys: set[tuple[str, ...]],
    ) -> bool:
        key, work_id, canonical_code = _record_builder_identity(
            source_id, result, position
        )
        builder = self._builders.get(key)
        if builder is None:
            if len(self._builders) >= self.result_limit or not self._reserve_slot(
                source_id
            ):
                return False
            builder = _WorkBuilder(
                work_id=work_id,
                canonical_code=canonical_code,
                code=result.code,
                title=result.title,
                supplementary=self._supplementary,
            )
            self._builders[key] = builder
            previous = None
        else:
            previous = self._results[key]
        builder.add(source_id, result)
        for waiting_source, _position, waiting in self._supplementary_pending.pop(
            key, ()
        ):
            builder.add(waiting_source, waiting)
        current = builder.build()
        self._results[key] = current
        if current != previous and key not in changed_keys:
            changed.append(key)
            changed_keys.add(key)
        return True

    def _add_supplementary(
        self,
        source_id: str,
        position: int,
        result: SearchResult,
        changed: list[tuple[str, ...]],
        changed_keys: set[tuple[str, ...]],
    ) -> None:
        if _canonical_code(result.code) is None:
            # A torrent without one identifiable code can never merge into a
            # work; in an aggregate search it would only add an image-less row.
            return
        key, _work_id, _canonical = _record_builder_identity(
            source_id, result, position
        )
        if key in self._builders or self._primary_sources_done():
            if self._apply_record(source_id, position, result, changed, changed_keys):
                return
        waiting = self._supplementary_pending.get(key)
        if waiting is None:
            if len(self._supplementary_pending) >= max(self.result_limit, 200):
                return
            waiting = self._supplementary_pending[key] = []
        if len(waiting) < 100:
            waiting.append((source_id, position, result))

    def _primary_sources_done(self) -> bool:
        return all(source_id in self._completed_sources for source_id in self._primary)

    def _reserve_slot(self, source_id: str) -> bool:
        if source_id in self._supplementary and not self._primary_sources_done():
            return False
        quota_used = self._source_quota_used[source_id]
        if quota_used < self._source_quotas[source_id]:
            self._source_quota_used[source_id] = quota_used + 1
            return True
        if self._shared_slots > 0:
            self._shared_slots -= 1
            return True
        return False

    def _promote_pending(
        self,
        changed: list[tuple[str, ...]],
        changed_keys: set[tuple[str, ...]],
    ) -> None:
        for source_id in self._sources:
            pending = self._pending[source_id]
            retained: deque[tuple[int, SearchResult]] = deque()
            while pending:
                position, result = pending.popleft()
                if not self._apply_record(
                    source_id,
                    position,
                    result,
                    changed,
                    changed_keys,
                ):
                    retained.append((position, result))
            self._pending[source_id] = retained
        if not self._supplementary or not self._primary_sources_done():
            return
        for key in tuple(self._supplementary_pending):
            waiting = self._supplementary_pending.get(key)
            if not waiting:
                continue
            source_id, position, result = waiting[0]
            if not self._apply_record(source_id, position, result, changed, changed_keys):
                break
            # _apply_record merged every waiting record for this key.

    def results(self) -> tuple[WorkResult, ...]:
        return tuple(_sort_works(self.snapshot_results(), self.bounds.sort))

    def snapshot_results(self) -> tuple[WorkResult, ...]:
        self._merge_pending_sources()
        return tuple(self._results.values())

    def _merge_pending_sources(self) -> None:
        for source_id, pending in self._pending.items():
            for position, result in pending:
                key, _, _ = _record_builder_identity(source_id, result, position)
                builder = self._builders.get(key)
                if builder is not None:
                    builder.add(source_id, result)
                    self._results[key] = builder.build()


def _validated_search_continuation(
    value: SearchContinuation | None,
    *,
    sources: tuple[str, ...],
    result_limit: int | None,
) -> SearchContinuation | None:
    if value is None:
        return None
    if not isinstance(value, SearchContinuation):
        raise ValueError("search continuation is invalid")
    if result_limit is None or result_limit < value.result_limit:
        raise ValueError("continued search result limit must increase")
    if result_limit == value.result_limit and not value.errors:
        raise ValueError("continued search result limit must increase")
    if value.pages_scanned < 0 or value.pages_scanned > 10_000:
        raise ValueError("search continuation is invalid")
    if tuple(source.source_id for source in value.sources) != sources:
        raise ValueError("search continuation sources changed")
    for source in value.sources:
        if source.next_page is not None and not (
            1 <= source.next_page <= MAX_RESULT_SCAN_PAGES
        ):
            raise ValueError("search continuation cursor is invalid")
        if len(source.records) > MAX_RESULT_SCAN_PAGES * MAX_UPSTREAM_PAGE_RESULTS:
            raise ValueError("search continuation is too large")
        if any(not isinstance(record, SearchResult) for record in source.records):
            raise ValueError("search continuation records are invalid")
    if not set(value.errors).issubset(sources) or any(
        not isinstance(message, str) for message in value.errors.values()
    ):
        raise ValueError("search continuation errors are invalid")
    return value


def _search_continuation(
    *,
    sources: tuple[str, ...],
    records: Mapping[str, Iterable[Iterable[SearchResult]]],
    next_pages: Mapping[str, int | None],
    pages_scanned: int,
    result_limit: int,
    found_count: int,
    errors: dict[str, str],
) -> SearchContinuation | None:
    if not any(next_pages.get(source_id) is not None for source_id in sources):
        return None
    if result_limit >= 999 and found_count >= result_limit:
        return None
    return SearchContinuation(
        sources=tuple(
            SearchContinuationSource(
                source_id=source_id,
                records=tuple(
                    record for chunk in records[source_id] for record in chunk
                ),
                next_page=next_pages[source_id],
            )
            for source_id in sources
        ),
        pages_scanned=pages_scanned,
        result_limit=result_limit,
        errors=dict(errors),
    )


def _search_indexer_pages(
    source_id: str,
    indexer: Indexer,
    query: str,
    bounds: SearchBounds,
    on_page: Callable[[str, int, tuple[SearchResult, ...], bool, bool], bool]
    | None = None,
    limit_reached: Callable[[], bool] | None = None,
    cancelled: Callable[[], bool] | None = None,
    *,
    start_page: int = 1,
) -> "_SourceSearchOutput":
    results: list[SearchResult] = []
    seen_pages: set[tuple[tuple[str, str | None, str | None, str, str | None], ...]] = (
        set()
    )
    remaining_detail_budget = bounds.detail_limit
    target_count = bounds.page * bounds.limit
    pages_scanned = 0
    max_pages = (
        MAX_RESULT_SCAN_PAGES
        if bounds.result_limit is not None or on_page is not None
        else MAX_UPSTREAM_PAGES
    )
    clean_start_page = max(1, min(int(start_page), max_pages))
    for page in range(clean_start_page, max_pages + 1):
        if cancelled is not None and cancelled():
            break
        if page > clean_start_page and limit_reached is not None and limit_reached():
            break
        page_bounds = replace(
            bounds,
            limit=MAX_UPSTREAM_PAGE_RESULTS,
            page=page,
            max_pages=1,
            detail_limit=remaining_detail_budget,
        )
        try:
            page_results = indexer.search(query, page_bounds)[
                :MAX_UPSTREAM_PAGE_RESULTS
            ]
        except Exception:
            if cancelled is not None and cancelled():
                break
            if on_page is not None:
                on_page(source_id, page, (), False, True)
            raise
        if cancelled is not None and cancelled():
            break
        pages_scanned += 1
        page_fingerprint = tuple(
            (result.source, result.code, result.url, result.title, result.date)
            for result in page_results
        )
        repeated_page = page_fingerprint in seen_pages
        if not repeated_page:
            seen_pages.add(page_fingerprint)
            if on_page is None:
                results.extend(page_results)
        if bounds.fetch_magnets:
            remaining_detail_budget = max(
                0,
                remaining_detail_budget
                - min(_detail_requests_used(page_results), remaining_detail_budget),
            )
        source_done = (
            repeated_page
            or not page_results
            or (
                _uses_exact_matching(bounds, query)
                and _has_exact_result(page_results, query)
            )
            or page >= max_pages
            or (
                bounds.result_limit is None
                and page >= bounds.max_pages
                and len(results) >= target_count
            )
        )
        callback_results = () if repeated_page else page_results
        if on_page is not None and on_page(
            source_id, page, callback_results, source_done, False
        ):
            break
        if source_done:
            break
    return _SourceSearchOutput(tuple(results), pages_scanned)


def _has_exact_result(results: Iterable[SearchResult], query: str) -> bool:
    """True once a page contains the single work a complete code query names.

    Prefix-only exact queries (``ABP``) name a whole series, so they keep
    scanning until the result limit instead of stopping at the first hit.
    """

    pattern = query_code_pattern(query)
    if pattern is None or pattern.number is None:
        return False
    return any(code_matches_pattern(result.code, pattern) for result in results)


def _detail_requests_used(results: Iterable[SearchResult]) -> int:
    return sum(
        1
        for result in results
        if isinstance(result.metadata, dict)
        and result.metadata.get("magnet_checked") is True
    )


def _round_robin_records(
    sources: tuple[str, ...],
    outputs: dict[str, tuple[SearchResult, ...]],
) -> list[tuple[str, SearchResult]]:
    records: list[tuple[str, SearchResult]] = []
    largest = max((len(outputs.get(source, ())) for source in sources), default=0)
    for index in range(largest):
        for source in sources:
            source_results = outputs.get(source, ())
            if index < len(source_results):
                records.append((source, source_results[index]))
    return records


@dataclass
class _MagnetBuilder:
    info_hash: str
    display_name: str | None = None
    size_bytes: int | None = None
    size_is_exact: bool = False
    source_refs: list[MagnetSourceRef] = field(default_factory=list)
    ref_keys: set[tuple[str, str]] = field(default_factory=set)

    def add(self, source_id: str, magnet: MagnetInfo) -> None:
        if not self.display_name and magnet.display_name:
            self.display_name = magnet.display_name

        exact_size = _positive_int(magnet.exact_length)
        reported_size = _positive_int(magnet.reported_size_bytes)
        if exact_size is not None and not self.size_is_exact:
            self.size_bytes = exact_size
            self.size_is_exact = True
        elif self.size_bytes is None and reported_size is not None:
            self.size_bytes = reported_size

        ref_key = (source_id, magnet.uri)
        if ref_key in self.ref_keys:
            return
        self.ref_keys.add(ref_key)
        self.source_refs.append(
            MagnetSourceRef(
                source_id=source_id,
                uri=magnet.uri,
                display_name=magnet.display_name,
                reported_size_text=magnet.reported_size_text,
                reported_size_bytes=magnet.reported_size_bytes,
                badges=tuple(magnet.badges),
                trackers=tuple(magnet.trackers),
                reported_seeders=magnet.reported_seeders,
                reported_leechers=magnet.reported_leechers,
                reported_at=magnet.reported_at,
            )
        )

    def build(self) -> MagnetGroup:
        return MagnetGroup(
            info_hash=self.info_hash,
            display_name=self.display_name,
            size_bytes=self.size_bytes,
            source_refs=tuple(self.source_refs),
            size_is_exact=self.size_is_exact,
        )


@dataclass
class _WorkBuilder:
    work_id: str
    canonical_code: str | None
    code: str | None
    title: str
    sources: list[WorkSource] = field(default_factory=list)
    source_ids: set[str] = field(default_factory=set)
    release_dates: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    actor_keys: set[str] = field(default_factory=set)
    tags: list[str] = field(default_factory=list)
    tag_keys: set[str] = field(default_factory=set)
    magnets: dict[str, _MagnetBuilder] = field(default_factory=dict)
    supplementary: frozenset[str] = frozenset()
    primary_identity: bool = False

    def add(self, source_id: str, result: SearchResult) -> None:
        if not self.primary_identity and source_id not in self.supplementary:
            # A metadata source's title and display code replace a torrent
            # release name that happened to arrive first.
            self.primary_identity = True
            if result.title.strip():
                self.title = result.title
            if result.code:
                self.code = result.code
        if source_id not in self.source_ids:
            self.source_ids.add(source_id)
            source = _work_source(source_id, result)
            self.sources.append(source)
            if source.release_date:
                self.release_dates.append(source.release_date)

        _extend_unique(self.actors, self.actor_keys, result.actors)
        _extend_unique(self.tags, self.tag_keys, result.tags)

        for magnet in result.magnets:
            info_hash = str(magnet.info_hash or "").strip().lower()
            if not info_hash:
                continue
            builder = self.magnets.get(info_hash)
            if builder is None:
                builder = _MagnetBuilder(info_hash=info_hash)
                self.magnets[info_hash] = builder
            builder.add(source_id, magnet)

    def build(self) -> WorkResult:
        release_date = _preferred_release_date(self.release_dates)
        return WorkResult(
            work_id=self.work_id,
            canonical_code=self.canonical_code,
            code=self.code,
            title=self.title,
            release_date=release_date,
            release_date_conflict=len(set(self.release_dates)) > 1,
            actors=tuple(self.actors),
            tags=tuple(self.tags),
            sources=(
                *(
                    source
                    for source in self.sources
                    if source.source_id not in self.supplementary
                ),
                *(
                    source
                    for source in self.sources
                    if source.source_id in self.supplementary
                ),
            ),
            magnets=tuple(magnet.build() for magnet in self.magnets.values()),
        )


def _aggregate_records(
    records: list[tuple[str, SearchResult]],
    *,
    supplementary: frozenset[str] = frozenset(),
) -> list[WorkResult]:
    builders: dict[tuple[str, ...], _WorkBuilder] = {}
    for position, (source_id, result) in enumerate(records):
        key, work_id, canonical_code = _record_builder_identity(
            source_id, result, position
        )

        builder = builders.get(key)
        if builder is None:
            builder = _WorkBuilder(
                work_id=work_id,
                canonical_code=canonical_code,
                code=result.code,
                title=result.title,
                supplementary=supplementary,
            )
            builders[key] = builder
        builder.add(source_id, result)
    return [builder.build() for builder in builders.values()]


def _record_builder_identity(
    source_id: str,
    result: SearchResult,
    position: int,
) -> tuple[tuple[str, ...], str, str | None]:
    canonical_code = _canonical_code(result.code)
    if canonical_code:
        aggregation_code = _aggregation_code(result.code)
        assert aggregation_code is not None
        return (
            ("code", aggregation_code),
            f"code:{canonical_code}",
            canonical_code,
        )
    identity = result.url or f"{position}:{result.title}"
    digest = hashlib.sha256(f"{source_id}\0{identity}".encode("utf-8")).hexdigest()[:20]
    return ("record", source_id, identity), f"record:{digest}", None


def _canonical_code(raw_code: str | None) -> str | None:
    return canonical_catalog_code(raw_code)


def _aggregation_code(raw_code: str | None) -> str | None:
    canonical_code = _canonical_code(raw_code)
    if canonical_code is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(raw_code or "")).strip().upper()
    simple_match = re.fullmatch(r"([A-Z]{2,12})[-._ ]?(\d{2,8})", normalized)
    if simple_match is None:
        return canonical_code
    return f"{simple_match.group(1)}{int(simple_match.group(2))}"


def _work_source(source_id: str, result: SearchResult) -> WorkSource:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    image_error = sanitize_source_error(metadata.get("images_error"))
    magnet_error = sanitize_source_error(metadata.get("magnet_error"))
    details_error = sanitize_source_error(metadata.get("details_error"))
    source_error = sanitize_source_error(metadata.get("source_error"))
    if source_error:
        parse_status = "error"
    elif (
        metadata.get("images_resolved") is True
        or metadata.get("magnet_checked") is True
        or metadata.get("details_resolved") is True
    ):
        parse_status = "resolved"
    else:
        parse_status = "summary"
    return WorkSource(
        source_id=source_id,
        raw_code=result.code,
        title=result.title,
        detail_url=result.url,
        release_date=_release_date(result.date),
        images=_source_images(metadata),
        details=result.details,
        magnet_hint=result.magnet_hint,
        parse_status=parse_status,
        error=source_error,
        details_error=details_error,
        image_error=image_error,
        magnet_error=magnet_error,
        field_sources=metadata.get("field_sources", {}),
        detail_provider=_optional_text(metadata.get("detail_provider")),
        detail_identity_verified=metadata.get("detail_identity_verified") is True,
    )


def sanitize_source_error(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    text = SOURCE_ERROR_URL_RE.sub("[redacted-url]", text)
    if SOURCE_ERROR_SENSITIVE_CONTEXT_RE.search(text):
        return SENSITIVE_SOURCE_ERROR
    return text[:500] or None


def _source_images(metadata: dict[str, Any]) -> tuple[SourceImage, ...]:
    images: list[SourceImage] = []

    cover = _optional_text(metadata.get("cover"))
    if cover:
        images.append(SourceImage(kind="cover", url=cover))

    raw_images = metadata.get("images")
    if not isinstance(raw_images, (list, tuple)):
        return normalize_source_images(images)
    for raw_image in raw_images:
        image = _coerce_source_image(raw_image)
        if image is None:
            continue
        images.append(image)
    return normalize_source_images(images)


def _coerce_source_image(value: object) -> SourceImage | None:
    if isinstance(value, SourceImage):
        return value
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    url = _optional_text(value.get("url"))
    if kind not in {"cover", "backdrop", "sample"} or not url:
        return None
    return SourceImage(
        kind=kind,
        url=url,
        thumbnail_url=_optional_text(value.get("thumbnail_url")),
        width=_positive_int(value.get("width")),
        height=_positive_int(value.get("height")),
    )


def _release_date(value: object) -> str | None:
    clean = _optional_text(value)
    if not clean:
        return None
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError:
        return None


def _preferred_release_date(values: list[str]) -> str | None:
    if not values:
        return None
    counts = Counter(values)
    highest = max(counts.values())
    return next(value for value in values if counts[value] == highest)


def _extend_unique(output: list[str], seen: set[str], values: Iterable[str]) -> None:
    for value in values:
        clean = str(value or "").strip()
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        output.append(clean)


def _sort_works(works: list[WorkResult], sort: SearchSort) -> list[WorkResult]:
    if sort == "relevance":
        return list(works)

    indexed = list(enumerate(works))
    indexed.sort(key=cmp_to_key(lambda left, right: _compare_work(left, right, sort)))
    return [work for _, work in indexed]


def _filter_works(
    works: list[WorkResult], bounds: SearchBounds, query: str
) -> list[WorkResult]:
    pattern = _exact_pattern(bounds, query)
    if pattern is None:
        return works
    return [work for work in works if code_matches_pattern(work.code, pattern)]


def _search_result_matches(
    result: SearchResult,
    bounds: SearchBounds,
    query: str,
) -> bool:
    pattern = _exact_pattern(bounds, query)
    return pattern is None or code_matches_pattern(result.code, pattern)


def _uses_exact_matching(bounds: SearchBounds, query: str) -> bool:
    return _exact_pattern(bounds, query) is not None


def _exact_pattern(bounds: SearchBounds, query: str) -> CodePattern | None:
    """The code pattern results must match, or ``None`` for unfiltered results.

    ``exact`` filters on the query's code prefix (and serial when given), so a
    site's greedy matches such as ``ABPA-001`` for ``ABP`` are dropped before
    they count toward the result limit. ``auto`` only filters complete codes.
    A query without a code-like term has nothing to match exactly against.
    """

    if bounds.search_kind not in {"keyword", "code"}:
        return None
    if bounds.match == "exact":
        return query_code_pattern(query)
    if bounds.match == "auto" and looks_like_catalog_code(query):
        pattern = query_code_pattern(query)
        return pattern if pattern is not None and pattern.number is not None else None
    return None


def _compare_work(
    left: tuple[int, WorkResult],
    right: tuple[int, WorkResult],
    sort: SearchSort,
) -> int:
    left_index, left_work = left
    right_index, right_work = right
    if sort.startswith("release_date_"):
        comparison = _compare_optional(
            left_work.release_date,
            right_work.release_date,
            descending=sort.endswith("_desc"),
        )
    else:
        comparison = _compare_optional(
            _natural_code_key(left_work),
            _natural_code_key(right_work),
            descending=sort.endswith("_desc"),
        )
    if comparison:
        return comparison
    return (left_index > right_index) - (left_index < right_index)


def _compare_optional(
    left: object | None, right: object | None, *, descending: bool
) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    comparison = (left > right) - (left < right)
    return -comparison if descending else comparison


def _natural_code_key(work: WorkResult) -> tuple[tuple[int, object, int], ...] | None:
    code = work.canonical_code or _canonical_code(work.code)
    if not code:
        return None
    tokens: list[tuple[int, object, int]] = []
    for token in NATURAL_TOKEN_RE.findall(code):
        if token.isdigit():
            tokens.append((1, int(token), len(token)))
        else:
            tokens.append((0, token, 0))
    return tuple(tokens)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None
