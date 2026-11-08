"""Catalog-prefix folders and their user-selected portrait covers."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import PurePosixPath

from ..config.paths import default_database_path


def catalog_prefix(code: str | None) -> str:
    if not code:
        return "__unidentified__"
    return re.sub(r"[-_ ]?\d+$", "", code.upper()).rstrip("-_ ") or "__unidentified__"


def poster_path(item: dict) -> str:
    media = PurePosixPath(item["primary_media_path"])
    name = (
        f"{media.stem}-poster.jpg"
        if media.parent == PurePosixPath(".")
        else "poster.jpg"
    )
    return str(media.parent / name)


@contextmanager
def preferences_db():
    path = default_database_path("library_preferences.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS folder_covers (root_key TEXT NOT NULL, prefix TEXT NOT NULL, position INTEGER NOT NULL, entry_id TEXT NOT NULL, PRIMARY KEY(root_key,prefix,position))"
            )
            yield db
    finally:
        db.close()


def cover_selection(root_key: str, prefix: str) -> list[str]:
    with preferences_db() as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT entry_id FROM folder_covers WHERE root_key=? AND prefix=? ORDER BY position",
                (root_key, prefix),
            )
        ]


def set_cover_selection(root_key: str, prefix: str, entries: list[str]) -> None:
    with preferences_db() as db:
        db.execute(
            "DELETE FROM folder_covers WHERE root_key=? AND prefix=?",
            (root_key, prefix),
        )
        db.executemany(
            "INSERT INTO folder_covers VALUES(?,?,?,?)",
            [(root_key, prefix, index, entry) for index, entry in enumerate(entries)],
        )
