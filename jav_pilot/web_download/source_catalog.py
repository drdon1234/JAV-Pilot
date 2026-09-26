"""Bounded catalog and media probes for opt-in Web sources.

A catalog entry and even a playable VOD are not evidence of a complete film.
Download eligibility additionally requires an advertised duration that agrees
with the actual finite media playlist. No JavaScript is executed here.
"""
from __future__ import annotations

import html
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.catalog_code import normalize_catalog_code
from ..net.http_client import FetchError, fetch_text
from .variant import MissavVariant
from .resume import parse_media_playlist


KISSJAV_SITE_ID = "kissjav"
JAVNONI_SITE_ID = "javnoni"
CANDIDATE_WEB_SOURCE_IDS = (KISSJAV_SITE_ID, JAVNONI_SITE_ID)
CANDIDATE_WEB_ORIGINS = {
    KISSJAV_SITE_ID: "https://kissjav.li",
    JAVNONI_SITE_ID: "https://jav-noni.live",
}
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_CODE = re.compile(
    r"(?<![A-Z0-9])(?:FC2[-_ ]*(?:PPV[-_ ]*)?\d{2,9}|"
    r"[A-Z]{2,12}[-_]\d{2,8})(?![A-Z0-9])", re.I
)
_UNSUPPORTED_EDIT = re.compile(
    r"trailer|preview|\bteaser\b|\bclip\b|sample|予告|預告|预告|"
    r"試看片|试看|體驗版|体验版|ダイジェスト|切り抜き|抜粋|"
    r"モザイク破壊|モザイク除去|AI.{0,4}(?:无码|無碼|去码)|合集|総集編|compilation",
    re.I,
)
_CHALLENGE = re.compile(
    r"<title>\s*(?:just a moment|attention required)|cf-chl-|"
    r"verify (?:that )?you are human", re.I
)
_MEDIA = re.compile(
    r"(?:https?:)?//[^\s\"'<>\\]+\.(?:m3u8|mp4)(?:\?[^\s\"'<>\\]*)?", re.I
)
_PLAYER_HOSTS = {"luluvdo.com", "www.luluvdo.com", "streamhls.to"}
_MEDIA_SUFFIXES = {JAVNONI_SITE_ID: ("tnmr.org",), KISSJAV_SITE_ID: ("kissjav.li",)}


class WebCatalogError(ValueError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"Web catalog probe failed: {code}")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    code: str
    title: str
    detail_url: str
    variant: MissavVariant


@dataclass(frozen=True, slots=True)
class CatalogPage:
    entries: tuple[CatalogEntry, ...]
    total_pages: int


@dataclass(frozen=True, slots=True)
class CatalogMedia:
    url: str
    page_url: str
    referer: str
    selected_height: int | None
    duration_seconds: float
    expected_duration_seconds: float | None
    full_length_verified: bool


def catalog_search_url(source_id: str, origin: str, query: str, page: int) -> str:
    if source_id not in CANDIDATE_WEB_SOURCE_IDS or not 1 <= page <= 999:
        raise WebCatalogError("configuration")
    encoded = quote(query, safe="")
    if source_id == KISSJAV_SITE_ID:
        base = f"{origin}/search/{encoded}/"
        return base if page == 1 else f"{base}?from_videos={page}"
    return f"{origin}/?s={encoded}" if page == 1 else f"{origin}/page/{page}/?s={encoded}"


def _catalog_identity(title: str) -> tuple[str, str] | None:
    identities = {}
    for match in _CODE.finditer(unicodedata.normalize("NFKC", title)):
        normalized = normalize_catalog_code(match.group(), max_length=32)
        if normalized is not None:
            identities[normalized[1]] = normalized
    return next(iter(identities.values())) if len(identities) == 1 else None


def _variant(title: str) -> MissavVariant | None:
    if _UNSUPPORTED_EDIT.search(title):
        return None
    if re.search(r"中文字幕|中字|chinese[ -]sub", title, re.I):
        return "chinese_subtitle"
    if re.search(r"流出|uncensored[ -]leak", title, re.I):
        return "uncensored_leak"
    return "original"


def _same_origin_url(value: str, origin: str) -> str | None:
    target = urljoin(origin + "/", value)
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https" or parsed.netloc != urlsplit(origin).netloc
        or parsed.username is not None or parsed.password is not None or parsed.fragment
    ):
        return None
    return target


