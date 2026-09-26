"""Results of MissAV discovery: series codes and resource pages."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..web_download.variant import MissavVariant

__all__ = [
    "ManifestRequest",
    "MissavResourceDiscovery",
    "MissavResourceItem",
    "MissavResourcePage",
    "MissavResourceState",
    "MissavSeriesDiscovery",
]


@dataclass(frozen=True, slots=True)
class MissavSeriesDiscovery:
    codes: tuple[str, ...]
    complete: bool
    variants_by_code: tuple[tuple[str, tuple[MissavVariant, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class MissavResourceItem:
    code: str
    available_variants: tuple[MissavVariant, ...]
    title: str | None = None


@dataclass(frozen=True, slots=True)
class MissavResourceState:
    next_page: int | None
    pending: tuple[MissavResourceItem, ...]
    cursor: int
    total_pages: int | None
    scanned_pages: int


@dataclass(frozen=True, slots=True)
class MissavResourcePage:
    page: int
    fetched: bool
    items: tuple[MissavResourceItem, ...]
    state: MissavResourceState


@dataclass(frozen=True, slots=True)
class MissavResourceDiscovery:
    items: tuple[MissavResourceItem, ...]
    state: MissavResourceState
    complete: bool


@dataclass(frozen=True, slots=True)
class ManifestRequest:
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    page_url: str
    selected_height: int | None = None

    def __repr__(self) -> str:
        return (
            "ManifestRequest(url=<redacted>, headers=<redacted>, "
            f"page_url={self.page_url!r}, selected_height={self.selected_height!r})"
        )
