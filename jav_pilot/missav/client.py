"""Public MissAV operations: discovery, descriptions, qualities and manifest capture."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..core.catalog_code import normalize_catalog_code
from ..core.guards import QueryError, normalize_query
from ..web_download.quality import (
    QualityHeightError,
    WebDownloadVariantOption,
    validate_quality_height,
)
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    normalize_web_download_variant,
)
from .automation import (
    capture_with_playwright,
    discover_options_with_playwright,
    discover_resources_with_playwright,
    discover_series_with_playwright,
    discover_with_playwright,
    fetch_description_with_playwright,
)
from .errors import MissavError, MissavTransientError
from .models import (
    ManifestRequest,
    MissavResourceDiscovery,
    MissavResourceItem,
    MissavResourcePage,
    MissavResourceState,
    MissavSeriesDiscovery,
)
from .navigation import (
    is_browser_target_closed,
    is_browser_transport_reset,
    prepare_exact_detail_capture,
    raise_capture_cancelled,
    remove_page_listener,
)
from .parsing import (
    optional_bounded_integer,
    required_bounded_integer,
    validated_code,
    validated_resource_pending,
    validated_series_prefix,
)
from .player import (
    ManifestCaptureState,
    PlayerQualityChoice,
    resolve_player_quality_choices,
    select_player_quality,
    start_player,
    stop_player_source,
    wait_for_manifest_count,
    wait_for_manifest_stability,
    wait_for_verified_manifest,
)
from .routing import BrowserMediaGate
from .site import (
    MAX_RESOURCE_PAGES,
    MAX_RESOURCE_RESULTS,
    MAX_SERIES_CANDIDATES,
    remaining_milliseconds,
    uses_configured_missav_description_site,
    uses_configured_missav_resource_site,
    uses_configured_missav_site,
    validate_quality_strategy,
)

__all__ = [
    "capture_manifest",
    "capture_manifest_on_page",
    "discover_options",
    "discover_qualities",
    "discover_resource_items",
    "discover_series_codes",
    "fetch_description",
]


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
        maximum=999_999_999,
    )
    clean_end = optional_bounded_integer(
        end,
        name="series end",
        minimum=0,
        maximum=999_999_999,
    )
    if clean_start is not None and clean_end is not None and clean_start > clean_end:
        raise MissavError("MissAV series range is invalid")
    clean_max_codes = required_bounded_integer(
        max_codes,
        name="series result limit",
        minimum=1,
        maximum=MAX_SERIES_CANDIDATES,
    )
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MissavError("MissAV series timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError("MissAV series timeout must be between 1 and 180 seconds")
    cancelled = cancel_event or threading.Event()
    if cancelled.is_set():
        return MissavSeriesDiscovery(codes=(), complete=False)
    return discover_series_with_playwright(
        display_prefix,
        canonical_prefix,
        suffix_width=clean_width,
        start=clean_start,
        end=clean_end,
        max_codes=clean_max_codes,
        timeout_seconds=clean_timeout,
        cancel_event=cancelled,
    )


@uses_configured_missav_resource_site
def discover_resource_items(
    query: object,
    *,
    result_limit: object,
    suffix_width: object | None = None,
    start: object | None = None,
    end: object | None = None,
    next_page: object | None = 1,
    pending: Sequence[MissavResourceItem] = (),
    cursor: object = 0,
    total_pages: object | None = None,
    scanned_pages: object = 0,
    timeout_seconds: float = 120.0,
    cancel_event: threading.Event | None = None,
    on_page: Callable[[MissavResourcePage], None] | None = None,
) -> MissavResourceDiscovery:
    """Discover bounded MissAV resources without opening a video player."""

    try:
        display_query = normalize_query(query)
    except QueryError as exc:
        raise MissavError("A valid MissAV resource query is required") from exc
    canonical_query: str | None = None
    clean_limit = required_bounded_integer(
        result_limit,
        name="resource result limit",
        minimum=1,
        maximum=MAX_RESOURCE_RESULTS,
    )
    clean_width = optional_bounded_integer(
        suffix_width,
        name="suffix width",
        minimum=1,
        maximum=9,
    )
    clean_start = optional_bounded_integer(
        start,
        name="resource start",
        minimum=0,
        maximum=999_999_999,
    )
    clean_end = optional_bounded_integer(
        end,
        name="resource end",
        minimum=0,
        maximum=999_999_999,
    )
    if clean_start is not None and clean_end is not None and clean_start > clean_end:
        raise MissavError("MissAV resource range is invalid")
    if clean_width is not None or clean_start is not None or clean_end is not None:
        display_query, canonical_query = validated_series_prefix(display_query)
    else:
        exact_code = normalize_catalog_code(display_query, max_length=32)
        if exact_code is not None:
            canonical_query = exact_code[1]
    clean_next_page = optional_bounded_integer(
        next_page,
        name="resource next page",
        minimum=1,
        maximum=MAX_RESOURCE_PAGES,
    )
    clean_total_pages = optional_bounded_integer(
        total_pages,
        name="resource total pages",
        minimum=1,
        maximum=MAX_RESOURCE_PAGES,
    )
    clean_scanned_pages = required_bounded_integer(
        scanned_pages,
        name="resource scanned pages",
        minimum=0,
        maximum=MAX_RESOURCE_PAGES,
    )
    clean_pending = validated_resource_pending(pending)
    clean_cursor = required_bounded_integer(
        cursor,
        name="resource cursor",
        minimum=0,
        maximum=len(clean_pending),
    )
    if clean_pending and clean_next_page is None:
        raise MissavError("MissAV resource cursor is invalid")
    if not clean_pending and clean_cursor:
        raise MissavError("MissAV resource cursor is invalid")
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MissavError("MissAV resource timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError("MissAV resource timeout must be between 1 and 180 seconds")
    cancelled = cancel_event or threading.Event()
    initial_state = MissavResourceState(
        next_page=clean_next_page,
        pending=clean_pending,
        cursor=clean_cursor,
        total_pages=clean_total_pages,
        scanned_pages=clean_scanned_pages,
    )
    if clean_next_page is None or cancelled.is_set():
        return MissavResourceDiscovery(
            items=(),
            state=initial_state,
            complete=clean_next_page is None,
        )
    return discover_resources_with_playwright(
        display_query,
        canonical_query,
        result_limit=clean_limit,
        suffix_width=clean_width,
        start=clean_start,
        end=clean_end,
        state=initial_state,
        timeout_seconds=clean_timeout,
        cancel_event=cancelled,
        on_page=on_page,
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
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise MissavError("MissAV variant is invalid") from exc
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise MissavError("MissAV description timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError(
            "MissAV description timeout must be between 1 and 180 seconds"
        )
    if profile_parent is None:
        return fetch_description_with_playwright(
            search_code,
            canonical_code,
            clean_timeout,
            variant=clean_variant,
        )
    profile_root = Path(profile_parent)
    if (
        not profile_root.is_absolute()
        or not profile_root.is_dir()
        or profile_root.is_symlink()
    ):
        raise MissavError(
            "MissAV description browser profile parent must be a regular directory"
        )
    return fetch_description_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=profile_root.resolve(strict=True),
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
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise MissavError("MissAV variant is invalid") from exc
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise MissavError("MissAV capture timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError("MissAV capture timeout must be between 1 and 180 seconds")
    if requested_height is None:
        clean_height = None
    else:
        try:
            clean_height = validate_quality_height(requested_height)
        except QualityHeightError as exc:
            raise MissavError("MissAV requested quality is invalid") from exc
    clean_strategy = validate_quality_strategy(
        quality_strategy,
        requested_height=clean_height,
    )
    if profile_parent is None:
        return capture_with_playwright(
            search_code,
            canonical_code,
            clean_timeout,
            variant=clean_variant,
            requested_height=clean_height,
            quality_strategy=clean_strategy,
        )
    profile_root = Path(profile_parent)
    if (
        not profile_root.is_absolute()
        or not profile_root.is_dir()
        or profile_root.is_symlink()
    ):
        raise MissavError("MissAV browser profile parent must be a regular directory")
    return capture_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=profile_root.resolve(strict=True),
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
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise MissavError("MissAV variant is invalid") from exc
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise MissavError("MissAV capture timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError("MissAV capture timeout must be between 1 and 180 seconds")
    if requested_height is None:
        clean_height = None
    else:
        try:
            clean_height = validate_quality_height(requested_height)
        except QualityHeightError as exc:
            raise MissavError("MissAV requested quality is invalid") from exc
    clean_strategy = validate_quality_strategy(
        quality_strategy,
        requested_height=clean_height,
    )
    deadline = time.monotonic() + clean_timeout
    capture_state = ManifestCaptureState()
    gate = media_gate or BrowserMediaGate(block_surrit_requests=False)
    gate.block_surrit_requests = False
    request_callback: Callable[[object], None] | None = None
    try:
        raise_capture_cancelled(cancel_event)
        set_timeout = getattr(page, "set_default_timeout", None)
        if callable(set_timeout):
            set_timeout(remaining_milliseconds(deadline))
        request_callback = capture_state.capture_request
        detail_url = prepare_exact_detail_capture(
            page,
            search_code,
            canonical_code,
            deadline,
            variant=clean_variant,
            request_handler=request_callback,
            cancel_event=cancel_event,
            stage_callback=stage_callback,
        )
        capture_state.detail_page_url = detail_url
        raise_capture_cancelled(cancel_event)
        if stage_callback is not None:
            stage_callback("start_player")
        start_player(page, deadline)
        raise_capture_cancelled(cancel_event)

        if clean_strategy != "legacy":
            if stage_callback is not None:
                stage_callback("manifest_wait")
            initial_manifests = capture_state.manifests_for(capture_state.generation)
            wait_for_manifest_count(page, initial_manifests, 1, deadline)
            wait_for_manifest_stability(
                page,
                initial_manifests,
                deadline,
                seconds=0.5,
            )
            resolved_choices: Mapping[int, PlayerQualityChoice] | None = None
            selected_height = clean_height
            if stage_callback is not None:
                stage_callback("quality_resolution")
            if clean_strategy == "highest":
                resolved_choices = resolve_player_quality_choices(
                    page,
                    deadline,
                    capture_state=capture_state,
                    probe_parent=None,
                    media_gate=gate,
                )
                eligible_heights = tuple(
                    height
                    for height in resolved_choices
                    if clean_height is None or height <= clean_height
                )
                if not eligible_heights:
                    raise MissavError("MissAV requested quality is unavailable")
                selected_height = max(eligible_heights)
            if selected_height is None:
                raise MissavError("MissAV selected quality requires a height")
            selection = select_player_quality(
                page,
                selected_height,
                deadline,
                manifest_generation=capture_state.generation,
                begin_manifest_generation=capture_state.begin_generation,
                capture_state=capture_state,
                probe_parent=None,
                media_gate=gate,
                resolved_choices=resolved_choices,
            )
            if selection.verified_manifest is not None:
                selected = selection.verified_manifest
                return ManifestRequest(
                    url=selected.url,
                    headers=dict(selected.headers),
                    page_url=selected.page_url,
                    selected_height=selection.selected_height,
                )
            selected_manifests = capture_state.manifests_for(
                selection.manifest_generation
            )
            try:
                selected = wait_for_verified_manifest(
                    page,
                    selected_manifests,
                    selection.selected_height,
                    deadline,
                    probe_parent=None,
                )
            finally:
                stop_player_source(page)
            return ManifestRequest(
                url=selected.url,
                headers=dict(selected.headers),
                page_url=selected.page_url,
                selected_height=selection.selected_height,
            )

        if stage_callback is not None:
            stage_callback("manifest_wait")
        while time.monotonic() < deadline:
            raise_capture_cancelled(cancel_event)
            if (
                capture_state.manifests
                and capture_state.first_manifest_at is not None
                and time.monotonic() - capture_state.first_manifest_at >= 1.5
            ):
                break
            page.wait_for_timeout(min(250, remaining_milliseconds(deadline)))
        if not capture_state.manifests:
            raise MissavError("MissAV did not expose a downloadable HLS stream")
        return capture_state.manifests[-1]
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
    finally:
        if stage_callback is not None:
            stage_callback(None)
        stop_player_source(page)
        remove_page_listener(page, "request", request_callback)


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
    try:
        clean_variant = normalize_web_download_variant(variant)
    except ValueError as exc:
        raise MissavError("MissAV variant is invalid") from exc
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise MissavError("MissAV discovery timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError("MissAV discovery timeout must be between 1 and 180 seconds")
    if profile_parent is None:
        return discover_with_playwright(
            search_code,
            canonical_code,
            clean_timeout,
            variant=clean_variant,
        )
    profile_root = Path(profile_parent)
    if (
        not profile_root.is_absolute()
        or not profile_root.is_dir()
        or profile_root.is_symlink()
    ):
        raise MissavError("MissAV browser profile parent must be a regular directory")
    return discover_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=profile_root.resolve(strict=True),
        variant=clean_variant,
    )


@uses_configured_missav_site
def discover_options(
    code: object,
    *,
    timeout_seconds: float = 45.0,
    profile_parent: Path | str | None = None,
) -> tuple[WebDownloadVariantOption, ...]:
    """Return isolated quality results for every supported exact work variant."""

    search_code, canonical_code = validated_code(code)
    try:
        clean_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise MissavError("MissAV discovery timeout must be a number") from exc
    if not 1.0 <= clean_timeout <= 180.0:
        raise MissavError("MissAV discovery timeout must be between 1 and 180 seconds")
    if profile_parent is None:
        return discover_options_with_playwright(
            search_code,
            canonical_code,
            clean_timeout,
        )
    profile_root = Path(profile_parent)
    if (
        not profile_root.is_absolute()
        or not profile_root.is_dir()
        or profile_root.is_symlink()
    ):
        raise MissavError("MissAV browser profile parent must be a regular directory")
    return discover_options_with_playwright(
        search_code,
        canonical_code,
        clean_timeout,
        profile_parent=profile_root.resolve(strict=True),
    )
