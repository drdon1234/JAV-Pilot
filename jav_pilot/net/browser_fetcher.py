from __future__ import annotations

import os
import threading
import time
import hashlib
import json
from dataclasses import replace
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from urllib.parse import urlsplit

from ..core.models import SearchBounds
from ..config.paths import runtime_data_dir
from ..core.storage import atomic_write_text
from .challenges import (
    age_gate,
    is_challenge,
    is_firewall_block,
    try_age_confirmation,
    try_verification_checkbox,
    trusted_verification_origin,
)
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

    def __init__(
        self,
        bounds: SearchBounds,
        *,
        session_origin: str | None = None,
        allowed_origins: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        self.bounds = bounds.normalized()
        # Browser verification owns its caller's bounded budget, not the
        # shorter generic search limit. Keep all other normalized limits.
        if session_origin:
            self.bounds = replace(self.bounds, timeout_seconds=max(1.0, min(float(bounds.timeout_seconds), 120.0)))
        self._session_origin = session_origin
        self._trusted_origins = frozenset(_http_origin(url) for url in allowed_origins)
        self._headers = headers or {}
        self._deadline: float | None = None
        self._document_status = 0
        self._document_generation = 0
        self._cookie_path: Path | None = None
        self._playwright = None
        self._context = None
        self._page = None
        self._allowed_origin: tuple[str, str, int] | None = None
        self._host_resolver = PublicHostResolver(max_hosts=32)
        self._tmpdir: TemporaryDirectory[str] | None = None
        self._browser_slot_acquired = False
        self._profile_lock = None
        self._playwright_timeout_error: type[BaseException] = TimeoutError

    def __enter__(self) -> "BrowserPageFetcher":
        if self._session_origin:
            self._deadline = time.monotonic() + self.bounds.timeout_seconds
        wait_seconds = min(self.bounds.timeout_seconds, BROWSER_QUEUE_TIMEOUT_SECONDS)
        if not BROWSER_SLOTS.acquire(timeout=wait_seconds):
            raise BrowserFetchError("browser fetch capacity is busy")
        self._browser_slot_acquired = True
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
            self._release_browser_slot()
            raise BrowserFetchError(
                "Playwright is not available; install playwright to use browser fetch"
            ) from exc

        try:
            self._playwright_timeout_error = PlaywrightTimeoutError
            self._playwright = sync_playwright().start()
            proxy = (
                os.environ.get("JAV_PILOT_PROXY")
                or os.environ.get("HTTPS_PROXY")
                or os.environ.get("ALL_PROXY")
            )
            if self._session_origin:
                key = hashlib.sha256(
                    repr((_http_origin(self._session_origin), proxy)).encode()
                ).hexdigest()
                profile = runtime_data_dir() / "browser-sessions" / key
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._lock_profile(profile)
                profile_path = str(profile)
                self._cookie_path = profile / "site-cookies.json"
            else:
                self._tmpdir = TemporaryDirectory()
                profile_path = self._tmpdir.name

            headless = os.environ.get(
                "JAV_PILOT_BROWSER_HEADLESS", "0" if os.environ.get("DISPLAY") else "1"
            ).lower() not in {"0", "false", "no"}
            channel = (
                os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "chrome").strip() or None
            )
            kwargs = {
                "user_data_dir": profile_path,
                "headless": headless,
                "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
                "viewport": {"width": 1280, "height": 900},
                "service_workers": "block",
                "accept_downloads": False,
                "ignore_default_args": ["--enable-automation"],
                "args": ["--disable-blink-features=AutomationControlled"],
                "timeout": self._remaining_ms(),
            }
            if proxy:
                kwargs["proxy"] = {"server": proxy}

            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    channel=channel, **kwargs
                )
            except Exception:
                if not channel:
                    raise
                self._context = self._playwright.chromium.launch_persistent_context(
                    **kwargs
                )

            self._context.route("**/*", self._route_request)
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if not callable(route_web_socket):
                raise BrowserFetchError("Playwright does not support WebSocket routing")
            # A routed socket is mocked until connect_to_server is called.
            # Closing it synchronously inside this callback can deadlock the
            # Playwright dispatcher during a challenge navigation.
            route_web_socket("**/*", lambda web_socket: None)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
            self._page.on("response", self._record_document_response)
            if self._session_origin:
                if self._cookie_path and self._cookie_path.exists():
                    try:
                        saved = json.loads(
                            self._cookie_path.read_text(encoding="utf-8")
                        )
                        if isinstance(saved, list):
                            self._context.add_cookies(saved)
                    except (ValueError, OSError):
                        pass
                cookies = SimpleCookie()
                cookies.load(self._headers.get("Cookie", ""))
                self._context.add_cookies(
                    [
                        {"name": name, "value": item.value, "url": self._session_origin}
                        for name, item in cookies.items()
                    ]
                )
        except Exception as exc:
            try:
                self.__exit__(None, None, None)
            except Exception:
                pass
            if isinstance(exc, BrowserFetchError):
                raise
            raise BrowserFetchError("browser runtime could not start") from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._context:
                try:
                    if self._cookie_path:
                        atomic_write_text(
                            self._cookie_path, json.dumps(self._context.cookies())
                        )
                finally:
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
                        if self._profile_lock is not None:
                            self._profile_lock.close()
                            self._profile_lock = None
                        self._context = None
                        self._playwright = None
                        self._tmpdir = None
                        self._page = None
                        self._allowed_origin = None
                        self._host_resolver = PublicHostResolver(max_hosts=32)
                    finally:
                        self._release_browser_slot()

    def _lock_profile(self, profile: Path) -> None:
        # An OS lock survives neither a killed worker nor container recreation.
        # Hold it through browser shutdown before clearing Chromium's stale
        # hostname/PID symlinks, which otherwise outlive the owning container.
        handle = (profile / "session.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise BrowserFetchError("browser fetch capacity is busy") from exc
        self._profile_lock = handle
        if os.name != "nt":
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                target = profile / name
                if target.is_symlink():
                    target.unlink()

    def _release_browser_slot(self) -> None:
        if not self._browser_slot_acquired:
            return
        self._browser_slot_acquired = False
        BROWSER_SLOTS.release()

    def _remaining_ms(self) -> int:
        seconds = (
            (self._deadline - time.monotonic())
            if self._deadline
            else self.bounds.timeout_seconds
        )
        if seconds <= 0:
            raise BrowserFetchError("browser request timed out")
        return max(1, int(seconds * 1000))

    def _record_document_response(self, response: object) -> None:
        if (
            self._page
            and response.request.resource_type == "document"
            and response.frame == self._page.main_frame
        ):
            self._document_status = response.status
            self._document_generation += 1

    def fetch(self, url: str) -> str:
        if not self._page:
            raise BrowserFetchError("browser fetcher is not started")

        self._host_resolver = PublicHostResolver(max_hosts=32)
        self._allowed_origin = _http_origin(url)
        if not self._host_resolver.is_public(self._allowed_origin[1]):
            raise BrowserFetchError("browser host must resolve to a public address")
        timeout_ms = self._remaining_ms()
        deadline = self._deadline or time.monotonic() + self.bounds.timeout_seconds
        self._document_status = 0
        wait_ms = _wait_after_load_ms()
        try:
            response = self._page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            if not self._navigation_allowed(self._page.url):
                raise BrowserFetchError("cross-origin browser navigation was rejected")
            if wait_ms > 0 and not self._session_origin:
                self._page.wait_for_timeout(
                    min(wait_ms, max(1, int((deadline - time.monotonic()) * 1000)))
                )
            html = self._read_html(deadline)
            clicked = False
            age_clicked = False
            started = time.monotonic()
            generation = self._document_generation
            last_scan = started
            stable_since = None
            while self._session_origin:
                now = time.monotonic()
                if generation != self._document_generation:
                    generation = self._document_generation
                    clicked = age_clicked = False
                    started = last_scan = now
                    stable_since = None
                if is_firewall_block(html):
                    raise BrowserFetchError(
                        "HTTP 403: source firewall blocked this network"
                    )
                gate = age_gate(html, self._page.url)
                pending = is_challenge(html) or gate is not None
                if pending:
                    stable_since = None
                    if gate and not age_clicked:
                        age_clicked = try_age_confirmation(self._page, gate, deadline)
                    if not clicked and now - started >= 5 and now - last_scan >= 3:
                        last_scan = now
                        clicked = try_verification_checkbox(self._page, deadline)
                else:
                    stable_since = stable_since or now
                    if now - stable_since >= 0.5:
                        break
                if now >= deadline:
                    raise BrowserFetchError(
                        "challenge verification timed out"
                        if pending
                        else "browser page timed out"
                    )
                self._page.wait_for_timeout(
                    min(250, max(1, int((deadline - now) * 1000)))
                )
                html = self._read_html(deadline)
            status = self._document_status or (
                response.status if response is not None else 0
            )
            if status >= 400:
                raise BrowserFetchError(f"HTTP {status}")
        except Exception as exc:  # noqa: BLE001 - Playwright wraps browser failures.
            raise BrowserFetchError(str(exc)) from exc

        return html

    def _navigation_allowed(self, url: str) -> bool:
        origin = _http_origin(url)
        return origin == self._allowed_origin or origin in self._trusted_origins

    def _read_html(self, deadline: float) -> str:
        while True:
            try:
                return self._page_html()
            except Exception as exc:
                message = str(exc).lower()
                if (
                    "navigating" not in message
                    and "execution context was destroyed" not in message
                ):
                    raise
                if time.monotonic() >= deadline:
                    raise BrowserFetchError("browser page timed out") from exc
                self._page.wait_for_timeout(
                    min(100, max(1, int((deadline - time.monotonic()) * 1000)))
                )

    def wait_for_selector(self, selector: str, *, timeout_seconds: float) -> str:
        if not self._page:
            raise BrowserFetchError("browser fetcher is not started")
        clean_selector = str(selector or "").strip()
        if not clean_selector:
            raise ValueError("selector is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        timeout_ms = int(
            min(float(timeout_seconds), self.bounds.timeout_seconds) * 1000
        )
        try:
            self._page.wait_for_selector(
                clean_selector, state="attached", timeout=timeout_ms
            )
            return self._page_html()
        except self._playwright_timeout_error as exc:
            raise BrowserWaitTimeout(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - Playwright wraps browser failures.
            raise BrowserFetchError(str(exc)) from exc

    def _page_html(self) -> str:
        if not self._page:
            raise BrowserFetchError("browser fetcher is not started")
        if not self._navigation_allowed(self._page.url):
            raise BrowserFetchError("cross-origin browser navigation was rejected")
        html = self._page.content()
        if len(html.encode("utf-8", errors="replace")) > self.bounds.max_response_bytes:
            raise BrowserFetchError(
                f"rendered response exceeded {self.bounds.max_response_bytes} bytes"
            )
        return html

    def _route_request(self, route: object) -> None:
        _route_without_heavy_resources(
            route,
            allowed_origin=self._allowed_origin,
            host_resolver=self._host_resolver,
            trusted_origins=self._trusted_origins,
            allow_rendering_assets=bool(self._session_origin),
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
    trusted_origins: frozenset[tuple[str, str, int]] = frozenset(),
    allow_rendering_assets: bool = False,
) -> None:
    request = getattr(route, "request", None)
    resource_type = str(getattr(request, "resource_type", "") or "").lower()
    if resource_type == "media" or (
        not allow_rendering_assets and resource_type in {"image", "font"}
    ):
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
    if (
        callable(is_navigation)
        and is_navigation()
        and allowed_origin
        and request_origin != allowed_origin
        and request_origin not in trusted_origins
    ):
        frame = getattr(request, "frame", None)
        if not (
            trusted_verification_origin(*request_origin)
            and frame is not None
            and getattr(frame, "parent_frame", None) is not None
            and urlsplit(request_url).path.startswith(
                ("/turnstile/", "/cdn-cgi/challenge-platform/")
            )
        ):
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
    return (
        scheme,
        parsed.hostname.rstrip(".").lower(),
        port or (443 if scheme == "https" else 80),
    )
