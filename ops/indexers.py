#!/usr/bin/env python3
"""Configure or inspect the dedicated Jackett service without printing secrets."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


INDEXERS = ("sukebeinyaasi", "tokyotosho")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class IndexerOperationError(RuntimeError):
    pass


def inherit_app_proxy(server_config: Path) -> dict[str, object]:
    """Reuse only JAV Pilot's configured egress, never inspect other services."""
    service = json.loads(
        subprocess.run(
            ["docker", "inspect", "jav-pilot-indexers"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )[0]
    if service["State"]["Running"]:
        raise IndexerOperationError(
            "stop Jackett before changing its proxy configuration"
        )
    app = json.loads(
        subprocess.run(
            ["docker", "inspect", "jav-pilot"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )[0]
    environment = dict(
        entry.split("=", 1) for entry in app["Config"].get("Env", []) if "=" in entry
    )
    proxy = next(
        (
            environment.get(name)
            for name in ("JAV_PILOT_PROXY", "HTTPS_PROXY", "HTTP_PROXY")
            if environment.get(name)
        ),
        "",
    )
    parsed = urllib.parse.urlsplit(proxy)
    proxy_types = {"http": 0, "socks4": 1, "socks5": 2, "socks5h": 2}
    if (
        parsed.scheme not in proxy_types
        or not parsed.hostname
        or not parsed.port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise IndexerOperationError(
            "the application does not have a supported explicit proxy"
        )
    if (
        not server_config.is_file()
        or server_config.is_symlink()
        or server_config.stat().st_size > 1024 * 1024
    ):
        raise IndexerOperationError(
            "Jackett configuration is not a bounded regular file"
        )
    configuration = json.loads(server_config.read_text())
    configuration.update(
        {
            "ProxyType": proxy_types[parsed.scheme],
            "ProxyUrl": parsed.hostname,
            "ProxyPort": parsed.port,
            "ProxyUsername": urllib.parse.unquote(parsed.username or ""),
            "ProxyPassword": urllib.parse.unquote(parsed.password or ""),
        }
    )
    temporary = server_config.with_name(".ServerConfig.json.proxy.tmp")
    created = False
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(configuration, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, server_config)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return {
        "proxy_configured": True,
        "source_container": "jav-pilot",
        "credentials_printed": False,
    }


class JackettClient:
    def __init__(self, origin: str, server_config: Path) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise IndexerOperationError("service origin is invalid")
        self.origin = origin.rstrip("/")
        if not server_config.is_file() or server_config.is_symlink():
            raise IndexerOperationError("Jackett configuration is not a regular file")
        if server_config.stat().st_size > 1024 * 1024:
            raise IndexerOperationError("Jackett configuration is too large")
        configuration = json.loads(server_config.read_text())
        self._key = configuration.get("APIKey")
        if not isinstance(self._key, str) or not self._key:
            raise IndexerOperationError("Jackett API key is not configured")
        self._client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, path: str, *, data: object = None, timeout: float = 35) -> bytes:
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            self.origin + path,
            data=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
        )
        try:
            with self._client.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise IndexerOperationError("Jackett response exceeded byte limit")
            return raw
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise IndexerOperationError(f"Jackett HTTP {code}") from None
        except (OSError, urllib.error.URLError):
            raise IndexerOperationError("Jackett request failed or timed out") from None

    def configured(self) -> dict[str, bool]:
        self.request("/UI/Dashboard", timeout=10)
        entries = json.loads(self.request("/api/v2.0/indexers", timeout=15))
        return {
            entry["id"]: bool(entry.get("configured"))
            for entry in entries
            if entry.get("id") in INDEXERS
        }

    def configure_public(self) -> list[dict[str, object]]:
        current = self.configured()
        output: list[dict[str, object]] = []
        for indexer in INDEXERS:
            if current.get(indexer):
                output.append({"indexer": indexer, "status": "already_configured"})
                continue
            fields = json.loads(
                self.request(f"/api/v2.0/indexers/{indexer}/config", timeout=15)
            )
            if indexer == "tokyotosho":
                for item in fields:
                    if item.get("id") == "sitelink":
                        # Maintained Jackett mirror; preserve an existing user's
                        # choice by applying this only on first configuration.
                        item["value"] = "https://www.tokyotosho.se/"
            self.request(f"/api/v2.0/indexers/{indexer}/config", data=fields)
            output.append({"indexer": indexer, "status": "configured"})
        return output

    def torznab(self, indexer: str, parameters: dict[str, str]) -> ET.Element:
        query = urllib.parse.urlencode({**parameters, "apikey": self._key})
        body = self.request(f"/api/v2.0/indexers/{indexer}/results/torznab/api?{query}")
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise IndexerOperationError("Jackett returned forbidden XML declarations")
        try:
            root = ET.fromstring(body.decode("utf-8-sig"))
        except (UnicodeError, ET.ParseError):
            raise IndexerOperationError("Jackett returned invalid XML") from None
        if root.tag == "error":
            code = root.get("code", "")
            clean_code = code if re.fullmatch(r"[0-9]{1,5}", code) else "unknown"
            raise IndexerOperationError(f"Jackett Torznab error {clean_code}")
        return root

    def verify(self, queries: list[str]) -> dict[str, object]:
        configured = self.configured()
        output: list[dict[str, object]] = []
        for indexer in INDEXERS:
            entry: dict[str, object] = {
                "indexer": indexer,
                "configured": configured.get(indexer, False),
                "endpoint": f"{self.origin}/api/v2.0/indexers/{indexer}/results/torznab/api",
                "queries": [],
            }
            try:
                caps = self.torznab(indexer, {"t": "caps"})
                search = caps.find("./searching/search")
                entry["search_available"] = (
                    caps.tag == "caps"
                    and search is not None
                    and search.get("available") == "yes"
                )
            except IndexerOperationError as error:
                entry["caps_error"] = str(error)
            for query in queries:
                started = time.monotonic()
                try:
                    rss = self.torznab(
                        indexer, {"t": "search", "q": query, "limit": "10"}
                    )
                    summary = summarize_results(rss)
                    entry["queries"].append(
                        {
                            "query": query,
                            "seconds": round(time.monotonic() - started, 2),
                            **summary,
                        }
                    )
                except IndexerOperationError as error:
                    entry["queries"].append(
                        {
                            "query": query,
                            "seconds": round(time.monotonic() - started, 2),
                            "error": str(error),
                        }
                    )
            output.append(entry)
        return {"sources": output}


def summarize_results(root: ET.Element) -> dict[str, object]:
    if root.tag != "rss" or root.find("channel") is None:
        raise IndexerOperationError("Jackett search did not return RSS")
    items = root.findall("./channel/item")
    hashes: set[str] = set()
    magnet_hashes: set[str] = set()
    fc2_ids: set[str] = set()
    codes: set[str] = set()
    torrent_links = 0
    native_magnets = 0
    for item in items:
        title = item.findtext("title", "")
        fc2_ids.update(
            re.findall(
                r"(?<![A-Z0-9])FC2[-_. ]*(?:PPV[-_. ]*)?(\d{2,9})(?![A-Z0-9])",
                title,
                flags=re.I,
            )
        )
        codes.update(
            re.findall(
                r"(?<![A-Z0-9])([A-Z]{2,8}-\d{2,8})(?![A-Z0-9])", title, flags=re.I
            )
        )
        attributes = {
            element.get("name", ""): element.get("value", "")
            for element in item.findall("{http://torznab.com/schemas/2015/feed}attr")
        }
        info_hash = attributes.get("infohash", "")
        if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
            hashes.add(info_hash.lower())
        candidates = [attributes.get("magneturl", ""), item.findtext("link", "")]
        candidates.extend(
            element.get("url", "") for element in item.findall("enclosure")
        )
        for candidate in candidates:
            if not candidate.lower().startswith("magnet:?"):
                continue
            for xt in urllib.parse.parse_qs(
                urllib.parse.urlsplit(candidate).query, max_num_fields=128
            ).get("xt", []):
                if re.fullmatch(r"urn:btih:[0-9a-fA-F]{40}", xt, flags=re.I):
                    magnet_hashes.add(xt[9:].lower())
        if any(value.lower().startswith("magnet:?") for value in candidates):
            native_magnets += 1
        if any(value.startswith(("http://", "https://")) for value in candidates):
            torrent_links += 1
    return {
        "item_count": len(items),
        "unique_declared_infohashes": len(hashes),
        "unique_magnet_infohashes": len(magnet_hashes),
        "native_magnet_items": native_magnets,
        "torrent_link_items": torrent_links,
        "fc2_ids": sorted(fc2_ids)[:10],
        "catalog_codes": sorted(
            code for code in codes if not code.upper().startswith("PPV-")
        )[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("configure-public", "inherit-proxy", "verify")
    )
    parser.add_argument("--origin", required=True)
    parser.add_argument("--server-config", type=Path, required=True)
    parser.add_argument("--query", action="append")
    args = parser.parse_args()
    try:
        if args.action == "inherit-proxy":
            print(json.dumps(inherit_app_proxy(args.server_config), indent=2))
            return 0
        client = JackettClient(args.origin, args.server_config)
        if args.action == "configure-public":
            output = {"sources": client.configure_public()}
        else:
            if not args.query:
                raise SystemExit("verify requires at least one --query")
            output = client.verify(args.query)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        if args.action == "verify" and any(
            not source.get("search_available")
            or any("error" in query for query in source["queries"])
            for source in output["sources"]
        ):
            return 1
        return 0
    except IndexerOperationError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
    except Exception as error:
        # Never print raw HTTP exceptions, file contents or request URLs.
        print(json.dumps({"ok": False, "error": type(error).__name__}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
