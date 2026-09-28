"""Shared types and helpers for the ranking boards."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ...core.catalog_code import normalize_catalog_code
from ...indexers.metadata_catalog import public_url
from ...net.http_client import FetchError, fetch_text

MAX_ITEMS = 100
_TIMEOUT_SECONDS = 25.0
_MAX_BYTES = 8 * 1024 * 1024
_WORK_ID_RE = re.compile(r"code:[A-Za-z0-9._-]{2,72}")

Item = dict[str, Any]


class RankingError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class RankingRequest:
    board: str
    period: str
    category: str = ""


@dataclass(frozen=True, slots=True)
class Board:
    """One ranking list: where it comes from and which views it offers.

    ``group`` is ``works``, ``actors`` or ``genres``; ``item_kind`` tells the
    page whether rows are works (with codes) or people. Boards whose
    categories come from the source (FANZA genres) set ``load_categories``.
    """

    id: str
    label: str
    group: str
    item_kind: str
    source: str
    periods: tuple[Option, ...]
    fetch: Callable[[RankingRequest], list[Item]]
    categories: tuple[Option, ...] = ()
    load_categories: Callable[[], tuple[Option, ...]] | None = None
    scope: Callable[[], str] | None = None
    note: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "item_kind": self.item_kind,
            "source": self.source,
            "periods": [{"value": item.value, "label": item.label} for item in self.periods],
            "categories": [{"value": item.value, "label": item.label} for item in self.categories],
            "dynamic_categories": self.load_categories is not None,
            "note": self.note,
        }


def fetch_page(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> str:
    """Fetch one page of a fixed first-party ranking URL through the app proxy."""
    return fetch_text(
        url,
        timeout=_TIMEOUT_SECONDS,
        max_bytes=_MAX_BYTES,
        headers=headers,
        data=data,
    )


def http_status(exc: FetchError) -> int | None:
    match = re.fullmatch(r"HTTP (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def https_url(value: object, base: str) -> str | None:
    """Resolve a scraped link or image against its page, keeping only https."""
    url = public_url(value, base)
    return url if url and urlsplit(url).scheme == "https" else None


def work_id_for(code: str | None) -> str | None:
    """The search results' work id for a code, so detail links share caches.

    Codes without a catalog form (the uncensored studios' date-based ids,
    which also collide across studios) get none and offer search only.
    """
    # Same normalization as the detail prefetch endpoint, so every listed id
    # can be submitted there.
    normalized = normalize_catalog_code(code)
    work_id = f"code:{normalized[1]}" if normalized else None
    return work_id if work_id and _WORK_ID_RE.fullmatch(work_id) else None


def work_item(
    rank: int,
    *,
    source_id: str,
    code: str | None,
    title: str,
    detail_url: str | None,
    cover: str | None,
    release_date: str | None = None,
    subtitle: str | None = None,
    rating: float | None = None,
    votes: int | None = None,
) -> Item:
    return {
        "rank": rank,
        "code": code,
        "work_id": work_id_for(code),
        "title": title,
        "subtitle": subtitle or None,
        "release_date": release_date,
        "detail_url": detail_url,
        "cover": cover,
        "rating": rating,
        "votes": votes,
        "source_id": source_id,
    }


def person_item(
    rank: int,
    *,
    source_id: str,
    name: str,
    detail_url: str | None,
    cover: str | None,
    subtitle: str | None = None,
) -> Item:
    return work_item(
        rank,
        source_id=source_id,
        code=None,
        title=name,
        detail_url=detail_url,
        cover=cover,
        subtitle=subtitle,
    )


def rating_value(value: object) -> float | None:
    try:
        number = round(float(str(value)), 2)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 5 else None


def count_value(value: object) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
