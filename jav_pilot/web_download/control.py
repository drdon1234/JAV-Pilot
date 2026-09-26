from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MIN_BANDWIDTH_BYTES_PER_SECOND = 64 * 1024
MAX_BANDWIDTH_BYTES_PER_SECOND = 10 * 1024**3
MAX_SCHEDULE_WINDOWS = 32


class WebDownloadControlError(ValueError):
    pass


def normalize_target_concurrency(value: object, *, hard_limit: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= hard_limit
    ):
        raise WebDownloadControlError(
            f"target concurrency must be between 1 and {hard_limit}"
        )
    return value


def normalize_bandwidth_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebDownloadControlError("bandwidth limit must be an integer")
    if value == 0:
        return 0
    if not MIN_BANDWIDTH_BYTES_PER_SECOND <= value <= MAX_BANDWIDTH_BYTES_PER_SECOND:
        raise WebDownloadControlError("bandwidth limit is outside the safe range")
    return value


def normalize_timezone(value: object) -> str:
    clean = "UTC" if value is None else str(value).strip()
    if not clean or len(clean) > 64 or not clean.isascii():
        raise WebDownloadControlError("download schedule timezone is invalid")
    try:
        ZoneInfo(clean)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WebDownloadControlError("download schedule timezone is invalid") from exc
    return clean


def normalize_schedule(value: object) -> str:
    if value in (None, "", []):
        return "[]"
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WebDownloadControlError("download schedule is invalid") from exc
    else:
        raw = value
    if not isinstance(raw, list) or len(raw) > MAX_SCHEDULE_WINDOWS:
        raise WebDownloadControlError("download schedule is invalid")
    normalized: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"days", "start", "end"}:
            raise WebDownloadControlError("download schedule window is invalid")
        days = item.get("days")
        if (
            not isinstance(days, list)
            or not days
            or len(days) > 7
            or any(isinstance(day, bool) or not isinstance(day, int) for day in days)
        ):
            raise WebDownloadControlError("download schedule days are invalid")
        clean_days = sorted(set(days))
        if len(clean_days) != len(days) or any(not 0 <= day <= 6 for day in clean_days):
            raise WebDownloadControlError("download schedule days are invalid")
        start = _minute(item.get("start"))
        end = _minute(item.get("end"))
        if start == end:
            raise WebDownloadControlError("download schedule window cannot span 24 hours")
        normalized.append(
            {
                "days": clean_days,
                "start": _format_minute(start),
                "end": _format_minute(end),
            }
        )
    normalized.sort(
        key=lambda item: (item["days"], item["start"], item["end"])
    )
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True)


def schedule_is_open(
    schedule_json: object,
    timezone_name: object,
    *,
    now: datetime | None = None,
) -> bool:
    schedule = json.loads(normalize_schedule(schedule_json))
    if not schedule:
        return True
    zone = ZoneInfo(normalize_timezone(timezone_name))
    current = (now or datetime.now(UTC)).astimezone(zone)
    weekday = current.weekday()
    minute = current.hour * 60 + current.minute
    for window in schedule:
        start = _minute(window["start"])
        end = _minute(window["end"])
        for day in window["days"]:
            if start < end and weekday == day and start <= minute < end:
                return True
            if start > end and (
                (weekday == day and minute >= start)
                or (weekday == (day + 1) % 7 and minute < end)
            ):
                return True
    return False


def _minute(value: object) -> int:
    clean = str(value or "").strip()
    parts = clean.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise WebDownloadControlError("download schedule time is invalid")
    hour, minute = int(parts[0]), int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise WebDownloadControlError("download schedule time is invalid")
    return hour * 60 + minute


def _format_minute(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"
