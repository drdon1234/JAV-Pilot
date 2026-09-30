"""SubtitleCat: listing pages with machine-translated subtitles per language."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ...net.http_client import FetchError, fetch_bytes, fetch_text
from ..matching import code_matches
from ..models import MAX_SUBTITLE_BYTES, SCRIPTS, SubtitleCandidate
from .base import ProviderError, Throttle, no_throttle, provider_error

MAX_PAGE_BYTES = 1024 * 1024
MAX_DETAIL_PAGES = 3
_RESULT_MARKER = "subtitles found"


class SubtitleCatProvider:
    provider_id = "subtitlecat"

    def __init__(self, base_url: str, *, throttle: Throttle = no_throttle) -> None:
        self.base_url = base_url.rstrip("/")
        self._throttle = throttle

    def search(self, code: str, *, timeout: float) -> list[SubtitleCandidate]:
        page = self._page(f"{self.base_url}/index.php?search={quote(code, safe='')}", timeout)
        if _RESULT_MARKER not in page:
            raise ProviderError("parse", "subtitle search page structure changed")
        details: list[str] = []
        for link in BeautifulSoup(page, "html.parser").select("table tr td a[href]"):
            href = str(link.get("href") or "")
            if not href.lstrip("/").startswith("subs/") or not href.endswith(".html"):
                continue
            if not code_matches(link.get_text(" ", strip=True), code):
                continue
            url = urljoin(f"{self.base_url}/", href)
            if url not in details:
                details.append(url)
            if len(details) >= MAX_DETAIL_PAGES:
                break
        candidates: list[SubtitleCandidate] = []
        seen: set[str] = set()
        last_error: ProviderError | None = None
        succeeded = 0
        for url in details:
            try:
                detail = self._page(url, timeout)
            except ProviderError as exc:
                last_error = exc
                continue
            succeeded += 1
            for candidate in self._detail_candidates(detail):
                if candidate.download_url not in seen:
                    seen.add(candidate.download_url)
                    candidates.append(candidate)
        if details and not succeeded and last_error is not None:
            raise last_error
        return candidates

    def fetch(self, candidate: SubtitleCandidate, *, timeout: float) -> bytes:
        self._throttle()
        try:
            return fetch_bytes(
                candidate.download_url,
                timeout=timeout,
                max_bytes=MAX_SUBTITLE_BYTES,
                allowed_origins=(self.base_url,),
            )
        except FetchError as exc:
            raise provider_error(exc) from exc

    def _detail_candidates(self, page: str) -> list[SubtitleCandidate]:
        soup = BeautifulSoup(page, "html.parser")
        found: list[SubtitleCandidate] = []
        for script in SCRIPTS:
            link = soup.find("a", id=f"download_{script}")
            href = str(link.get("href") or "") if link is not None else ""
            if not href.lower().endswith(".srt"):
                continue
            url = urljoin(f"{self.base_url}/", href)
            if _origin(url) != _origin(self.base_url):
                continue
            name = unquote(PurePosixPath(urlsplit(url).path).name)
            found.append(
                SubtitleCandidate(
                    provider="subtitlecat",
                    file_name=name[:255],
                    download_url=url,
                    format="srt",
                    declared_script=script,
                    duration_ms=None,
                    machine_translated=True,
                )
            )
        return found

    def _page(self, url: str, timeout: float) -> str:
        self._throttle()
        try:
            return fetch_text(
                url,
                timeout=timeout,
                max_bytes=MAX_PAGE_BYTES,
                allowed_origin=self.base_url,
            )
        except FetchError as exc:
            raise provider_error(exc) from exc


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()
