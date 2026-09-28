from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from ..core.models import MagnetInfo


MAGNET_RE = re.compile(r"magnet:\?[^\s\"'<>]+", re.IGNORECASE)
HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
BASE32_RE = re.compile(r"^[A-Z2-7a-z]{32}$")


class MagnetError(ValueError):
    pass


def find_magnet_uris(text: str, *, max_items: int = 200) -> list[str]:
    if not text:
        return []

    safe_limit = max(1, min(int(max_items), 500))
    seen: set[str] = set()
    uris: list[str] = []
    for match in MAGNET_RE.finditer(text):
        uri = match.group(0).rstrip(".,);]")
        if uri not in seen:
            seen.add(uri)
            uris.append(uri)
            if len(uris) >= safe_limit:
                break
    return uris


def parse_magnet_text(text: str, *, max_items: int = 200) -> list[MagnetInfo]:
    parsed: list[MagnetInfo] = []
    for uri in find_magnet_uris(text, max_items=max_items):
        try:
            parsed.append(parse_magnet(uri))
        except MagnetError:
            continue
    return parsed


def parse_magnet(uri: str) -> MagnetInfo:
    if not uri or not uri.lower().startswith("magnet:?"):
        raise MagnetError("not a magnet URI")
    if len(uri) > 16 * 1024:
        raise MagnetError("magnet URI is too long")

    parsed = urlsplit(uri)
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=128)
    except ValueError as exc:
        raise MagnetError("magnet URI has too many parameters") from exc
    params: dict[str, list[str]] = {}
    for key, value in pairs:
        if _has_decoded_control(key) or _has_decoded_control(value):
            raise MagnetError("magnet fields cannot contain control characters")
        params.setdefault(key, []).append(value)

    xt_values = params.get("xt", [])
    info_hash = None
    for xt in xt_values:
        if xt.lower().startswith("urn:btih:"):
            info_hash = _normalize_btih(xt[9:])
            break
    if not info_hash:
        raise MagnetError("missing urn:btih info hash")

    exact_length = None
    xl = _first(params, "xl")
    if xl:
        try:
            exact_length = int(xl)
        except ValueError:
            exact_length = None

    return MagnetInfo(
        uri=uri,
        info_hash=info_hash,
        display_name=_first(params, "dn"),
        trackers=tuple(params.get("tr", [])[:64]),
        exact_length=exact_length,
        params={key: list(values) for key, values in params.items()},
    )


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key, [])
    if not values:
        return None
    return values[0] or None


def _normalize_btih(value: str) -> str:
    value = unquote(value).strip()
    if HEX40_RE.match(value):
        return value.lower()
    if BASE32_RE.match(value):
        try:
            raw = base64.b32decode(value.upper())
        except (binascii.Error, ValueError) as exc:
            raise MagnetError("invalid base32 btih hash") from exc
        return raw.hex()
    raise MagnetError("btih must be 40 hex chars or 32 base32 chars")


def _has_decoded_control(value: str) -> bool:
    candidate = value
    for _ in range(16):
        if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in candidate):
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    # Excessive nested encoding is not a valid user-facing magnet field and
    # must not provide a bypass around decoded control validation.
    return True
