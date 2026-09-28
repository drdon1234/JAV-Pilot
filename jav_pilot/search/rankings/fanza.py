"""FANZA rankings from the public video.dmm.co.jp GraphQL API.

The ranking queries answer from any region (only purchase and playback are
restricted outside Japan). Work lists cover realtime, daily, weekly and
monthly sales; the actress list is monthly only. FANZA has no time-windowed
genre ranking, so the genre board sorts each genre's catalogue by FANZA's
cumulative popularity, sales or review score.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ...indexers.fanza import fanza_display_code
from ...indexers.metadata_catalog import release_date, text
from ...net.http_client import FetchError
from .common import (
    MAX_ITEMS,
    Item,
    Option,
    RankingError,
    RankingRequest,
    count_value,
    fetch_page,
    https_url,
    person_item,
    rating_value,
    work_item,
)

SOURCE_ID = "ranking-fanza"
_API = "https://api.video.dmm.co.jp/graphql"
_SITE = "https://video.dmm.co.jp/"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": _SITE,
    "Origin": _SITE.rstrip("/"),
    "Fanza-Device": "BROWSER",
}
_CID_RE = re.compile(r"(?:h_\d+|\d+)?([a-z]{2,10})0*(\d{2,6})([a-z]?)")
_FLOOR_PATHS = {"AV": "av", "AMATEUR": "amateur", "ANIME": "anime"}
_JST = timezone(timedelta(hours=9))
_PERIOD_FILTERS = {"realtime": "trending", "daily": "daily", "weekly": "weekly", "monthly": "monthly"}
GENRE_SORTS = {"popular": "SALES_RANK_SCORE", "sales": "SALES_COUNT", "review": "REVIEW_RANK_SCORE"}

_WORKS_QUERY = """query Ranking($filter: PPVContentRankingFilterInput, $limit: Int!, $amateur: Boolean!) {
  ppvContentRanking(limit: $limit, offset: 0, filter: $filter) {
    items {
      id rank
      content {
        title deliveryStartDate
        packageImage { largeUrl mediumUrl }
        review { average total }
        actresses @skip(if: $amateur) { name }
      }
    }
  }
}"""
_ACTRESS_QUERY = """query ActressRanking($limit: Int!) {
  ppvActressRanking(limit: $limit, offset: 0, filter: {monthly: {floor: AV}}) {
    items { rank actress { id name imageUrl contentsCountOnSale } }
  }
}"""
_GENRE_QUERY = """query GenreRanking($sort: ContentSearchPPVSort!, $filter: ContentSearchPPVFilterInput, $limit: Int!) {
  legacySearchPPV(limit: $limit, offset: 0, floor: AV, sort: $sort, filter: $filter,
                  facetLimit: 1, includeExplicit: true, excludeUndelivered: true) {
    result {
      contents {
        id title deliveryStartAt
        packageImage { largeUrl mediumUrl }
        review { average count }
        actresses { name }
      }
    }
  }
}"""
_GENRE_LIST_QUERY = """query Genres {
  legacySearchPPV(limit: 1, offset: 0, floor: AV, sort: SALES_RANK_SCORE,
                  facetLimit: 300, includeExplicit: true, excludeUndelivered: true) {
    result { facet { ... on PPVContentSearchFacet { genre { items { id name count } } } } }
  }
}"""
# Format and delivery tags describe nearly every work and make poor boards.
_TECHNICAL_GENRES = {"ハイビジョン", "独占配信", "4K", "8KVR", "ハイクオリティVR", "VR専用", "単体作品", "デビュー作品", "サンプル動画", "4時間以上作品", "ベスト・総集編", "16時間以上作品"}


def ranking_code(cid: str) -> str | None:
    """Display code for a FANZA content id, dropping vendor number prefixes."""
    code = fanza_display_code(cid)
    if code:
        return code
    match = _CID_RE.fullmatch(cid.lower())
    if not match:
        return None
    return f"{match.group(1).upper()}-{int(match.group(2)):03d}{match.group(3).upper()}"


def works(request: RankingRequest) -> list[Item]:
    floor = request.category
    payload = _query(
        _WORKS_QUERY,
        {
            "filter": {_PERIOD_FILTERS[request.period]: {"floor": floor}},
            "limit": MAX_ITEMS,
            "amateur": floor == "AMATEUR",
        },
    )
    rows = (payload.get("ppvContentRanking") or {}).get("items")
    if not isinstance(rows, list):
        raise RankingError("FANZA ranking list is missing", code="unavailable")
    items = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("content"), dict):
            continue
        cid = text(row.get("id"), 60)
        content = row["content"]
        review = content.get("review") if isinstance(content.get("review"), dict) else {}
        items.append(
            _work(
                len(items) + 1,
                cid,
                content,
                detail_path=_FLOOR_PATHS.get(floor, "av"),
                release=content.get("deliveryStartDate"),
                rating=review.get("average"),
                votes=review.get("total"),
            )
        )
    return _require(items)


def actresses(request: RankingRequest) -> list[Item]:
    payload = _query(_ACTRESS_QUERY, {"limit": MAX_ITEMS})
    rows = (payload.get("ppvActressRanking") or {}).get("items")
    if not isinstance(rows, list):
        raise RankingError("FANZA actress ranking is missing", code="unavailable")
    items = []
    for row in rows:
        actress = row.get("actress") if isinstance(row, dict) else None
        if not isinstance(actress, dict):
            continue
        name = text(actress.get("name"), 80)
        actress_id = text(actress.get("id"), 20)
        if not name or not actress_id.isdigit():
            continue
        works_on_sale = count_value(actress.get("contentsCountOnSale"))
        items.append(
            person_item(
                len(items) + 1,
                source_id=SOURCE_ID,
                name=name,
                subtitle=f"在售作品 {works_on_sale} 部" if works_on_sale else None,
                detail_url=f"{_SITE}av/list/?actress={actress_id}",
                cover=https_url(actress.get("imageUrl"), _SITE),
            )
        )
    return _require(items)


def genre_works(request: RankingRequest) -> list[Item]:
    if not request.category.isdigit():
        raise RankingError("FANZA genre is invalid", code="invalid")
    payload = _query(
        _GENRE_QUERY,
        {
            "sort": GENRE_SORTS[request.period],
            "filter": {"genreIds": {"ids": [{"id": request.category}], "op": "AND"}},
            "limit": MAX_ITEMS,
        },
    )
    result = ((payload.get("legacySearchPPV") or {}).get("result")) or {}
    rows = result.get("contents")
    if not isinstance(rows, list):
        raise RankingError("FANZA genre list is missing", code="unavailable")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        review = row.get("review") if isinstance(row.get("review"), dict) else {}
        items.append(
            _work(
                len(items) + 1,
                text(row.get("id"), 60),
                row,
                detail_path="av",
                release=row.get("deliveryStartAt"),
                rating=review.get("average"),
                votes=review.get("count"),
            )
        )
    return _require(items)


def genres() -> tuple[Option, ...]:
    payload = _query(_GENRE_LIST_QUERY, {})
    result = ((payload.get("legacySearchPPV") or {}).get("result")) or {}
    rows = (((result.get("facet") or {}).get("genre")) or {}).get("items")
    if not isinstance(rows, list):
        raise RankingError("FANZA genre list is missing", code="unavailable")
    options = []
    for row in sorted((row for row in rows if isinstance(row, dict)), key=lambda row: -(count_value(row.get("count")) or 0)):
        genre_id = text(row.get("id"), 12)
        name = text(row.get("name"), 40)
        if genre_id.isdigit() and name and name not in _TECHNICAL_GENRES:
            options.append(Option(genre_id, name))
    if not options:
        raise RankingError("FANZA genre list is empty", code="unavailable")
    return tuple(options)


def _work(
    rank: int,
    cid: str,
    content: dict[str, Any],
    *,
    detail_path: str,
    release: object,
    rating: object,
    votes: object,
) -> Item:
    package = content.get("packageImage") if isinstance(content.get("packageImage"), dict) else {}
    actresses_list = content.get("actresses") if isinstance(content.get("actresses"), list) else []
    names = [text(actor.get("name"), 40) for actor in actresses_list if isinstance(actor, dict)]
    names = [name for name in names if name]
    subtitle = "、".join(names[:4]) + (f" 等 {len(names)} 人" if len(names) > 4 else "") if names else None
    return work_item(
        rank,
        source_id=SOURCE_ID,
        code=ranking_code(cid) if cid else None,
        title=text(content.get("title"), 300),
        subtitle=subtitle,
        release_date=_japan_date(release),
        detail_url=f"{_SITE}{detail_path}/content/?id={cid}" if re.fullmatch(r"[a-z0-9_]{1,60}", cid) else None,
        cover=https_url(package.get("largeUrl") or package.get("mediumUrl"), _SITE),
        rating=rating_value(rating),
        votes=count_value(votes),
    )


def _japan_date(value: object) -> str | None:
    """FANZA times are instants (often midnight in Japan as 15:00 UTC); show Japan's date."""
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            return moment.astimezone(_JST).date().isoformat()
    return release_date(value)


def _query(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = fetch_page(
            _API,
            headers=_HEADERS,
            data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        )
        payload = json.loads(raw)
    except (FetchError, ValueError) as exc:
        raise RankingError("FANZA ranking is temporarily unavailable", code="unavailable") from exc
    if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
        raise RankingError("FANZA ranking answered with an error", code="unavailable")
    return payload["data"]


def _require(items: list[Item]) -> list[Item]:
    if not items:
        raise RankingError("FANZA ranking had no entries", code="unavailable")
    return items
