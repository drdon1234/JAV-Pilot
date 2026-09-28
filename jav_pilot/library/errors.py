"""Media library error types."""

from __future__ import annotations

__all__ = [
    "MediaLibraryConflictError",
    "MediaLibraryError",
    "MediaLibraryRootChangedError",
    "MediaLibraryUnavailableError",
]


class MediaLibraryError(RuntimeError):
    pass


class MediaLibraryUnavailableError(MediaLibraryError):
    pass


class MediaLibraryRootChangedError(MediaLibraryUnavailableError):
    pass


class MediaLibraryConflictError(MediaLibraryError):
    pass


class MediaLibraryCapacityError(MediaLibraryError):
    pass
