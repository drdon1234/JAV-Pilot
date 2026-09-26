from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from jav_pilot.net.http_client import FetchError
from jav_pilot.core.models import SearchBounds, SearchResult, SourceDetails

from .fc2 import fc2_product_id
from .metadata_catalog import (
    MetadataCatalogIndexer,
    display_code,
    document,
    duration_minutes,
    meta,
    metadata_result,
    public_url,
    refs,
    release_date,
    same_code,
    table_fields,
)


class MgsIndexer(MetadataCatalogIndexer):
    name = "mgs"
    default_url = "https://www.mgstage.com"
    headers = {"Cookie": "adc=1"}

    def detail_url_allowed(self, url: str) -> bool:
        return self.request_url_allowed(url) and bool(
            re.fullmatch(
                r"/product/product_detail/[A-Za-z0-9_-]+/?", urlsplit(url).path
            )
        )

    def skip_reason(self, query: str, bounds: SearchBounds) -> str | None:
        return "不收录 FC2 作品" if fc2_product_id(query) else None

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query, bounds = self._query(query, bounds)
        if fc2_product_id(query):
            return ()
        url = f"{self.base_url}/search/cSearch.php?search_word={quote(query, safe='')}&page={bounds.page}"
        soup = document(self._fetch(url, bounds))
        cards = soup.select("#center_column ul.product_list > li")
        output = []
        seen = set()
        for card in cards:
            anchor = card.select_one("h5 a[href]")
            target = public_url(anchor.get("href"), self.base_url) if anchor else None
            if not target or not self.detail_url_allowed(target) or target in seen:
                continue
            code = display_code(urlsplit(target).path.rstrip("/").split("/")[-1])
            title_node = card.select_one("a.title, a p")
            title = (
                title_node.get_text(" ", strip=True)
                if title_node
                else anchor.get("title", "")
            )
            if not code or not title:
                continue
            image = card.select_one("h5 a img")
            output.append(
                metadata_result(
                    source_id=self.source_id,
                    provider=self.name,
                    url=target,
                    code=code,
                    title=title,
                    resolved=False,
                    images=[("cover", image.get("src") if image else None)],
                )
            )
            seen.add(target)
            if len(output) >= bounds.limit:
                break
        if not output and not re.search(
            r"該当.*(?:ありません|ございません)|検索結果.*0件|見つかりません",
            soup.get_text(" ", strip=True),
        ):
            raise FetchError("MGS search page did not contain recognized results")
        results = tuple(output)
        return self.enrich_results(results, bounds) if bounds.fetch_magnets else results

    def resolve(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        if not result.url or not self.detail_url_allowed(result.url):
            raise FetchError("MGS detail URL is invalid")
        soup = document(self._fetch(result.url, bounds))
        fields = table_fields(soup)
        code = display_code(next(iter(fields.get("品番", ())), ""))
        if not code or not same_code(code, result.code):
            raise FetchError("MGS detail identity does not match")
        heading = soup.select_one("#center_column h1")
        title = heading.get_text(" ", strip=True) if heading else meta(soup, "og:title")

        def first(*labels: str) -> str:
            return next((fields[label][0] for label in labels if fields.get(label)), "")

        details = SourceDetails(
            title=title,
            release_date=release_date(first("配信開始日", "商品発売日")),
            duration_minutes=duration_minutes(first("収録時間")),
            duration_text=first("収録時間") or None,
            actors=refs("actor", list(fields.get("出演", ()))),
            makers=refs("maker", list(fields.get("メーカー", ()))),
            publishers=refs("publisher", list(fields.get("レーベル", ()))),
            series=refs("series", list(fields.get("シリーズ", ()))),
            tags=refs("tag", list(fields.get("ジャンル", ()))),
        )
        cover = soup.select_one("#EnlargeImage")
        images = [("cover", cover.get("href") if cover else meta(soup, "og:image"))]
        images.extend(
            ("sample", node.get("href"))
            for node in soup.select("#sample-photo a[href]")
        )
        return metadata_result(
            source_id=self.source_id,
            provider=self.name,
            url=result.url,
            code=code,
            title=title,
            details=details,
            images=images,
            description=meta(soup, "og:description"),
        )
