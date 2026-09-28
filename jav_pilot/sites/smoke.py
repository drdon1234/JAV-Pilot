from __future__ import annotations

import math
import re
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from itertools import islice
from typing import Protocol

from ..core.catalog_code import normalize_catalog_code
from ..net.network_guard import PublicHostResolver
from ..config.settings import SITE_DIAGNOSTIC_SITE_IDS
from ..web_download.media import (
    SafeMediaError,
    require_allowed_media_url,
    validate_native_hls_manifest,
)
from ..web_download.quality import QualityHeightError, normalize_quality_heights


MAX_SMOKE_MANIFEST_BYTES = 1024 * 1024
MAX_SMOKE_MEDIA_SAMPLE_BYTES = 64 * 1024
MAX_SMOKE_IMAGE_BYTES = 5 * 1024 * 1024

SMOKE_SITES = frozenset((*SITE_DIAGNOSTIC_SITE_IDS, "system"))
SMOKE_CHECKS = frozenset(
    {
        "snapshot",
        "adapter",
        "exact_code",
        "detail",
        "image",
        "qualities",
        "manifest",
        "media_range",
        "side_effects",
    }
)
SMOKE_ERROR_CODES = frozenset(
    {
        "invalid_config",
        "dns_failed",
        "tls_failed",
        "connection_failed",
        "redirect_rejected",
        "response_too_large",
        "upstream_http",
        "challenge_detected",
        "parse_drift",
        "code_mismatch",
        "image_invalid",
        "image_host_rejected",
        "manifest_invalid",
        "media_host_rejected",
        "range_unsupported",
        "timeout",
        "dependency_unavailable",
        "internal_error",
        "side_effect_detected",
        "snapshot_failed",
    }
)
SMOKE_PUBLIC_FIELDS = frozenset(
    {
        "site",
        "check",
        "ok",
        "latency",
        "error_code",
        "code",
        "heights",
        "bytes",
        "count",
    }
)

_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTENT_RANGE_RE = re.compile(r"^bytes 0-(\d+)/(\d+|\*)$", re.IGNORECASE)
_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/apng",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)
_MAX_LATENCY_MS = 180_000
_MAX_SAFE_COUNT = 10_000_000_000


class SiteSmokeError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = _validate_error_code(error_code)
        super().__init__(self.error_code)


class SmokeHttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]
    url: str

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class SmokeAdapter(Protocol):
    site_id: str

    def run(self) -> Sequence["SmokeCheck"]: ...


@dataclass(frozen=True)
class SmokeCheck:
    site: str
    check: str
    ok: bool
    latency: int
    error_code: str | None = None
    code: str | None = None
    heights: tuple[int, ...] | None = None
    bytes: int | None = None
    count: int | None = None

    def __post_init__(self) -> None:
        if self.site not in SMOKE_SITES:
            raise ValueError("site smoke site is invalid")
        if self.check not in SMOKE_CHECKS:
            raise ValueError("site smoke check is invalid")
        if not isinstance(self.ok, bool):
            raise ValueError("site smoke status is invalid")
        if (
            isinstance(self.latency, bool)
            or not isinstance(self.latency, int)
            or not 0 <= self.latency <= _MAX_LATENCY_MS
        ):
            raise ValueError("site smoke latency is invalid")
        if self.ok and self.error_code is not None:
            raise ValueError("successful site smoke check cannot contain an error")
        if not self.ok:
            _validate_error_code(self.error_code)
        if self.code is not None:
            normalized = normalize_catalog_code(self.code, max_length=40)
            if normalized is None or normalized[0] != self.code:
                raise ValueError("site smoke catalog code is invalid")
        if self.heights is not None:
            try:
                normalized_heights = normalize_quality_heights(self.heights)
            except (QualityHeightError, TypeError) as exc:
                raise ValueError("site smoke heights are invalid") from exc
            if not self.heights or normalized_heights != self.heights:
                raise ValueError("site smoke heights are invalid")
        for value in (self.bytes, self.count):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_SAFE_COUNT
            ):
                raise ValueError("site smoke count is invalid")

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "site": self.site,
            "check": self.check,
            "ok": self.ok,
            "latency": self.latency,
            "error_code": self.error_code,
        }
        if self.code is not None:
            payload["code"] = self.code
        if self.heights is not None:
            payload["heights"] = list(self.heights)
        if self.bytes is not None:
            payload["bytes"] = self.bytes
        if self.count is not None:
            payload["count"] = self.count
        if not set(payload).issubset(SMOKE_PUBLIC_FIELDS):
            raise ValueError("site smoke output contains an unsafe field")
        return payload


