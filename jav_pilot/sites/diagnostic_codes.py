"""Acceptance codes for site diagnostics, entered and remembered by the user.

Two codes are kept because the sources cover different catalogues: FC2-only
sources (FC2, FC2DB, JAVTEN) are checked with the FC2 code and every other
source with the JAV code. Nothing is filled in by default; a source without a
matching code is only checked up to its connection stage.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from ..core.catalog_code import normalize_catalog_code
from ..config.paths import runtime_data_dir

FC2_DIAGNOSTIC_SITE_IDS = frozenset({"fc2", "fc2db", "javten"})
_LOCK = threading.Lock()


class SiteDiagnosticCodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SiteDiagnosticCodes:
    jav: str | None = None
    fc2: str | None = None

    def for_site(self, site_id: str) -> str | None:
        return self.fc2 if site_id in FC2_DIAGNOSTIC_SITE_IDS else self.jav

    def public_dict(self) -> dict[str, str]:
        return {"jav": self.jav or "", "fc2": self.fc2 or ""}


def normalize_diagnostic_codes(jav: object, fc2: object) -> SiteDiagnosticCodes:
    return SiteDiagnosticCodes(jav=_clean(jav, fc2=False), fc2=_clean(fc2, fc2=True))


def _clean(value: object, *, fc2: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise SiteDiagnosticCodeError("site diagnostic code is invalid")
    normalized = normalize_catalog_code(value.strip(), max_length=40)
    if normalized is None:
        raise SiteDiagnosticCodeError("site diagnostic code is invalid")
    display, key = normalized
    if key.startswith("FC2PPV") != fc2:
        raise SiteDiagnosticCodeError(
            "the second acceptance code must be an FC2 code"
            if fc2
            else "the first acceptance code must be a JAV code"
        )
    return display


def site_diagnostic_codes_path() -> Path:
    return runtime_data_dir() / "site_diagnostic_codes.json"


def load_site_diagnostic_codes(path: Path | None = None) -> SiteDiagnosticCodes:
    target = path or site_diagnostic_codes_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict):
        try:
            return normalize_diagnostic_codes(payload.get("jav"), payload.get("fc2"))
        except SiteDiagnosticCodeError:
            pass
    # Deployments that configured the former single environment code keep it
    # as their JAV code; nothing is assumed when it is unset.
    legacy = os.environ.get("JAV_PILOT_SITE_DIAGNOSTIC_CODE", "").strip()
    try:
        return normalize_diagnostic_codes(legacy or None, None)
    except SiteDiagnosticCodeError:
        return SiteDiagnosticCodes()


def save_site_diagnostic_codes(
    codes: SiteDiagnosticCodes, path: Path | None = None
) -> SiteDiagnosticCodes:
    target = path or site_diagnostic_codes_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(codes.public_dict(), ensure_ascii=True, sort_keys=True)
    with _LOCK:
        handle, temporary = tempfile.mkstemp(
            prefix=".site_diagnostic_codes.", dir=str(target.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(body)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return codes
