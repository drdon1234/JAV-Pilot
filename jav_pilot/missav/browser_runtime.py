"""The single trusted MissAV browser used by production workers.

This module deliberately keeps Playwright and transport credentials behind an
in-memory broker boundary.  Callers receive only a short-lived ManifestRequest
object; the runtime never serializes it, logs it, or stores it in SQLite.
"""

from __future__ import annotations

import math
import os
import stat
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Mapping

from .browser_service import (
    BrowserOperation,
    BrowserOperationError,
    BrowserOperationKind,
    BrowserDriverCrashed,
    BrowserOperationCancelled,
    BrowserServiceClosed,
    BrowserServiceError,
    BrowserUpstreamError,
    BrowserChallengeActive,
    BrowserSessionCounts,
    MissavBrowserService,
    FileBrowserOwnerLease,
)
from ..config.settings import (
    DESCRIPTION_CAPABILITY,
    RESOURCE_SEARCH_CAPABILITY,
    WEB_DOWNLOAD_CAPABILITY,
)
from ..web_download.quality import WebDownloadVariantOption
from ..web_download.variant import WEB_DOWNLOAD_VARIANTS


# Browser operations carry their own upstream deadline, but they may spend
# time waiting behind the single trusted page.  Every caller needs a finite
# wait as well; otherwise one challenged operation can leave a download or
# batch in ``discovering`` forever.  Keep the grace bounded and configurable
# without allowing an unbounded value from the environment.
_BROWSER_TASK_QUEUE_GRACE_DEFAULT_SECONDS = 120.0
_BROWSER_TASK_QUEUE_GRACE_MAX_SECONDS = 600.0
def _browser_task_queue_grace_seconds() -> float:
    raw = os.environ.get(
        "JAV_PILOT_MISSAV_BROWSER_QUEUE_TIMEOUT_SECONDS",
        str(_BROWSER_TASK_QUEUE_GRACE_DEFAULT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        value = _BROWSER_TASK_QUEUE_GRACE_DEFAULT_SECONDS
    if value != value or value == float("inf"):
        value = _BROWSER_TASK_QUEUE_GRACE_DEFAULT_SECONDS
    return max(0.0, min(_BROWSER_TASK_QUEUE_GRACE_MAX_SECONDS, value))


def wait_for_browser_task(
    task: object,
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    timeout_message: str = "MissAV browser operation timed out",
    timeout_code: str = "navigation_timeout",
) -> object:
    """Wait for a broker task with a bounded queue-and-operation deadline.

    The broker owns the browser thread, so cancellation must go through the
    task object rather than killing Chromium from the caller.  The helper is
    intentionally shared by downloads and batch discovery so neither path
    can strand a durable job.
    """

    try:
        operation_timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("browser task timeout is invalid") from exc
    if operation_timeout < 1.0 or not math.isfinite(operation_timeout):
        raise ValueError("browser task timeout is invalid")
    result = getattr(task, "result", None)
    cancel = getattr(task, "cancel", None)
    if not callable(result) or not callable(cancel):
        raise TypeError("browser task is invalid")
    deadline = time.monotonic() + operation_timeout + _browser_task_queue_grace_seconds()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancel()
            raise BrowserOperationCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancel()
            raise BrowserOperationError(timeout_message, code=timeout_code)
        try:
            return result(timeout=min(0.25, remaining))
        except CancelledError:
            raise BrowserOperationCancelled() from None
        except TimeoutError:
            continue


class PlaywrightMissavDriver:
    """A driver with exactly one persistent context and one active page."""

    # A single operation may probe several variants/pages.  Cloudflare can
    # rotate its one-level challenge transport subdomain between probes, so
    # keep a bounded but non-fragile per-operation DNS budget.
    _RESOLVER_MAX_HOSTS = 16

    def __init__(self, profile_dir: str | os.PathLike[str]) -> None:
        self.profile_dir = Path(profile_dir)
        self._playwright: object | None = None
        self._context: object | None = None
        self._page: object | None = None
        self._resolver: object | None = None
        self._media_gate: object | None = None
        self._stage_lock = threading.Lock()
        self._active_stage: str | None = None
        self._active_stage_started_at: float | None = None

    def start(self) -> None:
        from . import routing as missav_routing
        from .automation import browser_proxy_server
        from ..net.network_guard import PublicHostResolver

        self._prepare_profile()
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - runtime-only dependency
            raise RuntimeError("MissAV browser runtime is unavailable") from exc

        playwright = sync_playwright().start()
        context = None
        try:
            launch_options: dict[str, object] = {
                "user_data_dir": str(self.profile_dir),
                "headless": False,
                "locale": os.environ.get("JAV_PILOT_BROWSER_LOCALE", "zh-CN"),
                "viewport": {"width": 1280, "height": 900},
                "service_workers": "block",
                "accept_downloads": False,
                # Managed Cloudflare challenges reject Chromium's explicit
                # automation marker before their normal JavaScript check can
                # complete.  Keep the real headful browser and all network
                # route guards, but omit only that marker and expose the
                # standards-compatible navigator value used by normal Chrome.
                "ignore_default_args": ["--enable-automation"],
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            proxy = browser_proxy_server()
            if proxy:
                launch_options["proxy"] = {"server": proxy}
            channel = os.environ.get("JAV_PILOT_BROWSER_CHANNEL", "").strip() or None
            if channel is not None:
                context = playwright.chromium.launch_persistent_context(
                    channel=channel,
                    **launch_options,
                )
            else:
                context = playwright.chromium.launch_persistent_context(**launch_options)

            # A challenge can use both the fixed Cloudflare host and a
            # single-level dynamic challenge subdomain in addition to the
            # MissAV origin, player CDN, and media host.
            resolver = PublicHostResolver(max_hosts=self._RESOLVER_MAX_HOSTS)
            media_gate = missav_routing.BrowserMediaGate(block_surrit_requests=False)
            # Store these before registering callbacks.  The callback reads
            # the current resolver so every broker operation gets a fresh,
            # bounded DNS cache even though the browser context is shared.
            self._resolver = resolver
            self._media_gate = media_gate
            context.route(
                "**/*",
                lambda route: missav_routing.route_request(
                    route,
                    resolver=self._resolver
                    or PublicHostResolver(max_hosts=self._RESOLVER_MAX_HOSTS),
                    media_gate=self._media_gate or media_gate,
                ),
            )
            route_web_socket = getattr(context, "route_web_socket", None)
            if not callable(route_web_socket):
                raise RuntimeError("MissAV WebSocket routing is unavailable")
            route_web_socket("**/*", missav_routing.deny_routed_web_socket)

            pages = list(getattr(context, "pages", ()) or ())
            page = pages[0] if pages else context.new_page()
            for extra in pages[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
            # MissAV advertisements can synchronously create an about:blank
            # popup before the guarded navigation is rejected.  Close every
            # non-primary page at creation time so the broker's one-page
            # invariant does not mistake a blocked popup for a crashed
            # browser and discard the trusted session.
            def close_extra_page(candidate: object) -> None:
                if candidate is page:
                    return
                try:
                    candidate.close()
                except Exception:
                    pass

            context.on("page", close_extra_page)
            page.set_default_timeout(45_000)
            self._playwright = playwright
            self._context = context
            self._page = page
        except Exception:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass
            raise

    def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        self._resolver = None
        self._media_gate = None
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass

    def session_counts(self) -> BrowserSessionCounts:
        context = self._context
        if context is None:
            return BrowserSessionCounts(0, 0, 0)
        pages = tuple(getattr(context, "pages", ()) or ())
        return BrowserSessionCounts(1, 1, len(pages))

    def execute(self, operation: BrowserOperation, *, cancel_event: threading.Event) -> object:
        page = self._page
        if page is None:
            raise RuntimeError("MissAV browser page is unavailable")
        payload = operation.payload
        if not isinstance(payload, Mapping):
            raise BrowserOperationError("MissAV browser operation is invalid")
        from . import client as missav
        from . import errors as missav_errors
        from . import site as missav_site
        from ..net.network_guard import PublicHostResolver

        try:
            # PublicHostResolver caches DNS decisions per operation.  Reusing
            # one cache for the lifetime of a persistent browser would let
            # rotating Cloudflare challenge subdomains exhaust its host
            # budget and permanently fail closed.
            self._resolver = PublicHostResolver(max_hosts=self._RESOLVER_MAX_HOSTS)
            if self._media_gate is not None:
                self._media_gate.block_surrit_requests = False
            # Keep site capabilities aligned with the operation being run.
            # Search/series and description are intentionally independent of
            # the web-download switch; using the default capability here made
            # an otherwise enabled search fail whenever web downloads were
            # disabled in settings.
            with missav_site.missav_operation(
                self._required_capability(operation.kind)
            ):
                if operation.kind in {
                    BrowserOperationKind.DOWNLOAD_CAPTURE,
                    BrowserOperationKind.MANIFEST_REFRESH,
                }:
                    return missav.capture_manifest_on_page(
                        page,
                        payload.get("code"),
                        variant=payload.get("variant", "original"),
                        timeout_seconds=payload.get("timeout_seconds", 45.0),
                        requested_height=payload.get("requested_height"),
                        quality_strategy=payload.get("quality_strategy", "legacy"),
                        cancel_event=cancel_event,
                        media_gate=self._media_gate,
                        stage_callback=self._set_active_stage,
                    )
                if operation.kind is BrowserOperationKind.QUALITY_DISCOVERY:
                    return self._discover_options_on_page(page, payload, cancel_event)
                if operation.kind is BrowserOperationKind.DESCRIPTION:
                    return self._description_on_page(page, payload, cancel_event)
                if operation.kind is BrowserOperationKind.SERIES_DISCOVERY:
                    return self._series_discovery_on_page(page, payload, cancel_event)
                # Interactive/search/diagnostic operations are intentionally
                # handled by the same page and never create a second tab.
                return self._diagnostic_on_page(page, payload, cancel_event)
        except missav_errors.MissavNotFound:
            raise BrowserOperationError(
                "MissAV has no exact result for this catalog code",
                code="not_found",
            ) from None
        except missav_errors.MissavTransientError as exc:
            if exc.code == "challenge_active":
                raise BrowserChallengeActive() from None
            if (
                exc.code == "transport_reset"
                and operation.kind
                in {
                    BrowserOperationKind.DOWNLOAD_CAPTURE,
                    BrowserOperationKind.MANIFEST_REFRESH,
                }
            ):
                # Chromium can retain a failed proxy/TLS connection after an
                # intermittent ERR_CONNECTION_RESET.  Reusing that network
                # service makes every automatic retry fail even though a new
                # context with the same persistent profile succeeds.  Let the
                # broker replace its one driver; its bounded restart budget
                # prevents loops and the profile preserves trusted cookies.
                raise BrowserDriverCrashed("transport_reset") from None
            status = 429 if exc.code == "rate_limited" else 503
            raise BrowserUpstreamError(status) from None
        except missav_errors.MissavError as exc:
            if cancel_event.is_set():
                raise BrowserOperationCancelled() from None
            raise BrowserOperationError(str(exc), code=exc.code) from None
        except Exception as exc:  # noqa: BLE001 - Playwright is optional/runtime-only.
            if cancel_event.is_set():
                raise BrowserOperationCancelled() from None
            # Playwright's TargetClosed/BrowserDisconnected errors vary by
            # version and can include the current URL or signed headers.  Map
            # only the fixed crash classification to the broker's restart
            # path; never expose the original exception text.
            if self._is_browser_crash(exc):
                raise BrowserDriverCrashed("target_closed") from None
            raise
        finally:
            self._set_active_stage(None)

    def _set_active_stage(self, stage: str | None) -> None:
        allowed = {
            "locate_exact_detail",
            "reload_detail",
            "start_player",
            "manifest_wait",
            "quality_resolution",
        }
        clean_stage = stage if stage in allowed else None
        with self._stage_lock:
            self._active_stage = clean_stage
            self._active_stage_started_at = (
                time.monotonic() if clean_stage is not None else None
            )

    def active_stage_snapshot(self) -> tuple[str | None, float]:
        with self._stage_lock:
            stage = self._active_stage
            started_at = self._active_stage_started_at
        elapsed = (
            max(0.0, time.monotonic() - started_at)
            if stage is not None and started_at is not None
            else 0.0
        )
        return stage, elapsed

    @staticmethod
    def _required_capability(kind: BrowserOperationKind) -> str:
        if kind in {
            BrowserOperationKind.SERIES_DISCOVERY,
            BrowserOperationKind.SEARCH_PAGE,
        }:
            return RESOURCE_SEARCH_CAPABILITY
        if kind is BrowserOperationKind.DESCRIPTION:
            return DESCRIPTION_CAPABILITY
        return WEB_DOWNLOAD_CAPABILITY

    @staticmethod
    def _is_browser_crash(error: BaseException) -> bool:
        """Return whether *error* is a closed/disconnected Playwright target.

        This deliberately examines the value only inside the trusted runtime;
        callers never receive its message. The classification is shared with
        the capture path so a crash is never split-brained into an upstream
        failure there and a driver crash here.
        """

        from . import navigation as missav_navigation

        return missav_navigation.is_browser_target_closed(error)

    def _discover_options_on_page(
        self,
        page: object,
        payload: Mapping[str, object],
        cancel_event: threading.Event,
    ) -> object:
        # Probe variants one at a time on the same page.  A failed optional
        # variant is isolated and cannot erase a confirmed original result.
        from . import automation as missav_automation
        from . import client as missav_client
        from . import errors as missav_errors
        from . import parsing as missav_parsing

        search_code, canonical_code = missav_parsing.validated_code(payload.get("code"))
        timeout_seconds = missav_client.validated_timeout(
            payload.get("timeout_seconds", 45.0), operation="quality discovery"
        )
        options: list[object] = []
        for variant in WEB_DOWNLOAD_VARIANTS:
            if cancel_event.is_set():
                raise BrowserOperationCancelled()
            try:
                heights = missav_automation.discover_qualities_on_page(
                    page,
                    search_code,
                    canonical_code,
                    time.monotonic() + timeout_seconds,
                    variant=variant,
                    media_gate=self._media_gate,
                    cancel_event=cancel_event,
                )
            except missav_errors.MissavNotFound:
                options.append(WebDownloadVariantOption(variant, "not_found", ()))
            except missav_errors.MissavTransientError as exc:
                # A challenge, rate limit, or upstream outage is a
                # broker-level transient.  Do not turn it into an optional
                # variant failure: doing so would bypass cooldown and make
                # every following variant repeat the challenged request.
                if exc.code != "navigation_timeout":
                    raise
                options.append(WebDownloadVariantOption(variant, "failed", ()))
            except missav_errors.MissavError:
                options.append(WebDownloadVariantOption(variant, "failed", ()))
            else:
                options.append(WebDownloadVariantOption(variant, "available", heights))
            if cancel_event.is_set():
                raise BrowserOperationCancelled()
        return tuple(options)

    def _description_on_page(
        self,
        page: object,
        payload: Mapping[str, object],
        cancel_event: threading.Event,
    ) -> str | None:
        from . import automation as missav_automation
        from . import client as missav_client
        from . import parsing as missav_parsing

        search_code, canonical_code = missav_parsing.validated_code(payload.get("code"))
        clean_variant = missav_client.validated_variant(
            payload.get("variant", "original")
        )
        timeout_seconds = missav_client.validated_timeout(
            payload.get("timeout_seconds", 45.0), operation="description"
        )
        return missav_automation.fetch_description_on_page(
            page,
            search_code,
            canonical_code,
            time.monotonic() + timeout_seconds,
            variant=clean_variant,
            cancel_event=cancel_event,
        )

    def _diagnostic_on_page(
        self,
        page: object,
        payload: Mapping[str, object],
        cancel_event: threading.Event,
    ) -> object:
        # Diagnostics use a bounded same-origin navigation and return only
        # public status/booleans; no response body or URL is exposed.
        code = payload.get("code")
        if code:
            return self._description_on_page(page, payload, cancel_event)
        return {"ok": True}

    def _series_discovery_on_page(
        self,
        page: object,
        payload: Mapping[str, object],
        cancel_event: threading.Event,
    ) -> object:
        """Discover a bounded series on the broker-owned page.

        The parser and exact-route rules are shared with the standalone
        worker while navigation stays on this trusted page.
        """

        from . import automation as missav_automation
        from . import client as missav_client

        request = missav_client.validated_series_request(
            payload.get("prefix"),
            suffix_width=payload.get("suffix_width"),
            start=payload.get("start"),
            end=payload.get("end"),
            max_codes=payload.get("max_codes"),
            timeout_seconds=payload.get("timeout_seconds"),
        )
        return missav_automation.discover_series_on_page(
            page,
            request.display_prefix,
            request.canonical_prefix,
            suffix_width=request.suffix_width,
            start=request.start,
            end=request.end,
            max_codes=request.max_codes,
            deadline=time.monotonic() + request.timeout_seconds,
            cancel_event=cancel_event,
        )

    def _prepare_profile(self) -> None:
        path = self.profile_dir
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise RuntimeError("MissAV browser profile path is invalid")
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("MissAV browser profile path is unsafe")
        try:
            os.chmod(path, stat.S_IRWXU)
        except OSError:
            pass
        # Chrome's persistent profile stores three process-local singleton
        # links at its root. A container stop invalidates their target PID and
        # Unix socket, but the links can survive on the dedicated persistent
        # browser-state volume and make every subsequent launch report that
        # the profile is in use.
        # The broker lease guarantees there is no second live owner here.
        # Remove only these fixed runtime lock names; history, cookies, site
        # trust, cache, and every other profile asset remain untouched.
        for name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
            singleton = path / name
            try:
                metadata = singleton.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("MissAV browser profile lock path is unsafe")
            try:
                singleton.unlink()
            except OSError as exc:
                raise RuntimeError(
                    "MissAV browser profile lock could not be cleared"
                ) from exc


# A failed browser launch leaves the service restartable; this cooldown paces
# how often consumer requests may trigger another Chromium launch attempt.
_START_RETRY_COOLDOWN_SECONDS = 30.0


class MissavBrowserRuntime:
    """Lazily started broker facade; explicit shutdown ends its lifecycle."""

    def __init__(
        self,
        *,
        profile_dir: str | os.PathLike[str] | None = None,
        owner_lease_path: str | os.PathLike[str] | None = None,
        cooldown_schedule: tuple[float, ...] | None = None,
    ) -> None:
        configured = profile_dir or os.environ.get(
            "JAV_PILOT_MISSAV_BROWSER_PROFILE_DIR",
        )
        if configured is None:
            # Browser trust is persistent operational state, but it is not
            # application data: it contains cookies, history and cache and
            # therefore must stay outside the snapshot-managed data tree.
            configured = str(Path.cwd() / "runtime" / "missav-browser-profile")
        self.profile_dir = Path(configured)
        lease = FileBrowserOwnerLease(owner_lease_path)
        schedule = cooldown_schedule or (5.0, 30.0, 120.0, 600.0)
        self._service = MissavBrowserService(
            driver_factory=lambda: PlaywrightMissavDriver(self.profile_dir),
            owner_lease=lease,
            cooldown_schedule=schedule,
            max_cooldown=max(schedule),
            max_restart_attempts=2,
        )
        self._lock = threading.Lock()
        self._shutdown_requested = threading.Event()
        self._started = False
        self._start_failure: BrowserServiceError | None = None
        self._start_failed_at = 0.0

    def start(self) -> None:
        if self._shutdown_requested.is_set():
            raise BrowserServiceClosed("browser runtime is shutting down")
        with self._lock:
            if self._shutdown_requested.is_set():
                raise BrowserServiceClosed("browser runtime is shutting down")
            if self._started and self._service.is_running():
                return
            # The service recycles itself from STOPPED, so a failed launch (or
            # a later restart-budget exhaustion) is recoverable — but pace the
            # relaunch attempts so a persistently broken environment does not
            # pay a Chromium launch on every request.
            now = time.monotonic()
            if (
                self._start_failure is not None
                and now - self._start_failed_at < _START_RETRY_COOLDOWN_SECONDS
            ):
                raise self._start_failure
            try:
                self._service.start()
            except BrowserServiceError as error:
                self._start_failure = error
                self._start_failed_at = now
                self._started = False
                raise
            self._start_failure = None
            self._started = True

    def shutdown(self, *, timeout: float = 15.0) -> bool:
        # Close admission before waiting for an in-flight launch to release the lock.
        self._shutdown_requested.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            stopped = self._service.shutdown(
                timeout=max(0.0, deadline - time.monotonic())
            )
            if stopped:
                self._started = False
            return stopped
        finally:
            self._lock.release()

    def capture_manifest(
        self,
        code: object,
        *,
        variant: object = "original",
        timeout_seconds: float = 45.0,
        requested_height: object | None = None,
        quality_strategy: object | None = "legacy",
        cancel_event: threading.Event | None = None,
    ) -> object:
        task = self.submit(
            BrowserOperationKind.DOWNLOAD_CAPTURE,
            {
                "code": code,
                "variant": variant,
                "timeout_seconds": timeout_seconds,
                "requested_height": requested_height,
                "quality_strategy": quality_strategy,
            },
        )
        return wait_for_browser_task(
            task,
            timeout_seconds=float(timeout_seconds),
            cancel_event=cancel_event,
            timeout_message="MissAV browser capture timed out",
            timeout_code="navigation_timeout",
        )

    def submit(
        self,
        kind: BrowserOperationKind | str,
        payload: Mapping[str, object],
        *,
        retry_on_upstream: bool = True,
    ):
        self.start()
        with self._lock:
            if self._shutdown_requested.is_set():
                raise BrowserServiceClosed("browser runtime is shutting down")
            return self._service.submit(
                kind, dict(payload), retry_on_upstream=retry_on_upstream
            )

    def metrics_snapshot(self):
        return self._service.metrics_snapshot()


_RUNTIME_LOCK = threading.Lock()
_RUNTIME: MissavBrowserRuntime | None = None
_RUNTIME_STOPPING = False


def get_missav_browser_runtime() -> MissavBrowserRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME_STOPPING:
            raise BrowserServiceClosed("browser runtime is shutting down")
        if _RUNTIME is None:
            _RUNTIME = MissavBrowserRuntime()
        return _RUNTIME


def shutdown_missav_browser_runtime(*, timeout: float = 15.0) -> bool:
    global _RUNTIME_STOPPING
    with _RUNTIME_LOCK:
        _RUNTIME_STOPPING = True
        runtime = _RUNTIME
    return True if runtime is None else runtime.shutdown(timeout=timeout)
