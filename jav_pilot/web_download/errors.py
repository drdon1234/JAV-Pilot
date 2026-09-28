"""Web download error types."""

from __future__ import annotations

class WebDownloadError(RuntimeError):
    pass


class WebDownloadConfigError(WebDownloadError):
    pass


class WebDownloadNotFoundError(WebDownloadError):
    pass


class WebDownloadConflictError(WebDownloadError):
    pass


class WebDownloadDisabledError(WebDownloadError):
    pass


class WebDownloadArchiveUnavailableError(WebDownloadError):
    pass


class WebDownloadRunnerError(WebDownloadError):
    pass


class WebDownloadDiskLowError(WebDownloadRunnerError):
    def __init__(self, volume_key: str) -> None:
        super().__init__("download storage does not have enough free space")
        self.volume_key = volume_key
