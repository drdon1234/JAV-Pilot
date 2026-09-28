"""SQLite schema migrations and verification for web download jobs."""

from __future__ import annotations

import hashlib
import sqlite3

from ..core.migrations import MigrationError, add_column_if_missing, require_columns
from .control import (
    WebDownloadControlError,
    normalize_bandwidth_limit,
    normalize_schedule,
    normalize_target_concurrency,
    normalize_timezone,
)
from .errors import WebDownloadError
from .jobs import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    LONG_RETRY_DELAYS_SECONDS,
    MAX_CONCURRENCY,
    MAX_QUEUE_PRIORITY,
    RETRY_WAIT_STATUS,
    normalize_web_download_code,
    sql_slots,
)

def migrate_web_download_v1(connection: sqlite3.Connection) -> None:
    statuses = ", ".join(f"'{status}'" for status in ALL_STATUSES)
    active_statuses = ", ".join(f"'{status}'" for status in ACTIVE_STATUSES)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS web_download_jobs (
            job_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            idempotency_key TEXT,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
            downloaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (downloaded_bytes >= 0),
            total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 0),
            speed REAL NOT NULL DEFAULT 0 CHECK (speed >= 0),
            eta INTEGER CHECK (eta IS NULL OR eta >= 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            error TEXT,
            output_path TEXT
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS web_download_jobs_idempotency "
        "ON web_download_jobs(provider, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS web_download_jobs_active_code "
        "ON web_download_jobs(provider, code_key) "
        f"WHERE status IN ({active_statuses})"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS web_download_jobs_created_at "
        "ON web_download_jobs(created_at DESC)"
    )


def migrate_web_download_v2(connection: sqlite3.Connection) -> None:
    columns = (
        ("requested_height", "INTEGER"),
        ("selected_height", "INTEGER"),
        (
            "quality_strategy",
            "TEXT NOT NULL DEFAULT 'legacy' CHECK "
            "(quality_strategy IN ('legacy', 'selected', 'highest'))",
        ),
        ("verified_height", "INTEGER"),
        (
            "existing_policy",
            "TEXT NOT NULL DEFAULT 'keep_both' CHECK "
            "(existing_policy IN "
            "('keep_both', 'higher_quality', 'overwrite', 'skip'))",
        ),
        ("incumbent_output_path", "TEXT"),
        ("replaces_job_id", "TEXT"),
        ("superseded_by_job_id", "TEXT"),
        ("publication_outcome", "TEXT"),
    )
    for column, definition in columns:
        add_column_if_missing(
            connection,
            "web_download_jobs",
            column,
            definition,
        )
    connection.execute(
        "UPDATE web_download_jobs SET quality_strategy = 'selected' "
        "WHERE requested_height IS NOT NULL AND quality_strategy = 'legacy'"
    )


