"""MissAV error types."""

from __future__ import annotations

from ..core.guards import contains_sensitive_transport_text

__all__ = [
    "MissavError",
    "MissavNotFound",
    "MissavTransientError",
]


_MISSAV_ERROR_CODES = frozenset(
    {
        "challenge_active",
        "dependency_unavailable",
        "discovery_unavailable",
        "navigation_timeout",
        "not_found",
        "parse_drift",
        "rate_limited",
        "route_drift",
        "safety_rejected",
        "transport_reset",
        "upstream_unavailable",
    }
)


class MissavError(RuntimeError):
    """A sanitized MissAV failure with a bounded worker-facing reason code."""

    default_code = "discovery_unavailable"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved_code = code or self.default_code
        if resolved_code not in _MISSAV_ERROR_CODES:
            raise ValueError("MissAV error code is invalid")
        # Keep accidental transport details (manifest URLs, cookies, signed
        # headers, and tokens) out of exception text.  Callers may persist or
        # display the exception, so redaction belongs at this boundary rather
        # than relying on every worker to remember it.
        safe_message = str(message or "")
        if contains_sensitive_transport_text(safe_message):
            safe_message = "MissAV operation failed"
        super().__init__(safe_message)
        self.code = resolved_code


class MissavNotFound(MissavError):
    default_code = "not_found"


class MissavTransientError(MissavError):
    """A sanitized MissAV failure that is safe to retry once."""

    default_code = "upstream_unavailable"
    retryable = True
