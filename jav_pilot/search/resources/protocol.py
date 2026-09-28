"""Errors reported by resource search workers."""

from __future__ import annotations

from .errors import ResourceSearchError
from .models import ResourceSearchState
from .validation import validate_error_code

__all__ = [
    "ResourceSearchWorkerError",
]


class ResourceSearchWorkerError(ResourceSearchError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        state: ResourceSearchState | None = None,
    ) -> None:
        super().__init__("resource search worker failed")
        self.code = validate_error_code(code, retryable=retryable)
        self.retryable = retryable
        self.state = state

    def attach_state(self, state: ResourceSearchState | None) -> None:
        """Keep the last validated stream cursor with a worker failure."""

        if self.state is None and state is not None:
            self.state = state
