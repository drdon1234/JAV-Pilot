from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, quote, urlsplit

from jav_pilot.net.http_client import FetchError
from jav_pilot.core.models import SearchBounds, SearchResult, SourceDetails

from .fc2 import fc2_product_id
from .metadata_catalog import (
    MetadataCatalogIndexer,
    display_code,
    document,
    duration_minutes,
    json_ld,
    json_objects,
    meta,
    metadata_result,
    public_url,
    refs,
    release_date,
    same_code,
    table_fields,
    text,
)


_DETAIL_QUERY = """query Metadata($id: ID!) {
  ppvContent(id: $id) {
    id floor title description makerContentId duration deliveryStartDate makerReleasedAt
    packageImage { largeUrl mediumUrl }
    sampleImages { largeImageUrl }
    maker { name } label { name } series { name }
    actresses { name } directors { name } genres { name }
  }
}"""


def fanza_display_code(value: object) -> str | None:
    """Map unambiguous letter/number CIDs; do not guess vendor-prefixed IDs."""
    raw = text(value, 100)
    match = re.fullmatch(r"([a-zA-Z]{2,12})0*(\d{1,8})", raw)
    if match:
        return f"{match.group(1).upper()}-{int(match.group(2)):03d}"
    return (
        display_code(raw) if "-" in raw and not raw.lower().startswith("h_") else None
    )


def fanza_search_payload(html: str) -> dict:
    soup = document(html)
    payloads = []
    flight = []
    decoder = json.JSONDecoder()
    for script in soup.select("script"):
        raw = script.get_text()
        if script.get("type") == "application/json":
            try:
                payloads.append(json.loads(raw))
            except ValueError:
                pass
        for marker in re.finditer(r"self\.__next_f\.push\(", raw):
            try:
                part, _ = decoder.raw_decode(raw[marker.end() :].lstrip())
            except ValueError:
                continue
            if (
                isinstance(part, list)
                and len(part) == 2
                and part[0] == 1
                and isinstance(part[1], str)
            ):
                flight.append(part[1])
    for line in "".join(flight).splitlines():
        _, _, raw = line.partition(":")
        if "backendResponse" not in raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except ValueError:
            continue
    for payload in payloads:
        for item in json_objects(payload):
            backend = item.get("backendResponse")
            if isinstance(backend, dict) and isinstance(backend.get("contents"), dict):
                return backend
    raise FetchError("FANZA search page did not contain recognized metadata")


