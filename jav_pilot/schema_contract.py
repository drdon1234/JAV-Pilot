"""Stable import path for deployment tooling.

``ops/nas_release.py`` imports ``jav_pilot.schema_contract`` inside release images of
any version, so this path must keep working across layout changes. The
implementation lives in :mod:`jav_pilot.maintenance.schema_contract`.
"""

from .maintenance.schema_contract import (
    SCHEMA_COMPONENTS,
    SCHEMA_CONTRACT_VERSION,
    SchemaComponent,
    runtime_schema_contract,
    sqlite_schema_versions,
)

__all__ = [
    "SCHEMA_COMPONENTS",
    "SCHEMA_CONTRACT_VERSION",
    "SchemaComponent",
    "runtime_schema_contract",
    "sqlite_schema_versions",
]
