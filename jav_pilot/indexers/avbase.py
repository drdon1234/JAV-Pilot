from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit

from jav_pilot.net.http_client import FetchError
from jav_pilot.core.models import SearchBounds, SearchResult, SourceDetails

from .fc2 import fc2_product_id
from .metadata_catalog import (
    MetadataCatalogIndexer,
    display_code,
    document,
    metadata_result,
    public_url,
    refs,
    release_date,
    same_code,
    text,
)


class AvBaseIndexer(MetadataCatalogIndexer):
    name = "avbase"
    default_url = "https://www.avbase.net"

    def detail_url_allowed(self, url: str) -> bool:
        return self.request_url_allowed(url) and bool(
            re.fullmatch(r"/works/[A-Za-z0-9_:%.-]+/?", urlsplit(url).path)
        )

    def _page(self, url: str, bounds: SearchBounds, *, api_path: str) -> dict:
        soup = document(self._fetch(url, bounds))
        script = soup.select_one("script#__NEXT_DATA__")
        try:
            payload = json.loads(script.get_text() if script else "")
            props = payload.get("props", {}).get("pageProps", {})
            if "works" in props or "work" in props:
                return props
            build = payload.get("buildId")
            if not isinstance(build, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", build
            ):
                raise ValueError
            data = json.loads(
                self._fetch(f"{self.base_url}/_next/data/{build}/{api_path}", bounds)
            )
            props = data.get("pageProps")
            if not isinstance(props, dict):
                raise ValueError
            return props
        except (ValueError, AttributeError, TypeError):
            raise FetchError("AVBase returned an invalid metadata document") from None

    def _result(self, work: dict, *, resolved: bool) -> SearchResult:
        code = display_code(work.get("work_id"))
        if not code or fc2_product_id(code):
            raise FetchError("AVBase returned an unsupported work identity")
        prefix = text(work.get("prefix"), 50)
        raw_id = f"{prefix}:{work['work_id']}" if prefix else work["work_id"]
        url = f"{self.base_url}/works/{quote(raw_id, safe=':')}"
        products = work.get("products")
        if not isinstance(products, list):
            raise FetchError("AVBase products are missing")
        product = next(
            (
                item
                for item in products
                if isinstance(item, dict) and text(item.get("title"))
            ),
            None,
        )
        if product is None:
            raise FetchError("AVBase work has no metadata product")
        title = text(work.get("title")) or text(product.get("title"))
        casts = work.get("casts")
        cast = work.get("actors") or [
            item.get("actor")
            for item in (casts if isinstance(casts, list) else [])
            if isinstance(item, dict)
        ]
        details = SourceDetails(
            title=title,
            release_date=release_date(product.get("date") or work.get("min_date")),
            makers=refs("maker", product.get("maker")),
            publishers=refs("publisher", product.get("label")),
            series=refs("series", product.get("series")),
            actors=refs("actor", cast),
            tags=refs("tag", work.get("genres")),
        )
        images = [("cover", product.get("image_url") or product.get("thumbnail_url"))]
        samples = product.get("sample_image_urls")
        images.extend(
            ("sample", item.get("l"))
            for item in (samples if isinstance(samples, list) else [])
            if isinstance(item, dict)
        )
        info = (
            product.get("iteminfo") if isinstance(product.get("iteminfo"), dict) else {}
        )
        result = metadata_result(
            source_id=self.source_id,
            provider=self.name,
            url=url,
            code=code,
            title=title,
            details=details,
            images=images,
            description=info.get("description"),
            resolved=resolved,
        )
        upstream = text(product.get("source"), 50)
        upstream_url = public_url(product.get("url"), url)
        # AVBase remains the consulted source; identify its reported upstream without fetching it.
        upstream_fields = {"makers", "publishers", "series", "images", "description"}
        if not text(work.get("title")):
            upstream_fields.add("title")
        if release_date(product.get("date")):
            upstream_fields.add("release_date")
        for field, origin in result.metadata["field_sources"].items():
            if field in upstream_fields:
                origin["upstream_source"] = upstream
                if upstream_url:
                    origin["upstream_url"] = upstream_url
        result.metadata["upstream_products"] = [
            {
                "source": text(item.get("source"), 50),
                "product_id": text(item.get("product_id"), 100),
            }
            for item in products[:20]
            if isinstance(item, dict)
        ]
        return result

    def skip_reason(self, query: str, bounds: SearchBounds) -> str | None:
        return "不收录 FC2 作品" if fc2_product_id(query) else None

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query, bounds = self._query(query, bounds)
        if fc2_product_id(query):
            return ()
        args = f"q={quote(query, safe='')}&page={bounds.page}"
        props = self._page(
            f"{self.base_url}/works?{args}", bounds, api_path=f"works.json?{args}"
        )
        works = props.get("works")
        if not isinstance(works, list):
            raise FetchError("AVBase search result list is missing")
        output = []
        for work in works[:200]:
            if not isinstance(work, dict):
                continue
            try:
                output.append(self._result(work, resolved=False))
            except FetchError:
                continue
            if len(output) >= bounds.limit:
                break
        if works and not output:
            raise FetchError("AVBase search result identities are invalid")
        results = tuple(output)
        return self.enrich_results(results, bounds) if bounds.fetch_magnets else results

    def resolve(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        if not result.url or not self.detail_url_allowed(result.url):
            raise FetchError("AVBase detail URL is invalid")
        identity = urlsplit(result.url).path.rstrip("/").split("/")[-1]
        props = self._page(
            result.url, bounds, api_path=f"works/{identity}.json?id={identity}"
        )
        work = props.get("work")
        if not isinstance(work, dict):
            raise FetchError("AVBase work metadata is missing")
        resolved = self._result(work, resolved=True)
        if not same_code(resolved.code, result.code):
            raise FetchError("AVBase detail identity does not match")
        return resolved
