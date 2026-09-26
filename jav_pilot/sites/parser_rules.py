from __future__ import annotations

import copy
from typing import Any

import soupsieve


PARSER_RULES_SCHEMA_VERSION = 1
PARSER_RULE_MODES = frozenset({"inherit", "custom"})
PARSER_ATTRIBUTES = frozenset(
    {
        "text",
        "href",
        "src",
        "title",
        "alt",
        "content",
        "datetime",
        "poster",
        "data-src",
        "data-href",
        "data-url",
        "data-original",
        "data-lazy-src",
    }
)
DETAIL_FIELD_NAMES = (
    "title",
    "original_title",
    "release_date",
    "duration",
    "rating",
    "maker",
    "publisher",
    "series",
    "director",
    "actor",
    "tag",
)
IMAGE_KINDS = ("cover", "backdrop", "sample")
MAGNET_FIELD_NAMES = ("uri", "name", "size", "badges")
MAX_SELECTOR_LENGTH = 240
MAX_SELECTOR_GROUPS = 8
MAX_SELECTOR_COMPONENTS = 24


class ParserRulesError(ValueError):
    pass


def _value_rule(
    selector: str,
    *attributes: str,
    index: int | None = None,
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "selector": selector,
        "attributes": list(attributes or ("text",)),
    }
    if index is not None:
        rule["index"] = index
    return rule


_COMMON_MAGNET_RULES = {
    "item_selector": (
        '#magnets-content .item, #magnets-content a[href^="magnet:"], '
        '#magnet-table tr, a[href^="magnet:"]'
    ),
    "uri": _value_rule(":scope", "href", "data-href", "text"),
    "name": _value_rule('.name, .magnet-name, a[href^="magnet:"]', "text", "title"),
    "size": _value_rule(".size, .meta", "text"),
    "badges": _value_rule(".tag, .badge, .label", "text"),
}


