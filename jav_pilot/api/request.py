"""Request parsing helpers: query parameters, identifiers and origin checks."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..core.guards import QueryError
from ..security.posture import trusted_proxy_peer


def single_param(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key)
    if not values:
        return ""
    return values[0]


def int_param(params: dict[str, list[str]], key: str, default: int) -> int:
    value = single_param(params, key)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def strict_int_param(params: dict[str, list[str]], key: str, default: int) -> int:
    if key not in params:
        return default
    value = single_param(params, key)
    if re.fullmatch(r"-?[0-9]+", value) is None:
        raise QueryError(f"{key} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise QueryError(f"{key} must be an integer") from exc


def search_result_limit_param(params: dict[str, list[str]]) -> int | None:
    if "result_limit" not in params:
        return None
    value = single_param(params, "result_limit")
    if re.fullmatch(r"[0-9]+", value) is None:
        raise QueryError("result_limit must be an integer between 1 and 999")
    result_limit = int(value)
    if not 1 <= result_limit <= 999:
        raise QueryError("result_limit must be an integer between 1 and 999")
    return result_limit


def query_params(query_string: str) -> dict[str, list[str]]:
    try:
        return parse_qs(query_string, keep_blank_values=True, max_num_fields=64)
    except ValueError as exc:
        raise QueryError("too many query parameters") from exc


def filters_param(params: dict[str, list[str]]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for key, values in params.items():
        if not key.startswith("filter."):
            continue
        filter_id = key.removeprefix("filter.").strip()
        value = values[0].strip() if values else ""
        if filter_id:
            filters[filter_id] = value
        if len(filters) >= 24:
            break
    return filters


def semantic_refs_param(params: dict[str, list[str]]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for key, values in params.items():
        if not key.startswith("ref."):
            continue
        source_id = key.removeprefix("ref.").strip()
        value = values[0].strip()[:2048] if values else ""
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source_id) and value:
            refs[source_id] = value
        if len(refs) >= 16:
            break
    return refs


def valid_request_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{8,80}", value))


def valid_work_id(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:code:[A-Za-z0-9._-]{2,72}|record:[A-Za-z0-9._-]{4,72})",
            value,
        )
    )


def same_origin_request(headers: object, *, peer_host: object = "") -> bool:
    get = getattr(headers, "get", None)
    if not callable(get):
        return False
    fetch_site = str(get("Sec-Fetch-Site", "") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = str(get("Origin", "") or "").strip()
    if not origin:
        return True
    forwarded = (
        str(get("X-Forwarded-Host", "") or "") if trusted_proxy_peer(peer_host) else ""
    )
    host = str(forwarded or get("Host", "") or "").split(",", 1)[0].strip().lower()
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return bool(host and origin_host == host)


def request_peer_host(handler: object) -> str:
    address = getattr(handler, "client_address", ())
    if isinstance(address, tuple) and address:
        return str(address[0] or "unknown")[:128]
    return "unknown"


def request_server_host(handler: object) -> str:
    server = getattr(handler, "server", None)
    address = getattr(server, "server_address", ())
    if isinstance(address, tuple) and address:
        return str(address[0] or "127.0.0.1")[:128]
    return "127.0.0.1"


def request_server_port(handler: object) -> int | None:
    server = getattr(handler, "server", None)
    address = getattr(server, "server_address", ())
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    try:
        port = int(address[1])
    except (TypeError, ValueError, OverflowError):
        return None
    return port if 1 <= port <= 65535 else None


def request_is_secure(handler: object) -> bool:
    peer_host = request_peer_host(handler)
    if not trusted_proxy_peer(peer_host):
        return False
    headers = getattr(handler, "headers", None)
    get = getattr(headers, "get", None)
    if not callable(get):
        return False
    forwarded_proto = str(get("X-Forwarded-Proto", "") or "").split(",", 1)[0]
    return forwarded_proto.strip().lower() == "https"