@dataclass(frozen=True)
class SmokeStateSnapshot:
    task_counts: tuple[tuple[str, int], ...]
    media_files: int

    def __post_init__(self) -> None:
        normalized: list[tuple[str, int]] = []
        seen: set[str] = set()
        for key, value in self.task_counts:
            clean_key = str(key or "").strip().lower()
            if (
                not _SAFE_NAME_RE.fullmatch(clean_key)
                or clean_key in seen
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_SAFE_COUNT
            ):
                raise ValueError("site smoke task snapshot is invalid")
            seen.add(clean_key)
            normalized.append((clean_key, value))
        if tuple(sorted(normalized)) != self.task_counts:
            raise ValueError("site smoke task snapshot must be sorted")
        if (
            isinstance(self.media_files, bool)
            or not isinstance(self.media_files, int)
            or not 0 <= self.media_files <= _MAX_SAFE_COUNT
        ):
            raise ValueError("site smoke media snapshot is invalid")

    @classmethod
    def from_counts(
        cls,
        task_counts: Mapping[str, int],
        *,
        media_files: int,
    ) -> "SmokeStateSnapshot":
        if not isinstance(task_counts, Mapping) or len(task_counts) > 32:
            raise ValueError("site smoke task snapshot is invalid")
        return cls(
            tuple(
                sorted(
                    (str(key or "").strip().lower(), value)
                    for key, value in task_counts.items()
                )
            ),
            media_files,
        )

    @property
    def total_count(self) -> int:
        return sum(value for _, value in self.task_counts) + self.media_files


@dataclass(frozen=True)
class SearchSiteObservation:
    code: str
    detail_field_count: int
    image_url: str
    image_fetch: Callable[[], SmokeHttpResponse]


@dataclass(frozen=True)
class MissavSmokeObservation:
    code: str
    heights: Sequence[int]
    manifest_url: str
    sample_url: str
    manifest_fetch: Callable[[], SmokeHttpResponse]
    sample_fetch: Callable[[Mapping[str, str]], SmokeHttpResponse]


