"""Disposable browser worker; its parent owns the hard wall-clock deadline."""

from __future__ import annotations

import json
import sys

from ..core.models import SearchBounds
from .browser_fetcher import BrowserPageFetcher


def main() -> None:
    request = json.load(sys.stdin)
    try:
        with BrowserPageFetcher(
            SearchBounds(
                timeout_seconds=request["timeout"],
                max_response_bytes=request["max_bytes"],
            ),
            session_origin=request["origin"],
            allowed_origins=tuple(request["allowed_origins"]),
            headers=request["headers"],
        ) as browser:
            html = browser.fetch(request["url"])
        result = {"html": html}
    except Exception as exc:
        result = {"error": str(exc)}
    json.dump(result, sys.stdout, ensure_ascii=True)


if __name__ == "__main__":
    main()
