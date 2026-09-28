"""Results of MissAV discovery and manifest capture."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..web_download.variant import MissavVariant

__all__ = [
    "ManifestRequest",
    "MissavSeriesDiscovery",
]


@dataclass(frozen=True, slots=True)
class MissavSeriesDiscovery:
    codes: tuple[str, ...]
    complete: bool
    variants_by_code: tuple[tuple[str, tuple[MissavVariant, ...]], ...] = ()


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
