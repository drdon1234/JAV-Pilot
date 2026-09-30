"""Xunlei's public subtitle lookup: JSON search, files on a separate CDN host."""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from ...net.http_client import FetchError, fetch_bytes, fetch_text
from ..models import FORMATS, MAX_DURATION_MS, MAX_SUBTITLE_BYTES, SubtitleCandidate
from .base import ProviderError, Throttle, no_throttle, provider_error

XUNLEI_DOWNLOAD_ORIGINS = ("https://subtitle.v.geilijiasu.com",)
MAX_SEARCH_BYTES = 512 * 1024
MAX_RESULTS = 20


class XunleiProvider:
    provider_id = "xunlei"

    def __init__(self, base_url: str, *, throttle: Throttle = no_throttle) -> None:
        self.base_url = base_url.rstrip("/")
        self._throttle = throttle

    def search(self, code: str, *, timeout: float) -> list[SubtitleCandidate]:
        url = f"{self.base_url}/oracle/subtitle?name={quote(code, safe='')}"
        self._throttle()
        try:
            body = fetch_text(
                url,
                timeout=timeout,
                max_bytes=MAX_SEARCH_BYTES,
                headers={"Accept": "application/json"},
                allowed_origin=self.base_url,
            )
        except FetchError as exc:
            raise provider_error(exc) from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError("parse", "subtitle search response is not JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("code") != 0
            or not isinstance(payload.get("data"), list)
        ):
            raise ProviderError("parse", "subtitle search response shape changed")
        candidates: list[SubtitleCandidate] = []
        seen: set[str] = set()
        for item in payload["data"][:MAX_RESULTS]:
            candidate = _candidate(item)
            if candidate is not None and candidate.download_url not in seen:
                seen.add(candidate.download_url)
                candidates.append(candidate)
        return candidates

    def fetch(self, candidate: SubtitleCandidate, *, timeout: float) -> bytes:
        self._throttle()
        try:
            return fetch_bytes(
                candidate.download_url,
                timeout=timeout,
                max_bytes=MAX_SUBTITLE_BYTES,
                allowed_origins=XUNLEI_DOWNLOAD_ORIGINS,
                headers={"Accept": "*/*"},
            )
        except FetchError as exc:
            raise provider_error(exc) from exc


def _candidate(item: object) -> SubtitleCandidate | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    name = item.get("name")
    if not isinstance(url, str) or not isinstance(name, str) or not name.strip():
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if f"{parsed.scheme}://{parsed.netloc}".lower() not in XUNLEI_DOWNLOAD_ORIGINS:
        return None
    ext = str(item.get("ext") or "").strip().lower()
    duration = item.get("duration")
    duration_ms = (
        duration
        if isinstance(duration, int)
        and not isinstance(duration, bool)
        and 0 < duration <= MAX_DURATION_MS
        else None
    )
    return SubtitleCandidate(
        provider="xunlei",
        file_name=name.strip()[:255],
        download_url=url,
        format=ext if ext in FORMATS else None,
        declared_script=_declared_script(item.get("languages")),
        duration_ms=duration_ms,
        machine_translated=False,
    )


def _declared_script(languages: object) -> str | None:
    if not isinstance(languages, list):
        return None
    scripts: set[str] = set()
    for value in languages:
        if not isinstance(value, str):
            continue
        if "繁" in value:
            scripts.add("zh-TW")
        if "简" in value:
            scripts.add("zh-CN")
    return scripts.pop() if len(scripts) == 1 else None
