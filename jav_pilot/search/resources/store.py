"""SQLite store for resource search sessions, items and aggregates."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ...core.catalog_code import (
    code_matches_pattern,
    normalize_catalog_code,
    query_code_pattern,
)
from ...core.migrations import SQLiteMigration, migrate_sqlite, require_columns
from ...web_download.variant import WEB_DOWNLOAD_VARIANTS, MissavVariant
from .errors import (
    ResourceSearchConflictError,
    ResourceSearchError,
    ResourceSearchNotFoundError,
)
from .models import (
    DEFAULT_RESOURCE_SEARCH_RESULTS,
    MAX_PENDING_ITEMS,
    MAX_RESOURCE_PAGE_SIZE,
    MAX_RESOURCE_SEARCH_RESULTS,
    RESOURCE_SEARCH_SCHEMA_COMPONENT,
    RESOURCE_SEARCH_SCHEMA_VERSION,
    SESSION_ID_RE,
    TERMINAL_STATUSES,
    ResourceSearchItem,
    ResourceSearchPageEvent,
    ResourceSearchState,
    ResourceSearchWork,
    ResourceSearchWorkerResult,
)
from .schema import (
    create_schema,
    migrate_schema_v2,
    migrate_schema_v3,
    migrate_schema_v4,
    verify_schema,
    verify_schema_v1,
    verify_schema_v2,
)
from .validation import (
    bounded_int,
    make_item_id,
    public_item,
    public_session,
    require_revision,
    safe_timestamp,
    validate_error_code,
    validate_filter_keyword,
    validate_item_ids,
    validate_items,
    validate_optional_variant,
    validate_page_event,
    validate_query,
    validate_range,
    validate_range_query,
    validate_revision,
    validate_session_id,
    validate_source_id,
    validate_state,
    validate_state_transition,
    validate_worker_result,
    variants_from_json,
)

__all__ = [
    "ResourceSearchStore",
]


class ResourceSearchStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise ResourceSearchError("resource search database path must be absolute")
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.recover_interrupted()

    def create(
        self,
        source_id: object,
        query: object,
        *,
        result_limit: object = DEFAULT_RESOURCE_SEARCH_RESULTS,
        suffix_width: object | None = None,
        start: object | None = None,
        end: object | None = None,
        source_ids: Sequence[str] | None = None,
        exact_match: bool = False,
    ) -> dict[str, object]:
        if not isinstance(exact_match, bool):
            raise ResourceSearchError("resource search exact match must be a boolean")
        clean_source = "all" if source_id == "all" else validate_source_id(source_id)
        sources = tuple(validate_source_id(source) for source in (source_ids or ()))
        if clean_source == "all" and (not sources or len(set(sources)) != len(sources)):
            raise ResourceSearchError("resource search sources are invalid")
        if clean_source != "all" and source_ids is not None:
            raise ResourceSearchError("resource search sources are invalid")
        clean_query = validate_query(query)
        normalized = normalize_catalog_code(clean_query, max_length=32)
        if normalized is not None and normalized[1].startswith("FC2PPV"):
            clean_query = normalized[0]
        clean_limit = bounded_int(
            result_limit,
            "result_limit",
            1,
            MAX_RESOURCE_SEARCH_RESULTS,
        )
        clean_width, clean_start, clean_end = validate_range(
            suffix_width=suffix_width,
            start=start,
            end=end,
        )
        validate_range_query(
            clean_query,
            suffix_width=clean_width,
            start=clean_start,
            end=clean_end,
        )
        session_id = str(self._id_factory())
        if not SESSION_ID_RE.fullmatch(session_id):
            raise ResourceSearchError("resource search identity is invalid")
        now = safe_timestamp(self._clock())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for position, source in enumerate((clean_source, *sources)):
                    child_id = uuid.uuid4().hex if position else session_id
                    connection.execute(
                        """
                        INSERT INTO resource_search_sessions (
                            session_id, source_id, query, result_limit, suffix_width,
                            range_start, range_end, status, revision, item_count,
                            next_page, pending_cursor, total_pages, scanned_pages,
                            retryable, created_at, updated_at, exact_match
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 1, 0, 1, 0, NULL, 0,
                                  0, ?, ?, ?)
                        """,
                        (
                            child_id,
                            source,
                            clean_query,
                            clean_limit,
                            clean_width,
                            clean_start,
                            clean_end,
                            now,
                            now,
                            int(exact_match),
                        ),
                    )
                    if position:
                        connection.execute(
                            "INSERT INTO resource_search_members (group_id, session_id, position) VALUES (?, ?, ?)",
                            (session_id, child_id, position - 1),
                        )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ResourceSearchError("resource search identity is not unique") from exc
        return self.get(session_id)

    def member_ids(self, session_id: object) -> tuple[str, ...]:
        clean_id = validate_session_id(session_id)
        with self._connect() as connection:
            return tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT session_id FROM resource_search_members WHERE group_id = ? ORDER BY position",
                    (clean_id,),
                )
            )

    def claim(self, session_id: object) -> ResourceSearchWork | None:
        clean_id = validate_session_id(session_id)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE resource_search_sessions
                SET status = 'running', revision = revision + 1,
                    error_code = NULL, retryable = 0,
                    started_at = COALESCE(started_at, ?), heartbeat_at = ?,
                    finished_at = NULL, updated_at = ?
                WHERE session_id = ? AND status = 'queued'
                """,
                (now, now, now, clean_id),
            ).rowcount
            if not changed:
                row = _session_row(connection, clean_id)
                connection.commit()
                if row is None:
                    raise ResourceSearchNotFoundError(
                        "resource search session was not found"
                    )
                return None
            row = _session_row(connection, clean_id)
            if row is None:
                raise ResourceSearchNotFoundError(
                    "resource search session was not found"
                )
            work = _work_from_row(connection, row)
            connection.commit()
            return work

    def heartbeat(self, session_id: object) -> bool:
        clean_id = validate_session_id(session_id)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE resource_search_sessions SET heartbeat_at = ?, updated_at = ? "
                "WHERE session_id = ? AND status = 'running'",
                (now, now, clean_id),
            ).rowcount
            connection.commit()
            return changed == 1

    def append_page(
        self,
        session_id: object,
        event: ResourceSearchPageEvent,
    ) -> bool:
        clean_id = validate_session_id(session_id)
        clean_event = validate_page_event(event)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _session_row(connection, clean_id)
            if row is None:
                raise ResourceSearchNotFoundError(
                    "resource search session was not found"
                )
            if str(row["status"]) != "running":
                connection.commit()
                return False
            if row["next_page"] is None or clean_event.page != int(row["next_page"]):
                raise ResourceSearchError("resource search page event is out of order")
            validate_state_transition(row, clean_event.state)
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) FROM resource_search_items "
                "WHERE session_id = ?",
                (clean_id,),
            ).fetchone()
            ordinal = int(ordinal_row[0]) if ordinal_row is not None else 0
            # Exact sessions drop the site's greedy near-matches here, before
            # they count toward result_limit. A worker that stops early is
            # re-queued by finish() until the filtered count reaches the limit.
            exact_pattern = (
                query_code_pattern(row["query"]) if row["exact_match"] else None
            )
            for item in clean_event.items:
                normalized = normalize_catalog_code(item.code, max_length=32)
                if normalized is None:
                    raise ResourceSearchError("resource search item is invalid")
                if exact_pattern is not None and not code_matches_pattern(
                    item.code, exact_pattern
                ):
                    continue
                display_code, code_key = normalized
                item_id = make_item_id(clean_id, code_key)
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO resource_search_items (
                        item_id, session_id, code, code_key, title, ordinal, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        clean_id,
                        display_code,
                        code_key,
                        item.title,
                        ordinal + 1,
                        now,
                    ),
                ).rowcount
                if inserted:
                    ordinal += 1
                elif item.title is not None:
                    connection.execute(
                        """
                        UPDATE resource_search_items
                        SET title = ?
                        WHERE item_id = ?
                          AND (title IS NULL OR length(title) < length(?))
                        """,
                        (item.title, item_id, item.title),
                    )
                for variant in item.available_variants:
                    connection.execute(
                        "INSERT OR IGNORE INTO resource_search_variants "
                        "(item_id, variant) VALUES (?, ?)",
                        (item_id, variant),
                    )
            _replace_pending(connection, clean_id, clean_event.state.pending)
            count_row = connection.execute(
                "SELECT COUNT(*) FROM resource_search_items WHERE session_id = ?",
                (clean_id,),
            ).fetchone()
            item_count = int(count_row[0]) if count_row is not None else 0
            if item_count > int(row["result_limit"]):
                raise ResourceSearchError("resource search result limit was exceeded")
            connection.execute(
                """
                UPDATE resource_search_sessions
                SET item_count = ?, next_page = ?, pending_cursor = ?,
                    total_pages = ?, scanned_pages = ?, heartbeat_at = ?,
                    revision = revision + 1, updated_at = ?
                WHERE session_id = ? AND status = 'running'
                """,
                (
                    item_count,
                    clean_event.state.next_page,
                    clean_event.state.cursor,
                    clean_event.state.total_pages,
                    clean_event.state.scanned_pages,
                    now,
                    now,
                    clean_id,
                ),
            )
            connection.commit()
            return True

    def finish(
        self,
        session_id: object,
        result: ResourceSearchWorkerResult,
    ) -> str | None:
        clean_id = validate_session_id(session_id)
        clean_result = validate_worker_result(result)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _session_row(connection, clean_id)
            if row is None:
                raise ResourceSearchNotFoundError(
                    "resource search session was not found"
                )
            if str(row["status"]) != "running":
                connection.commit()
                return None
            validate_state_transition(row, clean_result.state)
            _replace_pending(connection, clean_id, clean_result.state.pending)
            item_count = int(row["item_count"])
            if clean_result.complete:
                status = "completed"
            elif item_count >= int(row["result_limit"]):
                status = "limit_reached"
            else:
                status = "queued"
            finished_at = now if status in TERMINAL_STATUSES else None
            connection.execute(
                """
                UPDATE resource_search_sessions
                SET status = ?, next_page = ?, pending_cursor = ?, total_pages = ?,
                    scanned_pages = ?, error_code = NULL, retryable = 0,
                    heartbeat_at = ?, finished_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE session_id = ? AND status = 'running'
                """,
                (
                    status,
                    clean_result.state.next_page,
                    clean_result.state.cursor,
                    clean_result.state.total_pages,
                    clean_result.state.scanned_pages,
                    now,
                    finished_at,
                    now,
                    clean_id,
                ),
            )
            connection.commit()
            return status

    def checkpoint(
        self,
        session_id: object,
        state: ResourceSearchState,
    ) -> bool:
        """Persist the last worker cursor before recording a transient failure.

        Page deltas normally checkpoint their cursor through ``append_page``.
        A browser challenge can time out between page events, however, and the
        worker may only have emitted a heartbeat by then.  Persisting the
        validated state here makes the retry point explicit without adding a
        retry loop or changing the worker wire protocol.
        """

        clean_id = validate_session_id(session_id)
        clean_state = validate_state(state)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _session_row(connection, clean_id)
            if row is None:
                raise ResourceSearchNotFoundError(
                    "resource search session was not found"
                )
            if str(row["status"]) != "running":
                connection.commit()
                return False
            validate_state_transition(row, clean_state)
            current_state = _work_from_row(connection, row).state
            if current_state == clean_state:
                connection.execute(
                    "UPDATE resource_search_sessions SET heartbeat_at = ?, "
                    "updated_at = ? WHERE session_id = ? AND status = 'running'",
                    (now, now, clean_id),
                )
            else:
                _replace_pending(connection, clean_id, clean_state.pending)
                connection.execute(
                    """
                    UPDATE resource_search_sessions
                    SET next_page = ?, pending_cursor = ?, total_pages = ?,
                        scanned_pages = ?, heartbeat_at = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE session_id = ? AND status = 'running'
                    """,
                    (
                        clean_state.next_page,
                        clean_state.cursor,
                        clean_state.total_pages,
                        clean_state.scanned_pages,
                        now,
                        now,
                        clean_id,
                    ),
                )
            connection.commit()
            return True

    def fail(
        self,
        session_id: object,
        code: object,
        *,
        retryable: object,
    ) -> bool:
        clean_id = validate_session_id(session_id)
        if not isinstance(retryable, bool):
            raise ResourceSearchError("resource search retry flag is invalid")
        clean_code = validate_error_code(code, retryable=retryable)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE resource_search_sessions
                SET status = 'failed', error_code = ?, retryable = ?,
                    finished_at = ?, heartbeat_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE session_id = ? AND status = 'running'
                """,
                (clean_code, int(retryable), now, now, now, clean_id),
            ).rowcount
            connection.commit()
            return changed == 1

    def continue_search(
        self,
        session_id: object,
        expected_revision: object,
        result_limit: object,
    ) -> dict[str, object]:
        clean_id = validate_session_id(session_id)
        clean_revision = validate_revision(expected_revision)
        clean_limit = bounded_int(
            result_limit,
            "result_limit",
            1,
            MAX_RESOURCE_SEARCH_RESULTS,
        )
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_session_row(connection, clean_id)
            if str(row["source_id"]) == "all":
                return _change_aggregate(
                    connection, row, clean_revision, "continue", now, clean_limit
                )
            require_revision(row, clean_revision)
            status = str(row["status"])
            if status not in {"limit_reached", "cancelled"}:
                raise ResourceSearchConflictError(
                    "resource search session cannot be continued"
                )
            if row["next_page"] is None:
                raise ResourceSearchConflictError(
                    "resource search session has no continuation cursor"
                )
            if clean_limit <= int(row["result_limit"]):
                raise ResourceSearchConflictError(
                    "continued resource search limit must increase"
                )
            connection.execute(
                """
                UPDATE resource_search_sessions
                SET result_limit = ?, status = 'queued', error_code = NULL,
                    retryable = 0, finished_at = NULL,
                    scan_generation_revision = revision + 1,
                    revision = revision + 1,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (clean_limit, now, clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def retry(
        self,
        session_id: object,
        expected_revision: object,
    ) -> dict[str, object]:
        clean_id = validate_session_id(session_id)
        clean_revision = validate_revision(expected_revision)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_session_row(connection, clean_id)
            if str(row["source_id"]) == "all":
                return _change_aggregate(connection, row, clean_revision, "retry", now)
            require_revision(row, clean_revision)
            if str(row["status"]) != "failed" or not bool(row["retryable"]):
                raise ResourceSearchConflictError(
                    "resource search session cannot be retried"
                )
            connection.execute(
                """
                UPDATE resource_search_sessions
                SET status = 'queued', error_code = NULL, retryable = 0,
                    finished_at = NULL,
                    scan_generation_revision = revision + 1,
                    revision = revision + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (now, clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def cancel(
        self,
        session_id: object,
        expected_revision: object,
    ) -> dict[str, object]:
        clean_id = validate_session_id(session_id)
        clean_revision = validate_revision(expected_revision)
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_session_row(connection, clean_id)
            if str(row["source_id"]) == "all":
                return _change_aggregate(connection, row, clean_revision, "cancel", now)
            current_revision = int(row["revision"])
            generation_revision = int(row["scan_generation_revision"])
            if not generation_revision <= clean_revision <= current_revision:
                raise ResourceSearchConflictError("resource search revision changed")
            status = str(row["status"])
            if status == "cancelled":
                connection.commit()
            elif status not in {"queued", "running"}:
                raise ResourceSearchConflictError(
                    "resource search session cannot be cancelled"
                )
            else:
                connection.execute(
                    """
                    UPDATE resource_search_sessions
                    SET status = 'cancelled', error_code = NULL, retryable = 0,
                        finished_at = ?, revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, now, clean_id),
                )
                connection.commit()
        return self.get(clean_id)

    def remove(
        self,
        session_id: object,
        expected_revision: object,
    ) -> dict[str, object]:
        clean_id = validate_session_id(session_id)
        clean_revision = validate_revision(expected_revision)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_session_row(connection, clean_id)
            if str(row["source_id"]) == "all":
                return _change_aggregate(
                    connection,
                    row,
                    clean_revision,
                    "remove",
                    safe_timestamp(self._clock()),
                )
            require_revision(row, clean_revision)
            if str(row["status"]) not in TERMINAL_STATUSES:
                raise ResourceSearchConflictError(
                    "resource search session cannot be removed"
                )
            connection.execute(
                "DELETE FROM resource_search_sessions WHERE session_id = ?",
                (clean_id,),
            )
            connection.commit()
        return {"session_id": clean_id, "removed": True}

    def get(
        self,
        session_id: object,
        *,
        limit: object = 25,
        offset: object = 0,
        keyword: object | None = None,
        variant: object | None = None,
    ) -> dict[str, object]:
        clean_id = validate_session_id(session_id)
        clean_limit = bounded_int(limit, "limit", 1, MAX_RESOURCE_PAGE_SIZE)
        clean_offset = bounded_int(offset, "offset", 0, MAX_RESOURCE_SEARCH_RESULTS)
        clean_keyword = validate_filter_keyword(keyword)
        clean_variant = validate_optional_variant(variant)
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = _required_session_row(connection, clean_id)
            if str(row["source_id"]) == "all":
                result = _aggregate_session(
                    connection,
                    row,
                    limit=clean_limit,
                    offset=clean_offset,
                    keyword=clean_keyword,
                    variant=clean_variant,
                )
                connection.commit()
                return result
            where = ["i.session_id = ?"]
            params: list[object] = [clean_id]
            if clean_keyword is not None:
                for term in clean_keyword.split(" "):
                    where.append(
                        "instr(upper(i.code || ' ' || COALESCE(i.title, '')), ?) > 0"
                    )
                    params.append(term.upper())
            if clean_variant is not None:
                where.append(
                    "EXISTS (SELECT 1 FROM resource_search_variants vf "
                    "WHERE vf.item_id = i.item_id AND vf.variant = ?)"
                )
                params.append(clean_variant)
            where_sql = " AND ".join(where)
            total_row = connection.execute(
                f"SELECT COUNT(*) FROM resource_search_items i WHERE {where_sql}",
                params,
            ).fetchone()
            filtered_total = int(total_row[0]) if total_row is not None else 0
            rows = connection.execute(
                f"""
                SELECT i.item_id, i.code, i.title
                FROM resource_search_items i
                WHERE {where_sql}
                ORDER BY i.ordinal, i.item_id
                LIMIT ? OFFSET ?
                """,
                (*params, clean_limit, clean_offset),
            ).fetchall()
            items = [public_item(connection, item_row) for item_row in rows]
            pending_count_row = connection.execute(
                "SELECT COUNT(*) FROM resource_search_pending WHERE session_id = ?",
                (clean_id,),
            ).fetchone()
            pending_count = (
                int(pending_count_row[0]) if pending_count_row is not None else 0
            )
            result = public_session(
                row,
                items=items,
                filtered_total=filtered_total,
                limit=clean_limit,
                offset=clean_offset,
                keyword=clean_keyword,
                variant=clean_variant,
                pending_count=pending_count,
            )
            result["source_ids"] = [str(row["source_id"])]
            result["sources"] = [_source_summary(row)]
            connection.commit()
            return result

    def snapshot_selected(
        self,
        session_id: object,
        expected_revision: object,
        item_ids: Sequence[object],
    ) -> tuple[dict[str, object], ...]:
        clean_id = validate_session_id(session_id)
        clean_revision = validate_revision(expected_revision)
        clean_item_ids = validate_item_ids(item_ids)
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = _required_session_row(connection, clean_id)
            if str(row["source_id"]) == "all":
                result = _aggregate_session(
                    connection, row, limit=MAX_RESOURCE_SEARCH_RESULTS
                )
                _require_aggregate_revision(
                    row, result, clean_revision, allow_progress=True
                )
                available = {item["item_id"]: item for item in result["items"]}
                if any(item_id not in available for item_id in clean_item_ids):
                    raise ResourceSearchConflictError(
                        "selected resource does not belong to this session"
                    )
                connection.commit()
                return tuple(available[item_id] for item_id in clean_item_ids)
            current_revision = int(row["revision"])
            generation_revision = int(row["scan_generation_revision"])
            if not generation_revision <= clean_revision <= current_revision:
                raise ResourceSearchConflictError("resource search revision changed")
            snapshots: list[dict[str, object]] = []
            for item_id in clean_item_ids:
                item_row = connection.execute(
                    "SELECT item_id, code, title FROM resource_search_items "
                    "WHERE session_id = ? AND item_id = ?",
                    (clean_id, item_id),
                ).fetchone()
                if item_row is None:
                    raise ResourceSearchConflictError(
                        "selected resource does not belong to this session"
                    )
                item = public_item(connection, item_row)
                variants = item["available_variants"]
                if not isinstance(variants, list) or not variants:
                    raise ResourceSearchConflictError(
                        "selected resource categories are invalid"
                    )
                snapshots.append(item)
            connection.commit()
            return tuple(snapshots)

    def queued_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM resource_search_sessions "
                "WHERE status = 'queued' AND source_id != 'all' ORDER BY created_at, session_id"
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def recover_interrupted(self) -> int:
        now = safe_timestamp(self._clock())
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE resource_search_sessions
                SET status = 'failed', error_code = 'interrupted', retryable = 1,
                    finished_at = ?, heartbeat_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now, now, now),
            ).rowcount
            connection.commit()
            return changed

    def _initialize(self) -> None:
        with self._connect() as connection:
            migrate_sqlite(
                connection,
                component=RESOURCE_SEARCH_SCHEMA_COMPONENT,
                current_version=RESOURCE_SEARCH_SCHEMA_VERSION,
                migrations=(
                    SQLiteMigration(1, create_schema, verify_schema_v1),
                    SQLiteMigration(2, migrate_schema_v2, verify_schema_v2),
                    SQLiteMigration(3, migrate_schema_v3, verify_schema),
                    SQLiteMigration(4, migrate_schema_v4, verify_schema),
                    SQLiteMigration(
                        5, _create_aggregate_schema, _verify_aggregate_schema
                    ),
                    SQLiteMigration(6, _migrate_exact_match, _verify_exact_match),
                ),
                clock=self._clock,
                verify_current=_verify_exact_match,
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()


def _create_aggregate_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE resource_search_sessions ADD COLUMN aggregate_generation_revision INTEGER NOT NULL DEFAULT 1"
    )
    connection.execute("""
        CREATE TABLE resource_search_members (
            group_id TEXT NOT NULL REFERENCES resource_search_sessions(session_id) ON DELETE CASCADE,
            session_id TEXT NOT NULL UNIQUE REFERENCES resource_search_sessions(session_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            PRIMARY KEY (group_id, position)
        )
    """)


