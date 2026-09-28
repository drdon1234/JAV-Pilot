"""Process-wide runtime state shared by the HTTP API: service singletons, locks and caches."""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path

from ..core.cache import TtlCache
from ..core.models import WorkResult
from ..downloads.replacements import DownloadReplacementStore
from ..downloads.resource_recovery import DownloadResourceRecoveryManager
from ..history.lifecycle import HistoryLifecycle
from ..history.retention import HistoryRetentionScheduler
from ..library.worker import MediaLibraryManager
from ..media_metadata.manager import MediaMetadataManager
from ..media_metadata.review.images import ReviewImage
from ..media_metadata.review.manager import MediaMetadataReviewManager
from ..notifications.dispatcher import NotificationDispatcher
from ..notifications.outbox import SQLiteNotificationOutbox
from ..search.detail_prefetch import DetailPrefetchManager
from ..search.history import SearchHistoryStore
from ..search.resources.manager import ResourceSearchManager
from ..search.session_store import SQLiteMetadataSearchStore
from ..security.login_rate_limit import LoginRateLimiter
from ..sites.diagnostic_store import SQLiteSiteDiagnosticStore
from ..sites.diagnostic_worker import SiteDiagnosticScheduler
from ..torrent.magnet_probe import MagnetProbeManager, MagnetProbeService
from ..torrent.magnet_selection import MagnetSelectionManager, MagnetSelectionService
from ..translation.ai import AiTranslationService
from ..translation.machine import TranslationService
from ..web_download.batches.manager import WebDownloadBatchManager
from ..web_download.manager import WebDownloadManager
from .registries import (
    DownloadDispositionSnapshotRegistry,
    MetadataSearchContinuationOverlay,
    SearchContinuationRegistry,
    SearchJobRegistry,
)

SEARCH_CACHE = TtlCache[dict[str, object]](
    max_items=96,
    ttl_seconds=900,
    max_weight=16 * 1024 * 1024,
    weigher=lambda payload: len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ),
)


SEARCH_CONTINUATIONS = SearchContinuationRegistry(
    max_items=12,
    ttl_seconds=900,
)


