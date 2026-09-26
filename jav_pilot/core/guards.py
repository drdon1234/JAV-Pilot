from __future__ import annotations

import re
import unicodedata

from .catalog_code import canonical_catalog_code


class QueryError(ValueError):
    pass


MAX_SEARCH_QUERY_LENGTH = 80
MAX_SEARCH_QUERY_TERMS = 16
_MAX_SEARCH_QUERY_INPUT_LENGTH = 320
_CATALOG_QUERY_RE = re.compile(r"[A-Z0-9]+(?:[-._][A-Z0-9]+)*[-._ ]?\d{2,9}")
_SENSITIVE_TRANSPORT_TEXT_RE = re.compile(
    r"(?:https?|wss?|ftp)://|://|www\.|"
    r"\b(?:authorization|cookies?|headers?|manifest(?:_url)?|password|referer|"
    r"secret|session|token|url)\s*[:=]|"
    r"[?&](?:authorization|cookie|password|secret|session|token)\s*=",
    re.IGNORECASE,
)


def looks_like_catalog_code(query: str) -> bool:
    """Recognize a complete code query, not arbitrary canonicalizable text.

    Stored identities intentionally accept broad alphanumeric values. Search
    intent additionally requires a serial number and at most one space between
    the prefix and serial, so source prefixes and keyword phrases stay fuzzy.
    """
    clean = unicodedata.normalize("NFKC", str(query or "")).strip().upper()
    return (
        canonical_catalog_code(clean, max_length=20) is not None
        and _CATALOG_QUERY_RE.fullmatch(clean) is not None
    )


def contains_sensitive_transport_text(value: object) -> bool:
    return bool(_SENSITIVE_TRANSPORT_TEXT_RE.search(str(value or "")))


def normalize_query(raw: object) -> str:
    if raw is None:
        raise QueryError("query is required")
    if not isinstance(raw, str):
        raise QueryError("query must be text")

    value = raw
    if len(value) > _MAX_SEARCH_QUERY_INPUT_LENGTH:
        raise QueryError(
            f"query is too long; keep it under {MAX_SEARCH_QUERY_LENGTH} chars"
        )
    value = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(character).startswith("C")
        and not character.isspace()
        for character in value
    ):
        raise QueryError("query contains control characters")

    raw_terms = value.split()
    if not raw_terms:
        raise QueryError("query is empty")

    terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    if len(terms) > MAX_SEARCH_QUERY_TERMS:
        raise QueryError(
            f"query has too many terms; keep it under {MAX_SEARCH_QUERY_TERMS} terms"
        )
    query = " ".join(terms)
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise QueryError(
            f"query is too long; keep it under {MAX_SEARCH_QUERY_LENGTH} chars"
        )

    return query