def _verify_aggregate_schema(connection: sqlite3.Connection) -> None:
    verify_schema(connection)
    require_columns(
        connection, "resource_search_sessions", ("aggregate_generation_revision",)
    )
    require_columns(
        connection, "resource_search_members", ("group_id", "session_id", "position")
    )


def _migrate_exact_match(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE resource_search_sessions ADD COLUMN exact_match INTEGER "
        "NOT NULL DEFAULT 0 CHECK (exact_match IN (0, 1))"
    )


def _verify_exact_match(connection: sqlite3.Connection) -> None:
    _verify_aggregate_schema(connection)
    require_columns(connection, "resource_search_sessions", ("exact_match",))


def _source_summary(row: sqlite3.Row) -> dict[str, object]:
    return {
        "source_id": str(row["source_id"]),
        "status": str(row["status"]),
        "item_count": int(row["item_count"]),
        "error_code": str(row["error_code"]) if row["error_code"] else None,
        "retryable": bool(row["retryable"]),
    }


def _aggregate_members(
    connection: sqlite3.Connection, group_id: str
) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT s.* FROM resource_search_members m JOIN resource_search_sessions s "
        "ON s.session_id = m.session_id WHERE m.group_id = ? ORDER BY m.position",
        (group_id,),
    ).fetchall()
    if not rows:
        raise ResourceSearchError("resource search group has no sources")
    return rows


