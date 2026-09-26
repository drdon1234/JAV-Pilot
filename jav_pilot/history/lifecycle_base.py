"""Shared state of HistoryLifecycle: database locations, connections and schema checks."""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path

from ..web_download.batches.records import materialize_expired_web_download_batches
from ..web_download.jobs import ACTIVE_STATUSES as WEB_ACTIVE_STATUSES
from .databases import (
    current_runtime_revision,
    database_path,
    path_identity,
    table_exists,
)
from .errors import HistoryLifecycleConflictError, HistoryLifecycleValidationError
from .fields import finite_seconds, sql_slots
from .models import (
    BATCH_ACTIVE_STATUSES,
    DEFAULT_PREVIEW_SECONDS,
    METADATA_ACTIVE_STATUSES,
    Preview,
)


class HistoryLifecycleBase:
    def __init__(
        self,
        *,
        web_database_path: Path | str,
        metadata_database_path: Path | str,
        media_library_database_path: Path | str,
        review_database_path: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
        preview_seconds: float = DEFAULT_PREVIEW_SECONDS,
        maintenance_guard: Callable[[], bool] = lambda: False,
        workers_stopped_guard: Callable[[], bool] = lambda: False,
        backup_verifier: Callable[[Path], Mapping[str, object]] | None = None,
        runtime_revision: object | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.web_path = database_path(web_database_path, "web download")
        self.metadata_path = database_path(metadata_database_path, "metadata")
        self.library_path = database_path(media_library_database_path, "media library")
        if len({self.web_path, self.metadata_path, self.library_path}) != 3:
            raise HistoryLifecycleValidationError(
                "history lifecycle databases must be distinct"
            )
        from ..library.schema import initialize_media_library_schema

        initialize_media_library_schema(self.library_path, clock=clock)
        self.review_path = (
            database_path(review_database_path, "metadata review")
            if review_database_path is not None
            else None
        )
        if self.review_path is None:
            with closing(sqlite3.connect(self.metadata_path, timeout=30.0)) as probe:
                if table_exists(probe, "media_metadata_reviews"):
                    self.review_path = self.metadata_path
        self._clock = clock
        self._token_factory = token_factory
        self._preview_seconds = finite_seconds(preview_seconds)
        self._maintenance_guard = maintenance_guard
        self._workers_stopped_guard = workers_stopped_guard
        self._runtime_revision = current_runtime_revision(runtime_revision)
        self._fault_hook = fault_hook or (lambda _point: None)
        if backup_verifier is None:
            from ..maintenance.backup import verify_backup

            backup_verifier = verify_backup
        self._backup_verifier = backup_verifier
        self._lock = threading.RLock()
        self._previews: dict[str, Preview] = {}
        self._identities = {
            path: path_identity(path) for path in self._all_database_paths()
        }
        self._verify_required_schema()
        self.recover_prepared_operations()

    def _verify_required_schema(self) -> None:
        requirements = (
            (self.web_path, ("web_download_jobs", "web_download_batches")),
            (self.metadata_path, ("jobs",)),
            (
                self.library_path,
                (
                    "media_library_history_facts",
                    "media_library_history_cleanup_operations",
                    "media_library_history_cleanup_candidates",
                    "media_library_history_cleanup_facts",
                    "media_library_entries",
                ),
            ),
        )
        for path, tables in requirements:
            with self._readonly(path) as connection:
                for table in tables:
                    row = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    if row is None:
                        raise HistoryLifecycleValidationError(
                            "history lifecycle database schema is incomplete"
                        )

    def _verify_database_identities(self) -> None:
        for path, expected in self._identities.items():
            if path_identity(path) != expected:
                raise HistoryLifecycleConflictError(
                    "history lifecycle database identity changed"
                )

    def _all_database_paths(self) -> tuple[Path, ...]:
        paths = [self.web_path, self.metadata_path, self.library_path]
        if self.review_path is not None and self.review_path not in paths:
            paths.append(self.review_path)
        return tuple(paths)

    @contextmanager
    def _readonly(self, path: Path) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
        finally:
            connection.close()

    def _open_review_readonly(self) -> sqlite3.Connection | None:
        if self.review_path is None:
            return None
        if self.review_path == self.metadata_path:
            return None
        connection = sqlite3.connect(
            f"file:{self.review_path.as_posix()}?mode=ro", uri=True, timeout=30.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _library_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.library_path, timeout=30.0, isolation_level=None
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _coordinated_connection(
        self,
    ) -> Iterator[tuple[sqlite3.Connection, str | None]]:
        connection = sqlite3.connect(
            self.library_path, timeout=30.0, isolation_level=None
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("ATTACH DATABASE ? AS webdb", (str(self.web_path),))
            connection.execute(
                "ATTACH DATABASE ? AS metadb", (str(self.metadata_path),)
            )
            review_schema: str | None = None
            if self.review_path is not None:
                if self.review_path == self.metadata_path:
                    review_schema = "metadb"
                elif self.review_path == self.library_path:
                    review_schema = "main"
                elif self.review_path == self.web_path:
                    review_schema = "webdb"
                else:
                    connection.execute(
                        "ATTACH DATABASE ? AS reviewdb", (str(self.review_path),)
                    )
                    review_schema = "reviewdb"
            yield connection, review_schema
        finally:
            connection.close()

    def _active_work_counts(self) -> dict[str, int]:
        materialize_expired_web_download_batches(
            self.web_path,
            clock=self._clock,
        )
        with self._readonly(self.web_path) as connection:
            web = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM web_download_jobs WHERE status IN "
                    f"({sql_slots(WEB_ACTIVE_STATUSES)})",
                    tuple(WEB_ACTIVE_STATUSES),
                ).fetchone()[0]
            )
            batches = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM web_download_batches WHERE status IN "
                    f"({sql_slots(BATCH_ACTIVE_STATUSES)})",
                    tuple(BATCH_ACTIVE_STATUSES),
                ).fetchone()[0]
            )
        with self._readonly(self.metadata_path) as connection:
            metadata = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE status IN "
                    f"({sql_slots(METADATA_ACTIVE_STATUSES)})",
                    tuple(METADATA_ACTIVE_STATUSES),
                ).fetchone()[0]
            )
        with self._readonly(self.library_path) as connection:
            library = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media_library_generations "
                    "WHERE status = 'building'"
                ).fetchone()[0]
            )
        reviews = 0
        if self.review_path is not None:
            path = self.review_path
            with self._readonly(path) as connection:
                if table_exists(connection, "media_metadata_review_refetch"):
                    reviews = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM media_metadata_review_refetch "
                            "WHERE status IN ('queued', 'running')"
                        ).fetchone()[0]
                    )
        return {
            "web": web,
            "batch": batches,
            "metadata": metadata,
            "library": library,
            "review": reviews,
        }
