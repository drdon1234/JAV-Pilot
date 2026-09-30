"""SQLite state: subtitle jobs, their last ranked candidates, files written."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..core.catalog_code import normalize_catalog_code
from ..core.migrations import (
    MigrationError,
    SQLiteMigration,
    SchemaTooNewError,
    migrate_sqlite,
    require_columns,
)
from ..library.errors import MediaLibraryError
from ..library.filesystem import safe_relative_path
from ..web_download.variant import WEB_DOWNLOAD_VARIANTS
from .models import PROVIDER_IDS, SCRIPTS
from .sidecars import is_subtitle_sidecar, renamed_sidecar

SCHEMA_COMPONENT = "subtitles"
CURRENT_SCHEMA_VERSION = 1
STATUSES = (
    "queued",
    "running",
    "retry",
    "completed",
    "not_found",
    "existing",
    "skipped",
    "failed",
    "removed",
)
ORIGINS = ("auto", "manual", "batch")
ACTIVE_STATUSES = ("queued", "running", "retry")
FINAL_OUTCOMES = ("existing", "skipped", "failed")
STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "all": (),
    "waiting": ("queued", "retry"),
    "running": ("running",),
    "completed": ("completed",),
    "not_found": ("not_found",),
    "skipped": ("existing", "skipped", "removed"),
    "failed": ("failed",),
}
NOT_FOUND_RECHECK_SECONDS = (7 * 86_400.0, 30 * 86_400.0)
MAX_CANDIDATE_JSON_BYTES = 256 * 1024
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CANDIDATE_ID_RE = re.compile(r"^[a-z]+-[0-9a-f]{20}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_SELECT = (
    "SELECT j.*, f.relative_path AS subtitle_path, f.script AS subtitle_script "
    "FROM subtitle_jobs j LEFT JOIN subtitle_files f ON f.job_id = j.job_id"
)


class SubtitleStoreError(RuntimeError):
    pass


class SubtitleJobNotFound(SubtitleStoreError):
    pass


class SubtitleStoreConflict(SubtitleStoreError):
    pass


@dataclass(frozen=True, slots=True)
class SubtitleFileRecord:
    relative_path: str
    sha256: str
    size: int
    mtime_ns: int
    script: str
    provider: str
    media_size: int
    media_mtime_ns: int


class SubtitleStore:
    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise SubtitleStoreError("subtitle database path must be absolute")
        self._clock = clock
        self._id_factory = id_factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def now(self) -> float:
        return _timestamp(self._clock())

    # ---- enqueue -------------------------------------------------------

    def enqueue_auto(
        self,
        relative_media_path: object,
        code: object,
        variant: object | None,
        *,
        media_size: int,
        media_mtime_ns: int,
    ) -> dict[str, object] | None:
        """Queue a newly published media file; ``None`` when nothing changed.

        An existing job is left alone, so a republished NFO neither fetches
        again nor undoes a removal, unless the path now holds another work or
        the media file was replaced after its subtitle was written.
        """

        path = _clean_path(relative_media_path)
        display, key = _clean_code(code)
        clean_variant = _clean_variant(variant)
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM subtitle_jobs WHERE relative_media_path = ?", (path,)
            ).fetchone()
            if row is None:
                job_id = self._insert(connection, path, display, key, clean_variant, "auto", now)
                connection.commit()
                return self.get(job_id)
            job_id = str(row["job_id"])
            reset = str(row["code_key"]) != key
            if not reset and str(row["status"]) == "completed":
                media = connection.execute(
                    "SELECT media_size, media_mtime_ns FROM subtitle_files WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                reset = media is not None and (
                    int(media["media_size"]),
                    int(media["media_mtime_ns"]),
                ) != (int(media_size), int(media_mtime_ns))
            if not reset or str(row["status"]) == "running":
                connection.commit()
                return None
            self._reset(connection, job_id, display, key, clean_variant, "auto", now)
            connection.commit()
        return self.get(job_id)

    def enqueue_request(
        self,
        relative_media_path: object,
        code: object,
        variant: object | None,
        *,
        origin: str,
    ) -> dict[str, object] | None:
        """Queue a manual or batch request; a batch never undoes a removal."""

        if origin not in {"manual", "batch"}:
            raise SubtitleStoreError("subtitle request origin is invalid")
        path = _clean_path(relative_media_path)
        display, key = _clean_code(code)
        clean_variant = _clean_variant(variant)
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM subtitle_jobs WHERE relative_media_path = ?", (path,)
            ).fetchone()
            if row is None:
                job_id = self._insert(connection, path, display, key, clean_variant, origin, now)
            else:
                job_id = str(row["job_id"])
                status = str(row["status"])
                if origin == "batch" and status == "removed":
                    connection.commit()
                    return None
                if status not in ACTIVE_STATUSES:
                    self._reset(connection, job_id, display, key, clean_variant, origin, now)
            connection.commit()
        return self.get(job_id)

    # ---- worker transitions -------------------------------------------

    def claim_ready(self, now: float | None = None) -> dict[str, object] | None:
        claim_time = self.now() if now is None else _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_id FROM subtitle_jobs
                WHERE (status IN ('queued', 'retry')
                       OR (status = 'not_found' AND next_attempt_at > 0))
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at, created_at, job_id
                LIMIT 1
                """,
                (claim_time,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job_id = str(row["job_id"])
            connection.execute(
                "UPDATE subtitle_jobs SET status = 'running', attempts = attempts + 1, "
                "updated_at = ? WHERE job_id = ?",
                (claim_time, job_id),
            )
            connection.commit()
        return self.get(job_id)

    def set_completed(
        self,
        job_id: object,
        *,
        provider: str,
        candidate_id: str,
        file: SubtitleFileRecord,
    ) -> dict[str, object]:
        clean_id = _clean_job_id(job_id)
        _validate_file_record(file)
        if provider not in PROVIDER_IDS or not _CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise SubtitleStoreError("subtitle selection is invalid")
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_status(connection, clean_id, ("running",))
            connection.execute(
                """
                INSERT INTO subtitle_files (
                    job_id, relative_path, sha256, size, mtime_ns, script, provider,
                    media_size, media_mtime_ns, written_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    relative_path = excluded.relative_path,
                    sha256 = excluded.sha256,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    script = excluded.script,
                    provider = excluded.provider,
                    media_size = excluded.media_size,
                    media_mtime_ns = excluded.media_mtime_ns,
                    written_at = excluded.written_at
                """,
                (
                    clean_id,
                    file.relative_path,
                    file.sha256,
                    file.size,
                    file.mtime_ns,
                    file.script,
                    file.provider,
                    file.media_size,
                    file.media_mtime_ns,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE subtitle_jobs
                SET status = 'completed', reason = NULL, attempts = 0,
                    not_found_checks = 0, next_attempt_at = 0,
                    selected_provider = ?, selected_candidate_id = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (provider, candidate_id, now, clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def set_outcome(self, job_id: object, status: str, reason: str) -> dict[str, object]:
        if status not in FINAL_OUTCOMES:
            raise SubtitleStoreError("subtitle outcome is invalid")
        return self._finish(job_id, status=status, reason=reason, next_attempt_at=0.0)

    def set_retry(self, job_id: object, reason: str, next_attempt_at: float) -> dict[str, object]:
        return self._finish(
            job_id,
            status="retry",
            reason=reason,
            next_attempt_at=_timestamp(next_attempt_at),
            keep_attempts=True,
        )

    def set_not_found(self, job_id: object, reason: str) -> dict[str, object]:
        clean_id = _clean_job_id(job_id)
        clean_reason = _clean_reason(reason)
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_status(connection, clean_id, ("running",))
            checks = int(row["not_found_checks"])
            next_attempt_at = (
                now + NOT_FOUND_RECHECK_SECONDS[checks]
                if checks < len(NOT_FOUND_RECHECK_SECONDS)
                else 0.0
            )
            connection.execute(
                "UPDATE subtitle_jobs SET status = 'not_found', reason = ?, attempts = 0, "
                "not_found_checks = ?, next_attempt_at = ?, updated_at = ? WHERE job_id = ?",
                (clean_reason, checks + 1, next_attempt_at, now, clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def recover_running(self) -> int:
        now = self.now()
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE subtitle_jobs SET status = 'queued', next_attempt_at = ?, "
                "updated_at = ? WHERE status = 'running'",
                (now, now),
            ).rowcount
        return int(changed)

    # ---- manual operations ----------------------------------------------

    def lease(self, job_id: object) -> dict[str, object]:
        """Hold a job for a synchronous manual selection; returns the prior state."""

        clean_id = _clean_job_id(job_id)
        previous = self.get(clean_id)
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, clean_id)
            if str(row["status"]) == "running":
                connection.rollback()
                raise SubtitleStoreConflict("subtitle job is running")
            connection.execute(
                "UPDATE subtitle_jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
                (now, clean_id),
            )
            connection.commit()
        return previous

    def release(self, job_id: object, previous: Mapping[str, object]) -> None:
        clean_id = _clean_job_id(job_id)
        status = str(previous.get("status"))
        if status not in STATUSES or status == "running":
            raise SubtitleStoreError("subtitle job state is invalid")
        with self._connect() as connection:
            connection.execute(
                "UPDATE subtitle_jobs SET status = ?, reason = ?, attempts = ?, "
                "next_attempt_at = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
                (
                    status,
                    previous.get("reason"),
                    int(previous.get("attempts") or 0),
                    float(previous.get("next_attempt_at") or 0.0),
                    self.now(),
                    clean_id,
                ),
            )

    def mark_removed(self, job_id: object) -> dict[str, object]:
        clean_id = _clean_job_id(job_id)
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_status(connection, clean_id, ("completed",))
            connection.execute("DELETE FROM subtitle_files WHERE job_id = ?", (clean_id,))
            connection.execute(
                "UPDATE subtitle_jobs SET status = 'removed', reason = NULL, "
                "selected_provider = NULL, selected_candidate_id = NULL, next_attempt_at = 0, "
                "updated_at = ? WHERE job_id = ?",
                (now, clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def forget(self, job_id: object) -> None:
        clean_id = _clean_job_id(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, clean_id)
            if str(row["status"]) == "running":
                connection.rollback()
                raise SubtitleStoreConflict("subtitle job is running")
            connection.execute("DELETE FROM subtitle_jobs WHERE job_id = ?", (clean_id,))
            connection.commit()

    def relocate_media(
        self,
        old_relative_path: object,
        new_relative_path: object,
        *,
        replace_stale: bool = False,
    ) -> bool:
        """Follow a media rename; the owned subtitle is renamed the same way.

        ``replace_stale`` drops a job still recorded at the new path. Callers
        pass it only after verifying that no media file exists there.
        """

        old_path = _clean_path(old_relative_path)
        new_path = _clean_path(new_relative_path)
        if old_path == new_path:
            return False
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id FROM subtitle_jobs WHERE relative_media_path = ?", (old_path,)
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            if connection.execute(
                "SELECT 1 FROM subtitle_jobs WHERE relative_media_path = ?", (new_path,)
            ).fetchone() is not None:
                if not replace_stale:
                    connection.rollback()
                    raise SubtitleStoreConflict("another subtitle job owns the new media path")
                connection.execute(
                    "DELETE FROM subtitle_jobs WHERE relative_media_path = ?", (new_path,)
                )
            job_id = str(row["job_id"])
            connection.execute(
                "UPDATE subtitle_jobs SET relative_media_path = ?, updated_at = ? WHERE job_id = ?",
                (new_path, now, job_id),
            )
            owned = connection.execute(
                "SELECT relative_path FROM subtitle_files WHERE job_id = ?", (job_id,)
            ).fetchone()
            if owned is not None:
                moved = moved_subtitle_path(str(owned["relative_path"]), old_path, new_path)
                if moved is not None:
                    connection.execute(
                        "UPDATE subtitle_files SET relative_path = ? WHERE job_id = ?",
                        (moved, job_id),
                    )
            connection.commit()
        return True

    # ---- candidates and files -------------------------------------------

    def save_candidates(self, job_id: object, records: Sequence[Mapping[str, object]]) -> None:
        clean_id = _clean_job_id(job_id)
        kept = [dict(record) for record in records]
        encoded = json.dumps(kept, ensure_ascii=False, separators=(",", ":"))
        while kept and len(encoded.encode("utf-8")) > MAX_CANDIDATE_JSON_BYTES:
            kept = kept[: len(kept) * 3 // 4]
            encoded = json.dumps(kept, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO subtitle_candidates (job_id, searched_at, candidates_json) "
                "VALUES (?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
                "searched_at = excluded.searched_at, candidates_json = excluded.candidates_json",
                (clean_id, self.now(), encoded),
            )

    def load_candidates(self, job_id: object) -> tuple[float, list[dict[str, object]]] | None:
        clean_id = _clean_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT searched_at, candidates_json FROM subtitle_candidates WHERE job_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            records = json.loads(str(row["candidates_json"]))
        except json.JSONDecodeError as exc:
            raise SubtitleStoreError("subtitle candidates could not be read") from exc
        if not isinstance(records, list):
            raise SubtitleStoreError("subtitle candidates could not be read")
        return float(row["searched_at"]), [item for item in records if isinstance(item, dict)]

    def file_record(self, job_id: object) -> SubtitleFileRecord | None:
        clean_id = _clean_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subtitle_files WHERE job_id = ?", (clean_id,)
            ).fetchone()
        if row is None:
            return None
        return SubtitleFileRecord(
            relative_path=str(row["relative_path"]),
            sha256=str(row["sha256"]),
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            script=str(row["script"]),
            provider=str(row["provider"]),
            media_size=int(row["media_size"]),
            media_mtime_ns=int(row["media_mtime_ns"]),
        )

    # ---- reads -----------------------------------------------------------

    def get(self, job_id: object) -> dict[str, object]:
        clean_id = _clean_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(f"{_JOB_SELECT} WHERE j.job_id = ?", (clean_id,)).fetchone()
        if row is None:
            raise SubtitleJobNotFound("subtitle job was not found")
        return _row_to_job(row)

    def get_by_path(self, relative_media_path: object) -> dict[str, object] | None:
        path = _clean_path(relative_media_path)
        with self._connect() as connection:
            row = connection.execute(
                f"{_JOB_SELECT} WHERE j.relative_media_path = ?", (path,)
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        status_filter: str = "all",
        query: object | None = None,
    ) -> list[dict[str, object]]:
        where, values = _filters(status_filter, query)
        clean_limit = _bounded(limit, 1, 500)
        clean_offset = _bounded(offset, 0, 10_000_000)
        with self._connect() as connection:
            rows = connection.execute(
                f"{_JOB_SELECT}{where} ORDER BY j.updated_at DESC, j.job_id DESC LIMIT ? OFFSET ?",
                (*values, clean_limit, clean_offset),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count(self, *, status_filter: str = "all", query: object | None = None) -> int:
        where, values = _filters(status_filter, query)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM subtitle_jobs j{where}", values
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(status IN ('queued', 'retry')) AS waiting,
                    SUM(status = 'running') AS running,
                    SUM(status = 'completed') AS completed,
                    SUM(status = 'not_found') AS not_found,
                    SUM(status IN ('existing', 'skipped', 'removed')) AS skipped,
                    SUM(status = 'failed') AS failed
                FROM subtitle_jobs
                """
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    # ---- internals -------------------------------------------------------

    def _insert(
        self,
        connection: sqlite3.Connection,
        path: str,
        display: str,
        key: str,
        variant: str | None,
        origin: str,
        now: float,
    ) -> str:
        job_id = _clean_job_id(self._id_factory())
        connection.execute(
            """
            INSERT INTO subtitle_jobs (
                job_id, relative_media_path, code, code_key, variant, origin, status,
                reason, attempts, not_found_checks, next_attempt_at,
                selected_provider, selected_candidate_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, 0, 0, ?, NULL, NULL, ?, ?)
            """,
            (job_id, path, display, key, variant, origin, now, now, now),
        )
        return job_id

    def _reset(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        display: str,
        key: str,
        variant: str | None,
        origin: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            UPDATE subtitle_jobs
            SET code = ?, code_key = ?, variant = ?, origin = ?, status = 'queued',
                reason = NULL, attempts = 0, not_found_checks = 0, next_attempt_at = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (display, key, variant, origin, now, now, job_id),
        )

    def _finish(
        self,
        job_id: object,
        *,
        status: str,
        reason: str,
        next_attempt_at: float,
        keep_attempts: bool = False,
    ) -> dict[str, object]:
        clean_id = _clean_job_id(job_id)
        clean_reason = _clean_reason(reason)
        now = self.now()
        attempts_sql = "attempts" if keep_attempts else "0"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_status(connection, clean_id, ("running",))
            connection.execute(
                f"UPDATE subtitle_jobs SET status = ?, reason = ?, attempts = {attempts_sql}, "
                "next_attempt_at = ?, updated_at = ? WHERE job_id = ?",
                (status, clean_reason, next_attempt_at, now, clean_id),
            )
            connection.commit()
        return self.get(clean_id)

    def _row(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM subtitle_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            connection.rollback()
            raise SubtitleJobNotFound("subtitle job was not found")
        return row

    def _require_status(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        allowed: tuple[str, ...],
    ) -> sqlite3.Row:
        row = self._row(connection, job_id)
        if str(row["status"]) not in allowed:
            connection.rollback()
            raise SubtitleStoreConflict("subtitle job state changed")
        return row

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                migrate_sqlite(
                    connection,
                    component=SCHEMA_COMPONENT,
                    current_version=CURRENT_SCHEMA_VERSION,
                    migrations=(SQLiteMigration(1, _migrate_v1, _verify_schema),),
                    clock=self._clock,
                    verify_current=_verify_schema,
                )
            except SchemaTooNewError as exc:
                raise SubtitleStoreError(
                    "subtitle database schema is newer than this application supports"
                ) from exc
            except MigrationError as exc:
                raise SubtitleStoreError(str(exc)) from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()


def moved_subtitle_path(owned: str, old_media: str, new_media: str) -> str | None:
    """Where an owned subtitle ends up when its media file is renamed."""

    owned_path = PurePosixPath(owned)
    old_path = PurePosixPath(old_media)
    new_path = PurePosixPath(new_media)
    if owned_path.parent != old_path.parent or not is_subtitle_sidecar(owned_path.name, old_path.stem):
        return None
    return (new_path.parent / renamed_sidecar(owned_path.name, old_path.stem, new_path.stem)).as_posix()


def _migrate_v1(connection: sqlite3.Connection) -> None:
    def slots(values: Sequence[str]) -> str:
        return ", ".join(f"'{value}'" for value in values)

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS subtitle_jobs (
            job_id TEXT PRIMARY KEY,
            relative_media_path TEXT NOT NULL UNIQUE,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            variant TEXT CHECK (variant IS NULL OR variant IN ({slots(WEB_DOWNLOAD_VARIANTS)})),
            origin TEXT NOT NULL CHECK (origin IN ({slots(ORIGINS)})),
            status TEXT NOT NULL CHECK (status IN ({slots(STATUSES)})),
            reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            not_found_checks INTEGER NOT NULL DEFAULT 0 CHECK (not_found_checks >= 0),
            next_attempt_at REAL NOT NULL CHECK (next_attempt_at >= 0),
            selected_provider TEXT CHECK (
                selected_provider IS NULL OR selected_provider IN ({slots(PROVIDER_IDS)})
            ),
            selected_candidate_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS subtitle_jobs_ready "
        "ON subtitle_jobs(status, next_attempt_at, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS subtitle_jobs_updated "
        "ON subtitle_jobs(updated_at DESC, job_id DESC)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS subtitle_jobs_code ON subtitle_jobs(code_key)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS subtitle_candidates (
            job_id TEXT PRIMARY KEY REFERENCES subtitle_jobs(job_id) ON DELETE CASCADE,
            searched_at REAL NOT NULL,
            candidates_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS subtitle_files (
            job_id TEXT PRIMARY KEY REFERENCES subtitle_jobs(job_id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            size INTEGER NOT NULL CHECK (size > 0),
            mtime_ns INTEGER NOT NULL,
            script TEXT NOT NULL CHECK (script IN ({slots(SCRIPTS)})),
            provider TEXT NOT NULL CHECK (provider IN ({slots(PROVIDER_IDS)})),
            media_size INTEGER NOT NULL,
            media_mtime_ns INTEGER NOT NULL,
            written_at REAL NOT NULL
        )
        """
    )


def _verify_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "subtitle_jobs",
        (
            "job_id", "relative_media_path", "code", "code_key", "variant", "origin",
            "status", "reason", "attempts", "not_found_checks", "next_attempt_at",
            "selected_provider", "selected_candidate_id", "created_at", "updated_at",
        ),
    )
    require_columns(connection, "subtitle_candidates", ("job_id", "searched_at", "candidates_json"))
    require_columns(
        connection,
        "subtitle_files",
        (
            "job_id", "relative_path", "sha256", "size", "mtime_ns", "script",
            "provider", "media_size", "media_mtime_ns", "written_at",
        ),
    )


def _row_to_job(row: sqlite3.Row) -> dict[str, object]:
    return {
        "job_id": str(row["job_id"]),
        "relative_media_path": str(row["relative_media_path"]),
        "code": str(row["code"]),
        "code_key": str(row["code_key"]),
        "variant": row["variant"],
        "origin": str(row["origin"]),
        "status": str(row["status"]),
        "reason": row["reason"],
        "attempts": int(row["attempts"]),
        "next_attempt_at": float(row["next_attempt_at"]),
        "selected_provider": row["selected_provider"],
        "selected_candidate_id": row["selected_candidate_id"],
        "subtitle_path": row["subtitle_path"],
        "subtitle_script": row["subtitle_script"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _filters(status_filter: str, query: object | None) -> tuple[str, tuple[object, ...]]:
    statuses = STATUS_FILTERS.get(str(status_filter or "all"))
    if statuses is None:
        raise SubtitleStoreError("subtitle status filter is invalid")
    clauses: list[str] = []
    values: list[object] = []
    if statuses:
        clauses.append(f"j.status IN ({', '.join('?' for _ in statuses)})")
        values.extend(statuses)
    key = "".join(character for character in str(query or "").upper() if character.isalnum())[:40]
    if key:
        clauses.append("j.code_key LIKE ?")
        values.append(f"%{key}%")
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), tuple(values)


def _validate_file_record(record: SubtitleFileRecord) -> None:
    _clean_path(record.relative_path)
    if (
        not _SHA256_RE.fullmatch(record.sha256)
        or record.size <= 0
        or record.script not in SCRIPTS
        or record.provider not in PROVIDER_IDS
    ):
        raise SubtitleStoreError("subtitle file record is invalid")


def _clean_path(value: object) -> str:
    try:
        return safe_relative_path(value, allow_root=False)
    except MediaLibraryError as exc:
        raise SubtitleStoreError("subtitle media path is invalid") from exc


def _clean_code(value: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(value, max_length=40)
    if normalized is None:
        raise SubtitleStoreError("subtitle catalog code is invalid")
    return normalized


def _clean_variant(value: object | None) -> str | None:
    if value is None:
        return None
    if value not in WEB_DOWNLOAD_VARIANTS:
        raise SubtitleStoreError("subtitle media variant is invalid")
    return str(value)


def _clean_job_id(value: object) -> str:
    clean = str(value or "").strip()
    if not _JOB_ID_RE.fullmatch(clean):
        raise SubtitleJobNotFound("subtitle job id is invalid")
    return clean


def _clean_reason(value: object) -> str:
    clean = str(value or "")
    if not _REASON_RE.fullmatch(clean):
        raise SubtitleStoreError("subtitle reason is invalid")
    return clean


def _timestamp(value: object) -> float:
    if isinstance(value, bool):
        raise SubtitleStoreError("subtitle timestamp is invalid")
    try:
        clean = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise SubtitleStoreError("subtitle timestamp is invalid") from exc
    if not math.isfinite(clean) or clean < 0:
        raise SubtitleStoreError("subtitle timestamp is invalid")
    return clean


def _bounded(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SubtitleStoreError("subtitle pagination is invalid")
    return value
