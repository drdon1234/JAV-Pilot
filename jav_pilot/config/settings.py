from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .schema import SETTINGS_SCHEMA_VERSION as CURRENT_SETTINGS_SCHEMA_VERSION
from ..core.migrations import JSONMigration, MigrationError, migrate_json
from .qb_paths import QbPathError, resolve_qb_download_destination
from ..sites.parser_rules import (
    PARSER_RULE_MODES,
    ParserRulesError,
    default_parser_rules,
    normalize_parser_rules,
)
from ..core.storage import atomic_write_text, backup_file
from .paths import runtime_data_dir
from .source_catalog import METADATA_CATALOG, SEARCH_PROFILES, WEB_CATALOG, additional_sites
from .workflow_defaults import (
    DEFAULT_WORKFLOW_DEFAULTS,
    WorkflowDefaultsError,
    normalize_workflow_defaults,
)

_SETTINGS_LOCK = threading.RLock()
SUPPORTED_SEARCH_PROFILES = SEARCH_PROFILES
RULE_BASED_SEARCH_PROFILES = frozenset({"javbus", "javdb"})
MISSAV_SITE_ID = "missav"
JABLE_SITE_ID = "jable"
SUPJAV_SITE_ID = "supjav"
FC2_SITE_ID = "fc2"
BUILTIN_WEB_DOWNLOAD_SITE_IDS = (JABLE_SITE_ID, SUPJAV_SITE_ID, MISSAV_SITE_ID)
BUILTIN_WEB_RESOURCE_SITE_IDS = (*BUILTIN_WEB_DOWNLOAD_SITE_IDS, *WEB_CATALOG)
DEFAULT_METADATA_SEARCH_SITE_IDS = ("javdb", "javbus", FC2_SITE_ID)
DEFAULT_METADATA_SCRAPER_SITE_IDS = ("javbus", "javdb", FC2_SITE_ID)
SITE_DIAGNOSTIC_SITE_IDS = (
    "javbus",
    "javdb",
    "fc2",
    *METADATA_CATALOG,
    *BUILTIN_WEB_RESOURCE_SITE_IDS,
)
METADATA_SEARCH_CAPABILITY = "metadata_search"
METADATA_DETAIL_CAPABILITY = "metadata_detail"
TORRENT_SEARCH_CAPABILITY = "torrent_search"
RESOURCE_SEARCH_CAPABILITY = "resource_search"
WEB_DOWNLOAD_CAPABILITY = "web_download"
DESCRIPTION_CAPABILITY = "description"
SITE_CAPABILITY_ORDER = (
    METADATA_SEARCH_CAPABILITY,
    METADATA_DETAIL_CAPABILITY,
    TORRENT_SEARCH_CAPABILITY,
    RESOURCE_SEARCH_CAPABILITY,
    WEB_DOWNLOAD_CAPABILITY,
    DESCRIPTION_CAPABILITY,
)
SITE_CAPABILITIES = frozenset(SITE_CAPABILITY_ORDER)
MISSAV_CAPABILITIES = (
    RESOURCE_SEARCH_CAPABILITY,
    WEB_DOWNLOAD_CAPABILITY,
    DESCRIPTION_CAPABILITY,
)
WEB_RESOURCE_CAPABILITIES = (
    RESOURCE_SEARCH_CAPABILITY,
    WEB_DOWNLOAD_CAPABILITY,
)
_BASE_URL_TEMPLATE_FIELD_RE = re.compile(r"(?<!\{)\{base_url\}(?!\})")
_LEGACY_SEARCH_TEMPLATE_MAX_LENGTH = 500
_SEARCH_TEMPLATE_MAX_LENGTH = 1024
_DISABLED_LEGACY_SITE_ORIGIN = "https://invalid.invalid"


@dataclass(frozen=True, slots=True)
class _LegacySiteLocation:
    origin: str
    suffix: str
    safe_to_enable: bool


DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": CURRENT_SETTINGS_SCHEMA_VERSION,
    "detail_default_site_id": "javdb",
    "metadata_search_site_priority": list(DEFAULT_METADATA_SEARCH_SITE_IDS),
    "web_resource_search_provider_priority": list(BUILTIN_WEB_RESOURCE_SITE_IDS),
    "web_download_provider_priority": list(BUILTIN_WEB_DOWNLOAD_SITE_IDS),
    "metadata_scraper_site_priority": list(DEFAULT_METADATA_SCRAPER_SITE_IDS),
    "sites": [
        {
            "id": "javbus",
            "name": "JavBus",
            "capabilities": [METADATA_SEARCH_CAPABILITY],
            "enabled": True,
            "base_url": "https://www.javbus.com",
            "parser_profile": "javbus",
            "parser_rules_mode": "inherit",
            "parser_rules": default_parser_rules("javbus"),
            "search": {
                "url_template": "{base_url}/search/{query}{page_path}?type=&parent={parent}",
            },
            "filters": [
                {
                    "id": "parent",
                    "label": "搜索范围",
                    "type": "select",
                    "default": "ce",
                    "options": [
                        {"label": "有码", "value": "ce"},
                        {"label": "无码", "value": "uc"},
                    ],
                },
            ],
        },
        {
            "id": "javdb",
            "name": "JavDB",
            "capabilities": [METADATA_SEARCH_CAPABILITY],
            "enabled": True,
            "base_url": "https://javdb.com",
            "parser_profile": "javdb",
            "parser_rules_mode": "inherit",
            "parser_rules": default_parser_rules("javdb"),
            "search": {
                "url_template": "{base_url}/search?q={query}&f={f}&page={page}{sb_part}",
            },
            "filters": [
                {
                    "id": "f",
                    "label": "搜索范围",
                    "type": "select",
                    "default": "all",
                    "options": [
                        {"label": "影片", "value": "all"},
                        {"label": "可播放", "value": "playable"},
                        {"label": "单体作品", "value": "single"},
                        {"label": "番号", "value": "code"},
                        {"label": "演员", "value": "actor"},
                        {"label": "片商", "value": "maker"},
                        {"label": "导演", "value": "director"},
                        {"label": "系列", "value": "series"},
                        {"label": "清单", "value": "list"},
                        {"label": "含磁链", "value": "download"},
                        {"label": "字幕", "value": "cnsub"},
                        {"label": "预览图", "value": "preview"},
                    ],
                },
                {
                    "id": "sb",
                    "label": "排序",
                    "type": "select",
                    "default": "",
                    "options": [
                        {"label": "相关度", "value": ""},
                        {"label": "发布日期", "value": "1"},
                    ],
                },
            ],
        },
        {
            "id": FC2_SITE_ID,
            "name": "FC2 (AVSOX + Official + PPV DataBank)",
            "capabilities": [METADATA_SEARCH_CAPABILITY],
            "enabled": True,
            "base_url": "https://avsox.click",
            "parser_profile": "fc2",
            "search": {
                "url_template": "{base_url}/javu/data/api/search",
            },
            "filters": [],
        },
        {
            "id": JABLE_SITE_ID,
            "name": "JableTV",
            "capabilities": list(WEB_RESOURCE_CAPABILITIES),
            "enabled": True,
            "base_url": "https://fs1.app",
            "parser_profile": "jable",
        },
        {
            "id": SUPJAV_SITE_ID,
            "name": "SupJav",
            "capabilities": list(WEB_RESOURCE_CAPABILITIES),
            "enabled": True,
            "base_url": "https://supjav.com",
            "parser_profile": "supjav",
        },
        {
            "id": MISSAV_SITE_ID,
            "name": "MissAV",
            "capabilities": list(MISSAV_CAPABILITIES),
            "enabled": True,
            "base_url": "https://missav.ai",
            "parser_profile": "missav",
        },
    ],
    "organizer": {
        "enabled": True,
        "mode": "qbittorrent",
        "rules": [
            {
                "id": "default-movie",
                "name": "默认影片",
                "enabled": True,
                "priority": 1000,
                "match": {
                    "sources": [],
                    "title_contains": [],
                    "magnet_name_contains": [],
                    "code_regex": "",
                },
                "actions": {
                    "media_type": "movie",
                    "category": "",
                    "save_path": "",
                    "tags": "jav-pilot",
                },
            }
        ],
    },
}

