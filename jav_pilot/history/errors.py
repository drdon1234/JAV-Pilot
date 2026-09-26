"""History lifecycle error types."""

from __future__ import annotations

__all__ = [
    "HistoryLifecycleConflictError",
    "HistoryLifecycleError",
    "HistoryLifecycleValidationError",
]


class HistoryLifecycleError(RuntimeError):
    pass


class HistoryLifecycleValidationError(HistoryLifecycleError):
    pass


class HistoryLifecycleConflictError(HistoryLifecycleError):
    pass


class CoordinatedApplyError(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        *,
        rollback_confirmed: bool,
        commit_attempted: bool,
    ) -> None:
        super().__init__("coordinated history cleanup failed")
        self.cause = cause
        self.rollback_confirmed = rollback_confirmed
        self.commit_attempted = commit_attempted
