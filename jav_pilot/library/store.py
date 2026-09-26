"""SQLite store for media library roots, entries, assets and history facts."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..core.catalog_code import canonical_catalog_code
from .media_probe import LocalFileIdentity
from .errors import (
    MediaLibraryConflictError,
    MediaLibraryError,
    MediaLibraryRootChangedError,
)
from .fields import (
    bounded_int,
    escape_like,
    normalize_error_code,
    optional_enum,
    optional_height,
    optional_query,
    optional_term,
    stored_timestamp,
    term_key,
    validate_entry_id,
    validate_generation_id,
    validate_root_key,
)
from .filesystem import (
    exact_catalog_query,
    identity_scalar,
    is_below,
    optional_web_download_variant,
    safe_relative_path,
)
from .models import PRESENCE_STATES, DirectoryIdentity, FileRecord
from .nfo import nfo_from_json
from .schema import initialize_media_library_schema

__all__ = [
    "MediaLibraryStore",
]


class MediaLibraryStore:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(database_path)
        if not self.path.is_absolute():
            raise MediaLibraryError("media library database path must be absolute")
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._recover_interrupted_generations()

    def root(self, root_key: str) -> dict[str, object] | None:
        clean_key = validate_root_key(root_key)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_library_roots WHERE root_key = ?",
                (clean_key,),
            ).fetchone()
        return _root_row(row) if row is not None else None

    def register_root(
        self,
        root_key: str,
        *,
        device: str,
        inode: str,
        accept_identity_change: bool = False,
    ) -> dict[str, object]:
        clean_key = validate_root_key(root_key)
        clean_device = identity_scalar(device)
        clean_inode = identity_scalar(inode)
        now = stored_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM media_library_roots WHERE root_key = ?",
                (clean_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO media_library_roots (
                        root_key, device, inode, state, published_generation_id,
                        revision, last_success_at, last_full_scan_at, updated_at
                    ) VALUES (?, ?, ?, 'unknown', NULL, 0, NULL, NULL, ?)
                    """,
                    (clean_key, clean_device, clean_inode, now),
                )
            elif not str(row["device"]) and not str(row["inode"]):
                connection.execute(
                    "UPDATE media_library_roots SET device = ?, inode = ?, "
                    "updated_at = ? WHERE root_key = ?",
                    (clean_device, clean_inode, now, clean_key),
                )
            elif str(row["device"]) != clean_device or str(row["inode"]) != clean_inode:
                if not accept_identity_change:
                    connection.execute(
                        "UPDATE media_library_roots SET state = 'unknown', "
                        "revision = revision + 1, updated_at = ? WHERE root_key = ?",
                        (now, clean_key),
                    )
                    connection.commit()
                    raise MediaLibraryRootChangedError(
                        "media library root identity changed"
                    )
                connection.execute(
                    """
                    UPDATE media_library_roots
                    SET device = ?, inode = ?, state = 'unknown',
                        revision = revision + 1, last_full_scan_at = NULL,
                        updated_at = ?
                    WHERE root_key = ?
                    """,
                    (clean_device, clean_inode, now, clean_key),
                )
            connection.commit()
        current = self.root(clean_key)
        if current is None:
            raise MediaLibraryError("media library root could not be registered")
        return current

    def mark_unknown(self, root_key: str) -> None:
        clean_key = validate_root_key(root_key)
        now = stored_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO media_library_roots (
                    root_key, device, inode, state, published_generation_id,
                    revision, last_success_at, last_full_scan_at, updated_at
                ) VALUES (?, '', '', 'unknown', NULL, 0, NULL, NULL, ?)
                ON CONFLICT(root_key) DO UPDATE SET
                    state = 'unknown', revision = revision + 1, updated_at = excluded.updated_at
                """,
                (clean_key, now),
            )
            connection.commit()

    def mark_available_without_generation(self, root_key: str) -> None:
        clean_key = validate_root_key(root_key)
        now = stored_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute(
                "UPDATE media_library_roots SET state = 'available', "
                "revision = revision + CASE WHEN state = 'available' THEN 0 ELSE 1 END, "
                "last_success_at = ?, updated_at = ? WHERE root_key = ?",
                (now, now, clean_key),
            )

    def begin_generation(
        self,
        root_key: str,
        *,
        scan_kind: str,
        base_generation_id: str | None,
        root_device: str,
        root_inode: str,
        generation_id: str | None = None,
    ) -> str:
        clean_key = validate_root_key(root_key)
        if scan_kind not in {"full", "incremental"}:
            raise MediaLibraryError("media library scan kind is invalid")
        clean_base = (
            validate_generation_id(base_generation_id)
            if base_generation_id is not None
            else None
        )
        clean_id = validate_generation_id(generation_id or uuid.uuid4().hex)
        now = stored_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT 1 FROM media_library_generations "
                "WHERE root_key = ? AND status = 'building'",
                (clean_key,),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise MediaLibraryConflictError(
                    "media library reconciliation is already running"
                )
            root = connection.execute(
                "SELECT published_generation_id, state "
                "FROM media_library_roots WHERE root_key = ?",
                (clean_key,),
            ).fetchone()
            if root is None:
                connection.rollback()
                raise MediaLibraryError("media library root is unavailable")
            if scan_kind == "incremental" and (
                clean_base is None
                or root["published_generation_id"] != clean_base
                or str(root["state"]) != "available"
            ):
                connection.rollback()
                raise MediaLibraryError("media library base generation is unavailable")
            if scan_kind == "full" and clean_base is not None:
                connection.rollback()
                raise MediaLibraryError(
                    "full media library scan cannot have a base generation"
                )
            connection.execute(
                """
                INSERT INTO media_library_generations (
                    generation_id, root_key, base_generation_id, scan_kind,
                    status, root_device, root_inode, started_at, completed_at,
                    error_code
                ) VALUES (?, ?, ?, ?, 'building', ?, ?, ?, NULL, NULL)
                """,
                (
                    clean_id,
                    clean_key,
                    clean_base,
                    scan_kind,
                    identity_scalar(root_device),
                    identity_scalar(root_inode),
                    now,
                ),
            )
            connection.commit()
        return clean_id

    def fail_generation(self, generation_id: str, error_code: str) -> None:
        clean_id = validate_generation_id(generation_id)
        clean_error = normalize_error_code(error_code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE media_library_generations SET status = 'failed', "
                "completed_at = ?, error_code = ? "
                "WHERE generation_id = ? AND status = 'building'",
                (stored_timestamp(self._clock()), clean_error, clean_id),
            ).rowcount
            if changed:
                self._clear_workspace(connection, clean_id)
            connection.commit()

    def generation_directories(
        self, generation_id: str
    ) -> dict[str, DirectoryIdentity]:
        clean_id = validate_generation_id(generation_id)
        with self._connect() as connection:
            generation = self._generation_context(connection, clean_id)
            rows: dict[str, sqlite3.Row] = {}
            if self._uses_published_state(generation):
                rows.update(
                    {
                        str(row["relative_path"]): row
                        for row in connection.execute(
                            "SELECT relative_path, device, inode, modified_ns, "
                            "changed_ns FROM media_library_directories "
                            "WHERE root_key = ? AND retired_generation_id IS NULL",
                            (str(generation["root_key"]),),
                        ).fetchall()
                    }
                )
            if str(generation["status"]) == "building":
                for deleted in self._workspace_deletions(connection, clean_id):
                    rows = {
                        path: row
                        for path, row in rows.items()
                        if not is_below(path, deleted)
                    }
                rows.update(
                    {
                        str(row["relative_path"]): row
                        for row in connection.execute(
                            "SELECT relative_path, device, inode, modified_ns, "
                            "changed_ns FROM media_library_workspace_directories "
                            "WHERE generation_id = ?",
                            (clean_id,),
                        ).fetchall()
                    }
                )
        return {
            path: DirectoryIdentity(
                relative_path=path,
                device=str(row["device"]),
                inode=str(row["inode"]),
                modified_ns=int(row["modified_ns"]),
                changed_ns=int(row["changed_ns"]),
            )
            for path, row in rows.items()
        }

    def generation_media_directories(self, generation_id: str) -> tuple[str, ...]:
        clean_id = validate_generation_id(generation_id)
        with self._connect() as connection:
            generation = self._generation_context(connection, clean_id)
            parents: set[str] = set()
            if self._uses_published_state(generation):
                parents.update(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT parent_path FROM media_library_files "
                        "WHERE root_key = ? AND retired_generation_id IS NULL",
                        (str(generation["root_key"]),),
                    ).fetchall()
                )
            if str(generation["status"]) == "building":
                replaced = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT relative_path "
                        "FROM media_library_workspace_directories "
                        "WHERE generation_id = ?",
                        (clean_id,),
                    ).fetchall()
                }
                deleted = self._workspace_deletions(connection, clean_id)
                parents = {
                    parent
                    for parent in parents
                    if parent not in replaced
                    and not any(is_below(parent, ancestor) for ancestor in deleted)
                }
                parents.update(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT parent_path "
                        "FROM media_library_workspace_files "
                        "WHERE generation_id = ?",
                        (clean_id,),
                    ).fetchall()
                )
        return tuple(sorted(parents))

    def generation_directory_files(
        self,
        generation_id: str,
        relative_path: str,
    ) -> tuple[FileRecord, ...]:
        clean_id = validate_generation_id(generation_id)
        clean_path = safe_relative_path(relative_path, allow_root=True)
        with self._connect() as connection:
            generation = self._generation_context(connection, clean_id)
            rows = self._effective_file_rows(
                connection, generation, parent_path=clean_path
            )
            rows.sort(key=lambda row: str(row["relative_path"]))
        return tuple(_file_record_row(row) for row in rows)

    def generation_quality_cache(
        self,
        generation_id: str,
    ) -> dict[LocalFileIdentity, int | None]:
        clean_id = validate_generation_id(generation_id)
        with self._connect() as connection:
            generation = self._generation_context(connection, clean_id)
            if str(generation["status"]) == "published":
                rows = connection.execute(
                    "SELECT device, inode, size, modified_ns, changed_ns, "
                    "quality_height, quality_source FROM media_library_files "
                    "WHERE root_key = ? AND retired_generation_id IS NULL "
                    "AND quality_source = 'probe' AND changed_ns IS NOT NULL",
                    (str(generation["root_key"]),),
                ).fetchall()
            else:
                rows = [
                    row
                    for row in self._effective_file_rows(connection, generation)
                    if str(row["quality_source"] or "") == "probe"
                    and row["changed_ns"] is not None
                ]
        cache: dict[LocalFileIdentity, int | None] = {}
        conflicted: set[LocalFileIdentity] = set()
        for row in rows:
            try:
                identity = LocalFileIdentity(
                    device=int(row["device"]),
                    inode=int(row["inode"]),
                    size=int(row["size"]),
                    modified_ns=int(row["modified_ns"]),
                    changed_ns=int(row["changed_ns"]),
                )
                height = (
                    int(row["quality_height"])
                    if row["quality_height"] is not None
                    else None
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                identity.device < 0
                or identity.inode < 0
                or identity.size <= 0
                or identity.modified_ns < 0
                or identity.changed_ns < 0
                or (height is not None and not 144 <= height <= 4320)
                or identity in conflicted
            ):
                continue
            if identity in cache and cache[identity] != height:
                cache.pop(identity, None)
                conflicted.add(identity)
            else:
                cache[identity] = height
        return cache

    def _generation_context(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        *,
        require_building: bool = False,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM media_library_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if row is None or str(row["status"]) == "failed":
            raise MediaLibraryError("media library generation is unavailable")
        if require_building and str(row["status"]) != "building":
            raise MediaLibraryConflictError(
                "media library generation can no longer be changed"
            )
        if str(row["status"]) == "published":
            current = connection.execute(
                "SELECT published_generation_id FROM media_library_roots "
                "WHERE root_key = ?",
                (str(row["root_key"]),),
            ).fetchone()
            if current is None or current[0] != generation_id:
                raise MediaLibraryError("media library generation is unavailable")
        return row

    @staticmethod
    def _uses_published_state(generation: sqlite3.Row) -> bool:
        return str(generation["status"]) == "published" or (
            str(generation["status"]) == "building"
            and str(generation["scan_kind"]) == "incremental"
        )

    @staticmethod
    def _workspace_deletions(
        connection: sqlite3.Connection, generation_id: str
    ) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT relative_path FROM media_library_workspace_deletions "
                "WHERE generation_id = ? ORDER BY relative_path",
                (generation_id,),
            ).fetchall()
        )

    def _effective_file_rows(
        self,
        connection: sqlite3.Connection,
        generation: sqlite3.Row,
        *,
        parent_path: str | None = None,
        entry_id: str | None = None,
        subtree: str | None = None,
    ) -> list[sqlite3.Row]:
        generation_id = str(generation["generation_id"])
        root_key = str(generation["root_key"])
        rows: dict[str, sqlite3.Row] = {}
        if self._uses_published_state(generation):
            clauses = ["root_key = ?", "retired_generation_id IS NULL"]
            values: list[object] = [root_key]
            if parent_path is not None:
                clauses.append("parent_path = ?")
                values.append(parent_path)
            if entry_id is not None:
                clauses.append("entry_id = ?")
                values.append(entry_id)
            current_rows = connection.execute(
                "SELECT * FROM media_library_files WHERE " + " AND ".join(clauses),
                values,
            ).fetchall()
            rows.update({str(row["relative_path"]): row for row in current_rows})
        if str(generation["status"]) == "building":
            replaced = {
                str(row[0])
                for row in connection.execute(
                    "SELECT relative_path FROM media_library_workspace_directories "
                    "WHERE generation_id = ?",
                    (generation_id,),
                ).fetchall()
            }
            deleted = self._workspace_deletions(connection, generation_id)
            rows = {
                path: row
                for path, row in rows.items()
                if str(row["parent_path"]) not in replaced
                and not any(
                    is_below(str(row["parent_path"]), ancestor) for ancestor in deleted
                )
            }
            clauses = ["generation_id = ?"]
            values = [generation_id]
            if parent_path is not None:
                clauses.append("parent_path = ?")
                values.append(parent_path)
            if entry_id is not None:
                clauses.append("entry_id = ?")
                values.append(entry_id)
            workspace_rows = connection.execute(
                "SELECT * FROM media_library_workspace_files WHERE "
                + " AND ".join(clauses),
                values,
            ).fetchall()
            rows.update({str(row["relative_path"]): row for row in workspace_rows})
        if subtree is not None:
            rows = {
                path: row
                for path, row in rows.items()
                if is_below(str(row["parent_path"]), subtree)
            }
        return list(rows.values())

    def replace_directory(
        self,
        generation_id: str,
        identity: DirectoryIdentity,
        files: Sequence[FileRecord],
    ) -> set[str]:
        clean_id = validate_generation_id(generation_id)
        relative = safe_relative_path(identity.relative_path, allow_root=True)
        affected: set[str] = set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._generation_context(
                connection, clean_id, require_building=True
            )
            affected.update(
                str(row["entry_id"])
                for row in self._effective_file_rows(
                    connection, generation, parent_path=relative
                )
            )
            connection.execute(
                "DELETE FROM media_library_workspace_files "
                "WHERE generation_id = ? AND parent_path = ?",
                (clean_id, relative),
            )
            connection.execute(
                """
                INSERT INTO media_library_workspace_directories (
                    generation_id, relative_path, device, inode,
                    modified_ns, changed_ns
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(generation_id, relative_path) DO UPDATE SET
                    device = excluded.device, inode = excluded.inode,
                    modified_ns = excluded.modified_ns, changed_ns = excluded.changed_ns
                """,
                (
                    clean_id,
                    relative,
                    identity.device,
                    identity.inode,
                    identity.modified_ns,
                    identity.changed_ns,
                ),
            )
            connection.execute(
                "DELETE FROM media_library_workspace_deletions "
                "WHERE generation_id = ? AND relative_path = ?",
                (clean_id, relative),
            )
            for item in files:
                if safe_relative_path(item.parent_path, allow_root=True) != relative:
                    connection.rollback()
                    raise MediaLibraryError(
                        "media library file parent does not match its directory"
                    )
                self._insert_file(connection, clean_id, item)
                affected.add(item.entry_id)
            connection.commit()
        return affected

    def remove_subtree(self, generation_id: str, relative_path: str) -> set[str]:
        clean_id = validate_generation_id(generation_id)
        clean_path = safe_relative_path(relative_path, allow_root=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._generation_context(
                connection, clean_id, require_building=True
            )
            rows = self._effective_file_rows(connection, generation, subtree=clean_path)
            like_value = escape_like(clean_path) + "/%"
            for table, column in (
                ("media_library_workspace_files", "parent_path"),
                ("media_library_workspace_directories", "relative_path"),
            ):
                if clean_path == ".":
                    connection.execute(
                        f"DELETE FROM {table} WHERE generation_id = ?", (clean_id,)
                    )
                else:
                    connection.execute(
                        f"DELETE FROM {table} WHERE generation_id = ? AND "
                        f"({column} = ? OR {column} LIKE ? ESCAPE '\\')",
                        (clean_id, clean_path, like_value),
                    )
            if clean_path == ".":
                connection.execute(
                    "DELETE FROM media_library_workspace_deletions "
                    "WHERE generation_id = ?",
                    (clean_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM media_library_workspace_deletions "
                    "WHERE generation_id = ? AND "
                    "(relative_path = ? OR relative_path LIKE ? ESCAPE '\\')",
                    (clean_id, clean_path, like_value),
                )
            connection.execute(
                "INSERT INTO media_library_workspace_deletions "
                "(generation_id, relative_path) VALUES (?, ?)",
                (clean_id, clean_path),
            )
            connection.commit()
        return {str(row["entry_id"]) for row in rows}

    def rebuild_entries(
        self,
        generation_id: str,
        entry_ids: Iterable[str] | None = None,
    ) -> None:
        clean_id = validate_generation_id(generation_id)
        clean_entries = (
            tuple(dict.fromkeys(validate_entry_id(value) for value in entry_ids))
            if entry_ids is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._generation_context(
                connection, clean_id, require_building=True
            )
            if clean_entries is None:
                if str(generation["scan_kind"]) == "full":
                    ids = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT DISTINCT entry_id "
                            "FROM media_library_workspace_files "
                            "WHERE generation_id = ?",
                            (clean_id,),
                        ).fetchall()
                    }
                else:
                    ids = {
                        str(row["entry_id"])
                        for row in self._effective_file_rows(connection, generation)
                    }
            else:
                ids = set(clean_entries)
            for entry_id in sorted(ids):
                rows = self._effective_file_rows(
                    connection, generation, entry_id=entry_id
                )
                rows.sort(
                    key=lambda row: (-int(row["size"]), str(row["relative_path"]))
                )
                if not rows:
                    previous = connection.execute(
                        "SELECT * FROM media_library_entries "
                        "WHERE root_key = ? AND entry_id = ? "
                        "AND retired_generation_id IS NULL",
                        (str(generation["root_key"]), entry_id),
                    ).fetchone()
                    if previous is not None:
                        self._stage_entry(
                            connection,
                            clean_id,
                            previous,
                            presence="missing",
                            duplicate_count=0,
                        )
                    continue
                self._rebuild_entry(connection, clean_id, entry_id, rows)
            self._remove_relocated_tombstones(connection, clean_id)
            connection.commit()

    def copy_missing_tombstones(
        self,
        base_generation_id: str | None,
        generation_id: str,
    ) -> None:
        if base_generation_id is None:
            return
        clean_base = validate_generation_id(base_generation_id)
        clean_id = validate_generation_id(generation_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._generation_context(
                connection, clean_id, require_building=True
            )
            if (
                str(generation["scan_kind"]) != "full"
                or connection.execute(
                    "SELECT published_generation_id FROM media_library_roots "
                    "WHERE root_key = ?",
                    (str(generation["root_key"]),),
                ).fetchone()[0]
                != clean_base
            ):
                connection.rollback()
                raise MediaLibraryError(
                    "media library tombstone base generation is unavailable"
                )
            existing = {
                str(row[0])
                for row in connection.execute(
                    "SELECT entry_id FROM media_library_workspace_entries "
                    "WHERE generation_id = ?",
                    (clean_id,),
                ).fetchall()
            }
            for previous in connection.execute(
                "SELECT * FROM media_library_entries WHERE root_key = ? "
                "AND retired_generation_id IS NULL",
                (str(generation["root_key"]),),
            ).fetchall():
                if str(previous["entry_id"]) in existing:
                    continue
                self._stage_entry(
                    connection,
                    clean_id,
                    previous,
                    presence="missing",
                    duplicate_count=0,
                )
            self._remove_relocated_tombstones(connection, clean_id)
            connection.commit()

    def _remove_relocated_tombstones(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
    ) -> None:
        generation = self._generation_context(
            connection, generation_id, require_building=True
        )
        missing = connection.execute(
            "SELECT entry_id FROM media_library_workspace_entries "
            "WHERE generation_id = ? AND presence = 'missing'",
            (generation_id,),
        ).fetchall()
        if not missing:
            return
        effective_files = self._effective_file_rows(connection, generation)
        live_fingerprints = {
            (
                str(row["device"]),
                str(row["inode"]),
                int(row["size"]),
                int(row["modified_ns"]),
                str(row["code_key"] or ""),
            )
            for row in effective_files
        }
        root_key = str(generation["root_key"])
        for item in missing:
            entry_id = str(item["entry_id"])
            historical = connection.execute(
                "SELECT device, inode, size, modified_ns, code_key "
                "FROM media_library_files WHERE root_key = ? AND entry_id = ?",
                (root_key, entry_id),
            ).fetchall()
            if not any(
                (
                    str(row["device"]),
                    str(row["inode"]),
                    int(row["size"]),
                    int(row["modified_ns"]),
                    str(row["code_key"] or ""),
                )
                in live_fingerprints
                for row in historical
            ):
                continue
            connection.execute(
                "DELETE FROM media_library_workspace_entries "
                "WHERE generation_id = ? AND entry_id = ?",
                (generation_id, entry_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO media_library_workspace_entry_deletions "
                "(generation_id, entry_id) VALUES (?, ?)",
                (generation_id, entry_id),
            )

    def publish_generation(
        self,
        generation_id: str,
        *,
        root_device: str,
        root_inode: str,
    ) -> tuple[int, int]:
        clean_id = validate_generation_id(generation_id)
        now = stored_timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = connection.execute(
                "SELECT * FROM media_library_generations WHERE generation_id = ?",
                (clean_id,),
            ).fetchone()
            if generation is None or str(generation["status"]) != "building":
                connection.rollback()
                raise MediaLibraryConflictError(
                    "media library generation can no longer be published"
                )
            root_key = str(generation["root_key"])
            root = connection.execute(
                "SELECT * FROM media_library_roots WHERE root_key = ?",
                (root_key,),
            ).fetchone()
            if root is None or (
                str(root["device"]) != identity_scalar(root_device)
                or str(root["inode"]) != identity_scalar(root_inode)
            ):
                connection.rollback()
                raise MediaLibraryRootChangedError(
                    "media library root identity changed before publication"
                )
            if str(generation["scan_kind"]) == "incremental" and (
                generation["base_generation_id"] is None
                or generation["base_generation_id"] != root["published_generation_id"]
            ):
                connection.rollback()
                raise MediaLibraryConflictError(
                    "media library base generation changed before publication"
                )
            self._refresh_duplicate_counts(connection, clean_id)
            self._publish_workspace(connection, generation)
            connection.execute(
                "UPDATE media_library_generations SET status = 'published', "
                "completed_at = ?, error_code = NULL WHERE generation_id = ?",
                (now, clean_id),
            )
            full_scan_at = (
                now
                if str(generation["scan_kind"]) == "full"
                else root["last_full_scan_at"]
            )
            connection.execute(
                """
                UPDATE media_library_roots
                SET state = 'available', published_generation_id = ?,
                    revision = revision + 1, last_success_at = ?,
                    last_full_scan_at = ?, updated_at = ?
                WHERE root_key = ?
                """,
                (clean_id, now, full_scan_at, now, root_key),
            )
            counts = connection.execute(
                "SELECT "
                "SUM(CASE WHEN presence = 'present' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN presence = 'missing' THEN 1 ELSE 0 END) "
                "FROM media_library_entries WHERE root_key = ? "
                "AND retired_generation_id IS NULL",
                (root_key,),
            ).fetchone()
            connection.commit()
        return int(counts[0] or 0), int(counts[1] or 0)

    def published_counts(self, root_key: str) -> tuple[int, int]:
        clean_key = validate_root_key(root_key)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN e.presence = 'present' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN e.presence = 'missing' THEN 1 ELSE 0 END)
                FROM media_library_roots r
                LEFT JOIN media_library_entries e
                  ON e.root_key = r.root_key
                 AND e.retired_generation_id IS NULL
                WHERE r.root_key = ?
                """,
                (clean_key,),
            ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def published_root_identity(self, root_key: str) -> tuple[str, str] | None:
        clean_key = validate_root_key(root_key)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT g.root_device, g.root_inode
                FROM media_library_roots r
                JOIN media_library_generations g
                  ON g.generation_id = r.published_generation_id
                WHERE r.root_key = ?
                """,
                (clean_key,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def list_entries(
        self,
        *,
        root_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
        query: object | None = None,
        actor: object | None = None,
        maker: object | None = None,
        tag: object | None = None,
        series: object | None = None,
        source: object | None = None,
        presence: object | None = None,
        completeness: object | None = None,
        anomaly: object | None = None,
        min_height: object | None = None,
        max_height: object | None = None,
    ) -> dict[str, object]:
        clean_limit = bounded_int(limit, "limit", minimum=1, maximum=500)
        clean_offset = bounded_int(offset, "offset", minimum=0, maximum=10_000_000)
        filter_values = {
            "root_key": root_key,
            "query": query,
            "actor": actor,
            "maker": maker,
            "tag": tag,
            "series": series,
            "source": source,
            "presence": presence,
            "completeness": completeness,
            "anomaly": anomaly,
            "min_height": min_height,
            "max_height": max_height,
        }
        clauses, values = self._entry_filters(**filter_values)
        exact_code = exact_catalog_query(query)
        base_clauses: list[str] | None = None
        base_values: list[object] | None = None
        if exact_code is not None:
            base_clauses, base_values = self._entry_filters(
                **{**filter_values, "query": None}
            )
        effective_presence = (
            "CASE WHEN r.state = 'available' THEN e.presence ELSE 'unknown' END"
        )
        with self._connect() as connection:
            if (
                exact_code is not None
                and base_clauses is not None
                and base_values is not None
            ):
                base_where = " AND ".join(base_clauses) if base_clauses else "1 = 1"
                exact_match = connection.execute(
                    "SELECT 1 FROM media_library_entries e "
                    "JOIN media_library_roots r ON r.root_key = e.root_key "
                    "WHERE e.retired_generation_id IS NULL "
                    f"AND {base_where} AND e.code_key = ? LIMIT 1",
                    (*base_values, exact_code),
                ).fetchone()
                if exact_match is not None:
                    clauses = [*base_clauses, "e.code_key = ?"]
                    values = [*base_values, exact_code]
            where = " AND ".join(clauses) if clauses else "1 = 1"
            count_row = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM media_library_entries e
                JOIN media_library_roots r
                  ON r.root_key = e.root_key
                WHERE e.retired_generation_id IS NULL AND {where}
                """,
                values,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT e.*, e.created_generation_id AS generation_id,
                       r.revision AS library_revision,
                       {effective_presence} AS effective_presence
                FROM media_library_entries e
                JOIN media_library_roots r
                  ON r.root_key = e.root_key
                WHERE e.retired_generation_id IS NULL AND {where}
                ORDER BY e.code_key IS NULL, e.code_key, e.scope_path, e.entry_id
                LIMIT ? OFFSET ?
                """,
                (*values, clean_limit, clean_offset),
            ).fetchall()
            terms = self._terms_for_rows(connection, rows)
            files = self._files_for_rows(connection, rows)
        items = [
            _entry_payload(row, terms.get(str(row["entry_id"]), {}), files)
            for row in rows
        ]
        count = int(count_row[0] if count_row is not None else 0)
        return {
            "items": items,
            "count": count,
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": clean_offset + len(items) < count,
        }

    def get_entry(self, entry_id: str) -> dict[str, object]:
        clean_id = validate_entry_id(entry_id)
        effective_presence = (
            "CASE WHEN r.state = 'available' THEN e.presence ELSE 'unknown' END"
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT e.*, e.created_generation_id AS generation_id,
                       r.revision AS library_revision,
                       {effective_presence} AS effective_presence
                FROM media_library_entries e
                JOIN media_library_roots r
                  ON r.root_key = e.root_key
                WHERE e.retired_generation_id IS NULL AND e.entry_id = ?
                ORDER BY r.updated_at DESC
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
            if row is not None:
                terms = self._terms_for_rows(connection, (row,))
                files = self._files_for_rows(connection, (row,))
                return _entry_payload(row, terms.get(clean_id, {}), files)
        raise MediaLibraryError("media library entry was not found")

    def generation_statuses(self, root_key: str) -> list[dict[str, object]]:
        clean_key = validate_root_key(root_key)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT generation_id, status, scan_kind, error_code "
                "FROM media_library_generations WHERE root_key = ? "
                "ORDER BY started_at, generation_id",
                (clean_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _entry_filters(self, **raw: object) -> tuple[list[str], list[object]]:
        clauses: list[str] = []
        values: list[object] = []
        root_key = raw["root_key"]
        if root_key is not None:
            clauses.append("r.root_key = ?")
            values.append(validate_root_key(str(root_key)))
        query = optional_query(raw["query"])
        if query is not None:
            code_query = canonical_catalog_code(query, max_length=80)
            escaped = f"%{escape_like(query.casefold())}%"
            if code_query is not None:
                clauses.append(
                    "(e.code_key LIKE ? ESCAPE '\\' OR lower(COALESCE(e.title, '')) "
                    "LIKE ? ESCAPE '\\')"
                )
                values.extend((f"%{escape_like(code_query)}%", escaped))
            else:
                clauses.append("lower(COALESCE(e.title, '')) LIKE ? ESCAPE '\\'")
                values.append(escaped)
        for key, kind in (
            ("actor", "actor"),
            ("maker", "maker"),
            ("tag", "tag"),
            ("series", "series"),
        ):
            value = optional_term(raw[key])
            if value is not None:
                clauses.append(
                    "EXISTS (SELECT 1 FROM media_library_terms t "
                    "WHERE t.root_key = e.root_key "
                    "AND t.entry_id = e.entry_id "
                    "AND t.entry_generation_id = e.created_generation_id "
                    "AND t.kind = ? AND t.value_key = ?)"
                )
                values.extend((kind, term_key(value)))
        source = optional_enum(raw["source"], {"path", "nfo"}, "source")
        if source is not None:
            clauses.append("e.source = ?")
            values.append(source)
        presence = optional_enum(raw["presence"], PRESENCE_STATES, "presence")
        effective_presence = (
            "CASE WHEN r.state = 'available' THEN e.presence ELSE 'unknown' END"
        )
        if presence is not None:
            clauses.append(f"{effective_presence} = ?")
            values.append(presence)
        completeness = optional_enum(
            raw["completeness"], {"complete", "incomplete"}, "completeness"
        )
        complete_sql = (
            "e.nfo_status = 'present' AND e.portrait_status = 'present' "
            "AND e.landscape_status = 'present'"
        )
        if completeness == "complete":
            clauses.append(f"({complete_sql})")
        elif completeness == "incomplete":
            clauses.append(f"NOT ({complete_sql})")
        anomaly = optional_enum(
            raw["anomaly"],
            {"duplicate", "unidentified", "missing", "nfo", "portrait", "landscape"},
            "anomaly",
        )
        if anomaly == "duplicate":
            clauses.append("e.duplicate_count > 1")
        elif anomaly == "unidentified":
            clauses.append("e.code_key IS NULL")
        elif anomaly == "missing":
            clauses.append(f"{effective_presence} = 'missing'")
        elif anomaly in {"nfo", "portrait", "landscape"}:
            clauses.append(f"e.{anomaly}_status != 'present'")
        minimum = optional_height(raw["min_height"], "min_height")
        maximum = optional_height(raw["max_height"], "max_height")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise MediaLibraryError("media library height range is invalid")
        if minimum is not None:
            clauses.append("e.quality_height >= ?")
            values.append(minimum)
        if maximum is not None:
            clauses.append("e.quality_height <= ?")
            values.append(maximum)
        return clauses, values

    def _terms_for_rows(
        self, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> dict[str, dict[str, list[str]]]:
        if not rows:
            return {}
        grouped: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        roots: defaultdict[str, list[str]] = defaultdict(list)
        for row in rows:
            roots[str(row["root_key"])].append(str(row["entry_id"]))
        for root_key, entry_ids in roots.items():
            for start in range(0, len(entry_ids), 400):
                chunk = tuple(entry_ids[start : start + 400])
                slots = ", ".join("?" for _ in chunk)
                terms = connection.execute(
                    "SELECT terms.entry_id, terms.kind, terms.value "
                    "FROM media_library_terms AS terms "
                    "JOIN media_library_entries AS entries "
                    "ON entries.root_key = terms.root_key "
                    "AND entries.entry_id = terms.entry_id "
                    "AND entries.created_generation_id = terms.entry_generation_id "
                    "AND entries.retired_generation_id IS NULL "
                    f"WHERE terms.root_key = ? AND terms.entry_id IN ({slots}) "
                    "ORDER BY terms.entry_id, terms.kind, terms.value_key, terms.value",
                    (root_key, *chunk),
                ).fetchall()
                for term in terms:
                    grouped[str(term["entry_id"])][str(term["kind"])].append(
                        str(term["value"])
                    )
        return {key: dict(value) for key, value in grouped.items()}

    def _files_for_rows(
        self, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> dict[tuple[str, str], list[dict[str, object]]]:
        result: dict[tuple[str, str], list[dict[str, object]]] = {
            (str(row["generation_id"]), str(row["entry_id"])): [] for row in rows
        }
        keys = {
            (str(row["root_key"]), str(row["entry_id"])): (
                str(row["generation_id"]),
                str(row["entry_id"]),
            )
            for row in rows
        }
        roots: defaultdict[str, list[str]] = defaultdict(list)
        for row in rows:
            roots[str(row["root_key"])].append(str(row["entry_id"]))
        for root_key, entry_ids in roots.items():
            for start in range(0, len(entry_ids), 400):
                chunk = tuple(entry_ids[start : start + 400])
                slots = ", ".join("?" for _ in chunk)
                files = connection.execute(
                    "SELECT entry_id, relative_path, variant "
                    "FROM media_library_files WHERE root_key = ? "
                    "AND retired_generation_id IS NULL "
                    f"AND entry_id IN ({slots}) ORDER BY entry_id, relative_path",
                    (root_key, *chunk),
                ).fetchall()
                for item in files:
                    key = keys[(root_key, str(item["entry_id"]))]
                    result[key].append(
                        {
                            "relative_path": str(item["relative_path"]),
                            "variant": optional_web_download_variant(item["variant"]),
                        }
                    )
        return result

    def _stage_entry(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        row: sqlite3.Row,
        *,
        presence: str | None = None,
        duplicate_count: int | None = None,
    ) -> None:
        entry_id = str(row["entry_id"])
        if "created_generation_id" in row.keys():
            terms = connection.execute(
                "SELECT kind, value, value_key FROM media_library_terms "
                "WHERE root_key = ? AND entry_id = ? AND entry_generation_id = ?",
                (
                    str(row["root_key"]),
                    entry_id,
                    str(row["created_generation_id"]),
                ),
            ).fetchall()
        else:
            terms = connection.execute(
                "SELECT kind, value, value_key "
                "FROM media_library_workspace_terms "
                "WHERE generation_id = ? AND entry_id = ?",
                (generation_id, entry_id),
            ).fetchall()
        connection.execute(
            """
            INSERT INTO media_library_workspace_entries (
                generation_id, entry_id, scope_path, code, code_key, variant,
                title, release_date, source, presence, primary_media_path,
                nfo_status, nfo_path, portrait_status, landscape_status,
                quality_height, duplicate_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(generation_id, entry_id) DO UPDATE SET
                scope_path = excluded.scope_path,
                code = excluded.code,
                code_key = excluded.code_key,
                variant = excluded.variant,
                title = excluded.title,
                release_date = excluded.release_date,
                source = excluded.source,
                presence = excluded.presence,
                primary_media_path = excluded.primary_media_path,
                nfo_status = excluded.nfo_status,
                nfo_path = excluded.nfo_path,
                portrait_status = excluded.portrait_status,
                landscape_status = excluded.landscape_status,
                quality_height = excluded.quality_height,
                duplicate_count = excluded.duplicate_count,
                updated_at = excluded.updated_at
            """,
            (
                generation_id,
                entry_id,
                str(row["scope_path"]),
                row["code"],
                row["code_key"],
                row["variant"],
                row["title"],
                row["release_date"],
                str(row["source"]),
                presence or str(row["presence"]),
                str(row["primary_media_path"]),
                str(row["nfo_status"]),
                row["nfo_path"],
                str(row["portrait_status"]),
                str(row["landscape_status"]),
                row["quality_height"],
                (
                    int(row["duplicate_count"])
                    if duplicate_count is None
                    else duplicate_count
                ),
                stored_timestamp(self._clock()),
            ),
        )
        connection.execute(
            "DELETE FROM media_library_workspace_entry_deletions "
            "WHERE generation_id = ? AND entry_id = ?",
            (generation_id, entry_id),
        )
        connection.execute(
            "DELETE FROM media_library_workspace_terms "
            "WHERE generation_id = ? AND entry_id = ?",
            (generation_id, entry_id),
        )
        for term in terms:
            connection.execute(
                "INSERT OR IGNORE INTO media_library_workspace_terms "
                "(generation_id, entry_id, kind, value, value_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    generation_id,
                    entry_id,
                    str(term["kind"]),
                    str(term["value"]),
                    str(term["value_key"]),
                ),
            )

    def _insert_file(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        item: FileRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO media_library_workspace_files (
                generation_id, relative_path, parent_path, entry_id, scope_path,
                code, code_key, variant, source, device, inode, size, modified_ns, suffix,
                changed_ns, part_key, quality_height, quality_source, nfo_status,
                nfo_path, nfo_json, portrait_status, landscape_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                item.relative_path,
                item.parent_path,
                item.entry_id,
                item.scope_path,
                item.code,
                item.code_key,
                item.variant,
                item.source,
                item.device,
                item.inode,
                item.size,
                item.modified_ns,
                item.suffix,
                item.changed_ns,
                item.part_key,
                item.quality_height,
                item.quality_source,
                item.nfo_status,
                item.nfo_path,
                item.nfo_json,
                item.portrait_status,
                item.landscape_status,
            ),
        )

    def _rebuild_entry(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        entry_id: str,
        rows: Sequence[sqlite3.Row],
    ) -> None:
        primary = rows[0]
        metadata_row = next((row for row in rows if row["nfo_json"] is not None), None)
        metadata = (
            nfo_from_json(str(metadata_row["nfo_json"]))
            if metadata_row is not None
            else None
        )
        code = next((str(row["code"]) for row in rows if row["code"]), None)
        code_key = next((str(row["code_key"]) for row in rows if row["code_key"]), None)
        variant = optional_web_download_variant(primary["variant"])
        source = "nfo" if metadata is not None else str(primary["source"])
        nfo_row = metadata_row or next(
            (row for row in rows if str(row["nfo_status"]) == "present"), primary
        )
        portrait_status = (
            "present"
            if any(str(row["portrait_status"]) == "present" for row in rows)
            else "missing"
        )
        landscape_status = (
            "present"
            if any(str(row["landscape_status"]) == "present" for row in rows)
            else "missing"
        )
        quality_values = [
            int(row["quality_height"])
            for row in rows
            if row["quality_height"] is not None
        ]
        connection.execute(
            """
            INSERT INTO media_library_workspace_entries (
                generation_id, entry_id, scope_path, code, code_key, variant, title,
                release_date, source, presence, primary_media_path, nfo_status,
                nfo_path, portrait_status, landscape_status, quality_height,
                duplicate_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'present', ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(generation_id, entry_id) DO UPDATE SET
                scope_path = excluded.scope_path, code = excluded.code,
                code_key = excluded.code_key, variant = excluded.variant,
                title = excluded.title,
                release_date = excluded.release_date, source = excluded.source,
                presence = 'present', primary_media_path = excluded.primary_media_path,
                nfo_status = excluded.nfo_status, nfo_path = excluded.nfo_path,
                portrait_status = excluded.portrait_status,
                landscape_status = excluded.landscape_status,
                quality_height = excluded.quality_height,
                updated_at = excluded.updated_at
            """,
            (
                generation_id,
                entry_id,
                str(primary["scope_path"]),
                code,
                code_key,
                variant,
                metadata.title if metadata else None,
                metadata.release_date if metadata else None,
                source,
                str(primary["relative_path"]),
                str(nfo_row["nfo_status"]),
                str(nfo_row["nfo_path"]) if nfo_row["nfo_path"] else None,
                portrait_status,
                landscape_status,
                max(quality_values) if quality_values else None,
                stored_timestamp(self._clock()),
            ),
        )
        connection.execute(
            "DELETE FROM media_library_workspace_entry_deletions "
            "WHERE generation_id = ? AND entry_id = ?",
            (generation_id, entry_id),
        )
        connection.execute(
            "DELETE FROM media_library_workspace_terms "
            "WHERE generation_id = ? AND entry_id = ?",
            (generation_id, entry_id),
        )
        if metadata is not None:
            for kind, values in (
                ("actor", metadata.actors),
                ("maker", metadata.makers),
                ("publisher", metadata.publishers),
                ("tag", metadata.tags),
                ("series", metadata.series),
                ("director", metadata.directors),
            ):
                for value in values:
                    connection.execute(
                        "INSERT OR IGNORE INTO media_library_workspace_terms "
                        "(generation_id, entry_id, kind, value, value_key) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (generation_id, entry_id, kind, value, term_key(value)),
                    )

    def _refresh_duplicate_counts(
        self, connection: sqlite3.Connection, generation_id: str
    ) -> None:
        generation = self._generation_context(
            connection, generation_id, require_building=True
        )
        if str(generation["scan_kind"]) == "full":
            connection.execute(
                "UPDATE media_library_workspace_entries "
                "SET duplicate_count = 0 WHERE generation_id = ?",
                (generation_id,),
            )
            groups = connection.execute(
                "SELECT code_key, variant, COUNT(*) AS entry_count "
                "FROM media_library_workspace_entries "
                "WHERE generation_id = ? AND presence = 'present' "
                "AND code_key IS NOT NULL GROUP BY code_key, variant "
                "HAVING COUNT(*) > 1",
                (generation_id,),
            ).fetchall()
            for group in groups:
                connection.execute(
                    "UPDATE media_library_workspace_entries "
                    "SET duplicate_count = ? WHERE generation_id = ? "
                    "AND presence = 'present' AND code_key = ? AND variant IS ?",
                    (
                        int(group["entry_count"]),
                        generation_id,
                        str(group["code_key"]),
                        group["variant"],
                    ),
                )
            return

        root_key = str(generation["root_key"])
        changed_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT entry_id FROM media_library_workspace_entries "
                "WHERE generation_id = ? UNION SELECT entry_id FROM "
                "media_library_workspace_entry_deletions WHERE generation_id = ?",
                (generation_id, generation_id),
            ).fetchall()
        }
        groups = {
            (str(row["code_key"]), row["variant"])
            for row in connection.execute(
                "SELECT code_key, variant FROM media_library_workspace_entries "
                "WHERE generation_id = ? AND code_key IS NOT NULL UNION "
                "SELECT published.code_key, published.variant "
                "FROM media_library_entries AS published "
                "WHERE published.root_key = ? "
                "AND published.retired_generation_id IS NULL "
                "AND published.code_key IS NOT NULL AND published.entry_id IN ("
                "SELECT entry_id FROM media_library_workspace_entries "
                "WHERE generation_id = ? UNION SELECT entry_id FROM "
                "media_library_workspace_entry_deletions WHERE generation_id = ?)",
                (generation_id, root_key, generation_id, generation_id),
            ).fetchall()
        }
        for code_key, variant in groups:
            effective: dict[str, sqlite3.Row] = {
                str(row["entry_id"]): row
                for row in connection.execute(
                    "SELECT * FROM media_library_entries WHERE root_key = ? "
                    "AND retired_generation_id IS NULL AND presence = 'present' "
                    "AND code_key = ? AND variant IS ?",
                    (root_key, code_key, variant),
                ).fetchall()
                if str(row["entry_id"]) not in changed_ids
            }
            effective.update(
                {
                    str(row["entry_id"]): row
                    for row in connection.execute(
                        "SELECT * FROM media_library_workspace_entries "
                        "WHERE generation_id = ? AND presence = 'present' "
                        "AND code_key = ? AND variant IS ?",
                        (generation_id, code_key, variant),
                    ).fetchall()
                }
            )
            desired = len(effective) if len(effective) > 1 else 0
            for row in effective.values():
                if int(row["duplicate_count"]) == desired:
                    continue
                if "generation_id" in row.keys():
                    connection.execute(
                        "UPDATE media_library_workspace_entries "
                        "SET duplicate_count = ? "
                        "WHERE generation_id = ? AND entry_id = ?",
                        (desired, generation_id, str(row["entry_id"])),
                    )
                else:
                    self._stage_entry(
                        connection,
                        generation_id,
                        row,
                        duplicate_count=desired,
                    )

    def _publish_workspace(
        self,
        connection: sqlite3.Connection,
        generation: sqlite3.Row,
    ) -> None:
        generation_id = str(generation["generation_id"])
        root_key = str(generation["root_key"])
        if str(generation["scan_kind"]) == "full":
            self._compact_full_workspace(connection, generation_id, root_key)
        else:
            for deleted in self._workspace_deletions(connection, generation_id):
                if deleted == ".":
                    directory_clause = "1 = 1"
                    directory_values: tuple[object, ...] = ()
                else:
                    like_value = escape_like(deleted) + "/%"
                    directory_clause = (
                        "(relative_path = ? OR relative_path LIKE ? ESCAPE '\\')"
                    )
                    directory_values = (deleted, like_value)
                connection.execute(
                    "UPDATE media_library_directories SET retired_generation_id = ? "
                    "WHERE root_key = ? AND retired_generation_id IS NULL AND "
                    + directory_clause,
                    (generation_id, root_key, *directory_values),
                )
                if deleted == ".":
                    file_clause = "1 = 1"
                    file_values: tuple[object, ...] = ()
                else:
                    file_clause = "(parent_path = ? OR parent_path LIKE ? ESCAPE '\\')"
                    file_values = (deleted, like_value)
                connection.execute(
                    "UPDATE media_library_files SET retired_generation_id = ? "
                    "WHERE root_key = ? AND retired_generation_id IS NULL AND "
                    + file_clause,
                    (generation_id, root_key, *file_values),
                )
            self._compact_incremental_workspace(connection, generation_id, root_key)
            self._compact_workspace_entries(
                connection, generation_id, root_key, complete=False
            )

        connection.execute(
            """
            INSERT INTO media_library_directories (
                root_key, relative_path, created_generation_id,
                retired_generation_id, device, inode, modified_ns, changed_ns
            )
            SELECT ?, relative_path, ?, NULL, device, inode, modified_ns, changed_ns
            FROM media_library_workspace_directories WHERE generation_id = ?
            """,
            (root_key, generation_id, generation_id),
        )
        connection.execute(
            """
            INSERT INTO media_library_files (
                root_key, relative_path, created_generation_id,
                retired_generation_id, parent_path, entry_id, scope_path,
                code, code_key, variant, source, device, inode, size, modified_ns,
                suffix, changed_ns, part_key, quality_height, quality_source,
                nfo_status, nfo_path, nfo_json, portrait_status, landscape_status
            )
            SELECT ?, relative_path, ?, NULL, parent_path, entry_id, scope_path,
                   code, code_key, variant, source, device, inode, size, modified_ns,
                   suffix, changed_ns, part_key, quality_height, quality_source,
                   nfo_status, nfo_path, nfo_json, portrait_status, landscape_status
            FROM media_library_workspace_files WHERE generation_id = ?
            """,
            (root_key, generation_id, generation_id),
        )
        changed_entries = {
            str(row[0])
            for row in connection.execute(
                "SELECT entry_id FROM media_library_workspace_entries "
                "WHERE generation_id = ? UNION SELECT entry_id FROM "
                "media_library_workspace_entry_deletions WHERE generation_id = ?",
                (generation_id, generation_id),
            ).fetchall()
        }
        for entry_id in changed_entries:
            connection.execute(
                "UPDATE media_library_entries SET retired_generation_id = ? "
                "WHERE root_key = ? AND entry_id = ? "
                "AND retired_generation_id IS NULL",
                (generation_id, root_key, entry_id),
            )
        connection.execute(
            """
            INSERT INTO media_library_entries (
                root_key, entry_id, created_generation_id, retired_generation_id,
                scope_path, code, code_key, variant, title, release_date, source,
                presence, primary_media_path, nfo_status, nfo_path,
                portrait_status, landscape_status, quality_height,
                duplicate_count, updated_at
            )
            SELECT ?, entry_id, ?, NULL, scope_path, code, code_key, variant,
                   title, release_date, source, presence, primary_media_path,
                   nfo_status, nfo_path, portrait_status, landscape_status,
                   quality_height, duplicate_count, updated_at
            FROM media_library_workspace_entries WHERE generation_id = ?
            """,
            (root_key, generation_id, generation_id),
        )
        connection.execute(
            """
            INSERT INTO media_library_terms (
                root_key, entry_id, entry_generation_id, kind, value, value_key
            )
            SELECT ?, entry_id, ?, kind, value, value_key
            FROM media_library_workspace_terms WHERE generation_id = ?
            """,
            (root_key, generation_id, generation_id),
        )
        self._clear_workspace(connection, generation_id)

    def _compact_incremental_workspace(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        root_key: str,
    ) -> None:
        file_fields = (
            "parent_path",
            "entry_id",
            "scope_path",
            "code",
            "code_key",
            "variant",
            "source",
            "device",
            "inode",
            "size",
            "modified_ns",
            "suffix",
            "changed_ns",
            "part_key",
            "quality_height",
            "quality_source",
            "nfo_status",
            "nfo_path",
            "nfo_json",
            "portrait_status",
            "landscape_status",
        )
        replaced = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT relative_path FROM media_library_workspace_directories "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
        )
        file_equality = " AND ".join(
            f"workspace.{field} IS published.{field}" for field in file_fields
        )
        for relative in replaced:
            connection.execute(
                "UPDATE media_library_directories SET retired_generation_id = ? "
                "WHERE root_key = ? AND relative_path = ? "
                "AND retired_generation_id IS NULL "
                "AND NOT EXISTS (SELECT 1 "
                "FROM media_library_workspace_directories AS workspace "
                "WHERE workspace.generation_id = ? "
                "AND workspace.relative_path = media_library_directories.relative_path "
                "AND workspace.device IS media_library_directories.device "
                "AND workspace.inode IS media_library_directories.inode "
                "AND workspace.modified_ns IS media_library_directories.modified_ns "
                "AND workspace.changed_ns IS media_library_directories.changed_ns)",
                (generation_id, root_key, relative, generation_id),
            )
            connection.execute(
                "UPDATE media_library_files AS published "
                "SET retired_generation_id = ? WHERE published.root_key = ? "
                "AND published.parent_path = ? "
                "AND published.retired_generation_id IS NULL "
                "AND NOT EXISTS (SELECT 1 "
                "FROM media_library_workspace_files AS workspace "
                "WHERE workspace.generation_id = ? "
                "AND workspace.relative_path = published.relative_path "
                f"AND {file_equality})",
                (generation_id, root_key, relative, generation_id),
            )
        connection.execute(
            "DELETE FROM media_library_workspace_directories AS workspace "
            "WHERE workspace.generation_id = ? "
            "AND EXISTS (SELECT 1 FROM media_library_directories AS published "
            "WHERE published.root_key = ? "
            "AND published.retired_generation_id IS NULL "
            "AND published.relative_path = workspace.relative_path "
            "AND published.device IS workspace.device "
            "AND published.inode IS workspace.inode "
            "AND published.modified_ns IS workspace.modified_ns "
            "AND published.changed_ns IS workspace.changed_ns)",
            (generation_id, root_key),
        )
        connection.execute(
            "DELETE FROM media_library_workspace_files AS workspace "
            "WHERE workspace.generation_id = ? "
            "AND EXISTS (SELECT 1 FROM media_library_files AS published "
            "WHERE published.root_key = ? "
            "AND published.retired_generation_id IS NULL "
            "AND published.relative_path = workspace.relative_path "
            f"AND {file_equality})",
            (generation_id, root_key),
        )

    def _compact_full_workspace(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        root_key: str,
    ) -> None:
        directory_fields = ("device", "inode", "modified_ns", "changed_ns")
        file_fields = (
            "parent_path",
            "entry_id",
            "scope_path",
            "code",
            "code_key",
            "variant",
            "source",
            "device",
            "inode",
            "size",
            "modified_ns",
            "suffix",
            "changed_ns",
            "part_key",
            "quality_height",
            "quality_source",
            "nfo_status",
            "nfo_path",
            "nfo_json",
            "portrait_status",
            "landscape_status",
        )
        for published_table, workspace_table, fields in (
            (
                "media_library_directories",
                "media_library_workspace_directories",
                directory_fields,
            ),
            (
                "media_library_files",
                "media_library_workspace_files",
                file_fields,
            ),
        ):
            equality = " AND ".join(
                f"workspace.{field} IS published.{field}" for field in fields
            )
            connection.execute(
                f"UPDATE {published_table} AS published "
                "SET retired_generation_id = ? "
                "WHERE published.root_key = ? "
                "AND published.retired_generation_id IS NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {workspace_table} AS workspace "
                "WHERE workspace.generation_id = ? "
                "AND workspace.relative_path = published.relative_path "
                f"AND {equality})",
                (generation_id, root_key, generation_id),
            )
            connection.execute(
                f"DELETE FROM {workspace_table} AS workspace "
                "WHERE workspace.generation_id = ? "
                f"AND EXISTS (SELECT 1 FROM {published_table} AS published "
                "WHERE published.root_key = ? "
                "AND published.retired_generation_id IS NULL "
                "AND published.relative_path = workspace.relative_path "
                f"AND {equality})",
                (generation_id, root_key),
            )

        self._compact_workspace_entries(
            connection, generation_id, root_key, complete=True
        )

    def _compact_workspace_entries(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        root_key: str,
        *,
        complete: bool,
    ) -> None:
        desired_entries = {
            str(row[0])
            for row in connection.execute(
                "SELECT entry_id FROM media_library_workspace_entries "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
        }
        if complete:
            current_rows = connection.execute(
                "SELECT * FROM media_library_entries WHERE root_key = ? "
                "AND retired_generation_id IS NULL",
                (root_key,),
            ).fetchall()
        else:
            current_rows = connection.execute(
                "SELECT published.* FROM media_library_entries AS published "
                "JOIN media_library_workspace_entries AS workspace "
                "ON workspace.generation_id = ? "
                "AND workspace.entry_id = published.entry_id "
                "WHERE published.root_key = ? "
                "AND published.retired_generation_id IS NULL",
                (generation_id, root_key),
            ).fetchall()
        current_entries = {str(row["entry_id"]): row for row in current_rows}
        if complete:
            for entry_id in current_entries.keys() - desired_entries:
                connection.execute(
                    "INSERT OR IGNORE INTO media_library_workspace_entry_deletions "
                    "(generation_id, entry_id) VALUES (?, ?)",
                    (generation_id, entry_id),
                )
        entry_fields = (
            "scope_path",
            "code",
            "code_key",
            "variant",
            "title",
            "release_date",
            "source",
            "presence",
            "primary_media_path",
            "nfo_status",
            "nfo_path",
            "portrait_status",
            "landscape_status",
            "quality_height",
            "duplicate_count",
        )
        for entry_id in desired_entries & current_entries.keys():
            desired = connection.execute(
                "SELECT * FROM media_library_workspace_entries "
                "WHERE generation_id = ? AND entry_id = ?",
                (generation_id, entry_id),
            ).fetchone()
            current = current_entries[entry_id]
            if desired is None or any(
                desired[field] != current[field] for field in entry_fields
            ):
                continue
            desired_terms = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in connection.execute(
                    "SELECT kind, value, value_key "
                    "FROM media_library_workspace_terms "
                    "WHERE generation_id = ? AND entry_id = ?",
                    (generation_id, entry_id),
                ).fetchall()
            }
            current_terms = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in connection.execute(
                    "SELECT kind, value, value_key FROM media_library_terms "
                    "WHERE root_key = ? AND entry_id = ? "
                    "AND entry_generation_id = ?",
                    (root_key, entry_id, str(current["created_generation_id"])),
                ).fetchall()
            }
            if desired_terms == current_terms:
                connection.execute(
                    "DELETE FROM media_library_workspace_entries "
                    "WHERE generation_id = ? AND entry_id = ?",
                    (generation_id, entry_id),
                )

    @staticmethod
    def _clear_workspace(connection: sqlite3.Connection, generation_id: str) -> None:
        for table in (
            "media_library_workspace_terms",
            "media_library_workspace_entry_deletions",
            "media_library_workspace_entries",
            "media_library_workspace_files",
            "media_library_workspace_deletions",
            "media_library_workspace_directories",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE generation_id = ?", (generation_id,)
            )

    def _initialize(self) -> None:
        initialize_media_library_schema(self.path, clock=self._clock)

    def _recover_interrupted_generations(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interrupted = [
                str(row[0])
                for row in connection.execute(
                    "SELECT generation_id FROM media_library_generations "
                    "WHERE status = 'building'"
                ).fetchall()
            ]
            connection.execute(
                "UPDATE media_library_generations SET status = 'failed', "
                "completed_at = ?, error_code = 'interrupted' "
                "WHERE status = 'building'",
                (stored_timestamp(self._clock()),),
            )
            for generation_id in interrupted:
                self._clear_workspace(connection, generation_id)
            connection.commit()

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


def _root_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "root_key": str(row["root_key"]),
        "device": str(row["device"]),
        "inode": str(row["inode"]),
        "state": str(row["state"]),
        "published_generation_id": (
            str(row["published_generation_id"])
            if row["published_generation_id"] is not None
            else None
        ),
        "revision": int(row["revision"]),
        "last_success_at": (
            float(row["last_success_at"])
            if row["last_success_at"] is not None
            else None
        ),
        "last_full_scan_at": (
            float(row["last_full_scan_at"])
            if row["last_full_scan_at"] is not None
            else None
        ),
        "updated_at": float(row["updated_at"]),
    }


def _file_record_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        relative_path=str(row["relative_path"]),
        parent_path=str(row["parent_path"]),
        entry_id=str(row["entry_id"]),
        scope_path=str(row["scope_path"]),
        code=str(row["code"]) if row["code"] is not None else None,
        code_key=(str(row["code_key"]) if row["code_key"] is not None else None),
        variant=optional_web_download_variant(row["variant"]),
        source=str(row["source"]),
        device=str(row["device"]),
        inode=str(row["inode"]),
        size=int(row["size"]),
        modified_ns=int(row["modified_ns"]),
        changed_ns=(int(row["changed_ns"]) if row["changed_ns"] is not None else -1),
        suffix=str(row["suffix"]),
        part_key=(str(row["part_key"]) if row["part_key"] is not None else None),
        quality_height=(
            int(row["quality_height"]) if row["quality_height"] is not None else None
        ),
        quality_source=(
            str(row["quality_source"]) if row["quality_source"] is not None else None
        ),
        nfo_status=str(row["nfo_status"]),
        nfo_path=(str(row["nfo_path"]) if row["nfo_path"] is not None else None),
        nfo_json=(str(row["nfo_json"]) if row["nfo_json"] is not None else None),
        portrait_status=str(row["portrait_status"]),
        landscape_status=str(row["landscape_status"]),
    )


def _entry_payload(
    row: sqlite3.Row,
    terms: Mapping[str, Sequence[str]],
    files: Mapping[tuple[str, str], list[dict[str, object]]],
) -> dict[str, object]:
    media_files = files.get((str(row["generation_id"]), str(row["entry_id"])), [])
    return {
        "entry_id": str(row["entry_id"]),
        "revision": int(row["library_revision"]),
        "scope_path": str(row["scope_path"]),
        "code": str(row["code"]) if row["code"] is not None else None,
        "code_key": str(row["code_key"]) if row["code_key"] is not None else None,
        "variant": optional_web_download_variant(row["variant"]),
        "title": str(row["title"]) if row["title"] is not None else None,
        "release_date": (
            str(row["release_date"]) if row["release_date"] is not None else None
        ),
        "source": str(row["source"]),
        "presence": str(row["effective_presence"]),
        "primary_media_path": str(row["primary_media_path"]),
        "media_paths": [str(item["relative_path"]) for item in media_files],
        "media_files": media_files,
        "nfo_status": str(row["nfo_status"]),
        "nfo_path": str(row["nfo_path"]) if row["nfo_path"] is not None else None,
        "portrait_status": str(row["portrait_status"]),
        "landscape_status": str(row["landscape_status"]),
        "quality_height": (
            int(row["quality_height"]) if row["quality_height"] is not None else None
        ),
        "duplicate_count": int(row["duplicate_count"]),
        "actors": list(terms.get("actor", ())),
        "makers": list(terms.get("maker", ())),
        "publishers": list(terms.get("publisher", ())),
        "tags": list(terms.get("tag", ())),
        "series": list(terms.get("series", ())),
        "directors": list(terms.get("director", ())),
    }
