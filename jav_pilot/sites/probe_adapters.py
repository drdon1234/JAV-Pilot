from __future__ import annotations

from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from ..core.catalog_code import canonical_catalog_code
from ..search.cover_proxy import (
    CoverProxyError,
    CoverRequestError,
    CoverUpstreamError,
    fetch_cover,
)
from ..net.http_client import FetchError, fetch_text
from ..core.models import SearchBounds, SearchResult
from ..net.network_guard import PublicHostResolver
from ..config.settings import site_by_id
from ..config.source_catalog import METADATA_CATALOG, WEB_CATALOG
from ..search.engine import default_indexers
from .diagnostic_codes import SiteDiagnosticCodes
from .diagnostics import FunctionalProbeAdapter, SiteDiagnosticError
from ..web_download.providers import (
    ProviderManifest,
    ProviderSearchPage,
    WebDownloadProviderError,
    WebDownloadProviderNotFound,
    probe_exact_web_download_provider,
    probe_jable_current_search,
    probe_web_download_provider_connection,
    probe_web_download_provider_search,
)


class _ConfiguredProbe:
    def __init__(
        self, site_id: str, code: str | None, settings: dict[str, object]
    ) -> None:
        self.site_id = site_id
        # Without an acceptance code only configuration, DNS and connection
        # stages are built, so code-dependent stages never read this.
        self.code = code or ""
        self.code_key = canonical_catalog_code(code, max_length=40) if code else None
        if code and self.code_key is None:
            raise ValueError("site diagnostic catalog code is invalid")
        self.settings = settings
        self.site = site_by_id(site_id, settings)
        self.base_url = str((self.site or {}).get("base_url") or "")

    def configuration(self) -> None:
        if (
            self.site is None
            or not self.site.get("enabled")
            or self.site.get("parser_profile") != self.site_id
        ):
            raise SiteDiagnosticError("invalid_config")
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except ValueError as exc:
            raise SiteDiagnosticError("invalid_config") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65535
        ):
            raise SiteDiagnosticError("invalid_config")

    def dns(self) -> None:
        self.configuration()
        hostname = urlsplit(self.base_url).hostname or ""
        if not PublicHostResolver(max_hosts=1).is_public(hostname):
            raise SiteDiagnosticError("dns_failed")

    def connection(self) -> None:
        self.dns()
        try:
            fetch_text(
                self.base_url,
                timeout=10.0,
                max_bytes=512 * 1024,
                allowed_origin=self.base_url,
            )
        except FetchError as exc:
            raise SiteDiagnosticError(_upstream_error_code(str(exc))) from exc


class _SearchSiteProbe(_ConfiguredProbe):
    def __init__(
        self, site_id: str, code: str | None, settings: dict[str, object]
    ) -> None:
        super().__init__(site_id, code, settings)
        self._result: SearchResult | None = None

    def stage(self, stage: str) -> None:
        if stage in {"configuration", "dns", "connection"}:
            getattr(self, stage)()
            return
        result = self._exact_result()
        if stage == "search":
            return
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        if stage == "detail":
            details = result.details.to_dict()
            if (
                metadata.get("details_resolved") is not True
                or bool(metadata.get("details_error"))
                or not result.title
                or not any(value for value in details.values())
            ):
                raise SiteDiagnosticError("parse_drift")
            return
        if stage == "image":
            if metadata.get("images_resolved") is not True or bool(
                metadata.get("images_error")
            ):
                raise SiteDiagnosticError("parse_drift")
            cover_url = _diagnostic_cover_url(metadata)
            if cover_url is None:
                raise SiteDiagnosticError("parse_drift")
            try:
                image = fetch_cover(
                    self.site_id,
                    cover_url,
                    settings=self.settings,
                    timeout=10.0,
                    max_bytes=5 * 1024 * 1024,
                )
                with Image.open(BytesIO(image.body)) as decoded:
                    decoded.verify()
            except CoverRequestError as exc:
                raise SiteDiagnosticError("image_host_rejected") from exc
            except CoverUpstreamError as exc:
                raise SiteDiagnosticError(_cover_error_code(str(exc))) from exc
            except CoverProxyError as exc:
                raise SiteDiagnosticError("image_invalid") from exc
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise SiteDiagnosticError("image_invalid") from exc
            return
        raise SiteDiagnosticError("invalid_config")

    def _exact_result(self) -> SearchResult:
        if self._result is not None:
            return self._result
        registry = default_indexers(self.settings)
        indexer = registry.get(self.site_id)
        if indexer is None:
            raise SiteDiagnosticError("invalid_config")
        try:
            results = indexer.diagnostic_detail_search(
                self.code,
                SearchBounds(
                    limit=5,
                    page=1,
                    max_pages=1,
                    # Detail enrichment provides the component metadata used by
                    # the stage-specific checks below.
                    fetch_magnets=True,
                    detail_limit=5,
                    match="exact",
                ),
            )
        except FetchError as exc:
            raise SiteDiagnosticError(_upstream_error_code(str(exc))) from exc
        for result in results:
            if (
                isinstance(result, SearchResult)
                and result.source == self.site_id
                and canonical_catalog_code(result.code, max_length=40) == self.code_key
            ):
                self._result = result
                return result
        raise SiteDiagnosticError("code_mismatch")


