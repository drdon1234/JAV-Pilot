"""Uncensored studio rankings from the studios' own sites.

1pondo, 10musume and Pacopacomama share one platform that publishes weekly
and monthly ranking lists as JSON. Caribbeancom renders its weekly, monthly
and actress rankings as EUC-JP HTML.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ...indexers.metadata_catalog import release_date, text
from ...net.http_client import FetchError
from .common import (
    MAX_ITEMS,
    Item,
    RankingError,
    RankingRequest,
    fetch_page,
    https_url,
    person_item,
    rating_value,
    work_item,
)

SOURCE_ID = "ranking-uncensored"
_JSON_STUDIOS = {
    "1pondo": ("https://www.1pondo.tv", "一本道"),
    "10musume": ("https://www.10musume.com", "天然むすめ"),
    "pacopacomama": ("https://www.pacopacomama.com", "パコパコママ"),
}
_CARIB = "https://www.caribbeancom.com"
_MOVIE_ID_RE = re.compile(r"\d{6}[_-]\d{2,3}")
_CARIB_MOVIE_RE = re.compile(r"^/moviepages/(\d{6}-\d{3})/index\.html$")
_CARIB_ACTRESS_RE = re.compile(r"^/search_act/(\d{1,8})/1\.html$")
_WORKS_RE = re.compile(r"出演作品数:\s*(\d+)")


def works(request: RankingRequest) -> list[Item]:
    if request.category == "caribbeancom":
        return _carib_works(request.period)
    origin, studio = _JSON_STUDIOS[request.category]
    try:
        payload = json.loads(fetch_page(f"{origin}/dyn/phpauto/movie_lists/list_{request.period}_0.json"))
    except (FetchError, ValueError) as exc:
        raise RankingError(f"{studio} ranking is temporarily unavailable", code="unavailable") from exc
    rows = payload.get("Rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RankingError(f"{studio} ranking list is missing", code="unavailable")
    items: list[Item] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        movie_id = text(row.get("MovieID"), 20)
        if not _MOVIE_ID_RE.fullmatch(movie_id):
            continue
        actor = text(row.get("Actor"), 80)
        items.append(
            work_item(
                len(items) + 1,
                source_id=SOURCE_ID,
                code=movie_id,
                title=text(row.get("Title"), 300),
                subtitle=" · ".join(part for part in (studio, actor) if part),
                release_date=release_date(row.get("Release")),
                detail_url=f"{origin}/movies/{movie_id}/",
                cover=https_url(row.get("ThumbHigh") or row.get("MovieThumb"), origin + "/"),
                rating=rating_value(row.get("AvgRating")),
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError(f"{studio} ranking had no works", code="unavailable")
    return items


def carib_actresses(request: RankingRequest) -> list[Item]:
    soup = _carib_page("/ranking/actress.htm")
    items: list[Item] = []
    for entry in soup.select(".is-actress-ranking a.entry[href]"):
        match = _CARIB_ACTRESS_RE.match(str(entry.get("href")))
        title = entry.select_one(".meta-title")
        name = text(title.get_text(), 80) if title else ""
        if match is None or not name:
            continue
        meta = " ".join(node.get_text() for node in entry.select(".meta-data"))
        count = _WORKS_RE.search(meta)
        image = entry.select_one("img.media-image")
        items.append(
            person_item(
                len(items) + 1,
                source_id=SOURCE_ID,
                name=name,
                subtitle=f"加勒比出演 {count.group(1)} 部" if count else None,
                detail_url=f"{_CARIB}/search_act/{match.group(1)}/1.html",
                cover=https_url(image.get("src") if image else None, _CARIB + "/"),
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError("Caribbeancom actress ranking had no entries", code="unavailable")
    return items


def _carib_works(period: str) -> list[Item]:
    soup = _carib_page(f"/ranking/{period}.html")
    items: list[Item] = []
    for entry in soup.select(".is-movie-ranking a.entry[href]"):
        match = _CARIB_MOVIE_RE.match(str(entry.get("href")))
        title = entry.select_one(".meta-title")
        if match is None or title is None:
            continue
        meta = [text(node.get_text(), 80) for node in entry.select(".meta-data")]
        stars = entry.select_one(".meta-rating")
        rating = stars.get_text().count("★") if stars else 0
        image = entry.select_one("img.media-image")
        movie_id = match.group(1)
        items.append(
            work_item(
                len(items) + 1,
                source_id=SOURCE_ID,
                code=movie_id,
                title=text(title.get_text(), 300),
                subtitle=" · ".join(part for part in ("加勒比", meta[1] if len(meta) > 1 else "") if part),
                release_date=release_date(meta[0]) if meta else None,
                detail_url=f"{_CARIB}/moviepages/{movie_id}/index.html",
                cover=https_url(image.get("src") if image else None, _CARIB + "/"),
                rating=float(rating) if rating else None,
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError("Caribbeancom ranking had no works", code="unavailable")
    return items


def _carib_page(path: str) -> BeautifulSoup:
    try:
        html = fetch_page(_CARIB + path)
    except FetchError as exc:
        raise RankingError("Caribbeancom ranking is temporarily unavailable", code="unavailable") from exc
    return BeautifulSoup(html, "html.parser")

