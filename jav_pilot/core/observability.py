from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import math
import re
import secrets
import sys
import threading
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CORRELATION_RE = re.compile(r"[A-Za-z0-9_.-]{8,80}")
_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_SAFE_FIELD_NAMES = frozenset(
    {
        "changed",
        "count",
        "duration_ms",
        "error_code",
        "method",
        "outcome",
        "revision",
        "source",
        "status",
        "status_class",
    }
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_SAFE_SOURCES = frozenset(
    {"metadata_publish", "qb_organizer", "unknown", "web_archive"}
)
_MAX_EXCEPTION_FRAMES = 8
_correlation_id = contextvars.ContextVar(
    "jav_pilot_correlation_id", default="system0000"
)
_log_lock = threading.Lock()


def new_correlation_id() -> str:
    return secrets.token_hex(12)


def normalize_correlation_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _CORRELATION_RE.fullmatch(candidate) else new_correlation_id()


def current_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: object) -> contextvars.Token[str]:
    return _correlation_id.set(normalize_correlation_id(value))


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    _correlation_id.reset(token)


def emit_json_log(
    component: str,
    event: str,
    *,
    level: str = "info",
    correlation_id: str | None = None,
    **fields: object,
) -> None:
    payload = structured_log_record(
        component,
        event,
        level=level,
        correlation_id=correlation_id,
        fields=fields,
    )
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    with _log_lock:
        sys.stdout.write(f"{encoded}\n")
        sys.stdout.flush()


def structured_log_record(
    component: str,
    event: str,
    *,
    level: str,
    correlation_id: str | None,
    fields: Mapping[str, object],
) -> dict[str, object]:
    clean_component = _identifier(component, "component")
    clean_event = _identifier(event, "event")
    clean_level = str(level or "").strip().lower()
    if clean_level not in _LEVELS:
        raise ValueError("log level is invalid")
    unknown = set(fields) - _SAFE_FIELD_NAMES
    if unknown:
        raise ValueError("log field is not allowed")
    clean_fields = {key: _safe_log_value(key, value) for key, value in fields.items()}
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "level": clean_level,
        "component": clean_component,
        "event": clean_event,
        "correlation_id": normalize_correlation_id(
            correlation_id if correlation_id is not None else current_correlation_id()
        ),
        **clean_fields,
    }


