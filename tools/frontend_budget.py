from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


ENTRY_JS_MAX_BYTES = 350 * 1024
CSS_MAX_BYTES = 100 * 1024
LAZY_CHUNK_MAX_BYTES = 75 * 1024
INITIAL_MAX_BYTES = 450 * 1024


class FrontendBudgetError(RuntimeError):
    pass


class _IndexAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.styles: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "script" and values.get("type") == "module":
            self.scripts.append(values.get("src", ""))
        if tag.lower() == "link" and values.get("rel") == "stylesheet":
            self.styles.append(values.get("href", ""))


@dataclass(frozen=True)
class FrontendBudgetReport:
    entry_js_bytes: int
    css_bytes: int
    initial_bytes: int
    largest_lazy_chunk_bytes: int
    lazy_chunk_count: int

    def public_dict(self) -> dict[str, int | bool]:
        return {
            "ok": True,
            "entry_js_bytes": self.entry_js_bytes,
            "css_bytes": self.css_bytes,
            "initial_bytes": self.initial_bytes,
            "largest_lazy_chunk_bytes": self.largest_lazy_chunk_bytes,
            "lazy_chunk_count": self.lazy_chunk_count,
        }


def check_frontend_budget(dist_root: Path) -> FrontendBudgetReport:
    root = dist_root.resolve()
    index_path = root / "index.html"
    if not index_path.is_file():
        raise FrontendBudgetError("frontend build index is missing")
    parser = _IndexAssets()
    parser.feed(index_path.read_text(encoding="utf-8"))
    entry_scripts = tuple(_asset_path(root, value) for value in parser.scripts)
    styles = tuple(_asset_path(root, value) for value in parser.styles)
    if not entry_scripts:
        raise FrontendBudgetError("frontend build has no module entry")

    entry_bytes = sum(path.stat().st_size for path in entry_scripts)
    css_bytes = sum(path.stat().st_size for path in styles)
    initial_bytes = entry_bytes + css_bytes
    entry_set = {path.resolve() for path in entry_scripts}
    lazy_chunks = tuple(
        path
        for path in (root / "assets").glob("*.js")
        if path.resolve() not in entry_set
    )
    largest_lazy = max((path.stat().st_size for path in lazy_chunks), default=0)
    violations: list[str] = []
    if entry_bytes > ENTRY_JS_MAX_BYTES:
        violations.append("entry JavaScript exceeds 350 KiB")
    if css_bytes > CSS_MAX_BYTES:
        violations.append("CSS exceeds 100 KiB")
    if largest_lazy > LAZY_CHUNK_MAX_BYTES:
        violations.append("a lazy JavaScript chunk exceeds 75 KiB")
    if initial_bytes > INITIAL_MAX_BYTES:
        violations.append("initial frontend resources exceed 450 KiB")
    if violations:
        raise FrontendBudgetError("; ".join(violations))
    return FrontendBudgetReport(
        entry_js_bytes=entry_bytes,
        css_bytes=css_bytes,
        initial_bytes=initial_bytes,
        largest_lazy_chunk_bytes=largest_lazy,
        lazy_chunk_count=len(lazy_chunks),
    )


def _asset_path(root: Path, raw_value: str) -> Path:
    value = str(raw_value or "").split("?", 1)[0].lstrip("/")
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts:
        raise FrontendBudgetError("frontend index contains an invalid asset path")
    candidate = root.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FrontendBudgetError("frontend asset escapes the build root") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise FrontendBudgetError("frontend index references a missing asset")
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify raw JAV Pilot frontend release-size budgets."
    )
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    try:
        report = check_frontend_budget(args.dist)
    except (FrontendBudgetError, OSError, UnicodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error_code": "frontend_budget_exceeded", "detail": str(exc)},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(report.public_dict(), ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