class FanzaIndexer(MetadataCatalogIndexer):
    name = "fanza"
    default_url = "https://www.dmm.co.jp"
    headers = {"Cookie": "age_check_done=1"}

    def request_url_allowed(self, url: str) -> bool:
        if super().request_url_allowed(url):
            return True
        safe = public_url(url, self.base_url)
        return bool(
            safe
            and urlsplit(safe).scheme == "https"
            and urlsplit(safe).netloc in {"video.dmm.co.jp", "api.video.dmm.co.jp"}
        )

    def detail_url_allowed(self, url: str) -> bool:
        if not self.request_url_allowed(url):
            return False
        parsed = urlsplit(url)
        return bool(
            re.fullmatch(r"/(?:av|amateur)/content/?", parsed.path)
            and re.fullmatch(
                r"[a-zA-Z0-9_]{1,100}", parse_qs(parsed.query).get("id", [""])[0]
            )
            or re.fullmatch(r"/mono/dvd/-/detail/=/cid=[a-zA-Z0-9_]+/", parsed.path)
        )

    def skip_reason(self, query: str, bounds: SearchBounds) -> str | None:
        return "不收录 FC2 作品" if fc2_product_id(query) else None

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query, bounds = self._query(query, bounds)
        if fc2_product_id(query):
            return ()
        keyword = query.replace("-", "") if display_code(query) else query
        url = f"{self.base_url}/search/=/searchstr={quote(keyword, safe='')}/limit=30/sort=date/page={bounds.page}/"
        payload = fanza_search_payload(self._fetch(url, bounds))
        rows = payload["contents"].get("data")
        if not isinstance(rows, list):
            raise FetchError("FANZA search result list is missing")
        output = []
        seen = set()
        for item in rows[:200]:
            if not isinstance(item, dict):
                continue
            target = public_url(item.get("detail_url"), self.base_url)
            if not target or not self.detail_url_allowed(target) or target in seen:
                continue
            cid = text(item.get("content_id"), 100)
            title = text(item.get("title"))
            if not title or not cid:
                continue
            target_path = urlsplit(target)
            path_id = re.search(r"/cid=([A-Za-z0-9_]+)/", target_path.path)
            target_id = (
                path_id.group(1)
                if path_id
                else parse_qs(target_path.query).get("id", [""])[0]
            )
            if cid.lower() != target_id.lower():
                continue
            code = fanza_display_code(cid)
            details = SourceDetails(
                title=title,
                actors=refs("actor", item.get("casts")),
                makers=refs("maker", item.get("makers")),
                tags=refs("tag", item.get("keywords")),
                series=refs("series", item.get("series")),
            )
            output.append(
                metadata_result(
                    source_id=self.source_id,
                    provider=self.name,
                    url=target,
                    code=code,
                    title=title,
                    details=details,
                    resolved=False,
                    images=[("cover", item.get("thumbnail_image_url"))],
                    extra={"content_id": cid},
                )
            )
            seen.add(target)
            if len(output) >= bounds.limit:
                break
        if rows and not output:
            raise FetchError("FANZA search returned no supported video products")
        results = tuple(output)
        return self.enrich_results(results, bounds) if bounds.fetch_magnets else results

    def resolve(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        if not result.url or not self.detail_url_allowed(result.url):
            raise FetchError("FANZA detail URL is invalid")
        parsed = urlsplit(result.url)
        if parsed.path.startswith("/mono/"):
            return self._mono_detail(result, bounds)
        cid = parse_qs(parsed.query).get("id", [""])[0]
        raw = self._fetch(
            "https://api.video.dmm.co.jp/graphql",
            bounds,
            headers={
                "Content-Type": "application/json",
                "Referer": "https://video.dmm.co.jp/",
                "Fanza-Device": "BROWSER",
            },
            data=json.dumps({"query": _DETAIL_QUERY, "variables": {"id": cid}}).encode(
                "utf-8"
            ),
        )
        try:
            payload = json.loads(raw)
            item = payload.get("data", {}).get("ppvContent")
            if (
                payload.get("errors")
                or not isinstance(item, dict)
                or item.get("id") != cid
            ):
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise FetchError(
                "FANZA detail identity is missing or does not match"
            ) from None
        code = display_code(item.get("makerContentId")) or fanza_display_code(cid)
        if result.code and not same_code(result.code, code):
            raise FetchError("FANZA display number does not match the selected work")
        title = text(item.get("title"))
        duration = item.get("duration")
        minutes = (
            round(duration / 60)
            if isinstance(duration, (float, int)) and 0 < duration < 86400
            else None
        )
        details = SourceDetails(
            title=title,
            release_date=release_date(
                item.get("deliveryStartDate") or item.get("makerReleasedAt")
            ),
            duration_minutes=minutes,
            actors=refs("actor", item.get("actresses")),
            makers=refs("maker", item.get("maker")),
            publishers=refs("publisher", item.get("label")),
            series=refs("series", item.get("series")),
            directors=refs("director", item.get("directors")),
            tags=refs("tag", item.get("genres")),
        )
        package = (
            item.get("packageImage")
            if isinstance(item.get("packageImage"), dict)
            else {}
        )
        images = [("cover", package.get("largeUrl") or package.get("mediumUrl"))]
        samples = item.get("sampleImages")
        images.extend(
            ("sample", image.get("largeImageUrl"))
            for image in (samples if isinstance(samples, list) else [])
            if isinstance(image, dict)
        )
        return metadata_result(
            source_id=self.source_id,
            provider=self.name,
            url=result.url,
            code=code,
            title=title,
            details=details,
            images=images,
            description=item.get("description"),
            extra={"content_id": cid},
        )

    def _mono_detail(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        soup = document(self._fetch(result.url, bounds))
        fields = table_fields(soup)
        item = next(iter(json_ld(soup)), {})
        cid = text(item.get("sku")) or next(iter(fields.get("品番", ())), "")
        expected = re.search(r"/cid=([A-Za-z0-9_]+)/", result.url).group(1)
        if cid.lower() != expected.lower():
            raise FetchError("FANZA detail identity does not match")
        code = fanza_display_code(cid)
        if result.code and not same_code(result.code, code):
            raise FetchError("FANZA display number does not match")
        title = text(item.get("name")) or meta(soup, "og:title")

        def first(*labels: str) -> str:
            return next((fields[label][0] for label in labels if fields.get(label)), "")

        details = SourceDetails(
            title=title,
            release_date=release_date(first("商品発売日", "発売日", "配信開始日")),
            duration_minutes=duration_minutes(first("収録時間")),
            duration_text=first("収録時間") or None,
            actors=refs(
                "actor",
                [
                    node.get_text(" ", strip=True)
                    for node in soup.select("#performer a")
                ],
            ),
            makers=refs("maker", list(fields.get("メーカー", ()))),
            publishers=refs("publisher", list(fields.get("レーベル", ()))),
            tags=refs("tag", list(fields.get("ジャンル", ()))),
        )
        images = [("cover", item.get("image") or meta(soup, "og:image"))]
        images.extend(
            ("sample", node.get("href"))
            for node in soup.select('#sample-image-block a[name="sample-image"]')
        )
        return metadata_result(
            source_id=self.source_id,
            provider=self.name,
            url=result.url,
            code=code,
            title=title,
            details=details,
            images=images,
            description=item.get("description"),
            extra={"content_id": cid},
        )
