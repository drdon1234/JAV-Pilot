from __future__ import annotations

import copy
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class MigrationError(RuntimeError):
    pass


class SchemaTooNewError(MigrationError):
    pass


@dataclass(frozen=True)
class SQLiteMigration:
    version: int
    apply: Callable[[sqlite3.Connection], None]
    verify: Callable[[sqlite3.Connection], None] | None = None


@dataclass(frozen=True)
class JSONMigration:
    version: int
    apply: Callable[[dict[str, Any]], dict[str, Any]]
    verify: Callable[[dict[str, Any]], None] | None = None


def migrate_json(
    payload: dict[str, Any],
    *,
    component: str,
    current_version: int,
    migrations: Sequence[JSONMigration],
    allow_unversioned_legacy: bool = True,
    fault_injector: Callable[[str], None] | None = None,
    verify_current: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Apply a complete JSON migration chain without mutating the input payload."""

    if not isinstance(payload, dict):
        raise MigrationError("JSON config must be an object")
    clean_component = _validate_component(component)
    ordered = _validate_json_migrations(current_version, migrations)
    version = _json_schema_version(
        payload,
        current_version=current_version,
        allow_unversioned_legacy=allow_unversioned_legacy,
        component=clean_component,
    )
    migrated = version != current_version
    working = copy.deepcopy(payload)

    for migration in ordered:
        if migration.version <= version:
            continue
        candidate = migration.apply(copy.deepcopy(working))
        if not isinstance(candidate, dict):
            raise MigrationError("JSON migration must return an object")
        candidate["schema_version"] = migration.version
        if migration.verify is not None:
            migration.verify(candidate)
        if fault_injector is not None:
            fault_injector(
                f"after_json_migration_{clean_component}_{migration.version}"
            )
        working = candidate
        version = migration.version

    if verify_current is not None:
        verify_current(working)
    return working, migrated


def migrate_sqlite(
    connection: sqlite3.Connection,
    *,
    component: str,
    current_version: int,
    migrations: Sequence[SQLiteMigration],
    clock: Callable[[], float] = time.time,
    fault_injector: Callable[[str], None] | None = None,
    verify_current: Callable[[sqlite3.Connection], None] | None = None,
) -> int:
    """Apply every pending migration and its ledger row in one transaction."""

    clean_component = _validate_component(component)
    ordered = _validate_migrations(current_version, migrations)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                component TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                applied_at REAL NOT NULL,
                PRIMARY KEY (component, version)
            )
            """
        )
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations WHERE component = ?",
            (clean_component,),
        ).fetchone()
        version = int(row[0]) if row is not None and row[0] is not None else 0
        if version > current_version:
            raise SchemaTooNewError(
                f"{clean_component} database schema is newer than this application supports"
            )

        for migration in ordered:
            if migration.version <= version:
                continue
            migration.apply(connection)
            if fault_injector is not None:
                fault_injector(
                    f"after_sqlite_migration_{clean_component}_{migration.version}_apply"
                )
            if migration.verify is not None:
                migration.verify(connection)
            if fault_injector is not None:
                fault_injector(
                    f"after_sqlite_migration_{clean_component}_{migration.version}_verify"
                )
            applied_at = float(clock())
            if applied_at < 0:
                raise MigrationError("migration clock returned an invalid timestamp")
            connection.execute(
                "INSERT INTO schema_migrations (component, version, applied_at) "
                "VALUES (?, ?, ?)",
                (clean_component, migration.version, applied_at),
            )
            if fault_injector is not None:
                fault_injector(
                    f"after_sqlite_migration_{clean_component}_{migration.version}_ledger"
                )
            version = migration.version

        if verify_current is not None:
            verify_current(connection)
        connection.commit()
        return version
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if not _COMPONENT_RE.fullmatch(table):
        raise MigrationError("migration table name is invalid")
    return frozenset(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def require_columns(
    connection: sqlite3.Connection,
    table: str,
    expected: Sequence[str],
) -> None:
    columns = table_columns(connection, table)
    missing = sorted(set(expected) - columns)
    if missing:
        raise MigrationError(
            f"{table} schema is incomplete: missing {', '.join(missing)}"
        )


def add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if not _COMPONENT_RE.fullmatch(column):
        raise MigrationError("migration column name is invalid")
    if column not in table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _validate_component(component: object) -> str:
    clean = str(component or "").strip()
    if not _COMPONENT_RE.fullmatch(clean):
        raise MigrationError("migration component is invalid")
    return clean


def _validate_migrations(
    current_version: int,
    migrations: Sequence[SQLiteMigration],
) -> tuple[SQLiteMigration, ...]:
    if isinstance(current_version, bool) or current_version < 1:
        raise MigrationError("current schema version is invalid")
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    versions = tuple(item.version for item in ordered)
    expected = tuple(range(1, current_version + 1))
    if versions != expected:
        raise MigrationError("migrations must define every version from 1 to current")
    return ordered


def _validate_json_migrations(
    current_version: int,
    migrations: Sequence[JSONMigration],
) -> tuple[JSONMigration, ...]:
    if isinstance(current_version, bool) or current_version < 1:
        raise MigrationError("current schema version is invalid")
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    versions = tuple(item.version for item in ordered)
    expected = tuple(range(1, current_version + 1))
    if versions != expected:
        raise MigrationError("migrations must define every version from 1 to current")
    return ordered


def _json_schema_version(
    payload: dict[str, Any],
    *,
    current_version: int,
    allow_unversioned_legacy: bool,
    component: str,
) -> int:
    raw = payload.get("schema_version")
    if raw is None and allow_unversioned_legacy:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise MigrationError(f"{component} schema version is invalid")
    if raw > current_version:
        raise SchemaTooNewError(
            f"{component} schema is newer than this application supports"
        )
    return raw