class _WebDownloadSiteProbe(_ConfiguredProbe):
    def __init__(
        self, site_id: str, code: str | None, settings: dict[str, object]
    ) -> None:
        super().__init__(site_id, code, settings)
        self._manifest: ProviderManifest | None = None
        self._manifest_error_code: str | None = None
        self._search_page: ProviderSearchPage | None = None
        self._search_fetch_error_code: str | None = None
        self._search_checked = False
        self._search_error_code: str | None = None

    def stage(self, stage: str) -> None:
        if stage in {"configuration", "dns"}:
            getattr(self, stage)()
            return
        if stage == "connection":
            self.dns()
            self._captured_search_page(timeout_seconds=15.0)
            return
        if stage == "search":
            self._validate_search_page()
            return
        if stage in {"detail", "quality", "manifest"}:
            manifest = self._exact_manifest()
            if stage == "quality" and (
                manifest.selected_height is None or manifest.selected_height <= 0
            ):
                raise SiteDiagnosticError("parse_drift")
            return
        raise SiteDiagnosticError("invalid_config")

    def _captured_search_page(self, *, timeout_seconds: float) -> ProviderSearchPage:
        if self._search_page is not None:
            return self._search_page
        if self._search_fetch_error_code is not None:
            raise SiteDiagnosticError(self._search_fetch_error_code)
        try:
            page = probe_web_download_provider_connection(
                self.site_id,
                self.code,
                self.base_url,
                timeout_seconds=timeout_seconds,
            )
        except WebDownloadProviderError as exc:
            self._search_fetch_error_code = _web_provider_error_code(exc)
            raise SiteDiagnosticError(self._search_fetch_error_code) from exc
        except Exception as exc:  # noqa: BLE001 - diagnostics expose only codes.
            self._search_fetch_error_code = "internal_error"
            raise SiteDiagnosticError("internal_error") from exc
        if (
            not isinstance(page, ProviderSearchPage)
            or page.provider != self.site_id
            or canonical_catalog_code(page.query, max_length=40) != self.code_key
        ):
            self._search_fetch_error_code = "parse_drift"
            raise SiteDiagnosticError("parse_drift")
        self._search_page = page
        return page

    def _validate_search_page(self) -> None:
        if self._search_checked:
            if self._search_error_code is not None:
                raise SiteDiagnosticError(self._search_error_code)
            return
        page = self._captured_search_page(timeout_seconds=30.0)
        try:
            try:
                probe_web_download_provider_search(page)
            except WebDownloadProviderNotFound:
                if self.site_id != "jable":
                    raise
                # A removed title is not a site outage. Choose one current
                # same-origin catalog sample, then retain it for every stage
                # in this probe. Real challenges and parser errors still fail.
                current = probe_jable_current_search(
                    self.base_url, timeout_seconds=15.0
                )
                code_key = canonical_catalog_code(current.query, max_length=40)
                if current.provider != self.site_id or code_key is None:
                    raise WebDownloadProviderError(
                        "Diagnostic sample is invalid", code="response_invalid"
                    )
                self.code = current.query
                self.code_key = code_key
                self._search_page = current
                probe_web_download_provider_search(current)
        except WebDownloadProviderNotFound as exc:
            self._search_error_code = "code_mismatch"
            self._search_checked = True
            raise SiteDiagnosticError("code_mismatch") from exc
        except WebDownloadProviderError as exc:
            self._search_error_code = _web_provider_error_code(exc)
            self._search_checked = True
            raise SiteDiagnosticError(self._search_error_code) from exc
        except Exception as exc:  # noqa: BLE001 - diagnostics expose only codes.
            self._search_error_code = "internal_error"
            self._search_checked = True
            raise SiteDiagnosticError("internal_error") from exc
        self._search_checked = True

    def _exact_manifest(self) -> ProviderManifest:
        if self._manifest is not None:
            return self._manifest
        if self._manifest_error_code is not None:
            raise SiteDiagnosticError(self._manifest_error_code)
        if self.site_id == "jable":
            self._validate_search_page()
        try:
            manifest = probe_exact_web_download_provider(
                self.site_id,
                self.code,
                self.base_url,
                timeout_seconds=45.0,
            )
        except WebDownloadProviderNotFound as exc:
            self._manifest_error_code = "code_mismatch"
            raise SiteDiagnosticError("code_mismatch") from exc
        except WebDownloadProviderError as exc:
            self._manifest_error_code = _web_provider_error_code(exc)
            raise SiteDiagnosticError(self._manifest_error_code) from exc
        except Exception as exc:  # noqa: BLE001 - diagnostics expose only codes.
            self._manifest_error_code = "internal_error"
            raise SiteDiagnosticError("internal_error") from exc
        if manifest.provider != self.site_id or not manifest.url or not manifest.page_url:
            self._manifest_error_code = "parse_drift"
            raise SiteDiagnosticError("parse_drift")
        self._manifest = manifest
        return manifest


