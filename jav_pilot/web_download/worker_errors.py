"""Error types raised inside the web download worker process."""

from __future__ import annotations

class WebDownloadWorkerError(RuntimeError):
    pass


class WebDownloadWorkerDiskLowError(WebDownloadWorkerError):
    def __init__(self, volume_key: str) -> None:
        if volume_key not in {"staging", "library"}:
            raise ValueError("disk volume key is invalid")
        self.volume_key = volume_key
        super().__init__("download storage does not have enough free space")


class WebDownloadWorkerCancelled(WebDownloadWorkerError):
    pass


class WebDownloadWorkerTransientTransportError(WebDownloadWorkerError):
    def __init__(
        self,
        checkpoint_bytes: int | None = None,
        checkpoint_fragments: int | None = None,
        *,
        checkpoint_reset: bool = False,
    ) -> None:
        if (checkpoint_bytes is None) != (checkpoint_fragments is None):
            raise ValueError("transient checkpoint progress is incomplete")
        if checkpoint_reset and checkpoint_bytes is None:
            raise ValueError("transient checkpoint reset has no progress")
        self.checkpoint_bytes = (
            None if checkpoint_bytes is None else max(0, int(checkpoint_bytes))
        )
        self.checkpoint_fragments = (
            None if checkpoint_fragments is None else max(0, int(checkpoint_fragments))
        )
        self.checkpoint_reset = bool(checkpoint_reset)
        super().__init__("media transport was interrupted")


class MediaSegmentContentLengthMismatch(WebDownloadWorkerError):
    pass


class HlsDurationMismatch(WebDownloadWorkerError):
    pass
