"""Metadata search streaming, work detail and detail prefetch endpoints."""

from __future__ import annotations

import queue
import re
import secrets
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from http import HTTPStatus

from ...config.settings import load_settings, source_list
from ...core.catalog_code import normalize_catalog_code
from ...core.guards import QueryError, normalize_query
from ...core.models import (
    SearchBounds,
    SearchContinuation,
    SearchContinuationSource,
    SearchPageUpdate,
    SearchResponse,
    WorkResult,
)
from ...search.detail_prefetch import (
    DetailPrefetchError,
    DetailPrefetchNotFoundError,
    DetailPrefetchUnavailableError,
    DetailPrefetchValidationError,
)
from ...search.engine import default_indexers, sanitize_source_error, search
from ...search.session_store import (
    MetadataSearchNotFoundError,
    MetadataSearchStoreError,
)
from .. import state
from ..base import BaseHandler
from ..registries import SearchContinuationEnvelope, completed_stream_snapshot
from ..request import (
    filters_param,
    int_param,
    query_params,
    search_result_limit_param,
    semantic_refs_param,
    single_param,
    valid_request_id,
    valid_work_id,
)
from ..services.detail_prefetch import (
    detail_prefetch_cached_result,
    detail_prefetch_manager,
    detail_prefetch_snapshots_from_search_session,
)
from ..services.enrichment import (
    MAX_STREAM_ENRICHMENTS_PER_SOURCE,
    JavDbFetcherPool,
    code_key,
    enrich_stream_work,
    merge_continued_search_results,
    merge_stream_work,
    remember_works,
    stream_source_work,
    stream_work_needs_enrichment,
    with_source_lookup_errors,
    work_has_enrichment_errors,
    work_has_requested_sources,
)
from ..services.search_capacity import (
    acquire_search_capacity,
    inline_future,
    release_search_capacity,
)
from ..services.search_stream import (
    cache_stream_snapshot,
    inspect_stream_search_continuation,
    prepare_stream_search_continuation,
    replay_stream_snapshot,
    restore_stream_search_continuation,
    stream_search_cache_key,
    stream_search_continuation_binding,
    stream_search_continuation_mode,
    work_cache_key,
)


