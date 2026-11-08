"""Playback records for online works and local indexed videos."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit
from urllib.parse import urlencode

from ...watching.catalog import detail_url, sites
from ...watching.history import duration_value, history_id, record, records, remove_many
from ...core.selection import selected_ids
from ..base import BaseHandler
from ..request import query_params, single_param


class PlaybackHistoryRoutes(BaseHandler):
    def _handle_playback_history(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            items = records()
            for item in items:
                if item.get("kind") == "local" and item.get("entry"):
                    item["cover"] = "/api/library/artwork?" + urlencode({"entry": item["entry"], "kind": "landscape"})
            item_id = single_param(params, "id")
            source = single_param(params, "source")
            url = single_param(params, "url")
            if source and url:
                item_id = history_id("online", f"{source}:{url}")
            self._send_json({"ok": True, "items": [item for item in items if not item_id or item["id"] == item_id]})
        except (ValueError, sqlite3.Error, OSError):
            self._send_json({"ok": False, "error": "Playback history is unavailable"}, 503)

    def _handle_playback_history_action(self) -> None:
        try:
            body = self._read_json_body(512 * 1024)
            if body.get("action") == "remove":
                remove_many(selected_ids(body.get("ids", [body.get("id", "")])))
            else:
                source = str(body.get("source", ""))
                site = next((site for site in sites() if site["id"] == source), None)
                if site is None:
                    raise ValueError("Unknown source")
                url = detail_url(source, site["base_url"].rstrip("/"), str(body.get("url", "")))
                cover = str(body.get("cover", ""))[:2048]
                if urlsplit(cover).scheme != "https":
                    cover = ""
                record({"id": history_id("online", f"{source}:{url}"), "kind": "online", "source": source, "url": url, "cover": cover, "title": str(body.get("title", ""))[:500], "code": str(body.get("code", ""))[:64], "position": duration_value(body.get("position")), "duration": duration_value(body.get("duration")), "completed": body.get("completed") is True})
            self._send_json({"ok": True})
        except (ValueError, sqlite3.Error, OSError):
            self._send_json({"ok": False, "error": "Unable to update playback history"}, 400)
