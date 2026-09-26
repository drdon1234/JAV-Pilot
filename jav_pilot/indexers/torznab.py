from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from jav_pilot.core.catalog_code import canonical_catalog_code, normalize_catalog_code
from jav_pilot.core.guards import looks_like_catalog_code, normalize_query
from jav_pilot.net.http_client import FetchError
from jav_pilot.torrent.magnet import MagnetError, parse_magnet
from jav_pilot.core.models import MagnetInfo, SearchBounds, SearchResult
from jav_pilot.net.network_guard import resolve_public_addresses

from .base import Indexer


_ATTR_NAMESPACE = "{http://torznab.com/schemas/2015/feed}attr"
_MAX_TORRENT_BYTES = 4 * 1024 * 1024
_SECRET_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "jackett_apikey",
        "token",
        "password",
        "passkey",
        "auth",
        "authorization",
        "secret",
    }
)
_FC2_RE = re.compile(
    r"(?<![A-Z0-9])FC2[-_. ]*(?:PPV[-_. ]*)?(\d{2,9})(?![A-Z0-9])", re.I
)
_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2,12}[-_. ]?\d{2,8})(?![A-Z0-9])", re.I)
_PLACEHOLDER_ACTIVITY_SOURCES = frozenset(
    {"onejav", "freejavtorrent", "freejav", "fjt"}
)


class TorznabError(FetchError):
    """A deliberately credential-free upstream or validation error."""


@dataclass(frozen=True, slots=True)
class TorznabConfig:
    endpoint: str
    api_key: str = field(repr=False)
    source_id: str = "sukebei"
    display_name: str = ""
    pinned_addresses: tuple[str, ...] = ()
    categories: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        parsed = _checked_url(self.endpoint)
        if parsed.query or parsed.fragment or not parsed.path or "%" in parsed.netloc:
            raise TorznabError(
                "Torznab endpoint must be an API URL without query or credentials"
            )
        if re.search(r"/indexers/all(?:/|$)", parsed.path, flags=re.I):
            raise TorznabError("Torznab requires one upstream per configured source")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.source_id):
            raise TorznabError("Torznab source identifier is invalid")
        if (
            not isinstance(self.api_key, str)
            or not 1 <= len(self.api_key) <= 512
            or _controls(self.api_key)
        ):
            raise TorznabError("Torznab API key is invalid")
        if len(self.display_name) > 100 or _controls(self.display_name):
            raise TorznabError("Torznab display name is invalid")
        if (
            not isinstance(self.pinned_addresses, tuple)
            or len(self.pinned_addresses) > 8
        ):
            raise TorznabError("Torznab address pins are invalid")
        for value in self.pinned_addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                raise TorznabError("Torznab address pins must be IP literals") from None
            if (
                address.is_unspecified
                or address.is_multicast
                or address.is_link_local
                or address.is_reserved
            ):
                raise TorznabError(
                    "Torznab address pin is not an allowed unicast address"
                )
        if (
            not isinstance(self.categories, tuple)
            or len(self.categories) > 32
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 1_000_000
                for value in self.categories
            )
        ):
            raise TorznabError("Torznab categories are invalid")
        if parsed.scheme == "http" and not self.pinned_addresses:
            raise TorznabError("HTTP Torznab services require explicit address pins")
        try:
            literal = ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            literal = None
        if (
            literal is not None
            and self.pinned_addresses
            and literal.compressed
            not in {
                ipaddress.ip_address(value).compressed
                for value in self.pinned_addresses
            }
        ):
            raise TorznabError(
                "Torznab literal endpoint does not match its address pins"
            )
        if literal is not None and not literal.is_global and not self.pinned_addresses:
            raise TorznabError("Private Torznab services require explicit address pins")


@dataclass(frozen=True, slots=True)
class _Response:
    body: bytes = b""
    magnet: str | None = None


