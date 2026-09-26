"""Route table: maps request paths to the handler methods of the route groups."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Route:
    """A handler method name plus how the dispatcher should call it."""

    handler: str
    with_query: bool = False
    public: bool = False


@dataclass(frozen=True, slots=True)
class PatternRoute:
    """A route whose path carries parameters; named groups become keyword arguments."""

    pattern: re.Pattern[str]
    route: Route


GET_ROUTES: dict[str, Route] = {
    "/healthz": Route("_handle_healthz", public=True),
    "/readyz": Route("_handle_readyz", public=True),
    "/api/auth/status": Route("_handle_auth_status", public=True),
    "/login": Route("_handle_login_page", public=True),
    "/metrics": Route("_handle_metrics"),
    "/": Route("_handle_root"),
    "/api/covers": Route("_handle_cover", with_query=True),
    "/api/search/sessions": Route("_handle_metadata_search_session", with_query=True),
    "/api/search/sessions/stream": Route(
        "_handle_metadata_search_session_stream", with_query=True
    ),
    "/api/works": Route("_handle_work", with_query=True),
    "/api/detail-prefetch/batches": Route(
        "_handle_detail_prefetch_batches", with_query=True
    ),
    "/api/settings": Route("_handle_settings"),
    "/api/ai-translation": Route("_handle_ai_translation_config"),
    "/api/search-history": Route("_handle_search_history", with_query=True),
    "/api/rankings": Route("_handle_rankings", with_query=True),
    "/api/site-diagnostics": Route("_handle_site_diagnostics", with_query=True),
    "/api/notifications": Route("_handle_notifications", with_query=True),
    "/api/history/status": Route("_handle_history_status"),
    "/api/config": Route("_handle_config"),
    "/api/health": Route("_handle_config"),
    "/api/downloader/status": Route("_handle_downloader_status"),
    "/api/downloads": Route("_handle_downloads", with_query=True),
    "/api/download-replacements": Route(
        "_handle_download_replacement_get", with_query=True
    ),
    "/api/failed-download-archive": Route(
        "_handle_failed_download_archive_get", with_query=True
    ),
    "/api/web-downloads": Route("_handle_web_downloads", with_query=True),
    "/api/resource-searches": Route("_handle_resource_search", with_query=True),
    "/api/web-downloads/control": Route("_handle_web_download_control_get"),
    "/api/web-downloads/batches/chains": Route(
        "_handle_web_download_batch_chains", with_query=True
    ),
    "/api/web-downloads/batches/chains/export": Route(
        "_handle_web_download_batch_chain_export", with_query=True
    ),
    "/api/web-downloads/batches/rules": Route(
        "_handle_web_download_batch_rules", with_query=True
    ),
    "/api/web-downloads/batches": Route("_handle_web_download_batch", with_query=True),
    "/api/media-metadata": Route("_handle_media_metadata", with_query=True),
    "/api/media-metadata/review": Route(
        "_handle_media_metadata_review", with_query=True
    ),
    "/api/library": Route("_handle_media_library", with_query=True),
    "/api/magnets/probe": Route("_handle_magnet_probe_get", with_query=True),
    "/api/magnets/select": Route("_handle_magnet_selection_get", with_query=True),
}

GET_PATTERN_ROUTES: tuple[PatternRoute, ...] = (
    PatternRoute(
        re.compile(r"/api/detail-prefetch/batches/(?P<batch_id>[0-9a-fA-F]{32})"),
        Route("_handle_detail_prefetch_batches", with_query=True),
    ),
)

POST_ROUTES: dict[str, Route] = {
    "/api/auth/login": Route("_handle_auth_login", public=True),
    "/api/auth/logout": Route("_handle_auth_logout", public=True),
    "/api/auth/password": Route("_handle_auth_password", public=True),
    "/api/client-events": Route("_handle_client_event"),
    "/api/magnets/probe": Route("_handle_magnet_probe_start"),
    "/api/magnets/select": Route("_handle_magnet_selection_start"),
    "/api/download": Route("_handle_download"),
    "/api/download-replacements": Route("_handle_download_replacement_create"),
    "/api/download-replacements/smart-selection": Route(
        "_handle_download_replacement_smart_selection_start"
    ),
    "/api/download-replacements/cleanup-no-sources": Route(
        "_handle_download_replacement_cleanup_no_sources"
    ),
    "/api/download-replacements/disposition-preview": Route(
        "_handle_download_replacement_disposition_preview"
    ),
    "/api/download-replacements/dispose": Route("_handle_download_replacement_dispose"),
    "/api/failed-download-archive/action": Route(
        "_handle_failed_download_archive_action"
    ),
    "/api/translate": Route("_handle_translate"),
    "/api/ai-translate": Route("_handle_ai_translate"),
    "/api/ai-translation/config": Route("_handle_ai_translation_config_save"),
    "/api/ai-translation/test": Route("_handle_ai_translation_test"),
    "/api/search-history/action": Route("_handle_search_history_action"),
    "/api/downloads/history-lookup": Route("_handle_download_history_lookup"),
    "/api/downloads/import/preview": Route("_handle_download_import_preview"),
    "/api/downloads/import/inspect": Route("_handle_download_import_inspect"),
    "/api/downloads/import": Route("_handle_download_import"),
    "/api/downloads/action": Route("_handle_download_action"),
    "/api/web-downloads": Route("_handle_web_download_start"),
    "/api/resource-searches": Route("_handle_resource_search_create"),
    "/api/resource-searches/action": Route("_handle_resource_search_action"),
    "/api/resource-searches/downloads": Route("_handle_resource_search_downloads"),
    "/api/web-downloads/action": Route("_handle_web_download_action"),
    "/api/web-downloads/retry-failed": Route("_handle_web_download_retry_failed"),
    "/api/web-downloads/control": Route("_handle_web_download_control_update"),
    "/api/web-downloads/queue": Route("_handle_web_download_queue_update"),
    "/api/web-downloads/cleanup-missing": Route(
        "_handle_web_download_cleanup_missing"
    ),
    "/api/web-downloads/batches/action": Route("_handle_web_download_batch_action"),
    "/api/web-downloads/batches/chains/action": Route(
        "_handle_web_download_batch_chain_action"
    ),
    "/api/web-downloads/batches/rules": Route("_handle_web_download_batch_rule_save"),
    "/api/web-downloads/batches/rules/action": Route(
        "_handle_web_download_batch_rule_action"
    ),
    "/api/media-metadata/scan": Route("_handle_media_metadata_scan"),
    "/api/media-metadata/migrate-titles": Route(
        "_handle_media_metadata_migrate_titles"
    ),
    "/api/media-metadata/action": Route("_handle_media_metadata_action"),
    "/api/media-metadata/review/open": Route("_handle_media_metadata_review_open"),
    "/api/media-metadata/review/draft": Route("_handle_media_metadata_review_draft"),
    "/api/media-metadata/review/abandon": Route(
        "_handle_media_metadata_review_abandon"
    ),
    "/api/media-metadata/review/refetch": Route(
        "_handle_media_metadata_review_refetch"
    ),
    "/api/media-metadata/review/image": Route("_handle_media_metadata_review_image"),
    "/api/media-metadata/review/preview": Route(
        "_handle_media_metadata_review_preview"
    ),
    "/api/media-metadata/review/publish": Route(
        "_handle_media_metadata_review_publish"
    ),
    "/api/library/action": Route("_handle_media_library_action"),
    "/api/search/cancel": Route("_handle_search_cancel"),
    "/api/search/sessions": Route("_handle_metadata_search_session_create"),
    "/api/detail-prefetch/batches": Route("_handle_detail_prefetch_batch_create"),
    "/api/detail-prefetch/batches/action": Route(
        "_handle_detail_prefetch_batch_action"
    ),
    "/api/search/sessions/action": Route("_handle_metadata_search_session_action"),
    "/api/organizer/preview": Route("_handle_organizer_preview"),
    "/api/settings": Route("_handle_save_settings"),
    "/api/settings/validate": Route("_handle_validate_settings"),
    "/api/site-diagnostics/probe": Route("_handle_site_diagnostic_probe"),
    "/api/notifications/config": Route("_handle_notification_config"),
    "/api/notifications/test": Route("_handle_notification_test"),
    "/api/notifications/retry": Route("_handle_notification_retry"),
    "/api/history/preview": Route("_handle_history_preview"),
    "/api/history/execute": Route("_handle_history_execute"),
    "/api/history/export": Route("_handle_history_export"),
    "/api/history/retention": Route("_handle_history_retention"),
    "/api/history/vacuum": Route("_handle_history_vacuum"),
    "/api/config/qb": Route("_handle_save_qb_config"),
}


def all_routes() -> tuple[Route, ...]:
    return (
        *GET_ROUTES.values(),
        *(pattern.route for pattern in GET_PATTERN_ROUTES),
        *POST_ROUTES.values(),
    )
