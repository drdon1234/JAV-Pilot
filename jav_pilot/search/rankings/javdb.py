"""JavDB work and actor rankings, fetched through the configured JavDB source.

JavDB publishes daily, weekly and monthly work lists plus a monthly actor
list. The censored work lists and all actor lists are public; the
uncensored, western and FC2 work lists require a signed-in session and are
reported as such instead of looking empty.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ...config.settings import load_settings
from ...core.models import SearchBounds
from ...indexers.html_parsers import JavDbSearchParser
from ...indexers.javdb import JavDbIndexer
from ...indexers.metadata_catalog import text
from ...net.browser_fetcher import BrowserFetchError
from ...net.http_client import FetchError
from ..engine import default_indexers
from .common import (
    MAX_ITEMS,
    Item,
    RankingError,
    RankingRequest,
    https_url,
    person_item,
    work_item,
)


def configured_indexer() -> JavDbIndexer:
    indexer = next(
        (item for item in default_indexers(load_settings()).values() if isinstance(item, JavDbIndexer)),
        None,
    )
    if indexer is None:
        raise RankingError("JavDB source is not enabled", code="source_disabled")
    return indexer


def javdb_scope() -> str:
    try:
        return configured_indexer().base_url
    except RankingError:
        return ""


def movies(request: RankingRequest) -> list[Item]:
    indexer = configured_indexer()
    url = f"{indexer.base_url}/rankings/movies?{urlencode({'p': request.period, 't': request.category})}"
    html = _fetch(indexer, url)
    parser = JavDbSearchParser(indexer.base_url, MAX_ITEMS)
    parser.feed(html)
    results = [replace(result, source=indexer.source_id) for result in parser.results()]
    if not results:
        # Only the censored lists are public; the others answer with a sign-in
        # page, which the browser fallback fetches without an error status.
        raise RankingError(
            "JavDB ranking page had no works",
            code="unavailable" if request.category == "censored" else "login_required",
        )
    items = []
    for rank, result in enumerate(results[:MAX_ITEMS], start=1):
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        rating = result.details.rating if result.details else None
        items.append(
            work_item(
                rank,
                source_id=indexer.source_id,
                code=result.code,
                title=result.title,
                release_date=result.date,
                detail_url=result.url,
                cover=metadata.get("cover"),
                rating=rating.value if rating else None,
                votes=rating.votes if rating else None,
            )
        )
    return items


def actors(request: RankingRequest) -> list[Item]:
    indexer = configured_indexer()
    url = f"{indexer.base_url}/rankings/actors?{urlencode({'t': request.category})}"
    soup = BeautifulSoup(_fetch(indexer, url), "html.parser")
    items: list[Item] = []
    for anchor in soup.select("#actors .actor-box a[href^='/actors/']"):
        name = text(anchor.select_one("strong").get_text() if anchor.select_one("strong") else "", 80)
        if not name:
            continue
        image = anchor.select_one("img")
        aliases = text(anchor.get("title"), 160)
        items.append(
            person_item(
                len(items) + 1,
                source_id=indexer.source_id,
                name=name,
                subtitle=aliases if aliases and aliases != name else None,
                detail_url=https_url(anchor.get("href"), indexer.base_url + "/"),
                cover=https_url(image.get("src") if image else None, indexer.base_url + "/"),
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError("JavDB actor ranking page had no actors", code="unavailable")
    return items


def _fetch(indexer: JavDbIndexer, url: str) -> str:
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
    except BrowserFetchError as exc:
        raise RankingError("JavDB ranking is temporarily unavailable", code="unavailable") from exc
    return html
