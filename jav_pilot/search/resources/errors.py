"""Resource search error types."""

from __future__ import annotations

__all__ = [
    "ResourceSearchCancelledError",
    "ResourceSearchConflictError",
    "ResourceSearchError",
    "ResourceSearchNotFoundError",
    "ResourceSearchUnavailableError",
]


class ResourceSearchError(RuntimeError):
    pass


class ResourceSearchNotFoundError(ResourceSearchError):
    pass


class ResourceSearchConflictError(ResourceSearchError):
    pass


class ResourceSearchUnavailableError(ResourceSearchError):
    pass


class ResourceSearchCancelledError(ResourceSearchError):
    pass
