"""Stable import path for deployment tooling.

``ops/nas_release.py`` imports ``jav_pilot.browser_acceptance`` inside release images of
any version, so this path must keep working across layout changes. The
implementation lives in :mod:`jav_pilot.maintenance.browser_acceptance`.
"""

from .maintenance.browser_acceptance import (
    BrowserAcceptanceError,
    run_browser_acceptance,
)

__all__ = [
    "BrowserAcceptanceError",
    "run_browser_acceptance",
]
