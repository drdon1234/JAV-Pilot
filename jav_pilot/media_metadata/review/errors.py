"""Media metadata review error types."""

from __future__ import annotations

class MediaMetadataReviewError(RuntimeError):
    pass


class MediaMetadataReviewNotFound(MediaMetadataReviewError):
    pass


class MediaMetadataReviewConflict(MediaMetadataReviewError):
    pass


class MediaMetadataReviewValidationError(MediaMetadataReviewError):
    pass
