"""MGStage rankings (overall and per channel) from www.mgstage.com.

MGStage only serves Japanese IP addresses; elsewhere it answers 403, which is
reported as ``region_restricted`` so the page can explain the proxy need.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ...indexers.metadata_catalog import text
from ...net.http_client import FetchError
from .common import (
    MAX_ITEMS,
    Item,
    RankingError,
    RankingRequest,
    fetch_page,
    http_status,
    https_url,
    work_item,
)

SOURCE_ID = "ranking-mgs"
_ORIGIN = "https://www.mgstage.com"
_PERIOD_IDS = {"daily": "day", "weekly": "week", "monthly": "month"}
CHANNELS = {"all": "", "mgs": "mgs_", "shiroutotv": "shiroutotv_", "nanpatv": "nanpatv_", "luxutv": "luxutv_"}
# Single-purchase works live under /product/; subscription channels under /monthly/<channel>/video/.
_DETAIL_RE = re.compile(r"^/(?:product/product_detail|monthly/[a-z0-9]{1,30}/video)/([A-Za-z0-9_-]{2,40})/?$")
_RANK_RE = re.compile(r"(\d+)\s*位")


def works(request: RankingRequest) -> list[Item]:
    url = f"{_ORIGIN}/ranking/ranking.php?id={CHANNELS[request.category]}{_PERIOD_IDS[request.period]}"
    try:
        html = fetch_page(url, headers={"Cookie": "adc=1"})
    except FetchError as exc:
        if http_status(exc) == 403:
            raise RankingError("MGStage only serves Japanese IP addresses", code="region_restricted") from exc
        raise RankingError("MGStage ranking is temporarily unavailable", code="unavailable") from exc
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    seen: set[str] = set()
    for row in soup.select("#center_column .rank_list_detail li"):
        anchor = row.select_one("h5 a[href]")
        match = _DETAIL_RE.match(str(anchor.get("href") if anchor else ""))
        if anchor is None or match is None or match.group(1).upper() in seen:
            continue
        code = match.group(1).upper()
        seen.add(code)
        rank_text = row.select_one("[class^='ranking_']")
        rank = _RANK_RE.search(rank_text.get_text() if rank_text else "")
        image = row.select_one("h6 img")
        performer = next(
            (text(node.get_text(), 80).split("：", 1)[-1] for node in row.select("p.data") if "出演者" in node.get_text()),
            "",
        )
        items.append(
            work_item(
                int(rank.group(1)) if rank else len(items) + 1,
                source_id=SOURCE_ID,
                code=code,
                title=text(anchor.get_text(), 300).removesuffix("...").strip(),
                subtitle=performer if performer and performer != "----" else None,
                detail_url=https_url(anchor.get("href"), _ORIGIN + "/"),
                cover=https_url(image.get("src") if image else None, _ORIGIN + "/"),
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RankingError("MGStage ranking page had no works", code="unavailable")
    return items
