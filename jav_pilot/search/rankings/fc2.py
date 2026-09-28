"""FC2 Contents Market rankings from the official adult.contents.fc2.com pages.

Work rankings (realtime to yearly) and seller rankings (last month, last year)
list 100 entries over five pages of twenty. Later pages are fetched in
parallel, and the list ends at the first of them that fails.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup, Tag

from ...indexers.metadata_catalog import text
from ...net.http_client import FetchError
from .common import (
    MAX_ITEMS,
    Item,
    RankingError,
    RankingRequest,
    fetch_page,
    https_url,
    person_item,
    work_item,
)

SOURCE_ID = "ranking-fc2"
_ORIGIN = "https://adult.contents.fc2.com"
_PAGES = 5
_ARTICLE_RE = re.compile(r"/article_search\.php\?id=(\d{2,9})$")
# Sellers without a public profile link to their product search instead.
_USER_RE = re.compile(
    r"^(?:https:)?//adult\.contents\.fc2\.com/(?:users/([A-Za-z0-9_-]{1,64})/|search/\?author_id=([A-Za-z0-9_-]{1,64}))$"
)
_COUNT_RE = re.compile(r"\((\d+)\)")
SELLER_PERIODS = {"monthly": "/ranking/writer/", "yearly": "/ranking/writer/yearly"}


def works(request: RankingRequest) -> list[Item]:
    return _paged(f"{_ORIGIN}/ranking/article/{request.period}", _work)


def sellers(request: RankingRequest) -> list[Item]:
    return _paged(f"{_ORIGIN}{SELLER_PERIODS[request.period]}", _seller)


def _paged(url: str, parse: Callable[[Tag, int], Item | None]) -> list[Item]:
    try:
        first = fetch_page(url)
    except FetchError as exc:
        raise RankingError("FC2 ranking is temporarily unavailable", code="unavailable") from exc
    pages = [first]
    with ThreadPoolExecutor(max_workers=_PAGES - 1) as pool:
        futures = [pool.submit(fetch_page, f"{url}?page={page}") for page in range(2, _PAGES + 1)]
        for future in futures:
            try:
                pages.append(future.result())
            except FetchError:
                break
    items: list[Item] = []
    for html in pages:
        rows = BeautifulSoup(html, "html.parser").select(".c-rankbox-100 > .c-ranklist-110")
        for row in rows:
            item = parse(row, len(items) + 1)
            if item is not None:
                items.append(item)
        if len(rows) < 20 or len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError("FC2 ranking page had no entries", code="unavailable")
    return items[:MAX_ITEMS]


def _work(row: Tag, rank: int) -> Item | None:
    anchor = row.select_one(".c-ranklist-110_info h3 a[href]")
    match = _ARTICLE_RE.search(str(anchor.get("href") if anchor else ""))
    if anchor is None or match is None:
        return None
    product_id = match.group(1)
    image = row.select_one(".c-ranklist-110_tmb img")
    seller = row.select(".c-ranklist-110_own a")
    return work_item(
        rank,
        source_id=SOURCE_ID,
        code=f"FC2-PPV-{product_id}",
        title=text(anchor.get_text(), 300),
        subtitle=text(seller[-1].get_text(), 60) if seller else None,
        detail_url=f"{_ORIGIN}/article/{product_id}/",
        cover=https_url(image.get("src") if image else None, _ORIGIN + "/"),
    )


def _seller(row: Tag, rank: int) -> Item | None:
    anchor = row.select_one(".c-ranklist-130 h3 a[href]")
    match = _USER_RE.match(str(anchor.get("href") if anchor else ""))
    name = text(anchor.get_text(), 80) if anchor else ""
    if match is None or not name:
        return None
    image = row.select_one(".c-rankwriter-100 img")
    listing = row.select_one("a[href^='/search/?author_id=']")
    count = _COUNT_RE.search(listing.get_text() if listing else "")
    description = text(row.select_one(".c-ranklist-130_dis").get_text(), 80) if row.select_one(".c-ranklist-130_dis") else ""
    parts = [f"作品 {count.group(1)} 部" if count else "", description]
    return person_item(
        rank,
        source_id=SOURCE_ID,
        name=name,
        subtitle=" · ".join(part for part in parts if part) or None,
        detail_url=f"{_ORIGIN}/users/{match.group(1)}/" if match.group(1) else f"{_ORIGIN}/search/?author_id={match.group(2)}",
        cover=https_url(image.get("src") if image else None, _ORIGIN + "/"),
    )
