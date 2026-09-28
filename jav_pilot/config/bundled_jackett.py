"""The Jackett service bundled with the full Docker Compose deployment.

``JAV_PILOT_JACKETT_URL`` and ``JAV_PILOT_JACKETT_SERVER_CONFIG`` (Jackett's
``ServerConfig.json``, mounted read-only) turn it on. Torrent sources whose
Torznab address and API key are both left empty then use this Jackett, with
the API key read from its configuration on every use.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# JAV Pilot source ID -> Jackett indexer ID.
JACKETT_INDEXERS = {"sukebei": "sukebeinyaasi", "tokyotoshokan": "tokyotosho"}
_MAX_SERVER_CONFIG_BYTES = 1024 * 1024
_API_KEY_RE = re.compile(r"[A-Za-z0-9]{16,128}")


@dataclass(frozen=True, slots=True)
class BundledJackett:
    origin: str
    server_config: Path

    @classmethod
    def from_env(cls) -> BundledJackett | None:
        origin = os.environ.get("JAV_PILOT_JACKETT_URL", "").strip().rstrip("/")
        server_config = os.environ.get("JAV_PILOT_JACKETT_SERVER_CONFIG", "").strip()
        if not origin or not server_config:
            return None
        try:
            parsed = urlsplit(origin)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
            parsed.port  # noqa: B018 - raises ValueError for an invalid port
        except ValueError:
            valid = False
        return cls(origin, Path(server_config)) if valid else None

    def endpoint(self, site_id: str) -> str:
        indexer = JACKETT_INDEXERS[site_id]
        return f"{self.origin}/api/v2.0/indexers/{indexer}/results/torznab/api"

    def api_key(self) -> str | None:
        """Jackett's API key, or None until Jackett has written it."""

        try:
            with self.server_config.open("rb") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > _MAX_SERVER_CONFIG_BYTES
                ):
                    return None
                payload = json.loads(handle.read(_MAX_SERVER_CONFIG_BYTES + 1))
        except (OSError, ValueError):
            return None
        key = payload.get("APIKey") if isinstance(payload, dict) else None
        return key if isinstance(key, str) and _API_KEY_RE.fullmatch(key) else None


def bundled_jackett_site(site_id: object) -> bool:
    """Whether this source can fall back to the bundled Jackett here."""

    return site_id in JACKETT_INDEXERS and BundledJackett.from_env() is not None
