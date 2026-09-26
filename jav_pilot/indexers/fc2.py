from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from jav_pilot.core.catalog_code import canonical_catalog_code, normalize_catalog_code
from jav_pilot.search.fc2_images import fc2_image_path_allowed
from jav_pilot.core.guards import looks_like_catalog_code, normalize_query
from jav_pilot.net.http_client import FetchError, fetch_text
from jav_pilot.core.models import (
    Rating,
    RelatedRef,
    SearchBounds,
    SearchResult,
    SourceDetails,
    SourceImage,
    normalize_source_images,
)

from .base import Indexer


_FC2_KEY_RE = re.compile(r"^FC2PPV(\d{2,9})$")
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
_DURATION_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?!\d)")
_OFFICIAL_HOST = "adult.contents.fc2.com"
_PPV_DATABANK_HOST = "ppvdatabank.com"
_OFFICIAL_DETAIL_RE = re.compile(r"^/article/(\d{2,9})/$")
_PPV_DETAIL_RE = re.compile(r"^/article/(\d{2,9})/$")
_AVSOX_DETAIL_RE = re.compile(r"^/(?:[a-z]{2}/)?movies/[a-z0-9]+/?$", re.IGNORECASE)
_MAX_AVSOX_RESULTS = 200
_MAX_IMAGES = 80


@dataclass(frozen=True, slots=True)
class _Fc2Detail:
    product_id: str
    detail_url: str
    details: SourceDetails
    images: tuple[SourceImage, ...]
    description: str | None
    provider: str