def _aggregate_session(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    limit: int = 25,
    offset: int = 0,
    keyword: str | None = None,
    variant: MissavVariant | None = None,
) -> dict[str, object]:
    group_id = str(row["session_id"])
    members = _aggregate_members(connection, group_id)
    result_limit = int(row["result_limit"])
    merged: dict[str, dict[str, object]] = {}
    item_rows = connection.execute(
        "SELECT i.*, s.source_id FROM resource_search_members m "
        "JOIN resource_search_sessions s ON s.session_id = m.session_id "
        "JOIN resource_search_items i ON i.session_id = m.session_id "
        "WHERE m.group_id = ? ORDER BY i.rowid",
        (group_id,),
    ).fetchall()
    for item_row in item_rows:
        item = public_item(connection, item_row)
        key = str(item_row["code_key"])
        current = merged.get(key)
        if current is None:
            item["item_id"] = make_item_id(group_id, key)
            merged[key] = item
        else:
            current["available_variants"] = [
                value
                for value in WEB_DOWNLOAD_VARIANTS
                if value in current["available_variants"]
                or value in item["available_variants"]
            ]
            current["source_ids"] = list(
                dict.fromkeys([*current["source_ids"], *item["source_ids"]])
            )
            if len(str(item["title"] or "")) > len(str(current["title"] or "")):
                current["title"] = item["title"]
    all_items = list(merged.values())
    visible = all_items[:result_limit]
    filtered = [
        item
        for item in visible
        if (variant is None or variant in item["available_variants"])
        and (
            keyword is None
            or all(
                term.upper()
                in (str(item["code"]) + " " + str(item["title"] or "")).upper()
                for term in keyword.split()
            )
        )
    ]
    statuses = {str(member["status"]) for member in members}
    active = bool(statuses & {"queued", "running"})
    overflow = len(all_items) > result_limit
    if str(row["status"]) == "cancelled":
        status = "cancelled"
    elif active:
        status = "running" if statuses != {"queued"} else "queued"
    elif statuses == {"failed"}:
        status = "failed"
    elif overflow or "limit_reached" in statuses:
        status = "limit_reached"
    else:
        status = "completed"
    retryable = any(
        member["status"] == "failed" and member["retryable"] for member in members
    )
    can_continue = (
        not active
        and result_limit < MAX_RESOURCE_SEARCH_RESULTS
        and (
            overflow
            or any(
                member["status"] in {"limit_reached", "cancelled"}
                and member["next_page"] is not None
                for member in members
            )
        )
    )
    public = public_session(
        row,
        items=filtered[offset : offset + limit],
        filtered_total=len(filtered),
        limit=limit,
        offset=offset,
        keyword=keyword,
        variant=variant,
        pending_count=0,
    )
    total_pages = (
        sum(int(member["total_pages"]) for member in members)
        if all(member["total_pages"] is not None for member in members)
        else None
    )
    public.update(
        {
            "source_ids": [str(member["source_id"]) for member in members],
            "sources": [_source_summary(member) for member in members],
            "status": status,
            "revision": int(row["revision"])
            + sum(int(member["revision"]) for member in members),
            "item_count": len(visible),
            "error_code": next(
                (
                    str(member["error_code"])
                    for member in members
                    if member["error_code"]
                ),
                None,
            )
            if status == "failed"
            else None,
            "retryable": retryable,
            "updated_at": max(
                float(member["updated_at"]) for member in [row, *members]
            ),
            "started_at": min(
                (
                    float(member["started_at"])
                    for member in members
                    if member["started_at"] is not None
                ),
                default=None,
            ),
            "finished_at": None
            if active
            else max(float(member["updated_at"]) for member in [row, *members]),
            "heartbeat_at": max(
                (
                    float(member["heartbeat_at"])
                    for member in members
                    if member["heartbeat_at"] is not None
                ),
                default=None,
            ),
            "can_continue": can_continue,
            "can_retry": not active and retryable,
            "can_cancel": active,
            "can_remove": not active,
            "progress": {
                "percent": min(99, int(len(visible) / result_limit * 100))
                if active
                else 100,
                "items_found": len(visible),
                "result_limit": result_limit,
                "scanned_pages": sum(
                    int(member["scanned_pages"]) for member in members
                ),
                "total_pages": total_pages,
                "next_page": None,
                "pending_total": max(0, len(all_items) - result_limit),
                "pending_cursor": 0,
                "pending_remaining": max(0, len(all_items) - result_limit),
                "determinate": False,
            },
        }
    )
    return public


