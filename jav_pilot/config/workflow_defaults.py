"""Defaults for everyday workflows, edited on the 工作流默认参数 page.

They seed the search forms, Web download batches, metadata automation,
translation and search history. Each form still remembers the last values a
browser used; these defaults are the starting point and the reset target.
"""

from __future__ import annotations

import copy
from typing import Any

from ..web_download.variant import WEB_DOWNLOAD_VARIANTS

SEARCH_KINDS = ("keyword", "code", "actor", "tag", "series", "maker", "publisher", "director")
PAGE_SIZES = (10, 20, 50, 100)
QUALITY_HEIGHTS = (4320, 2160, 1440, 1080, 720, 480)
EXISTING_POLICIES = ("higher_quality", "overwrite", "skip")

DEFAULT_WORKFLOW_DEFAULTS: dict[str, Any] = {
    "search": {
        "site_mode": "all",
        "sources": [],
        "result_limit": 100,
        "page_size": 20,
        "fetch_magnets": True,
        "exact_match": False,
        "search_kind": "keyword",
    },
    "resource_search": {
        "result_limit": 100,
        "exact_match": False,
        "max_height": 2160,
        "default_quality": "highest",
        "variant_priority": list(WEB_DOWNLOAD_VARIANTS),
        "existing_policy": "higher_quality",
    },
    "translation": {
        "enabled": True,
        "show_original": False,
    },
    "search_history_limit": 100,
    "metadata_auto_fallback": True,
    "metadata_auto_complete": True,
}


class WorkflowDefaultsError(ValueError):
    pass


def normalize_workflow_defaults(value: object, site_ids: set[str]) -> dict[str, Any]:
    """Validate a stored or submitted value, filling any missing field."""

    defaults = copy.deepcopy(DEFAULT_WORKFLOW_DEFAULTS)
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise WorkflowDefaultsError("工作流默认参数格式无效")
    unknown = set(value) - set(defaults)
    if unknown:
        raise WorkflowDefaultsError("工作流默认参数包含未识别字段")

    search = _section(value, "search", defaults)
    search["site_mode"] = _choice(search["site_mode"], ("all", "custom"), "默认站点模式")
    sources = search["sources"]
    if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
        raise WorkflowDefaultsError("默认搜索站点无效")
    search["sources"] = [item for item in dict.fromkeys(sources) if item in site_ids]
    search["result_limit"] = _int(search["result_limit"], 1, 999, "默认结果上限")
    search["page_size"] = _choice(search["page_size"], PAGE_SIZES, "默认每页显示")
    search["fetch_magnets"] = _bool(search["fetch_magnets"], "默认解析磁链")
    search["exact_match"] = _bool(search["exact_match"], "默认精确匹配")
    search["search_kind"] = _choice(search["search_kind"], SEARCH_KINDS, "默认搜索类别")

    resource = _section(value, "resource_search", defaults)
    resource["result_limit"] = _int(resource["result_limit"], 1, 999, "Web 搜索默认结果上限")
    resource["exact_match"] = _bool(resource["exact_match"], "Web 搜索默认精确匹配")
    resource["max_height"] = _choice(resource["max_height"], QUALITY_HEIGHTS, "默认画质上限")
    quality = resource["default_quality"]
    if quality != "highest":
        quality = _choice(quality, QUALITY_HEIGHTS, "默认画质")
        if quality > resource["max_height"]:
            raise WorkflowDefaultsError("默认画质不能高于画质上限")
    resource["default_quality"] = quality
    priority = resource["variant_priority"]
    if (
        not isinstance(priority, list)
        or sorted(priority) != sorted(WEB_DOWNLOAD_VARIANTS)
    ):
        raise WorkflowDefaultsError("分类优先级无效")
    resource["existing_policy"] = _choice(
        resource["existing_policy"], EXISTING_POLICIES, "已有作品处理方式"
    )

    translation = _section(value, "translation", defaults)
    translation["enabled"] = _bool(translation["enabled"], "默认翻译")
    translation["show_original"] = _bool(translation["show_original"], "显示原文")

    return {
        "search": search,
        "resource_search": resource,
        "translation": translation,
        "search_history_limit": _int(
            value.get("search_history_limit", defaults["search_history_limit"]),
            10,
            1000,
            "搜索记录保留条数",
        ),
        "metadata_auto_fallback": _bool(
            value.get("metadata_auto_fallback", defaults["metadata_auto_fallback"]),
            "元数据自动换源",
        ),
        "metadata_auto_complete": _bool(
            value.get("metadata_auto_complete", defaults["metadata_auto_complete"]),
            "新入库作品自动补全",
        ),
    }


def _section(value: dict[str, Any], name: str, defaults: dict[str, Any]) -> dict[str, Any]:
    raw = value.get(name, {})
    if not isinstance(raw, dict):
        raise WorkflowDefaultsError(f"工作流默认参数 {name} 格式无效")
    if set(raw) - set(defaults[name]):
        raise WorkflowDefaultsError(f"工作流默认参数 {name} 包含未识别字段")
    return {**defaults[name], **raw}


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowDefaultsError(f"{label}必须是开关值")
    return value


def _int(value: object, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WorkflowDefaultsError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def _choice(value: Any, allowed: tuple[Any, ...], label: str) -> Any:
    if isinstance(value, bool) or value not in allowed:
        raise WorkflowDefaultsError(f"{label}无效")
    return value
