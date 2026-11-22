"""Named, ordered playback lists belonging to the local account."""

import re
from .store import connection


def playlists(name=None):
    with connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS personal_playlists (
            name TEXT NOT NULL, entry TEXT NOT NULL, PRIMARY KEY(name,entry))""")
        if name is None:
            return [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT name FROM personal_playlists ORDER BY name"
                )
            ]
        return [
            r[0]
            for r in db.execute(
                "SELECT entry FROM personal_playlists WHERE name=? ORDER BY rowid",
                (name,),
            )
        ]


def edit(name, entry, remove=False):
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 80
        or not re.fullmatch(r"[a-f0-9]{40}", entry)
    ):
        raise ValueError("播放列表或馆藏作品无效")
    playlists()
    with connection() as db:
        if remove:
            db.execute(
                "DELETE FROM personal_playlists WHERE name=? AND entry=?",
                (name.strip(), entry),
            )
        else:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT COUNT(*) FROM personal_playlists").fetchone()[0]
                >= 10000
            ):
                raise ValueError("播放列表已满")
            db.execute(
                "INSERT OR IGNORE INTO personal_playlists VALUES(?,?)",
                (name.strip(), entry),
            )
