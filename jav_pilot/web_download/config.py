"""Web download configuration loaded from the environment."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config.paths import (
    default_database_path,
    default_library_path,
    default_staging_path,
)
from .errors import WebDownloadConfigError, WebDownloadError
from .jobs import (
    DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    DEFAULT_INITIAL_MEDIA_BYTES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MIN_FREE_BYTES,
    IDEMPOTENCY_RE,
    MAX_CONCURRENCY,
    PROVIDER,
)

@dataclass(frozen=True)
class WebDownloadConfig:
    enabled: bool = False
    database_path: Path | str = field(
        default_factory=lambda: default_database_path("web_downloads.sqlite3")
    )
    staging_path: str = field(default_factory=lambda: default_staging_path().as_posix())
    library_path: str = field(default_factory=lambda: default_library_path().as_posix())
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    capture_timeout_seconds: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    initial_media_bytes: int = DEFAULT_INITIAL_MEDIA_BYTES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    def __post_init__(self) -> None:
        database_path = Path(self.database_path).expanduser()
        if not database_path.is_absolute():
            raise WebDownloadConfigError("web download database path must be absolute")
        staging = _validate_host_root(self.staging_path, "staging")
        library = _validate_host_root(self.library_path, "library")
        if _roots_overlap(staging, library):
            raise WebDownloadConfigError(
                "web download staging and library paths must be isolated"
            )
        try:
            max_concurrency = int(self.max_concurrency)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadConfigError(
                "web download concurrency must be an integer"
            ) from exc
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or max_concurrency != self.max_concurrency
            or not 1 <= max_concurrency <= MAX_CONCURRENCY
        ):
            raise WebDownloadConfigError(
                f"web download concurrency must be between 1 and {MAX_CONCURRENCY}"
            )
        if isinstance(self.capture_timeout_seconds, bool):
            raise WebDownloadConfigError(
                "web download capture timeout must be between 15 and 120 seconds"
            )
        try:
            capture_timeout_seconds = float(self.capture_timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebDownloadConfigError(
                "web download capture timeout must be between 15 and 120 seconds"
            ) from exc
        if not math.isfinite(capture_timeout_seconds) or not (
            15.0 <= capture_timeout_seconds <= 120.0
        ):
            raise WebDownloadConfigError(
                "web download capture timeout must be between 15 and 120 seconds"
            )
        if isinstance(self.min_free_bytes, bool) or int(self.min_free_bytes) < 0:
            raise WebDownloadConfigError(
                "web download minimum free bytes must be non-negative"
            )
        if isinstance(self.max_file_bytes, bool) or int(self.max_file_bytes) <= 0:
            raise WebDownloadConfigError(
                "web download maximum file bytes must be positive"
            )
        if (
            isinstance(self.initial_media_bytes, bool)
            or int(self.initial_media_bytes) <= 0
            or int(self.initial_media_bytes) > int(self.max_file_bytes)
        ):
            raise WebDownloadConfigError(
                "web download initial media bytes must be positive and "
                "not exceed the maximum file bytes"
            )
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "staging_path", staging.as_posix())
        object.__setattr__(self, "library_path", library.as_posix())
        object.__setattr__(self, "max_concurrency", max_concurrency)
        object.__setattr__(
            self,
            "capture_timeout_seconds",
            capture_timeout_seconds,
        )
        object.__setattr__(self, "min_free_bytes", int(self.min_free_bytes))
        object.__setattr__(
            self,
            "initial_media_bytes",
            int(self.initial_media_bytes),
        )
        object.__setattr__(self, "max_file_bytes", int(self.max_file_bytes))

    @classmethod
    def from_env(cls) -> "WebDownloadConfig":
        max_file_bytes = _env_int(
            "JAV_PILOT_WEB_DOWNLOAD_MAX_FILE_BYTES",
            DEFAULT_MAX_FILE_BYTES,
            minimum=1,
            maximum=1024**5,
        )
        return cls(
            enabled=_env_bool("JAV_PILOT_WEB_DOWNLOAD_ENABLED", False),
            database_path=os.environ.get(
                "JAV_PILOT_WEB_DOWNLOAD_DATABASE_PATH", ""
            ).strip()
            or default_database_path("web_downloads.sqlite3"),
            staging_path=os.environ.get(
                "JAV_PILOT_WEB_DOWNLOAD_STAGING_PATH", ""
            ).strip()
            or default_staging_path().as_posix(),
            library_path=os.environ.get(
                "JAV_PILOT_WEB_DOWNLOAD_LIBRARY_PATH", ""
            ).strip()
            or default_library_path().as_posix(),
            max_concurrency=_env_int(
                "JAV_PILOT_WEB_DOWNLOAD_MAX_CONCURRENCY",
                DEFAULT_MAX_CONCURRENCY,
                minimum=1,
                maximum=MAX_CONCURRENCY,
            ),
            capture_timeout_seconds=_env_float(
                "JAV_PILOT_WEB_DOWNLOAD_CAPTURE_TIMEOUT_SECONDS",
                DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                minimum=15.0,
                maximum=120.0,
            ),
            min_free_bytes=_env_int(
                "JAV_PILOT_WEB_DOWNLOAD_MIN_FREE_BYTES",
                DEFAULT_MIN_FREE_BYTES,
                minimum=0,
                maximum=1024**5,
            ),
            initial_media_bytes=_env_int(
                "JAV_PILOT_WEB_DOWNLOAD_INITIAL_MEDIA_BYTES",
                DEFAULT_INITIAL_MEDIA_BYTES,
                minimum=1,
                maximum=1024**5,
            ),
            max_file_bytes=max_file_bytes,
        )

    @property
    def configured(self) -> bool:
        return self.enabled

    def public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "provider": PROVIDER,
            "database_path": str(self.database_path),
            "staging_path": self.staging_path,
            "library_path": self.library_path,
            "max_concurrency": self.max_concurrency,
            "capture_timeout_seconds": self.capture_timeout_seconds,
            "min_free_bytes": self.min_free_bytes,
            "initial_media_bytes": self.initial_media_bytes,
            "max_file_bytes": self.max_file_bytes,
        }


def normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not IDEMPOTENCY_RE.fullmatch(clean) or "://" in clean:
        raise WebDownloadError("invalid idempotency key")
    return clean


def _validate_host_root(value: object, name: str) -> Path:
    raw = str(value or "").strip()
    path = Path(raw).expanduser()
    if (
        not raw
        or len(raw) > 512
        or any(ord(character) < 32 for character in raw)
        or not path.is_absolute()
        or path == Path(path.anchor)
        or any(part in {".", ".."} for part in raw.replace("\\", "/").split("/"))
    ):
        raise WebDownloadConfigError(
            f"web download {name} path must be an absolute host path"
        )
    return path


def _roots_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise WebDownloadConfigError(f"{name} must be a boolean")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WebDownloadConfigError(f"{name} must be an integer") from exc
    return max(minimum, min(value, maximum))


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebDownloadConfigError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise WebDownloadConfigError(f"{name} must be a number")
    return max(minimum, min(value, maximum))
