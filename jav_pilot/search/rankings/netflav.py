"""Netflav rankings: its "最受歡迎" monthly picks, popular actresses and yearly picks.

The monthly picks and the actress list come from the site's JSON API. The
yearly pages ("年度精選", 2019 to 2025) are rendered from object literals
bundled in each year's page script; those lists are ordered by views and
never change, so a fetched script is kept for the life of the process.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

from ...core.catalog_code import normalize_catalog_code
from ...indexers.metadata_catalog import release_date, text
from ...net.http_client import FetchError
from .common import (
    MAX_ITEMS,
    Item,
    RankingError,
    RankingRequest,
    count_value,
    fetch_page,
    https_url,
    person_item,
    work_item,
)

SOURCE_ID = "ranking-netflav"
_ORIGIN = "https://netflav.com"
_API = f"{_ORIGIN}/api98"
_HEADERS = {"Accept": "application/json", "Referer": f"{_ORIGIN}/"}
# "本月" is value 1 and "5個月前" value 6; without a range the site lists every pick.
PICK_PERIODS = {"m1": "1", "m2": "2", "m3": "3", "m4": "4", "m5": "5", "m6": "6", "all": ""}
YEARS = tuple(str(year) for year in range(2025, 2018, -1))
YEARLY_CATEGORIES = ("censored", "chinese")
_PICK_PAGE_SIZE = 20
_ACTRESS_PAGE_SIZE = 48
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9]{6,16}")
_FC2_RE = re.compile(r"^FC2[-_ ]*(?:PPV[-_ ]*)?(\d{4,9})$", re.I)
_LEADING_TAG_RE = re.compile(r"^(?:\s*[\[【][^\]】]{1,12}[\]】])+\s*")
_CHINESE_TAG_RE = re.compile(r"^\s*[\[【](?:中字|中文字幕)[\]】]")
_ARRAY_START_RE = re.compile(r"[,;\s]([A-Za-z_$][\w$]{0,3})=\[\{")
_SCRIPT_CACHE_SIZE = 8
_SCRIPT_CACHE: OrderedDict[str, str] = OrderedDict()
_SCRIPT_CACHE_LOCK = threading.Lock()


def picks(request: RankingRequest) -> list[Item]:
    """The site's "最受歡迎" list: editor picks, in the site's order."""
    params = "" if request.period == "all" else f"&range=month&value={PICK_PERIODS[request.period]}"
    pages = -(-MAX_ITEMS // _PICK_PAGE_SIZE)
    docs = _paged(lambda page: f"{_API}/editor/getEditorChoice?page={page}{params}", pages)
    return _works(docs)


def actresses(request: RankingRequest) -> list[Item]:
    if request.period != "current":
        return _yearly_actresses(request.period)
    pages = -(-MAX_ITEMS // _ACTRESS_PAGE_SIZE)
    docs = _paged(lambda page: f"{_API}/actress/getActress?page={page}&type=", pages)
    items: list[Item] = []
    for doc in docs:
        item = _actress(len(items) + 1, doc, with_profile=False)
        if item is not None:
            items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return _require(items)


def yearly(request: RankingRequest) -> list[Item]:
    """The yearly "你們愛看的有碼影片" or "中文字幕影片" list, ordered by views."""
    lists = [docs for docs in _yearly_lists(request.period) if docs and "code" in docs[0]]
    # Each year has a censored list first and a Chinese-subtitle list last; 2020
    # also lists FC2 works between them.
    if len(lists) < 2:
        raise RankingError(f"Netflav {request.period} picks are missing", code="unavailable")
    return _works(lists[0] if request.category == "censored" else lists[-1])


def _yearly_actresses(year: str) -> list[Item]:
    lists = [docs for docs in _yearly_lists(year) if docs and "name" in docs[0] and "code" not in docs[0]]
    if not lists:
        raise RankingError(f"Netflav {year} actresses are missing", code="unavailable")
    items: list[Item] = []
    for doc in lists[0]:
        item = _actress(len(items) + 1, doc, with_profile=True)
        if item is not None:
            items.append(item)
    return _require(items)


def _paged(url_for: Any, pages: int) -> list[dict[str, Any]]:
    first = _api_result(url_for(1))
    docs = list(_docs(first))
    total_pages = count_value(first.get("pages")) or 1
    later = range(2, min(pages, total_pages) + 1)
    if later:
        with ThreadPoolExecutor(max_workers=len(later)) as pool:
            futures = [pool.submit(_api_result, url_for(page)) for page in later]
            for future in futures:
                try:
                    docs.extend(_docs(future.result()))
                except RankingError:
                    # Keep the pages before the first failure, like the FC2 lists.
                    break
    return docs


def _api_result(url: str) -> dict[str, Any]:
    try:
        payload = json.loads(fetch_page(url, headers=_HEADERS))
    except (FetchError, ValueError) as exc:
        raise RankingError("Netflav ranking is temporarily unavailable", code="unavailable") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RankingError("Netflav ranking answered with an unexpected payload", code="unavailable")
    return result


def _docs(result: dict[str, Any]) -> list[dict[str, Any]]:
    docs = result.get("docs")
    return [doc for doc in docs if isinstance(doc, dict)] if isinstance(docs, list) else []


def _works(docs: list[dict[str, Any]]) -> list[Item]:
    items: list[Item] = []
    seen: set[str] = set()
    for doc in docs:
        # The API nests the work under "video" for some picks.
        if isinstance(doc.get("video"), dict):
            doc = {**doc["video"], **doc}
        code = _code(doc.get("code"))
        video_id = text(doc.get("videoId"), 20)
        if code is None or not _VIDEO_ID_RE.fullmatch(video_id):
            continue
        # A Chinese-subtitle upload and the original share a code; list the work once.
        key = code.upper()
        if key in seen:
            continue
        seen.add(key)
        raw_title = text(doc.get("title_zh") or doc.get("title"), 400)
        chinese = bool(_CHINESE_TAG_RE.match(text(doc.get("title"), 40)) or _CHINESE_TAG_RE.match(raw_title))
        chinese = chinese or text(doc.get("uCode"), 40).upper().endswith("-C")
        title = _LEADING_TAG_RE.sub("", raw_title)
        # Titles repeat the code at the start or, in older lists, at the end.
        if title.upper().startswith(code.upper()):
            title = title[len(code):].strip()
        if title.upper().endswith(code.upper()):
            title = title[:-len(code)].strip()
        subtitle = " · ".join(part for part in ("中文字幕" if chinese else "", _actors(doc.get("actors"))) if part)
        items.append(
            work_item(
                len(items) + 1,
                source_id=SOURCE_ID,
                code=code,
                title=title[:300] or code,
                subtitle=subtitle or None,
                release_date=release_date(doc.get("videoDate")),
                detail_url=f"{_ORIGIN}/video?id={video_id}",
                cover=https_url(doc.get("preview_hp") or doc.get("preview"), f"{_ORIGIN}/"),
            )
        )
        if len(items) >= MAX_ITEMS:
            break
    return _require(items)


def _actress(rank: int, doc: dict[str, Any], *, with_profile: bool) -> Item | None:
    name = text(doc.get("name"), 80)
    if not name:
        return None
    alias = text(doc.get("name_zh"), 80)
    works = count_value(doc.get("videoCount"))
    parts = [alias if alias and alias != name else "", f"作品 {works} 部" if works else ""]
    if with_profile:
        parts.extend((text(doc.get("birthday"), 10), _measurements(doc)))
    return person_item(
        rank,
        source_id=SOURCE_ID,
        name=name,
        subtitle=" · ".join(part for part in parts if part) or None,
        detail_url=f"{_ORIGIN}/all?actress={quote(name, safe='')}",
        cover=https_url(doc.get("icon"), f"{_ORIGIN}/"),
    )


def _measurements(doc: dict[str, Any]) -> str:
    def number(key: str) -> str:
        match = re.fullmatch(r"(\d{2,3})(?:\s*cm)?", text(doc.get(key), 10))
        return match.group(1) if match else ""

    height, breast, waist, hip = (number(key) for key in ("height", "breast", "waist", "hip"))
    cup = text(doc.get("cup"), 3)
    sizes = " ".join(
        part
        for part in (
            f"B{breast}" + (f"({cup})" if re.fullmatch(r"[A-Z]", cup) else "") if breast else "",
            f"W{waist}" if waist else "",
            f"H{hip}" if hip else "",
        )
        if part
    )
    return " ".join(part for part in (f"{height}cm" if height else "", sizes) if part)


def _actors(value: object) -> str:
    names: list[str] = []
    for raw in value if isinstance(value, list) else []:
        name = text(raw, 60)
        # Each name is also listed as "jp:", "zh:" and "en:" variants.
        if name and not re.match(r"^[a-z]{2}:", name) and name not in names:
            names.append(name)
    return "、".join(names[:3])


def _code(value: object) -> str | None:
    raw = text(value, 40)
    fc2 = _FC2_RE.match(raw)
    normalized = normalize_catalog_code(f"FC2-PPV-{fc2.group(1)}" if fc2 else raw)
    return normalized[0] if normalized else None


def _require(items: list[Item]) -> list[Item]:
    if not items:
        raise RankingError("Netflav ranking page had no entries", code="unavailable")
    return items


def _yearly_lists(year: str) -> list[list[dict[str, str]]]:
    if year not in YEARS:
        raise RankingError("ranking request is invalid", code="invalid")
    try:
        page = fetch_page(f"{_ORIGIN}/{year}")
        match = re.search(rf"/_next/static/chunks/pages/{year}-[0-9a-f]{{8,40}}\.js", page)
        if match is None:
            raise RankingError(f"Netflav {year} page has changed", code="unavailable")
        script = _cached_script(f"{_ORIGIN}{match.group()}")
    except FetchError as exc:
        raise RankingError(f"Netflav {year} picks are temporarily unavailable", code="unavailable") from exc
    return [_array_objects(script, found.end() - 2) for found in _ARRAY_START_RE.finditer(script)]


def _cached_script(url: str) -> str:
    # The script name carries a content hash, so a fetched copy never goes stale.
    with _SCRIPT_CACHE_LOCK:
        if url in _SCRIPT_CACHE:
            _SCRIPT_CACHE.move_to_end(url)
            return _SCRIPT_CACHE[url]
    script = fetch_page(url, headers={"Referer": f"{_ORIGIN}/"})
    with _SCRIPT_CACHE_LOCK:
        _SCRIPT_CACHE[url] = script
        while len(_SCRIPT_CACHE) > _SCRIPT_CACHE_SIZE:
            _SCRIPT_CACHE.popitem(last=False)
    return script


def _array_objects(source: str, start: int) -> list[dict[str, str]]:
    """Read the scalar fields of each object in the array literal at ``start``.

    Only string and number values directly on each element are kept; nested
    arrays and objects are skipped. The scan stops at the array's end.
    """
    objects: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key: str | None = None
    depth = 0
    index = start
    length = len(source)
    while index < length:
        char = source[index]
        if char in "\"'":
            value, index = _js_string(source, index)
            if depth == 2 and key is not None:
                current[key] = value
            key = None
            continue
        if char in "[{":
            depth += 1
            if depth == 2 and char == "{":
                current = {}
            key = None
        elif char in "]}":
            if depth == 2 and char == "}":
                objects.append(current)
            depth -= 1
            key = None
            if depth == 0:
                break
        elif char == ",":
            key = None
        elif depth == 2 and (char.isalnum() or char in "_$.-"):
            end = index
            while end < length and (source[end].isalnum() or source[end] in "_$.-"):
                end += 1
            token = source[index:end]
            if end < length and source[end] == ":" and key is None:
                key = token
                end += 1
            elif key is not None:
                current[key] = token
                key = None
            index = end
            continue
        index += 1
    return objects


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_string(source: str, start: int) -> tuple[str, int]:
    quote_char = source[start]
    parts: list[str] = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == quote_char:
            return "".join(parts), index + 1
        if char == "\\" and index + 1 < len(source):
            escape = source[index + 1]
            if escape == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", source[index + 2:index + 6]):
                parts.append(chr(int(source[index + 2:index + 6], 16)))
                index += 6
                continue
            if escape == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", source[index + 2:index + 4]):
                parts.append(chr(int(source[index + 2:index + 4], 16)))
                index += 4
                continue
            parts.append(_JS_ESCAPES.get(escape, escape))
            index += 2
            continue
        parts.append(char)
        index += 1
    raise RankingError("Netflav page script has an unterminated string", code="unavailable")
