"""Preview, cleanup, export and vacuum of download history across the task databases."""

from __future__ import annotations

from .cleanup import HistoryCleanupMixin
from .export import HistoryExportMixin
from .lifecycle_base import HistoryLifecycleBase
from .preview import HistoryPreviewMixin
from .vacuum import HistoryVacuumMixin

__all__ = [
    "HistoryLifecycle",
]


class HistoryLifecycle(
    HistoryPreviewMixin,
    HistoryCleanupMixin,
    HistoryExportMixin,
    HistoryVacuumMixin,
    HistoryLifecycleBase,
):
    """History maintenance over the task databases.

    Each capability lives in its own mixin; the base owns the database
    locations, connections and schema checks they share.
    """