DEFAULT_PARSER_RULES: dict[str, dict[str, Any]] = {
    "javbus": {
        "schema_version": PARSER_RULES_SCHEMA_VERSION,
        "search": {
            # Some JavBus mirrors wrap the detail anchor in a movie-box
            # container instead of putting the class on the anchor itself.
            "item_selector": "a.movie-box, .movie-box a[href]",
            "empty_selector": "",
            "ready_selector": "a.movie-box, .movie-box a[href]",
            "detail_url": _value_rule(":scope", "href", "data-href"),
            "title": _value_rule("img", "title", "alt", "text"),
            "code": _value_rule("date, span", "text"),
            "date": _value_rule("date, time", "datetime", "text"),
            "rating": _value_rule("", "text"),
            "cover": _value_rule(
                "img", "data-original", "data-src", "data-lazy-src", "src"
            ),
            "magnet_available_selector": ":scope",
            "default_magnet_hint": "available",
        },
        "detail": {
            "fields": {
                "title": _value_rule("h3, h1, h2", "text"),
                "original_title": _value_rule(
                    ".origin-title, .original-title, .original_title", "text"
                ),
                "release_date": _value_rule("", "text"),
                "duration": _value_rule("", "text"),
                "rating": _value_rule("", "text"),
                "maker": _value_rule(
                    'a[href^="/studio/"], a[href^="/studios/"], '
                    'a[href^="/maker/"], a[href^="/makers/"]',
                    "text",
                    "title",
                ),
                "publisher": _value_rule(
                    'a[href^="/label/"], a[href^="/labels/"], '
                    'a[href^="/publisher/"], a[href^="/publishers/"]',
                    "text",
                    "title",
                ),
                "series": _value_rule('a[href^="/series/"]', "text", "title"),
                "director": _value_rule(
                    'a[href^="/director/"], a[href^="/directors/"]',
                    "text",
                    "title",
                ),
                "actor": _value_rule(
                    'a[href^="/star/"], a[href^="/stars/"], '
                    'a[href^="/actor/"], a[href^="/actors/"]',
                    "text",
                    "title",
                ),
                "tag": _value_rule(
                    'a[href^="/genre/"], a[href^="/genres/"], '
                    'a[href^="/tag/"], a[href^="/tags/"]',
                    "text",
                    "title",
                ),
            },
            "images": {
                "cover": _value_rule(
                    'a.bigImage, .bigImage a, a[href*="/pics/cover/"]',
                    "href",
                    "data-src",
                    "src",
                ),
                "backdrop": _value_rule("", "href", "src"),
                "sample": _value_rule(
                    'a.sample-box, .sample-box a, a[href*="/pics/sample/"], '
                    'a[href*="/pics/samples/"]',
                    "href",
                    "data-src",
                    "src",
                ),
            },
            "magnets": copy.deepcopy(_COMMON_MAGNET_RULES),
        },
    },
    "javdb": {
        "schema_version": PARSER_RULES_SCHEMA_VERSION,
        "search": {
            "item_selector": 'a.box[href*="/v/"]',
            "empty_selector": ".empty-message",
            "ready_selector": 'a.box[href*="/v/"], .empty-message',
            "detail_url": _value_rule(":scope", "href", "data-href"),
            "title": _value_rule(":scope", "title", "text"),
            "code": _value_rule(".video-title strong, strong, .video-title", "text"),
            "date": _value_rule(".meta, time", "datetime", "text"),
            "rating": _value_rule(".score", "text"),
            "cover": _value_rule(
                ".video-cover img, img",
                "data-original",
                "data-src",
                "data-lazy-src",
                "src",
            ),
            "magnet_available_selector": ".tag.is-success",
            "default_magnet_hint": "unavailable",
        },
        "detail": {
            "fields": {
                "title": _value_rule(
                    ".current-title, h1.title, h2.title, h3.title", "text"
                ),
                "original_title": _value_rule(
                    ".origin-title, .original-title, .original_title", "text"
                ),
                "release_date": _value_rule("", "text"),
                "duration": _value_rule("", "text"),
                "rating": _value_rule("", "text"),
                "maker": _value_rule(
                    'a[href^="/makers/"], a[href^="/maker/"]', "text", "title"
                ),
                "publisher": _value_rule(
                    'a[href^="/publishers/"], a[href^="/publisher/"]',
                    "text",
                    "title",
                ),
                "series": _value_rule('a[href^="/series/"]', "text", "title"),
                "director": _value_rule(
                    'a[href^="/directors/"], a[href^="/director/"]',
                    "text",
                    "title",
                ),
                "actor": _value_rule(
                    '.video-meta-panel .panel-block .value '
                    'a:is([href^="/actors/"], [href^="/actor/"], '
                    '[href^="/stars/"], [href^="/star/"])',
                    "text",
                    "title",
                ),
                "tag": _value_rule(
                    'a[href^="/tags"], a[href^="/tag/"], '
                    'a[href^="/genres/"], a[href^="/genre/"]',
                    "text",
                    "title",
                ),
            },
            "images": {
                "cover": _value_rule(
                    ".column-video-cover a, .column-video-cover img",
                    "href",
                    "data-original",
                    "data-src",
                    "src",
                ),
                "backdrop": _value_rule("", "href", "src"),
                "sample": _value_rule(
                    '[data-fancybox] a, a[data-fancybox], a[href*="/samples/"], '
                    'a[href*="/sample/"]',
                    "href",
                    "data-src",
                    "src",
                ),
            },
            "magnets": copy.deepcopy(_COMMON_MAGNET_RULES),
        },
    },
}


