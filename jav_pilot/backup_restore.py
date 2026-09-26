"""Stable import path for deployment tooling.

``ops/nas_release.py`` imports ``jav_pilot.backup_restore`` inside release images of
any version, so this path must keep working across layout changes. The
implementation lives in :mod:`jav_pilot.maintenance.backup`.
"""

from .maintenance.backup import (
    BACKUP_FORMAT_VERSION,
    MANIFEST_NAME,
    SNAPSHOT_KINDS,
    SUPPORTED_BACKUP_FORMAT_VERSIONS,
    BackupError,
    BackupRetentionPolicy,
    apply_backup_retention,
    create_backup,
    create_media_manifest,
    restore_backup,
    verify_backup,
)

__all__ = [
    "BACKUP_FORMAT_VERSION",
    "MANIFEST_NAME",
    "SNAPSHOT_KINDS",
    "SUPPORTED_BACKUP_FORMAT_VERSIONS",
    "BackupError",
    "BackupRetentionPolicy",
    "apply_backup_retention",
    "create_backup",
    "create_media_manifest",
    "restore_backup",
    "verify_backup",
]
