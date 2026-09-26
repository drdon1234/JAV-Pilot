from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..missav.browser_gate import MISSAV_BROWSER_GATE


DIAGNOSTIC_STAGES = (
    "configuration",
    "dns",
    "connection",
    "search",
    "detail",
    "image",
    "quality",
    "manifest",
)
DIAGNOSTIC_STATUSES = ("ok", "failed", "deferred")
DIAGNOSTIC_ERROR_CODES = frozenset(
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
        "busy",
    }
)

_SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_LATENCY_MS = 180_000


class SiteDiagnosticError(RuntimeError):
    """A deliberately structure-only probe failure."""

    def __init__(self, error_code: str) -> None:
        self.error_code = _validate_error_code(error_code)
        super().__init__(self.error_code)


@dataclass(frozen=True)
class DiagnosticResult:
    site: str
    stage: str
    status: str
    checked_at: float
    latency_ms: int | None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _validate_site_id(self.site)
        _validate_stage(self.stage)
        if self.status not in DIAGNOSTIC_STATUSES:
            raise ValueError("site diagnostic status is invalid")
        if (
            isinstance(self.checked_at, bool)
            or not isinstance(self.checked_at, (int, float))
            or not math.isfinite(float(self.checked_at))
            or self.checked_at < 0
        ):
            raise ValueError("site diagnostic timestamp is invalid")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or not 0 <= self.latency_ms <= _MAX_LATENCY_MS
        ):
            raise ValueError("site diagnostic latency is invalid")
        if self.status == "ok" and self.error_code is not None:
            raise ValueError("successful site diagnostic cannot contain an error")
        if self.status != "ok":
            _validate_error_code(self.error_code)
        if self.status == "deferred" and self.error_code != "busy":
            raise ValueError("deferred site diagnostic must report busy")
        if self.error_code == "busy" and self.status != "deferred":
            raise ValueError("busy site diagnostic must be deferred")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def public_dict(self) -> dict[str, object]:
        return {
            "site": self.site,
            "stage": self.stage,
            "status": self.status,
            "ok": self.ok,
            "checked_at": float(self.checked_at),
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class DiagnosticStatus:
    site: str
    stage: str
    last_checked_at: float
    last_success_at: float | None
    last_latency_ms: int
    consecutive_failures: int
    last_error_code: str | None

    def __post_init__(self) -> None:
        _validate_site_id(self.site)
        _validate_stage(self.stage)
        for value in (self.last_checked_at, self.last_success_at):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError("site diagnostic timestamp is invalid")
        if (
            self.last_success_at is not None
            and self.last_success_at > self.last_checked_at
        ):
            raise ValueError("site diagnostic success timestamp is invalid")
        if (
            isinstance(self.last_latency_ms, bool)
            or not isinstance(self.last_latency_ms, int)
            or not 0 <= self.last_latency_ms <= _MAX_LATENCY_MS
        ):
            raise ValueError("site diagnostic latency is invalid")
        if (
            isinstance(self.consecutive_failures, bool)
            or not isinstance(self.consecutive_failures, int)
            or not 0 <= self.consecutive_failures <= 1_000_000
        ):
            raise ValueError("site diagnostic failure count is invalid")
        if self.last_error_code is not None:
            _validate_error_code(self.last_error_code)
        if (self.consecutive_failures == 0) != (self.last_error_code is None):
            raise ValueError("site diagnostic failure state is inconsistent")

    def public_dict(self) -> dict[str, object]:
        return {
            "site": self.site,
            "stage": self.stage,
            "last_checked_at": self.last_checked_at,
            "last_success_at": self.last_success_at,
            "last_latency_ms": self.last_latency_ms,
            "consecutive_failures": self.consecutive_failures,
            "last_error_code": self.last_error_code,
        }


class SiteDiagnosticStore(Protocol):
    def record(self, result: DiagnosticResult) -> DiagnosticStatus | None: ...

    def list(self, *, site: str | None = None) -> list[DiagnosticStatus]: ...


class SiteProbeAdapter(Protocol):
    site_id: str
    stages: Sequence[str]
    missav_browser_stages: Sequence[str]

    def probe(self, stage: str) -> None: ...


class BackgroundBrowserGate(Protocol):
    def acquire_background(self, *, blocking: bool) -> object | None: ...


@dataclass(frozen=True)
class FunctionalProbeAdapter:
    site_id: str
    stages: Sequence[str]
    probe_stage: Callable[[str], None]
    missav_browser_stages: Sequence[str] = ()

    def __post_init__(self) -> None:
        _validate_site_id(self.site_id)
        clean_stages = _validate_stages(self.stages)
        browser_stages = (
            _validate_stages(self.missav_browser_stages)
            if self.missav_browser_stages
            else ()
        )
        if not set(browser_stages).issubset(clean_stages):
            raise ValueError("browser stages must be supported by the adapter")
        if browser_stages and self.site_id != "missav":
            raise ValueError("only MissAV may use the MissAV browser gate")
        object.__setattr__(self, "stages", clean_stages)
        object.__setattr__(self, "missav_browser_stages", browser_stages)

    def probe(self, stage: str) -> None:
        self.probe_stage(_validate_stage(stage))


@dataclass(frozen=True)
class AutomaticProbeOutcome:
    results: tuple[DiagnosticResult, ...]
    next_delay_seconds: float


class SiteDiagnosticService:
    def __init__(
        self,
        store: SiteDiagnosticStore,
        adapters: Sequence[SiteProbeAdapter],
        *,
        browser_gate: BackgroundBrowserGate = MISSAV_BROWSER_GATE,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._adapters: dict[str, SiteProbeAdapter] = {}
        for adapter in adapters:
            site_id = _validate_site_id(adapter.site_id)
            if site_id in self._adapters:
                raise ValueError("duplicate site diagnostic adapter")
            _validate_stages(adapter.stages)
            browser_stages = (
                _validate_stages(adapter.missav_browser_stages)
                if adapter.missav_browser_stages
                else ()
            )
            if browser_stages and site_id != "missav":
                raise ValueError("only MissAV may use the MissAV browser gate")
            if not set(browser_stages).issubset(adapter.stages):
                raise ValueError("browser stages must be supported by the adapter")
            self._adapters[site_id] = adapter
        self._browser_gate = browser_gate
        self._clock = clock
        self._monotonic = monotonic

    def probe_manual(
        self,
        site: str,
        *,
        stages: Sequence[str] | None = None,
    ) -> tuple[DiagnosticResult, ...]:
        return self._probe(site, stages=stages)

    def probe_automatic(
        self,
        site: str,
        *,
        stages: Sequence[str] | None = None,
        base_delay_seconds: float = 900.0,
        max_delay_seconds: float = 21_600.0,
    ) -> AutomaticProbeOutcome:
        results = self._probe(site, stages=stages)
        return AutomaticProbeOutcome(
            results=results,
            next_delay_seconds=self.automatic_delay_seconds(
                site,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            ),
        )

    def automatic_delay_seconds(
        self,
        site: str,
        *,
        base_delay_seconds: float = 900.0,
        max_delay_seconds: float = 21_600.0,
    ) -> float:
        site_id = _validate_site_id(site)
        base = _bounded_delay(base_delay_seconds)
        maximum = _bounded_delay(max_delay_seconds)
        if maximum < base:
            raise ValueError("site diagnostic maximum delay is invalid")
        statuses = self.store.list(site=site_id)
        failures = max(
            (status.consecutive_failures for status in statuses),
            default=0,
        )
        return min(maximum, base * (2 ** min(failures, 10)))

    def _probe(
        self,
        site: str,
        *,
        stages: Sequence[str] | None,
    ) -> tuple[DiagnosticResult, ...]:
        site_id = _validate_site_id(site)
        adapter = self._adapters.get(site_id)
        if adapter is None:
            raise ValueError("site diagnostic adapter is unavailable")
        requested = (
            _validate_stages(stages)
            if stages is not None
            else _validate_stages(adapter.stages)
        )
        if not set(requested).issubset(adapter.stages):
            raise ValueError("site diagnostic stage is unsupported")

        browser_stages = set(adapter.missav_browser_stages).intersection(requested)
        permit: object | None = None
        browser_deferred = False
        if browser_stages:
            try:
                permit = self._browser_gate.acquire_background(blocking=False)
            except Exception:  # noqa: BLE001 - gate internals are never exposed.
                permit = None
            browser_deferred = permit is None

        results: list[DiagnosticResult] = []
        try:
            for stage in requested:
                if stage in browser_stages and browser_deferred:
                    result = DiagnosticResult(
                        site=site_id,
                        stage=stage,
                        status="deferred",
                        checked_at=_safe_timestamp(self._clock()),
                        latency_ms=None,
                        error_code="busy",
                    )
                else:
                    result = self._run_stage(adapter, site_id, stage)
                results.append(result)
                if result.status != "deferred":
                    self.store.record(result)
        finally:
            if permit is not None:
                release = getattr(permit, "release", None)
                if callable(release):
                    release()
        return tuple(results)

    def _run_stage(
        self,
        adapter: SiteProbeAdapter,
        site_id: str,
        stage: str,
    ) -> DiagnosticResult:
        started = _safe_monotonic(self._monotonic())
        status = "ok"
        error_code: str | None = None
        try:
            adapter.probe(stage)
        except SiteDiagnosticError as exc:
            status = "deferred" if exc.error_code == "busy" else "failed"
            error_code = exc.error_code
        except TimeoutError:
            status = "failed"
            error_code = "timeout"
        except OSError:
            status = "failed"
            error_code = "connection_failed"
        except Exception:  # noqa: BLE001 - upstream exception text is never exposed.
            status = "failed"
            error_code = "internal_error"
        elapsed = max(0.0, _safe_monotonic(self._monotonic()) - started)
        latency_ms = min(_MAX_LATENCY_MS, int(round(elapsed * 1000)))
        return DiagnosticResult(
            site=site_id,
            stage=stage,
            status=status,
            checked_at=_safe_timestamp(self._clock()),
            latency_ms=latency_ms,
            error_code=error_code,
        )


def _validate_site_id(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _SITE_ID_RE.fullmatch(clean):
        raise ValueError("site diagnostic site is invalid")
    return clean


def _validate_stage(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in DIAGNOSTIC_STAGES:
        raise ValueError("site diagnostic stage is invalid")
    return clean


def _validate_stages(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values or len(values) > len(
        DIAGNOSTIC_STAGES
    ):
        raise ValueError("site diagnostic stages are invalid")
    result: list[str] = []
    for value in values:
        stage = _validate_stage(value)
        if stage in result:
            raise ValueError("site diagnostic stages contain duplicates")
        result.append(stage)
    return tuple(result)


def _validate_error_code(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in DIAGNOSTIC_ERROR_CODES:
        raise ValueError("site diagnostic error code is invalid")
    return clean


def _safe_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError("site diagnostic clock is invalid")
    return float(value)


def _safe_monotonic(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("site diagnostic monotonic clock is invalid")
    return float(value)


def _bounded_delay(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("site diagnostic delay is invalid") from exc
    if not math.isfinite(parsed) or not 1.0 <= parsed <= 7 * 24 * 60 * 60:
        raise ValueError("site diagnostic delay is invalid")
    return parsed


__all__ = [
    "AutomaticProbeOutcome",
    "DIAGNOSTIC_ERROR_CODES",
    "DIAGNOSTIC_STAGES",
    "DiagnosticResult",
    "DiagnosticStatus",
    "FunctionalProbeAdapter",
    "SiteDiagnosticError",
    "SiteDiagnosticService",
]
