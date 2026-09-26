"""Rankings from JavDB, fetched through the configured JavDB source.

JavDB publishes daily, weekly and monthly lists. The censored lists are
public; the uncensored, western and FC2 lists require a signed-in session and
are reported as such instead of looking empty.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlencode

from ..net.http_client import FetchError
from ..indexers.html_parsers import JavDbSearchParser
from ..indexers.javdb import JavDbIndexer
from ..core.models import SearchBounds
from ..config.settings import load_settings
from .engine import default_indexers

RANKING_PERIODS = ("daily", "weekly", "monthly")
RANKING_TYPES = ("censored", "uncensored", "western", "fc2")
_CACHE_SECONDS = 30 * 60
_MAX_ITEMS = 100
_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


class RankingError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RankingRequest:
    period: str
    kind: str

    def __post_init__(self) -> None:
        if self.period not in RANKING_PERIODS or self.kind not in RANKING_TYPES:
            raise RankingError("ranking request is invalid", code="invalid")


def javdb_ranking(request: RankingRequest, *, refresh: bool = False) -> dict[str, Any]:
    settings = load_settings()
    indexer = next(
        (item for item in default_indexers(settings).values() if isinstance(item, JavDbIndexer)),
        None,
    )
    if indexer is None:
        raise RankingError("JavDB source is not enabled", code="source_disabled")
    key = (indexer.base_url, request.period, request.kind)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and not refresh and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    payload = _fetch_ranking(indexer, request)
    with _CACHE_LOCK:
        _CACHE[key] = (now, payload)
        for stale in [item for item, (at, _) in _CACHE.items() if now - at > _CACHE_SECONDS * 4]:
            _CACHE.pop(stale, None)
    return payload


def _fetch_ranking(indexer: JavDbIndexer, request: RankingRequest) -> dict[str, Any]:
    url = f"{indexer.base_url}/rankings/movies?{urlencode({'p': request.period, 't': request.kind})}"
    bounds = SearchBounds(limit=50, fetch_magnets=False, detail_limit=0).normalized()
    try:
        html = indexer._fetch_search_html(url, bounds)
        if html is None:
            with indexer._make_browser_fetcher(bounds) as browser:
                html = browser.fetch(url)
    except FetchError as exc:
        text = str(exc).lower()
        if "403" in text or "401" in text:
            raise RankingError("this JavDB ranking requires a signed-in session", code="login_required") from exc
        raise RankingError("JavDB ranking is temporarily unavailable", code="unavailable") from exc
    parser = JavDbSearchParser(indexer.base_url, _MAX_ITEMS)
    parser.feed(html)
    results = [replace(result, source=indexer.source_id) for result in parser.results()]
    if not results:
        # Only the censored lists are public; the others answer with a sign-in
        # page, which the browser fallback fetches without an error status.
        raise RankingError(
            "JavDB ranking page had no works",
            code="unavailable" if request.kind == "censored" else "login_required",
        )
    items = []
    for rank, result in enumerate(results[:_MAX_ITEMS], start=1):
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        rating = result.details.rating if result.details else None
        items.append(
            {
                "rank": rank,
                "code": result.code,
                "title": result.title,
                "release_date": result.date,
                "detail_url": result.url,
                "cover": metadata.get("cover"),
                "rating": rating.value if rating else None,
                "votes": rating.votes if rating else None,
                "source_id": indexer.source_id,
            }
        )
    return {
        "source_id": indexer.source_id,
        "period": request.period,
        "type": request.kind,
        "items": items,
        "fetched_at": time.time(),
    }