class _StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        component = _identifier_from_logger(record.name)
        event = _identifier_from_message(record.msg)
        level = record.levelname.lower()
        if level not in _LEVELS:
            level = "info"
        payload = structured_log_record(
            component,
            event,
            level=level,
            correlation_id=None,
            fields={},
        )
        exception = _safe_exception_payload(record.exc_info)
        if exception is not None:
            payload["exception"] = exception
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_structured_logging(*, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    handler = next(
        (
            existing
            for existing in root.handlers
            if getattr(existing, "_jav_pilot_structured", False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._jav_pilot_structured = True  # type: ignore[attr-defined]
    # The application owns the root logging contract. Keeping handlers installed
    # by the launcher or an imported library would duplicate records and bypass
    # the field and exception redaction performed by this formatter.
    for existing in tuple(root.handlers):
        root.removeHandler(existing)
    handler.setFormatter(_StructuredFormatter())
    handler.setLevel(level)
    root.addHandler(handler)
    root.setLevel(level)


def _safe_exception_payload(
    exc_info: tuple[type[BaseException], BaseException, TracebackType]
    | tuple[None, None, None]
    | None,
) -> dict[str, object] | None:
    if not exc_info or exc_info[0] is None or exc_info[2] is None:
        return None
    exception_type = _identifier_from_logger(exc_info[0].__name__)
    frames: list[dict[str, object]] = []
    try:
        extracted = traceback.extract_tb(exc_info[2])[-_MAX_EXCEPTION_FRAMES:]
    except (AttributeError, TypeError, ValueError):
        extracted = []
    for frame in extracted:
        file_name = Path(frame.filename).name
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", file_name) is None:
            file_name = "runtime.py"
        frames.append(
            {
                "file": file_name,
                "function": _identifier_from_logger(frame.name),
                "line": max(0, min(int(frame.lineno), 10_000_000)),
            }
        )
    return {"type": exception_type, "frames": frames}


@dataclass(frozen=True)
class MetricSample:
    name: str
    value: int | float
    labels: tuple[tuple[str, str], ...] = ()
    help_text: str = "JAV Pilot runtime metric."
    metric_type: str = "gauge"

    def __post_init__(self) -> None:
        _identifier(self.name, "metric name", allow_prefix=True)
        if self.metric_type not in {"counter", "gauge"}:
            raise ValueError("metric type is invalid")
        numeric = float(self.value)
        if not math.isfinite(numeric):
            raise ValueError("metric value must be finite")
        for key, value in self.labels:
            _identifier(key, "metric label")
            _metric_label_value(value)


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_total: dict[tuple[str, str], int] = {}
        self._request_duration: dict[tuple[str, str], float] = {}
        self._retry_actions = 0
        self._sqlite_busy = 0

    def observe_request(self, method: object, status: object, duration: object) -> None:
        clean_method = str(method or "").strip().upper()
        if clean_method not in _SAFE_METHODS:
            clean_method = "OTHER"
        try:
            clean_status = int(status)
        except (TypeError, ValueError, OverflowError):
            clean_status = 500
        status_class = f"{min(5, max(1, clean_status // 100))}xx"
        try:
            elapsed = max(0.0, min(float(duration), 3600.0))
        except (TypeError, ValueError, OverflowError):
            elapsed = 0.0
        key = (clean_method.lower(), status_class)
        with self._lock:
            self._request_total[key] = self._request_total.get(key, 0) + 1
            self._request_duration[key] = self._request_duration.get(key, 0.0) + elapsed

    def record_retry_action(self) -> None:
        with self._lock:
            self._retry_actions += 1

    def record_sqlite_busy(self) -> None:
        with self._lock:
            self._sqlite_busy += 1

    def samples(self) -> list[MetricSample]:
        with self._lock:
            request_total = dict(self._request_total)
            request_duration = dict(self._request_duration)
            retries = self._retry_actions
            sqlite_busy = self._sqlite_busy
        samples = [
            MetricSample(
                "jav_pilot_web_download_retry_actions_total",
                retries,
                help_text="Explicit Web download retry and restart actions.",
                metric_type="counter",
            ),
            MetricSample(
                "jav_pilot_sqlite_busy_total",
                sqlite_busy,
                help_text="Observed bounded SQLite busy failures.",
                metric_type="counter",
            ),
        ]
        for key in sorted(request_total):
            labels = (("method", key[0]), ("status_class", key[1]))
            samples.extend(
                (
                    MetricSample(
                        "jav_pilot_http_requests_total",
                        request_total[key],
                        labels,
                        "Completed HTTP requests.",
                        "counter",
                    ),
                    MetricSample(
                        "jav_pilot_http_request_duration_seconds_total",
                        request_duration[key],
                        labels,
                        "Cumulative HTTP request duration.",
                        "counter",
                    ),
                )
            )
        return samples


def render_prometheus(samples: Iterable[MetricSample]) -> str:
    grouped: dict[str, list[MetricSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.name, []).append(sample)
    lines: list[str] = []
    for name in sorted(grouped):
        values = grouped[name]
        first = values[0]
        lines.append(f"# HELP {name} {_prometheus_help(first.help_text)}")
        lines.append(f"# TYPE {name} {first.metric_type}")
        for sample in sorted(values, key=lambda item: item.labels):
            label_text = ""
            if sample.labels:
                encoded = ",".join(
                    f'{key}="{_escape_label(value)}"' for key, value in sample.labels
                )
                label_text = f"{{{encoded}}}"
            lines.append(f"{name}{label_text} {_number(sample.value)}")
    return ("\n".join(lines) + "\n") if lines else ""


def _identifier(value: object, label: str, *, allow_prefix: bool = False) -> str:
    candidate = str(value or "").strip().lower()
    pattern = (
        re.compile(r"[a-z][a-z0-9_:]{0,127}") if allow_prefix else _IDENTIFIER_RE
    )
    if not pattern.fullmatch(candidate):
        raise ValueError(f"{label} is invalid")
    return candidate


def _safe_log_value(key: str, value: object) -> object:
    if key == "method":
        method = str(value or "").strip().upper()
        return method if method in _SAFE_METHODS else "OTHER"
    if key == "source":
        source = str(value or "").strip().lower()
        return source if source in _SAFE_SOURCES else "unknown"
    if key in {"error_code", "outcome", "revision", "status_class"}:
        candidate = str(value or "").strip().lower()
        return candidate if re.fullmatch(r"[a-z0-9_.-]{1,80}", candidate) else "unknown"
    if key == "changed":
        return value if isinstance(value, bool) else False
    if key in {"count", "duration_ms", "status"}:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0
    raise ValueError("log field is not allowed")


def _identifier_from_logger(value: object) -> str:
    candidate = str(value or "").rsplit(".", 1)[-1].lower()
    candidate = re.sub(r"[^a-z0-9]+", "_", candidate).strip("_")
    if not candidate or not candidate[0].isalpha():
        return "runtime"
    return candidate[:64]


def _identifier_from_message(value: object) -> str:
    candidate = str(value or "").split("%", 1)[0].lower()
    candidate = re.sub(r"[^a-z0-9]+", "_", candidate).strip("_")
    if not candidate or not candidate[0].isalpha():
        return "runtime_event"
    return candidate[:64]


def _metric_label_value(value: object) -> str:
    candidate = str(value or "")
    if not re.fullmatch(r"[a-z0-9_.-]{1,64}", candidate):
        raise ValueError("metric label value is invalid")
    return candidate


def _prometheus_help(value: object) -> str:
    return str(value or "JAV Pilot runtime metric.").replace("\\", " ").replace("\n", " ")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: int | float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else format(numeric, ".12g")


RUNTIME_METRICS = RuntimeMetrics()


__all__ = [
    "MetricSample",
    "RUNTIME_METRICS",
    "RuntimeMetrics",
    "configure_structured_logging",
    "current_correlation_id",
    "emit_json_log",
    "new_correlation_id",
    "normalize_correlation_id",
    "render_prometheus",
    "reset_correlation_id",
    "set_correlation_id",
    "structured_log_record",
]
