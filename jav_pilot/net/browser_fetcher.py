from __future__ import annotations

import os
import threading
from tempfile import TemporaryDirectory
from types import TracebackType
from urllib.parse import urlsplit

from ..core.models import SearchBounds
from .network_guard import PublicHostResolver


BROWSER_SLOTS = threading.BoundedSemaphore(value=1)
BROWSER_QUEUE_TIMEOUT_SECONDS = 10.0
_LOCAL_RESOURCE_SCHEMES = frozenset({"about", "blob", "data"})


class BrowserFetchError(RuntimeError):
    pass


class BrowserWaitTimeout(BrowserFetchError):
    pass


class BrowserPageFetcher:
    """Small Playwright-backed fetcher for pages that require browser rendering."""

    def __init__(self, bounds: SearchBounds) -> None:
        self.bounds = bounds.normalized()
        self._playwright = None
        self._context = None
        self._page = None
        self._allowed_origin: tuple[str, str, int] | None = None
        self._host_resolver = PublicHostResolver(max_hosts=32)
        self._tmpdir: TemporaryDirectory[str] | None = None
        self._browser_slot_acquired = False
        self._playwright_timeout_error: type[BaseException] = TimeoutError

    def __enter__(self) -> "BrowserPageFetcher":
        wait_seconds = min(self.bounds.timeout_seconds, BROWSER_QUEUE_TIMEOUT_SECONDS)
        if not BROWSER_SLOTS.acquire(timeout=wait_seconds):
            raise BrowserFetchError("browser fetch capacity is busy")
        self._browser_slot_acquired = True
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
            self._release_browser_slot()
            raise BrowserFetchError("Playwright is not available; install playwright to use browser fetch") from exc

        try:
            self._playwright_timeout_error = PlaywrightTimeoutError
            self._playwright = sync_playwright().start()
            self._tmpdir = TemporaryDirectory()

            headless = os.environ.get("JAV_PILOT_BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"}
            channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
            kwargs = {
                "user_data_dir": self._tmpdir.name,
                "headless": headless,
                "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
                "viewport": {"width": 1280, "height": 900},
                "service_workers": "block",
            }
            proxy = os.environ.get("JAV_PILOT_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY")
            if proxy:
                kwargs["proxy"] = {"server": proxy}

            try:
                self._context = self._playwright.chromium.launch_persistent_context(channel=channel, **kwargs)
            except Exception:
                if not channel:
                    raise
                self._context = self._playwright.chromium.launch_persistent_context(**kwargs)

            self._context.route("**/*", self._route_request)
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if not callable(route_web_socket):
                raise BrowserFetchError("Playwright does not support WebSocket routing")
            route_web_socket("**/*", lambda web_socket: web_socket.close())
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception:
            try:
                self.__exit__(None, None, None)
            except Exception:
                pass
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                try:
                    if self._tmpdir:
                        self._tmpdir.cleanup()
                finally:
                    try:
                        self._context = None
                        self._playwright = None
                        self._tmpdir = None
                        self._page = None
                        self._allowed_origin = None
                        self._host_resolver = PublicHostResolver(max_hosts=32)
                    finally:
                        self._release_browser_slot()

    def _release_browser_slot(self) -> None:
        if not self._browser_slot_acquired:
            return
        self._browser_slot_acquired = False
        BROWSER_SLOTS.release()

    def fetch(self, url: str) -> str:
        if not self._page:
            raise BrowserFetchError("browser fetcher is not started")

        self._host_resolver = PublicHostResolver(max_hosts=32)
        self._allowed_origin = _http_origin(url)
        if not self._host_resolver.is_public(self._allowed_origin[1]):
            raise BrowserFetchError("browser host must resolve to a public address")
        timeout_ms = int(self.bounds.timeout_seconds * 1000)
        wait_ms = _wait_after_load_ms()
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if _http_origin(self._page.url) != self._allowed_origin:
                raise BrowserFetchError("cross-origin browser navigation was rejected")
            if wait_ms > 0:
                self._page.wait_for_timeout(wait_ms)
            html = self._page_html()
        except Exception as exc:  # noqa: BLE001 - Playwright wraps browser failures.
            raise BrowserFetchError(str(exc)) from exc

        return html

    def wait_for_selector(self, selector: str, *, timeout_seconds: float) -> str:
        if not self._page:
            raise BrowserFetchError("browser fetcher is not started")
        clean_selector = str(selector or "").strip()
        if not clean_selector:
            raise ValueError("selector is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        timeout_ms = int(min(float(timeout_seconds), self.bounds.timeout_seconds) * 1000)
        try:
            self._page.wait_for_selector(clean_selector, state="attached", timeout=timeout_ms)
            return self._page_html()
        except self._playwright_timeout_error as exc:
            raise BrowserWaitTimeout(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - Playwright wraps browser failures.
            raise BrowserFetchError(str(exc)) from exc

    def _page_html(self) -> str:
        if not self._page:
            raise BrowserFetchError("browser fetcher is not started")
        html = self._page.content()
        if len(html.encode("utf-8", errors="replace")) > self.bounds.max_response_bytes:
            raise BrowserFetchError(f"rendered response exceeded {self.bounds.max_response_bytes} bytes")
        return html

    def _route_request(self, route: object) -> None:
        _route_without_heavy_resources(
            route,
            allowed_origin=self._allowed_origin,
            host_resolver=self._host_resolver,
        )


def _wait_after_load_ms() -> int:
    raw = os.environ.get("JAV_PILOT_BROWSER_WAIT_MS", "3000")
    try:
        value = int(raw)
    except ValueError:
        value = 3000
    return max(0, min(value, 30000))


def _route_without_heavy_resources(
    route: object,
    *,
    allowed_origin: tuple[str, str, int] | None = None,
    host_resolver: PublicHostResolver | None = None,
) -> None:
    request = getattr(route, "request", None)
    resource_type = str(getattr(request, "resource_type", "") or "").lower()
    if resource_type in {"image", "media", "font"}:
        route.abort()
        return
    request_url = str(getattr(request, "url", "") or "")
    try:
        request_scheme = urlsplit(request_url).scheme.lower()
    except ValueError:
        request_scheme = ""
    if request_scheme in _LOCAL_RESOURCE_SCHEMES:
        route.continue_()
        return
    request_origin = _optional_http_origin(request_url)
    if request_origin is None:
        route.abort()
        return
    resolver = host_resolver or PublicHostResolver(max_hosts=32)
    if not resolver.is_public(request_origin[1]):
        route.abort()
        return
    is_navigation = getattr(request, "is_navigation_request", None)
    if callable(is_navigation) and is_navigation() and allowed_origin and request_origin != allowed_origin:
        route.abort()
        return
    route.continue_()


def _http_origin(url: str) -> tuple[str, str, int]:
    origin = _optional_http_origin(url)
    if origin is None:
        raise BrowserFetchError("browser URL must use HTTP or HTTPS")
    return origin


def _optional_http_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return scheme, parsed.hostname.rstrip(".").lower(), port or (443 if scheme == "https" else 80)
