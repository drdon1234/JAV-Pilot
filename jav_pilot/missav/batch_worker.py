from __future__ import annotations

import json
import sys

MAX_INPUT_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
PROTOCOL_VERSION = 2
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "challenge_active",
        "navigation_timeout",
        "rate_limited",
        "upstream_unavailable",
    }
)
_PERMANENT_ERROR_CODES = frozenset(
    {
        "dependency_unavailable",
        "discovery_unavailable",
        "not_found",
        "parse_drift",
        "route_drift",
        "safety_rejected",
    }
)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid batch instruction")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> object:
    raise ValueError("invalid batch instruction")


def _write_response(response: dict[str, object]) -> None:
    encoded = json.dumps(
        response,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    if len(encoded) + 1 > MAX_OUTPUT_BYTES:
        raise ValueError("batch response is too large")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _write_error(code: str, *, retryable: bool) -> None:
    _write_response(
        {
            "protocol": PROTOCOL_VERSION,
            "ok": False,
            "error": {"code": code, "retryable": retryable},
        }
    )


def _worker_error_code(error: BaseException, *, retryable: bool) -> str:
    allowed = _RETRYABLE_ERROR_CODES if retryable else _PERMANENT_ERROR_CODES
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in allowed:
        return code
    return "upstream_unavailable" if retryable else "discovery_unavailable"


def _read_instruction() -> dict[str, object]:
    raw = sys.stdin.buffer.readline(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid batch instruction")
    if sys.stdin.buffer.read(1):
        raise ValueError("invalid batch instruction")
    payload = json.loads(
        raw.decode("ascii"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )
    if not isinstance(payload, dict) or set(payload) != {
        "prefix",
        "suffix_width",
        "start",
        "end",
        "max_codes",
        "timeout_seconds",
    }:
        raise ValueError("invalid batch instruction")
    return payload


def main() -> int:
    try:
        payload = _read_instruction()
    except Exception:
        _write_error("invalid_instruction", retryable=False)
        return 0

    try:
        from .client import discover_series_codes
        from .errors import MissavError, MissavTransientError
    except Exception:
        _write_error("dependency_unavailable", retryable=False)
        return 0

    try:
        result = discover_series_codes(
            payload["prefix"],
            suffix_width=payload["suffix_width"],
            start=payload["start"],
            end=payload["end"],
            max_codes=payload["max_codes"],
            timeout_seconds=payload["timeout_seconds"],
        )
        _write_response(
            {
                "protocol": PROTOCOL_VERSION,
                "ok": True,
                "codes": list(result.codes),
                "items": [
                    {"code": code, "variants": list(variants)}
                    for code, variants in result.variants_by_code
                ],
                "complete": result.complete,
            }
        )
        return 0
    except MissavTransientError as exc:
        _write_error(_worker_error_code(exc, retryable=True), retryable=True)
        return 0
    except MissavError as exc:
        _write_error(_worker_error_code(exc, retryable=False), retryable=False)
        return 0
    except Exception:
        _write_error("internal_failure", retryable=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
