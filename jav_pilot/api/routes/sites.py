"""Site diagnostic endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...config.settings import SITE_DIAGNOSTIC_SITE_IDS, load_settings
from ...core.guards import QueryError
from ...sites.diagnostic_codes import (
    load_site_diagnostic_codes,
    normalize_diagnostic_codes,
    save_site_diagnostic_codes,
)
from ...sites.diagnostic_store import SiteDiagnosticStoreError
from ...sites.diagnostics import SiteDiagnosticService
from ...sites.probe_adapters import build_site_probe_adapters
from ...web_download.errors import WebDownloadError
from ..base import BaseHandler
from ..request import query_params, single_param
from ..services.site_diagnostics import site_diagnostic_store


class SiteDiagnosticRoutes(BaseHandler):
    def _handle_site_diagnostics(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            site = single_param(params, "site") or None
            if site is not None and site not in SITE_DIAGNOSTIC_SITE_IDS:
                raise ValueError("site diagnostic site is invalid")
            statuses = site_diagnostic_store().list(site=site)
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error, SiteDiagnosticStoreError):
            self._send_json(
                {"ok": False, "error": "Site diagnostic storage is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(
            {
                "ok": True,
                "statuses": [status.public_dict() for status in statuses],
                "codes": load_site_diagnostic_codes().public_dict(),
            }
        )

    def _handle_site_diagnostic_probe(self) -> None:
        try:
            payload = self._read_json_body(16 * 1024)
            site = str(payload.get("site") or "").strip().lower()
            if site not in SITE_DIAGNOSTIC_SITE_IDS:
                raise ValueError("site diagnostic site is invalid")
            if "jav_code" in payload or "fc2_code" in payload:
                codes = normalize_diagnostic_codes(
                    payload.get("jav_code"), payload.get("fc2_code")
                )
                # Remember what the user typed so it is not re-entered next time;
                # scheduled diagnostics use the same codes.
                if codes != load_site_diagnostic_codes():
                    save_site_diagnostic_codes(codes)
            else:
                codes = load_site_diagnostic_codes()
            raw_stages = payload.get("stages")
            if raw_stages is not None and (
                not isinstance(raw_stages, list)
                or not raw_stages
                or any(not isinstance(stage, str) for stage in raw_stages)
            ):
                raise ValueError("site diagnostic stages are invalid")
            store = site_diagnostic_store()
            service = SiteDiagnosticService(
                store,
                build_site_probe_adapters(load_settings(), codes),
            )
            results = service.probe_manual(site, stages=raw_stages)
            # The response replaces the client's diagnostics snapshot. Return all
            # persisted sites so probing one site cannot hide previous results.
            statuses = store.list()
        except (ValueError, WebDownloadError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error, SiteDiagnosticStoreError):
            self._send_json(
                {"ok": False, "error": "Site diagnostic storage is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(
            {
                "ok": True,
                "results": [result.public_dict() for result in results],
                "statuses": [status.public_dict() for status in statuses],
            }
        )
