from __future__ import annotations

import json
import sys
import time

from ..missav.client import discover_qualities
from ..missav.errors import MissavError, MissavNotFound
from .quality import (
    QualityDiscoveryError,
    normalize_quality_discovery_code,
    normalize_quality_heights,
)
from .variant import MissavVariant, normalize_web_download_variant

_MAX_TASK_BYTES = 1024


def main() -> int:
    code = ""
    try:
        payload = _read_task()
        code = normalize_quality_discovery_code(payload.get("code"))
        timeout_seconds = _read_timeout(payload.get("timeout_seconds"))
        variant = normalize_web_download_variant(payload.get("variant"))
    except (QualityDiscoveryError, ValueError):
        _emit(code, (), "invalid_request")
        return 2

    options: list[tuple[MissavVariant, str, tuple[int, ...]]] = []
    deadline = time.monotonic() + timeout_seconds
    remaining = deadline - time.monotonic()
    if remaining < 1.0:
        options.append((variant, "failed", ()))
    else:
        try:
            heights = normalize_quality_heights(
                discover_qualities(
                    code,
                    variant=variant,
                    timeout_seconds=min(remaining, 180.0),
                )
            )
            if not heights:
                raise MissavError("MissAV did not expose selectable video qualities")
        except MissavNotFound:
            options.append((variant, "not_found", ()))
        except BaseException:  # noqa: BLE001 - never expose browser or request details.
            options.append((variant, "failed", ()))
        else:
            options.append((variant, "available", heights))
    _emit(code, tuple(options), None)
    return 0


def _read_task() -> dict[str, object]:
    raw = sys.stdin.buffer.read(_MAX_TASK_BYTES + 1)
    if len(raw) > _MAX_TASK_BYTES:
        raise QualityDiscoveryError()
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualityDiscoveryError() from exc
    if not isinstance(payload, dict) or set(payload) != {
        "code",
        "timeout_seconds",
        "variant",
    }:
        raise QualityDiscoveryError()
    return payload


def _read_timeout(value: object) -> float:
    if isinstance(value, bool):
        raise QualityDiscoveryError()
    try:
        timeout_seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise QualityDiscoveryError() from exc
    if not 1.0 <= timeout_seconds <= 180.0:
        raise QualityDiscoveryError()
    return timeout_seconds


def _emit(
    code: str,
    options: tuple[tuple[MissavVariant, str, tuple[int, ...]], ...],
    error: str | None,
) -> None:
    response = {
        "code": code,
        "options": [
            {"variant": variant, "status": status, "heights": list(heights)}
            for variant, status, heights in options
        ],
        "error": error,
    }
    raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
    sys.stdout.buffer.write(raw.encode("ascii"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