class TorznabIndexer(Indexer):
    """One configured upstream, preserving the source used by work aggregation.

    Use an indexer-specific Jackett/Prowlarr endpoint, not their all-indexers
    endpoint. API credentials never enter returned results or public errors.
    """

    name = "torznab"

    def __init__(self, config: TorznabConfig) -> None:
        self.config = config
        self.source_id = config.source_id
        self.base_url = config.endpoint

    def skip_reason(self, query: str, bounds: SearchBounds) -> str | None:
        if bounds.search_kind not in {"keyword", "code"}:
            return "仅支持番号和关键词搜索"
        return None

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query = normalize_query(query)
        bounds = bounds.normalized()
        if bounds.search_kind not in {"keyword", "code"}:
            return ()
        deadline = time.monotonic() + bounds.timeout_seconds
        code_query = (
            normalize_catalog_code(query.replace(" ", "-"))
            if looks_like_catalog_code(query)
            else None
        )
        parameters = {
            "t": "search",
            "q": code_query[0] if code_query else query,
            "limit": str(bounds.limit),
            "offset": str((bounds.page - 1) * bounds.limit),
        }
        if self.config.categories:
            parameters["cat"] = ",".join(str(value) for value in self.config.categories)
        response = self._request(
            self.config.endpoint + "?" + urlencode(parameters),
            deadline,
            bounds.max_response_bytes,
        )
        if response.magnet:
            raise TorznabError("Torznab search returned an invalid response")
        items = parse_torznab_feed(response.body, max_bytes=bounds.max_response_bytes)
        output: list[SearchResult] = []
        seen: set[str] = set()
        download_budget = min(bounds.detail_limit, 5) if bounds.fetch_magnets else 0
        for item in items[:200]:
            title = _text(item.findtext("title"), 1000)
            if (
                not title
                or _contains_secret(title, self.config.api_key)
                or not _matches_identity(query, title, bounds)
            ):
                continue
            attributes = {
                entry.get("name", "").lower(): entry.get("value", "")
                for entry in item.findall(_ATTR_NAMESPACE)
            }
            code = _title_code(title)
            metadata: dict[str, Any] = {
                "indexer": self.source_id,
                "indexer_name": self.config.display_name or self.source_id,
                "details_resolved": False,
            }
            published = _text(item.findtext("pubDate"), 100)
            if published and not _contains_secret(published, self.config.api_key):
                metadata["indexed_at"] = published
            magnet: MagnetInfo | None = None
            declared_hash = attributes.get("infohash", "")
            torrent_url: str | None = None
            candidates = [attributes.get("magneturl", ""), item.findtext("link", "")]
            candidates.extend(
                entry.get("url", "") for entry in item.findall("enclosure")
            )
            for candidate in candidates:
                if candidate.lower().startswith("magnet:?"):
                    try:
                        magnet = _safe_magnet(candidate, title, self.config.api_key)
                    except MagnetError:
                        continue
                    break
                if candidate and torrent_url is None:
                    torrent_url = candidate
            if magnet is None and declared_hash:
                try:
                    magnet = _safe_magnet(
                        "magnet:?"
                        + urlencode({"xt": "urn:btih:" + declared_hash, "dn": title}),
                        title,
                        self.config.api_key,
                    )
                except MagnetError:
                    pass
            if magnet is not None and declared_hash:
                try:
                    declared = parse_magnet(
                        "magnet:?" + urlencode({"xt": "urn:btih:" + declared_hash})
                    ).info_hash
                except MagnetError:
                    continue
                if magnet.info_hash != declared:
                    continue
            if magnet is None and torrent_url and download_budget:
                download_budget -= 1
                metadata["magnet_checked"] = True
                try:
                    download = self._request(
                        urljoin(self.config.endpoint, torrent_url),
                        deadline,
                        _MAX_TORRENT_BYTES,
                        download=True,
                    )
                    magnet = (
                        _safe_magnet(download.magnet, title, self.config.api_key)
                        if download.magnet
                        else parse_torrent(download.body, secret=self.config.api_key)
                    )
                except (TorznabError, MagnetError):
                    metadata["magnet_error"] = "索引种子文件解析失败，尚未验证可下载性"
            if magnet is not None and not _compatible_identity(
                title, magnet.display_name or ""
            ):
                continue
            if magnet is not None and not _matches_identity(
                query, magnet.display_name or title, bounds
            ):
                continue
            seeders = _integer(attributes.get("seeders"))
            leechers = _integer(attributes.get("leechers"))
            if leechers is None and seeders is not None:
                peers = _integer(attributes.get("peers"))
                if peers is not None and peers >= seeders:
                    leechers = peers - seeders
            activity_names = (
                self.source_id,
                self.config.display_name,
                *urlsplit(self.config.endpoint).path.split("/"),
            )
            placeholder = any(
                re.sub(r"[^a-z0-9]", "", name.lower()) in _PLACEHOLDER_ACTIVITY_SOURCES
                for name in activity_names
            )
            if placeholder:
                seeders = leechers = None
            observed = (
                datetime.now(timezone.utc).isoformat()
                if seeders is not None or leechers is not None
                else None
            )
            metadata["indexer_reported_activity"] = {
                "seeders": seeders,
                "leechers": leechers,
                "reported_at": observed,
                "verified_by_qb": False,
            }
            if magnet is not None:
                size = _integer(
                    attributes.get("size") or item.findtext("size"), maximum=10**16
                )
                if size is None:
                    size = next(
                        (
                            _integer(entry.get("length"), maximum=10**16)
                            for entry in item.findall("enclosure")
                            if entry.get("length")
                        ),
                        None,
                    )
                badges = (
                    (f"索引报告 {seeders} 做种 · 未实测",)
                    if seeders is not None
                    else ()
                )
                magnet = replace(
                    magnet,
                    reported_size_bytes=size or magnet.reported_size_bytes,
                    badges=badges,
                    reported_seeders=seeders,
                    reported_leechers=leechers,
                    reported_at=observed,
                )
                identity = magnet.info_hash
            else:
                identity = title.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            detail_url = next(
                (
                    clean
                    for value in (
                        item.findtext("comments", ""),
                        item.findtext("guid", ""),
                    )
                    if (clean := _public_result_url(value, self.config)) is not None
                ),
                None,
            )
            output.append(
                SearchResult(
                    source=self.source_id,
                    title=title,
                    url=detail_url,
                    code=code,
                    magnet_hint="available" if magnet else "unknown",
                    magnets=(magnet,) if magnet else (),
                    metadata=metadata,
                )
            )
            if len(output) >= bounds.limit:
                break
        return tuple(output)

    def diagnostic_detail_search(
        self, query: str, bounds: SearchBounds
    ) -> tuple[SearchResult, ...]:
        return self.search(query, replace(bounds, fetch_magnets=True))

    def detail_url_allowed(self, url: str) -> bool:
        """Validate a displayed source link; enrichment never fetches this URL."""
        return _public_result_url(url, self.config) is not None

    def enrich_results(
        self,
        results: tuple[SearchResult, ...],
        bounds: SearchBounds,
        *,
        include_images: bool = False,
        detail_limit: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[SearchResult, ...]:
        """Restore a compact work snapshot through its configured indexer API."""
        bounds = bounds.normalized()
        budget = bounds.detail_limit if detail_limit is None else max(0, detail_limit)
        query_bounds = replace(
            bounds,
            page=1,
            max_pages=1,
            fetch_magnets=True,
            detail_limit=max(1, bounds.detail_limit),
            search_kind="code",
            match="exact",
            filters={},
            semantic_refs={},
        )
        output: list[SearchResult] = []
        for result in results:
            if result.source != self.source_id or (cancelled and cancelled()):
                output.append(result)
                continue
            if result.magnets and not result.metadata.get("magnet_error"):
                output.append(
                    replace(
                        result,
                        metadata={
                            **result.metadata,
                            "magnet_checked": True,
                            "magnet_count": len(result.magnets),
                            "details_resolved": True,
                        },
                    )
                )
                continue
            if budget <= 0 or (
                result.metadata.get("magnet_checked")
                and not result.metadata.get("magnet_error")
            ):
                output.append(result)
                continue
            budget -= 1
            metadata = dict(result.metadata)
            magnets = {magnet.info_hash: magnet for magnet in result.magnets}
            code = normalize_catalog_code(result.code)
            error: str | None = None
            if code is None:
                error = "索引作品缺少有效番号，无法重新查询磁链"
            else:
                try:
                    matches = tuple(
                        candidate
                        for candidate in self.search(code[0], query_bounds)
                        if candidate.source == self.source_id
                        and canonical_catalog_code(candidate.code) == code[1]
                    )
                    if not matches:
                        error = "索引未返回同一番号的结果，未能恢复磁链"
                    else:
                        for candidate in matches:
                            for magnet in candidate.magnets:
                                # Fresh reports replace stale activity for this hash;
                                # hashes from other exact releases remain separate.
                                magnets[magnet.info_hash] = magnet
                        if any(
                            candidate.metadata.get("magnet_error")
                            for candidate in matches
                        ) or not any(candidate.magnets for candidate in matches):
                            error = "部分索引结果未能解析磁链，尚未验证可下载性"
                except Exception:
                    # Upstream failures may contain credential-bearing URLs.
                    error = "索引磁链查询失败，请稍后重试"
            if cancelled and cancelled():
                output.append(result)
                continue
            metadata.update(
                {
                    "indexer": self.source_id,
                    "indexer_name": self.config.display_name or self.source_id,
                    "magnet_checked": True,
                    "magnet_count": len(magnets),
                    "details_resolved": True,
                }
            )
            if error:
                metadata["magnet_error"] = error
            else:
                metadata.pop("magnet_error", None)
            output.append(
                replace(
                    result,
                    magnets=tuple(magnets.values()),
                    magnet_hint="available" if magnets else "unknown",
                    metadata=metadata,
                )
            )
        return tuple(output)

    def _request(
        self, url: str, deadline: float, max_bytes: int, *, download: bool = False
    ) -> _Response:
        try:
            return _request_bytes(
                self.config,
                url,
                deadline=deadline,
                max_bytes=max_bytes,
                download=download,
            )
        except TorznabError:
            raise
        except Exception:
            # Transport exceptions can contain a complete credential-bearing URL.
            raise TorznabError("Torznab request failed") from None


def parse_torznab_feed(
    body: bytes, *, max_bytes: int = 4 * 1024 * 1024
) -> tuple[ET.Element, ...]:
    if (
        not isinstance(body, bytes)
        or not body
        or len(body) > min(max_bytes, 4 * 1024 * 1024)
    ):
        raise TorznabError("Torznab response size is invalid")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise TorznabError("Torznab response must use UTF-8") from None
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise TorznabError("Torznab response contains a forbidden XML declaration")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise TorznabError("Torznab returned invalid XML") from None
    if root.tag == "error":
        raise TorznabError("Torznab service rejected the search request")
    if root.tag != "rss" or root.find("channel") is None:
        raise TorznabError("Torznab response is not an RSS feed")
    return tuple(root.findall("./channel/item")[:200])


def parse_torrent(body: bytes, *, secret: str = "") -> MagnetInfo:
    """Compute the v1 BTIH from the original bencoded info bytes, not a re-encode."""
    reader = _BencodeReader(body)
    payload = reader.read()
    if (
        reader.position != len(body)
        or not isinstance(payload, dict)
        or reader.info_span is None
    ):
        raise TorznabError("Invalid torrent metainfo")
    info = payload.get(b"info")
    if not isinstance(info, dict):
        raise TorznabError("Torrent has no info dictionary")
    name = _torrent_text(info.get(b"name.utf-8") or info.get(b"name"))
    pieces = info.get(b"pieces")
    piece_length = info.get(b"piece length")
    if (
        not name
        or not isinstance(pieces, bytes)
        or not pieces
        or len(pieces) % 20
        or not isinstance(piece_length, int)
        or not 1 <= piece_length <= 64 * 1024 * 1024
    ):
        raise TorznabError("Torrent does not contain supported v1 file metadata")
    if b"files" in info:
        files = info[b"files"]
        if (
            not isinstance(files, list)
            or not files
            or len(files) > 10_000
            or b"length" in info
        ):
            raise TorznabError("Torrent file list is invalid")
        size = 0
        for item in files:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get(b"length"), int)
                or item[b"length"] < 0
                or not isinstance(item.get(b"path"), list)
                or not item[b"path"]
            ):
                raise TorznabError("Torrent file entry is invalid")
            size += item[b"length"]
    else:
        size = info.get(b"length")
    if (
        not isinstance(size, int)
        or not 0 < size <= 10**16
        or len(pieces) // 20 != (size + piece_length - 1) // piece_length
    ):
        raise TorznabError("Torrent size and piece count do not agree")
    if info.get(b"private") == 1:
        # Converting private metainfo to a public DHT magnet loses its access
        # contract. The first supported indexers publish public torrents only.
        raise TorznabError(
            "Private torrent files require a dedicated download workflow"
        )
    start, end = reader.info_span
    info_hash = hashlib.sha1(body[start:end], usedforsecurity=False).hexdigest()
    trackers: list[str] = []
    announce = _torrent_text(payload.get(b"announce"))
    if announce:
        trackers.append(announce)
    tiers = payload.get(b"announce-list", [])
    if isinstance(tiers, list):
        for tier in tiers[:32]:
            if isinstance(tier, list):
                trackers.extend(
                    value for raw in tier[:16] if (value := _torrent_text(raw))
                )
    pairs = [("xt", "urn:btih:" + info_hash), ("dn", name), ("xl", str(size))]
    pairs.extend(
        ("tr", value)
        for value in dict.fromkeys(trackers)
        if _safe_tracker(value, secret)
    )
    magnet = _safe_magnet("magnet:?" + urlencode(pairs), name, secret)
    return replace(magnet, exact_length=size)


