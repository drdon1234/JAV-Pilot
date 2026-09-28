"""JavMenu censored rankings, a public fallback when JavDB is rate limited.

JavMenu mirrors JavDB's ranking categories, but only its censored lists stay
current (its uncensored and FC2 lists stopped updating), so only those are
offered.
"""

from __future__ import annotations

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
    work_item,
)

SOURCE_ID = "ranking-javmenu"
_ORIGIN = "https://javmenu.com"
_PERIOD_PATHS = {"daily": "day", "weekly": "week", "monthly": "month"}
_DETAIL_RE = re.compile(r"^https://javmenu\.com/zh/([A-Za-z0-9]{2,12}-\d{2,6}[A-Za-z]?)$")


def works(request: RankingRequest) -> list[Item]:
    try:
        html = fetch_page(f"{_ORIGIN}/zh/rank/censored/{_PERIOD_PATHS[request.period]}")
    except FetchError as exc:
        raise RankingError("JavMenu ranking is temporarily unavailable", code="unavailable") from exc
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    for card in soup.select(".card"):
        anchor = card.select_one(".card-body a[href]")
        match = _DETAIL_RE.match(str(anchor.get("href") if anchor else ""))
        if match is None:
            continue
        title = card.select_one(".card-text")
        date = card.select_one(".card-body .text-muted")
        image = card.select_one("img.card-img-top")
        items.append(
            work_item(
                len(items) + 1,
                source_id=SOURCE_ID,
                code=match.group(1).upper(),
                title=text(title.get("title") or title.get_text(), 300) if title else "",
                release_date=release_date(date.get_text()) if date else None,
                detail_url=f"{_ORIGIN}/zh/{match.group(1)}",
                cover=https_url(image.get("data-src") if image else None, _ORIGIN + "/"),
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError("JavMenu ranking page had no works", code="unavailable")
    return items
