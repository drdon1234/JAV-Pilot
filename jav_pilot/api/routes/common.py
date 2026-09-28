"""Error responses shared by the download-related route groups."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...downloads.replacements import (
    DownloadReplacementConflictError,
    DownloadReplacementNotFoundError,
)
from ...torrent.qbittorrent import DownloaderError
from ...web_download.batches.errors import (
    WebDownloadBatchConflictError,
    WebDownloadBatchNotFoundError,
    WebDownloadBatchUnavailableError,
)
from ...web_download.errors import (
    WebDownloadArchiveUnavailableError,
    WebDownloadConfigError,
    WebDownloadConflictError,
    WebDownloadDisabledError,
    WebDownloadError,
    WebDownloadNotFoundError,
)
from ..base import BaseHandler


class CommonErrorResponses(BaseHandler):
    def _send_download_replacement_error(self, error: Exception) -> None:
        if isinstance(error, DownloadReplacementNotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(error, DownloadReplacementConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(error, (OSError, sqlite3.Error)):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(error, DownloaderError):
            status = HTTPStatus.BAD_GATEWAY
        elif isinstance(error, WebDownloadError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_json({"ok": False, "error": str(error)}, status)

    def _send_web_download_error(self, error: WebDownloadError) -> None:
        if isinstance(error, (WebDownloadNotFoundError, WebDownloadBatchNotFoundError)):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(
            error, (WebDownloadConflictError, WebDownloadBatchConflictError)
        ):
            status = HTTPStatus.CONFLICT
        elif isinstance(
            error,
            (
                WebDownloadDisabledError,
                WebDownloadConfigError,
                WebDownloadBatchUnavailableError,
            ),
        ):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(error, WebDownloadArchiveUnavailableError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_json({"ok": False, "error": str(error)}, status)

    def _send_web_download_unavailable(self) -> None:
        self._send_json(
            {"ok": False, "error": "Web download storage is unavailable"},
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
