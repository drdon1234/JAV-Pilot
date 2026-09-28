"""Ranking boards from JavDB, FANZA, FC2, MGStage, uncensored studios and JavMenu.

Every board is a fixed first-party list fetched through the app's proxy.
Lists are cached for 30 minutes and dynamic category lists (FANZA genres)
for 12 hours; ``refresh`` bypasses the list cache.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import fanza, fc2, javdb, javmenu, mgs, studios
from .common import Board, Option, RankingError, RankingRequest

__all__ = ["RankingError", "RankingRequest", "fetch_ranking", "ranking_boards"]

LOGGER = logging.getLogger(__name__)

_CACHE_SECONDS = 30 * 60
_CATEGORY_CACHE_SECONDS = 12 * 60 * 60
_CACHE: dict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = {}
_CATEGORY_CACHE: dict[str, tuple[float, tuple[Option, ...]]] = {}
_CACHE_LOCK = threading.Lock()

_DAILY = Option("daily", "日榜")
_WEEKLY = Option("weekly", "周榜")
_MONTHLY = Option("monthly", "月榜")
_REALTIME = Option("realtime", "实时")
_YEARLY = Option("yearly", "年榜")
_JAVDB_TYPES = (Option("censored", "有码"), Option("uncensored", "无码"), Option("western", "欧美"))

BOARDS: tuple[Board, ...] = (
    Board(
        id="javdb", label="JavDB", group="works", item_kind="work", source="JavDB",
        periods=(_DAILY, _WEEKLY, _MONTHLY), categories=(*_JAVDB_TYPES, Option("fc2", "FC2")),
        fetch=javdb.movies, scope=javdb.javdb_scope,
        note="无码、欧美和 FC2 榜单需要登录后的 JavDB 会话。",
    ),
    Board(
        id="fanza", label="FANZA", group="works", item_kind="work", source="FANZA",
        periods=(_REALTIME, _DAILY, _WEEKLY, _MONTHLY),
        categories=(Option("AV", "视频"), Option("AMATEUR", "素人"), Option("ANIME", "动画")),
        fetch=fanza.works, note="FANZA 官方销量榜，实时榜每半小时更新。",
    ),
    Board(
        id="fc2", label="FC2", group="works", item_kind="work", source="FC2 Contents Market",
        periods=(_REALTIME, _DAILY, _WEEKLY, _MONTHLY, _YEARLY),
        fetch=fc2.works, note="FC2 官方销量榜。",
    ),
    Board(
        id="uncensored", label="无码厂商", group="works", item_kind="work", source="无码厂商官网",
        periods=(_WEEKLY, _MONTHLY),
        categories=(
            Option("1pondo", "一本道"), Option("caribbeancom", "加勒比"),
            Option("10musume", "天然むすめ"), Option("pacopacomama", "パコパコママ"),
        ),
        fetch=studios.works, note="各厂商官网公布的人气榜。",
    ),
    Board(
        id="mgs", label="MGStage", group="works", item_kind="work", source="MGStage",
        periods=(_DAILY, _WEEKLY, _MONTHLY),
        categories=(
            Option("all", "综合"), Option("mgs", "MGS 单品"), Option("shiroutotv", "素人TV"),
            Option("nanpatv", "ナンパTV"), Option("luxutv", "ラグジュTV"),
        ),
        fetch=mgs.works, note="MGStage 只允许日本 IP 访问，需要配置日本代理。",
    ),
    Board(
        id="javmenu", label="JavMenu", group="works", item_kind="work", source="JavMenu",
        periods=(_DAILY, _WEEKLY, _MONTHLY),
        fetch=javmenu.works, note="有码榜单，可在 JavDB 暂时无法访问时使用。",
    ),
    Board(
        id="fanza_actress", label="FANZA", group="actors", item_kind="actor", source="FANZA",
        periods=(_MONTHLY,), fetch=fanza.actresses, note="FANZA 官方女优月榜（按销量）。",
    ),
    Board(
        id="javdb_actors", label="JavDB", group="actors", item_kind="actor", source="JavDB",
        periods=(_MONTHLY,), categories=_JAVDB_TYPES,
        fetch=javdb.actors, scope=javdb.javdb_scope,
    ),
    Board(
        id="carib_actress", label="加勒比", group="actors", item_kind="actor", source="加勒比",
        periods=(Option("current", "当前"),), fetch=studios.carib_actresses,
        note="加勒比官网的人气无码女优榜。",
    ),
    Board(
        id="fc2_sellers", label="FC2 卖家", group="actors", item_kind="seller", source="FC2 Contents Market",
        periods=(Option("monthly", "上月"), Option("yearly", "年度")), fetch=fc2.sellers,
        note="FC2 按卖家统计的销量榜。",
    ),
    Board(
        id="fanza_genre", label="FANZA", group="genres", item_kind="work", source="FANZA",
        periods=(Option("popular", "人气"), Option("sales", "销量"), Option("review", "评价")),
        fetch=fanza.genre_works, load_categories=fanza.genres,
        note="FANZA 没有按周期统计的分类榜，这里按累计人气、销量或评价排序。",
    ),
)
_BOARDS_BY_ID = {board.id: board for board in BOARDS}


def ranking_boards() -> list[dict[str, Any]]:
    return [board.public() for board in BOARDS]


def fetch_ranking(request: RankingRequest, *, refresh: bool = False) -> dict[str, Any]:
    board = _BOARDS_BY_ID.get(request.board)
    if board is None or request.period not in {option.value for option in board.periods}:
        raise RankingError("ranking request is invalid", code="invalid")
    categories = _categories(board)
    category = request.category or (categories[0].value if categories else "")
    if categories and category not in {option.value for option in categories}:
        raise RankingError("ranking request is invalid", code="invalid")
    if not categories and category:
        raise RankingError("ranking request is invalid", code="invalid")
    request = RankingRequest(board.id, request.period, category)
    key = (board.id, request.period, category, board.scope() if board.scope else "")
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and not refresh and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    try:
        items = board.fetch(request)
    except RankingError:
        raise
    except Exception as exc:  # noqa: BLE001 - parsers read third-party pages.
        LOGGER.exception("ranking board %s failed", board.id)
        raise RankingError("ranking source answered with an unexpected page", code="unavailable") from exc
    payload: dict[str, Any] = {
        "board": board.id,
        "period": request.period,
        "category": category,
        "item_kind": board.item_kind,
        "items": items,
        "fetched_at": now,
    }
    if board.load_categories is not None:
        payload["categories"] = [{"value": option.value, "label": option.label} for option in categories]
    with _CACHE_LOCK:
        _CACHE[key] = (now, payload)
        for stale in [item for item, (at, _) in _CACHE.items() if now - at > _CACHE_SECONDS * 4]:
            _CACHE.pop(stale, None)
    return payload


def _categories(board: Board) -> tuple[Option, ...]:
    if board.load_categories is None:
        return board.categories
    now = time.time()
    with _CACHE_LOCK:
        cached = _CATEGORY_CACHE.get(board.id)
        if cached is not None and now - cached[0] < _CATEGORY_CACHE_SECONDS:
            return cached[1]
    options = board.load_categories()
    with _CACHE_LOCK:
        _CATEGORY_CACHE[board.id] = (now, options)
    return options
