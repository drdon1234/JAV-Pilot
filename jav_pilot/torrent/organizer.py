from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from ..config.app_config import QbittorrentConfig
from .qbittorrent import AddDownloadRequest, resolve_download_destination


def apply_organizer(
    request: AddDownloadRequest,
    settings: dict[str, Any],
) -> tuple[AddDownloadRequest, dict[str, Any] | None]:
    organizer = settings.get("organizer", {})
    if not organizer.get("enabled", True):
        return request, None

    rule = classify_download(request, settings)
    if not rule:
        return request, None

    actions = rule.get("actions", {})
    updated = replace(
        request,
        category=str(actions.get("category") or request.category).strip(),
        save_path=str(actions.get("save_path") or request.save_path).strip(),
        tags=str(actions.get("tags") or request.tags).strip(),
    )
    return updated, _rule_summary(rule)


def classify_download(request: AddDownloadRequest, settings: dict[str, Any]) -> dict[str, Any] | None:
    rules = settings.get("organizer", {}).get("rules", [])
    ordered = sorted(
        (rule for rule in rules if isinstance(rule, dict) and rule.get("enabled", True)),
        key=lambda rule: int(
            1000 if rule.get("priority") is None else rule.get("priority")
        ),
    )
    for rule in ordered:
        if _matches_rule(rule, request):
            return rule
    return None


def preview_organizer(
    payload: dict[str, Any],
    settings: dict[str, Any],
    config: QbittorrentConfig,
) -> dict[str, Any]:
    request = AddDownloadRequest(
        magnet=str(payload.get("magnet") or "magnet:?xt=urn:btih:0000000000000000000000000000000000000000"),
        name=str(payload.get("name") or "").strip(),
        category=str(payload.get("category") or "").strip(),
        save_path=str(payload.get("save_path") or "").strip(),
        tags=str(payload.get("tags") or "").strip(),
        result=payload.get("result") if isinstance(payload.get("result"), dict) else {},
        magnet_info=payload.get("magnet_info") if isinstance(payload.get("magnet_info"), dict) else {},
        auto_organize=True,
    )
    request, rule = apply_organizer(request, settings)
    if not rule:
        return {"matched": False, "rule": None, "destination": None}
    category, save_path = resolve_download_destination(request, config)
    return {
        "matched": True,
        "rule": rule,
        "destination": {
            "category": category,
            "save_path": save_path,
            "tags": request.tags or config.tags,
        },
    }


def _matches_rule(rule: dict[str, Any], request: AddDownloadRequest) -> bool:
    match = rule.get("match", {})
    result = request.result or {}
    magnet_info = request.magnet_info or {}
    result_sources = _result_sources(result, magnet_info)
    code = str(result.get("code") or "").strip()
    title = str(result.get("title") or "").strip()
    magnet_name = request.name or str(magnet_info.get("display_name") or "")

    sources = [str(item).lower() for item in match.get("sources", []) if str(item).strip()]
    if sources and not result_sources.intersection(sources):
        return False

    code_regex = str(match.get("code_regex") or "").strip()
    if code_regex and not re.search(code_regex, code, flags=re.IGNORECASE):
        return False

    title_terms = [str(item).lower() for item in match.get("title_contains", []) if str(item).strip()]
    title_text = title.lower()
    if title_terms and not any(term in title_text for term in title_terms):
        return False

    magnet_terms = [str(item).lower() for item in match.get("magnet_name_contains", []) if str(item).strip()]
    magnet_text = magnet_name.lower()
    if magnet_terms and not any(term in magnet_text for term in magnet_terms):
        return False

    return True


def _result_sources(result: dict[str, Any], magnet_info: dict[str, Any]) -> set[str]:
    magnet_source = magnet_info.get("source_id")
    if isinstance(magnet_source, str) and magnet_source.strip():
        return {magnet_source.strip().lower()}

    result_source = result.get("source")
    if isinstance(result_source, str) and result_source.strip():
        return {result_source.strip().lower()}

    values: list[object] = []
    raw_sources = result.get("sources")
    if isinstance(raw_sources, list):
        values.extend(
            source.get("source_id")
            for source in raw_sources
            if isinstance(source, dict)
        )
    return {
        str(value).strip().lower()
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _rule_summary(rule: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rule:
        return None
    return {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "priority": rule.get("priority"),
        "actions": rule.get("actions", {}),
    }
