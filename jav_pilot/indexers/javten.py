from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from jav_pilot.net.http_client import FetchError
from jav_pilot.core.models import SearchBounds, SearchResult

from .fc2 import fc2_product_id
from .fc2db import parse_fc2_metadata
from .metadata_catalog import MetadataCatalogIndexer


class JavTenIndexer(MetadataCatalogIndexer):
    name = "javten"
    catalog_family = "fc2"
    default_url = "https://javten.com"
    supports_keyword_search = False

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query, bounds = self._query(query, bounds)
        product_id = fc2_product_id(query)
        if product_id is None or bounds.page != 1:
            return ()
        # The source maps the FC2 ID to its internal ID with a same-origin redirect.
        url = f"{self.base_url}/search?kw={product_id}"
        return (
            parse_fc2_metadata(
                self._fetch(url, bounds),
                product_id=product_id,
                url=url,
                source_id=self.source_id,
                provider=self.name,
            ),
        )

    def detail_url_allowed(self, url: str) -> bool:
        if not self.request_url_allowed(url):
            return False
        parsed = urlsplit(url)
        if re.match(r"/video/\d+/id\d{2,9}(?:/|$)", parsed.path):
            return True
        query = parse_qs(parsed.query)
        return bool(
            parsed.path == "/search"
            and set(query) == {"kw"}
            and len(query["kw"]) == 1
            and re.fullmatch(r"\d{2,9}", query["kw"][0])
        )

    def resolve(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        product_id = fc2_product_id(result.code)
        if (
            product_id is None
            or not result.url
            or not self.detail_url_allowed(result.url)
        ):
            raise FetchError("JAVTEN detail identity is invalid")
        parsed = urlsplit(result.url)
        path_id = re.match(r"/video/\d+/id(\d{2,9})(?:/|$)", parsed.path)
        url_id = path_id.group(1) if path_id else parse_qs(parsed.query)["kw"][0]
        if url_id != product_id:
            raise FetchError("JAVTEN detail identity does not match")
        return self.search(f"FC2-PPV-{product_id}", replace(bounds, page=1))[0]
