"""Sources served by the bundled Jackett, and its one-time indexer setup.

While Jackett has no admin password its management API needs no login, so a
background task adds the public indexers JAV Pilot uses and, when JAV Pilot
has an explicit proxy and Jackett none, hands that proxy to Jackett. Once an
admin password is set the indexers are the user's to manage. Until Jackett is
ready its sources are skipped with a reason, never reported as failures.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

from ..config.bundled_jackett import JACKETT_INDEXERS, BundledJackett
from .torznab import PendingTorznabIndexer, TorznabConfig, TorznabIndexer

LOGGER = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRY_MIN_SECONDS = 5.0
_RETRY_MAX_SECONDS = 300.0
_PROXY_TYPES = {"http": 0, "socks4": 1, "socks5": 2, "socks5h": 2}
_PROXY_DISABLED = -1
# Jackett's default Tokyo Toshokan mirror is unreliable; used only when the
# indexer is first added, so a later choice in Jackett is kept.
_TOKYOTOSHO_SITE_LINK = "https://www.tokyotosho.se/"

# Set by the bootstrap task; None when it is not running in this process
# (the CLI), in which case sources are used as soon as the API key exists.
_setup_lock = threading.Lock()
_setup_pending: str | None = None


class JackettUnavailable(RuntimeError):
    """Jackett cannot be set up yet; a short, credential-free reason."""


def bundled_torznab_indexer(
    site_id: str,
    display_name: str,
    config: dict,
    jackett: BundledJackett | None,
) -> TorznabIndexer:
    """The indexer of a torrent source left empty for the bundled Jackett."""

    if jackett is None or site_id not in JACKETT_INDEXERS:
        return PendingTorznabIndexer(site_id, "未填写 Torznab 地址与 API 密钥")
    with _setup_lock:
        pending = _setup_pending
    if pending is not None:
        return PendingTorznabIndexer(site_id, pending)
    api_key = jackett.api_key()
    if api_key is None:
        return PendingTorznabIndexer(site_id, "内置 Jackett 尚未就绪")
    return TorznabIndexer(
        TorznabConfig(
            endpoint=jackett.endpoint(site_id),
            api_key=api_key,
            source_id=site_id,
            display_name=display_name,
            pinned_addresses=tuple(config.get("pinned_addresses", [])),
            categories=tuple(config.get("categories", [])),
        )
    )


def run_jackett_setup(
    stop_event: threading.Event,
    jackett: BundledJackett | None = None,
) -> None:
    """Retry the bundled Jackett setup until it is done; never raises."""

    global _setup_pending
    jackett = jackett or BundledJackett.from_env()
    if jackett is None:
        return
    proxy = os.environ.get("JAV_PILOT_PROXY", "").strip()
    delay = _RETRY_MIN_SECONDS
    last_reason: str | None = None
    with _setup_lock:
        _setup_pending = "内置 Jackett 正在准备"
    try:
        while not stop_event.is_set():
            try:
                if setup_bundled_jackett(jackett, proxy=proxy):
                    LOGGER.info("bundled Jackett indexers are ready")
                else:
                    LOGGER.info(
                        "bundled Jackett has an admin password; "
                        "its indexers are left to the user"
                    )
                return
            except JackettUnavailable as exc:
                if str(exc) != last_reason:
                    LOGGER.info("bundled Jackett is not ready, retrying: %s", exc)
                    last_reason = str(exc)
            except Exception:
                LOGGER.exception("unexpected bundled Jackett setup failure")
            stop_event.wait(delay)
            delay = min(delay * 2, _RETRY_MAX_SECONDS)
    finally:
        with _setup_lock:
            _setup_pending = None


def setup_bundled_jackett(jackett: BundledJackett, *, proxy: str = "") -> bool:
    """Add the missing indexers; False when an admin password locks Jackett."""

    if jackett.api_key() is None:
        raise JackettUnavailable("waiting for Jackett to create its API key")
    session = _JackettSession(jackett.origin)
    if not session.sign_in():
        return False
    configured = session.configured_indexers()
    missing = [
        indexer for indexer in JACKETT_INDEXERS.values() if not configured.get(indexer)
    ]
    if not missing:
        return True
    if proxy:
        # Indexers are added with Jackett's proxy in place, so it applies to
        # them from the first query.
        session.inherit_proxy(proxy)
    for indexer in missing:
        session.add_indexer(indexer)
    # Structured logs keep only the message text, not its arguments.
    LOGGER.info("added the missing public indexers to the bundled Jackett")
    return True


class _JackettSession:
    def __init__(self, origin: str) -> None:
        self.origin = origin
        # Jackett is a service on the Compose network: never send its
        # requests through HTTP_PROXY.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def sign_in(self) -> bool:
        # Without an admin password the dashboard signs the client in; with
        # one it redirects to the login page.
        final_url = self._request("/UI/Dashboard")[0]
        return not urllib.parse.urlsplit(final_url).path.rstrip("/").endswith(
            "/UI/Login"
        )

    def configured_indexers(self) -> dict[str, bool]:
        entries = self._json("/api/v2.0/indexers")
        if not isinstance(entries, list):
            raise JackettUnavailable("Jackett returned an invalid indexer list")
        return {
            entry["id"]: entry.get("configured") is True
            for entry in entries
            if isinstance(entry, dict) and entry.get("id") in JACKETT_INDEXERS.values()
        }

    def inherit_proxy(self, proxy: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(proxy)
            port = parsed.port
        except ValueError:
            parsed = None
            port = None
        if (
            parsed is None
            or parsed.scheme not in _PROXY_TYPES
            or not parsed.hostname
            or not port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            LOGGER.warning(
                "JAV_PILOT_PROXY is not an http or socks proxy URL; "
                "configure the bundled Jackett proxy in its WebUI"
            )
            return
        config = self._json("/api/v2.0/server/config")
        if not isinstance(config, dict):
            raise JackettUnavailable("Jackett returned an invalid server config")
        if config.get("proxy_type", _PROXY_DISABLED) != _PROXY_DISABLED:
            return
        config.update(
            {
                "proxy_type": _PROXY_TYPES[parsed.scheme],
                "proxy_url": parsed.hostname,
                "proxy_port": port,
                "proxy_username": urllib.parse.unquote(parsed.username or "") or None,
                "proxy_password": urllib.parse.unquote(parsed.password or "") or None,
            }
        )
        self._request("/api/v2.0/server/config", data=config)
        LOGGER.info("configured the bundled Jackett to use JAV_PILOT_PROXY")

    def add_indexer(self, indexer: str) -> None:
        path = f"/api/v2.0/indexers/{indexer}/config"
        fields = self._json(path)
        if not isinstance(fields, list):
            raise JackettUnavailable("Jackett returned an invalid indexer config")
        if indexer == "tokyotosho":
            for item in fields:
                if isinstance(item, dict) and item.get("id") == "sitelink":
                    item["value"] = _TOKYOTOSHO_SITE_LINK
        self._request(path, data=fields)

    def _json(self, path: str) -> object:
        try:
            return json.loads(self._request(path)[1])
        except ValueError:
            raise JackettUnavailable("Jackett returned invalid JSON") from None

    def _request(self, path: str, *, data: object = None) -> tuple[str, bytes]:
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            self.origin + path,
            data=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
        )
        try:
            with self._opener.open(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise JackettUnavailable(f"Jackett HTTP {code}") from None
        except (OSError, http.client.HTTPException):
            # Never surface transport errors: they can carry request URLs.
            raise JackettUnavailable("Jackett is unreachable") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise JackettUnavailable("Jackett response is too large")
        return final_url, raw
