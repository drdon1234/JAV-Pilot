"""Validate explicit bulk selections before any mutation takes place."""
from __future__ import annotations

import re


def selected_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5000 or any(
        not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", item) for item in value
    ) or len(set(value)) != len(value):
        raise ValueError("Invalid selection")
    return value