def migrate_web_download_v3(connection: sqlite3.Connection) -> None:
    statuses = ", ".join(f"'{status}'" for status in ALL_STATUSES)
    active_statuses = ", ".join(f"'{status}'" for status in ACTIVE_STATUSES)
    connection.execute(
        f"""
        CREATE TABLE web_download_jobs_v3 (
            job_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            idempotency_key TEXT,
            requested_height INTEGER CHECK (
                requested_height IS NULL OR
                (requested_height >= 144 AND requested_height <= 4320)
            ),
            selected_height INTEGER CHECK (
                selected_height IS NULL OR
                (selected_height >= 144 AND selected_height <= 4320)
            ),
            verified_height INTEGER CHECK (
                verified_height IS NULL OR
                (verified_height >= 144 AND verified_height <= 4320)
            ),
            quality_strategy TEXT NOT NULL DEFAULT 'legacy' CHECK (
                quality_strategy IN ('legacy', 'selected', 'highest')
            ),
            existing_policy TEXT NOT NULL DEFAULT 'keep_both' CHECK (
                existing_policy IN (
                    'keep_both', 'higher_quality', 'overwrite', 'skip'
                )
            ),
            incumbent_output_path TEXT,
            replaces_job_id TEXT,
            superseded_by_job_id TEXT,
            publication_outcome TEXT,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
            downloaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (downloaded_bytes >= 0),
            total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 0),
            speed REAL NOT NULL DEFAULT 0 CHECK (speed >= 0),
            eta INTEGER CHECK (eta IS NULL OR eta >= 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            error TEXT,
            output_path TEXT,
            priority INTEGER NOT NULL DEFAULT 0 CHECK (
                priority >= -{MAX_QUEUE_PRIORITY} AND priority <= {MAX_QUEUE_PRIORITY}
            ),
            queue_position INTEGER NOT NULL DEFAULT 0 CHECK (queue_position >= 0),
            pause_origin TEXT CHECK (pause_origin IN ('manual', 'global'))
        )
        """
    )
    connection.execute(
        """
        INSERT INTO web_download_jobs_v3 (
            job_id, provider, code, code_key, idempotency_key,
            requested_height, selected_height, verified_height, quality_strategy,
            existing_policy, incumbent_output_path, replaces_job_id,
            superseded_by_job_id, publication_outcome, status, progress,
            downloaded_bytes, total_bytes, speed, eta, created_at, updated_at,
            error, output_path, priority, queue_position, pause_origin
        )
        SELECT
            job_id, provider, code, code_key, idempotency_key,
            requested_height, selected_height, verified_height, quality_strategy,
            existing_policy, incumbent_output_path, replaces_job_id,
            superseded_by_job_id, publication_outcome, status, progress,
            downloaded_bytes, total_bytes, speed, eta, created_at, updated_at,
            error, output_path, 0,
            ROW_NUMBER() OVER (ORDER BY created_at, job_id), NULL
        FROM web_download_jobs
        """
    )
    connection.execute("DROP TABLE web_download_jobs")
    connection.execute("ALTER TABLE web_download_jobs_v3 RENAME TO web_download_jobs")
    connection.execute(
        "CREATE UNIQUE INDEX web_download_jobs_idempotency "
        "ON web_download_jobs(provider, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX web_download_jobs_active_code "
        "ON web_download_jobs(provider, code_key) "
        f"WHERE status IN ({active_statuses})"
    )
    connection.execute(
        "CREATE INDEX web_download_jobs_created_at "
        "ON web_download_jobs(created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX web_download_jobs_queue_order "
        "ON web_download_jobs(status, priority DESC, queue_position, created_at, job_id)"
    )
    connection.execute(
        """
        CREATE TABLE web_download_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            queue_revision INTEGER NOT NULL DEFAULT 0 CHECK (queue_revision >= 0),
            global_paused INTEGER NOT NULL DEFAULT 0 CHECK (global_paused IN (0, 1)),
            target_concurrency INTEGER NOT NULL DEFAULT 8 CHECK (
                target_concurrency >= 1 AND target_concurrency <= 8
            ),
            bandwidth_limit INTEGER NOT NULL DEFAULT 0 CHECK (bandwidth_limit >= 0),
            timezone TEXT NOT NULL DEFAULT 'UTC',
            schedule_json TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO web_download_control (id, updated_at) VALUES (1, 0)"
    )
    for trigger_sql in (
        """
        CREATE TRIGGER web_download_jobs_queue_insert
        AFTER INSERT ON web_download_jobs
        WHEN NEW.status = 'queued'
        BEGIN
            UPDATE web_download_jobs
            SET queue_position = (
                SELECT COALESCE(MAX(queue_position), 0) + 1
                FROM web_download_jobs
                WHERE job_id != NEW.job_id
            )
            WHERE job_id = NEW.job_id AND NEW.queue_position = 0;
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, NEW.updated_at)
            WHERE id = 1;
        END
        """,
        """
        CREATE TRIGGER web_download_jobs_queue_delete
        AFTER DELETE ON web_download_jobs
        WHEN OLD.status = 'queued'
        BEGIN
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, OLD.updated_at)
            WHERE id = 1;
        END
        """,
        """
        CREATE TRIGGER web_download_jobs_queue_update
        AFTER UPDATE OF status, priority, queue_position ON web_download_jobs
        WHEN (OLD.status = 'queued' OR NEW.status = 'queued') AND (
            OLD.status != NEW.status OR
            OLD.priority != NEW.priority OR
            OLD.queue_position != NEW.queue_position
        )
        BEGIN
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, NEW.updated_at)
            WHERE id = 1;
        END
        """,
    ):
        connection.execute(trigger_sql)


def migrate_web_download_v4(connection: sqlite3.Connection) -> None:
    statuses = ", ".join(f"'{status}'" for status in ALL_STATUSES)
    active_statuses = ", ".join(f"'{status}'" for status in ACTIVE_STATUSES)
    connection.execute(
        f"""
        CREATE TABLE web_download_jobs_v4 (
            job_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            code TEXT NOT NULL,
            code_key TEXT NOT NULL,
            idempotency_key TEXT,
            requested_height INTEGER CHECK (
                requested_height IS NULL OR
                (requested_height >= 144 AND requested_height <= 4320)
            ),
            selected_height INTEGER CHECK (
                selected_height IS NULL OR
                (selected_height >= 144 AND selected_height <= 4320)
            ),
            verified_height INTEGER CHECK (
                verified_height IS NULL OR
                (verified_height >= 144 AND verified_height <= 4320)
            ),
            quality_strategy TEXT NOT NULL DEFAULT 'legacy' CHECK (
                quality_strategy IN ('legacy', 'selected', 'highest')
            ),
            existing_policy TEXT NOT NULL DEFAULT 'keep_both' CHECK (
                existing_policy IN (
                    'keep_both', 'higher_quality', 'overwrite', 'skip'
                )
            ),
            incumbent_output_path TEXT,
            replaces_job_id TEXT,
            superseded_by_job_id TEXT,
            publication_outcome TEXT,
            status TEXT NOT NULL CHECK (status IN ({statuses})),
            progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
            downloaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (downloaded_bytes >= 0),
            total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 0),
            speed REAL NOT NULL DEFAULT 0 CHECK (speed >= 0),
            eta INTEGER CHECK (eta IS NULL OR eta >= 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            error TEXT,
            output_path TEXT,
            priority INTEGER NOT NULL DEFAULT 0 CHECK (
                priority >= -{MAX_QUEUE_PRIORITY} AND priority <= {MAX_QUEUE_PRIORITY}
            ),
            queue_position INTEGER NOT NULL DEFAULT 0 CHECK (queue_position >= 0),
            pause_origin TEXT CHECK (pause_origin IN ('manual', 'global')),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (
                retry_count >= 0 AND retry_count <= {len(LONG_RETRY_DELAYS_SECONDS)}
            ),
            next_retry_at REAL CHECK (next_retry_at IS NULL OR next_retry_at >= 0),
            checkpoint_bytes INTEGER NOT NULL DEFAULT 0 CHECK (checkpoint_bytes >= 0),
            checkpoint_fragments INTEGER NOT NULL DEFAULT 0 CHECK (
                checkpoint_fragments >= 0
            ),
            retry_progress_bytes INTEGER CHECK (
                retry_progress_bytes IS NULL OR retry_progress_bytes >= 0
            ),
            retry_progress_fragments INTEGER CHECK (
                retry_progress_fragments IS NULL OR retry_progress_fragments >= 0
            ),
            CHECK (
                status != '{RETRY_WAIT_STATUS}' OR
                (retry_count > 0 AND next_retry_at IS NOT NULL)
            ),
            CHECK (
                next_retry_at IS NULL OR
                status IN ('{RETRY_WAIT_STATUS}', 'paused')
            ),
            CHECK (
                (retry_progress_bytes IS NULL) =
                (retry_progress_fragments IS NULL)
            )
        )
        """
    )
    connection.execute(
        """
        INSERT INTO web_download_jobs_v4 (
            job_id, provider, code, code_key, idempotency_key,
            requested_height, selected_height, verified_height, quality_strategy,
            existing_policy, incumbent_output_path, replaces_job_id,
            superseded_by_job_id, publication_outcome, status, progress,
            downloaded_bytes, total_bytes, speed, eta, created_at, updated_at,
            error, output_path, priority, queue_position, pause_origin,
            retry_count, next_retry_at, checkpoint_bytes, checkpoint_fragments,
            retry_progress_bytes, retry_progress_fragments
        )
        SELECT
            job_id, provider, code, code_key, idempotency_key,
            requested_height, selected_height, verified_height, quality_strategy,
            existing_policy, incumbent_output_path, replaces_job_id,
            superseded_by_job_id, publication_outcome, status, progress,
            downloaded_bytes, total_bytes, speed, eta, created_at, updated_at,
            error, output_path, priority, queue_position, pause_origin,
            0, NULL, 0, 0, NULL, NULL
        FROM web_download_jobs
        """
    )
    connection.execute("DROP TABLE web_download_jobs")
    connection.execute("ALTER TABLE web_download_jobs_v4 RENAME TO web_download_jobs")
    connection.execute(
        "CREATE UNIQUE INDEX web_download_jobs_idempotency "
        "ON web_download_jobs(provider, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX web_download_jobs_active_code "
        "ON web_download_jobs(provider, code_key) "
        f"WHERE status IN ({active_statuses})"
    )
    connection.execute(
        "CREATE INDEX web_download_jobs_created_at "
        "ON web_download_jobs(created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX web_download_jobs_queue_order "
        "ON web_download_jobs(status, priority DESC, queue_position, created_at, job_id)"
    )
    for trigger_sql in (
        """
        CREATE TRIGGER web_download_jobs_queue_insert
        AFTER INSERT ON web_download_jobs
        WHEN NEW.status = 'queued'
        BEGIN
            UPDATE web_download_jobs
            SET queue_position = (
                SELECT COALESCE(MAX(queue_position), 0) + 1
                FROM web_download_jobs
                WHERE job_id != NEW.job_id
            )
            WHERE job_id = NEW.job_id AND NEW.queue_position = 0;
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, NEW.updated_at)
            WHERE id = 1;
        END
        """,
        """
        CREATE TRIGGER web_download_jobs_queue_delete
        AFTER DELETE ON web_download_jobs
        WHEN OLD.status = 'queued'
        BEGIN
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, OLD.updated_at)
            WHERE id = 1;
        END
        """,
        """
        CREATE TRIGGER web_download_jobs_queue_update
        AFTER UPDATE OF status, priority, queue_position ON web_download_jobs
        WHEN (OLD.status = 'queued' OR NEW.status = 'queued') AND (
            OLD.status != NEW.status OR
            OLD.priority != NEW.priority OR
            OLD.queue_position != NEW.queue_position
        )
        BEGIN
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, NEW.updated_at)
            WHERE id = 1;
        END
        """,
    ):
        connection.execute(trigger_sql)


def migrate_web_download_v5(connection: sqlite3.Connection) -> None:
    for trigger_name in (
        "web_download_jobs_queue_insert",
        "web_download_jobs_queue_delete",
        "web_download_jobs_queue_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    normalize_pending_queue_positions(connection)
    connection.execute(
        "UPDATE web_download_control SET queue_revision = queue_revision + 1 WHERE id = 1"
    )
    for trigger_sql in (
        """
        CREATE TRIGGER web_download_jobs_queue_insert
        AFTER INSERT ON web_download_jobs
        WHEN NEW.status IN ('queued', 'retry_wait')
        BEGIN
            UPDATE web_download_jobs
            SET queue_position = (
                SELECT COALESCE(MAX(queue_position), 0) + 1
                FROM web_download_jobs
                WHERE job_id != NEW.job_id
                  AND status IN ('queued', 'retry_wait')
            )
            WHERE job_id = NEW.job_id AND NEW.queue_position = 0;
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, NEW.updated_at)
            WHERE id = 1;
        END
        """,
        """
        CREATE TRIGGER web_download_jobs_queue_delete
        AFTER DELETE ON web_download_jobs
        WHEN OLD.status IN ('queued', 'retry_wait')
        BEGIN
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, OLD.updated_at)
            WHERE id = 1;
        END
        """,
        """
        CREATE TRIGGER web_download_jobs_queue_update
        AFTER UPDATE OF status, priority, queue_position ON web_download_jobs
        WHEN (
            OLD.status IN ('queued', 'retry_wait') OR
            NEW.status IN ('queued', 'retry_wait')
        ) AND (
            OLD.status != NEW.status OR
            OLD.priority != NEW.priority OR
            OLD.queue_position != NEW.queue_position
        )
        BEGIN
            UPDATE web_download_control
            SET queue_revision = queue_revision + 1,
                updated_at = MAX(updated_at, NEW.updated_at)
            WHERE id = 1;
        END
        """,
    ):
        connection.execute(trigger_sql)


def migrate_web_download_v6(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "web_download_jobs",
        "variant",
        "TEXT NOT NULL DEFAULT 'original' CHECK (variant IN ("
        "'original', 'chinese_subtitle', 'uncensored_leak'))",
    )
    connection.execute("DROP INDEX IF EXISTS web_download_jobs_active_code")
    active_statuses = ", ".join(f"'{status}'" for status in ACTIVE_STATUSES)
    connection.execute(
        "CREATE UNIQUE INDEX web_download_jobs_active_code "
        "ON web_download_jobs(provider, code_key, variant) "
        f"WHERE status IN ({active_statuses})"
    )


def migrate_web_download_v7(connection: sqlite3.Connection) -> None:
    """Persist only bounded failure classification, never transport details."""
    add_column_if_missing(
        connection,
        "web_download_jobs",
        "failure_stage",
        "TEXT CHECK (failure_stage IS NULL OR length(failure_stage) <= 32)",
    )
    add_column_if_missing(
        connection,
        "web_download_jobs",
        "failure_code",
        "TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 64)",
    )


def migrate_web_download_v8(connection: sqlite3.Connection) -> None:
    add_column_if_missing(
        connection,
        "web_download_jobs",
        "resolved_provider",
        "TEXT CHECK (resolved_provider IS NULL OR resolved_provider IN ('missav', 'jable', 'supjav'))",
    )


def migrate_web_download_v9(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT job_id, provider, code, code_key, variant, status "
        "FROM web_download_jobs ORDER BY job_id"
    ).fetchall()
    active_identities: dict[tuple[str, str, str], str] = {}
    updates: list[tuple[str, str, str]] = []
    for row in rows:
        try:
            display_code, code_key = normalize_web_download_code(row["code"])
        except WebDownloadError as exc:
            raise MigrationError(
                "web download job catalog code is invalid"
            ) from exc
        job_id = str(row["job_id"])
        if str(row["status"]) in ACTIVE_STATUSES:
            identity = (str(row["provider"]), code_key, str(row["variant"]))
            collided = active_identities.setdefault(identity, job_id)
            if collided != job_id:
                raise MigrationError(
                    "web download jobs collide after catalog code normalization"
                )
        if str(row["code"]) != display_code or str(row["code_key"]) != code_key:
            updates.append((job_id, display_code, code_key))

    reserved_keys = {
        str(row["code_key"]) for row in rows
    } | {code_key for _job_id, _display_code, code_key in updates}
    temporary_keys: dict[str, str] = {}
    for job_id, _display_code, _code_key in updates:
        for salt in range(1024):
            temporary_key = "MIGRATION" + hashlib.sha256(
                f"{job_id}\0{salt}".encode("ascii")
            ).hexdigest().upper()
            if temporary_key not in reserved_keys:
                reserved_keys.add(temporary_key)
                temporary_keys[job_id] = temporary_key
                break
        else:
            raise MigrationError("web download job identity migration collided")
    connection.executemany(
        "UPDATE web_download_jobs SET code_key = ? WHERE job_id = ?",
        ((temporary_keys[job_id], job_id) for job_id, *_rest in updates),
    )
    connection.executemany(
        "UPDATE web_download_jobs SET code = ?, code_key = ? WHERE job_id = ?",
        (
            (display_code, code_key, job_id)
            for job_id, display_code, code_key in updates
        ),
    )


def normalize_pending_queue_positions(
    connection: sqlite3.Connection, *, updated_at: float | None = None
) -> None:
    assignments = "queue_position = pending_queue.position"
    parameters: tuple[object, ...] = ()
    if updated_at is not None:
        assignments += ", updated_at = ?"
        parameters = (float(updated_at),)
    connection.execute(
        f"""
        WITH pending_queue AS (
            SELECT job_id,
                   ROW_NUMBER() OVER (
                       ORDER BY priority DESC, queue_position, created_at, job_id
                   ) AS position
            FROM web_download_jobs
            WHERE status IN ('queued', 'retry_wait')
        )
        UPDATE web_download_jobs
        SET {assignments}
        FROM pending_queue
        WHERE web_download_jobs.job_id = pending_queue.job_id
          AND web_download_jobs.queue_position != pending_queue.position
        """,
        parameters,
    )


def verify_web_download_schema(connection: sqlite3.Connection) -> None:
    require_columns(
        connection,
        "web_download_jobs",
        (
            "job_id",
            "provider",
            "code",
            "code_key",
            "variant",
            "idempotency_key",
            "requested_height",
            "selected_height",
            "verified_height",
            "quality_strategy",
            "existing_policy",
            "incumbent_output_path",
            "replaces_job_id",
            "superseded_by_job_id",
            "publication_outcome",
            "status",
            "progress",
            "downloaded_bytes",
            "total_bytes",
            "speed",
            "eta",
            "created_at",
            "updated_at",
            "error",
            "failure_stage",
            "failure_code",
            "resolved_provider",
            "output_path",
            "priority",
            "queue_position",
            "pause_origin",
            "retry_count",
            "next_retry_at",
            "checkpoint_bytes",
            "checkpoint_fragments",
            "retry_progress_bytes",
            "retry_progress_fragments",
        ),
    )
    require_columns(
        connection,
        "web_download_control",
        (
            "id",
            "queue_revision",
            "global_paused",
            "target_concurrency",
            "bandwidth_limit",
            "timezone",
            "schedule_json",
            "updated_at",
        ),
    )
    control_rows = connection.execute(
        "SELECT * FROM web_download_control ORDER BY id"
    ).fetchall()
    if len(control_rows) != 1 or int(control_rows[0]["id"]) != 1:
        raise MigrationError("web download control singleton is invalid")
    control = control_rows[0]
    try:
        normalize_target_concurrency(
            control["target_concurrency"], hard_limit=MAX_CONCURRENCY
        )
        normalize_bandwidth_limit(control["bandwidth_limit"])
        normalize_timezone(control["timezone"])
        normalize_schedule(control["schedule_json"])
    except WebDownloadControlError as exc:
        raise MigrationError("web download control values are invalid") from exc
    required_objects = {
        "web_download_jobs_idempotency": "index",
        "web_download_jobs_active_code": "index",
        "web_download_jobs_created_at": "index",
        "web_download_jobs_queue_order": "index",
        "web_download_jobs_queue_insert": "trigger",
        "web_download_jobs_queue_delete": "trigger",
        "web_download_jobs_queue_update": "trigger",
    }
    rows = connection.execute(
        "SELECT name, type FROM sqlite_master WHERE name IN "
        f"({sql_slots(tuple(required_objects))})",
        tuple(required_objects),
    ).fetchall()
    actual_objects = {str(row["name"]): str(row["type"]) for row in rows}
    if actual_objects != required_objects:
        raise MigrationError("web download queue schema objects are incomplete")
    active_identity_columns = tuple(
        str(row["name"])
        for row in connection.execute(
            "PRAGMA index_info(web_download_jobs_active_code)"
        ).fetchall()
    )
    if active_identity_columns != ("provider", "code_key", "variant"):
        raise MigrationError("web download active identity index is invalid")
    for row in connection.execute(
        "SELECT code, code_key FROM web_download_jobs"
    ).fetchall():
        try:
            expected_code, expected_key = normalize_web_download_code(row["code"])
        except WebDownloadError as exc:
            raise MigrationError(
                "web download job catalog code is invalid"
            ) from exc
        if (
            str(row["code"]) != expected_code
            or str(row["code_key"]) != expected_key
        ):
            raise MigrationError("web download job catalog identity is invalid")
