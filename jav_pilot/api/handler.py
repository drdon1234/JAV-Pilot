"""The request handler that dispatches HTTP requests to route groups."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import urlsplit

from .request import request_peer_host, same_origin_request
from .routes.assets import AssetRoutes
from .routes.auth import AuthRoutes
from .routes.downloads import DownloadRoutes
from .routes.history import HistoryRoutes
from .routes.library import MediaLibraryRoutes
from .routes.media_metadata import MediaMetadataRoutes
from .routes.metadata_search import MetadataSearchRoutes
from .routes.notifications import NotificationRoutes
from .routes.replacements import DownloadReplacementRoutes
from .routes.resource_search import ResourceSearchRoutes
from .routes.search import SearchRoutes
from .routes.search_history import SearchHistoryRoutes
from .routes.settings import SettingsRoutes
from .routes.sites import SiteDiagnosticRoutes
from .routes.system import SystemRoutes
from .routes.translation import TranslationRoutes
from .routes.web_download_batches import WebDownloadBatchRoutes
from .routes.web_downloads import WebDownloadRoutes
from .routing import GET_PATTERN_ROUTES, GET_ROUTES, POST_ROUTES, Route, all_routes
from .static_files import is_app_route


class JavPilotHandler(
    AssetRoutes,
    AuthRoutes,
    DownloadReplacementRoutes,
    DownloadRoutes,
    HistoryRoutes,
    MediaLibraryRoutes,
    MediaMetadataRoutes,
    MetadataSearchRoutes,
    NotificationRoutes,
    ResourceSearchRoutes,
    SearchHistoryRoutes,
    SearchRoutes,
    SettingsRoutes,
    SiteDiagnosticRoutes,
    SystemRoutes,
    TranslationRoutes,
    WebDownloadBatchRoutes,
    WebDownloadRoutes,
):
    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/assets/"):
            self._handle_static_asset(parsed.path)
            return
        route = GET_ROUTES.get(parsed.path)
        if route is not None and route.public:
            self._dispatch(route, parsed.query)
            return
        if not self._authenticated():
            self._send_unauthorized(parsed.path)
            return
        if route is None and is_app_route(parsed.path):
            self._handle_frontend()
            return
        if route is not None:
            self._dispatch(route, parsed.query)
            return
        for pattern_route in GET_PATTERN_ROUTES:
            match = pattern_route.pattern.fullmatch(parsed.path)
            if match is not None:
                self._dispatch(pattern_route.route, parsed.query, **match.groupdict())
                return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlsplit(self.path)
        if not same_origin_request(
            self.headers,
            peer_host=request_peer_host(self),
        ):
            self._send_json(
                {"ok": False, "error": "cross-site request rejected"},
                HTTPStatus.FORBIDDEN,
            )
            return
        route = POST_ROUTES.get(parsed.path)
        if route is not None and route.public:
            self._dispatch(route, parsed.query)
            return
        if not self._authenticated():
            self._send_unauthorized(parsed.path)
            return
        if route is not None:
            self._dispatch(route, parsed.query)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _dispatch(self, route: Route, query: str, **params: str) -> None:
        handler = getattr(self, route.handler)
        if route.with_query:
            handler(query, **params)
        else:
            handler(**params)


_missing = sorted(
    {route.handler for route in all_routes()}
    - {name for name in dir(JavPilotHandler) if name.startswith("_handle_")}
)
if _missing:
    raise RuntimeError(f"routes reference unknown handlers: {', '.join(_missing)}")