def build_site_probe_adapters(
    settings: dict[str, object],
    codes: SiteDiagnosticCodes | str | None,
) -> tuple[FunctionalProbeAdapter, ...]:
    """Probe adapters for every configured site.

    ``codes`` supplies a JAV and an FC2 acceptance code; each site uses the one
    matching its catalogue. A site without a matching code is checked only as
    far as its connection, because every later stage needs a real work.
    """

    if not isinstance(codes, SiteDiagnosticCodes):
        codes = SiteDiagnosticCodes(jav=codes or None)
    adapters: list[FunctionalProbeAdapter] = []
    for site_id in ("javbus", "javdb", "fc2", *METADATA_CATALOG):
        if site_by_id(site_id, settings) is None:
            continue
        code = codes.for_site(site_id)
        probe = _SearchSiteProbe(site_id, code, settings)
        adapters.append(
            FunctionalProbeAdapter(
                site_id,
                ("configuration", "dns", "connection", "search", "detail", "image")
                if code
                else ("configuration", "dns", "connection"),
                probe.stage,
            )
        )
    for site_id in ("jable", "supjav", "missav"):
        code = codes.for_site(site_id)
        probe = _WebDownloadSiteProbe(site_id, code, settings)
        if not code:
            stages: tuple[str, ...] = ("configuration", "dns")
        elif site_id == "missav":
            stages = ("configuration", "dns", "connection", "quality", "manifest")
        else:
            stages = (
                "configuration",
                "dns",
                "connection",
                "search",
                "detail",
                "manifest",
            )
        adapters.append(
            FunctionalProbeAdapter(
                site_id,
                stages,
                probe.stage,
            )
        )
    for site_id in WEB_CATALOG:
        if site_by_id(site_id, settings) is None:
            continue
        code = codes.for_site(site_id)
        probe = _WebDownloadSiteProbe(site_id, code, settings)
        adapters.append(FunctionalProbeAdapter(
            site_id,
            ("configuration", "dns", "connection", "search") if code else ("configuration", "dns"),
            probe.stage,
        ))
    return tuple(adapters)


def _web_provider_error_code(error: WebDownloadProviderError) -> str:
    return {
        "configuration": "invalid_config",
        "host_policy": "media_host_rejected",
        "redirect_policy": "redirect_rejected",
        "response_invalid": "parse_drift",
        "manifest_invalid": "manifest_invalid",
        "not_found": "code_mismatch",
        "challenge_active": "challenge_detected",
        "rate_limited": "timeout",
        "timeout": "timeout",
        "upstream_unavailable": "upstream_http",
        "all_providers_unavailable": "upstream_http",
        "cancelled": "dependency_unavailable",
    }.get(error.code, "internal_error")


def _upstream_error_code(message: str) -> str:
    lowered = str(message or "").casefold()
    if "redirect" in lowered or "outside the configured source" in lowered:
        return "redirect_rejected"
    if "exceeded" in lowered or "too large" in lowered:
        return "response_too_large"
    if any(marker in lowered for marker in ("tls", "ssl", "certificate")):
        return "tls_failed"
    if "unavailable from the current network region" in lowered:
        return "upstream_http"
    if any(
        marker in lowered
        for marker in (
            "challenge",
            "captcha",
            "cloudflare",
            "temporarily blocked this client",
        )
    ):
        return "challenge_detected"
    if "http" in lowered:
        return "upstream_http"
    return "connection_failed"


def _cover_error_code(message: str) -> str:
    lowered = str(message or "").casefold()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "exceeded" in lowered or "too large" in lowered:
        return "response_too_large"
    if "http" in lowered:
        return "upstream_http"
    if any(
        marker in lowered
        for marker in (
            "redirect",
            "rejected",
            "outside",
            "public address",
            "configuration",
        )
    ):
        return "image_host_rejected"
    if any(
        marker in lowered
        for marker in ("raster image", "content-length", "did not match")
    ):
        return "image_invalid"
    return "connection_failed"


def _diagnostic_cover_url(metadata: dict[str, object]) -> str | None:
    images = metadata.get("images")
    if isinstance(images, (list, tuple)):
        for image in images:
            if not isinstance(image, dict) or image.get("kind") != "cover":
                continue
            url = image.get("url")
            if isinstance(url, str) and url.strip():
                return url
    cover = metadata.get("cover")
    return cover if isinstance(cover, str) and cover.strip() else None


__all__ = ["build_site_probe_adapters"]