class SearchSiteSmokeAdapter:
    def __init__(
        self,
        site_id: str,
        expected_code: str,
        probe: Callable[[], SearchSiteObservation],
        *,
        image_url_validator: Callable[[str], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if site_id not in {"javbus", "javdb"}:
            raise ValueError("search smoke adapter site is invalid")
        self.site_id = site_id
        self.expected_code, self._expected_key = _normalized_code(expected_code)
        self._probe = probe
        self._image_url_validator = image_url_validator
        self._monotonic = monotonic

    def run(self) -> tuple[SmokeCheck, ...]:
        started = _monotonic_value(self._monotonic())
        try:
            observation = self._probe()
        except Exception as exc:  # noqa: BLE001 - raw upstream details stay private.
            return (
                _failed_check(
                    self.site_id,
                    "exact_code",
                    _exception_code(exc),
                    _elapsed_ms(started, self._monotonic),
                    code=self.expected_code,
                ),
            )
        if not isinstance(observation, SearchSiteObservation):
            return (
                _failed_check(
                    self.site_id,
                    "exact_code",
                    "internal_error",
                    _elapsed_ms(started, self._monotonic),
                    code=self.expected_code,
                ),
            )
        latency = _elapsed_ms(started, self._monotonic)
        try:
            observed_code, observed_key = _normalized_code(observation.code)
        except ValueError:
            observed_code, observed_key = self.expected_code, ""
        if observed_key != self._expected_key:
            return (
                _failed_check(
                    self.site_id,
                    "exact_code",
                    "code_mismatch",
                    latency,
                    code=self.expected_code,
                ),
            )

        checks = [
            SmokeCheck(
                site=self.site_id,
                check="exact_code",
                ok=True,
                latency=latency,
                code=observed_code,
            )
        ]
        detail_count = observation.detail_field_count
        if (
            isinstance(detail_count, bool)
            or not isinstance(detail_count, int)
            or not 1 <= detail_count <= 10_000
        ):
            checks.append(
                _failed_check(
                    self.site_id,
                    "detail",
                    "parse_drift",
                    latency,
                    code=observed_code,
                )
            )
        else:
            checks.append(
                SmokeCheck(
                    site=self.site_id,
                    check="detail",
                    ok=True,
                    latency=latency,
                    code=observed_code,
                    count=detail_count,
                )
            )

        image_started = _monotonic_value(self._monotonic())
        try:
            body, content_type, _ = _fetch_bounded_body(
                observation.image_fetch,
                initial_url=observation.image_url,
                url_validator=self._image_url_validator,
                max_bytes=MAX_SMOKE_IMAGE_BYTES,
                accepted_statuses=frozenset({200}),
                host_error_code="image_host_rejected",
            )
            _validate_raster_image(body, content_type)
            checks.append(
                SmokeCheck(
                    site=self.site_id,
                    check="image",
                    ok=True,
                    latency=_elapsed_ms(image_started, self._monotonic),
                    code=observed_code,
                    bytes=len(body),
                )
            )
        except Exception as exc:  # noqa: BLE001 - raw image errors stay private.
            checks.append(
                _failed_check(
                    self.site_id,
                    "image",
                    _exception_code(exc, default="image_invalid"),
                    _elapsed_ms(image_started, self._monotonic),
                    code=observed_code,
                )
            )
        return tuple(checks)


class JavBusSmokeAdapter(SearchSiteSmokeAdapter):
    def __init__(
        self,
        expected_code: str,
        probe: Callable[[], SearchSiteObservation],
        *,
        image_url_validator: Callable[[str], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            "javbus",
            expected_code,
            probe,
            image_url_validator=image_url_validator,
            monotonic=monotonic,
        )


class JavDbSmokeAdapter(SearchSiteSmokeAdapter):
    def __init__(
        self,
        expected_code: str,
        probe: Callable[[], SearchSiteObservation],
        *,
        image_url_validator: Callable[[str], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            "javdb",
            expected_code,
            probe,
            image_url_validator=image_url_validator,
            monotonic=monotonic,
        )


class MissavSmokeAdapter:
    site_id = "missav"

    def __init__(
        self,
        expected_code: str,
        probe: Callable[[], MissavSmokeObservation],
        *,
        manifest_validator: Callable[[str, str], object] = (
            validate_native_hls_manifest
        ),
        media_url_validator: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.expected_code, self._expected_key = _normalized_code(expected_code)
        self._probe = probe
        self._manifest_validator = manifest_validator
        self._media_url_validator = media_url_validator or _validate_media_url
        self._monotonic = monotonic

    def run(self) -> tuple[SmokeCheck, ...]:
        started = _monotonic_value(self._monotonic())
        try:
            observation = self._probe()
        except Exception as exc:  # noqa: BLE001 - raw browser details stay private.
            return (
                _failed_check(
                    self.site_id,
                    "exact_code",
                    _exception_code(exc),
                    _elapsed_ms(started, self._monotonic),
                    code=self.expected_code,
                ),
            )
        if not isinstance(observation, MissavSmokeObservation):
            return (
                _failed_check(
                    self.site_id,
                    "exact_code",
                    "internal_error",
                    _elapsed_ms(started, self._monotonic),
                    code=self.expected_code,
                ),
            )
        latency = _elapsed_ms(started, self._monotonic)
        try:
            observed_code, observed_key = _normalized_code(observation.code)
        except ValueError:
            observed_code, observed_key = self.expected_code, ""
        if observed_key != self._expected_key:
            return (
                _failed_check(
                    self.site_id,
                    "exact_code",
                    "code_mismatch",
                    latency,
                    code=self.expected_code,
                ),
            )

        checks: list[SmokeCheck] = [
            SmokeCheck(
                site=self.site_id,
                check="exact_code",
                ok=True,
                latency=latency,
                code=observed_code,
            )
        ]
        try:
            heights = normalize_quality_heights(observation.heights)
            if not heights:
                raise QualityHeightError("empty quality list")
            checks.append(
                SmokeCheck(
                    site=self.site_id,
                    check="qualities",
                    ok=True,
                    latency=latency,
                    code=observed_code,
                    heights=heights,
                )
            )
        except (QualityHeightError, TypeError):
            checks.append(
                _failed_check(
                    self.site_id,
                    "qualities",
                    "parse_drift",
                    latency,
                    code=observed_code,
                )
            )

        manifest_started = _monotonic_value(self._monotonic())
        try:
            manifest_bytes, _, final_manifest_url = _fetch_bounded_body(
                observation.manifest_fetch,
                initial_url=observation.manifest_url,
                url_validator=self._media_url_validator,
                max_bytes=MAX_SMOKE_MANIFEST_BYTES,
                accepted_statuses=frozenset({200}),
                host_error_code="media_host_rejected",
            )
            try:
                manifest_text = manifest_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise SiteSmokeError("manifest_invalid") from exc
            playlist = self._manifest_validator(
                manifest_text,
                final_manifest_url,
            )
            segment_count = getattr(playlist, "segment_count", None)
            if (
                isinstance(segment_count, bool)
                or not isinstance(segment_count, int)
                or segment_count < 1
                or segment_count > _MAX_SAFE_COUNT
            ):
                raise SiteSmokeError("manifest_invalid")
            checks.append(
                SmokeCheck(
                    site=self.site_id,
                    check="manifest",
                    ok=True,
                    latency=_elapsed_ms(manifest_started, self._monotonic),
                    code=observed_code,
                    bytes=len(manifest_bytes),
                    count=segment_count,
                )
            )
        except Exception as exc:  # noqa: BLE001 - signed manifest details stay private.
            checks.append(
                _failed_check(
                    self.site_id,
                    "manifest",
                    _exception_code(exc, default="manifest_invalid"),
                    _elapsed_ms(manifest_started, self._monotonic),
                    code=observed_code,
                )
            )
            return tuple(checks)

        sample_started = _monotonic_value(self._monotonic())
        try:
            sample = fetch_range_sample(
                observation.sample_fetch,
                initial_url=observation.sample_url,
                url_validator=self._media_url_validator,
            )
            checks.append(
                SmokeCheck(
                    site=self.site_id,
                    check="media_range",
                    ok=True,
                    latency=_elapsed_ms(sample_started, self._monotonic),
                    code=observed_code,
                    bytes=len(sample),
                )
            )
        except Exception as exc:  # noqa: BLE001 - signed sample details stay private.
            checks.append(
                _failed_check(
                    self.site_id,
                    "media_range",
                    _exception_code(exc, default="range_unsupported"),
                    _elapsed_ms(sample_started, self._monotonic),
                    code=observed_code,
                )
            )
        return tuple(checks)


def run_site_smoke(
    adapters: Sequence[SmokeAdapter],
    snapshot_provider: Callable[[], SmokeStateSnapshot],
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[SmokeCheck, ...]:
    if not adapters or len(adapters) > len(SITE_DIAGNOSTIC_SITE_IDS):
        raise ValueError("site smoke adapters are invalid")
    seen: set[str] = set()
    for adapter in adapters:
        if adapter.site_id not in SITE_DIAGNOSTIC_SITE_IDS:
            raise ValueError("site smoke adapter is invalid")
        if adapter.site_id in seen:
            raise ValueError("duplicate site smoke adapter")
        seen.add(adapter.site_id)

    snapshot_started = _monotonic_value(monotonic())
    try:
        before = snapshot_provider()
        if not isinstance(before, SmokeStateSnapshot):
            raise TypeError("invalid snapshot")
    except Exception:  # noqa: BLE001 - snapshot details are not public.
        return (
            _failed_check(
                "system",
                "snapshot",
                "snapshot_failed",
                _elapsed_ms(snapshot_started, monotonic),
            ),
        )

    checks: list[SmokeCheck] = []
    for adapter in adapters:
        adapter_started = _monotonic_value(monotonic())
        try:
            adapter_checks = tuple(islice(iter(adapter.run()), 17))
            if not adapter_checks or any(
                not isinstance(check, SmokeCheck) or check.site != adapter.site_id
                for check in adapter_checks
            ) or len(adapter_checks) > 16:
                raise ValueError("invalid adapter output")
            checks.extend(adapter_checks)
        except Exception:  # noqa: BLE001 - adapter details are not public.
            checks.append(
                _failed_check(
                    adapter.site_id,
                    "adapter",
                    "internal_error",
                    _elapsed_ms(adapter_started, monotonic),
                )
            )

    verify_started = _monotonic_value(monotonic())
    try:
        after = snapshot_provider()
        if not isinstance(after, SmokeStateSnapshot):
            raise TypeError("invalid snapshot")
    except Exception:  # noqa: BLE001 - snapshot details are not public.
        checks.append(
            _failed_check(
                "system",
                "side_effects",
                "snapshot_failed",
                _elapsed_ms(verify_started, monotonic),
            )
        )
        return tuple(checks)

    if after != before:
        checks.append(
            _failed_check(
                "system",
                "side_effects",
                "side_effect_detected",
                _elapsed_ms(verify_started, monotonic),
                count=after.total_count,
            )
        )
    else:
        checks.append(
            SmokeCheck(
                site="system",
                check="side_effects",
                ok=True,
                latency=_elapsed_ms(verify_started, monotonic),
                count=after.total_count,
            )
        )
    return tuple(checks)


def smoke_public_payload(checks: Sequence[SmokeCheck]) -> list[dict[str, object]]:
    if len(checks) > 64:
        raise ValueError("site smoke output is too large")
    payload = [check.public_dict() for check in checks]
    if any(not set(item).issubset(SMOKE_PUBLIC_FIELDS) for item in payload):
        raise ValueError("site smoke output contains an unsafe field")
    return payload


def fetch_range_sample(
    fetch: Callable[[Mapping[str, str]], SmokeHttpResponse],
    *,
    initial_url: str,
    url_validator: Callable[[str], None],
) -> bytes:
    _apply_url_validator(initial_url, url_validator, "media_host_rejected")
    response: SmokeHttpResponse | None = None
    try:
        response = fetch({"Range": f"bytes=0-{MAX_SMOKE_MEDIA_SAMPLE_BYTES - 1}"})
        _validate_response_url(response, url_validator, "media_host_rejected")
        status = _response_status(response)
        if status != 206:
            if status in {200, 416}:
                raise SiteSmokeError("range_unsupported")
            raise SiteSmokeError("upstream_http")
        content_range = _header(response.headers, "content-range")
        match = _CONTENT_RANGE_RE.fullmatch(content_range or "")
        if match is None:
            raise SiteSmokeError("range_unsupported")
        end = int(match.group(1))
        if end < 0 or end >= MAX_SMOKE_MEDIA_SAMPLE_BYTES:
            raise SiteSmokeError("range_unsupported")
        total_text = match.group(2)
        if total_text != "*" and int(total_text) <= end:
            raise SiteSmokeError("range_unsupported")
        body = _read_bounded(response, MAX_SMOKE_MEDIA_SAMPLE_BYTES)
        if not body or len(body) != end + 1:
            raise SiteSmokeError("range_unsupported")
        declared_length = _content_length(response.headers)
        if declared_length is not None and declared_length != len(body):
            raise SiteSmokeError("range_unsupported")
        return body
    except SiteSmokeError:
        raise
    except TimeoutError as exc:
        raise SiteSmokeError("timeout") from exc
    except OSError as exc:
        raise SiteSmokeError("connection_failed") from exc
    except Exception as exc:  # noqa: BLE001 - transport details remain private.
        raise SiteSmokeError("internal_error") from exc
    finally:
        _close_response(response)


def _fetch_bounded_body(
    fetch: Callable[[], SmokeHttpResponse],
    *,
    initial_url: str,
    url_validator: Callable[[str], None],
    max_bytes: int,
    accepted_statuses: frozenset[int],
    host_error_code: str,
) -> tuple[bytes, str, str]:
    _apply_url_validator(initial_url, url_validator, host_error_code)
    response: SmokeHttpResponse | None = None
    try:
        response = fetch()
        _validate_response_url(response, url_validator, host_error_code)
        if _response_status(response) not in accepted_statuses:
            raise SiteSmokeError("upstream_http")
        declared_length = _content_length(response.headers)
        if declared_length is not None and declared_length > max_bytes:
            raise SiteSmokeError("response_too_large")
        body = _read_bounded(response, max_bytes)
        if not body:
            raise SiteSmokeError("parse_drift")
        if declared_length is not None and declared_length != len(body):
            raise SiteSmokeError("parse_drift")
        content_type = (_header(response.headers, "content-type") or "").split(
            ";", 1
        )[0].strip().lower()
        return body, content_type, response.url
    except SiteSmokeError:
        raise
    except TimeoutError as exc:
        raise SiteSmokeError("timeout") from exc
    except OSError as exc:
        raise SiteSmokeError("connection_failed") from exc
    except Exception as exc:  # noqa: BLE001 - transport details remain private.
        raise SiteSmokeError("internal_error") from exc
    finally:
        _close_response(response)


def _read_bounded(response: SmokeHttpResponse, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = response.read(max_bytes + 1 - total)
        if not isinstance(chunk, bytes):
            raise SiteSmokeError("parse_drift")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        raise SiteSmokeError("response_too_large")
    return b"".join(chunks)


def _validate_response_url(
    response: SmokeHttpResponse,
    validator: Callable[[str], None],
    error_code: str,
) -> None:
    final_url = getattr(response, "url", None)
    if not isinstance(final_url, str) or not final_url:
        raise SiteSmokeError(error_code)
    _apply_url_validator(final_url, validator, error_code)


def _apply_url_validator(
    url: str,
    validator: Callable[[str], None],
    error_code: str,
) -> None:
    try:
        validator(url)
    except Exception as exc:  # noqa: BLE001 - URL details remain private.
        raise SiteSmokeError(error_code) from exc


def _response_status(response: SmokeHttpResponse) -> int:
    status = getattr(response, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise SiteSmokeError("upstream_http")
    return status


def _header(headers: Mapping[str, str], wanted: str) -> str | None:
    if not isinstance(headers, Mapping) or len(headers) > 64:
        raise SiteSmokeError("parse_drift")
    for name, value in headers.items():
        if str(name).strip().lower() != wanted:
            continue
        text = str(value or "").strip()
        if len(text) > 4096 or any(character in text for character in "\r\n\0"):
            raise SiteSmokeError("parse_drift")
        return text
    return None


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "content-length")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 20:
        raise SiteSmokeError("parse_drift")
    value = int(raw)
    if value < 0:
        raise SiteSmokeError("parse_drift")
    return value


def _validate_raster_image(body: bytes, content_type: str) -> None:
    if content_type not in _IMAGE_CONTENT_TYPES:
        raise SiteSmokeError("image_invalid")
    try:
        from PIL import Image, UnidentifiedImageError
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        raise SiteSmokeError("dependency_unavailable") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(BytesIO(body)) as image:
                width, height = image.size
                if (
                    isinstance(width, bool)
                    or isinstance(height, bool)
                    or not isinstance(width, int)
                    or not isinstance(height, int)
                    or not 1 <= width <= 16_384
                    or not 1 <= height <= 16_384
                    or width * height > 100_000_000
                ):
                    raise SiteSmokeError("image_invalid")
                image.verify()
    except SiteSmokeError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Warning) as exc:
        raise SiteSmokeError("image_invalid") from exc


def _validate_media_url(url: str) -> None:
    try:
        require_allowed_media_url(url, resolver=PublicHostResolver(max_hosts=1))
    except SafeMediaError as exc:
        raise SiteSmokeError("media_host_rejected") from exc


def _normalized_code(value: object) -> tuple[str, str]:
    normalized = normalize_catalog_code(value, max_length=40)
    if normalized is None:
        raise ValueError("site smoke catalog code is invalid")
    return normalized


def _failed_check(
    site: str,
    check: str,
    error_code: str,
    latency: int,
    *,
    code: str | None = None,
    count: int | None = None,
) -> SmokeCheck:
    return SmokeCheck(
        site=site,
        check=check,
        ok=False,
        latency=latency,
        error_code=error_code,
        code=code,
        count=count,
    )


def _exception_code(error: BaseException, *, default: str = "internal_error") -> str:
    if isinstance(error, SiteSmokeError):
        return error.error_code
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "connection_failed"
    return _validate_error_code(default)


def _validate_error_code(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in SMOKE_ERROR_CODES:
        raise ValueError("site smoke error code is invalid")
    return clean


def _monotonic_value(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("site smoke monotonic clock is invalid")
    return float(value)


def _elapsed_ms(started: float, monotonic: Callable[[], float]) -> int:
    elapsed = max(0.0, _monotonic_value(monotonic()) - started)
    return min(_MAX_LATENCY_MS, int(round(elapsed * 1000)))


def _close_response(response: SmokeHttpResponse | None) -> None:
    if response is None:
        return
    try:
        response.close()
    except Exception:  # noqa: BLE001 - cleanup remains best effort.
        pass


__all__ = [
    "JavBusSmokeAdapter",
    "JavDbSmokeAdapter",
    "MAX_SMOKE_MANIFEST_BYTES",
    "MAX_SMOKE_MEDIA_SAMPLE_BYTES",
    "MissavSmokeAdapter",
    "MissavSmokeObservation",
    "SMOKE_PUBLIC_FIELDS",
    "SearchSiteObservation",
    "SiteSmokeError",
    "SmokeCheck",
    "SmokeStateSnapshot",
    "fetch_range_sample",
    "run_site_smoke",
    "smoke_public_payload",
]
