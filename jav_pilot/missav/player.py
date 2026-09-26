"""Player control and HLS manifest capture on MissAV detail pages."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from ..web_download.media import SafeMediaError, probe_media_dimensions
from ..web_download.quality import (
    QualityHeightError,
    height_matches_selected_quality,
    normalize_quality_heights,
    validate_quality_height,
)
from .errors import MissavError
from .models import ManifestRequest
from .routing import BrowserMediaGate, clean_replay_headers
from .site import (
    MEDIA_HOST,
    PLAYER_SOURCE_NAMES,
    QUALITY_HEIGHT_LADDER,
    remaining_milliseconds,
)
from .urls import validated_https_url

@dataclass(frozen=True, slots=True)
class _PlayerQualitySelection:
    selected_height: int
    manifest_generation: int
    requires_new_manifest: bool
    verified_manifest: ManifestRequest | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class PlayerQualityChoice:
    height: int
    menu_option: object | None = field(default=None, repr=False, compare=False)
    source_url: str | None = field(default=None, repr=False, compare=False)
    verified_manifest: ManifestRequest | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (self.menu_option is None) == (self.source_url is None):
            raise ValueError("player quality choice must have exactly one target")
        if self.source_url is None and self.verified_manifest is not None:
            raise ValueError("menu quality choice cannot contain a manifest")
        if self.source_url is not None and self.verified_manifest is None:
            raise ValueError("source quality choice requires a verified manifest")


@dataclass
class ManifestCaptureState:
    # Playwright attaches an internal wrapper marker to bound event
    # callbacks.  This state intentionally keeps a __dict__ so its bound
    # request/response handlers can be registered and removed safely on a
    # persistent page.
    detail_page_url: str | None = None
    generation: int = 0
    manifests: list[ManifestRequest] = field(default_factory=list)
    first_manifest_at: float | None = None
    _generation_manifests: dict[int, list[ManifestRequest]] = field(
        default_factory=dict,
        repr=False,
    )
    _generation_source_urls: dict[int, str] = field(
        default_factory=dict,
        repr=False,
    )

    def capture_request(self, request: object) -> None:
        expected_source = self._generation_source_urls.get(self.generation)
        if expected_source is not None and not _request_chain_contains_url(
            request,
            expected_source,
        ):
            return
        manifest = _manifest_from_request(
            request,
            page_url=self.detail_page_url,
        )
        if manifest is None:
            return
        generation_manifests = self.manifests_for(self.generation)
        if len(generation_manifests) >= 32:
            return
        self.manifests.append(manifest)
        generation_manifests.append(manifest)
        if self.first_manifest_at is None:
            self.first_manifest_at = time.monotonic()

    def begin_generation(self, source_url: str | None = None) -> int:
        self.generation += 1
        self._generation_manifests.setdefault(self.generation, [])
        if source_url is not None:
            self._generation_source_urls[self.generation] = source_url
        return self.generation

    def manifests_for(self, generation: int) -> list[ManifestRequest]:
        return self._generation_manifests.setdefault(generation, [])


def _request_chain_contains_url(request: object, expected_url: str) -> bool:
    current: object | None = request
    for _ in range(7):
        if current is None:
            return False
        if str(getattr(current, "url", "") or "") == expected_url:
            return True
        current = getattr(current, "redirected_from", None)
    return False


def start_player(page: object, deadline: float) -> None:
    play_button = page.locator("button.plyr__control--overlaid").first
    try:
        count = int(play_button.count())
    except Exception:
        # Lightweight test doubles and older drivers may not expose
        # Locator.count(); retain the direct Playwright click path.
        play_button.click(timeout=remaining_milliseconds(deadline))
        return
    if count > 0:
        play_button.click(timeout=remaining_milliseconds(deadline))
        return
    observation_deadline = min(deadline, time.monotonic() + 8.0)
    while time.monotonic() < observation_deadline:
        try:
            count = int(play_button.count())
        except Exception:
            # Lightweight test doubles and older drivers may not expose
            # Locator.count(); retain the direct Playwright click path.
            play_button.click(timeout=remaining_milliseconds(deadline))
            return
        if count > 0:
            try:
                play_button.click(timeout=remaining_milliseconds(deadline))
                return
            except Exception:
                break
        page.wait_for_timeout(min(100, remaining_milliseconds(observation_deadline)))

    # Some MissAV responses initialize the video element and expose the
    # signed source variables before Plyr has rendered its overlay.  Loading
    # that already validated source is sufficient to capture the manifest and
    # avoids treating a slow control render as a quality-discovery failure.
    try:
        has_video = int(page.locator("video.player").count()) > 0
        sources = _player_source_candidates(page) if has_video else ()
    except Exception:
        has_video, sources = False, ()
    if has_video and sources:
        _load_player_source(page, sources[0], deadline)
        return
    raise MissavError("MissAV player controls are unavailable")


def select_player_quality(
    page: object,
    requested_height: int,
    deadline: float,
    *,
    manifest_generation: int,
    begin_manifest_generation: Callable[[], int],
    capture_state: ManifestCaptureState | None = None,
    probe_parent: Path | None = None,
    media_gate: BrowserMediaGate | None = None,
    resolved_choices: Mapping[int, PlayerQualityChoice] | None = None,
) -> _PlayerQualitySelection:
    try:
        try:
            selected_height = validate_quality_height(requested_height)
        except QualityHeightError as exc:
            raise MissavError("MissAV requested quality is invalid") from exc

        choices = (
            resolved_choices
            if resolved_choices is not None
            else resolve_player_quality_choices(
                page,
                deadline,
                capture_state=capture_state,
                probe_parent=probe_parent,
                media_gate=media_gate,
            )
        )
        target = choices.get(selected_height)
        if target is None:
            raise MissavError("MissAV requested quality is unavailable")

        if target.source_url is not None:
            if capture_state is None or target.verified_manifest is None:
                raise MissavError("MissAV requested quality is unavailable")
            return _PlayerQualitySelection(
                selected_height=selected_height,
                manifest_generation=capture_state.generation,
                requires_new_manifest=False,
                verified_manifest=target.verified_manifest,
            )

        if target.menu_option is not None and _player_option_checked(
            target.menu_option
        ):
            return _PlayerQualitySelection(
                selected_height=selected_height,
                manifest_generation=manifest_generation,
                requires_new_manifest=False,
            )

        selected_generation = begin_manifest_generation()
        if target.menu_option is None:  # pragma: no cover - guarded by choice.
            raise MissavError("MissAV requested quality is unavailable")
        _click_player_quality_option(page, target.menu_option, deadline)
        return _PlayerQualitySelection(
            selected_height=selected_height,
            manifest_generation=selected_generation,
            requires_new_manifest=True,
        )
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors can contain sensitive page state.
        raise MissavError("MissAV quality controls are unavailable") from None


def _open_player_quality_options(page: object, deadline: float) -> object | None:
    settings_controls = page.locator('button[data-plyr="settings"]')
    if not _wait_for_locator_presence(
        page,
        settings_controls,
        deadline,
        seconds=2.0,
    ):
        return None
    settings = settings_controls.first
    try:
        settings.click(timeout=min(1000, remaining_milliseconds(deadline)))
    except Exception:
        # The player may still be playing or covered by a responsive overlay;
        # source/manifest probing below is independent of this menu.
        return None
    quality_controls = page.locator(
        'button[data-plyr="setting"][data-setting="quality"]'
    )
    if not _wait_for_locator_presence(page, quality_controls, deadline, seconds=1.0):
        # Current Plyr builds expose the quality submenu as a forward
        # menuitem instead of the older data-setting attribute.
        try:
            menu_controls = page.locator(
                'button[data-plyr="settings"][role="menuitem"]'
                '[aria-haspopup="true"]:visible'
            )
            menu_count = int(menu_controls.count())
        except Exception:
            return None
        quality_controls = None
        quality_markers = ("quality", "画质", "质量")
        for index in range(menu_count):
            candidate = menu_controls.nth(index)
            try:
                label = str(candidate.inner_text() or "").casefold()
            except Exception:
                continue
            if any(marker in label for marker in quality_markers):
                quality_controls = candidate
                break
        if quality_controls is None:
            return None
    try:
        quality_button = (
            quality_controls.first
            if hasattr(quality_controls, "first")
            else quality_controls
        )
        quality_button.click(
            timeout=min(1000, remaining_milliseconds(deadline))
        )
    except Exception:
        return None
    try:
        options = page.locator('button[role="menuitemradio"]:visible')
        if int(options.count()) == 0:
            options = page.locator(
                'button[data-plyr="quality"][role="menuitemradio"]:visible'
            )
    except Exception:
        options = page.locator('button[role="menuitemradio"]:visible')
    _wait_for_locator_presence(page, options, deadline, seconds=1.0)
    return options


def _wait_for_locator_presence(
    page: object,
    locator: object,
    deadline: float,
    *,
    seconds: float,
) -> bool:
    observation_deadline = min(deadline, time.monotonic() + seconds)
    while True:
        if int(locator.count()) > 0:  # type: ignore[attr-defined]
            return True
        now = time.monotonic()
        if now >= observation_deadline:
            if now >= deadline:
                raise MissavError("MissAV capture timed out")
            return False
        page.wait_for_timeout(
            min(100, remaining_milliseconds(observation_deadline))  # type: ignore[attr-defined]
        )


def _visible_player_quality_choices(
    page: object,
    deadline: float,
) -> list[tuple[int, object]]:
    options = _open_player_quality_options(page, deadline)
    return _player_quality_choices(options) if options is not None else []


def discover_player_qualities(
    page: object,
    deadline: float,
    *,
    capture_state: ManifestCaptureState | None = None,
    probe_parent: Path | None = None,
    media_gate: BrowserMediaGate | None = None,
) -> tuple[int, ...]:
    try:
        return normalize_quality_heights(
            resolve_player_quality_choices(
                page,
                deadline,
                capture_state=capture_state,
                probe_parent=probe_parent,
                media_gate=media_gate,
            )
        )
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors can contain sensitive page state.
        raise MissavError("MissAV quality controls are unavailable") from None


def resolve_player_quality_choices(
    page: object,
    deadline: float,
    *,
    capture_state: ManifestCaptureState | None = None,
    probe_parent: Path | None = None,
    media_gate: BrowserMediaGate | None = None,
) -> dict[int, PlayerQualityChoice]:
    try:
        visible_choices = _visible_player_quality_choices(page, deadline)
    except MissavError:
        raise
    except Exception:  # noqa: BLE001 - browser errors can expose signed state.
        raise MissavError("MissAV quality controls are unavailable") from None
    if visible_choices:
        return {
            height: PlayerQualityChoice(height=height, menu_option=option)
            for height, option in sorted(visible_choices, reverse=True)
        }

    if capture_state is None or media_gate is None:
        raise MissavError("MissAV quality controls are unavailable")

    verified: dict[int, PlayerQualityChoice] = {}
    try:
        candidates = _player_source_candidates(page)
    except Exception:  # noqa: BLE001 - page state can contain signed URLs.
        candidates = ()
    for source_url in candidates:
        try:
            manifests = _capture_player_source_manifests(
                page,
                source_url,
                capture_state,
                deadline,
                media_gate=media_gate,
            )
        except MissavError as exc:
            if str(exc) == "MissAV capture timed out":
                raise
            continue
        except Exception:  # noqa: BLE001 - candidate failures stay isolated.
            continue
        for manifest in _latest_unique_manifests(manifests):
            try:
                dimensions = _probe_manifest_dimensions(
                    manifest,
                    deadline,
                    probe_parent=probe_parent,
                )
            except MissavError as exc:
                if str(exc) == "MissAV capture timed out":
                    raise
                continue
            except Exception:  # noqa: BLE001 - never expose signed media state.
                continue
            measured_height = _measured_quality_height(dimensions)
            if measured_height is None:
                continue
            verified.setdefault(
                measured_height,
                PlayerQualityChoice(
                    height=measured_height,
                    source_url=source_url,
                    verified_manifest=manifest,
                ),
            )
            break
    if not verified:
        raise MissavError("MissAV quality controls are unavailable")
    return dict(sorted(verified.items(), reverse=True))


def _player_source_candidates(page: object) -> tuple[str, ...]:
    raw_sources = page.evaluate(  # type: ignore[attr-defined]
        """() => ({
            source1280: typeof window.source1280 === "string"
                ? window.source1280
                : null,
            source842: typeof window.source842 === "string"
                ? window.source842
                : null,
        })"""
    )
    if not isinstance(raw_sources, Mapping):
        return ()
    candidates: dict[str, None] = {}
    for source_name in PLAYER_SOURCE_NAMES:
        raw_url = raw_sources.get(source_name)
        if not isinstance(raw_url, str):
            continue
        if (
            not raw_url
            or len(raw_url) > 8192
            or any(character in raw_url for character in "\r\n\0")
        ):
            continue
        parsed = validated_https_url(
            raw_url,
            allowed_hosts=frozenset({MEDIA_HOST}),
        )
        if parsed is None or parsed.fragment:
            continue
        candidates.setdefault(raw_url, None)
    return tuple(candidates)


def _capture_player_source_manifests(
    page: object,
    source_url: str,
    capture_state: ManifestCaptureState,
    deadline: float,
    *,
    media_gate: BrowserMediaGate,
) -> tuple[ManifestRequest, ...]:
    generation = capture_state.begin_generation(source_url)
    manifests = capture_state.manifests_for(generation)
    media_gate.block_surrit_requests = True
    try:
        _load_player_source(page, source_url, deadline)
        wait_for_manifest_count(page, manifests, 1, deadline)
        wait_for_manifest_stability(page, manifests, deadline, seconds=0.5)
        return tuple(manifests)
    finally:
        stop_player_source(page)


def _load_player_source(page: object, source_url: str, deadline: float) -> None:
    loaded = page.evaluate(  # type: ignore[attr-defined]
        """source => {
            const video = document.querySelector("video.player");
            if (!video) return false;
            video.pause();
            video.preload = "metadata";
            video.src = source;
            video.load();
            const playback = video.play();
            if (playback && typeof playback.catch === "function") {
                playback.catch(() => {});
            }
            return video.src === source;
        }""",
        source_url,
    )
    if time.monotonic() >= deadline:
        raise MissavError("MissAV capture timed out")
    if not loaded:
        raise MissavError("MissAV quality controls are unavailable")


def stop_player_source(page: object) -> None:
    try:
        page.evaluate(  # type: ignore[attr-defined]
            """() => {
                const video = document.querySelector("video.player");
                if (!video) return;
                video.pause();
                video.removeAttribute("src");
                video.load();
            }"""
        )
    except Exception:  # noqa: BLE001 - cleanup must not expose signed page state.
        pass


def _probe_manifest_dimensions(
    manifest: ManifestRequest,
    deadline: float,
    *,
    probe_parent: Path | None,
) -> tuple[int, int]:
    remaining = deadline - time.monotonic()
    if remaining < 1.0:
        raise MissavError("MissAV capture timed out")
    try:
        return probe_media_dimensions(
            manifest.url,
            manifest.headers,
            timeout_seconds=min(10.0, remaining),
            temp_root=probe_parent,
        )
    except SafeMediaError as exc:
        if str(exc) == "media dimension probe timed out":
            raise MissavError("MissAV capture timed out") from None
        raise MissavError("MissAV media dimensions could not be verified") from None


def _measured_quality_height(dimensions: tuple[int, int]) -> int | None:
    width, height = dimensions
    try:
        actual = validate_quality_height(min(width, height))
    except (QualityHeightError, TypeError):
        return None
    nearest = min(QUALITY_HEIGHT_LADDER, key=lambda candidate: abs(candidate - actual))
    if height_matches_selected_quality(actual, nearest):
        return nearest
    return None


def wait_for_verified_manifest(
    page: object,
    manifests: list[ManifestRequest],
    selected_height: int,
    deadline: float,
    *,
    probe_parent: Path | None,
) -> ManifestRequest:
    probed: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    while time.monotonic() < deadline:
        for manifest in tuple(manifests):
            fingerprint = (manifest.url, tuple(sorted(manifest.headers.items())))
            if fingerprint in probed:
                continue
            probed.add(fingerprint)
            try:
                width, height = _probe_manifest_dimensions(
                    manifest,
                    deadline,
                    probe_parent=probe_parent,
                )
            except MissavError as exc:
                if str(exc) == "MissAV capture timed out":
                    raise
                continue
            except Exception:  # noqa: BLE001 - signed request state stays private.
                continue
            if height_matches_selected_quality(min(width, height), selected_height):
                return manifest
        page.wait_for_timeout(min(100, remaining_milliseconds(deadline)))
    raise MissavError("MissAV capture timed out")


def _verified_manifest_for_height(
    manifests: list[ManifestRequest],
    selected_height: int,
    deadline: float,
    *,
    probe_parent: Path | None,
) -> ManifestRequest | None:
    for manifest in _latest_unique_manifests(manifests):
        try:
            width, height = _probe_manifest_dimensions(
                manifest,
                deadline,
                probe_parent=probe_parent,
            )
        except MissavError as exc:
            if str(exc) == "MissAV capture timed out":
                raise
            continue
        except Exception:  # noqa: BLE001 - media errors may contain signed URLs.
            continue
        if height_matches_selected_quality(min(width, height), selected_height):
            return manifest
    return None


def _latest_unique_manifests(
    manifests: tuple[ManifestRequest, ...] | list[ManifestRequest],
) -> tuple[ManifestRequest, ...]:
    unique: dict[str, ManifestRequest] = {}
    for manifest in manifests:
        unique[manifest.url] = manifest
    return tuple(unique.values())


def _player_quality_choices(options: object) -> list[tuple[int, object]]:
    count = int(options.count())  # type: ignore[attr-defined]
    if count < 1:
        return []
    if count > 32:
        raise MissavError("MissAV quality controls are unavailable")
    choices: dict[int, object] = {}
    for index in range(count):
        option = options.nth(index)  # type: ignore[attr-defined]
        height = _player_option_height(option)
        if height is not None and height not in choices:
            choices[height] = option
    return sorted(choices.items())


def _player_option_height(option: object) -> int | None:
    candidates = (
        option.get_attribute("value"),  # type: ignore[attr-defined]
        option.inner_text(),  # type: ignore[attr-defined]
    )
    heights: set[int] = set()
    for candidate in candidates:
        raw = str(candidate or "").strip().lower()
        matches = re.findall(r"(?<![0-9])([0-9]{3,4})p?(?![0-9])", raw)
        if len(set(matches)) != 1:
            continue
        try:
            heights.add(validate_quality_height(int(matches[0])))
        except QualityHeightError:
            continue
    return next(iter(heights)) if len(heights) == 1 else None


def _player_option_checked(option: object) -> bool:
    return str(option.get_attribute("aria-checked") or "").lower() == "true"  # type: ignore[attr-defined]


def _click_player_quality_option(page: object, option: object, deadline: float) -> None:
    option.click(timeout=remaining_milliseconds(deadline))  # type: ignore[attr-defined]
    if _player_option_checked(option):
        return
    while time.monotonic() < deadline:
        if _player_option_checked(option):
            return
        page.wait_for_timeout(min(100, remaining_milliseconds(deadline)))  # type: ignore[attr-defined]
    raise MissavError("MissAV quality selection could not be confirmed")


def wait_for_manifest_count(
    page: object,
    manifests: list[ManifestRequest],
    count: int,
    deadline: float,
) -> None:
    while len(manifests) < count and time.monotonic() < deadline:
        page.wait_for_timeout(min(100, remaining_milliseconds(deadline)))
    if len(manifests) < count:
        raise MissavError("MissAV capture timed out")


def wait_for_manifest_stability(
    page: object,
    manifests: list[ManifestRequest],
    deadline: float,
    *,
    seconds: float,
) -> None:
    last_count = len(manifests)
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        if len(manifests) != last_count:
            last_count = len(manifests)
            stable_since = time.monotonic()
        if manifests and time.monotonic() - stable_since >= seconds:
            return
        page.wait_for_timeout(min(100, remaining_milliseconds(deadline)))
    raise MissavError("MissAV capture timed out")


def _manifest_from_request(
    request: object,
    *,
    page_url: str | None,
) -> ManifestRequest | None:
    if page_url is None:
        return None
    try:
        url = str(getattr(request, "url", "") or "")
        if validated_https_url(url, allowed_hosts=frozenset({MEDIA_HOST})) is None:
            return None
        if str(getattr(request, "method", "") or "").upper() != "GET":
            return None
        resource_type = str(getattr(request, "resource_type", "") or "").lower()
        if resource_type not in {"fetch", "media", "xhr"}:
            return None
        all_headers = getattr(request, "all_headers", None)
        raw_headers = (
            all_headers() if callable(all_headers) else getattr(request, "headers", {})
        )
        return ManifestRequest(
            url=url,
            headers=clean_replay_headers(raw_headers),
            page_url=page_url,
        )
    except Exception:  # noqa: BLE001 - request callbacks must remain non-disruptive.
        return None