def default_parser_rules(profile: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(DEFAULT_PARSER_RULES[profile])
    except KeyError as exc:
        raise ParserRulesError(f"unsupported parser profile: {profile}") from exc


def normalize_parser_rules(
    value: Any,
    *,
    profile: str,
    mode: str,
    path: str,
) -> dict[str, Any]:
    defaults = default_parser_rules(profile)
    if mode == "inherit":
        return defaults
    if mode != "custom":
        raise ParserRulesError(f"{path}_mode must be inherit or custom")
    if isinstance(value, dict) and "schema_version" in value:
        raw_schema_version = value.get("schema_version")
        if not isinstance(raw_schema_version, int) or isinstance(
            raw_schema_version, bool
        ):
            raise ParserRulesError(f"{path}.schema_version must be 1")
        schema_version = raw_schema_version
        if schema_version > PARSER_RULES_SCHEMA_VERSION:
            raise ParserRulesError(
                f"{path}.schema_version is newer than this application supports"
            )
        if schema_version != PARSER_RULES_SCHEMA_VERSION:
            raise ParserRulesError(f"{path}.schema_version must be 1")
    if not isinstance(value, dict):
        raise ParserRulesError(f"{path} must be an object in custom mode")
    _reject_unknown(value, {"schema_version", "search", "detail"}, path)

    search = _object(value.get("search"), f"{path}.search", fallback={})
    detail = _object(value.get("detail"), f"{path}.detail", fallback={})
    _reject_unknown(
        search,
        {
            "item_selector",
            "empty_selector",
            "ready_selector",
            "detail_url",
            "title",
            "code",
            "date",
            "rating",
            "cover",
            "magnet_available_selector",
            "default_magnet_hint",
        },
        f"{path}.search",
    )
    _reject_unknown(detail, {"fields", "images", "magnets"}, f"{path}.detail")

    default_search = defaults["search"]
    normalized_search = {
        "item_selector": _normalize_selector(
            search.get("item_selector", default_search["item_selector"]),
            f"{path}.search.item_selector",
            required=True,
        ),
        "empty_selector": _normalize_selector(
            search.get("empty_selector", default_search["empty_selector"]),
            f"{path}.search.empty_selector",
        ),
        "ready_selector": _normalize_selector(
            search.get("ready_selector", default_search["ready_selector"]),
            f"{path}.search.ready_selector",
        ),
    }
    for field in ("detail_url", "title", "code", "date", "rating", "cover"):
        normalized_search[field] = _normalize_value_rule(
            search.get(field),
            default_search[field],
            f"{path}.search.{field}",
        )
    normalized_search["magnet_available_selector"] = _normalize_selector(
        search.get(
            "magnet_available_selector",
            default_search["magnet_available_selector"],
        ),
        f"{path}.search.magnet_available_selector",
    )
    magnet_hint = str(
        search.get("default_magnet_hint", default_search["default_magnet_hint"])
    ).strip()
    if magnet_hint not in {"available", "unavailable", "unknown"}:
        raise ParserRulesError(
            f"{path}.search.default_magnet_hint must be available, unavailable, or unknown"
        )
    normalized_search["default_magnet_hint"] = magnet_hint

    fields = _object(detail.get("fields"), f"{path}.detail.fields", fallback={})
    images = _object(detail.get("images"), f"{path}.detail.images", fallback={})
    magnets = _object(detail.get("magnets"), f"{path}.detail.magnets", fallback={})
    _reject_unknown(fields, set(DETAIL_FIELD_NAMES), f"{path}.detail.fields")
    _reject_unknown(images, set(IMAGE_KINDS), f"{path}.detail.images")
    _reject_unknown(
        magnets,
        {"item_selector", *MAGNET_FIELD_NAMES},
        f"{path}.detail.magnets",
    )

    default_detail = defaults["detail"]
    normalized_fields = {
        field: _normalize_value_rule(
            fields.get(field),
            default_detail["fields"][field],
            f"{path}.detail.fields.{field}",
        )
        for field in DETAIL_FIELD_NAMES
    }
    normalized_images = {
        kind: _normalize_value_rule(
            images.get(kind),
            default_detail["images"][kind],
            f"{path}.detail.images.{kind}",
        )
        for kind in IMAGE_KINDS
    }
    normalized_magnets = {
        "item_selector": _normalize_selector(
            magnets.get("item_selector", default_detail["magnets"]["item_selector"]),
            f"{path}.detail.magnets.item_selector",
        )
    }
    for field in MAGNET_FIELD_NAMES:
        normalized_magnets[field] = _normalize_value_rule(
            magnets.get(field),
            default_detail["magnets"][field],
            f"{path}.detail.magnets.{field}",
        )

    return {
        "schema_version": PARSER_RULES_SCHEMA_VERSION,
        "search": normalized_search,
        "detail": {
            "fields": normalized_fields,
            "images": normalized_images,
            "magnets": normalized_magnets,
        },
    }


def _normalize_value_rule(
    value: Any,
    fallback: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    if value is None:
        return copy.deepcopy(fallback)
    if not isinstance(value, dict):
        raise ParserRulesError(f"{path} must be an object")
    _reject_unknown(value, {"selector", "attributes", "index"}, path)
    selector = _normalize_selector(
        value.get("selector", fallback["selector"]), f"{path}.selector"
    )
    attributes = value.get("attributes", fallback["attributes"])
    if not isinstance(attributes, list) or not 1 <= len(attributes) <= 6:
        raise ParserRulesError(f"{path}.attributes must contain 1 to 6 attributes")
    normalized_attributes: list[str] = []
    for raw_attribute in attributes:
        attribute = str(raw_attribute or "").strip().lower()
        if attribute not in PARSER_ATTRIBUTES:
            raise ParserRulesError(
                f"{path}.attributes contains unsupported attribute: {attribute}"
            )
        if attribute not in normalized_attributes:
            normalized_attributes.append(attribute)

    normalized: dict[str, Any] = {
        "selector": selector,
        "attributes": normalized_attributes,
    }
    raw_index = value.get("index", fallback.get("index"))
    if raw_index is not None:
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise ParserRulesError(f"{path}.index must be an integer from 0 to 49")
        index = raw_index
        if not 0 <= index <= 49:
            raise ParserRulesError(f"{path}.index must be an integer from 0 to 49")
        normalized["index"] = index
    return normalized


def _normalize_selector(value: Any, path: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ParserRulesError(f"{path} must be a CSS selector string")
    selector = value.strip()
    if not selector:
        if required:
            raise ParserRulesError(f"{path} is required")
        return ""
    if len(selector) > MAX_SELECTOR_LENGTH:
        raise ParserRulesError(
            f"{path} must be at most {MAX_SELECTOR_LENGTH} characters"
        )
    if any(ord(character) < 32 for character in selector):
        raise ParserRulesError(f"{path} contains control characters")
    lowered = selector.casefold()
    if any(
        marker in lowered
        for marker in (":has(", ":-soup-contains(", ":contains(", "javascript:")
    ):
        raise ParserRulesError(f"{path} uses an unsupported selector feature")
    if selector.count(",") + 1 > MAX_SELECTOR_GROUPS:
        raise ParserRulesError(
            f"{path} must contain at most {MAX_SELECTOR_GROUPS} selector groups"
        )
    component_count = (
        sum(selector.count(character) for character in (" ", ">", "+", "~")) + 1
    )
    if component_count > MAX_SELECTOR_COMPONENTS:
        raise ParserRulesError(f"{path} is too complex")
    try:
        soupsieve.compile(selector)
    except soupsieve.SelectorSyntaxError as exc:
        raise ParserRulesError(f"{path} is not a valid CSS selector: {exc}") from exc
    return selector


def _object(value: Any, path: str, *, fallback: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        return copy.deepcopy(fallback)
    if not isinstance(value, dict):
        raise ParserRulesError(f"{path} must be an object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ParserRulesError(f"{path} contains unsupported field: {unknown[0]}")