class _BencodeReader:
    def __init__(self, body: bytes) -> None:
        if not isinstance(body, bytes) or not 1 <= len(body) <= _MAX_TORRENT_BYTES:
            raise TorznabError("Torrent size is invalid")
        self.body = body
        self.position = 0
        self.nodes = 0
        self.info_span: tuple[int, int] | None = None

    def read(self, depth: int = 0) -> Any:
        self.nodes += 1
        if depth > 32 or self.nodes > 100_000 or self.position >= len(self.body):
            raise TorznabError("Torrent structure exceeds parsing limits")
        marker = self.body[self.position : self.position + 1]
        if marker == b"i":
            self.position += 1
            end = self.body.find(b"e", self.position, self.position + 23)
            raw = self.body[self.position : end] if end >= 0 else b""
            if not re.fullmatch(rb"(?:0|-?[1-9][0-9]{0,19})", raw):
                raise TorznabError("Torrent integer is invalid")
            self.position = end + 1
            return int(raw)
        if marker in {b"l", b"d"}:
            self.position += 1
            result: Any = [] if marker == b"l" else {}
            previous: bytes | None = None
            while (
                self.position < len(self.body)
                and self.body[self.position : self.position + 1] != b"e"
            ):
                if marker == b"l":
                    result.append(self.read(depth + 1))
                    continue
                key = self.read(depth + 1)
                if not isinstance(key, bytes) or (
                    previous is not None and key <= previous
                ):
                    raise TorznabError("Torrent dictionary keys are invalid")
                previous = key
                start = self.position
                result[key] = self.read(depth + 1)
                if depth == 0 and key == b"info":
                    self.info_span = (start, self.position)
            if self.position >= len(self.body):
                raise TorznabError("Torrent container is truncated")
            self.position += 1
            return result
        end = self.body.find(b":", self.position, self.position + 10)
        raw = self.body[self.position : end] if end >= 0 else b""
        if not re.fullmatch(rb"(?:0|[1-9][0-9]{0,7})", raw):
            raise TorznabError("Torrent string length is invalid")
        length = int(raw)
        start = end + 1
        self.position = start + length
        if self.position > len(self.body):
            raise TorznabError("Torrent string is truncated")
        return self.body[start : self.position]