METADATA_SEARCH_CONTINUATIONS = MetadataSearchContinuationOverlay(
    max_items=12,
    ttl_seconds=900,
)
WORK_CACHE = TtlCache[WorkResult](
    max_items=128,
    ttl_seconds=900,
    max_weight=8 * 1024 * 1024,
    weigher=lambda work: len(
        json.dumps(work.to_dict(), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ),
)
TORRENT_HISTORY_CACHE = TtlCache[tuple[dict[str, object], ...]](
    max_items=8,
    ttl_seconds=10,
    max_weight=32 * 1024 * 1024,
    weigher=lambda tasks: len(
        json.dumps(tasks, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ),
)
TORRENT_HISTORY_CACHE_LOCK = threading.RLock()
TORRENT_HISTORY_CACHE_CREDENTIAL_SALT = secrets.token_bytes(32)
SEARCH_SLOTS = threading.BoundedSemaphore(value=3)
DETAIL_SEARCH_SLOTS = threading.BoundedSemaphore(value=2)
SEARCH_TOTAL_SLOTS = threading.BoundedSemaphore(value=4)
# Gallery pages can schedule a burst of lazy-image requests when users open
# several details in quick succession. Keep the request queue large enough to
# absorb that burst while the cover proxy's per-origin limiter bounds upstream
# concurrency.
COVER_SLOTS = threading.BoundedSemaphore(value=8)
COVER_REQUEST_SLOTS = threading.BoundedSemaphore(value=96)


SEARCH_JOBS = SearchJobRegistry()
MAGNET_PROBES = MagnetProbeManager(MagnetProbeService())
MAGNET_SELECTIONS = MagnetSelectionManager(
    MagnetSelectionService(),
    reserve_hashes=MAGNET_PROBES.reserve_external_hashes,
    release_hashes=MAGNET_PROBES.release_external_hashes,
)
WEB_DOWNLOADS_LOCK = threading.RLock()
OPERATIONAL_MANAGER_LOCK = threading.RLock()
WEB_DOWNLOADS: WebDownloadManager | None = None
WEB_DOWNLOADS_PENDING_SHUTDOWN: list[WebDownloadManager] = []
WEB_DOWNLOAD_BATCHES_LOCK = threading.RLock()
WEB_DOWNLOAD_BATCHES: WebDownloadBatchManager | None = None
DOWNLOAD_REPLACEMENTS_LOCK = threading.RLock()
DOWNLOAD_REPLACEMENTS: DownloadReplacementStore | None = None
WEB_DOWNLOAD_BULK_RETRY_LOCK = threading.Lock()
DOWNLOAD_RESOURCE_RECOVERY_LOCK = threading.RLock()
DOWNLOAD_RESOURCE_RECOVERY: DownloadResourceRecoveryManager | None = None
DOWNLOAD_REPLACEMENT_CLEANUP_LOCK = threading.Lock()
DOWNLOAD_DISPOSITION_SNAPSHOTS = DownloadDispositionSnapshotRegistry()
RESOURCE_SEARCHES_LOCK = threading.RLock()
RESOURCE_SEARCHES: ResourceSearchManager | None = None
MEDIA_METADATA_LOCK = threading.RLock()
MEDIA_METADATA: MediaMetadataManager | None = None
MEDIA_METADATA_REVIEW_LOCK = threading.RLock()
MEDIA_METADATA_REVIEW: MediaMetadataReviewManager | None = None
MEDIA_LIBRARY_LOCK = threading.RLock()
MEDIA_LIBRARY: MediaLibraryManager | None = None
DETAIL_PREFETCH_LOCK = threading.RLock()
DETAIL_PREFETCH: DetailPrefetchManager | None = None
HISTORY_LIFECYCLE_LOCK = threading.RLock()
HISTORY_LIFECYCLE: HistoryLifecycle | None = None
HISTORY_RETENTION_SCHEDULER_LOCK = threading.RLock()
HISTORY_RETENTION_SCHEDULER: HistoryRetentionScheduler | None = None
HISTORY_RETENTION_STARTUP_ERROR_CODE: str | None = None
SITE_DIAGNOSTICS_LOCK = threading.RLock()
SITE_DIAGNOSTICS: SQLiteSiteDiagnosticStore | None = None
SITE_DIAGNOSTIC_SCHEDULER: SiteDiagnosticScheduler | None = None
NOTIFICATION_CONFIG_LOCK = threading.RLock()
NOTIFICATION_RUNTIME_LOCK = threading.RLock()
NOTIFICATION_RUNTIME_CONDITION = threading.Condition(NOTIFICATION_RUNTIME_LOCK)
NOTIFICATION_OUTBOXES: tuple[SQLiteNotificationOutbox, ...] = ()
NOTIFICATION_DISPATCHERS: tuple[NotificationDispatcher, ...] = ()
NOTIFICATION_RUNTIME_GENERATION = 0
NOTIFICATION_RUNTIME_ACCEPTING = False
NOTIFICATION_RUNTIME_INFLIGHT: dict[int, int] = {}
NOTIFICATION_DISPATCHER_WORKER: threading.Thread | None = None
NOTIFICATION_STOP = threading.Event()
NOTIFICATION_WAKE = threading.Event()
NOTIFICATION_REGISTRATION_LOCK = threading.RLock()
NOTIFICATION_PENDING_OUTBOXES: dict[Path, str] = {}
NOTIFICATION_REGISTRATION_RETRY_AT = 0.0
NOTIFICATION_REGISTRATION_RETRY_DELAY_SECONDS = 2.0
COMPLETED_ORGANIZER_WORKER: threading.Thread | None = None
SERVER_STOPPING = threading.Event()
LOGIN_RATE_LIMITER = LoginRateLimiter()
LOGIN_VERIFICATION_SLOTS = threading.BoundedSemaphore(4)


REVIEW_IMAGE_CACHE = TtlCache[ReviewImage](
    max_items=32,
    ttl_seconds=900,
    max_weight=64 * 1024 * 1024,
    weigher=lambda image: len(image.body),
)
HISTORY_BACKUP_STATUS_CACHE = TtlCache[dict[str, object]](
    max_items=4,
    ttl_seconds=60,
)
METRICS_TEXT_CACHE = TtlCache[str](max_items=1, ttl_seconds=5)
METRICS_RENDER_LOCK = threading.Lock()


METADATA_SEARCH_STORE_LOCK = threading.Lock()
METADATA_SEARCH_STORE: SQLiteMetadataSearchStore | None = None
METADATA_SEARCH_STORE_PATH: Path | None = None


METADATA_SEARCH_CONTINUATION_RESTORE_LOCK = threading.Lock()


TRANSLATION_SERVICE: TranslationService | None = None
AI_TRANSLATION_SERVICE: AiTranslationService | None = None
SEARCH_HISTORY_STORE: SearchHistoryStore | None = None
LAZY_SERVICES_LOCK = threading.Lock()
