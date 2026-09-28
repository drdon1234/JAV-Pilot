"""Public MissAV operations: discovery, descriptions, qualities and manifest capture."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..web_download.quality import QualityHeightError, validate_quality_height
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
)
from .automation import (
    capture_manifest_on_prepared_page,
    capture_with_playwright,
    discover_series_with_playwright,
    discover_with_playwright,
    fetch_description_with_playwright,
)
from .errors import MissavError, MissavTransientError
from .models import (
    ManifestRequest,
    MissavSeriesDiscovery,
)
from .navigation import is_browser_target_closed, is_browser_transport_reset
from .parsing import (
    optional_bounded_integer,
    required_bounded_integer,
    validated_code,
    validated_series_prefix,
)
from .routing import BrowserMediaGate
from .site import (
    MAX_SERIES_CANDIDATES,
    QualityStrategy,
    uses_configured_missav_description_site,
    uses_configured_missav_resource_site,
    uses_configured_missav_site,
    validate_quality_strategy,
)

__all__ = [
    "capture_manifest",
    "capture_manifest_on_page",
    "discover_qualities",
    "discover_series_codes",
    "fetch_description",
    "validated_series_request",
]

_MAX_SERIES_BOUND = 999_999_999


@dataclass(frozen=True, slots=True)
class SeriesRequest:
    display_prefix: str
    canonical_prefix: str
    suffix_width: int | None
    start: int | None
    end: int | None
    max_codes: int
    timeout_seconds: float


def validated_timeout(value: object, *, operation: str) -> float:
    try:
        clean = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise MissavError(f"MissAV {operation} timeout must be a number") from exc
    if not math.isfinite(clean) or not 1.0 <= clean <= 180.0:
        raise MissavError(
            f"MissAV {operation} timeout must be between 1 and 180 seconds"
        )
    return clean


def validated_variant(value: object) -> MissavVariant:
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise MissavError("MissAV variant is invalid") from exc


def _validated_profile_parent(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    profile_root = Path(value)
    if (
        not profile_root.is_absolute()
        or not profile_root.is_dir()
        or profile_root.is_symlink()
    ):
        raise MissavError("MissAV browser profile parent must be a regular directory")
    return profile_root.resolve(strict=True)


def _validated_quality(
    requested_height: object | None,
    quality_strategy: object | None,
) -> tuple[int | None, QualityStrategy]:
    if requested_height is None:
        clean_height = None
    else:
        try:
            clean_height = validate_quality_height(requested_height)
        except QualityHeightError as exc:
            raise MissavError("MissAV requested quality is invalid") from exc
    return clean_height, validate_quality_strategy(
        quality_strategy,
        requested_height=clean_height,
    )


def validated_series_request(
    prefix: object,
    *,
    suffix_width: object | None,
    start: object | None,
    end: object | None,
    max_codes: object,
    timeout_seconds: object,
) -> SeriesRequest:
    display_prefix, canonical_prefix = validated_series_prefix(prefix)
    clean_width = optional_bounded_integer(
        suffix_width,
        name="suffix width",
        minimum=1,
        maximum=9,
    )
    clean_start = optional_bounded_integer(
        start,
        name="series start",
        minimum=0,
        maximum=_MAX_SERIES_BOUND,
    )
    clean_end = optional_bounded_integer(
        end,
        name="series end",
        minimum=0,
        maximum=_MAX_SERIES_BOUND,
    )
    if clean_start is not None and clean_end is not None and clean_start > clean_end:
        raise MissavError("MissAV series range is invalid")
    return SeriesRequest(
        display_prefix=display_prefix,
        canonical_prefix=canonical_prefix,
        suffix_width=clean_width,
        start=clean_start,
        end=clean_end,
        max_codes=required_bounded_integer(
            max_codes,
            name="series result limit",
            minimum=1,
            maximum=MAX_SERIES_CANDIDATES,
        ),
        timeout_seconds=validated_timeout(timeout_seconds, operation="series"),
    )


@uses_configured_missav_resource_site
def discover_series_codes(
    prefix: object,
    *,
    suffix_width: object | None = None,
    start: object | None = None,
    end: object | None = None,
    max_codes: object = 65,
    timeout_seconds: float = 120.0,
    cancel_event: threading.Event | None = None,
) -> MissavSeriesDiscovery:
    """Discover bounded exact-code results for a numeric MissAV catalog prefix."""

    request = validated_series_request(
        prefix,
        suffix_width=suffix_width,
        start=start,
        end=end,
        max_codes=max_codes,
        timeout_seconds=timeout_seconds,
    )
    cancelled = cancel_event or threading.Event()
    if cancelled.is_set():
        return MissavSeriesDiscovery(codes=(), complete=False)
    return discover_series_with_playwright(
        request.display_prefix,
        request.canonical_prefix,
        suffix_width=request.suffix_width,
        start=request.start,
        end=request.end,
        max_codes=request.max_codes,
        timeout_seconds=request.timeout_seconds,
        cancel_event=cancelled,
    )


@uses_configured_missav_description_site
def fetch_description(
    code: object,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    timeout_seconds: float = 30.0,
    profile_parent: Path | str | None = None,
) -> str | None:
    """Fetch only the optional description for an exact MissAV work."""

    search_code, canonical_code = validated_code(code)
    clean_variant = validated_variant(variant)
    clean_timeout = validated_timeout(timeout_seconds, operation="description")
    return fetch_description_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=_validated_profile_parent(profile_parent),
        variant=clean_variant,
    )


@uses_configured_missav_site
def capture_manifest(
    code: object,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    timeout_seconds: float = 45.0,
    profile_parent: Path | str | None = None,
    requested_height: object | None = None,
    quality_strategy: object | None = None,
) -> ManifestRequest:
    """Capture an in-memory HLS request for one exact work variant."""

    search_code, canonical_code = validated_code(code)
    clean_variant = validated_variant(variant)
    clean_timeout = validated_timeout(timeout_seconds, operation="capture")
    clean_height, clean_strategy = _validated_quality(
        requested_height, quality_strategy
    )
    return capture_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=_validated_profile_parent(profile_parent),
        variant=clean_variant,
        requested_height=clean_height,
        quality_strategy=clean_strategy,
    )


def capture_manifest_on_page(
    page: object,
    code: object,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    timeout_seconds: float = 45.0,
    requested_height: object | None = None,
    quality_strategy: object | None = None,
    cancel_event: threading.Event | None = None,
    media_gate: BrowserMediaGate | None = None,
    stage_callback: Callable[[str | None], None] | None = None,
) -> ManifestRequest:
    """Capture a manifest on a caller-owned trusted page.

    The public capture API owns a short-lived Playwright context.  The broker
    calls this lower-level entry point so all MissAV operations share one
    persistent browser context and one page.  The returned request is kept in
    memory by the caller and is never serialized by this function.
    """

    search_code, canonical_code = validated_code(code)
    clean_variant = validated_variant(variant)
    clean_timeout = validated_timeout(timeout_seconds, operation="capture")
    clean_height, clean_strategy = _validated_quality(
        requested_height, quality_strategy
    )
    deadline = time.monotonic() + clean_timeout
    # The trusted page lets the player load its manifests (the Plyr quality
    # menu is built from them); player-source probing re-blocks media itself.
    gate = media_gate or BrowserMediaGate()
    gate.block_surrit_requests = False
    try:
        return capture_manifest_on_prepared_page(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=clean_variant,
            quality_strategy=clean_strategy,
            requested_height=clean_height,
            media_gate=gate,
            cancel_event=cancel_event,
            stage_callback=stage_callback,
        )
    except MissavError:
        raise
    except Exception as exc:
        code = (
            "transport_reset"
            if is_browser_transport_reset(exc) or is_browser_target_closed(exc)
            else "upstream_unavailable"
        )
        raise MissavTransientError(
            "MissAV browser capture failed",
            code=code,
        ) from None


@uses_configured_missav_site
def discover_qualities(
    code: object,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    timeout_seconds: float = 45.0,
    profile_parent: Path | str | None = None,
) -> tuple[int, ...]:
    """Return numeric qualities exposed by one exact work variant."""

    search_code, canonical_code = validated_code(code)
    clean_variant = validated_variant(variant)
    clean_timeout = validated_timeout(timeout_seconds, operation="discovery")
    return discover_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=_validated_profile_parent(profile_parent),
        variant=clean_variant,
    )
