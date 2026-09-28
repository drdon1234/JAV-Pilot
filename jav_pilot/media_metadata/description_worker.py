from __future__ import annotations

import json
import re
import sys
import unicodedata

from ..core.catalog_code import canonical_catalog_code
from ..missav.client import fetch_description
from ..missav.errors import MissavNotFound
from ..web_download.variant import (
    DEFAULT_WEB_DOWNLOAD_VARIANT,
    MissavVariant,
    normalize_web_download_variant,
)


_MAX_TASK_BYTES = 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_DISPLAY_CODE_RE = re.compile(r"^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$")


class DescriptionWorkerRequestError(ValueError):
    pass


def main() -> int:
    code = ""
    variant = DEFAULT_WEB_DOWNLOAD_VARIANT
    try:
        payload = _read_task()
        code = _validated_code(payload.get("code"))
        variant = _validated_variant(payload.get("variant"))
        timeout_seconds = _read_timeout(payload.get("timeout_seconds"))
        description = fetch_description(
            code,
            variant=variant,
            timeout_seconds=timeout_seconds,
        )
    except MissavNotFound:
        _emit(code, variant, None, "not_found")
        return 3
    except DescriptionWorkerRequestError:
        _emit(code, variant, None, "invalid_request")
        return 2
    except BaseException:  # noqa: BLE001 - never expose browser or request details.
        _emit(code, variant, None, "description_failed")
        return 1
    _emit(code, variant, description, None)
    return 0


def _read_task() -> dict[str, object]:
    raw = sys.stdin.buffer.read(_MAX_TASK_BYTES + 1)
    if len(raw) > _MAX_TASK_BYTES:
        raise DescriptionWorkerRequestError()
    try:
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DescriptionWorkerRequestError() from exc
    if not isinstance(payload, dict) or set(payload) != {
        "code",
        "variant",
        "timeout_seconds",
    }:
        raise DescriptionWorkerRequestError()
    return payload


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _validated_code(value: object) -> str:
    code = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if (
        canonical_catalog_code(code, max_length=32) is None
        or not code.isascii()
        or not _DISPLAY_CODE_RE.fullmatch(code)
    ):
        raise DescriptionWorkerRequestError()
    if "-" not in code:
        match = re.fullmatch(r"([A-Z]{2,12})(\d{2,8})", code)
        if match:
            code = f"{match.group(1)}-{match.group(2)}"
    return code


def _validated_variant(value: object) -> MissavVariant:
    try:
        return normalize_web_download_variant(value)
    except ValueError as exc:
        raise DescriptionWorkerRequestError() from exc


def _read_timeout(value: object) -> float:
    if isinstance(value, bool):
        raise DescriptionWorkerRequestError()
    try:
        timeout_seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise DescriptionWorkerRequestError() from exc
    if not 1.0 <= timeout_seconds <= 180.0:
        raise DescriptionWorkerRequestError()
    return timeout_seconds


def _emit(
    code: str,
    variant: MissavVariant,
    description: str | None,
    error: str | None,
) -> None:
    response = {
        "code": code,
        "variant": variant,
        "description": description,
        "error": error,
    }
    raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
    encoded = raw.encode("ascii")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        fallback = {
            "code": code,
            "variant": variant,
            "description": None,
            "error": "description_failed",
        }
        encoded = (
            json.dumps(fallback, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
