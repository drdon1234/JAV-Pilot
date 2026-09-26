"""The concrete source adapters shipped by this application."""

METADATA_CATALOG = {
    "fanza": ("FANZA", "https://www.dmm.co.jp", True),
    "mgs": ("MGS", "https://www.mgstage.com", True),
    "avbase": ("AVBase", "https://www.avbase.net", True),
    "fc2db": ("FC2DB", "https://fc2db.net", False),
    "javten": ("JAVTEN", "https://javten.com", False),
}
TORRENT_CATALOG = {
    "sukebei": ("Sukebei", "https://sukebei.nyaa.si"),
    "tokyotoshokan": ("Tokyo Toshokan", "https://www.tokyotosho.se"),
}
WEB_CATALOG = {
    "kissjav": ("KissJAV", "https://kissjav.li"),
    "javnoni": ("JAV-NONI", "https://jav-noni.live"),
}
METADATA_PROFILES = frozenset({"javbus", "javdb", "fc2", *METADATA_CATALOG})
SEARCH_PROFILES = METADATA_PROFILES | {"torznab"}
MAX_SEARCH_SOURCES = 128


def additional_sites() -> list[dict]:
    sites = []
    for source_id, (name, origin, keywords) in METADATA_CATALOG.items():
        sites.append({
            "id": source_id, "name": name, "base_url": origin,
            "parser_profile": source_id, "enabled": False,
            "capabilities": (["metadata_search", "metadata_detail"] if keywords
                             else ["metadata_detail"]),
            "search": {"url_template": "{base_url}/"}, "filters": [],
        })
    for source_id, (name, origin) in TORRENT_CATALOG.items():
        sites.append({
            "id": source_id, "name": name, "base_url": origin,
            "parser_profile": "torznab", "enabled": False,
            "capabilities": ["torrent_search"],
            "search": {"url_template": "{base_url}/"}, "filters": [],
            "torznab": {"endpoint": "", "api_key": "", "pinned_addresses": [],
                        "categories": []},
        })
    for source_id, (name, origin) in WEB_CATALOG.items():
        sites.append({
            "id": source_id, "name": name, "base_url": origin,
            "parser_profile": source_id, "enabled": False,
            "capabilities": ["resource_search"],
        })
    return sites
