"""Bounded metadata-only adapters; external sample videos are never downloads."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from jav_pilot.core.catalog_code import canonical_catalog_code, normalize_catalog_code
from jav_pilot.search.fc2_images import fc2_image_path_allowed
from jav_pilot.core.guards import normalize_query
from jav_pilot.net.http_client import FetchError, fetch_text
from jav_pilot.core.models import RelatedRef, SearchBounds, SearchResult, SourceDetails

from .base import Indexer


def text(value: object, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def public_url(value: object, base: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = urljoin(base, value.strip())
        parsed = urlsplit(result)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or any(ord(ch) < 32 for ch in result)
        ):
            return None
        return result
    except ValueError:
        return None


def names(value: object) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(
        dict.fromkeys(
            clean
            for item in values
            if (
                clean := text(item.get("name") if isinstance(item, dict) else item, 200)
            )
        )
    )[:60]


def refs(kind: str, value: object) -> tuple[RelatedRef, ...]:
    return tuple(RelatedRef(kind=kind, label=name) for name in names(value))


def release_date(value: object) -> str | None:
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text(value))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def duration_minutes(value: object) -> int | None:
    raw = text(value)
    iso = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", raw, re.I)
    if iso:
        hours, minutes, seconds = (float(part or 0) for part in iso.groups())
        result = round(hours * 60 + minutes + seconds / 60)
    elif re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", raw):
        parts = [int(part) for part in raw.split(":")]
        result = round(
            sum(part * 60**index for index, part in enumerate(reversed(parts))) / 60
        )
    else:
        match = re.search(r"(\d{1,4})\s*(?:分|min)", raw, re.I)
        result = int(match.group(1)) if match else 0
    return result if 0 < result <= 24 * 60 else None


def json_objects(value: object, *, max_nodes: int = 10000) -> Iterable[dict]:
    queue = [value]
    scanned = 0
    while queue and scanned < max_nodes:
        current = queue.pop()
        scanned += 1
        if isinstance(current, dict):
            yield current
            queue.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            queue.extend(reversed(current))


def json_ld(soup: BeautifulSoup) -> tuple[dict, ...]:
    output = []
    for node in soup.select('script[type="application/ld+json"]')[:20]:
        try:
            payload = json.loads(node.get_text())
        except (ValueError, TypeError):
            continue
        output.extend(
            item
            for item in json_objects(payload)
            if item.get("@type") in ("Movie", "VideoObject", "Product")
        )
    return tuple(output)


def document(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if re.search(
        r"just a moment|attention required|access denied|captcha|verify.*human",
        title,
        re.I,
    ):
        raise FetchError("metadata source returned an access challenge")
    if "not-available-in-your-region" in html:
        raise FetchError("metadata source is unavailable in this region")
    return soup


def meta(soup: BeautifulSoup, property_name: str) -> str:
    node = soup.find("meta", attrs={"property": property_name})
    return text(node.get("content")) if node else ""


def table_fields(soup: BeautifulSoup) -> dict[str, tuple[str, ...]]:
    output = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).rstrip("：: ")
        links = [link.get_text(" ", strip=True) for link in cells[1].select("a")]
        output[label] = tuple(filter(None, links)) or (
            cells[1].get_text(" ", strip=True),
        )
    return output


def metadata_result(
    *,
    source_id: str,
    provider: str,
    url: str,
    code: str | None,
    title: str,
    details: SourceDetails | None = None,
    images: Iterable[tuple[str, object]] = (),
    description: object = None,
    resolved: bool = True,
    extra: dict | None = None,
) -> SearchResult:
    if not text(title):
        raise FetchError("metadata source title is missing")
    details = details or SourceDetails(title=title)
    image_rows = []
    seen = set()
    for kind, image in images:
        safe = public_url(image, url)
        if safe and safe not in seen:
            image_rows.append({"kind": kind, "url": safe})
            seen.add(safe)
        if len(image_rows) >= 40:
            break
    provenance = {
        key: {"source_id": source_id, "provider": provider, "url": url}
        for key, value in details.to_dict().items()
        if value
    }
    if code:
        provenance["code"] = {"source_id": source_id, "provider": provider, "url": url}
    metadata = {
        "details_resolved": resolved,
        "detail_provider": provider,
        "detail_identity_verified": bool(resolved and code),
        "field_sources": provenance,
        "images": image_rows,
        "magnet_checked": resolved,
    }
    if image_rows:
        metadata["cover"] = image_rows[0]["url"]
        provenance["images"] = {
            "source_id": source_id,
            "provider": provider,
            "url": url,
        }
    if text(description):
        metadata["description"] = text(description, 12000)
        provenance["description"] = {
            "source_id": source_id,
            "provider": provider,
            "url": url,
        }
    if extra:
        metadata.update(extra)
    return SearchResult(
        source=source_id,
        title=text(title),
        url=url,
        code=code,
        date=details.release_date,
        actors=tuple(x.label for x in details.actors),
        tags=tuple(x.label for x in details.tags),
        details=details,
        magnet_hint="unavailable",
        metadata=metadata,
    )


class MetadataCatalogIndexer(Indexer):
    default_url = ""
    headers: dict[str, str] = {}
    supports_keyword_search = True

    def __init__(
        self,
        base_url: str = "",
        *,
        source_id: str = "",
        search_template: str = "",
        default_filters: dict[str, str] | None = None,
        parser_rules: dict | None = None,
    ) -> None:
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.source_id = source_id or self.name
        # Native adapters own their schema and paths; CSS/template overrides are not applied.

    def _fetch(
        self,
        url: str,
        bounds: SearchBounds,
        *,
        headers: dict | None = None,
        data: bytes | None = None,
    ) -> str:
        if not self.request_url_allowed(url):
            raise FetchError("metadata request is outside the configured source")
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return fetch_text(
            url,
            timeout=bounds.timeout_seconds,
            max_bytes=bounds.max_response_bytes,
            headers={**self.headers, **(headers or {})},
            allowed_origin=origin,
            data=data,
        )

    def request_url_allowed(self, url: str) -> bool:
        safe = public_url(url, self.base_url)
        return bool(
            safe
            and urlsplit(safe).netloc == urlsplit(self.base_url).netloc
            and urlsplit(safe).scheme == urlsplit(self.base_url).scheme
        )

    def detail_url_allowed(self, url: str) -> bool:
        return self.request_url_allowed(url)

    def diagnostic_detail_search(
        self, query: str, bounds: SearchBounds
    ) -> tuple[SearchResult, ...]:
        return self.search(
            query, replace(bounds.normalized(), fetch_magnets=True, detail_limit=1)
        )

    def enrich_results(
        self,
        results: tuple[SearchResult, ...],
        bounds: SearchBounds,
        *,
        include_images: bool = False,
        detail_limit: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[SearchResult, ...]:
        budget = bounds.detail_limit if detail_limit is None else max(0, detail_limit)
        output = []
        for result in results:
            if (
                budget <= 0
                or (cancelled and cancelled())
                or result.metadata.get("details_resolved")
            ):
                output.append(result)
                continue
            budget -= 1
            try:
                output.append(self.resolve(result, bounds))
            except FetchError:
                output.append(
                    replace(
                        result,
                        metadata={
                            **result.metadata,
                            "details_error": "metadata detail lookup failed",
                            "magnet_checked": True,
                        },
                    )
                )
        return tuple(output)

    def resolve(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        raise NotImplementedError

    def _query(self, query: str, bounds: SearchBounds) -> tuple[str, SearchBounds]:
        return normalize_query(query), bounds.normalized()


def display_code(value: object) -> str | None:
    code = normalize_catalog_code(value)
    return code[0] if code else None


def same_code(left: object, right: object) -> bool:
    first, second = canonical_catalog_code(left), canonical_catalog_code(right)
    return bool(first and first == second)


def image_url_allowed(profile: str, url: str, base_url: str) -> bool:
    """Known image origins only; the caller still enforces public DNS and redirects."""
    safe = public_url(url, base_url)
    if not safe:
        return False
    parsed = urlsplit(safe)
    host = (parsed.hostname or "").lower()
    path = parsed.path
    if not re.search(r"\.(?:jpe?g|png|webp)(?:$)", path, re.I):
        return False
    if parsed.scheme != "https" or "%2f" in path.lower() or "%2e" in path.lower():
        return False
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    if profile in {"fanza", "avbase"} and host == "pics.dmm.co.jp":
        return path.startswith(("/digital/", "/mono/"))
    if profile in {"mgs", "avbase"} and host == "image.mgstage.com":
        return True
    if profile in {"fc2db", "javten"} and fc2_image_path_allowed(host, path):
        return True
    if profile == "fc2db" and host == "img.fc2db.net":
        return path.startswith("/wp-content/uploads/")
    return bool(
        profile in {"avbase", "fc2db", "javten"}
        and host == (urlsplit(base_url).hostname or "").lower()
        and path.startswith(("/images/", "/uploads/", "/storage/", "/media/"))
    )
