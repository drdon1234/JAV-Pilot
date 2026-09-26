from __future__ import annotations

import html
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Iterable, cast
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from jav_pilot.core.catalog_code import looks_like_catalog_code
from jav_pilot.torrent.magnet import parse_magnet_text
from jav_pilot.core.models import (
    MagnetHint,
    MagnetInfo,
    Rating,
    RelatedRef,
    SearchKind,
    SearchResult,
    SourceDetails,
    canonical_image_url_key,
    normalize_source_images,
)
from jav_pilot.net.network_guard import PublicHostResolver

if TYPE_CHECKING:
    from jav_pilot.core.models import SourceImage


JAVBUS_JS_VAR_RE = re.compile(
    r"var\s+(?P<name>gid|uc|img|lang)\s*=\s*(?P<quote>['\"]?)(?P<value>.*?)(?P=quote)\s*;",
    re.IGNORECASE | re.DOTALL,
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DETAIL_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)"
)
SIZE_RE = re.compile(
    r"(?<![\w.])(?P<amount>\d{1,5}(?:[.,]\d{1,3})?)\s*(?P<unit>TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\b",
    re.IGNORECASE,
)
DURATION_RE = re.compile(
    r"(?P<minutes>\d{1,4})\s*(?:分钟|分鐘|分|minutes?|mins?)", re.IGNORECASE
)
RATING_VALUE_RE = re.compile(
    r"(?P<value>\d{1,2}(?:\.\d+)?)\s*(?:分|/\s*10\b)", re.IGNORECASE
)
RATING_VOTES_RE = re.compile(
    r"(?:由\s*)?(?P<votes>\d[\d,]*)\s*(?:人(?:评价|評價)?|users?|votes?)",
    re.IGNORECASE,
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_MAGNET_CONTAINER_CLASSES = frozenset(
    {"item", "magnet", "magnet-row", "magnet-item", "torrent", "download", "columns"}
)
_BADGE_CLASS_MARKERS = ("badge", "label", "tag")
_IMAGE_EXTENSIONS = frozenset(
    {".apng", ".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
)
_IMAGE_NOISE_MARKERS = ("avatar", "button", "favicon", "flag", "icon", "logo", "sprite")
_VIDEO_ARTWORK_CLASS_MARKERS = (
    "movie-panel-video",
    "player-poster",
    "preview-video",
    "sample-video",
    "trailer",
    "video-player",
    "video-poster",
    "video-preview",
)
_VIDEO_ARTWORK_ATTRIBUTE_MARKERS = frozenset(
    {
        "data-player",
        "data-trailer",
        "data-trailer-url",
        "data-video",
        "data-video-url",
        "poster",
    }
)
_VIDEO_EXTENSIONS = frozenset({".m3u8", ".m4v", ".mov", ".mp4", ".webm"})
_JAVBUS_DMM_IMAGE_PATH_PREFIXES = {
    "awsimgsrc.dmm.co.jp": ("/pics_dig/",),
    "pics.dmm.co.jp": ("/pics_dig/", "/digital/video/"),
}
_JAVBUS_CDN_IMAGE_PATHS = (
    "/pics/",
    "/cover/",
    "/covers/",
    "/sample/",
    "/samples/",
    "/thumb/",
    "/thumbs/",
)
MAX_SOURCE_IMAGES = 120
MAX_IMAGE_CANDIDATES = MAX_SOURCE_IMAGES * 4
MAX_CONFIGURED_SELECTOR_MATCHES = 200
MAX_CONFIGURED_VALUE_LENGTH = 4096
_JAVDB_EMPTY_STATE_TEXTS = frozenset(
    {
        "暫無內容",
        "暂无内容",
        "no content",
        "no results",
        "nothing found",
    }
)
_MAGNET_BADGE_TEXTS = frozenset({"含磁鏈", "含磁链", "magnet", "magnet available"})
_DETAIL_LABELS: dict[str, str] = {
    "标题": "title",
    "標題": "title",
    "片名": "title",
    "作品名": "title",
    "title": "title",
    "原题": "original_title",
    "原題": "original_title",
    "原名": "original_title",
    "原始标题": "original_title",
    "原始標題": "original_title",
    "originaltitle": "original_title",
    "发行日期": "release_date",
    "發行日期": "release_date",
    "发售日期": "release_date",
    "發售日期": "release_date",
    "発売日": "release_date",
    "発売日期": "release_date",
    "配信日": "release_date",
    "配信開始日": "release_date",
    "日期": "release_date",
    "date": "release_date",
    "releasedate": "release_date",
    "released": "release_date",
    "时长": "duration",
    "時長": "duration",
    "长度": "duration",
    "長度": "duration",
    "duration": "duration",
    "片商": "maker",
    "制作商": "maker",
    "製作商": "maker",
    "maker": "maker",
    "studio": "maker",
    "发行商": "publisher",
    "發行商": "publisher",
    "publisher": "publisher",
    "label": "publisher",
    "distributor": "publisher",
    "系列": "series",
    "series": "series",
    "导演": "director",
    "導演": "director",
    "director": "director",
    "评分": "rating",
    "評分": "rating",
    "rating": "rating",
    "类别": "tag",
    "類別": "tag",
    "标签": "tag",
    "標籤": "tag",
    "tags": "tag",
    "genres": "tag",
    "genre": "tag",
    "演员": "actor",
    "演員": "actor",
    "actors": "actor",
    "actor(s)": "actor",
}
_RELATION_PATH_PREFIXES: dict[str, dict[str, tuple[str, ...]]] = {
    "javbus": {
        # JavBus mirrors have used both singular and plural relation paths.
        # Keep the allow-list explicit so a relation link can never become a
        # generic same-origin navigation link.
        "actor": ("/star/", "/stars/", "/actor/", "/actors/"),
        "tag": ("/genre/", "/genres/", "/tag/", "/tags/"),
        "series": ("/series/",),
        "maker": ("/studio/", "/studios/", "/maker/", "/makers/"),
        "publisher": (
            "/label/",
            "/labels/",
            "/studio/",
            "/studios/",
            "/publisher/",
            "/publishers/",
        ),
        "director": ("/director/", "/directors/"),
    },
    "javdb": {
        "actor": ("/actors/", "/actor/", "/stars/", "/star/"),
        "tag": ("/tags", "/tag/", "/genres/", "/genre/"),
        "series": ("/series/",),
        "maker": ("/makers/", "/maker/"),
        "publisher": ("/publishers/", "/publisher/"),
        "director": ("/directors/", "/director/"),
        "code": ("/video_codes/",),
    },
}


@dataclass(frozen=True)
class JavBusMagnetEndpoint:
    gid: str
    uc: str
    img: str
    lang: str

    def to_url(self, base_url: str) -> str:
        query = urlencode(
            {
                "gid": self.gid,
                "lang": self.lang,
                "img": self.img,
                "uc": self.uc,
                "floor": "1",
            }
        )
        return urljoin(base_url, f"/ajax/uncledatoolsbyajax.php?{query}")


def looks_like_javbus_age_verification(page: str) -> bool:
    lowered = page.lower()
    return "age verification javbus" in lowered or (
        "driver-verify" in lowered and "ageverify" in lowered
    )


def extract_magnets_from_html(page: str, *, limit: int = 100) -> tuple[MagnetInfo, ...]:
    decoded = page or ""
    safe_limit = max(1, min(int(limit), 200))
    document = _parse_document(decoded)
    magnets_by_hash: dict[str, MagnetInfo] = {}

    for node in document.walk():
        candidates = [
            value for value in node.attrs.values() if "magnet:?" in value.lower()
        ]
        if node.tag in {"script", "template"}:
            candidates.append(node.text_content())
        for candidate in candidates:
            for magnet in parse_magnet_text(candidate, max_items=safe_limit):
                context = _magnet_context(node)
                enriched = _with_reported_magnet_metadata(magnet, context)
                current = magnets_by_hash.get(enriched.info_hash)
                magnets_by_hash[enriched.info_hash] = merge_magnet_info(
                    current, enriched
                )

    # Keep supporting magnets embedded in plain text or uncommon markup, while
    # merging them with the richer row/list metadata collected above.
    for magnet in parse_magnet_text(decoded, max_items=safe_limit):
        current = magnets_by_hash.get(magnet.info_hash)
        magnets_by_hash[magnet.info_hash] = merge_magnet_info(current, magnet)

    return tuple(list(magnets_by_hash.values())[:safe_limit])


def merge_magnet_info(current: MagnetInfo | None, candidate: MagnetInfo) -> MagnetInfo:
    if current is None:
        return candidate

    trackers = tuple(dict.fromkeys((*current.trackers, *candidate.trackers)))
    badges = tuple(dict.fromkeys((*current.badges, *candidate.badges)))
    params = {key: list(values) for key, values in current.params.items()}
    for key, values in candidate.params.items():
        params[key] = list(dict.fromkeys((*params.get(key, []), *values)))

    preferred = (
        candidate
        if _magnet_richness(candidate) > _magnet_richness(current)
        else current
    )
    return replace(
        preferred,
        display_name=current.display_name or candidate.display_name,
        trackers=trackers,
        exact_length=current.exact_length or candidate.exact_length,
        params=params,
        reported_size_text=current.reported_size_text or candidate.reported_size_text,
        reported_size_bytes=current.reported_size_bytes
        or candidate.reported_size_bytes,
        badges=badges,
    )


def extract_javbus_images_from_html(
    page: str, base_url: str
) -> tuple["SourceImage", ...]:
    return _extract_source_images(page, base_url, profile="javbus")


def extract_javdb_images_from_html(
    page: str, base_url: str
) -> tuple["SourceImage", ...]:
    return _extract_source_images(page, base_url, profile="javdb")


def extract_javbus_details_from_html(page: str, base_url: str) -> SourceDetails:
    return _extract_source_details(page, base_url, profile="javbus")


def extract_javdb_details_from_html(page: str, base_url: str) -> SourceDetails:
    return _extract_source_details(page, base_url, profile="javdb")


def parse_search_results_with_rules(
    page: str,
    base_url: str,
    *,
    source_id: str,
    profile: str,
    rules: dict[str, Any],
    limit: int,
) -> tuple[SearchResult, ...]:
    soup = BeautifulSoup(page or "", "html.parser")
    item_selector = str(rules.get("item_selector") or "").strip()
    if not item_selector:
        return ()
    output: list[SearchResult] = []
    host_resolver = PublicHostResolver(max_hosts=16)
    clean_limit = max(1, min(int(limit), MAX_CONFIGURED_SELECTOR_MATCHES))
    for item in _configured_select(soup, item_selector)[:MAX_CONFIGURED_SELECTOR_MATCHES]:
        detail_value = _first_configured_value(item, rules.get("detail_url"))
        detail_url = _same_origin_http_url(base_url, detail_value or "")
        if not detail_url:
            continue

        title = _first_configured_value(item, rules.get("title"))
        if not title:
            title = _bounded_node_text(item)
        if not title:
            continue

        code = _first_catalog_code(
            _configured_values(item, rules.get("code")),
            fallback=title,
        )
        release_date = next(
            (
                parsed
                for value in _configured_values(item, rules.get("date"))
                if (parsed := _iso_release_date(value))
            ),
            None,
        )
        rating_text = _first_configured_value(item, rules.get("rating"))
        rating = _five_point_rating(rating_text or "")
        cover_value = _first_configured_value(item, rules.get("cover"))
        cover_url = (
            _absolute_image_url(
                base_url,
                cover_value,
                profile=profile,
                host_resolver=host_resolver,
            )
            if cover_value
            else None
        )
        magnet_hint = str(rules.get("default_magnet_hint") or "unknown")
        magnet_selector = str(rules.get("magnet_available_selector") or "").strip()
        if magnet_selector and _configured_select(item, magnet_selector):
            magnet_hint = "available"

        output.append(
            SearchResult(
                source=source_id,
                title=title,
                url=detail_url,
                code=code,
                date=release_date,
                details=SourceDetails(rating=rating),
                magnet_hint=cast(MagnetHint, magnet_hint),
                metadata={"metadata_only": True, "cover": cover_url},
            )
        )
        if len(output) >= clean_limit:
            break
    return tuple(output)


def has_configured_empty_search_state(page: str, selector: str) -> bool:
    clean_selector = str(selector or "").strip()
    if not clean_selector:
        return False
    soup = BeautifulSoup(page or "", "html.parser")
    return any(
        not _bs_node_or_ancestor_is_hidden(node)
        for node in _configured_select(soup, clean_selector)
    )


def extract_details_with_rules(
    page: str,
    base_url: str,
    *,
    rules: dict[str, Any],
) -> SourceDetails:
    soup = BeautifulSoup(page or "", "html.parser")
    fields = rules.get("fields") if isinstance(rules, dict) else {}
    if not isinstance(fields, dict):
        return SourceDetails()

    title = _first_configured_value(soup, fields.get("title"))
    original_title = _first_configured_value(soup, fields.get("original_title"))
    release_text = _first_configured_value(soup, fields.get("release_date"))
    duration_text = _first_configured_value(soup, fields.get("duration"))
    rating_text = _first_configured_value(soup, fields.get("rating"))
    relation_values = {
        field: _configured_relations(
            soup,
            fields.get(field),
            base_url=base_url,
            kind=field,
        )
        for field in ("maker", "publisher", "series", "director", "actor", "tag")
    }
    return SourceDetails(
        title=title,
        original_title=original_title,
        release_date=_iso_release_date(release_text or ""),
        duration_minutes=_duration_minutes(duration_text or ""),
        duration_text=duration_text,
        rating=_rating(rating_text or ""),
        makers=relation_values["maker"],
        publishers=relation_values["publisher"],
        series=relation_values["series"],
        directors=relation_values["director"],
        actors=relation_values["actor"],
        tags=relation_values["tag"],
    )


def extract_images_with_rules(
    page: str,
    base_url: str,
    *,
    profile: str,
    rules: dict[str, Any],
) -> tuple["SourceImage", ...]:
    from jav_pilot.core.models import SourceImage

    soup = BeautifulSoup(page or "", "html.parser")
    images: list[SourceImage] = []
    host_resolver = PublicHostResolver(max_hosts=16)
    for kind in ("cover", "backdrop", "sample"):
        rule = rules.get(kind) if isinstance(rules, dict) else None
        for node, value in _configured_node_values(soup, rule):
            url = _absolute_image_url(
                base_url,
                value,
                profile=profile,
                host_resolver=host_resolver,
            )
            if not url:
                continue
            width = _positive_dimension(node.get("width"))
            height = _positive_dimension(node.get("height"))
            images.append(
                SourceImage(kind=cast(Any, kind), url=url, width=width, height=height)
            )
    return normalize_source_images(images, limit=MAX_SOURCE_IMAGES)


def extract_magnets_with_rules(
    page: str,
    *,
    rules: dict[str, Any],
    limit: int = 100,
) -> tuple[MagnetInfo, ...]:
    soup = BeautifulSoup(page or "", "html.parser")
    item_selector = str(rules.get("item_selector") or "").strip()
    if not item_selector:
        return ()
    safe_limit = max(1, min(int(limit), 200))
    magnets_by_hash: dict[str, MagnetInfo] = {}
    for item in _configured_select(soup, item_selector)[:safe_limit]:
        candidates = list(extract_magnets_from_html(str(item), limit=safe_limit))
        for value in _configured_values(item, rules.get("uri")):
            candidates.extend(parse_magnet_text(value, max_items=safe_limit))
        name = _first_configured_value(item, rules.get("name"))
        size_text = _first_configured_value(item, rules.get("size"))
        reported_size_text, reported_size_bytes = _reported_size(size_text or "")
        badges = tuple(
            dict.fromkeys(
                _configured_values(item, rules.get("badges"))
            )
        )
        for magnet in candidates:
            enriched = replace(
                magnet,
                display_name=name or magnet.display_name,
                reported_size_text=reported_size_text or magnet.reported_size_text,
                reported_size_bytes=reported_size_bytes or magnet.reported_size_bytes,
                badges=tuple(dict.fromkeys((*magnet.badges, *badges))),
            )
            current = magnets_by_hash.get(enriched.info_hash)
            magnets_by_hash[enriched.info_hash] = merge_magnet_info(current, enriched)
    return tuple(list(magnets_by_hash.values())[:safe_limit])


def _configured_relations(
    scope: BeautifulSoup | Tag,
    rule: Any,
    *,
    base_url: str,
    kind: str,
) -> tuple[RelatedRef, ...]:
    output: list[RelatedRef] = []
    seen: set[tuple[str, str]] = set()
    for selected in _configured_rule_nodes(scope, rule):
        nodes = (
            [selected] if selected.name == "a" else _configured_select(selected, "a")
        )
        if not nodes:
            nodes = [selected]
        for node in nodes:
            if len(output) >= MAX_CONFIGURED_SELECTOR_MATCHES:
                return tuple(output)
            label = _configured_node_value(node, _rule_attributes(rule))
            if not label:
                continue
            raw_url = str(node.get("href") or node.get("data-href") or "").strip()
            url = _same_origin_http_url(base_url, raw_url) if raw_url else None
            key = (label.casefold(), url or "")
            if key in seen:
                continue
            seen.add(key)
            output.append(RelatedRef(kind=cast(Any, kind), label=label, url=url))
    return tuple(output)


def _first_catalog_code(values: Iterable[str], *, fallback: str = "") -> str | None:
    candidates = [*values, fallback]
    for value in candidates:
        clean = " ".join(str(value or "").split())
        for candidate in (clean, clean.split(maxsplit=1)[0] if clean else ""):
            candidate = candidate.strip("[](){}:：,，")
            if candidate and _looks_like_code(candidate):
                return candidate
    return None


def _first_configured_value(scope: BeautifulSoup | Tag, rule: Any) -> str | None:
    return next(iter(_configured_values(scope, rule)), None)


def _configured_values(
    scope: BeautifulSoup | Tag,
    rule: Any,
) -> list[str]:
    return [
        value
        for _, value in _configured_node_values(
            scope,
            rule,
        )
    ]


def _configured_node_values(
    scope: BeautifulSoup | Tag,
    rule: Any,
) -> list[tuple[Tag, str]]:
    attributes = _rule_attributes(rule)
    output: list[tuple[Tag, str]] = []
    for node in _configured_rule_nodes(scope, rule):
        value = _configured_node_value(node, attributes)
        if value:
            output.append((node, value))
    return output


def _configured_rule_nodes(
    scope: BeautifulSoup | Tag,
    rule: Any,
) -> list[Tag]:
    if not isinstance(rule, dict):
        return []
    selector = str(rule.get("selector") or "").strip()
    if not selector:
        return []
    nodes = _configured_select(scope, selector)
    raw_index = rule.get("index")
    if raw_index is not None:
        index = int(raw_index)
        return nodes[index : index + 1]
    return nodes


def _configured_select(scope: BeautifulSoup | Tag, selector: str) -> list[Tag]:
    if selector == ":scope" and isinstance(scope, Tag):
        return [scope]
    return [
        node
        for node in scope.select(selector, limit=MAX_CONFIGURED_SELECTOR_MATCHES)
        if isinstance(node, Tag)
    ]


def _rule_attributes(rule: Any) -> tuple[str, ...]:
    if not isinstance(rule, dict) or not isinstance(rule.get("attributes"), list):
        return ("text",)
    return tuple(str(value) for value in rule["attributes"])


def _configured_node_value(node: Tag, attributes: Iterable[str]) -> str | None:
    for attribute in attributes:
        if attribute == "text":
            clean = _bounded_node_text(node)
        else:
            raw_value = node.get(attribute)
            value = (
                " ".join(raw_value)
                if isinstance(raw_value, list)
                else str(raw_value or "")
            )
            clean = _bounded_configured_value(value)
        if clean:
            return clean
    return None


def _bounded_configured_value(value: object) -> str:
    raw = str(value or "")[: MAX_CONFIGURED_VALUE_LENGTH * 8]
    clean = " ".join(html.unescape(raw).split())
    return clean[:MAX_CONFIGURED_VALUE_LENGTH].rstrip()


def _bounded_node_text(node: Tag) -> str:
    output = ""
    for value in node.stripped_strings:
        clean = _bounded_configured_value(value)
        if not clean:
            continue
        separator = " " if output else ""
        remaining = MAX_CONFIGURED_VALUE_LENGTH - len(output) - len(separator)
        if remaining <= 0:
            break
        output += separator + clean[:remaining]
        if len(output) >= MAX_CONFIGURED_VALUE_LENGTH:
            break
    return output.rstrip()


def _bs_node_or_ancestor_is_hidden(node: Tag) -> bool:
    current: Tag | None = node
    while current is not None:
        if current.name in {"template", "script"} or current.has_attr("hidden"):
            return True
        style = str(current.get("style") or "").replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def resolve_semantic_ref_url(
    base_url: str,
    value: str,
    *,
    profile: str,
    kind: SearchKind,
) -> str | None:
    absolute = _same_origin_http_url(base_url, value)
    if not absolute:
        return None
    prefixes = _RELATION_PATH_PREFIXES.get(profile, {}).get(kind, ())
    path = urlsplit(absolute).path.lower()
    if not any(_path_matches_prefix(path, prefix) for prefix in prefixes):
        return None
    return absolute


def _path_matches_prefix(path: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def has_javdb_empty_search_state(page: str) -> bool:
    document = _parse_document(page or "")
    for node in document.walk():
        if "empty-message" not in node.class_tokens or _node_or_ancestor_is_hidden(
            node
        ):
            continue
        normalized = " ".join(node.text_content().split()).casefold()
        if normalized in _JAVDB_EMPTY_STATE_TEXTS:
            return True
    return False


class _HtmlNode:
    def __init__(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        parent: "_HtmlNode | None" = None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.content: list[str | _HtmlNode] = []

    @property
    def class_tokens(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").lower().split())

    def children(self) -> Iterable["_HtmlNode"]:
        return (item for item in self.content if isinstance(item, _HtmlNode))

    def walk(self) -> Iterable["_HtmlNode"]:
        yield self
        for child in self.children():
            yield from child.walk()

    def text_content(self) -> str:
        parts: list[str] = []
        for item in self.content:
            parts.append(item if isinstance(item, str) else item.text_content())
        return " ".join(" ".join(parts).split())


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(
            tag.lower(),
            {key.lower(): value or "" for key, value in attrs},
            self._stack[-1],
        )
        self._stack[-1].content.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == lowered:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].content.append(data)


def _parse_document(page: str) -> _HtmlNode:
    parser = _DocumentParser()
    parser.feed(page)
    parser.close()
    return parser.root


def _extract_source_details(page: str, base_url: str, *, profile: str) -> SourceDetails:
    document = _parse_document(page or "")
    relations: dict[str, list[RelatedRef]] = {
        "maker": [],
        "publisher": [],
        "series": [],
        "director": [],
        "actor": [],
        "tag": [],
    }
    seen_relations: set[tuple[str, str, str]] = set()
    title = _detail_page_title(document, profile=profile)
    original_title = _detail_original_title(document)
    release_date: str | None = None
    duration_text: str | None = None
    duration_minutes: int | None = None
    rating: Rating | None = None

    for node in document.walk():
        if not _is_detail_field_container(node, profile=profile):
            continue
        header = _detail_header_node(node)
        if header is None:
            continue
        field = _DETAIL_LABELS.get(_normalized_detail_label(header.text_content()))
        if not field:
            continue
        value_node = _detail_value_node(node, header, profile=profile)
        value_text = _detail_value_text(value_node, header)
        if field == "title":
            title = value_text or title
        elif field == "original_title":
            original_title = value_text or original_title
        elif field == "release_date":
            release_date = _iso_release_date(value_text) or release_date
        elif field == "duration":
            duration_text = value_text or duration_text
            duration_minutes = _duration_minutes(value_text) or duration_minutes
        elif field == "rating":
            rating = _rating(value_text) or rating
        else:
            _append_related_from_node(
                relations[field],
                seen_relations,
                value_node,
                field=field,
                value_text=value_text,
                base_url=base_url,
                profile=profile,
            )

    # JavBus actor links normally live in star boxes rather than a labelled p.
    # JavDB also uses /actors/* for global navigation categories, so applying
    # this fallback to every profile pollutes persisted metadata and NFO files.
    if profile == "javbus":
        actor_kind = "actor"
        for node in document.walk():
            if node.tag != "a":
                continue
            label = node.text_content().strip()
            if not label:
                continue
            url = resolve_semantic_ref_url(
                base_url,
                node.attrs.get("href", ""),
                profile=profile,
                kind=actor_kind,
            )
            if url:
                _append_related(
                    relations[actor_kind],
                    seen_relations,
                    RelatedRef(kind="actor", label=label, url=url),
                )

    return SourceDetails(
        title=title,
        original_title=original_title,
        release_date=release_date,
        duration_minutes=duration_minutes,
        duration_text=duration_text,
        rating=rating,
        makers=tuple(relations["maker"]),
        publishers=tuple(relations["publisher"]),
        series=tuple(relations["series"]),
        directors=tuple(relations["director"]),
        actors=tuple(relations["actor"]),
        tags=tuple(relations["tag"]),
    )


def _detail_page_title(document: _HtmlNode, *, profile: str) -> str | None:
    nodes = tuple(document.walk())
    if profile == "javdb":
        current = next(
            (
                node.text_content()
                for node in nodes
                if "current-title" in node.class_tokens and node.text_content()
            ),
            None,
        )
        if current:
            return current
        return next(
            (
                node.text_content()
                for node in nodes
                if node.tag in {"h1", "h2", "h3"}
                and "title" in node.class_tokens
                and node.text_content()
            ),
            None,
        )

    for heading_tag in ("h3", "h1", "h2"):
        value = next(
            (
                node.text_content()
                for node in nodes
                if node.tag == heading_tag and node.text_content()
            ),
            None,
        )
        if value:
            return value
    return None


def _detail_original_title(document: _HtmlNode) -> str | None:
    original_title_classes = frozenset(
        {"origin-title", "original-title", "original_title"}
    )
    return next(
        (
            node.text_content()
            for node in document.walk()
            if node.class_tokens & original_title_classes and node.text_content()
        ),
        None,
    )


def _is_detail_field_container(node: _HtmlNode, *, profile: str) -> bool:
    if profile == "javdb":
        return node.tag == "div" and "panel-block" in node.class_tokens
    return node.tag == "p" or (node.tag == "div" and "panel-block" in node.class_tokens)


def _detail_header_node(node: _HtmlNode) -> _HtmlNode | None:
    candidates = [candidate for candidate in node.walk() if candidate is not node]
    return next(
        (
            candidate
            for candidate in candidates
            if "header" in candidate.class_tokens or candidate.tag == "strong"
        ),
        None,
    )


def _detail_value_node(
    node: _HtmlNode, header: _HtmlNode, *, profile: str
) -> _HtmlNode:
    del profile
    return next(
        (
            candidate
            for candidate in node.walk()
            if candidate is not node
            and candidate is not header
            and "value" in candidate.class_tokens
        ),
        node,
    )


def _detail_value_text(node: _HtmlNode, header: _HtmlNode) -> str:
    value = node.text_content().strip(" \t\r\n:：")
    if node is not header:
        header_text = header.text_content().strip()
        if header_text and value.startswith(header_text):
            value = value[len(header_text) :].strip(" \t\r\n:：")
    return value


def _normalized_detail_label(value: str) -> str:
    return re.sub(r"[\s:：]+", "", value or "").casefold()


def _iso_release_date(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value or "")
    match = DETAIL_DATE_RE.search(normalized)
    if not match:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        ).isoformat()
    except ValueError:
        return None


def _duration_minutes(value: str) -> int | None:
    match = DURATION_RE.search(value or "")
    if not match:
        return None
    minutes = int(match.group("minutes"))
    return minutes if 0 < minutes <= 1440 else None


def _rating(value: str) -> Rating | None:
    clean = " ".join((value or "").split())
    if not clean:
        return None
    value_match = RATING_VALUE_RE.search(clean)
    votes_match = RATING_VOTES_RE.search(clean)
    rating_value = float(value_match.group("value")) if value_match else None
    votes = int(votes_match.group("votes").replace(",", "")) if votes_match else None
    if rating_value is not None and not 0 <= rating_value <= 10:
        rating_value = None
    return Rating(value=rating_value, votes=votes, text=clean)


def _five_point_rating(value: str) -> Rating | None:
    rating = _rating(value)
    if (
        rating is None
        or rating.value is None
        or not math.isfinite(rating.value)
        or not 0 <= rating.value <= 5
    ):
        return None
    return rating


def _append_related_from_node(
    output: list[RelatedRef],
    seen: set[tuple[str, str, str]],
    node: _HtmlNode,
    *,
    field: str,
    value_text: str,
    base_url: str,
    profile: str,
) -> None:
    anchors = [candidate for candidate in node.walk() if candidate.tag == "a"]
    related_kind = cast(SearchKind, field)
    if anchors:
        for anchor in anchors:
            label = anchor.text_content().strip()
            if not label:
                continue
            url = resolve_semantic_ref_url(
                base_url,
                anchor.attrs.get("href", ""),
                profile=profile,
                kind=related_kind,
            )
            _append_related(
                output,
                seen,
                RelatedRef(kind=related_kind, label=label, url=url),
            )
        return
    if value_text:
        _append_related(
            output,
            seen,
            RelatedRef(kind=related_kind, label=value_text),
        )


def _append_related(
    output: list[RelatedRef],
    seen: set[tuple[str, str, str]],
    item: RelatedRef,
) -> None:
    key = (item.kind, item.label.casefold(), item.url or "")
    if key in seen:
        return
    seen.add(key)
    output.append(item)


def _node_or_ancestor_is_hidden(node: _HtmlNode) -> bool:
    current: _HtmlNode | None = node
    while current and current.tag != "document":
        attrs = current.attrs
        style = attrs.get("style", "").replace(" ", "").lower()
        if (
            current.tag == "template"
            or "hidden" in attrs
            or attrs.get("aria-hidden", "").strip().lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            return True
        current = current.parent
    return False


def _magnet_context(node: _HtmlNode) -> _HtmlNode:
    ancestors: list[_HtmlNode] = []
    current: _HtmlNode | None = node
    while current and current.tag != "document":
        ancestors.append(current)
        current = current.parent

    for ancestor in ancestors:
        if ancestor.tag in {"tr", "li"}:
            return ancestor
        classes = ancestor.class_tokens
        if "item" in classes and (
            "columns" in classes or any("magnet" in token for token in classes)
        ):
            return ancestor
        if classes.intersection(
            {
                "magnet-row",
                "magnet-item",
                "torrent-row",
                "torrent-item",
                "download-row",
                "download-item",
            }
        ):
            return ancestor

    for ancestor in ancestors:
        if ancestor.class_tokens.intersection(_MAGNET_CONTAINER_CLASSES):
            return ancestor
    return node.parent or node


def _with_reported_magnet_metadata(
    magnet: MagnetInfo, context: _HtmlNode
) -> MagnetInfo:
    size_text, size_bytes = _reported_size(context.text_content())
    return replace(
        magnet,
        reported_size_text=size_text,
        reported_size_bytes=size_bytes,
        badges=_badge_texts(context),
    )


def _reported_size(value: str) -> tuple[str | None, int | None]:
    match = SIZE_RE.search(value)
    if not match:
        return None, None

    amount_text = match.group("amount")
    normalized_amount = amount_text.replace(",", ".")
    unit = _canonical_size_unit(match.group("unit"))
    powers = {
        "B": 0,
        "KB": 1,
        "KiB": 1,
        "MB": 2,
        "MiB": 2,
        "GB": 3,
        "GiB": 3,
        "TB": 4,
        "TiB": 4,
    }
    try:
        size_bytes = int(Decimal(normalized_amount) * (1024 ** powers[unit]))
    except (InvalidOperation, KeyError):
        return f"{amount_text} {unit}", None
    return f"{amount_text} {unit}", size_bytes


def _canonical_size_unit(value: str) -> str:
    upper = value.upper()
    return {"KIB": "KiB", "MIB": "MiB", "GIB": "GiB", "TIB": "TiB"}.get(upper, upper)


def _badge_texts(context: _HtmlNode) -> tuple[str, ...]:
    badges: list[str] = []
    for node in context.walk():
        if not any(
            token == marker
            or token.startswith(marker + "-")
            or token.endswith("-" + marker)
            for token in node.class_tokens
            for marker in _BADGE_CLASS_MARKERS
        ):
            continue
        value = node.text_content().strip(" \t\r\n,，|/")
        if (
            not value
            or len(value) > 24
            or SIZE_RE.search(value)
            or DATE_RE.match(value)
        ):
            continue
        if value not in badges:
            badges.append(value)
    return tuple(badges)


def _magnet_richness(magnet: MagnetInfo) -> int:
    return sum(
        (
            bool(magnet.display_name),
            bool(magnet.trackers),
            bool(magnet.exact_length),
            bool(magnet.reported_size_text),
            bool(magnet.reported_size_bytes),
            bool(magnet.badges),
        )
    ) + len(magnet.params)


def _extract_source_images(
    page: str, base_url: str, *, profile: str
) -> tuple["SourceImage", ...]:
    from jav_pilot.core.models import SourceImage

    document = _parse_document(page or "")
    images: list[SourceImage] = []
    images_with_anchor: set[int] = set()
    host_resolver = PublicHostResolver(max_hosts=16)
    candidates_checked = 0

    for node in document.walk():
        if node.tag != "a":
            continue
        if candidates_checked >= MAX_IMAGE_CANDIDATES:
            break
        candidates_checked += 1
        image_node = next(
            (candidate for candidate in node.walk() if candidate.tag == "img"), None
        )
        if profile == "javdb" and _is_video_artwork(node):
            continue
        href = _absolute_image_url(
            base_url,
            node.attrs.get("href", ""),
            profile=profile,
            host_resolver=host_resolver,
        )
        if not href:
            continue
        kind = _source_image_kind(node, href, profile=profile)
        if not kind or _is_noise_image(node, href, image_node):
            continue
        thumbnail = _thumbnail_url(
            image_node,
            base_url,
            profile=profile,
            full_url=href,
            host_resolver=host_resolver,
        )
        width, height = _image_dimensions(image_node)
        if (
            profile == "javbus"
            and kind == "backdrop"
            and thumbnail
            and canonical_image_url_key(thumbnail) != canonical_image_url_key(href)
        ):
            _append_source_image(
                images,
                SourceImage(kind="cover", url=thumbnail, width=width, height=height),
            )
        _append_source_image(
            images,
            SourceImage(
                kind=kind, url=href, thumbnail_url=thumbnail, width=width, height=height
            ),
        )
        if image_node:
            images_with_anchor.add(id(image_node))

    for node in document.walk():
        if (
            node.tag != "img"
            or id(node) in images_with_anchor
            or _nearest_ancestor(node, "a") is not None
        ):
            continue
        if candidates_checked >= MAX_IMAGE_CANDIDATES:
            break
        candidates_checked += 1
        if profile == "javdb" and _is_video_artwork(node):
            continue
        source = _preferred_image_source(node)
        url = _absolute_image_url(
            base_url,
            source,
            profile=profile,
            host_resolver=host_resolver,
        )
        if not url:
            continue
        kind = _source_image_kind(node, url, profile=profile)
        if not kind or _is_noise_image(node, url, node):
            continue
        width, height = _image_dimensions(node)
        _append_source_image(
            images,
            SourceImage(kind=kind, url=url, width=width, height=height),
        )

    return normalize_source_images(images, limit=MAX_SOURCE_IMAGES)


def _append_source_image(images: list[Any], candidate: Any) -> None:
    images.append(candidate)


def _source_image_kind(node: _HtmlNode, url: str, *, profile: str) -> str | None:
    classes = _context_class_tokens(node)
    attributes = _context_attribute_signals(node)
    path = urlsplit(url).path.lower()
    explicit_sample = any(
        marker in token
        for token in classes
        for marker in ("gallery", "preview", "sample-box", "samples")
    ) or any(marker in attributes for marker in ("fancybox", "gallery", "preview"))
    explicit_backdrop = profile == "javbus" and any(
        marker in token for token in classes for marker in ("backdrop", "bigimage")
    )
    explicit_cover = any(
        marker in token
        for token in classes
        for marker in ("column-video-cover", "video-cover", "cover")
    )
    if explicit_sample or "/sample/" in path or "/samples/" in path:
        return "sample"
    if explicit_backdrop:
        return "backdrop"
    if explicit_cover or "/cover/" in path or "/covers/" in path:
        return "cover"
    if profile == "javbus" and "/pics/thumb/" in path:
        return "cover"
    if profile == "javdb" and any("fancybox" in token for token in classes):
        return "sample"
    return None


def _is_video_artwork(node: _HtmlNode) -> bool:
    context: list[_HtmlNode] = []
    current: _HtmlNode | None = node
    for _ in range(8):
        if current is None or current.tag == "document":
            break
        context.append(current)
        current = current.parent
    context.extend(candidate for candidate in node.walk() if candidate is not node)

    return any(
        _element_has_video_artwork_signals(
            candidate.tag, candidate.class_tokens, candidate.attrs
        )
        for candidate in context
    )


def _element_has_video_artwork_signals(
    tag: str,
    classes: frozenset[str],
    attrs: dict[str, str],
) -> bool:
    if tag in {"video", "source"}:
        return True
    for token in classes:
        normalized_token = token.replace("_", "-")
        if any(marker in normalized_token for marker in _VIDEO_ARTWORK_CLASS_MARKERS):
            return True
    for key, value in attrs.items():
        normalized_key = key.strip().casefold().replace("_", "-")
        normalized_value = value.strip().casefold()
        if normalized_key in _VIDEO_ARTWORK_ATTRIBUTE_MARKERS:
            return True
        if normalized_key in {"data-fancybox", "data-type"} and any(
            marker in normalized_value for marker in ("trailer", "video")
        ):
            return True
        if _looks_like_video_reference(normalized_value):
            return True
    return False


def _looks_like_video_reference(value: str) -> bool:
    if not value:
        return False
    try:
        path = urlsplit(value).path.casefold()
    except ValueError:
        return False
    return any(path.endswith(extension) for extension in _VIDEO_EXTENSIONS)


def _javdb_search_cover_candidate(
    attrs: dict[str, str],
    ancestors: list[tuple[str, frozenset[str], dict[str, str]]],
) -> str | None:
    source = _preferred_raw_image_source(attrs)
    if not source:
        return None
    try:
        path = urlsplit(html.unescape(source)).path.casefold()
    except ValueError:
        return None
    if not _looks_like_raster_path(path):
        return None

    contexts = [
        *ancestors,
        ("img", frozenset(attrs.get("class", "").casefold().split()), attrs),
    ]
    if any(
        _element_has_video_artwork_signals(tag, classes, values)
        for tag, classes, values in contexts
    ):
        return None
    has_cover_path = "/cover/" in path or "/covers/" in path
    has_cover_context = any(
        _is_cover_class_token(token) for _, classes, _ in contexts for token in classes
    )
    return source if has_cover_path or has_cover_context else None


def _preferred_raw_image_source(attrs: dict[str, str]) -> str:
    for key in ("data-original", "data-src", "data-lazy-src", "src"):
        value = attrs.get(key, "").strip()
        if value:
            return value
    return ""


def _is_cover_class_token(value: str) -> bool:
    token = value.casefold().replace("_", "-")
    return (
        token == "cover"
        or token.startswith("cover-")
        or token.endswith("-cover")
        or "-cover-" in token
    )


def _context_class_tokens(node: _HtmlNode, *, max_depth: int = 6) -> frozenset[str]:
    tokens: set[str] = set()
    current: _HtmlNode | None = node
    for _ in range(max_depth):
        if current is None or current.tag == "document":
            break
        tokens.update(current.class_tokens)
        current = current.parent
    return frozenset(tokens)


def _context_attribute_signals(node: _HtmlNode, *, max_depth: int = 6) -> str:
    signals: list[str] = []
    current: _HtmlNode | None = node
    for _ in range(max_depth):
        if current is None or current.tag == "document":
            break
        for key, value in current.attrs.items():
            if key not in {"href", "src", "data-src", "data-original", "style"}:
                signals.extend((key.lower(), value.lower()))
        current = current.parent
    return " ".join(signals)


def _absolute_image_url(
    base_url: str,
    value: str,
    *,
    profile: str,
    host_resolver: PublicHostResolver | None = None,
) -> str | None:
    raw = html.unescape(value or "").strip()
    if not raw or raw.startswith(("data:", "javascript:")):
        return None
    absolute = urljoin(base_url.rstrip("/") + "/", raw)
    try:
        parsed = urlsplit(absolute)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if profile == "javdb":
        base_host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
        image_host = parsed.hostname.rstrip(".").lower()
        resolver = host_resolver or PublicHostResolver(max_hosts=1)
        if image_host != base_host and not resolver.is_public(image_host):
            return None
    path = parsed.path.lower()
    if profile == "javbus":
        base_host = (urlsplit(base_url).hostname or "").lower()
        image_host = (parsed.hostname or "").lower()
        is_site_image = image_host == base_host and "/pics/" in path
        site_domain = base_host.removeprefix("www.")
        is_site_cdn_image = (
            bool(site_domain)
            and image_host == f"pics.{site_domain}"
            and path.startswith(_JAVBUS_CDN_IMAGE_PATHS)
        )
        dmm_path_prefixes = _JAVBUS_DMM_IMAGE_PATH_PREFIXES.get(image_host, ())
        is_dmm_image = any(path.startswith(prefix) for prefix in dmm_path_prefixes)
        if not (
            is_site_image or is_site_cdn_image or is_dmm_image
        ) or not _looks_like_raster_path(path):
            return None
    elif not _looks_like_raster_path(path):
        return None
    return parsed._replace(fragment="").geturl()


def _same_origin_http_url(base_url: str, value: str) -> str | None:
    absolute = urljoin(base_url.rstrip("/") + "/", html.unescape(value or "").strip())
    try:
        base = urlsplit(base_url)
        parsed = urlsplit(absolute)
        base_port = base.port or (443 if base.scheme.lower() == "https" else 80)
        parsed_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or not base.hostname
    ):
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if (parsed.scheme.lower(), parsed.hostname.lower(), parsed_port) != (
        base.scheme.lower(),
        base.hostname.lower(),
        base_port,
    ):
        return None
    return parsed._replace(fragment="").geturl()


def _looks_like_raster_path(path: str) -> bool:
    return any(path.endswith(extension) for extension in _IMAGE_EXTENSIONS)


def _thumbnail_url(
    image_node: _HtmlNode | None,
    base_url: str,
    *,
    profile: str,
    full_url: str,
    host_resolver: PublicHostResolver | None = None,
) -> str | None:
    if image_node is None:
        return None
    for key in ("data-original", "data-src", "data-lazy-src", "src"):
        candidate = _absolute_image_url(
            base_url,
            image_node.attrs.get(key, ""),
            profile=profile,
            host_resolver=host_resolver,
        )
        if candidate and candidate != full_url and not _url_looks_noisy(candidate):
            return candidate
    return None


def _preferred_image_source(node: _HtmlNode) -> str:
    for key in ("data-original", "data-src", "data-lazy-src", "src"):
        value = node.attrs.get(key, "").strip()
        if value:
            return value
    return ""


def _image_dimensions(node: _HtmlNode | None) -> tuple[int | None, int | None]:
    if node is None:
        return None, None
    return _positive_dimension(node.attrs.get("width")), _positive_dimension(
        node.attrs.get("height")
    )


def _positive_dimension(value: str | None) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return None
    return parsed if 0 < parsed <= 20_000 else None


def _is_noise_image(node: _HtmlNode, url: str, image_node: _HtmlNode | None) -> bool:
    class_text = " ".join(_context_class_tokens(node)).lower()
    if any(
        marker in class_text or marker in url.lower() for marker in _IMAGE_NOISE_MARKERS
    ):
        return True
    width, height = _image_dimensions(image_node)
    return bool(width and height and width <= 64 and height <= 64)


def _url_looks_noisy(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in _IMAGE_NOISE_MARKERS)


def _nearest_ancestor(node: _HtmlNode, tag: str) -> _HtmlNode | None:
    current = node.parent
    while current and current.tag != "document":
        if current.tag == tag:
            return current
        current = current.parent
    return None


def extract_javbus_magnet_endpoint(page: str) -> JavBusMagnetEndpoint | None:
    values: dict[str, str] = {}
    for match in JAVBUS_JS_VAR_RE.finditer(page):
        name = match.group("name").lower()
        value = html.unescape(match.group("value")).strip().strip("'\"")
        if value:
            values[name] = value

    gid = values.get("gid")
    img = values.get("img")
    if not gid or not img:
        return None

    return JavBusMagnetEndpoint(
        gid=gid,
        uc=values.get("uc") or "0",
        img=img,
        lang=values.get("lang") or "zh",
    )


class _LinkCard:
    def __init__(self, href: str, *, magnet_hint: MagnetHint = "unknown") -> None:
        self.href = href
        self.text_parts: list[str] = []
        self.title: str | None = None
        self.code: str | None = None
        self.date: str | None = None
        self.cover_url: str | None = None
        self.rating_parts: list[str] = []
        self.magnet_hint = magnet_hint

    def append_text(self, text: str) -> None:
        clean = " ".join(text.split())
        if clean:
            self.text_parts.append(clean)

    def to_result(self, source: str, base_url: str) -> SearchResult | None:
        title = self.title or " ".join(self.text_parts)
        title = " ".join(title.split())
        if not title:
            return None
        detail_url = _same_origin_http_url(base_url, self.href)
        if not detail_url:
            return None
        code = self.code
        if not code:
            path_tail = urlsplit(detail_url).path.rstrip("/").rsplit("/", 1)[-1]
            if _looks_like_code(path_tail):
                code = path_tail
        # A generic movie-box wrapper can contain navigation anchors.  Only
        # retain JavBus cards that identify an actual catalogue entry. JavDB
        # can legitimately expose cards before a code is rendered in markup.
        if source == "javbus" and not code:
            return None
        return SearchResult(
            source=source,
            title=title,
            url=detail_url,
            code=code,
            date=self.date,
            details=SourceDetails(
                rating=_five_point_rating(" ".join(self.rating_parts))
            ),
            magnet_hint=self.magnet_hint,
            metadata={
                "metadata_only": True,
                "cover": (
                    _absolute_image_url(base_url, self.cover_url, profile=source)
                    if self.cover_url
                    else None
                ),
            },
        )


class JavBusSearchParser(HTMLParser):
    def __init__(self, base_url: str, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.limit = limit
        self.cards: list[_LinkCard] = []
        self._current: _LinkCard | None = None
        self._capture_tag: str | None = None
        self._movie_box_depth = 0
        self._movie_box_stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        class_name = attrs_dict.get("class", "")
        class_tokens = set(class_name.casefold().split())
        in_movie_box = self._movie_box_depth > 0

        if tag == "a" and attrs_dict.get("href") and (
            "movie-box" in class_tokens or in_movie_box
        ):
            self._current = _LinkCard(attrs_dict["href"], magnet_hint="available")
            return

        if tag in {"div", "article", "section"}:
            starts_movie_box = "movie-box" in class_tokens
            self._movie_box_stack.append(starts_movie_box)
            if starts_movie_box:
                self._movie_box_depth += 1

        if self._current:
            if tag == "img":
                title = attrs_dict.get("title") or attrs_dict.get("alt")
                if title:
                    self._current.title = title
                src = _preferred_raw_image_source(attrs_dict)
                if src:
                    self._current.cover_url = src
            if tag in {"date", "span"}:
                self._capture_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if self._current:
            if self._capture_tag == tag:
                self._capture_tag = None
            if tag == "a":
                result = self._current.to_result("javbus", self.base_url)
                if result:
                    self.cards.append(self._current)
                self._current = None
        if tag in {"div", "article", "section"} and self._movie_box_stack:
            if self._movie_box_stack.pop():
                self._movie_box_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._current:
            return
        self._current.append_text(data)
        clean = " ".join(data.split())
        if not clean:
            return
        if self._capture_tag == "date" and not self._current.date:
            if _looks_like_code(clean) and not self._current.code:
                self._current.code = clean
            else:
                self._current.date = clean
        if (
            self._capture_tag == "span"
            and not self._current.code
            and _looks_like_code(clean)
        ):
            self._current.code = clean

    def results(self) -> tuple[SearchResult, ...]:
        output: list[SearchResult] = []
        for card in self.cards[: self.limit]:
            result = card.to_result("javbus", self.base_url)
            if result:
                output.append(result)
        return tuple(output)


class JavDbSearchParser(HTMLParser):
    def __init__(self, base_url: str, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.limit = limit
        self.cards: list[_LinkCard] = []
        self._current: _LinkCard | None = None
        self._stack = 0
        self._last_strong_text: str | None = None
        self._badge_stack: list[bool] = []
        self._element_stack: list[tuple[str, frozenset[str], dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        class_name = attrs_dict.get("class", "")

        if tag == "a" and "box" in class_name and attrs_dict.get("href"):
            href = attrs_dict["href"]
            if href.startswith("/v/") or "/v/" in href:
                self._current = _LinkCard(href, magnet_hint="unavailable")
                title = attrs_dict.get("title")
                if title:
                    self._current.title = title
                    first_token = title.split(maxsplit=1)[0]
                    if _looks_like_code(first_token):
                        self._current.code = first_token
                self._stack = 1
                self._badge_stack = [False]
                self._element_stack = [
                    (tag, frozenset(class_name.casefold().split()), attrs_dict)
                ]
                return

        if self._current:
            if tag == "img" and not self._current.cover_url:
                self._current.cover_url = _javdb_search_cover_candidate(
                    attrs_dict, self._element_stack
                )
            if tag not in _VOID_TAGS:
                self._stack += 1
                classes = frozenset(class_name.lower().split())
                in_badge = (
                    bool(self._badge_stack and self._badge_stack[-1])
                    or "tag" in classes
                )
                self._badge_stack.append(in_badge)
                self._element_stack.append((tag, classes, attrs_dict))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self._current:
            return
        lowered = tag.casefold()
        if lowered in _VOID_TAGS:
            return
        match_index = next(
            (
                index
                for index in range(len(self._element_stack) - 1, -1, -1)
                if self._element_stack[index][0].casefold() == lowered
            ),
            None,
        )
        if match_index is None:
            return
        pop_count = len(self._element_stack) - match_index
        del self._element_stack[match_index:]
        if pop_count:
            del self._badge_stack[-pop_count:]
        self._stack -= pop_count
        if self._stack <= 0:
            if self._last_strong_text and not self._current.title:
                self._current.title = self._last_strong_text
            self.cards.append(self._current)
            self._current = None
            self._stack = 0
            self._last_strong_text = None
            self._badge_stack = []
            self._element_stack = []

    def handle_data(self, data: str) -> None:
        if not self._current:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        self._current.append_text(clean)
        in_score = any("score" in classes for _, classes, _ in self._element_stack)
        if in_score:
            self._current.rating_parts.append(clean)
        if (
            self._badge_stack
            and self._badge_stack[-1]
            and clean.casefold() in _MAGNET_BADGE_TEXTS
        ):
            self._current.magnet_hint = "available"
        if not self._current.code and _looks_like_code(clean):
            self._current.code = clean
        if not self._current.date and DATE_RE.match(clean):
            self._current.date = clean
        if len(clean) > 8 and not DATE_RE.match(clean) and not in_score:
            self._last_strong_text = clean

    def results(self) -> tuple[SearchResult, ...]:
        output: list[SearchResult] = []
        for card in self.cards[: self.limit]:
            result = card.to_result("javdb", self.base_url)
            if result:
                output.append(result)
        return tuple(output)


def _looks_like_code(value: str) -> bool:
    return looks_like_catalog_code(value)