def _require_aggregate_revision(
    row: sqlite3.Row,
    session: dict[str, object],
    expected: int,
    *,
    allow_progress: bool = False,
) -> None:
    current = int(session["revision"])
    if expected != current and not (
        allow_progress
        and int(row["aggregate_generation_revision"]) <= expected <= current
    ):
        raise ResourceSearchConflictError("resource search revision changed")


def _change_aggregate(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    expected: int,
    action: str,
    now: float,
    result_limit: int | None = None,
) -> dict[str, object]:
    session = _aggregate_session(connection, row)
    _require_aggregate_revision(
        row, session, expected, allow_progress=action == "cancel"
    )
    if not session.get(f"can_{action}"):
        raise ResourceSearchConflictError(f"resource search session cannot {action}")
    group_id = str(row["session_id"])
    members = _aggregate_members(connection, group_id)
    if action == "remove":
        connection.executemany(
            "DELETE FROM resource_search_sessions WHERE session_id = ?",
            [(str(member["session_id"]),) for member in members],
        )
        connection.execute(
            "DELETE FROM resource_search_sessions WHERE session_id = ?", (group_id,)
        )
        connection.commit()
        return {"session_id": group_id, "removed": True}
    if action == "continue" and (
        result_limit is None or result_limit <= int(row["result_limit"])
    ):
        raise ResourceSearchConflictError(
            "continued resource search limit must increase"
        )
    for member in members:
        child_id = str(member["session_id"])
        if action == "cancel" and member["status"] in {"queued", "running"}:
            connection.execute(
                "UPDATE resource_search_sessions SET status = 'cancelled', error_code = NULL, retryable = 0, "
                "finished_at = ?, updated_at = ?, revision = revision + 1 WHERE session_id = ?",
                (now, now, child_id),
            )
        elif (
            action == "retry" and member["status"] == "failed" and member["retryable"]
        ) or (
            action == "continue"
            and member["status"] in {"limit_reached", "cancelled"}
            and member["next_page"] is not None
        ):
            connection.execute(
                "UPDATE resource_search_sessions SET status = 'queued', error_code = NULL, retryable = 0, "
                "result_limit = ?, finished_at = NULL, scan_generation_revision = revision + 1, "
                "revision = revision + 1, updated_at = ? WHERE session_id = ?",
                (result_limit or int(member["result_limit"]), now, child_id),
            )
    connection.execute(
        "UPDATE resource_search_sessions SET status = ?, result_limit = ?, revision = revision + 1, "
        "aggregate_generation_revision = ?, updated_at = ? WHERE session_id = ?",
        (
            "cancelled" if action == "cancel" else "queued",
            result_limit or int(row["result_limit"]),
            int(row["aggregate_generation_revision"])
            if action == "cancel"
            else int(session["revision"]) + 1,
            now,
            group_id,
        ),
    )
    result = _aggregate_session(connection, _required_session_row(connection, group_id))
    connection.commit()
    return result


