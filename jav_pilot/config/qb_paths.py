from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath


DEFAULT_QB_STAGING_PATH = "/downloads/jav"
DEFAULT_QB_LIBRARY_PATH = "/media/JAV"
DEFAULT_QB_APP_LIBRARY_PATH = "/media/JAV"
LEGACY_SHARED_STAGING_ROOTS = (
    "/downloads/mp",
    "/downloads/movie",
    "/downloads/tv",
    "/downloads/short",
    "/downloads/short_video",
)


class QbPathError(ValueError):
    pass


def normalize_qb_path(value: str) -> str:
    text = str(value or "").strip()
    if (
        not text.startswith("/")
        or text.startswith("//")
        or text == "/"
        or "\\" in text
        or any(ord(character) < 32 for character in text)
    ):
        raise QbPathError("qBittorrent path must be an absolute POSIX container path")
    path = PurePosixPath(text)
    if ".." in path.parts:
        raise QbPathError("qBittorrent path cannot contain parent traversal")
    return path.as_posix()


def validate_qb_roots(staging_value: str, library_value: str) -> tuple[PurePosixPath, PurePosixPath]:
    staging = validate_qb_staging_path(staging_value)
    library = PurePosixPath(normalize_qb_path(library_value))
    if is_at_or_below(staging, library) or is_at_or_below(library, staging):
        raise QbPathError("qBittorrent staging and library paths must not overlap")
    return staging, library


def validate_qb_library_mapping(
    library_value: str,
    app_library_value: str,
    *,
    expected_app_root: str | None = None,
    require_accessible: bool = False,
) -> tuple[PurePosixPath | None, PurePosixPath]:
    """Validate the qB path and the path exposing the same files in this app."""

    library = (
        PurePosixPath(normalize_qb_path(library_value)) if library_value else None
    )
    app_library = PurePosixPath(normalize_qb_path(app_library_value))
    if expected_app_root:
        expected = PurePosixPath(normalize_qb_path(expected_app_root))
        if not is_at_or_below(app_library, expected):
            raise QbPathError(
                "qBittorrent app library path must be inside the metadata library root"
            )
    if require_accessible and library is not None:
        local_path = Path(app_library.as_posix())
        if local_path.is_symlink() or not local_path.is_dir():
            raise QbPathError(
                "qBittorrent app library path is not an accessible directory"
            )
        try:
            with tempfile.TemporaryFile(dir=local_path):
                pass
        except OSError as exc:
            raise QbPathError(
                "qBittorrent app library path is not writable"
            ) from exc
    return library, app_library


def validate_qb_staging_path(value: str) -> PurePosixPath:
    staging = PurePosixPath(normalize_qb_path(value))
    if staging.as_posix() == "/downloads":
        raise QbPathError("qBittorrent staging path must not use the shared downloads root")
    reserved_roots = [PurePosixPath(path) for path in LEGACY_SHARED_STAGING_ROOTS]
    reserved_roots.append(PurePosixPath(DEFAULT_QB_LIBRARY_PATH))
    if any(is_at_or_below(staging, root) for root in reserved_roots):
        raise QbPathError("qBittorrent staging path must be isolated from MoviePilot and the media library")
    return staging


def resolve_qb_download_destination(
    *,
    configured_category: str,
    configured_save_path: str,
    requested_category: str = "",
    requested_save_path: str = "",
) -> tuple[str, str]:
    category = str(configured_category or "").strip()
    if not category:
        raise QbPathError("qBittorrent category must be configured before adding downloads")
    requested = str(requested_category or "").strip()
    if requested and requested != category:
        raise QbPathError("download category must match the configured qBittorrent category")

    staging_root = validate_qb_staging_path(configured_save_path)
    raw_destination = str(requested_save_path or "").strip()
    destination = (
        staging_root
        if not raw_destination
        else PurePosixPath(normalize_qb_path(raw_destination))
    )
    if not is_at_or_below(destination, staging_root):
        raise QbPathError(
            "download save path must be the configured qBittorrent staging path "
            "or one of its child directories"
        )
    return category, destination.as_posix()


def is_legacy_shared_staging_path(value: str) -> bool:
    staging = PurePosixPath(normalize_qb_path(value))
    if staging.as_posix() == "/downloads":
        return True
    roots = [PurePosixPath(path) for path in LEGACY_SHARED_STAGING_ROOTS]
    roots.append(PurePosixPath(DEFAULT_QB_LIBRARY_PATH))
    return any(is_at_or_below(staging, root) for root in roots)


def is_at_or_below(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents
