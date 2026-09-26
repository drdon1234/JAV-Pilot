from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

from ..config.app_config import AppConfig
from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..search.cover_proxy import fetch_cover
from ..torrent.qbittorrent import QbittorrentClient
from ..media_metadata.manager import MediaMetadataConfig
from ..core.models import SearchBounds, WorkResult
from ..search.engine import default_indexers, search
from ..config.settings import DEFAULT_SETTINGS, MISSAV_SITE_ID, SITE_DIAGNOSTIC_SITE_IDS, site_by_id
from .diagnostics import FunctionalProbeAdapter, SiteDiagnosticError
from .probe_adapters import build_site_probe_adapters
from .smoke import (
    JavBusSmokeAdapter,
    JavDbSmokeAdapter,
    MAX_SMOKE_MANIFEST_BYTES,
    MAX_SMOKE_MEDIA_SAMPLE_BYTES,
    MissavSmokeAdapter,
    MissavSmokeObservation,
    SearchSiteObservation,
    SmokeAdapter,
    SmokeCheck,
    SmokeHttpResponse,
    SMOKE_ERROR_CODES,
    SmokeStateSnapshot,
)
from ..web_download.media import (
    SafeMediaError,
    SafeMediaResource,
    fetch_safe_media_resource,
    validate_native_hls_manifest,
)
from ..web_download.providers import probe_exact_web_download_provider
from ..web_download.config import WebDownloadConfig


_VIDEO_SUFFIXES = frozenset(
    {".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm", ".wmv"}
)
_TASK_TABLES = {
    "web": "web_download_jobs",
    "batch": "web_download_batches",
    "metadata": "jobs",
}


class _BufferedResponse(SmokeHttpResponse):
    def __init__(
        self,
        body: bytes,
        *,
        status: int,
        headers: Mapping[str, str],
        url: str,
    ) -> None:
        self.status = status
        self.headers = dict(headers)
        self.url = url
        self._body = BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self._body.close()


class _RetryAdapter:
    def __init__(self, adapter: SmokeAdapter, *, attempts: int = 2) -> None:
        self.site_id = adapter.site_id
        self._adapter = adapter
        self._attempts = attempts

    def run(self) -> Sequence[SmokeCheck]:
        result: Sequence[SmokeCheck] = ()
        for _attempt in range(self._attempts):
            result = self._adapter.run()
            if result and all(check.ok for check in result):
                break
        return result


class _DiagnosticWebSmokeAdapter:
    def __init__(
        self,
        site_id: str,
        code: str,
        probe: FunctionalProbeAdapter,
    ) -> None:
        self.site_id = site_id
        self._code = code
        self._probe = probe

    def run(self) -> Sequence[SmokeCheck]:
        checks: list[SmokeCheck] = []
        for stage, check_name in (
            ("search", "exact_code"),
            ("detail", "detail"),
            ("manifest", "manifest"),
        ):
            started = time.monotonic()
            try:
                self._probe.probe(stage)
            except SiteDiagnosticError as exc:
                error_code = (
                    exc.error_code
                    if exc.error_code in SMOKE_ERROR_CODES
                    else "dependency_unavailable"
                )
                checks.append(
                    SmokeCheck(
                        site=self.site_id,
                        check=check_name,
                        ok=False,
                        latency=_smoke_latency(started),
                        error_code=error_code,
                    )
                )
                break
            except Exception:  # noqa: BLE001 - smoke output is structure-only.
                checks.append(
                    SmokeCheck(
                        site=self.site_id,
                        check=check_name,
                        ok=False,
                        latency=_smoke_latency(started),
                        error_code="internal_error",
                    )
                )
                break
            checks.append(
                SmokeCheck(
                    site=self.site_id,
                    check=check_name,
                    ok=True,
                    latency=_smoke_latency(started),
                    code=self._code if check_name == "exact_code" else None,
                )
            )
        return tuple(checks)


def build_production_smoke_adapters(
    settings: dict[str, object],
    code: object,
    *,
    sites: Sequence[str] = SITE_DIAGNOSTIC_SITE_IDS,
    retry_once: bool = True,
    media_fetch: Callable[..., SafeMediaResource] = fetch_safe_media_resource,
) -> tuple[SmokeAdapter, ...]:
    normalized = normalize_catalog_code(code, max_length=40)
    if normalized is None:
        raise ValueError("site smoke catalog code is invalid")
    display_code, _code_key = normalized
    selected = tuple(str(site or "").strip().lower() for site in sites)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected).issubset(SITE_DIAGNOSTIC_SITE_IDS)
    ):
        raise ValueError("site smoke sites are invalid")

    diagnostic_adapters = {
        adapter.site_id: adapter
        for adapter in build_site_probe_adapters(settings, display_code)
        if adapter.site_id in {"jable", "supjav"}
    }
    adapters: list[SmokeAdapter] = []
    for site in selected:
        if site in {"javbus", "javdb"}:
            adapter_type = JavBusSmokeAdapter if site == "javbus" else JavDbSmokeAdapter
            adapter: SmokeAdapter = adapter_type(
                display_code,
                lambda site=site: _search_observation(
                    site,
                    display_code,
                    settings,
                ),
                # fetch_cover performs both initial and final URL policy checks.
                image_url_validator=lambda _url: None,
            )
        elif site in {"jable", "supjav"}:
            adapter = _DiagnosticWebSmokeAdapter(
                site,
                display_code,
                diagnostic_adapters[site],
            )
        else:
            configured_site = site_by_id(MISSAV_SITE_ID, settings)
            default_site = site_by_id(MISSAV_SITE_ID, DEFAULT_SETTINGS)
            base_url = str(
                (configured_site or default_site or {}).get("base_url") or ""
            )
            adapter = MissavSmokeAdapter(
                display_code,
                lambda: _missav_observation(
                    display_code,
                    base_url=base_url,
                    media_fetch=media_fetch,
                ),
            )
        adapters.append(_RetryAdapter(adapter) if retry_once else adapter)
    return tuple(adapters)


def _smoke_latency(started: float) -> int:
    return min(180_000, max(0, int((time.monotonic() - started) * 1000)))


def production_smoke_snapshot() -> SmokeStateSnapshot:
    web_config = WebDownloadConfig.from_env()
    metadata_config = MediaMetadataConfig.from_env()
    web_database = Path(web_config.database_path)
    metadata_database = metadata_config.database_path
    task_counts = {
        "web": _sqlite_count(web_database, _TASK_TABLES["web"]),
        "batch": _sqlite_count(web_database, _TASK_TABLES["batch"]),
        "metadata": _sqlite_count(metadata_database, _TASK_TABLES["metadata"]),
        "bt": _qb_task_count(),
    }
    library_path = (
        metadata_config.library_path
        if metadata_config.enabled
        else Path(web_config.library_path)
    )
    return SmokeStateSnapshot.from_counts(
        task_counts,
        media_files=_media_file_count(library_path),
    )


def _search_observation(
    site: str,
    code: str,
    settings: dict[str, object],
) -> SearchSiteObservation:
    response = search(
        code,
        sources=(site,),
        bounds=SearchBounds(
            limit=5,
            page=1,
            max_pages=1,
            fetch_magnets=True,
            detail_limit=5,
            match="exact",
        ),
        indexers=default_indexers(settings),
    )
    if response.errors:
        error = response.errors.get(site) or next(iter(response.errors.values()))
        raise ValueError(str(error or "site smoke upstream search failed"))
    expected_key = canonical_catalog_code(code, max_length=40)
    work = next(
        (item for item in response.results if item.canonical_code == expected_key),
        None,
    )
    if work is None:
        raise ValueError("site smoke exact code was not found")
    source = next((item for item in work.sources if item.source_id == site), None)
    if source is None or source.parse_status != "resolved":
        raise ValueError("site smoke detail parsing failed")
    cover = next((image for image in source.images if image.kind == "cover"), None)
    if cover is None:
        raise ValueError("site smoke image parsing failed")
    detail_field_count = _detail_field_count(work)

    def fetch_image() -> SmokeHttpResponse:
        image = fetch_cover(
            site,
            cover.url,
            settings=settings,
            timeout=20.0,
            max_bytes=5 * 1024 * 1024,
        )
        return _BufferedResponse(
            image.body,
            status=200,
            headers={
                "Content-Type": image.content_type,
                "Content-Length": str(len(image.body)),
            },
            url=image.source_url,
        )

    return SearchSiteObservation(
        code=work.code or code,
        detail_field_count=detail_field_count,
        image_url=cover.url,
        image_fetch=fetch_image,
    )


def _missav_observation(
    code: str,
    *,
    base_url: str,
    media_fetch: Callable[..., SafeMediaResource],
) -> MissavSmokeObservation:
    manifest = probe_exact_web_download_provider(
        MISSAV_SITE_ID,
        code,
        base_url,
        timeout_seconds=45.0,
    )
    heights = (
        (manifest.selected_height,)
        if isinstance(manifest.selected_height, int) and manifest.selected_height > 0
        else ()
    )
    manifest_resource = media_fetch(
        manifest.url,
        manifest.headers,
        max_bytes=MAX_SMOKE_MANIFEST_BYTES,
        timeout_seconds=20.0,
    )
    if manifest_resource.status != 200:
        raise SafeMediaError("media manifest request failed")
    try:
        manifest_text = manifest_resource.body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SafeMediaError("media manifest is not valid UTF-8") from None
    playlist = validate_native_hls_manifest(manifest_text, manifest_resource.url)
    if not playlist.segments:
        raise SafeMediaError("media manifest has no segments")
    sample_url = urljoin(manifest_resource.url, playlist.segments[0].uri)

    def fetch_manifest() -> SmokeHttpResponse:
        return _resource_response(manifest_resource)

    def fetch_sample(headers: Mapping[str, str]) -> SmokeHttpResponse:
        range_header = next(
            (value for name, value in headers.items() if name.casefold() == "range"),
            None,
        )
        resource = media_fetch(
            sample_url,
            manifest.headers,
            max_bytes=MAX_SMOKE_MEDIA_SAMPLE_BYTES,
            timeout_seconds=20.0,
            range_header=range_header,
        )
        return _resource_response(resource)

    return MissavSmokeObservation(
        code=code,
        heights=heights,
        manifest_url=manifest_resource.url,
        sample_url=sample_url,
        manifest_fetch=fetch_manifest,
        sample_fetch=fetch_sample,
    )


def _resource_response(resource: SafeMediaResource) -> SmokeHttpResponse:
    return _BufferedResponse(
        resource.body,
        status=resource.status,
        headers=resource.headers,
        url=resource.url,
    )


def _detail_field_count(work: WorkResult) -> int:
    source_fields = [
        value
        for source in work.sources
        for value in source.details.to_dict().values()
        if value
    ]
    return sum(
        bool(value)
        for value in (
            work.title,
            work.release_date,
            work.actors,
            work.tags,
            *source_fields,
        )
    )


def _sqlite_count(path: Path, table: str) -> int:
    if table not in _TASK_TABLES.values():
        raise ValueError("site smoke task table is invalid")
    if not path.exists():
        return 0
    connection = sqlite3.connect(
        f"file:{path.resolve(strict=True).as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], int):
        raise sqlite3.DatabaseError("site smoke task count is unavailable")
    return row[0]


def _qb_task_count() -> int:
    config = AppConfig.from_env().qbittorrent
    if not config.configured:
        return 0
    return len(
        QbittorrentClient(config).list_torrent_history(
            category=config.category,
            max_tasks=50_000,
        )
    )


def _media_file_count(root: Path) -> int:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise OSError("site smoke media root is unavailable")
    count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif (
                    entry.is_file(follow_symlinks=False)
                    and Path(entry.name).suffix.casefold() in _VIDEO_SUFFIXES
                ):
                    count += 1
    return count


__all__ = [
    "build_production_smoke_adapters",
    "production_smoke_snapshot",
]
