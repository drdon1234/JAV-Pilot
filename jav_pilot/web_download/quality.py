from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Literal, Sequence, TypeAlias

from ..core.catalog_code import canonical_catalog_code
from .variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
)


__all__ = [
    "MAX_QUALITY_HEIGHT",
    "MIN_QUALITY_HEIGHT",
    "QualityDiscoveryError",
    "QualityDiscoveryCancelled",
    "QualityDiscoveryNotFound",
    "QualityDiscoveryTimeout",
    "QualityHeightError",
    "SubprocessQualityDiscovery",
    "VariantOptionStatus",
    "WebDownloadVariantOption",
    "discover_web_download_qualities",
    "height_matches_selected_quality",
    "normalize_quality_heights",
    "normalize_quality_discovery_code",
    "validate_quality_height",
    "validate_selected_height",
]

MIN_QUALITY_HEIGHT = 144
MAX_QUALITY_HEIGHT = 4320
_QUALITY_TIER_HEIGHTS = (240, 360, 480, 720, 1080, 1440, 2160, 4320)
_MAX_WORKER_OUTPUT_BYTES = 4096
_DISPLAY_CODE_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")
_ALLOWED_WORKER_ERRORS = frozenset(("not_found", "discovery_failed", "invalid_request"))

VariantOptionStatus: TypeAlias = Literal["available", "not_found", "failed"]


class QualityHeightError(ValueError):
    pass


class QualityDiscoveryError(RuntimeError):
    def __init__(self, message: str = "Web download quality discovery failed") -> None:
        super().__init__(message)


class QualityDiscoveryCancelled(QualityDiscoveryError):
    def __init__(
        self, message: str = "Web download quality discovery was cancelled"
    ) -> None:
        super().__init__(message)


class QualityDiscoveryNotFound(QualityDiscoveryError):
    def __init__(
        self,
        message: str = "MissAV has no exact result for this catalog code",
    ) -> None:
        super().__init__(message)


class QualityDiscoveryTimeout(QualityDiscoveryError):
    def __init__(
        self, message: str = "Web download quality discovery timed out"
    ) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WebDownloadVariantOption:
    variant: MissavVariant
    status: VariantOptionStatus
    heights: tuple[int, ...]

    def __post_init__(self) -> None:
        try:
            clean_variant = normalize_web_download_variant(self.variant)
            clean_heights = normalize_quality_heights(self.heights)
        except (TypeError, ValueError, QualityHeightError) as exc:
            raise QualityDiscoveryError("Web download options are invalid") from exc
        if self.status not in {"available", "not_found", "failed"}:
            raise QualityDiscoveryError("Web download options are invalid")
        if (self.status == "available") != bool(clean_heights):
            raise QualityDiscoveryError("Web download options are invalid")
        object.__setattr__(self, "variant", clean_variant)
        object.__setattr__(self, "heights", clean_heights)


def validate_quality_height(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_QUALITY_HEIGHT <= value <= MAX_QUALITY_HEIGHT
    ):
        raise QualityHeightError("web download quality height is invalid")
    return value


def validate_selected_height(value: object) -> int:
    return validate_quality_height(value)


def normalize_quality_heights(values: Iterable[object]) -> tuple[int, ...]:
    return tuple(
        sorted(
            {validate_quality_height(value) for value in values},
            reverse=True,
        )
    )


def height_matches_selected_quality(
    actual_height: object, selected_height: object
) -> bool:
    try:
        actual = validate_quality_height(actual_height)
        selected = validate_quality_height(selected_height)
    except QualityHeightError:
        return False
    crop_tolerance = max(16, round(selected * 0.10))
    next_tier = next(
        (height for height in _QUALITY_TIER_HEIGHTS if height > selected),
        MAX_QUALITY_HEIGHT + 1,
    )
    return selected - crop_tolerance <= actual < next_tier


