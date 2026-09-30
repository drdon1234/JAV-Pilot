"""Whether a subtitle name or listing title belongs to a catalog code."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from ..core.catalog_code import normalize_catalog_code

_SEPARATOR = "[-_. ]?"


@lru_cache(maxsize=256)
def code_pattern(code: str) -> re.Pattern[str]:
    normalized = normalize_catalog_code(code, max_length=40)
    if normalized is None:
        raise ValueError("catalog code is invalid")
    tokens = re.split(r"[-._]", normalized[0])
    pieces = [re.escape(token) for token in tokens]
    if tokens[-1].isdigit():
        # A leading-zero difference is the same work (FXT-01 and FXT-001),
        # but another digit after it is not (FXT-0010).
        pieces[-1] = f"0*{int(tokens[-1])}"
    if tokens[:2] == ["FC2", "PPV"] and len(tokens) > 2:
        pattern = f"FC2{_SEPARATOR}(?:PPV{_SEPARATOR})?" + _SEPARATOR.join(pieces[2:])
    else:
        pattern = _SEPARATOR.join(pieces)
    return re.compile(rf"(?<![A-Z0-9]){pattern}(?!\d)")


def code_matches(text: str, code: str) -> bool:
    haystack = unicodedata.normalize("NFKC", str(text)).upper()
    return code_pattern(code).search(haystack) is not None
