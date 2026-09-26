"""Cleanup and retention previews: selecting candidates and holding preview tokens."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping

from ..web_download.batches.records import materialize_expired_web_download_batches
from ..web_download.jobs import ACTIVE_STATUSES as WEB_ACTIVE_STATUSES
from .candidates import (
    batch_candidate,
    batch_matches,
    complete_batch_chain,
    metadata_candidate,
    parse_criteria,
    sql_filter,
    web_candidate,
)
from .databases import review_in_progress
from .errors import HistoryLifecycleConflictError, HistoryLifecycleError
from .facts import metadata_nfo_provenance
from .fields import bounded_int, sql_slots, timestamp, validated_preview_token
from .lifecycle_base import HistoryLifecycleBase
from .models import (
    BATCH_ACTIVE_STATUSES,
    MAX_CLEANUP_RECORDS,
    MAX_EXPORT_RECORDS,
    MAX_PREVIEWS,
    TASK_TERMINAL_VALUES,
    TASK_TYPES,
    Candidate,
    Criteria,
    Preview,
    RetentionPolicy,
    Skipped,
)


class HistoryPreviewMixin(HistoryLifecycleBase):
    def preview_cleanup(
        self, filters: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        criteria = parse_criteria(filters, maximum=MAX_CLEANUP_RECORDS)
        return self._create_preview(criteria)

    def preview_retention(
        self,
        policy: RetentionPolicy | Mapping[str, object] | None = None,
        *,
        limit: object = MAX_CLEANUP_RECORDS,
    ) -> dict[str, object]:
        clean_policy = (
            policy
            if isinstance(policy, RetentionPolicy)
            else RetentionPolicy.from_mapping(policy)
        )
        enabled = clean_policy.enabled()
        if not enabled:
            return {
                "enabled": False,
                "preview_token": None,
                "expires_at": None,
                "selected": {"records": 0, "groups": 0, "estimated_bytes": 0},
                "items": [],
                "skipped": {"counts": {}, "items": [], "details_truncated": False},
                "policy": {task_type: None for task_type in TASK_TYPES},
            }
        now = timestamp(self._clock())
        clean_limit = bounded_int(
            limit, "history cleanup limit", 1, MAX_CLEANUP_RECORDS
        )
        criteria = Criteria(
            task_types=tuple(
                task_type for task_type in TASK_TYPES if task_type in enabled
            ),
            statuses=tuple(
                (task_type, tuple(sorted(TASK_TERMINAL_VALUES[task_type])))
                for task_type in TASK_TYPES
                if task_type in enabled
            ),
            created_after=None,
            created_before=None,
            updated_after=None,
            updated_before=None,
            code_query=None,
            limit=clean_limit,
            retention_cutoffs=tuple(
                (task_type, now - enabled[task_type] * 86_400.0)
                for task_type in TASK_TYPES
                if task_type in enabled
            ),
        )
        result = self._create_preview(criteria)
        result["enabled"] = True
        result["policy"] = {
            task_type: enabled.get(task_type) for task_type in TASK_TYPES
        }
        return result

    def _create_preview(self, criteria: Criteria) -> dict[str, object]:
        self._verify_database_identities()
        candidates, skipped = self._select_candidates(criteria)
        now = timestamp(self._clock())
        token = validated_preview_token(self._token_factory())
        preview = Preview(
            token,
            now,
            now + self._preview_seconds,
            criteria,
            tuple(candidates),
        )
        with self._lock:
            self._expire_previews(now)
            if token in self._previews:
                raise HistoryLifecycleConflictError(
                    "history cleanup preview identifier collided"
                )
            if len(self._previews) >= MAX_PREVIEWS:
                oldest = min(self._previews.values(), key=lambda item: item.created_at)
                self._previews.pop(oldest.token, None)
            self._previews[token] = preview
        return {
            "preview_token": token,
            "created_at": now,
            "expires_at": preview.expires_at,
            "filters": criteria.public(),
            "selected": {
                "records": sum(item.record_count for item in candidates),
                "groups": len(candidates),
                "estimated_bytes": sum(item.estimated_bytes for item in candidates),
                "by_type": dict(
                    sorted(Counter(item.task_type for item in candidates).items())
                ),
            },
            "items": [item.public() for item in candidates],
            "skipped": skipped.public(),
        }

    def _select_candidates(
        self, criteria: Criteria
    ) -> tuple[list[Candidate], Skipped]:
        materialize_expired_web_download_batches(
            self.web_path,
            clock=self._clock,
        )
        skipped = Skipped()
        candidates: list[Candidate] = []
        remaining = criteria.limit
        if "batch" in criteria.task_types:
            for candidate, reason in self._batch_candidates(criteria):
                if reason is not None:
                    skipped.add("batch", candidate.identity, reason)
                    continue
                if candidate.record_count > remaining:
                    skipped.add("batch", candidate.identity, "limit_boundary")
                    continue
                candidates.append(candidate)
                remaining -= candidate.record_count
                if remaining == 0:
                    break
        if remaining and "web" in criteria.task_types:
            for candidate, reason in self._job_candidates("web", criteria):
                if reason is not None:
                    skipped.add("web", candidate.identity, reason)
                    continue
                candidates.append(candidate)
                remaining -= 1
                if remaining == 0:
                    break
        if remaining and "metadata" in criteria.task_types:
            for candidate, reason in self._job_candidates("metadata", criteria):
                if reason is not None:
                    skipped.add("metadata", candidate.identity, reason)
                    continue
                candidates.append(candidate)
                remaining -= 1
                if remaining == 0:
                    break
        with (
            self._readonly(self.web_path) as web,
            self._readonly(self.metadata_path) as metadata,
        ):
            review = (
                metadata
                if self.review_path == self.metadata_path
                else self._open_review_readonly()
            )
            try:
                candidates = self._remove_preview_dependencies(
                    web, metadata, review, candidates, skipped
                )
            finally:
                if review is not None and review is not metadata:
                    review.close()
        return candidates, skipped

    def _batch_candidates(
        self, criteria: Criteria
    ) -> Iterator[tuple[Candidate, str | None]]:
        with self._readonly(self.web_path) as connection:
            rows = connection.execute(
                "SELECT * FROM web_download_batches "
                "ORDER BY updated_at, root_chain_id, page_number "
                "LIMIT ?",
                (MAX_EXPORT_RECORDS,),
            ).fetchall()
            roots: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                root_id = str(row["root_chain_id"] or row["batch_id"])
                roots.setdefault(root_id, []).append(row)
            ordered = sorted(
                roots.items(),
                key=lambda item: (
                    max(float(row["updated_at"]) for row in item[1]),
                    item[0],
                ),
            )
            for root_id, chain_rows in ordered:
                if not batch_matches(criteria, chain_rows):
                    continue
                items = connection.execute(
                    "SELECT * FROM web_download_batch_items "
                    f"WHERE batch_id IN ({sql_slots(chain_rows)}) "
                    "ORDER BY batch_id, position",
                    tuple(str(row["batch_id"]) for row in chain_rows),
                ).fetchall()
                candidate = batch_candidate(root_id, chain_rows, items)
                if not complete_batch_chain(root_id, chain_rows):
                    yield candidate, "incomplete_batch_chain"
                    continue
                if any(
                    str(row["status"]) in BATCH_ACTIVE_STATUSES for row in chain_rows
                ):
                    yield candidate, "active_task"
                    continue
                active_job = connection.execute(
                    "SELECT 1 FROM web_download_batch_items i "
                    "JOIN web_download_jobs j ON j.job_id = i.job_id "
                    f"WHERE i.batch_id IN ({sql_slots(chain_rows)}) "
                    f"AND j.status IN ({sql_slots(WEB_ACTIVE_STATUSES)}) LIMIT 1",
                    (
                        *(str(row["batch_id"]) for row in chain_rows),
                        *WEB_ACTIVE_STATUSES,
                    ),
                ).fetchone()
                if active_job is not None:
                    yield candidate, "active_task"
                    continue
                yield candidate, None

    def _job_candidates(
        self, task_type: str, criteria: Criteria
    ) -> Iterator[tuple[Candidate, str | None]]:
        path = self.web_path if task_type == "web" else self.metadata_path
        table = "web_download_jobs" if task_type == "web" else "jobs"
        clauses, values = sql_filter(criteria, task_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        id_column = "job_id"
        with self._readonly(path) as connection:
            rows = connection.execute(
                f"SELECT * FROM {table}{where} "
                f"ORDER BY updated_at, {id_column} LIMIT ?",
                (*values, MAX_EXPORT_RECORDS),
            ).fetchall()
            for row in rows:
                candidate = (
                    web_candidate(row)
                    if task_type == "web"
                    else metadata_candidate(row)
                )
                status = str(row["status"])
                if status not in TASK_TERMINAL_VALUES[task_type]:
                    yield candidate, "active_task"
                    continue
                if task_type == "metadata":
                    try:
                        metadata_nfo_provenance(row)
                    except HistoryLifecycleError:
                        yield candidate, "provenance_invalid"
                        continue
                yield candidate, None

    def _remove_preview_dependencies(
        self,
        web: sqlite3.Connection,
        metadata: sqlite3.Connection,
        review: sqlite3.Connection | None,
        candidates: list[Candidate],
        skipped: Skipped,
    ) -> list[Candidate]:
        current = list(candidates)
        while True:
            batch_roots = {
                item.identity for item in current if item.task_type == "batch"
            }
            metadata_ids = {
                item.identity for item in current if item.task_type == "metadata"
            }
            rejected: dict[tuple[str, str], str] = {}
            for item in current:
                if item.task_type == "metadata" and review is not None:
                    row = metadata.execute(
                        "SELECT code_key, relative_media_path FROM jobs WHERE job_id = ?",
                        (item.identity,),
                    ).fetchone()
                    if row is not None and review_in_progress(review, row):
                        rejected[(item.task_type, item.identity)] = "review_in_progress"
                if item.task_type != "web":
                    continue
                linked_roots = {
                    str(row[0])
                    for row in web.execute(
                        "SELECT DISTINCT b.root_chain_id "
                        "FROM web_download_batch_items i "
                        "JOIN web_download_batches b ON b.batch_id = i.batch_id "
                        "WHERE i.job_id = ?",
                        (item.identity,),
                    ).fetchall()
                }
                if linked_roots - batch_roots:
                    rejected[(item.task_type, item.identity)] = "linked_batch_history"
                    continue
                linked_metadata = {
                    str(row[0])
                    for row in metadata.execute(
                        "SELECT job_id FROM jobs WHERE kind = 'web' AND download_key = ?",
                        (item.identity,),
                    ).fetchall()
                }
                if linked_metadata - metadata_ids:
                    rejected[(item.task_type, item.identity)] = (
                        "linked_metadata_history"
                    )
            if not rejected:
                return current
            next_items: list[Candidate] = []
            for item in current:
                reason = rejected.get((item.task_type, item.identity))
                if reason is None:
                    next_items.append(item)
                else:
                    skipped.add(item.task_type, item.identity, reason)
            current = next_items

    def _expire_previews(self, now: float) -> None:
        expired = [
            token
            for token, preview in self._previews.items()
            if now >= preview.expires_at
        ]
        for token in expired:
            self._previews.pop(token, None)
