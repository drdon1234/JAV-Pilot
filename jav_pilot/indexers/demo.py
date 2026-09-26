from __future__ import annotations

from jav_pilot.torrent.magnet import parse_magnet
from jav_pilot.core.models import SearchBounds, SearchResult

from .base import Indexer


DEMO_RECORDS = (
    {
        "code": "DEMO-001",
        "title": "DEMO-001 Sample Drama Vol. 1",
        "date": "2024-01-01",
        "actors": ("Alice Example",),
        "tags": ("drama", "sample"),
        "magnet": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=DEMO-001.Sample.Drama",
    },
    {
        "code": "DEMO-002",
        "title": "DEMO-002 Keyword Safety Example",
        "date": "2024-02-01",
        "actors": ("Bea Example",),
        "tags": ("keyword", "sample"),
        "magnet": "magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef&dn=DEMO-002.Keyword.Safety",
    },
    {
        "code": "TEST-123",
        "title": "TEST-123 Offline Parser Fixture",
        "date": "2024-03-01",
        "actors": ("Cara Example",),
        "tags": ("fixture", "parser"),
        "magnet": "magnet:?xt=urn:btih:fedcba9876543210fedcba9876543210fedcba98&dn=TEST-123.Offline.Parser",
    },
    {
        "code": "PRFX-001",
        "title": "PRFX-001 Prefix Search Fixture",
        "date": "2024-04-01",
        "actors": ("Dana Example",),
        "tags": ("prefix", "catalog"),
        "magnet": "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&dn=PRFX-001.Prefix.Search",
    },
    {
        "code": "PRFX-002",
        "title": "PRFX-002 Prefix Search Fixture Two",
        "date": "2024-04-02",
        "actors": ("Eve Example",),
        "tags": ("prefix", "catalog"),
        "magnet": "magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&dn=PRFX-002.Prefix.Search",
    },
)


class DemoIndexer(Indexer):
    name = "demo"

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        bounds = bounds.normalized()
        needle = query.casefold()
        scored: list[tuple[int, SearchResult]] = []

        for row in DEMO_RECORDS:
            haystack = " ".join(
                [
                    row["code"],
                    row["title"],
                    " ".join(row["actors"]),
                    " ".join(row["tags"]),
                ]
            ).casefold()
            if needle not in haystack:
                continue

            score = 10 if row["code"].casefold() == needle.replace(" ", "-") else 1
            scored.append(
                (
                    score,
                    SearchResult(
                        source=self.name,
                        title=row["title"],
                        code=row["code"],
                        date=row["date"],
                        actors=tuple(row["actors"]),
                        tags=tuple(row["tags"]),
                        magnets=(parse_magnet(row["magnet"]),),
                        metadata={"fixture": True},
                    ),
                )
            )

        scored.sort(key=lambda item: (-item[0], item[1].title))
        return tuple(result for _, result in scored[: bounds.limit])