def _session_row(
    connection: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM resource_search_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()


def _required_session_row(
    connection: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row:
    row = _session_row(connection, session_id)
    if row is None:
        raise ResourceSearchNotFoundError("resource search session was not found")
    return row


def _work_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> ResourceSearchWork:
    session_id = str(row["session_id"])
    pending_rows = connection.execute(
        "SELECT code, variants_json, title FROM resource_search_pending "
        "WHERE session_id = ? ORDER BY position",
        (session_id,),
    ).fetchall()
    pending = tuple(
        ResourceSearchItem(
            code=str(pending_row["code"]),
            available_variants=variants_from_json(pending_row["variants_json"]),
            title=(
                str(pending_row["title"]) if pending_row["title"] is not None else None
            ),
        )
        for pending_row in pending_rows
    )
    state = ResourceSearchState(
        next_page=(int(row["next_page"]) if row["next_page"] is not None else None),
        pending=pending,
        cursor=int(row["pending_cursor"]),
        total_pages=(
            int(row["total_pages"]) if row["total_pages"] is not None else None
        ),
        scanned_pages=int(row["scanned_pages"]),
    )
    return ResourceSearchWork(
        session_id=session_id,
        source_id=str(row["source_id"]),
        query=str(row["query"]),
        result_limit=max(0, int(row["result_limit"]) - int(row["item_count"])),
        suffix_width=(
            int(row["suffix_width"]) if row["suffix_width"] is not None else None
        ),
        start=(int(row["range_start"]) if row["range_start"] is not None else None),
        end=(int(row["range_end"]) if row["range_end"] is not None else None),
        state=state,
    )


def _replace_pending(
    connection: sqlite3.Connection,
    session_id: str,
    pending: Sequence[ResourceSearchItem],
) -> None:
    clean_pending = validate_items(pending, maximum=MAX_PENDING_ITEMS)
    connection.execute(
        "DELETE FROM resource_search_pending WHERE session_id = ?",
        (session_id,),
    )
    for position, item in enumerate(clean_pending):
        normalized = normalize_catalog_code(item.code, max_length=32)
        if normalized is None:
            raise ResourceSearchError("resource search pending item is invalid")
        connection.execute(
            """
            INSERT INTO resource_search_pending (
                session_id, position, code, code_key, variants_json, title
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                position,
                normalized[0],
                normalized[1],
                json.dumps(
                    list(item.available_variants),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                item.title,
            ),
        )
