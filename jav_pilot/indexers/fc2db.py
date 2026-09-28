from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlsplit

from jav_pilot.net.http_client import FetchError
from jav_pilot.core.models import SearchBounds, SearchResult, SourceDetails

from .fc2 import fc2_product_id
from .metadata_catalog import (
    MetadataCatalogIndexer,
    document,
    duration_minutes,
    json_ld,
    meta,
    metadata_result,
    public_url,
    refs,
    release_date,
    text,
)


def parse_fc2_metadata(
    html: str, *, product_id: str, url: str, source_id: str, provider: str
) -> SearchResult:
    soup = document(html)
    candidates = json_ld(soup)
    heading = soup.select_one("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    heading_identities = set(
        re.findall(r"\bFC2[-_ ]*(?:PPV[-_ ]*)?(\d{2,9})(?!\d)", heading_text, re.I)
    )
    if heading_identities and heading_identities != {product_id}:
        raise FetchError("FC2 metadata heading conflicts with the requested work")
    matched = []
    for candidate in candidates:
        identifiers = candidate.get("identifier", [])
        if not isinstance(identifiers, list):
            identifiers = [identifiers]
        values = [candidate.get("name", "")]
        identities = set()
        for identifier in identifiers:
            if isinstance(identifier, dict):
                identifier = identifier.get("value", "")
            raw = text(identifier)
            if re.fullmatch(r"\d{2,9}", raw):
                identities.add(raw)
            values.append(raw)
        for value in values:
            identities.update(
                re.findall(
                    r"\bFC2[-_ ]*(?:PPV[-_ ]*)?(\d{2,9})(?!\d)", text(value), re.I
                )
            )
        # A page heading can identify a sole document, but cannot identify every
        # JSON-LD object when recommendations or other products are also present.
        if not identities and len(candidates) == 1:
            identities = heading_identities
        if product_id in identities:
            if identities != {product_id}:
                raise FetchError(
                    "FC2 metadata identity conflicts with the requested work"
                )
            matched.append(candidate)
    if not matched:
        raise FetchError("FC2 metadata identity is missing or does not match")
    item = matched[0]
    # Validate any published canonical URL independently from the input URL.
    for link in (soup.select_one('link[rel="canonical"]'),):
        if not link:
            continue
        canonical = public_url(link.get("href"), url)
        if not canonical or (
            urlsplit(canonical).scheme,
            urlsplit(canonical).netloc,
        ) != (urlsplit(url).scheme, urlsplit(url).netloc):
            raise FetchError("FC2 metadata canonical URL is invalid")
        identity = re.search(r"/(?:work/|id)(\d{2,9})(?:/|$)", urlsplit(canonical).path)
        if identity and identity.group(1) != product_id:
            raise FetchError("FC2 metadata canonical identity does not match")
        url = canonical
    title = heading_text or text(item.get("name"))
    seller = item.get("publisher")
    if provider == "javten" and not seller:
        # JAVTEN's Movie director field describes the publisher, not cast.
        seller = item.get("director")
    actors = refs("actor", item.get("actor", []))
    tags = refs("tag", item.get("genre", []))
    if not tags:
        tags = refs(
            "tag",
            [
                node.get_text(" ", strip=True)
                for node in soup.select('a[href*="/work-tags/"]')
            ],
        )
    details = SourceDetails(
        title=title,
        release_date=release_date(item.get("datePublished") or item.get("uploadDate")),
        duration_minutes=duration_minutes(item.get("duration")),
        duration_text=text(item.get("duration")) or None,
        makers=refs("maker", seller),
        publishers=refs("publisher", seller),
        actors=actors,
        tags=tags,
    )
    cover = item.get("image") or item.get("thumbnailUrl") or meta(soup, "og:image")
    if isinstance(cover, list):
        cover = cover[0] if cover else None
    if isinstance(cover, dict):
        cover = cover.get("url")
    images = [("cover", cover)]
    images.extend(
        ("sample", node.get("href"))
        for node in soup.select('a[data-fancybox="gallery"]')
    )
    return metadata_result(
        source_id=source_id,
        provider=provider,
        url=url,
        code=f"FC2-PPV-{product_id}",
        title=title,
        details=details,
        images=images,
        description=item.get("description"),
    )


class Fc2DbIndexer(MetadataCatalogIndexer):
    name = "fc2db"
    catalog_family = "fc2"
    default_url = "https://fc2db.net"
    supports_keyword_search = False
    # This endpoint serves its public work document with generic content negotiation;
    # the application's XML-preferred Accept header returns an age-gate document.
    headers = {"Accept": "*/*"}

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query, bounds = self._query(query, bounds)
        product_id = fc2_product_id(query)
        if product_id is None or bounds.page != 1:
            return ()
        url = f"{self.base_url}/work/{product_id}/"
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
        return self.request_url_allowed(url) and bool(
            re.fullmatch(r"/work/\d{2,9}/?", urlsplit(url).path)
        )

    def resolve(self, result: SearchResult, bounds: SearchBounds) -> SearchResult:
        product_id = fc2_product_id(result.code)
        if (
            product_id is None
            or not result.url
            or not self.detail_url_allowed(result.url)
            or urlsplit(result.url).path.rstrip("/") != f"/work/{product_id}"
        ):
            raise FetchError("FC2DB detail identity is invalid")
        return self.search(f"FC2-PPV-{product_id}", replace(bounds, page=1))[0]