DEFAULT_SETTINGS["sites"].extend(additional_sites())
DEFAULT_SETTINGS["workflow_defaults"] = copy.deepcopy(DEFAULT_WORKFLOW_DEFAULTS)

_LEGACY_BUILTIN_SEARCH_TEMPLATES: dict[str, set[str]] = {
    "javbus": {
        "{base_url}/search/{query}{page_path}&type={type}&parent={parent}",
        "{base_url}/search/{query}{page_path}&type=&parent={parent}",
        "{base_url}/search/{query}{page_path}?type={type}&parent={parent}",
        "{base_url}/search/{query}{page_path}?type=&parent={parent}",
    },
    "javdb": {
        "{base_url}/search?q={query}&f={f}&page={page}",
        "{base_url}/search?q={query}&f={f}&page={page}{sb_part}",
    },
}

_REMOVED_BUILTIN_FILTER_IDS: dict[str, set[str]] = {
    "javbus": {"type"},
    "javdb": set(),
}


class SettingsError(RuntimeError):
    pass


class SettingsSchemaError(SettingsError):
    pass


class SettingsConflictError(SettingsError):
    pass


def settings_path() -> Path:
    configured = os.environ.get("JAV_PILOT_SETTINGS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return runtime_data_dir() / "settings.json"


def load_settings(
    path: Path | None = None,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        return _load_settings(path, fault_injector=fault_injector)


def _load_settings(
    path: Path | None = None,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    path = path or settings_path()
    if not path.exists():
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["_config_error"] = str(exc)
        return settings
    try:
        settings, migrated = _normalize_settings(
            payload,
            fault_injector=fault_injector,
        )
    except SettingsSchemaError:
        raise
    except SettingsError as exc:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["_config_error"] = str(exc)
        return settings
    if not migrated:
        return settings
    try:
        return save_settings(settings, path)
    except OSError as exc:
        settings["_config_error"] = f"cannot migrate settings: {exc}"
        return settings


def save_settings(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        return _save_settings(payload, path)


def _settings_revision(settings: dict[str, Any]) -> str:
    encoded = json.dumps(
        settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_settings_snapshot(path: Path | None = None) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        settings = _load_settings(path)
        return {"settings": public_settings(settings), "revision": _settings_revision(settings)}


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    public = copy.deepcopy(settings)
    for site in public.get("sites", []):
        config = site.get("torznab")
        if isinstance(config, dict):
            config["api_key_configured"] = bool(config.pop("api_key", ""))
    return public


def prepare_settings_update(payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SettingsError("settings must be an object")
    updated = copy.deepcopy(payload)
    current = load_settings() if current is None else current
    previous = {site["id"]: site for site in current.get("sites", [])}
    for site in updated.get("sites", []):
        if not isinstance(site, dict):
            raise SettingsError("site must be an object")
        config = site.get("torznab")
        if site.get("parser_profile") == "torznab" and isinstance(config, dict):
            old = previous.get(site.get("id"), {}).get("torznab", {})
            if "api_key" not in config:
                config["api_key"] = old.get("api_key", "")
            config.pop("api_key_configured", None)
    return updated


def save_settings_if_revision(
    payload: dict[str, Any],
    expected_revision: object,
    path: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(expected_revision, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_revision
    ):
        raise SettingsError("保存设置需要有效的配置修订号，请重新加载后再试")
    with _SETTINGS_LOCK:
        current = _load_settings(path)
        if _settings_revision(current) != expected_revision:
            raise SettingsConflictError("设置已在其他页面修改，请重新加载后再保存")
        settings = _save_settings(prepare_settings_update(payload, current), path)
        return {"settings": public_settings(settings), "revision": _settings_revision(settings)}


def _save_settings(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    settings = normalize_settings(payload)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"cannot read existing settings: {exc}") from exc
        if not isinstance(existing, dict):
            raise SettingsError("existing settings must be an object")
        if _schema_version(existing) > CURRENT_SETTINGS_SCHEMA_VERSION:
            raise SettingsError(
                "settings schema is newer than this application supports"
            )
        _check_settings_fields(existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    raw = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, raw)
    return settings


def normalize_settings(
    payload: dict[str, Any],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    return _normalize_settings(payload, fault_injector=fault_injector)[0]


def _normalize_settings(
    payload: dict[str, Any],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise SettingsError("settings must be an object")
    _check_settings_fields(payload)
    try:
        migrated_payload, version_migrated = migrate_json(
            payload,
            component="settings",
            current_version=CURRENT_SETTINGS_SCHEMA_VERSION,
            migrations=_settings_migrations(),
            fault_injector=fault_injector,
        )
    except MigrationError as exc:
        raise SettingsSchemaError(str(exc)) from exc

    normalized = _normalize_current_settings(migrated_payload)
    return normalized, version_migrated or normalized != migrated_payload


def _check_settings_fields(payload: dict[str, Any]) -> None:
    if set(payload) - set(DEFAULT_SETTINGS) - {"_config_error"}:
        raise SettingsError("设置包含未识别字段；原文件已保留，请先检查配置")
    allowed = {"id", "name", "capabilities", "enabled", "base_url", "parser_profile",
               "search", "filters", "parser_rules_mode", "parser_rules", "torznab", "kind"}
    sites = payload.get("sites", [])
    if isinstance(sites, list):
        for site in sites:
            if isinstance(site, dict) and set(site) - allowed:
                raise SettingsError("站点设置包含未识别字段；原文件已保留，请先检查配置")
            if isinstance(site, dict) and isinstance(site.get("torznab"), dict):
                if set(site["torznab"]) - {"endpoint", "api_key", "pinned_addresses", "categories"}:
                    raise SettingsError("Torznab 设置包含未识别字段；原文件已保留")


def _normalize_current_settings(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(DEFAULT_SETTINGS)
    normalized["schema_version"] = CURRENT_SETTINGS_SCHEMA_VERSION

    if "sites" in payload:
        normalized["sites"] = _normalize_sites(
            payload.get("sites"),
            source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
        )
    if "organizer" in payload:
        normalized["organizer"] = _normalize_organizer(
            payload.get("organizer"),
            migrate_builtin_destination=False,
        )
    normalized["web_download_provider_priority"] = (
        _normalize_web_download_provider_priority(
            payload.get("web_download_provider_priority"), normalized["sites"]
        )
    )
    normalized["web_resource_search_provider_priority"] = (
        _normalize_web_resource_search_provider_priority(
            payload.get("web_resource_search_provider_priority"), normalized["sites"]
        )
    )
    normalized["metadata_search_site_priority"] = _normalize_metadata_site_priority(
        payload.get("metadata_search_site_priority"),
        normalized["sites"],
        default_ids=DEFAULT_METADATA_SEARCH_SITE_IDS,
        field_name="metadata search site priority",
    )
    normalized["metadata_scraper_site_priority"] = _normalize_metadata_site_priority(
        payload.get("metadata_scraper_site_priority"),
        normalized["sites"],
        default_ids=DEFAULT_METADATA_SCRAPER_SITE_IDS,
        field_name="metadata scraper site priority",
    )
    normalized["detail_default_site_id"] = _normalize_detail_default_site_id(
        payload.get("detail_default_site_id"),
        normalized["sites"],
    )
    try:
        normalized["workflow_defaults"] = normalize_workflow_defaults(
            payload.get("workflow_defaults"),
            {str(site.get("id")) for site in normalized["sites"]},
        )
    except WorkflowDefaultsError as exc:
        raise SettingsError(str(exc)) from exc
    return normalized


def _settings_migrations() -> tuple[JSONMigration, ...]:
    return (
        JSONMigration(1, _migrate_settings_v1),
        JSONMigration(2, _migrate_settings_v2, _verify_settings_v2),
        JSONMigration(3, _migrate_settings_v3, _verify_settings_v3),
        JSONMigration(4, _migrate_settings_v4, _verify_settings_v4),
        JSONMigration(5, _migrate_settings_v5, _verify_settings_v5),
        JSONMigration(6, _migrate_settings_v6, _verify_settings_v6),
        JSONMigration(7, _migrate_settings_v7, _verify_settings_v7),
        JSONMigration(8, _migrate_settings_v8, _verify_settings_v8),
        JSONMigration(9, _migrate_settings_v9, _verify_settings_v9),
        JSONMigration(10, _migrate_settings_v10, _verify_settings_v10),
        JSONMigration(11, _add_catalog_sources),
        JSONMigration(12, _add_workflow_defaults),
    )


def _add_workflow_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("workflow_defaults", copy.deepcopy(DEFAULT_WORKFLOW_DEFAULTS))
    return payload


def _add_catalog_sources(payload: dict[str, Any]) -> dict[str, Any]:
    sites = payload.setdefault("sites", copy.deepcopy(DEFAULT_SETTINGS["sites"]))
    if not isinstance(sites, list):
        raise SettingsError("sites must be a list")
    for site in sites:
        if (isinstance(site, dict) and site.get("id") in WEB_CATALOG
                and site.get("parser_profile") != site.get("id")):
            raise SettingsError("新增内置来源 ID 与已有自定义站点冲突；原配置已保留，请先调整自定义站点 ID")
    existing = {site.get("id") for site in sites if isinstance(site, dict)}
    sites.extend(site for site in additional_sites() if site["id"] not in existing)
    return payload


def _migrate_settings_v1(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _migrate_settings_v2(payload: dict[str, Any]) -> dict[str, Any]:
    if "organizer" in payload:
        payload["organizer"] = _normalize_organizer(
            payload.get("organizer"),
            migrate_builtin_destination=True,
        )
    return payload


def _migrate_settings_v3(payload: dict[str, Any]) -> dict[str, Any]:
    if "sites" not in payload:
        return payload
    sites = _normalize_sites(
        _migrate_legacy_site_capabilities(payload.get("sites")),
        source_schema_version=2,
    )
    for site in sites:
        if site_has_capability(site, METADATA_SEARCH_CAPABILITY):
            site.pop("parser_rules_mode", None)
            site.pop("parser_rules", None)
    payload["sites"] = sites
    return payload


def _migrate_settings_v4(payload: dict[str, Any]) -> dict[str, Any]:
    if "sites" in payload:
        payload["sites"] = _normalize_sites(
            _migrate_legacy_site_capabilities(payload.get("sites")),
            source_schema_version=3,
        )
    return payload


def _migrate_settings_v5(payload: dict[str, Any]) -> dict[str, Any]:
    if "sites" in payload:
        payload["sites"] = _normalize_sites(
            _migrate_legacy_site_capabilities(payload.get("sites")),
            source_schema_version=4,
        )
    return payload


def _migrate_settings_v6(payload: dict[str, Any]) -> dict[str, Any]:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=5,
    )
    if "sites" in payload:
        payload["sites"] = sites
    payload["detail_default_site_id"] = _normalize_detail_default_site_id(
        payload.get("detail_default_site_id"),
        sites,
    )
    return payload


def _migrate_settings_v7(payload: dict[str, Any]) -> dict[str, Any]:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=6,
    )
    payload["sites"] = sites
    payload["web_download_provider_priority"] = (
        _normalize_web_download_provider_priority(
            payload.get("web_download_provider_priority"), sites
        )
    )
    return payload


def _migrate_settings_v8(payload: dict[str, Any]) -> dict[str, Any]:
    payload["sites"] = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=7,
    )
    return payload


def _migrate_settings_v9(payload: dict[str, Any]) -> dict[str, Any]:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=8,
    )
    payload["sites"] = sites
    payload["metadata_search_site_priority"] = _normalize_metadata_site_priority(
        payload.get("metadata_search_site_priority"),
        sites,
        default_ids=DEFAULT_METADATA_SEARCH_SITE_IDS,
        field_name="metadata search site priority",
    )
    payload["web_resource_search_provider_priority"] = (
        _normalize_web_resource_search_provider_priority(
            payload.get("web_resource_search_provider_priority"), sites
        )
    )
    payload["web_download_provider_priority"] = (
        _normalize_web_download_provider_priority(
            payload.get("web_download_provider_priority"), sites
        )
    )
    payload["metadata_scraper_site_priority"] = _normalize_metadata_site_priority(
        payload.get("metadata_scraper_site_priority"),
        sites,
        default_ids=DEFAULT_METADATA_SCRAPER_SITE_IDS,
        field_name="metadata scraper site priority",
    )
    return payload


def _migrate_settings_v10(payload: dict[str, Any]) -> dict[str, Any]:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=9,
    )
    existing_fc2 = next(
        (site for site in sites if site.get("id") == FC2_SITE_ID),
        None,
    )
    if existing_fc2 is None:
        default = _default_site(FC2_SITE_ID)
        if default is None:  # pragma: no cover - guarded by the module default.
            raise SettingsError("FC2 site default is unavailable")
        insert_at = next(
            (
                index
                for index, site in enumerate(sites)
                if site.get("id") in BUILTIN_WEB_DOWNLOAD_SITE_IDS
            ),
            len(sites),
        )
        sites.insert(insert_at, default)
    payload["sites"] = _normalize_sites(
        sites,
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    payload["metadata_search_site_priority"] = _normalize_metadata_site_priority(
        payload.get("metadata_search_site_priority"),
        payload["sites"],
        default_ids=DEFAULT_METADATA_SEARCH_SITE_IDS,
        field_name="metadata search site priority",
    )
    payload["metadata_scraper_site_priority"] = _normalize_metadata_site_priority(
        payload.get("metadata_scraper_site_priority"),
        payload["sites"],
        default_ids=DEFAULT_METADATA_SCRAPER_SITE_IDS,
        field_name="metadata scraper site priority",
    )
    return payload


def _verify_settings_v2(payload: dict[str, Any]) -> None:
    if "organizer" in payload:
        _normalize_organizer(
            payload.get("organizer"),
            migrate_builtin_destination=False,
        )


def _verify_settings_v3(payload: dict[str, Any]) -> None:
    sites = payload.get("sites")
    if sites is None:
        return
    if not isinstance(sites, list) or not any(
        isinstance(site, dict) and site.get("id") == MISSAV_SITE_ID for site in sites
    ):
        raise SettingsError("settings v3 special site migration is incomplete")
    for site in sites:
        if not isinstance(site, dict):
            raise SettingsError("settings v3 site migration is incomplete")
        _safe_origin(site.get("base_url"))


def _verify_settings_v4(payload: dict[str, Any]) -> None:
    sites = payload.get("sites")
    if sites is None:
        return
    normalized = _normalize_sites(
        sites,
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    if normalized != sites:
        raise SettingsError("settings v4 parser-rule migration is incomplete")


def _verify_settings_v5(payload: dict[str, Any]) -> None:
    sites = payload.get("sites")
    if sites is None:
        return
    normalized = _normalize_sites(
        sites,
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    if normalized != sites or any("kind" in site for site in normalized):
        raise SettingsError("settings v5 site capability migration is incomplete")


def _verify_settings_v6(payload: dict[str, Any]) -> None:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    if payload.get("detail_default_site_id") != _normalize_detail_default_site_id(
        payload.get("detail_default_site_id"),
        sites,
    ):
        raise SettingsError("settings v6 detail default site migration is incomplete")


def _verify_settings_v7(payload: dict[str, Any]) -> None:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    priority = _normalize_web_download_provider_priority(
        payload.get("web_download_provider_priority"), sites
    )
    if (
        payload.get("sites") != sites
        or payload.get("web_download_provider_priority") != priority
    ):
        raise SettingsError("settings v7 web download provider migration is incomplete")


def _verify_settings_v8(payload: dict[str, Any]) -> None:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    if payload.get("sites") != sites:
        raise SettingsError("settings v8 Web resource search migration is incomplete")
    for site_id in (JABLE_SITE_ID, SUPJAV_SITE_ID):
        site = next((item for item in sites if item.get("id") == site_id), None)
        if site is None or not site_has_capability(site, RESOURCE_SEARCH_CAPABILITY):
            raise SettingsError(
                "settings v8 Web resource search migration is incomplete"
            )


def _verify_settings_v9(payload: dict[str, Any]) -> None:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    if payload.get("sites") != sites:
        raise SettingsError("settings v9 site migration is incomplete")
    expected = {
        "metadata_search_site_priority": _normalize_metadata_site_priority(
            payload.get("metadata_search_site_priority"),
            sites,
            default_ids=DEFAULT_METADATA_SEARCH_SITE_IDS,
            field_name="metadata search site priority",
        ),
        "web_resource_search_provider_priority": (
            _normalize_web_resource_search_provider_priority(
                payload.get("web_resource_search_provider_priority"), sites
            )
        ),
        "web_download_provider_priority": _normalize_web_download_provider_priority(
            payload.get("web_download_provider_priority"), sites
        ),
        "metadata_scraper_site_priority": _normalize_metadata_site_priority(
            payload.get("metadata_scraper_site_priority"),
            sites,
            default_ids=DEFAULT_METADATA_SCRAPER_SITE_IDS,
            field_name="metadata scraper site priority",
        ),
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise SettingsError("settings v9 site priority migration is incomplete")


def _verify_settings_v10(payload: dict[str, Any]) -> None:
    sites = _normalize_sites(
        payload.get("sites", DEFAULT_SETTINGS["sites"]),
        source_schema_version=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    if payload.get("sites") != sites or not any(
        site.get("id") == FC2_SITE_ID for site in sites
    ):
        raise SettingsError("settings v10 FC2 migration is incomplete")
    expected = {
        "metadata_search_site_priority": _normalize_metadata_site_priority(
            payload.get("metadata_search_site_priority"),
            sites,
            default_ids=DEFAULT_METADATA_SEARCH_SITE_IDS,
            field_name="metadata search site priority",
        ),
        "metadata_scraper_site_priority": _normalize_metadata_site_priority(
            payload.get("metadata_scraper_site_priority"),
            sites,
            default_ids=DEFAULT_METADATA_SCRAPER_SITE_IDS,
            field_name="metadata scraper site priority",
        ),
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise SettingsError("settings v10 FC2 priority migration is incomplete")


def _schema_version(payload: dict[str, Any]) -> int:
    raw = payload.get("schema_version")
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise SettingsSchemaError("settings schema version is invalid")
    return raw


def normalize_site_capabilities(
    value: Any,
    *,
    path: str = "site.capabilities",
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SettingsError(f"{path} must be a non-empty list")
    capabilities: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise SettingsError(f"{path} entries must be strings")
        capability = item.strip().lower()
        if capability not in SITE_CAPABILITIES:
            raise SettingsError(f"{path} contains an unsupported capability")
        capabilities.add(capability)
    return [item for item in SITE_CAPABILITY_ORDER if item in capabilities]


def site_has_capability(site: dict[str, Any], capability: str) -> bool:
    if not isinstance(site, dict):
        raise SettingsError("site must be an object")
    clean_capability = _validated_capability(capability)
    return clean_capability in normalize_site_capabilities(
        site.get("capabilities"),
        path="site.capabilities",
    )


def sites_with_capability(
    capability: str,
    settings: dict[str, Any] | None = None,
    *,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    clean_capability = _validated_capability(capability)
    settings = load_settings() if settings is None else settings
    sites = settings.get("sites", [])
    if not isinstance(sites, list):
        raise SettingsError("sites must be a list")
    matched = []
    for site in sites:
        if not isinstance(site, dict):
            raise SettingsError("site must be an object")
        if enabled_only and not site.get("enabled"):
            continue
        if site_has_capability(site, clean_capability):
            matched.append(site)
    return matched


def search_sites(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = load_settings() if settings is None else settings
    sites = [
        site
        for site in settings.get("sites", [])
        if site.get("enabled")
        and site.get("parser_profile") in SUPPORTED_SEARCH_PROFILES
        and any(site_has_capability(site, capability) for capability in (
            METADATA_SEARCH_CAPABILITY, METADATA_DETAIL_CAPABILITY, TORRENT_SEARCH_CAPABILITY
        ))
    ]
    return _sort_sites_by_priority(sites, settings.get("metadata_search_site_priority"))


def resource_search_sites(
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    settings = load_settings() if settings is None else settings
    sites = sites_with_capability(RESOURCE_SEARCH_CAPABILITY, settings)
    return _sort_sites_by_priority(
        sites, settings.get("web_resource_search_provider_priority")
    )


def _sort_sites_by_priority(
    sites: list[dict[str, Any]], priority: object
) -> list[dict[str, Any]]:
    raw = priority if isinstance(priority, list) else []
    rank = {
        str(site_id): index
        for index, site_id in enumerate(raw)
        if isinstance(site_id, str)
    }
    fallback = len(rank)
    return [
        site
        for _index, site in sorted(
            enumerate(sites),
            key=lambda item: (
                rank.get(str(item[1].get("id") or ""), fallback + item[0]),
                item[0],
            ),
        )
    ]


def _normalize_detail_default_site_id(
    value: Any,
    sites: list[dict[str, Any]],
) -> str:
    candidates = [
        site
        for site in sites
        if site.get("parser_profile") in SUPPORTED_SEARCH_PROFILES
        and any(site_has_capability(site, capability) for capability in (
            METADATA_SEARCH_CAPABILITY, METADATA_DETAIL_CAPABILITY
        ))
    ]
    requested = str(value or "").strip().lower()
    if any(site.get("id") == requested for site in candidates):
        return requested
    for site in candidates:
        if site.get("id") == "javdb":
            return "javdb"
    for site in candidates:
        if site.get("parser_profile") == "javdb":
            return str(site["id"])
    if candidates:
        return str(candidates[0]["id"])
    return "javdb"


def site_by_id(
    site_id: str, settings: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    settings = load_settings() if settings is None else settings
    for site in settings.get("sites", []):
        if site.get("id") == site_id:
            return site
    return None


def filter_defaults(site: dict[str, Any]) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for item in site.get("filters", []):
        key = str(item.get("id", "")).strip()
        value = str(item.get("default", "")).strip()
        if key and value:
            defaults[key] = value
    return defaults


def source_list(raw: str, settings: dict[str, Any] | None = None) -> tuple[str, ...]:
    settings = load_settings() if settings is None else settings
    enabled = [str(site.get("id")) for site in search_sites(settings)]
    if not raw or raw == "all":
        return tuple(enabled)
    selected = []
    for value in raw.split(","):
        source = value.strip()
        if source == "all":
            return tuple(enabled)
        if source and source in enabled and source not in selected:
            selected.append(source)
    return tuple(selected or enabled)


def validate_organizer_destinations(
    settings: dict[str, Any],
    *,
    category: str,
    save_path: str,
) -> None:
    try:
        resolve_qb_download_destination(
            configured_category=category,
            configured_save_path=save_path,
        )
    except QbPathError as exc:
        raise SettingsError(str(exc)) from exc

    rules = settings.get("organizer", {}).get("rules", [])
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        actions = rule.get("actions", {})
        if not isinstance(actions, dict):
            continue
        try:
            resolve_qb_download_destination(
                configured_category=category,
                configured_save_path=save_path,
                requested_category=str(actions.get("category") or ""),
                requested_save_path=str(actions.get("save_path") or ""),
            )
        except QbPathError as exc:
            rule_id = str(rule.get("id") or "<unknown>")
            raise SettingsError(f"organizer rule {rule_id}: {exc}") from exc


def _normalize_sites(
    sites: Any,
    *,
    source_schema_version: int = CURRENT_SETTINGS_SCHEMA_VERSION,
) -> list[dict[str, Any]]:
    if not isinstance(sites, list):
        raise SettingsError("sites must be a list")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    builtin_seen: set[str] = set()
    for site in sites:
        if not isinstance(site, dict):
            raise SettingsError("site must be an object")
        site_id = _safe_id(site.get("id"))
        if site_id in seen_ids:
            raise SettingsError(f"duplicate site id: {site_id}")
        seen_ids.add(site_id)
        if site_id in BUILTIN_WEB_RESOURCE_SITE_IDS:
            normalized.append(
                _normalize_builtin_web_download_site(
                    site,
                    migrate_legacy_base_url=source_schema_version < 3,
                )
            )
            builtin_seen.add(site_id)
            continue
        parser_profile = _safe_id(site.get("parser_profile") or site_id)
        if site_id == FC2_SITE_ID and parser_profile != FC2_SITE_ID:
            raise SettingsError(
                "site id 'fc2' is reserved for the FC2 parser profile"
            )
        filters = _normalize_filters(site.get("filters", []))
        default_site = _default_site(site_id)
        base_url, search, safe_to_enable = _normalize_site_location(
            site.get("base_url"),
            site.get("search", {}),
            migrate_legacy_base_url=source_schema_version < 3,
            legacy_fallback_origin=(
                str(default_site.get("base_url"))
                if default_site is not None
                else _DISABLED_LEGACY_SITE_ORIGIN
            ),
        )
        normalized_site = {
            "id": site_id,
            "name": _safe_text(site.get("name") or site_id, 80),
            "capabilities": normalize_site_capabilities(
                site.get("capabilities"),
                path=f"sites.{site_id}.capabilities",
            ),
            "enabled": bool(site.get("enabled", True)) and safe_to_enable,
            "base_url": base_url,
            "parser_profile": parser_profile,
            "search": search,
            "filters": filters,
        }
        if parser_profile in METADATA_CATALOG:
            normalized_site["capabilities"] = (
                [METADATA_SEARCH_CAPABILITY, METADATA_DETAIL_CAPABILITY]
                if METADATA_CATALOG[parser_profile][2] else [METADATA_DETAIL_CAPABILITY]
            )
        if parser_profile == "torznab":
            normalized_site["capabilities"] = [TORRENT_SEARCH_CAPABILITY]
            normalized_site["torznab"] = _normalize_torznab(site.get("torznab", {}))
            if normalized_site["enabled"] and not all(
                normalized_site["torznab"][key] for key in ("endpoint", "api_key")
            ):
                raise SettingsError("启用种子索引前，请填写 Torznab 地址与 API 密钥")
        if parser_profile in RULE_BASED_SEARCH_PROFILES:
            parser_rules_mode = (
                str(site.get("parser_rules_mode") or "inherit").strip().lower()
            )
            if parser_rules_mode not in PARSER_RULE_MODES:
                raise SettingsError(
                    f"sites.{site_id}.parser_rules_mode must be inherit or custom"
                )
            try:
                parser_rules = normalize_parser_rules(
                    site.get("parser_rules"),
                    profile=parser_profile,
                    mode=parser_rules_mode,
                    path=f"sites.{site_id}.parser_rules",
                )
            except ParserRulesError as exc:
                raise SettingsError(str(exc)) from exc
            normalized_site.update(
                {
                    "parser_rules_mode": parser_rules_mode,
                    "parser_rules": parser_rules,
                }
            )
        normalized.append(_migrate_builtin_site(normalized_site))
    for builtin_id in BUILTIN_WEB_RESOURCE_SITE_IDS:
        if builtin_id in builtin_seen:
            continue
        default = _default_site(builtin_id)
        if default is None:  # pragma: no cover - guarded by the module default.
            raise SettingsError("built-in Web download site default is unavailable")
        normalized.append(default)
    return normalized


def _normalize_torznab(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsError("Torznab 配置必须是对象")
    endpoint = str(value.get("endpoint") or "").strip()
    key = str(value.get("api_key") or "").strip()
    if len(endpoint) > 1024 or len(key) > 512 or any(c in endpoint + key for c in "\r\n\x00"):
        raise SettingsError("Torznab 配置格式无效")
    pins = value.get("pinned_addresses", [])
    if not isinstance(pins, list) or len(pins) > 8:
        raise SettingsError("Torznab 固定 IP 格式无效")
    try:
        addresses = [ipaddress.ip_address(str(item)) for item in pins]
        if any(ip.is_multicast or ip.is_unspecified or ip.is_link_local for ip in addresses):
            raise ValueError("invalid address")
        if endpoint:
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not 1 <= (parsed.port or (443 if parsed.scheme == "https" else 80)) <= 65535):
                raise ValueError("invalid endpoint")
    except ValueError:
        raise SettingsError("Torznab 地址或固定 IP 无效；地址不得包含密钥或查询参数") from None
    categories = value.get("categories", [])
    if not isinstance(categories, list) or len(categories) > 20 or any(
        isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 999999
        for item in categories
    ):
        raise SettingsError("Torznab 分类必须为正整数列表")
    normalized = {"endpoint": endpoint, "api_key": key,
                  "pinned_addresses": list(dict.fromkeys(str(ip) for ip in addresses)),
                  "categories": list(dict.fromkeys(categories))}
    if endpoint and key:
        from ..indexers.torznab import TorznabConfig, TorznabError
        try:
            TorznabConfig(endpoint, key, pinned_addresses=tuple(normalized["pinned_addresses"]),
                          categories=tuple(normalized["categories"]))
        except TorznabError as exc:
            raise SettingsError(str(exc)) from None
    return normalized


def _normalize_builtin_web_download_site(
    site: dict[str, Any],
    *,
    migrate_legacy_base_url: bool = False,
) -> dict[str, Any]:
    site_id = _safe_id(site.get("id"))
    if site_id not in BUILTIN_WEB_RESOURCE_SITE_IDS:
        raise SettingsError("built-in Web download site is invalid")
    default = _default_site(site_id)
    if default is None:  # pragma: no cover - guarded by the module default.
        raise SettingsError("built-in Web download site default is unavailable")
    raw_base_url = site.get("base_url") or default["base_url"]
    if migrate_legacy_base_url:
        legacy_location = _legacy_site_location(
            raw_base_url,
            fallback_origin=str(default["base_url"]),
        )
        base_url = legacy_location.origin
        safe_to_enable = legacy_location.safe_to_enable
    else:
        base_url = _safe_origin(raw_base_url)
        safe_to_enable = True
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or parsed.port not in {None, 443}:
        if not migrate_legacy_base_url:
            raise SettingsError(
                f"{default['name']} base_url must be an HTTPS origin on port 443"
            )
        base_url = str(default["base_url"])
        safe_to_enable = False
    return {
        "id": site_id,
        "name": _safe_text(site.get("name") or default["name"], 80),
        "capabilities": list(default["capabilities"]),
        "enabled": bool(site.get("enabled", True)) and safe_to_enable,
        "base_url": base_url,
        "parser_profile": str(default["parser_profile"]),
    }


def _normalize_web_download_provider_priority(
    value: object, sites: list[dict[str, Any]]
) -> list[str]:
    return _normalize_site_priority(
        value,
        sites,
        capability=WEB_DOWNLOAD_CAPABILITY,
        default_ids=BUILTIN_WEB_DOWNLOAD_SITE_IDS,
        allowed_ids=frozenset(BUILTIN_WEB_DOWNLOAD_SITE_IDS),
        field_name="web download provider priority",
        required=True,
    )


def _normalize_web_resource_search_provider_priority(
    value: object, sites: list[dict[str, Any]]
) -> list[str]:
    return _normalize_site_priority(
        value,
        sites,
        capability=RESOURCE_SEARCH_CAPABILITY,
        default_ids=BUILTIN_WEB_RESOURCE_SITE_IDS,
        allowed_ids=frozenset(BUILTIN_WEB_RESOURCE_SITE_IDS),
        field_name="web resource search provider priority",
        required=True,
    )


def _normalize_metadata_site_priority(
    value: object,
    sites: list[dict[str, Any]],
    *,
    default_ids: tuple[str, ...],
    field_name: str,
) -> list[str]:
    return _normalize_site_priority(
        value,
        sites,
        capability=METADATA_SEARCH_CAPABILITY,
        default_ids=default_ids,
        allowed_profiles=SUPPORTED_SEARCH_PROFILES,
        field_name=field_name,
        required=False,
    )


def _normalize_site_priority(
    value: object,
    sites: list[dict[str, Any]],
    *,
    capability: str,
    default_ids: tuple[str, ...],
    field_name: str,
    allowed_ids: frozenset[str] | None = None,
    allowed_profiles: frozenset[str] | None = None,
    required: bool,
) -> list[str]:
    configured: list[str] = []
    for site in sites:
        site_id = str(site.get("id") or "")
        profile = str(site.get("parser_profile") or site_id)
        if allowed_ids is not None and site_id not in allowed_ids:
            continue
        if allowed_profiles is not None and profile not in allowed_profiles:
            continue
        matches = site_has_capability(site, capability)
        if capability == METADATA_SEARCH_CAPABILITY:
            matches = matches or site_has_capability(site, METADATA_DETAIL_CAPABILITY)
            if field_name == "metadata search site priority":
                matches = matches or site_has_capability(site, TORRENT_SEARCH_CAPABILITY)
        if matches:
            configured.append(site_id)
    configured_set = set(configured)
    raw = value if isinstance(value, list) else list(default_ids)
    result: list[str] = []
    for item in raw:
        site_id = str(item or "").strip().lower()
        if site_id in configured_set and site_id not in result:
            result.append(site_id)
    for site_id in (*default_ids, *configured):
        if site_id in configured_set and site_id not in result:
            result.append(site_id)
    if required and not result:
        raise SettingsError(f"{field_name} is empty")
    return result


def _migrate_legacy_site_capabilities(sites: Any) -> Any:
    if not isinstance(sites, list):
        return sites
    migrated = copy.deepcopy(sites)
    for site in migrated:
        if not isinstance(site, dict):
            continue
        try:
            site_id = _safe_id(site.get("id"))
        except SettingsError:
            continue
        if site_id == MISSAV_SITE_ID:
            site["capabilities"] = list(MISSAV_CAPABILITIES)
        elif "kind" in site or "capabilities" not in site:
            kind = str(site.get("kind") or "search").strip().lower()
            if kind != "search":
                raise SettingsError("only MissAV may use a non-search legacy kind")
            site["capabilities"] = [METADATA_SEARCH_CAPABILITY]
        site.pop("kind", None)
    return migrated


def _validated_capability(value: object) -> str:
    if not isinstance(value, str):
        raise SettingsError("site capability must be a string")
    capability = value.strip().lower()
    if capability not in SITE_CAPABILITIES:
        raise SettingsError("site capability is unsupported")
    return capability


def _normalize_site_location(
    base_url: Any,
    search: Any,
    *,
    migrate_legacy_base_url: bool,
    legacy_fallback_origin: str,
) -> tuple[str, dict[str, str], bool]:
    normalized_search = _normalize_search(
        search,
        limit=(
            _LEGACY_SEARCH_TEMPLATE_MAX_LENGTH
            if migrate_legacy_base_url
            else _SEARCH_TEMPLATE_MAX_LENGTH
        ),
    )
    if not migrate_legacy_base_url:
        origin = _safe_origin(base_url)
        normalized_search["url_template"] = _configurable_search_template(
            normalized_search["url_template"],
            origin=origin,
        )
        return origin, normalized_search, True

    location = _legacy_site_location(
        base_url,
        fallback_origin=legacy_fallback_origin,
    )
    template, template_safe = _migrate_legacy_search_template(
        normalized_search["url_template"],
        origin=location.origin,
        legacy_suffix=location.suffix,
    )
    normalized_search["url_template"] = template
    return (
        location.origin,
        normalized_search,
        (location.safe_to_enable and template_safe),
    )


def _legacy_site_location(
    value: Any,
    *,
    fallback_origin: str,
) -> _LegacySiteLocation:
    fallback = _safe_origin(fallback_origin)
    raw_text = str(value or "")
    text = _safe_text(value, 260).rstrip("/")
    if any(character in raw_text for character in "\\\r\n\t\0"):
        return _LegacySiteLocation(fallback, "", False)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except (TypeError, ValueError):
        return _LegacySiteLocation(fallback, "", False)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").rstrip(".")
    if scheme not in {"http", "https"} or not hostname:
        return _LegacySiteLocation(fallback, "", False)
    host = f"[{hostname}]" if ":" in hostname else hostname
    port_suffix = f":{port}" if port is not None else ""
    try:
        origin = _safe_origin(f"{scheme}://{host}{port_suffix}")
    except SettingsError:
        return _LegacySiteLocation(fallback, "", False)
    query_suffix = f"?{parsed.query}" if "?" in text else ""
    return _LegacySiteLocation(
        origin=origin,
        suffix=f"{parsed.path}{query_suffix}",
        safe_to_enable=(
            parsed.username is None and parsed.password is None and "#" not in text
        ),
    )


def _migrate_legacy_search_template(
    template: str,
    *,
    origin: str,
    legacy_suffix: str,
) -> tuple[str, bool]:
    base_url_field = _BASE_URL_TEMPLATE_FIELD_RE.search(template)
    if base_url_field is not None and base_url_field.start() == 0:
        escaped_suffix = legacy_suffix.replace("{", "{{").replace("}", "}}")
        candidate = _BASE_URL_TEMPLATE_FIELD_RE.sub(
            lambda _match: "{base_url}" + escaped_suffix,
            template,
        )
        if len(candidate) <= _SEARCH_TEMPLATE_MAX_LENGTH:
            return candidate, True
        return template, False

    candidate, safe_to_enable = _template_with_configurable_origin(
        template,
        origin=origin,
        allow_unsafe_legacy=True,
    )
    if len(candidate) > _SEARCH_TEMPLATE_MAX_LENGTH:
        return "{base_url}/search/{query}", False
    return candidate, safe_to_enable


def _configurable_search_template(template: str, *, origin: str) -> str:
    base_url_field = _BASE_URL_TEMPLATE_FIELD_RE.search(template)
    if base_url_field is not None and base_url_field.start() == 0:
        return template
    candidate, safe_to_enable = _template_with_configurable_origin(
        template,
        origin=origin,
        allow_unsafe_legacy=False,
    )
    if not safe_to_enable or len(candidate) > _SEARCH_TEMPLATE_MAX_LENGTH:
        raise SettingsError("search.url_template must use the configured base_url")
    return candidate


def _template_with_configurable_origin(
    template: str,
    *,
    origin: str,
    allow_unsafe_legacy: bool,
) -> tuple[str, bool]:
    if template.startswith("/"):
        return "{base_url}" + template, allow_unsafe_legacy is False
    try:
        parsed = urlsplit(template)
        port = parsed.port
    except (TypeError, ValueError):
        parsed = None
        port = None
    if parsed is not None and parsed.scheme.lower() in {"http", "https"}:
        hostname = (parsed.hostname or "").rstrip(".")
        if hostname:
            host = f"[{hostname}]" if ":" in hostname else hostname
            port_suffix = f":{port}" if port is not None else ""
            try:
                template_origin = _safe_origin(
                    f"{parsed.scheme.lower()}://{host}{port_suffix}"
                )
            except SettingsError:
                template_origin = ""
            suffix = parsed.path
            if "?" in template.split("#", 1)[0]:
                suffix += f"?{parsed.query}"
            safe_to_enable = (
                bool(template_origin)
                and template_origin == origin
                and parsed.username is None
                and parsed.password is None
                and "#" not in template
            )
            if safe_to_enable or allow_unsafe_legacy:
                return "{base_url}" + suffix, safe_to_enable
    if not allow_unsafe_legacy:
        return template, False
    clean_relative = template.lstrip("/")
    if not clean_relative:
        return "{base_url}/search/{query}", False
    return "{base_url}/" + clean_relative, False


def _migrate_builtin_site(site: dict[str, Any]) -> dict[str, Any]:
    default = _default_site(site.get("id"))
    if not default or site.get("parser_profile") != default.get("parser_profile"):
        return site

    migrated = copy.deepcopy(site)
    current_template = migrated.get("search", {}).get("url_template", "")
    legacy_templates = _LEGACY_BUILTIN_SEARCH_TEMPLATES.get(str(site.get("id")), set())
    if not current_template or current_template in legacy_templates:
        migrated["search"] = copy.deepcopy(default["search"])

    migrated["filters"] = _merge_builtin_filters(
        str(site.get("id")),
        migrated.get("filters", []),
        default.get("filters", []),
    )
    return migrated


def _default_site(site_id: object) -> dict[str, Any] | None:
    for site in DEFAULT_SETTINGS["sites"]:
        if site.get("id") == site_id:
            return copy.deepcopy(site)
    return None


def _merge_builtin_filters(
    site_id: str,
    current_filters: list[dict[str, Any]],
    default_filters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    default_ids = {str(item.get("id")) for item in default_filters}
    removed_ids = _REMOVED_BUILTIN_FILTER_IDS.get(site_id, set())
    current_by_id = {str(item.get("id")): item for item in current_filters}
    merged = []
    for default in default_filters:
        filter_id = str(default.get("id"))
        current = current_by_id.get(filter_id)
        if current is None or _is_legacy_placeholder_filter(current):
            merged.append(copy.deepcopy(default))
        else:
            merged.append(copy.deepcopy(current))
    for item in current_filters:
        filter_id = str(item.get("id"))
        if filter_id in default_ids:
            continue
        if filter_id in removed_ids and _is_legacy_placeholder_filter(item):
            continue
        merged.append(copy.deepcopy(item))
    return merged


def _is_legacy_placeholder_filter(item: dict[str, Any]) -> bool:
    if _is_question_placeholder(item.get("label")):
        return True
    return any(
        _is_question_placeholder(option.get("label"))
        for option in item.get("options", [])
        if isinstance(option, dict)
    )


def _normalize_filters(filters: Any) -> list[dict[str, Any]]:
    if not isinstance(filters, list) or len(filters) > 32:
        raise SettingsError("filters must be a list with at most 32 items")
    normalized = []
    for item in filters:
        if not isinstance(item, dict):
            raise SettingsError("filter must be an object")
        options = []
        raw_options = item.get("options", [])
        if isinstance(raw_options, list):
            for option in raw_options[:80]:
                if not isinstance(option, dict):
                    continue
                options.append(
                    {
                        "label": _safe_text(option.get("label"), 80),
                        "value": _safe_text(option.get("value"), 120),
                    }
                )
        normalized.append(
            {
                "id": _safe_id(item.get("id")),
                "label": _safe_text(item.get("label"), 80),
                "type": _safe_choice(item.get("type"), {"select", "text"}, "select"),
                "default": _safe_text(item.get("default"), 120),
                "options": options,
            }
        )
    return normalized


def _normalize_search(
    search: Any,
    *,
    limit: int = _SEARCH_TEMPLATE_MAX_LENGTH,
) -> dict[str, str]:
    if not isinstance(search, dict):
        raise SettingsError("search must be an object")
    template = _safe_text(search.get("url_template"), limit)
    if not template:
        template = "{base_url}/search/{query}"
    return {"url_template": template}


def _normalize_organizer(
    organizer: Any,
    *,
    migrate_builtin_destination: bool = False,
) -> dict[str, Any]:
    if not isinstance(organizer, dict):
        raise SettingsError("organizer must be an object")
    rules = organizer.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 80:
        raise SettingsError("organizer.rules must be a list with at most 80 items")
    normalized_rules = [_normalize_organizer_rule(rule) for rule in rules]
    return {
        "enabled": bool(organizer.get("enabled", True)),
        "mode": _safe_choice(organizer.get("mode"), {"qbittorrent"}, "qbittorrent"),
        "rules": _migrate_builtin_organizer_rules(
            normalized_rules,
            migrate_builtin_destination=migrate_builtin_destination,
        ),
    }


def _normalize_organizer_rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise SettingsError("organizer rule must be an object")
    match = rule.get("match", {})
    actions = rule.get("actions", {})
    if not isinstance(match, dict) or not isinstance(actions, dict):
        raise SettingsError("organizer rule match/actions must be objects")
    code_regex = _safe_text(match.get("code_regex"), 180)
    if code_regex:
        try:
            re.compile(code_regex)
        except re.error as exc:
            raise SettingsError(f"invalid code_regex: {exc}") from exc
    return {
        "id": _safe_id(rule.get("id")),
        "name": _safe_text(rule.get("name"), 100),
        "enabled": bool(rule.get("enabled", True)),
        "priority": _safe_int(rule.get("priority"), 1000, 0, 99999),
        "match": {
            "sources": _safe_text_list(match.get("sources"), 20, 64),
            "title_contains": _safe_text_list(match.get("title_contains"), 20, 80),
            "magnet_name_contains": _safe_text_list(
                match.get("magnet_name_contains"), 20, 80
            ),
            "code_regex": code_regex,
        },
        "actions": {
            "media_type": _safe_choice(
                actions.get("media_type"), {"movie", "series", "other"}, "movie"
            ),
            "category": _safe_text(actions.get("category"), 120),
            "save_path": _safe_text(actions.get("save_path"), 260),
            "tags": _safe_text(actions.get("tags"), 180),
        },
    }


def _migrate_builtin_organizer_rules(
    rules: list[dict[str, Any]],
    *,
    migrate_builtin_destination: bool = False,
) -> list[dict[str, Any]]:
    default_rules = {
        str(rule.get("id")): rule
        for rule in DEFAULT_SETTINGS.get("organizer", {}).get("rules", [])
    }
    migrated = []
    for rule in rules:
        clean_rule = copy.deepcopy(rule)
        default = default_rules.get(str(clean_rule.get("id")))
        if default and _is_question_placeholder(clean_rule.get("name")):
            clean_rule["name"] = str(default.get("name") or clean_rule.get("id") or "")
        if (
            migrate_builtin_destination
            and clean_rule.get("id") == "default-movie"
            and clean_rule.get("actions", {}).get("category") == "jav"
        ):
            clean_rule["actions"]["category"] = ""
        migrated.append(clean_rule)
    return migrated


def _is_question_placeholder(value: object) -> bool:
    return bool(re.fullmatch(r"\?+", str(value or "").strip()))


def _safe_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", text):
        raise SettingsError(f"invalid id: {text or '<empty>'}")
    return text


def _safe_origin(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 260:
        raise SettingsError("base_url must be at most 260 characters")
    if any(character in text for character in "\\\r\n\t\0"):
        raise SettingsError("base_url must be an HTTP(S) origin")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SettingsError("base_url must be a valid HTTP(S) origin") from exc
    scheme = parsed.scheme.lower()
    hostname = _safe_hostname((parsed.hostname or "").rstrip(".").lower())
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or "?" in text
        or "#" in text
        or any(character.isspace() for character in hostname)
    ):
        raise SettingsError(
            "base_url must be an HTTP(S) origin without credentials, path, query, or fragment"
        )
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{port_suffix}"


def _safe_hostname(value: str) -> str:
    if not value:
        return ""
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass
    try:
        hostname = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SettingsError("base_url contains an invalid hostname") from exc
    labels = hostname.split(".")
    if len(hostname) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise SettingsError("base_url contains an invalid hostname")
    return hostname


def _safe_text(value: Any, limit: int = 200) -> str:
    return str(value or "").replace("\r", "").strip()[:limit]


def _safe_text_list(value: Any, max_items: int, item_limit: int) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise SettingsError("expected a list")
    output = []
    for item in value[:max_items]:
        text = _safe_text(item, item_limit)
        if text:
            output.append(text)
    return output


def _safe_choice(value: Any, choices: set[str], default: str) -> str:
    text = str(value or default).strip().lower()
    return text if text in choices else default


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