class Fc2Indexer(Indexer):
    """FC2 discovery backed by AVSOX with verified first-party details."""

    name = "fc2"

    def __init__(
        self,
        base_url: str = "https://avsox.click",
        *,
        search_template: str = "",
        default_filters: dict[str, str] | None = None,
        source_id: str = "",
        parser_rules: dict | None = None,
    ) -> None:
        self.base_url = (base_url or "https://avsox.click").rstrip("/")
        self.search_template = (
            search_template.strip() or "{base_url}/javu/data/api/search"
        )
        self.default_filters = default_filters or {}
        self.source_id = source_id.strip() or self.name
        # Kept for the common indexer construction contract. FC2 is a JSON/API
        # profile and intentionally has no user-editable CSS parser rules.
        self.parser_rules = parser_rules

    def skip_reason(self, query: str, bounds: SearchBounds) -> str | None:
        if fc2_product_id(query) is None and (
            bounds.search_kind == "code" or looks_like_catalog_code(query)
        ):
            return "仅支持 FC2 番号"
        return None

    def search(self, query: str, bounds: SearchBounds) -> tuple[SearchResult, ...]:
        query = normalize_query(query)
        bounds = bounds.normalized()
        product_id = fc2_product_id(query)
        if product_id is not None:
            return (self._resolve_exact(product_id, bounds),)
        if bounds.search_kind == "code" or looks_like_catalog_code(query):
            return ()

        results = self._search_avsox(query, bounds)
        if not bounds.fetch_magnets or bounds.detail_limit <= 0:
            return results
        return self.enrich_results(
            results,
            bounds,
            include_images=False,
            detail_limit=bounds.detail_limit,
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
        bounds = bounds.normalized()
        remaining = (
            bounds.detail_limit if detail_limit is None else max(0, int(detail_limit))
        )
        enriched: list[SearchResult] = []
        for result in results:
            if cancelled is not None and cancelled():
                enriched.append(result)
                continue
            product_id = fc2_product_id(result.code)
            if product_id is None or remaining <= 0:
                enriched.append(result)
                continue
            remaining -= 1
            try:
                detail = self._fetch_verified_detail(
                    product_id,
                    bounds,
                    include_images=include_images,
                )
            except Exception as exc:  # noqa: BLE001 - retain the AVSOX discovery record.
                metadata = dict(result.metadata)
                metadata.update(
                    {
                        "details_resolved": True,
                        "details_error": str(exc),
                        "magnet_checked": True,
                    }
                )
                if include_images:
                    metadata.update(
                        {
                            "images_resolved": True,
                            "images_error": "FC2 detail images are unavailable",
                        }
                    )
                enriched.append(
                    replace(result, magnet_hint="unavailable", metadata=metadata)
                )
                continue
            enriched.append(
                self._result_from_detail(
                    detail,
                    fallback=result,
                    include_images=include_images,
                )
            )
        return tuple(enriched)

    def detail_url_allowed(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
        ):
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host == _OFFICIAL_HOST:
            if _OFFICIAL_DETAIL_RE.fullmatch(parsed.path) is None:
                return False
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            except ValueError:
                return False
            return set(query).issubset({"lang"})
        if host == _PPV_DATABANK_HOST:
            return (
                not parsed.query and _PPV_DETAIL_RE.fullmatch(parsed.path) is not None
            )
        try:
            base = urlsplit(self.base_url)
        except ValueError:
            return False
        return (
            not parsed.query
            and _origin(parsed) == _origin(base)
            and _AVSOX_DETAIL_RE.fullmatch(parsed.path) is not None
        )

    def _resolve_exact(self, product_id: str, bounds: SearchBounds) -> SearchResult:
        detail = self._fetch_verified_detail(product_id, bounds, include_images=True)
        return self._result_from_detail(detail, fallback=None, include_images=True)

    def _fetch_verified_detail(
        self,
        product_id: str,
        bounds: SearchBounds,
        *,
        include_images: bool,
    ) -> _Fc2Detail:
        official_error: Exception | None = None
        try:
            return self._fetch_official_detail(
                product_id,
                bounds,
                include_images=include_images,
            )
        except Exception as exc:  # noqa: BLE001 - PPV DataBank is the intended fallback.
            official_error = exc
        try:
            return self._fetch_ppvdatabank_detail(
                product_id,
                bounds,
                include_images=include_images,
            )
        except Exception as exc:  # noqa: BLE001 - expose one stable source error.
            message = _detail_source_error(official_error, exc)
            raise FetchError(message) from exc

    def _search_avsox(
        self,
        query: str,
        bounds: SearchBounds,
    ) -> tuple[SearchResult, ...]:
        endpoint = self._search_endpoint()
        payload = json.dumps(
            [{"search": query, "lang": "tw"}, 60, bounds.page],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        page = fetch_text(
            endpoint,
            timeout=bounds.timeout_seconds,
            max_bytes=bounds.max_response_bytes,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
            },
            allowed_origin=self.base_url,
            data=payload,
        )
        try:
            response = json.loads(page)
        except (TypeError, json.JSONDecodeError) as exc:
            raise FetchError("AVSOX returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("code") != 200:
            raise FetchError("AVSOX search was rejected")
        raw_records = response.get("data")
        if not isinstance(raw_records, list):
            raise FetchError("AVSOX returned an invalid result list")

        results: list[SearchResult] = []
        for value in raw_records[:_MAX_AVSOX_RESULTS]:
            result = self._avsox_result(value)
            if result is not None:
                results.append(result)
            if len(results) >= bounds.limit:
                break
        return tuple(results)

    def _avsox_result(self, value: object) -> SearchResult | None:
        if (
            not isinstance(value, dict)
            or str(value.get("from") or "").casefold() != "fc2"
        ):
            return None
        normalized = normalize_catalog_code(value.get("movieFanHao"), max_length=40)
        if normalized is None:
            return None
        display_code, canonical_code = normalized
        match = _FC2_KEY_RE.fullmatch(canonical_code)
        if match is None:
            return None
        product_id = match.group(1)
        old_url = _clean_text(value.get("movieOldUrl"), 2048)
        if (
            not old_url
            or _detail_product_id(old_url, host=_OFFICIAL_HOST) != product_id
        ):
            return None
        detail_url = _official_detail_url(product_id)
        title = _first_text(
            value.get("title_tw"),
            value.get("title_ja"),
            value.get("title_en"),
            value.get("title"),
        )
        if title is None:
            title = display_code
        release_date = _date_text(value.get("releaseDate"))
        duration = _positive_int(value.get("length"), maximum=24 * 60)
        details = SourceDetails(
            title=title,
            release_date=release_date,
            duration_minutes=duration,
            duration_text=f"{duration} min" if duration is not None else None,
        )
        images: list[SourceImage] = []
        poster_large = _safe_fc2_image_url(value.get("posterLarge"), product_id)
        poster_small = _safe_fc2_image_url(value.get("posterSmall"), product_id)
        if poster_large:
            images.append(
                SourceImage(
                    kind="cover",
                    url=poster_large,
                    thumbnail_url=poster_small,
                )
            )
        elif poster_small:
            images.append(SourceImage(kind="cover", url=poster_small))
        sample_images = value.get("sampleLarge")
        if not isinstance(sample_images, list) or not sample_images:
            sample_images = value.get("sampleSmall")
        if not isinstance(sample_images, list):
            sample_images = ()
        for item in sample_images:
            sample = _safe_fc2_image_url(item, product_id)
            if sample:
                images.append(SourceImage(kind="sample", url=sample))
        images_tuple = normalize_source_images(images)
        metadata: dict[str, Any] = {
            "images": [image.to_dict() for image in images_tuple],
            "detail_identity_verified": False,
            "detail_provider": "avsox",
            "field_sources": {
                field_name: {
                    "source_id": self.source_id,
                    "provider": "avsox",
                    "url": self.base_url,
                }
                for field_name, field_value in details.to_dict().items()
                if field_value
            },
        }
        if images_tuple:
            metadata["cover"] = images_tuple[0].url
            metadata["field_sources"]["images"] = {
                "source_id": self.source_id, "provider": "avsox", "url": self.base_url,
            }
        description = _first_text(
            value.get("description_tw"),
            value.get("description_ja"),
            value.get("description_en"),
        )
        if description:
            metadata["description"] = description
            metadata["field_sources"]["description"] = {
                "source_id": self.source_id, "provider": "avsox", "url": self.base_url,
            }
        return SearchResult(
            source=self.source_id,
            title=title,
            url=detail_url,
            code=display_code,
            date=release_date,
            details=details,
            magnet_hint="unknown",
            metadata=metadata,
        )

    def _fetch_official_detail(
        self,
        product_id: str,
        bounds: SearchBounds,
        *,
        include_images: bool,
    ) -> _Fc2Detail:
        url = _official_detail_url(product_id)
        page = fetch_text(
            url,
            timeout=bounds.timeout_seconds,
            max_bytes=bounds.max_response_bytes,
            headers={"Referer": "https://adult.contents.fc2.com/"},
            allowed_origin="https://adult.contents.fc2.com",
        )
        return parse_fc2_official_detail(
            page,
            product_id,
            detail_url=url,
            include_images=include_images,
        )

    def _fetch_ppvdatabank_detail(
        self,
        product_id: str,
        bounds: SearchBounds,
        *,
        include_images: bool,
    ) -> _Fc2Detail:
        url = f"https://ppvdatabank.com/article/{product_id}/"
        page = fetch_text(
            url,
            timeout=bounds.timeout_seconds,
            max_bytes=bounds.max_response_bytes,
            headers={"Referer": "https://ppvdatabank.com/"},
            allowed_origin="https://ppvdatabank.com",
        )
        return parse_ppvdatabank_detail(
            page,
            product_id,
            detail_url=url,
            include_images=include_images,
        )

    def _result_from_detail(
        self,
        detail: _Fc2Detail,
        *,
        fallback: SearchResult | None,
        include_images: bool,
    ) -> SearchResult:
        display_code = f"FC2-PPV-{detail.product_id}"
        fallback_details = fallback.details if fallback is not None else SourceDetails()
        details = _merge_details(detail.details, fallback_details)
        images = list(detail.images)
        if fallback is not None:
            images.extend(_metadata_images(fallback.metadata))
        normalized_images = normalize_source_images(images)
        title = details.title or (
            fallback.title if fallback is not None else display_code
        )
        metadata = dict(fallback.metadata) if fallback is not None else {}
        primary_origin = {
            "source_id": self.source_id, "provider": detail.provider,
            "url": detail.detail_url,
        }
        fallback_origins = dict(metadata.get("field_sources") or {})
        primary_fields = detail.details.to_dict()
        field_sources = {
            field_name: dict(primary_origin) if primary_fields.get(field_name)
            else fallback_origins.get(field_name, {
                "source_id": self.source_id, "provider": "avsox", "url": self.base_url,
            })
            for field_name, field_value in details.to_dict().items() if field_value
        }
        field_sources["code"] = dict(primary_origin)
        if normalized_images:
            image_origin = dict(primary_origin) if detail.images else fallback_origins.get("images", {
                "source_id": self.source_id, "provider": "avsox", "url": self.base_url,
            })
            if detail.images and fallback is not None and _metadata_images(fallback.metadata):
                image_origin["contributors"] = [fallback_origins.get("images", {
                    "source_id": self.source_id, "provider": "avsox", "url": self.base_url,
                })]
            field_sources["images"] = image_origin
        if detail.description:
            field_sources["description"] = dict(primary_origin)
        elif metadata.get("description") and "description" in fallback_origins:
            field_sources["description"] = fallback_origins["description"]
        metadata.update(
            {
                "details_resolved": True,
                "detail_identity_verified": True,
                "detail_provider": detail.provider,
                "field_sources": field_sources,
                "magnet_checked": True,
                "images": [image.to_dict() for image in normalized_images],
            }
        )
        metadata.pop("details_error", None)
        metadata.pop("source_error", None)
        if include_images or normalized_images:
            metadata["images_resolved"] = True
            metadata.pop("images_error", None)
        cover = next(
            (image.url for image in normalized_images if image.kind == "cover"),
            None,
        )
        if cover:
            metadata["cover"] = cover
        if detail.description:
            metadata["description"] = detail.description
        return SearchResult(
            source=self.source_id,
            title=title,
            url=detail.detail_url,
            code=display_code,
            date=details.release_date,
            actors=tuple(item.label for item in details.actors),
            tags=tuple(item.label for item in details.tags),
            details=details,
            magnet_hint="unavailable",
            metadata=metadata,
        )

    def _search_endpoint(self) -> str:
        endpoint = self.search_template.replace("{base_url}", self.base_url)
        if "{" in endpoint or "}" in endpoint:
            raise FetchError("FC2 search URL template is invalid")
        try:
            parsed = urlsplit(endpoint)
            base = urlsplit(self.base_url)
        except ValueError as exc:
            raise FetchError("FC2 search URL template is invalid") from exc
        if (
            _origin(parsed) != _origin(base)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise FetchError("FC2 search URL template is outside the configured source")
        return endpoint


def fc2_product_id(value: object) -> str | None:
    canonical = canonical_catalog_code(value, max_length=40)
    match = _FC2_KEY_RE.fullmatch(canonical or "")
    return match.group(1) if match else None


def parse_fc2_official_detail(
    page: str,
    product_id: str,
    *,
    detail_url: str | None = None,
    include_images: bool = True,
) -> _Fc2Detail:
    if not isinstance(page, str) or not page:
        raise FetchError("FC2 official detail page was empty")
    expected = str(product_id)
    url = detail_url or _official_detail_url(expected)
    if _detail_product_id(url, host=_OFFICIAL_HOST) != expected:
        raise FetchError("FC2 official detail URL identity is invalid")
    soup = BeautifulSoup(page, "html.parser")
    product = _json_ld_product(soup)
    if product is None:
        raise FetchError("FC2 official detail identity is missing")
    identities: list[str] = []
    for key in ("sku", "productID", "@id"):
        if key not in product or product.get(key) in {None, ""}:
            continue
        identity = _numeric_identity(product.get(key))
        if identity is None:
            raise FetchError("FC2 official detail identity is invalid")
        identities.append(identity)
    raw_offers = product.get("offers")
    offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
    for offer in offers:
        if not isinstance(offer, dict) or offer.get("url") in {None, ""}:
            continue
        identity = _detail_product_id(str(offer.get("url")), host=_OFFICIAL_HOST)
        if identity is None:
            raise FetchError("FC2 official detail identity is invalid")
        identities.append(identity)
    if not identities or any(identity != expected for identity in identities):
        raise FetchError("FC2 official detail identity does not match")

    page_identities = _labeled_fc2_identities(soup.get_text(" ", strip=True))
    if any(identity != expected for identity in page_identities):
        raise FetchError("FC2 official page identity does not match")
    title = _clean_text(product.get("name"), 500)
    if not title:
        raise FetchError("FC2 official detail title is missing")
    description = _clean_text(product.get("description"), 8192)
    release_date = _labeled_date(soup.get_text(" ", strip=True), ("販売日", "発売日"))

    duration_text = None
    duration_minutes = None
    duration_node = soup.select_one(".items_article_MainitemThumb .items_article_info")
    if duration_node is None:
        duration_node = soup.select_one(".items_article_info")
    if duration_node is not None:
        duration_text = _clean_text(duration_node.get_text(" ", strip=True), 64)
        duration_minutes = _duration_minutes(duration_text)

    seller = None
    seller_node = soup.select_one("[data-article-seller-name]")
    if seller_node is None:
        seller_node = soup.select_one("[data-pdp-seller-profile-link]")
    if seller_node is not None:
        seller = _clean_text(seller_node.get_text(" ", strip=True), 200)
    seller_ref = (RelatedRef(kind="maker", label=seller),) if seller else ()
    publisher_ref = (RelatedRef(kind="publisher", label=seller),) if seller else ()
    tags = _related_refs(
        "tag",
        (node.get_text(" ", strip=True) for node in soup.select("[data-article-tag]")),
    )
    rating = _product_rating(product)
    details = SourceDetails(
        title=title,
        release_date=release_date,
        duration_minutes=duration_minutes,
        duration_text=duration_text,
        rating=rating,
        makers=seller_ref,
        publishers=publisher_ref,
        tags=tags,
    )

    images: list[SourceImage] = []
    cover = _product_image(product, expected)
    if cover:
        images.append(SourceImage(kind="cover", url=cover))
    if include_images:
        for anchor in soup.select('[data-feed="sample-images"] a[href]')[:_MAX_IMAGES]:
            href = anchor.get("href") if isinstance(anchor, Tag) else None
            full = urljoin(url, str(href or ""))
            image_url = _safe_fc2_image_url(full, expected)
            if not image_url:
                continue
            thumbnail = None
            image = anchor.find("img") if isinstance(anchor, Tag) else None
            if isinstance(image, Tag):
                thumbnail = _safe_fc2_image_url(
                    urljoin(url, str(image.get("src") or "")),
                    expected,
                )
            images.append(
                SourceImage(
                    kind="sample",
                    url=image_url,
                    thumbnail_url=thumbnail,
                )
            )
    return _Fc2Detail(
        product_id=expected,
        detail_url=url,
        details=details,
        images=normalize_source_images(images),
        description=description,
        provider="fc2_official",
    )


def parse_ppvdatabank_detail(
    page: str,
    product_id: str,
    *,
    detail_url: str | None = None,
    include_images: bool = True,
) -> _Fc2Detail:
    if not isinstance(page, str) or not page:
        raise FetchError("PPV DataBank detail page was empty")
    expected = str(product_id)
    url = detail_url or f"https://ppvdatabank.com/article/{expected}/"
    if _detail_product_id(url, host=_PPV_DATABANK_HOST) != expected:
        raise FetchError("PPV DataBank detail URL identity is invalid")
    soup = BeautifulSoup(page, "html.parser")
    identities: list[str] = []
    for anchor in soup.select(".article_title a[href]"):
        href = str(anchor.get("href") or "")
        try:
            query = parse_qs(urlsplit(href).query, max_num_fields=16)
        except ValueError:
            continue
        identities.extend(query.get("aid", ()))
    for field in soup.select('input[name="aid"][value]'):
        identities.append(str(field.get("value") or ""))
    clean_identities = [identity for identity in identities if identity.isdigit()]
    if not clean_identities or any(
        identity != expected for identity in clean_identities
    ):
        raise FetchError("PPV DataBank detail identity does not match")

    title_node = soup.select_one(".article_title a")
    if title_node is None:
        title_node = soup.select_one(".article_title")
    title = (
        _clean_text(title_node.get_text(" ", strip=True), 500)
        if title_node is not None
        else None
    )
    if not title:
        raise FetchError("PPV DataBank detail title is missing")
    meta_text = " ".join(
        node.get_text(" ", strip=True) for node in soup.select(".article_top .meta li")
    )
    if not meta_text:
        meta_text = soup.get_text(" ", strip=True)
    release_date = _labeled_date(meta_text, ("発売日", "販売日"))
    duration_match = re.search(
        r"(?:再生時間|収録時間|動画時間)\s*[:：]\s*([0-9:]+)",
        meta_text,
    )
    duration_text = duration_match.group(1) if duration_match else None
    duration_minutes = _duration_minutes(duration_text)
    seller = None
    for node in soup.select(".article_top .meta li"):
        text = node.get_text(" ", strip=True)
        if re.search(r"(?:販売者|販売元)\s*[:：]", text):
            anchor = node.find("a")
            seller = _clean_text(
                anchor.get_text(" ", strip=True)
                if isinstance(anchor, Tag)
                else text.split("：", 1)[-1].split(":", 1)[-1],
                200,
            )
            break
    makers = (RelatedRef(kind="maker", label=seller),) if seller else ()
    publishers = (RelatedRef(kind="publisher", label=seller),) if seller else ()
    details = SourceDetails(
        title=title,
        release_date=release_date,
        duration_minutes=duration_minutes,
        duration_text=duration_text,
        makers=makers,
        publishers=publishers,
    )

    images: list[SourceImage] = []
    cover_url = f"https://ppvdatabank.com/article/{expected}/img/thumb.webp"
    cover = _safe_fc2_image_url(cover_url, expected)
    if cover and soup.select_one(f'img[src="{cover_url}"]') is not None:
        images.append(SourceImage(kind="cover", url=cover))
    elif cover and soup.select_one('img[src$="/img/thumb.webp"]') is not None:
        images.append(SourceImage(kind="cover", url=cover))
    if include_images:
        for anchor in soup.select("a[href]"):
            href = urljoin(url, str(anchor.get("href") or ""))
            sample = _safe_fc2_image_url(href, expected)
            if not sample or "/img/" not in urlsplit(sample).path or sample == cover:
                continue
            images.append(SourceImage(kind="sample", url=sample))
            if len(images) >= _MAX_IMAGES:
                break
    description_node = soup.select_one('meta[name="description"]')
    description = (
        _clean_text(description_node.get("content"), 8192)
        if isinstance(description_node, Tag)
        else None
    )
    return _Fc2Detail(
        product_id=expected,
        detail_url=url,
        details=details,
        images=normalize_source_images(images),
        description=description,
        provider="ppvdatabank",
    )


def _json_ld_product(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in _json_ld_objects(value):
            kind = candidate.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(item).casefold() == "product" for item in kinds):
                return candidate
    return None


def _json_ld_objects(value: object) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    pending = [value]
    while pending and len(output) < 100:
        current = pending.pop()
        if isinstance(current, dict):
            output.append(current)
            graph = current.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph[:100])
        elif isinstance(current, list):
            pending.extend(current[:100])
    return tuple(output)


def _product_rating(product: dict[str, Any]) -> Rating | None:
    aggregate = product.get("aggregateRating")
    if not isinstance(aggregate, dict):
        return None
    value = _float_value(aggregate.get("ratingValue"), minimum=0.0, maximum=5.0)
    votes = _positive_int(aggregate.get("reviewCount"), maximum=10_000_000)
    if value is None and votes is None:
        return None
    return Rating(value=value, votes=votes)


def _product_image(product: dict[str, Any], product_id: str) -> str | None:
    raw = product.get("image")
    values = raw if isinstance(raw, list) else [raw]
    for value in values:
        candidate = value.get("url") if isinstance(value, dict) else value
        safe = _safe_fc2_image_url(candidate, product_id)
        if safe:
            return safe
    return None


def _safe_fc2_image_url(value: object, product_id: str) -> str | None:
    raw = str(value or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port not in {None, 443}
    ):
        return None
    host = parsed.hostname.rstrip(".").lower()
    path = parsed.path
    return raw if fc2_image_path_allowed(host, path, product_id=product_id) else None


def _metadata_images(metadata: object) -> tuple[SourceImage, ...]:
    if not isinstance(metadata, dict):
        return ()
    output: list[SourceImage] = []
    values = metadata.get("images")
    if not isinstance(values, (list, tuple)):
        return ()
    for value in values:
        if not isinstance(value, dict):
            continue
        kind = value.get("kind")
        url = _clean_text(value.get("url"), 8192)
        if kind not in {"cover", "backdrop", "sample"} or not url:
            continue
        output.append(
            SourceImage(
                kind=kind,
                url=url,
                thumbnail_url=_clean_text(value.get("thumbnail_url"), 8192),
                width=_positive_int(value.get("width"), maximum=100_000),
                height=_positive_int(value.get("height"), maximum=100_000),
            )
        )
    return tuple(output)


def _merge_details(primary: SourceDetails, fallback: SourceDetails) -> SourceDetails:
    return SourceDetails(
        title=primary.title or fallback.title,
        original_title=primary.original_title or fallback.original_title,
        release_date=primary.release_date or fallback.release_date,
        duration_minutes=primary.duration_minutes or fallback.duration_minutes,
        duration_text=primary.duration_text or fallback.duration_text,
        rating=primary.rating or fallback.rating,
        makers=primary.makers or fallback.makers,
        publishers=primary.publishers or fallback.publishers,
        series=primary.series or fallback.series,
        directors=primary.directors or fallback.directors,
        actors=primary.actors or fallback.actors,
        tags=primary.tags or fallback.tags,
    )


def _related_refs(kind: str, values: Any) -> tuple[RelatedRef, ...]:
    output: list[RelatedRef] = []
    seen: set[str] = set()
    for value in values:
        label = _clean_text(value, 200)
        if not label or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        output.append(RelatedRef(kind=kind, label=label))  # type: ignore[arg-type]
        if len(output) >= 100:
            break
    return tuple(output)


def _official_detail_url(product_id: str) -> str:
    return f"https://adult.contents.fc2.com/article/{product_id}/?lang=ja"


def _detail_product_id(value: str, *, host: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").rstrip(".").lower() != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        return None
    match = re.fullmatch(r"/article/(\d{2,9})/?", parsed.path)
    return match.group(1) if match else None


def _numeric_identity(value: object) -> str | None:
    match = re.search(r"(?<!\d)(\d{2,9})(?!\d)", str(value or ""))
    return match.group(1) if match else None


def _detail_source_error(*errors: Exception | None) -> str:
    messages = tuple(str(error) for error in errors if error is not None)
    if any(
        re.search(r"(?:timed?\s*out|timeout)", value, re.IGNORECASE)
        for value in messages
    ):
        return "FC2 detail upstream timed out"
    for value in messages:
        status = re.search(r"\bHTTP\s+(\d{3})\b", value, re.IGNORECASE)
        if status and (status.group(1) == "429" or status.group(1).startswith("5")):
            return f"FC2 detail upstream HTTP {status.group(1)}"
    return "FC2 detail sources are unavailable"


def _labeled_fc2_identities(value: str) -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for match in re.finditer(
            r"(?:商品ID|商品番号)\s*[:：]\s*"
            r"FC2[-_. ]*(?:PPV[-_. ]*)?(\d{2,9})",
            value,
            flags=re.IGNORECASE,
        )
    )


def _labeled_date(value: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:：]\s*(\d{{4}}[./-]\d{{1,2}}[./-]\d{{1,2}})",
        value,
    )
    return _date_text(match.group(1)) if match else None


def _date_text(value: object) -> str | None:
    match = _DATE_RE.search(str(value or ""))
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed.isoformat()


def _duration_minutes(value: object) -> int | None:
    text = str(value or "").strip()
    match = _DURATION_RE.fullmatch(text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        if minutes >= 60 and match.group(1) is not None or seconds >= 60:
            return None
        total = hours * 60 + minutes
        return total if 0 < total <= 24 * 60 else None
    number = re.fullmatch(r"(\d{1,4})(?:\s*(?:分|min(?:utes?)?))?", text, re.IGNORECASE)
    if number:
        return _positive_int(number.group(1), maximum=24 * 60)
    return None


def _clean_text(value: object, limit: int) -> str | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    clean = " ".join(text.replace("\x00", " ").split())
    return clean[:limit] if clean else None


def _first_text(*values: object) -> str | None:
    for value in values:
        clean = _clean_text(value, 500)
        if clean:
            return clean
    return None


def _positive_int(value: object, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= maximum else None


def _float_value(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _origin(parsed: Any) -> tuple[str, str, int]:
    scheme = str(parsed.scheme or "").lower()
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


__all__ = [
    "Fc2Indexer",
    "fc2_product_id",
    "parse_fc2_official_detail",
    "parse_ppvdatabank_detail",
]