def normalize_quality_discovery_code(raw_code: object) -> str:
    if canonical_catalog_code(raw_code, max_length=40) is None:
        raise QualityDiscoveryError("Invalid catalog code")
    normalized = unicodedata.normalize("NFKC", str(raw_code)).strip().upper()
    if not normalized.isascii() or not _DISPLAY_CODE_RE.fullmatch(normalized):
        raise QualityDiscoveryError("Invalid catalog code")
    if "-" not in normalized:
        match = re.fullmatch(r"([A-Z]{2,12})(\d{2,8})", normalized)
        if match:
            normalized = f"{match.group(1)}-{match.group(2)}"
    if len(normalized) > 40:
        raise QualityDiscoveryError("Invalid catalog code")
    return normalized


class SubprocessQualityDiscovery:
    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        discovery_timeout_seconds: float | None = None,
        process_timeout_seconds: float | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.command = tuple(command) if command is not None else None
        if self.command is not None and (
            not self.command
            or any(not isinstance(part, str) or not part for part in self.command)
        ):
            raise QualityDiscoveryError("Quality discovery worker command is invalid")
        self.discovery_timeout_seconds = _bounded_timeout(
            discovery_timeout_seconds
            if discovery_timeout_seconds is not None
            else os.environ.get("JAV_PILOT_WEB_DOWNLOAD_CAPTURE_TIMEOUT_SECONDS", "45")
        )
        default_process_timeout = self.discovery_timeout_seconds + 15.0
        self.process_timeout_seconds = _bounded_process_timeout(
            process_timeout_seconds
            if process_timeout_seconds is not None
            else default_process_timeout,
            minimum=self.discovery_timeout_seconds,
        )
        self._popen_factory = popen_factory

    def discover(
        self,
        code: object,
        *,
        variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, ...]:
        try:
            clean_variant = normalize_web_download_variant(variant)
        except ValueError as exc:
            raise QualityDiscoveryError("Web download variant is invalid") from exc
        options = self._discover(
            code,
            variant=clean_variant,
            expected_variants=(clean_variant,),
            cancel_event=cancel_event,
        )
        option = options[0]
        if option.status == "not_found":
            raise QualityDiscoveryNotFound()
        if option.status != "available":
            raise QualityDiscoveryError()
        return option.heights

    def _discover(
        self,
        code: object,
        *,
        variant: MissavVariant | None,
        expected_variants: tuple[MissavVariant, ...],
        cancel_event: threading.Event | None,
    ) -> tuple[WebDownloadVariantOption, ...]:
        normalized_code = normalize_quality_discovery_code(code)
        if cancel_event is not None and cancel_event.is_set():
            raise QualityDiscoveryCancelled()
        command = self.command or _default_worker_command()
        task = json.dumps(
            {
                "code": normalized_code,
                "timeout_seconds": self.discovery_timeout_seconds,
                "variant": variant,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        process: subprocess.Popen[bytes] | None = None
        try:
            kwargs: dict[str, object] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "shell": False,
                "bufsize": 0,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            process = self._popen_factory(list(command), **kwargs)
            stdout = self._communicate(
                process,
                task,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise QualityDiscoveryCancelled()
        except QualityDiscoveryError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            if process is not None:
                self._terminate_process_group(process)
            raise QualityDiscoveryError(
                "Quality discovery worker is unavailable"
            ) from exc

        if len(stdout) > _MAX_WORKER_OUTPUT_BYTES:
            raise QualityDiscoveryError()
        try:
            response = json.loads(stdout.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QualityDiscoveryError() from exc
        return _validate_worker_response(
            response,
            expected_code=normalized_code,
            expected_variants=expected_variants,
            return_code=process.returncode,
        )

    def _communicate(
        self,
        process: subprocess.Popen[bytes],
        task: bytes,
        *,
        cancel_event: threading.Event | None,
    ) -> bytes:
        deadline = time.monotonic() + self.process_timeout_seconds
        pending_input: bytes | None = task
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_and_drain(process)
                raise QualityDiscoveryCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_and_drain(process)
                raise QualityDiscoveryTimeout()
            try:
                stdout, _ = process.communicate(
                    input=pending_input,
                    timeout=min(0.1, remaining),
                )
                return stdout
            except subprocess.TimeoutExpired as exc:
                pending_input = None
                if time.monotonic() >= deadline:
                    self._terminate_and_drain(process)
                    raise QualityDiscoveryTimeout() from exc

    def _terminate_and_drain(self, process: subprocess.Popen[bytes]) -> None:
        self._terminate_process_group(process)
        try:
            process.communicate(timeout=1.0)
        except (OSError, subprocess.SubprocessError):
            pass

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError, ValueError):
            try:
                process.terminate()
            except OSError:
                return
        try:
            process.wait(timeout=2.0)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, ValueError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def discover_web_download_qualities(
    code: object,
    *,
    variant: object = DEFAULT_WEB_DOWNLOAD_VARIANT,
    cancel_event: threading.Event | None = None,
) -> tuple[int, ...]:
    return SubprocessQualityDiscovery().discover(
        code,
        variant=variant,
        cancel_event=cancel_event,
    )


def _default_worker_command() -> tuple[str, ...]:
    worker = (sys.executable, "-m", "jav_pilot.web_download.quality_worker")
    if os.name == "nt" or sys.platform == "darwin" or os.environ.get("DISPLAY"):
        return worker
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run:
        return (xvfb_run, "-a", *worker)
    raise QualityDiscoveryError(
        "Quality discovery requires xvfb-run when no display is available"
    )


def _validate_worker_response(
    response: object,
    *,
    expected_code: str,
    expected_variants: tuple[MissavVariant, ...],
    return_code: int | None,
) -> tuple[WebDownloadVariantOption, ...]:
    if not isinstance(response, dict) or set(response) != {"code", "options", "error"}:
        raise QualityDiscoveryError()
    if response["code"] != expected_code or not isinstance(response["options"], list):
        raise QualityDiscoveryError()
    error = response["error"]
    if error is not None:
        if (
            error not in _ALLOWED_WORKER_ERRORS
            or response["options"]
            or return_code == 0
        ):
            raise QualityDiscoveryError()
        if error == "not_found":
            raise QualityDiscoveryNotFound()
        raise QualityDiscoveryError()
    if return_code != 0:
        raise QualityDiscoveryError()
    raw_options = response["options"]
    if len(raw_options) != len(expected_variants):
        raise QualityDiscoveryError()
    options: list[WebDownloadVariantOption] = []
    for raw_option, expected_variant in zip(
        raw_options, expected_variants, strict=True
    ):
        if (
            not isinstance(raw_option, dict)
            or set(raw_option) != {"variant", "status", "heights"}
            or raw_option["variant"] != expected_variant
            or not isinstance(raw_option["heights"], list)
        ):
            raise QualityDiscoveryError()
        try:
            option = WebDownloadVariantOption(
                variant=normalize_web_download_variant(raw_option["variant"]),
                status=raw_option["status"],  # type: ignore[arg-type]
                heights=tuple(raw_option["heights"]),
            )
        except (TypeError, ValueError, QualityDiscoveryError) as exc:
            raise QualityDiscoveryError() from exc
        options.append(option)
    return tuple(options)


def _bounded_timeout(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise QualityDiscoveryError("Quality discovery timeout is invalid") from exc
    if not 1.0 <= parsed <= 180.0:
        raise QualityDiscoveryError("Quality discovery timeout is invalid")
    return parsed


def _bounded_process_timeout(value: object, *, minimum: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise QualityDiscoveryError(
            "Quality discovery process timeout is invalid"
        ) from exc
    if not minimum <= parsed <= 195.0:
        raise QualityDiscoveryError("Quality discovery process timeout is invalid")
    return parsed
