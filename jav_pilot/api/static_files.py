from __future__ import annotations

import mimetypes
import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

APP_ROUTES = {
    "/login",
    "/search",
    "/search-history",
    "/results",
    "/rankings",
    "/downloads",
    "/history",
    "/library",
    "/metadata",
    "/organizer",
    "/sites",
    "/settings",
    "/workflow-defaults",
}
WORK_ROUTE_RE = re.compile(r"^/works/[^/]+$")
MAX_STATIC_BYTES = 16 * 1024 * 1024


class StaticFileError(RuntimeError):
    pass


def is_app_route(path: str) -> bool:
    return path in APP_ROUTES or bool(WORK_ROUTE_RE.fullmatch(path))


@lru_cache(maxsize=1)
def static_root() -> Path:
    configured = os.environ.get("JAV_PILOT_STATIC_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        (
            Path.cwd() / "frontend" / "dist",
            Path(__file__).resolve().parents[1] / "static",
        )
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "index.html").is_file():
            return resolved
    raise StaticFileError("frontend build not found; run npm run build in frontend/")


def read_index() -> bytes:
    return _read_file(static_root() / "index.html")


def read_asset(url_path: str) -> tuple[bytes, str]:
    relative = PurePosixPath(url_path.lstrip("/"))
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise StaticFileError("invalid static path")
    root = static_root()
    path = root.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StaticFileError("invalid static path") from exc
    if not path.is_file():
        raise FileNotFoundError(url_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return _read_file(path), content_type


def _read_file(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StaticFileError(str(exc)) from exc
    if size > MAX_STATIC_BYTES:
        raise StaticFileError("static asset is too large")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StaticFileError(str(exc)) from exc