class SearchRoutes(BaseHandler):
    def _handle_search_stream(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            result_limit = search_result_limit_param(params)
            q = normalize_query(single_param(params, "q"))
        except QueryError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        settings = load_settings()
        sources = source_list(single_param(params, "source") or "all", settings)
        limit = int_param(params, "limit", 20)
        page = int_param(params, "page", 1)
        fetch_magnets = single_param(params, "magnets") != "0"
        filters = filters_param(params)
        semantic_refs = semantic_refs_param(params)
        sort = (
            SearchBounds(sort=single_param(params, "sort") or "relevance").normalized().sort
        )
        match = (
            SearchBounds(match=single_param(params, "match") or "auto").normalized().match
        )
        search_kind = (
            SearchBounds(search_kind=single_param(params, "kind") or "keyword")
            .normalized()
            .search_kind
        )
        bounds = SearchBounds(
            limit=limit,
            page=page,
            max_pages=3 if sort != "relevance" else 1,
            result_limit=result_limit,
            fetch_magnets=False,
            detail_limit=0,
            filters=filters,
            sort=sort,
            match=match,
            search_kind=search_kind,
            semantic_refs=semantic_refs,
        ).normalized()
        continuation_header = ""
        headers = getattr(self, "headers", None)
        header_get = getattr(headers, "get", None)
        if callable(header_get):
            continuation_header = str(
                header_get("X-Search-Continuation", "") or ""
            ).strip()
        continuation_binding = stream_search_continuation_binding(
            q,
            sources,
            bounds,
            fetch_magnets,
            settings=settings,
        )
        request_id = single_param(params, "request_id").strip()
        if not valid_request_id(request_id):
            request_id = secrets.token_urlsafe(18)
        continuation_envelope: SearchContinuationEnvelope | None = None
        if continuation_header:
            continuation_decision = inspect_stream_search_continuation(
                continuation_header,
                binding=continuation_binding,
                result_limit=result_limit,
            )
            if continuation_decision.status == "invalid":
                self._send_json(
                    {
                        "code": "continuation_invalid",
                        "error": "search continuation is invalid or expired",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if continuation_decision.status == "busy":
                self._send_json(
                    {
                        "code": "continuation_in_progress",
                        "error": "search continuation is already in use",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if continuation_decision.status == "replay":
                assert continuation_decision.snapshot is not None
                replay_cancel = state.SEARCH_JOBS.register(request_id)
                try:
                    if not self._send_sse_headers():
                        return
                    replay_stream_snapshot(
                        self,
                        continuation_decision.snapshot,
                        request_id,
                        cancelled=replay_cancel.is_set,
                    )
                finally:
                    state.SEARCH_JOBS.unregister(request_id, replay_cancel)
                return
            continuation_envelope = continuation_decision.envelope
        cache_key = stream_search_cache_key(
            q,
            sources,
            bounds,
            fetch_magnets,
            continuation_header=continuation_header,
        )
        cached_stream = state.SEARCH_CACHE.get(cache_key)
        if not continuation_header and completed_stream_snapshot(cached_stream):
            replay_cancel = state.SEARCH_JOBS.register(request_id)
            try:
                if not self._send_sse_headers():
                    return
                replay_stream_snapshot(
                    self,
                    cached_stream,
                    request_id,
                    cancelled=replay_cancel.is_set,
                )
            finally:
                state.SEARCH_JOBS.unregister(request_id, replay_cancel)
            return
        if not acquire_search_capacity(state.SEARCH_SLOTS):
            self._send_json(
                {"error": "too many active searches"}, HTTPStatus.TOO_MANY_REQUESTS
            )
            return

        cancel_event = state.SEARCH_JOBS.register(request_id)
        continuation_claimed = False
        continuation_handoff_committed = False
        continuation_lease_id: str | None = None
        next_continuation_token: str | None = None
        next_continuation_envelope: SearchContinuationEnvelope | None = None
        next_continuation_mode: str | None = None
        javdb_pool = None
        search_executor = None
        enrichment_executor = None
        stream_snapshot: dict[str, object] = {
            "sources": [],
            "base": None,
            "results": [],
            "done": None,
            "terminal": None,
        }
        client_disconnected = threading.Event()
        try:
            if continuation_envelope is not None:
                continuation_decision = state.SEARCH_CONTINUATIONS.begin(
                    continuation_header,
                    binding=continuation_binding,
                    result_limit=result_limit,
                )
                if continuation_decision.status == "replay":
                    assert continuation_decision.snapshot is not None
                    if self._send_sse_headers():
                        replay_stream_snapshot(
                            self,
                            continuation_decision.snapshot,
                            request_id,
                            cancelled=cancel_event.is_set,
                        )
                    return
                if continuation_decision.status != "claimed":
                    code = (
                        "continuation_in_progress"
                        if continuation_decision.status == "busy"
                        else "continuation_invalid"
                    )
                    self._send_json(
                        {
                            "code": code,
                            "error": (
                                "search continuation is already in use"
                                if code == "continuation_in_progress"
                                else "search continuation is invalid or expired"
                            ),
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                continuation_envelope = continuation_decision.envelope
                continuation_lease_id = continuation_decision.lease_id
                continuation_claimed = (
                    continuation_envelope is not None
                    and continuation_lease_id is not None
                )
            if not self._send_sse_headers():
                return

            registry = default_indexers(settings)
            messages: queue.Queue[tuple[str, object]] = queue.Queue()
            latest_works: dict[str, WorkResult] = {}
            enriched_sources: dict[str, dict[str, WorkResult]] = {}
            scheduled_sources: set[tuple[str, str]] = set()
            scheduled_works: dict[tuple[str, str], WorkResult] = {}
            scheduled_enrichments_by_source: dict[str, int] = {}
            enrichment_futures: set[Future] = set()
            progressive_done: set[str] = set()
            pending_enrichments = 0
            initial_continuation: SearchContinuation | None = None
            initial_results: tuple[WorkResult, ...] = ()
            initial_pages_scanned = 0
            if result_limit is not None:
                if continuation_envelope is not None:
                    initial_continuation = replace(
                        continuation_envelope.state,
                        result_limit=result_limit,
                    )
                    initial_results = continuation_envelope.works
                    initial_pages_scanned = continuation_envelope.state.pages_scanned
                else:
                    initial_continuation = SearchContinuation(
                        sources=tuple(
                            SearchContinuationSource(
                                source_id=source_id,
                                records=(),
                                next_page=1,
                            )
                            for source_id in sources
                            if source_id in registry
                        ),
                        pages_scanned=0,
                        result_limit=result_limit,
                    )
            response: SearchResponse | None = (
                SearchResponse(
                    query=q,
                    results=initial_results,
                    sort=bounds.sort,
                    match=bounds.match,
                    result_limit=result_limit,
                    found_count=len(initial_results),
                    pages_scanned=initial_pages_scanned,
                    continuation=initial_continuation,
                )
                if result_limit is not None
                else None
            )
            checkpoint_lock = threading.Lock()
            search_finished = False
            base_sent = False
            detail_bounds = replace(bounds, fetch_magnets=True, detail_limit=1)

            def stop_disconnected_client() -> None:
                client_disconnected.set()
                cancel_event.set()

            if fetch_magnets:
                javdb_pool = JavDbFetcherPool(detail_bounds)
                try:
                    enrichment_executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="search-detail",
                    )
                except (OSError, RuntimeError):
                    enrichment_executor = None

            def on_source(
                source_id: str,
                partial: SearchResponse,
                completed_sources: int,
                total_sources: int,
            ) -> None:
                if cancel_event.is_set():
                    return
                messages.put(
                    (
                        "source",
                        (source_id, partial, completed_sources, total_sources),
                    )
                )

            def on_page(update: SearchPageUpdate) -> None:
                if cancel_event.is_set():
                    return
                messages.put(("delta", update))

            def on_checkpoint(partial: SearchResponse) -> None:
                nonlocal response
                with checkpoint_lock:
                    response = partial

            def enqueue_enrichment(work: WorkResult, source_id: str) -> None:
                nonlocal enrichment_executor, pending_enrichments
                if cancel_event.is_set():
                    return
                key = (work.work_id, source_id)
                if key in scheduled_sources:
                    return
                if (
                    scheduled_enrichments_by_source.get(source_id, 0)
                    >= MAX_STREAM_ENRICHMENTS_PER_SOURCE
                ):
                    return
                source_work = stream_source_work(work, source_id)
                if source_work is None or not stream_work_needs_enrichment(
                    source_work
                ):
                    return
                scheduled_sources.add(key)
                scheduled_works[key] = source_work
                scheduled_enrichments_by_source[source_id] = (
                    scheduled_enrichments_by_source.get(source_id, 0) + 1
                )
                pending_enrichments += 1
                future: Future
                if enrichment_executor is not None:
                    try:
                        future = enrichment_executor.submit(
                            enrich_stream_work,
                            source_work,
                            registry,
                            detail_bounds,
                            javdb_pool,
                            include_images=False,
                            cancelled=cancel_event.is_set,
                        )
                    except (OSError, RuntimeError):
                        enrichment_executor.shutdown(
                            wait=True,
                            cancel_futures=True,
                        )
                        enrichment_executor = None
                        future = inline_future(
                            enrich_stream_work,
                            source_work,
                            registry,
                            detail_bounds,
                            javdb_pool,
                            include_images=False,
                            cancelled=cancel_event.is_set,
                        )
                else:
                    future = inline_future(
                        enrich_stream_work,
                        source_work,
                        registry,
                        detail_bounds,
                        javdb_pool,
                        include_images=False,
                        cancelled=cancel_event.is_set,
                    )
                enrichment_futures.add(future)
                future.add_done_callback(
                    lambda completed, source_key=key: messages.put(
                        ("enrichment", (source_key, completed))
                    )
                )

            def emit_base(search_response: SearchResponse) -> bool:
                nonlocal base_sent
                if base_sent:
                    return True
                current_response = replace(
                    search_response,
                    results=tuple(
                        merge_stream_work(work, enriched_sources.get(work.work_id, {}))
                        for work in search_response.results
                    ),
                )
                base_payload = current_response.to_dict()
                base_payload.update(
                    {
                        "page": bounds.page,
                        "limit": bounds.limit,
                        "request_id": request_id,
                        "can_continue": False,
                        "continuation_token": None,
                        "continuation_mode": None,
                    }
                )
                stream_snapshot["base"] = base_payload
                if not self._send_event("base", base_payload):
                    stop_disconnected_client()
                    return False
                base_sent = True
                return True

            search_options = {
                "sources": sources,
                "bounds": bounds,
                "indexers": registry,
                "include_torrent_sources": fetch_magnets,
                "on_source": on_source if result_limit is None else None,
                "on_page": on_page if result_limit is not None else None,
                "on_checkpoint": on_checkpoint if result_limit is not None else None,
                "cancelled": cancel_event.is_set,
                "continuation": (
                    continuation_envelope.state
                    if continuation_envelope is not None
                    else None
                ),
            }
            try:
                search_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="search-stream"
                )
                if continuation_claimed:
                    assert continuation_lease_id is not None
                    state.SEARCH_CONTINUATIONS.renew(
                        continuation_header, lease_id=continuation_lease_id
                    )
                try:
                    search_future = search_executor.submit(search, q, **search_options)
                except (OSError, RuntimeError):
                    search_executor.shutdown(wait=True, cancel_futures=True)
                    search_executor = None
                    search_future = inline_future(search, q, **search_options)
            except (OSError, RuntimeError):
                search_executor = None
                search_future = inline_future(search, q, **search_options)

            while True:
                if client_disconnected.is_set():
                    break
                if cancel_event.is_set():
                    for future in tuple(enrichment_futures):
                        future.cancel()
                    search_future.cancel()
                    with checkpoint_lock:
                        cancelled_response = response
                    if cancelled_response is None:
                        cancelled_response = SearchResponse(
                            query=q,
                            results=tuple(latest_works.values()),
                            sort=bounds.sort,
                            match=bounds.match,
                            result_limit=result_limit,
                            found_count=len(latest_works),
                        )
                    response = cancelled_response
                    latest_works = {work.work_id: work for work in response.results}
                    emit_base(response)
                    break

                try:
                    message_type, message = messages.get(timeout=0.05)
                except queue.Empty:
                    if not search_finished and search_future.done():
                        try:
                            response = search_future.result()
                        except QueryError as exc:
                            self._send_event(
                                "error", {"request_id": request_id, "error": str(exc)}
                            )
                            return
                        if cancel_event.is_set():
                            continue
                        if continuation_envelope is not None:
                            response = replace(
                                response,
                                results=merge_continued_search_results(
                                    response.results,
                                    continuation_envelope.works,
                                ),
                            )
                        prepared_continuation = prepare_stream_search_continuation(
                            continuation_binding, response
                        )
                        if prepared_continuation is not None:
                            (
                                next_continuation_token,
                                next_continuation_envelope,
                            ) = prepared_continuation
                        next_continuation_mode = stream_search_continuation_mode(
                            response
                        )
                        search_finished = True
                        latest_works = {work.work_id: work for work in response.results}
                        if fetch_magnets:
                            for work in response.results:
                                for source in work.sources:
                                    enqueue_enrichment(work, source.source_id)
                        emit_base(response)
                    if (
                        search_finished
                        and pending_enrichments == 0
                        and messages.empty()
                    ):
                        break
                    continue

                if message_type == "delta":
                    if not isinstance(message, SearchPageUpdate):
                        continue
                    if cancel_event.is_set():
                        continue
                    delta = tuple(
                        merge_stream_work(work, enriched_sources.get(work.work_id, {}))
                        for work in message.delta
                    )
                    if cancel_event.is_set():
                        continue
                    for work in message.delta:
                        latest_works[work.work_id] = work
                    delta_payload = {
                        **message.to_dict(),
                        "request_id": request_id,
                        "delta": [work.to_dict() for work in delta],
                    }
                    if cancel_event.is_set():
                        continue
                    if not self._send_event("delta", delta_payload):
                        stop_disconnected_client()
                        continue
                    if fetch_magnets:
                        for work in message.delta:
                            for source in work.sources:
                                enqueue_enrichment(work, source.source_id)
                    continue

                if message_type == "source":
                    source_id, partial, completed_sources, total_sources = message
                    if not isinstance(partial, SearchResponse):
                        continue
                    if cancel_event.is_set():
                        continue
                    enriched_partial = replace(
                        partial,
                        results=tuple(
                            merge_stream_work(
                                work, enriched_sources.get(work.work_id, {})
                            )
                            for work in partial.results
                        ),
                    )
                    if cancel_event.is_set():
                        continue
                    latest_works = {work.work_id: work for work in partial.results}
                    source_payload = enriched_partial.to_dict()
                    source_payload.update(
                        {
                            "request_id": request_id,
                            "source_id": source_id,
                            "completed_sources": completed_sources,
                            "total_sources": total_sources,
                            "page": bounds.page,
                            "limit": bounds.limit,
                        }
                    )
                    source_events = stream_snapshot["sources"]
                    if cancel_event.is_set():
                        continue
                    if isinstance(source_events, list):
                        source_events.append(source_payload)
                    if not self._send_event("source", source_payload):
                        stop_disconnected_client()
                        continue
                    if fetch_magnets:
                        for work in partial.results:
                            enqueue_enrichment(work, str(source_id))
                    continue

                if message_type != "enrichment":
                    continue
                key, completed = message
                pending_enrichments -= 1
                enrichment_futures.discard(completed)
                if cancel_event.is_set():
                    continue
                if not isinstance(completed, Future):
                    continue
                source_work = scheduled_works[key]
                try:
                    enriched = completed.result()
                except Exception as exc:  # noqa: BLE001 - preserve the work when one detail task fails.
                    failed_source = replace(
                        source_work.sources[0],
                        parse_status="error",
                        error=sanitize_source_error(exc) or "source enrichment failed",
                    )
                    enriched = replace(source_work, sources=(failed_source,))
                if cancel_event.is_set():
                    continue
                work_id, source_id = key
                enriched_sources.setdefault(work_id, {})[source_id] = enriched
                current_work = latest_works.get(work_id)
                if current_work is None:
                    continue
                progressive_done.add(work_id)
                merged = merge_stream_work(current_work, enriched_sources[work_id])
                result_payload = {
                    "request_id": request_id,
                    "work_id": work_id,
                    "done": len(progressive_done),
                    "total": max(len(latest_works), len(progressive_done)),
                    "result": merged.to_dict(),
                }
                if cancel_event.is_set():
                    continue
                if base_sent:
                    result_events = stream_snapshot["results"]
                    if isinstance(result_events, list):
                        result_events.append(result_payload)
                if not self._send_event("result", result_payload):
                    stop_disconnected_client()

            if client_disconnected.is_set() or response is None:
                return

            response = replace(
                response,
                results=tuple(
                    merge_stream_work(work, enriched_sources.get(work.work_id, {}))
                    for work in response.results
                ),
            )

            if next_continuation_token is None:
                prepared_continuation = prepare_stream_search_continuation(
                    continuation_binding, response
                )
                if prepared_continuation is not None:
                    (
                        next_continuation_token,
                        next_continuation_envelope,
                    ) = prepared_continuation
                next_continuation_mode = stream_search_continuation_mode(response)

            remember_works(response.results, sources, errors=response.errors)

            terminal_event = "cancelled" if cancel_event.is_set() else "done"
            terminal_payload = {
                "request_id": request_id,
                "done": (
                    len(progressive_done)
                    if fetch_magnets and cancel_event.is_set()
                    else len(response.results)
                    if fetch_magnets
                    else 0
                ),
                "total": len(response.results),
                "errors": response.errors,
                "skipped": response.skipped,
                "pages_scanned": response.pages_scanned,
                "pages_total": response.pages_total,
                "found_count": response.found_count,
                "result_limit": response.result_limit,
                "can_continue": next_continuation_token is not None,
                "continuation_token": next_continuation_token,
                "continuation_mode": next_continuation_mode,
            }
            if next_continuation_envelope is not None:
                next_continuation_envelope = replace(
                    next_continuation_envelope,
                    works=response.results,
                )
            stream_snapshot["done"] = terminal_payload
            stream_snapshot["terminal"] = terminal_event
            if continuation_claimed:
                assert continuation_envelope is not None
                assert continuation_lease_id is not None
                assert result_limit is not None
                continuation_handoff_committed = state.SEARCH_CONTINUATIONS.commit(
                    continuation_header,
                    lease_id=continuation_lease_id,
                    envelope=continuation_envelope,
                    result_limit=result_limit,
                    snapshot=stream_snapshot,
                    next_token=next_continuation_token,
                    next_envelope=next_continuation_envelope,
                )
                if not continuation_handoff_committed:
                    self._send_event(
                        "error",
                        {
                            "request_id": request_id,
                            "error": "search continuation expired before completion",
                        },
                    )
                    return
            elif (
                next_continuation_token is not None
                and next_continuation_envelope is not None
                and not state.SEARCH_CONTINUATIONS.remember(
                    next_continuation_token,
                    next_continuation_envelope,
                )
            ):
                self._send_event(
                    "error",
                    {
                        "request_id": request_id,
                        "error": "could not reserve search continuation",
                    },
                )
                return
            cache_stream_snapshot(cache_key, stream_snapshot)
            self._send_event(terminal_event, terminal_payload)
        finally:
            try:
                if enrichment_executor:
                    enrichment_executor.shutdown(
                        wait=True,
                        cancel_futures=True,
                    )
            finally:
                try:
                    if search_executor:
                        search_executor.shutdown(
                            wait=True,
                            cancel_futures=True,
                        )
                finally:
                    try:
                        if javdb_pool:
                            javdb_pool.close()
                    finally:
                        try:
                            state.SEARCH_JOBS.unregister(request_id, cancel_event)
                        finally:
                            try:
                                if (
                                    continuation_claimed
                                    and not continuation_handoff_committed
                                    and continuation_envelope is not None
                                    and continuation_lease_id is not None
                                ):
                                    restore_stream_search_continuation(
                                        continuation_header,
                                        continuation_envelope,
                                        lease_id=continuation_lease_id,
                                    )
                            finally:
                                release_search_capacity(state.SEARCH_SLOTS)

    def _handle_detail_prefetch_batch_create(self) -> None:
        try:
            payload = self._read_json_body(256 * 1024)
            if set(payload) != {"request_id", "work_ids", "source_scope"}:
                raise DetailPrefetchValidationError(
                    "detail prefetch request is invalid"
                )
            request_id = str(payload.get("request_id") or "").strip()
            source_scope = str(payload.get("source_scope") or "all").strip().lower()
            settings = load_settings()
            snapshots = detail_prefetch_snapshots_from_search_session(
                request_id,
                payload.get("work_ids"),
                source_scope,
                settings,
            )
            batch = detail_prefetch_manager().create(
                snapshots,
                source_scope=source_scope,
            )
        except (ValueError, DetailPrefetchValidationError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except MetadataSearchNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except MetadataSearchStoreError:
            self._send_detail_prefetch_unavailable()
            return
        except (DetailPrefetchUnavailableError, OSError, sqlite3.Error):
            self._send_detail_prefetch_unavailable()
            return
        self._send_json({"ok": True, "batch": batch}, HTTPStatus.ACCEPTED)

    def _handle_detail_prefetch_batch_action(self) -> None:
        try:
            payload = self._read_json_body(4096)
            if set(payload) != {"action", "batch_id"}:
                raise DetailPrefetchValidationError("detail prefetch action is invalid")
            if str(payload.get("action") or "").strip().lower() != "cancel":
                raise DetailPrefetchValidationError("detail prefetch action is invalid")
            batch = detail_prefetch_manager().cancel(payload.get("batch_id"))
        except (ValueError, DetailPrefetchValidationError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DetailPrefetchNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (DetailPrefetchUnavailableError, OSError, sqlite3.Error):
            self._send_detail_prefetch_unavailable()
            return
        self._send_json({"ok": True, "batch": batch})

    def _handle_detail_prefetch_batches(
        self, query_string: str, *, batch_id: str | None = None
    ) -> None:
        try:
            params = query_params(query_string)
            if batch_id is not None:
                if params:
                    raise QueryError("detail prefetch batch query is invalid")
                batch = detail_prefetch_manager().get(batch_id)
                self._send_json({"ok": True, "batch": batch})
                return
            if not set(params).issubset({"limit"}):
                raise QueryError("detail prefetch batch query is invalid")
            raw_limit = single_param(params, "limit").strip()
            if raw_limit and re.fullmatch(r"[0-9]+", raw_limit) is None:
                raise QueryError("detail prefetch batch limit is invalid")
            batches = detail_prefetch_manager().list(
                limit=int(raw_limit) if raw_limit else 20
            )
        except (QueryError, ValueError, DetailPrefetchValidationError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DetailPrefetchNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (DetailPrefetchUnavailableError, OSError, sqlite3.Error):
            self._send_detail_prefetch_unavailable()
            return
        self._send_json({"ok": True, "batches": batches})

    def _send_detail_prefetch_unavailable(self) -> None:
        self._send_json(
            {"ok": False, "error": "Detail prefetch storage is unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )

    def _handle_work(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
        except QueryError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        work_id = single_param(params, "work_id").strip()
        if work_id and not valid_work_id(work_id):
            self._send_json({"error": "invalid work id"}, HTTPStatus.BAD_REQUEST)
            return

        raw_requested = single_param(params, "code").strip()
        has_explicit_code = bool(raw_requested)
        requested = raw_requested
        if requested.lower().startswith("code:"):
            requested = requested[5:].strip()
        if not requested and work_id.lower().startswith("code:"):
            requested = work_id[5:].strip()
        if has_explicit_code:
            normalized_requested = normalize_catalog_code(requested)
            if normalized_requested is not None:
                requested = normalized_requested[0]
        requested_key = code_key(requested)
        if (
            has_explicit_code
            and work_id.lower().startswith("code:")
            and requested_key != code_key(work_id[5:])
        ):
            self._send_json(
                {"error": "work id and code do not match"}, HTTPStatus.BAD_REQUEST
            )
            return
        recovery_query = single_param(params, "q").strip()
        settings = load_settings()
        sources = source_list(single_param(params, "source") or "all", settings)
        prefetched = None
        if work_id:
            try:
                prefetched = detail_prefetch_cached_result(work_id, sources)
            except (
                DetailPrefetchError,
                OSError,
                sqlite3.Error,
            ):
                prefetched = None
        if prefetched is not None:
            self._send_json({"work": prefetched, "cached": True, "prefetched": True})
            return
        recent_work = (
            state.WORK_CACHE.get(work_cache_key(work_id, sources)) if work_id else None
        )
        if recent_work is None and not requested_key and not recovery_query:
            self._send_json(
                {"error": "work recovery query is required"}, HTTPStatus.BAD_REQUEST
            )
            return
        recovery_limit = int_param(
            params,
            "result_limit",
            int_param(params, "limit", 20),
        )
        recovery_page = int_param(params, "page", 1)
        recovery_page_size = int_param(params, "page_size", recovery_limit)
        if (
            not 1 <= recovery_limit <= 999
            or not 1 <= recovery_page <= 10_000_000
            or not 1 <= recovery_page_size <= 100
        ):
            self._send_json(
                {"error": "work recovery pagination is invalid"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        # A record can sit beyond the current visible page.  Search enough of
        # the original result window to restore it, while retaining the 999
        # server-side cap.
        recovery_limit = min(999, max(recovery_limit, recovery_page * recovery_page_size))
        recovery_sort = (
            SearchBounds(sort=single_param(params, "sort") or "relevance").normalized().sort
        )
        recovery_match = (
            SearchBounds(match=single_param(params, "match") or "auto").normalized().match
        )
        recovery_search_kind = (
            SearchBounds(search_kind=single_param(params, "kind") or "keyword")
            .normalized()
            .search_kind
        )
        recovery_filters = filters_param(params)
        recovery_semantic_refs = semantic_refs_param(params)
        cache_key = (
            "work",
            work_id or f"code:{requested_key}",
            sources,
        )
        cached = state.SEARCH_CACHE.get(cache_key)
        if cached is not None:
            payload = dict(cached)
            payload["cached"] = True
            self._send_json(payload)
            return
        if not acquire_search_capacity(state.DETAIL_SEARCH_SLOTS):
            self._send_json(
                {"error": "too many active searches"}, HTTPStatus.TOO_MANY_REQUESTS
            )
            return

        javdb_pool = None
        try:
            registry = default_indexers(settings)
            bounds = SearchBounds(
                limit=20 if requested_key else recovery_limit,
                page=1 if requested_key else recovery_page,
                max_pages=(
                    1 if requested_key else (3 if recovery_sort != "relevance" else 1)
                ),
                fetch_magnets=False,
                detail_limit=0,
                filters={} if requested_key else recovery_filters,
                sort="relevance" if requested_key else recovery_sort,
                match="exact" if requested_key else recovery_match,
                search_kind="code" if requested_key else recovery_search_kind,
                semantic_refs={} if requested_key else recovery_semantic_refs,
            ).normalized()
            work = recent_work
            lookup_errors: dict[str, str] = {}
            if work is None:
                try:
                    response = search(
                        requested if requested_key else recovery_query,
                        sources=sources,
                        bounds=bounds,
                        indexers=registry,
                    )
                except QueryError as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                lookup_errors = {
                    source: sanitize_source_error(error) or "source lookup failed"
                    for source, error in response.errors.items()
                }

                work = next(
                    (
                        result
                        for result in response.results
                        if (
                            result.work_id == work_id
                            if not requested_key
                            else result.canonical_code == requested_key
                            or code_key(result.code or "") == requested_key
                        )
                    ),
                    None,
                )
            if work is None:
                if lookup_errors:
                    self._send_json(
                        {
                            "error": "work lookup unavailable",
                            "errors": lookup_errors,
                        },
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                self._send_json({"error": "work not found"}, HTTPStatus.NOT_FOUND)
                return

            detail_bounds = replace(bounds, fetch_magnets=True, detail_limit=1)
            javdb_pool = JavDbFetcherPool(detail_bounds)
            enriched = enrich_stream_work(
                work,
                registry,
                detail_bounds,
                javdb_pool,
                include_images=True,
            )
        finally:
            try:
                if javdb_pool:
                    javdb_pool.close()
            finally:
                release_search_capacity(state.DETAIL_SEARCH_SLOTS)

        if lookup_errors:
            enriched = with_source_lookup_errors(enriched, lookup_errors)
        payload = {"work": enriched.to_dict(), "cached": False}
        if lookup_errors:
            payload["errors"] = lookup_errors
        has_all_sources = work_has_requested_sources(enriched, sources)
        if not lookup_errors and has_all_sources:
            state.WORK_CACHE.set(work_cache_key(enriched.work_id, sources), enriched)
            if not work_has_enrichment_errors(enriched):
                state.SEARCH_CACHE.set(cache_key, payload)
        self._send_json(payload)

    def _handle_search_cancel(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        raw_request_id = payload.get("request_id")
        if set(payload) != {"request_id"} or not isinstance(raw_request_id, str):
            self._send_json(
                {"ok": False, "error": "invalid request_id"}, HTTPStatus.BAD_REQUEST
            )
            return
        request_id = raw_request_id.strip()
        if not valid_request_id(request_id):
            self._send_json(
                {"ok": False, "error": "invalid request_id"}, HTTPStatus.BAD_REQUEST
            )
            return
        self._send_json({"ok": True, "cancelled": state.SEARCH_JOBS.cancel(request_id)})