def parse_catalog_search(source_id: str, document: str, origin: str, *, page: int = 1) -> CatalogPage:
    if source_id not in CANDIDATE_WEB_SOURCE_IDS:
        raise WebCatalogError("configuration")
    if _CHALLENGE.search(document):
        raise WebCatalogError("challenge_active", retryable=True)
    soup = BeautifulSoup(document, "html.parser")
    if source_id == JAVNONI_SITE_ID:
        # The site renders recommendations even on its explicit no-results page.
        # Only the search grid may contribute entries.
        if soup.select_one("body.search-no-results main"):
            return CatalogPage((), page)
        if not soup.select_one("body.search-results main"):
            raise WebCatalogError("response_invalid")
        cards = soup.select("main .movie-grid article.movie-card")
        anchors = [card.select_one("h2 a[href], h3 a[href]") for card in cards]
    else:
        container = soup.select_one("#list_videos_videos_list_search_result")
        if container is None:
            raise WebCatalogError("response_invalid")
        if container.select_one(".no-results, .no-result, .empty-content"):
            return CatalogPage((), page)
        anchors = container.select('a[href*="/video/"]')
        cards = anchors
    if not cards:
        raise WebCatalogError("response_invalid")
    entries = []
    seen = set()
    for anchor in anchors:
        if anchor is None:
            continue
        image = anchor.select_one("img[alt]")
        title = str(anchor.get("title") or anchor.get_text(" ", strip=True) or (image.get("alt") if image else ""))
        identity = _catalog_identity(title)
        variant = _variant(title)
        target = _same_origin_url(str(anchor.get("href") or ""), origin)
        if identity is None or variant is None or target is None:
            continue
        path = unquote(urlsplit(target).path)
        if source_id == JAVNONI_SITE_ID:
            if not path.startswith("/archives/") or path.startswith(("/archives/category/", "/archives/tag/")):
                continue
        elif re.match(r"^/video/\d+/", path) is None:
            continue
        key = (identity[1], variant, target)
        if key in seen:
            continue
        seen.add(key)
        entries.append(CatalogEntry(identity[0], title[:500], target, variant))
        if len(entries) > 128:
            raise WebCatalogError("response_invalid")
    total_pages = page
    for anchor in soup.select(".pagination a[href], .page-numbers[href], a[rel=next]"):
        target = _same_origin_url(str(anchor.get("href") or ""), origin)
        if target is None:
            continue
        parsed = urlsplit(target)
        match = re.fullmatch(r"/page/([1-9][0-9]{0,2})/", parsed.path)
        value = match.group(1) if match else parse_qs(parsed.query).get("from_videos", [""])[0]
        if value.isdigit() and 1 <= int(value) <= 999:
            total_pages = max(total_pages, int(value))
    return CatalogPage(tuple(entries), total_pages)


def _duration(value: object) -> float | None:
    text = str(value or "").strip()
    iso = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", text, re.I)
    try:
        if iso:
            result = sum(float(x or 0) * factor for x, factor in zip(iso.groups(), (3600, 60, 1)))
        elif re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", text):
            result = 0.0
            for part in text.split(":"):
                result = result * 60 + float(part)
        else:
            result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) and 60 <= result <= 24 * 3600 else None


