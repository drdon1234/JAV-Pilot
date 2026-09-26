"""Read-only media library snapshots used for deduplication."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .errors import (
    MediaLibraryCapacityError,
    MediaLibraryError,
    MediaLibraryUnavailableError,
)
from .fields import validate_entry_id, validate_generation_id, validate_root_key
from .models import MAX_SCAN_ENTRIES, MediaLibraryDedupRecord, MediaLibraryDedupSnapshot

def read_media_library_dedup_snapshot(
    database_path: Path | str,
    root_key: str,
    *,
    max_rows: int,
    page_size: int = 1000,
) -> MediaLibraryDedupSnapshot:
    """Read the minimal batch-dedup projection without opening a writer.

    The normal store constructor owns schema initialization and interrupted
    generation recovery.  A batch preview is only a consumer, so it uses a
    read-only SQLite snapshot and never creates directories, enables WAL, or
    runs migrations.  Keyset paging avoids OFFSET scans as the library grows.
    """

    path = Path(database_path)
    if not path.is_absolute():
        raise MediaLibraryError("media library database path must be absolute")
    clean_root_key = validate_root_key(root_key)
    if (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or max_rows < 1
        or max_rows > MAX_SCAN_ENTRIES
    ):
        raise MediaLibraryError("media library deduplication limit is invalid")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size < 1
        or page_size > 5000
    ):
        raise MediaLibraryError("media library deduplication page size is invalid")
    if not path.is_file():
        raise MediaLibraryUnavailableError("media library index is unavailable")

    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(
        sqlite3.connect(
            uri,
            timeout=5.0,
            isolation_level=None,
            uri=True,
        )
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN")
        root = connection.execute(
            "SELECT state, published_generation_id, revision "
            "FROM media_library_roots WHERE root_key = ?",
            (clean_root_key,),
        ).fetchone()
        if (
            root is None
            or str(root["state"]) != "available"
            or root["published_generation_id"] is None
            or not isinstance(root["revision"], int)
        ):
            raise MediaLibraryUnavailableError("media library index is unavailable")
        generation_id = validate_generation_id(str(root["published_generation_id"]))
        count_row = connection.execute(
            "SELECT COUNT(*) FROM media_library_entries "
            "WHERE root_key = ? AND retired_generation_id IS NULL "
            "AND presence = 'present'",
            (clean_root_key,),
        ).fetchone()
        count = int(count_row[0] if count_row is not None else 0)
        if count > max_rows:
            raise MediaLibraryCapacityError(
                "media library index contains too many records"
            )

        records: list[MediaLibraryDedupRecord] = []
        last_entry_id = ""
        while len(records) < count:
            rows = connection.execute(
                "SELECT entry_id, code_key, primary_media_path, variant, "
                "quality_height FROM media_library_entries "
                "WHERE root_key = ? AND retired_generation_id IS NULL "
                "AND presence = 'present' "
                "AND entry_id > ? ORDER BY entry_id LIMIT ?",
                (
                    clean_root_key,
                    last_entry_id,
                    min(page_size, count - len(records)),
                ),
            ).fetchall()
            if not rows:
                raise MediaLibraryError("media library index is inconsistent")
            for row in rows:
                last_entry_id = validate_entry_id(str(row["entry_id"]))
                records.append(
                    MediaLibraryDedupRecord(
                        code_key=(
                            str(row["code_key"])
                            if row["code_key"] is not None
                            else None
                        ),
                        primary_media_path=str(row["primary_media_path"]),
                        variant=(
                            str(row["variant"]) if row["variant"] is not None else None
                        ),
                        quality_height=(
                            int(row["quality_height"])
                            if row["quality_height"] is not None
                            else None
                        ),
                    )
                )
        if len(records) != count:
            raise MediaLibraryError("media library index is inconsistent")
        return MediaLibraryDedupSnapshot(
            root_key=clean_root_key,
            revision=int(root["revision"]),
            generation_id=generation_id,
            records=tuple(records),
        )
