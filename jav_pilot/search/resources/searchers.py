"""Routes a persisted resource search session to its site adapter."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from .errors import ResourceSearchError
from .models import (
    RESOURCE_SEARCH_SOURCE_IDS,
    ResourceSearchDiscoverer,
    ResourceSearchWork,
    ResourceSearchWorkerEvent,
    ResourceSearchWorkerResult,
)
from .validation import validate_work

__all__ = [
    "RoutedResourceSearcher",
]


class RoutedResourceSearcher:
    """Dispatch one persisted search session to its configured site adapter."""

    def __init__(self, discoverers: Mapping[str, ResourceSearchDiscoverer]) -> None:
        configured = dict(discoverers)
        if set(configured) != set(RESOURCE_SEARCH_SOURCE_IDS) or any(
            not callable(discoverer) for discoverer in configured.values()
        ):
            raise ResourceSearchError("resource search adapters are invalid")
        self._discoverers = configured

    def __call__(
        self,
        work: ResourceSearchWork,
        *,
        on_event: Callable[[ResourceSearchWorkerEvent], None],
        cancel_event: threading.Event,
    ) -> ResourceSearchWorkerResult:
        clean_work = validate_work(work)
        return self._discoverers[clean_work.source_id](
            clean_work,
            on_event=on_event,
            cancel_event=cancel_event,
        )
