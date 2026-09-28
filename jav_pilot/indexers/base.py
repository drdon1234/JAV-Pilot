from __future__ import annotations

from abc import ABC, abstractmethod

from jav_pilot.core.models import SearchBounds, SearchResult


class Indexer(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        raise NotImplementedError

    def skip_reason(self, query: str, bounds: SearchBounds) -> str | None:
        """A short reason when this source cannot answer the query at all."""

        return None

    def diagnostic_detail_search(
        self,
        query: str,
        bounds: SearchBounds,
    ) -> tuple[SearchResult, ...]:
        """Search and resolve detail components for functional diagnostics."""

        raise NotImplementedError