def _request_bytes(
    config: TorznabConfig, url: str, *, deadline: float, max_bytes: int, download: bool
) -> _Response:
    configured_origin = _origin(config.endpoint)
    initial_origin = _origin(url)
    if initial_origin != configured_origin and not download:
        raise TorznabError("Torznab request left its configured service")
    for _ in range(3):
        parsed = _checked_url(url)
        service_request = _origin(url) == configured_origin
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=64)
        if service_request:
            pairs = [
                (key, value) for key, value in pairs if key.lower() not in _SECRET_KEYS
            ]
            key_name = (
                "jackett_apikey" if re.search(r"(?:^|/)dl/", parsed.path) else "apikey"
            )
            pairs.append((key_name, config.api_key))
        elif _contains_secret(url, config.api_key) or any(
            key.lower() in _SECRET_KEYS for key, _ in pairs
        ):
            raise TorznabError("External torrent URL contains credentials")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = (
            config.pinned_addresses
            if service_request and config.pinned_addresses
            else resolve_public_addresses(host, port)
        )
        if not addresses:
            raise TorznabError("Torznab request target is not allowed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TorznabError("Torznab request timed out")
        connection = _PinnedConnection(
            host, port, addresses[0], tls=parsed.scheme == "https", timeout=remaining
        )
        response = None
        watchdog = None
        try:
            connection.connect()
            transport_socket = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TorznabError("Torznab request timed out")
            # A per-read timeout alone permits slow header/body trickles to
            # exceed the overall search budget. Interrupt the pinned socket
            # at the absolute deadline as well, then cancel the bounded timer.
            watchdog = threading.Timer(
                remaining, _abort_socket, args=(transport_socket,)
            )
            watchdog.daemon = True
            watchdog.start()
            target = urlunsplit(("", "", parsed.path or "/", urlencode(pairs), ""))
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "application/x-bittorrent,application/rss+xml,application/xml",
                    "Accept-Encoding": "identity",
                    "User-Agent": "JAV-Pilot/Torznab",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location", "")
                if download and location.lower().startswith("magnet:?"):
                    return _Response(magnet=location)
                redirected = urljoin(url, location)
                if not location or _origin(redirected) != initial_origin:
                    raise TorznabError("Torznab cross-origin redirect was rejected")
                url = redirected
                continue
            if response.status != 200:
                raise TorznabError(f"Torznab HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() not in {
                "",
                "identity",
            }:
                raise TorznabError("Torznab response encoding is unsupported")
            declared_size = _integer(
                response.getheader("Content-Length"), maximum=10**20
            )
            if declared_size is not None and declared_size > max_bytes:
                raise TorznabError("Torznab response exceeds the byte limit")
            chunks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TorznabError("Torznab request timed out")
                if transport_socket is not None:
                    transport_socket.settimeout(remaining)
                chunk = response.read1(min(64 * 1024, max_bytes + 1 - size))
                if not chunk:
                    return _Response(body=b"".join(chunks))
                chunks.append(chunk)
                size += len(chunk)
            raise TorznabError("Torznab response exceeds the byte limit")
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if response is not None:
                response.close()
            connection.close()
    raise TorznabError("Torznab redirect limit exceeded")


def _abort_socket(transport_socket: socket.socket | None) -> None:
    if transport_socket is None:
        return
    try:
        transport_socket.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(
        self, host: str, port: int, address: str, *, tls: bool, timeout: float
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self.tls = tls

    def connect(self) -> None:
        deadline = time.monotonic() + self.timeout
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.tls:
            try:
                self.sock.settimeout(max(0.001, deadline - time.monotonic()))
                self.sock = ssl.create_default_context().wrap_socket(
                    self.sock, server_hostname=self.host
                )
            except Exception:
                self.sock.close()
                self.sock = None
                raise


def _checked_url(value: str):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16 * 1024
        or _controls(value)
        or "\\" in value
    ):
        raise TorznabError("Torznab URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise TorznabError("Torznab URL is invalid") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.fragment
    ):
        raise TorznabError("Torznab URL is invalid")
    return parsed


def _origin(value: str) -> tuple[str, str, int]:
    parsed = _checked_url(value)
    return (
        parsed.scheme,
        (parsed.hostname or "").rstrip(".").lower(),
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _safe_magnet(uri: str, title: str, secret: str) -> MagnetInfo:
    parsed = parse_magnet(uri)
    name = parsed.display_name or title
    if _contains_secret(name, secret):
        raise MagnetError("Magnet display name contains credentials")
    pairs = [("xt", "urn:btih:" + parsed.info_hash), ("dn", name[:1000])]
    if parsed.exact_length is not None and 0 < parsed.exact_length <= 10**16:
        pairs.append(("xl", str(parsed.exact_length)))
    pairs.extend(
        ("tr", tracker)
        for tracker in parsed.trackers[:32]
        if _safe_tracker(tracker, secret)
    )
    return parse_magnet("magnet:?" + urlencode(pairs))


def _safe_tracker(value: str, secret: str) -> bool:
    if len(value) > 2048 or _controls(value) or _contains_secret(value, secret):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
        pairs = parse_qsl(parsed.query, max_num_fields=64)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https", "udp"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(key.lower() in _SECRET_KEYS for key, _ in pairs)
    ):
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_global
    except ValueError:
        return "." in parsed.hostname and not parsed.hostname.endswith(
            (".local", ".lan", ".internal", ".localhost")
        )


def _public_result_url(value: str, config: TorznabConfig) -> str | None:
    try:
        parsed = _checked_url(value)
        if _origin(value) == _origin(config.endpoint) or _contains_secret(
            value, config.api_key
        ):
            return None
        if any(
            key.lower() in _SECRET_KEYS
            or re.search(
                r"auth|cookie|credential|key|password|secret|session|sign|token",
                key,
                re.I,
            )
            for key, _ in parse_qsl(parsed.query, max_num_fields=64)
        ):
            return None
        hostname = (parsed.hostname or "").rstrip(".").lower()
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            if "." not in hostname or hostname.endswith(
                (".local", ".lan", ".internal", ".localhost")
            ):
                return None
        return value
    except (TorznabError, ValueError):
        return None


def _title_code(title: str) -> str | None:
    clean = unicodedata.normalize("NFKC", title).upper()
    fc2 = set(_FC2_RE.findall(clean))
    if fc2:
        return "FC2-PPV-" + next(iter(fc2)) if len(fc2) == 1 else None
    codes = {
        normalized
        for match in _CODE_RE.finditer(clean)
        if (normalized := normalize_catalog_code(match.group(1).replace(" ", "-")))
        is not None
    }
    return next(iter(codes))[0] if len(codes) == 1 else None


def _matches_identity(query: str, title: str, bounds: SearchBounds) -> bool:
    expected = canonical_catalog_code(query)
    if expected and expected.startswith("FC2PPV"):
        # A bare number, substring, neighbouring episode or multi-product pack
        # never proves the requested FC2 identity.
        return {
            _canonical_fc2(value)
            for value in _FC2_RE.findall(unicodedata.normalize("NFKC", title))
        } == {expected}
    if expected and (
        bounds.match == "exact"
        or (bounds.match == "auto" and looks_like_catalog_code(query))
        or bounds.search_kind == "code"
    ):
        return canonical_catalog_code(_title_code(title)) == expected
    return True


def _compatible_identity(title: str, torrent_name: str) -> bool:
    left, right = (
        canonical_catalog_code(_title_code(title)),
        canonical_catalog_code(_title_code(torrent_name)),
    )
    return not (left and right and left != right)


def _canonical_fc2(value: str) -> str:
    return "FC2PPV" + value


def _contains_secret(value: str, secret: str) -> bool:
    if not secret:
        return False
    for _ in range(4):
        if secret in value:
            return True
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return False


def _controls(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _text(value: object, limit: int) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).replace("\x00", "").split()
    )[:limit]


def _torrent_text(value: object) -> str:
    if not isinstance(value, bytes):
        return ""
    return _text(value.decode("utf-8", errors="replace"), 2048)


def _integer(value: object, *, maximum: int = 1_000_000_000) -> int | None:
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,20}", str(value or "")):
        return None
    result = int(str(value))
    return result if result <= maximum else None