def validate_catalog_detail(entry: CatalogEntry, document: str) -> float | None:
    if _CHALLENGE.search(document):
        raise WebCatalogError("challenge_active", retryable=True)
    soup = BeautifulSoup(document, "html.parser")
    heading = soup.select_one("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    actual = _catalog_identity(title)
    expected = normalize_catalog_code(entry.code)
    if actual is None or expected is None or actual[1] != expected[1] or _variant(title) != entry.variant:
        raise WebCatalogError("identity_mismatch")
    for meta in soup.select('meta[property="video:duration"], meta[itemprop="duration"]'):
        duration = _duration(meta.get("content"))
        if duration is not None:
            return duration
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        nodes = value if isinstance(value, list) else [value]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "VideoObject":
                duration = _duration(node.get("duration"))
                if duration is not None:
                    return duration
    element = soup.select_one(".duration")
    return _duration(element.get_text(strip=True)) if element else None


def extract_media_urls(document: str, page_url: str, *, unpack: Callable[[str], str | None] | None = None) -> tuple[str, ...]:
    """Extract declared MP4/HLS URLs; never return an iframe or execute JS."""
    soup = BeautifulSoup(document, "html.parser")
    values = [str(node.get("src") or "") for node in soup.select("video[src], video source[src]")]
    for script in soup.select("script"):
        text = script.get_text()
        if unpack is not None and "eval(function" in text:
            text += "\n" + (unpack(text) or "")
        text = html.unescape(text.replace("\\/", "/"))
        values.extend(_MEDIA.findall(text))
    result = []
    for value in values:
        target = urljoin(page_url, html.unescape(value))
        parsed = urlsplit(target)
        if (
            parsed.scheme == "https" and parsed.hostname and parsed.port in {None, 443}
            and parsed.username is None and parsed.password is None
            and parsed.path.lower().endswith((".m3u8", ".mp4"))
            and not _UNSUPPORTED_EDIT.search(parsed.path) and target not in result
        ):
            result.append(target)
    return tuple(result[:8])


def finite_playlist_duration(document: str) -> float:
    try:
        playlist = parse_media_playlist(document)
        duration = playlist.duration_seconds
    except ValueError as exc:
        raise WebCatalogError("media_unverified") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > 24 * 3600:
        raise WebCatalogError("media_unverified")
    return duration


def probe_catalog_media(
    source_id: str, entry: CatalogEntry, document: str, *, timeout: float,
    unpack: Callable[[str], str | None],
    select_playlist: Callable[[str, str], tuple[str, int | None]],
    fetch: Callable[..., str] = fetch_text,
) -> CatalogMedia:
    """Resolve a public player and validate its finite HLS, without media downloads."""
    if source_id not in CANDIDATE_WEB_SOURCE_IDS:
        raise WebCatalogError("configuration")
    deadline = time.monotonic() + max(1.0, min(timeout, 120.0))
    def read(url: str, *, max_bytes: int, referer: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebCatalogError("timeout", retryable=True)
        return fetch(url, timeout=remaining, max_bytes=max_bytes, headers={"Referer": referer})
    expected_duration = validate_catalog_detail(entry, document)
    pages = [(entry.detail_url, document)]
    soup = BeautifulSoup(document, "html.parser")
    for frame in soup.select("iframe[src], iframe[data-src]")[:3]:
        target = urljoin(entry.detail_url, str(frame.get("src") or frame.get("data-src") or ""))
        parsed = urlsplit(target)
        if parsed.scheme != "https" or parsed.hostname not in _PLAYER_HOSTS or parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}:
            continue
        try:
            body = read(target, max_bytes=_MAX_PAGE_BYTES, referer=entry.detail_url)
        except FetchError:
            continue
        pages.append((target, body))
    for page_url, body in pages:
        for media_url in extract_media_urls(body, page_url, unpack=unpack):
            parsed = urlsplit(media_url)
            host = parsed.hostname or ""
            if not any(host == suffix or host.endswith("." + suffix) for suffix in _MEDIA_SUFFIXES[source_id]):
                continue
            # MP4 candidates are deliberately not accepted without inspecting
            # their actual duration. A file extension or video MIME is not enough.
            if not parsed.path.lower().endswith(".m3u8"):
                continue
            try:
                master = read(media_url, max_bytes=2 * 1024 * 1024, referer=page_url)
                selected_url, height = select_playlist(media_url, master)
                selected = urlsplit(selected_url)
                if selected.scheme != "https" or selected.hostname != host or selected.username is not None or selected.password is not None or selected.port not in {None, 443}:
                    continue
                playlist = master if selected_url == media_url else read(selected_url, max_bytes=2 * 1024 * 1024, referer=page_url)
                duration = finite_playlist_duration(playlist)
                for segment in parse_media_playlist(playlist).segments:
                    segment_url = urlsplit(urljoin(selected_url, segment.uri))
                    if segment_url.scheme != "https" or segment_url.hostname != host or segment_url.username is not None or segment_url.password is not None or segment_url.port not in {None, 443}:
                        raise WebCatalogError("host_policy")
            except (FetchError, ValueError):
                continue
            complete = expected_duration is not None and abs(duration - expected_duration) <= max(8.0, expected_duration * 0.03)
            return CatalogMedia(selected_url, entry.detail_url, page_url, height, duration, expected_duration, complete)
    raise WebCatalogError("media_unverified")
