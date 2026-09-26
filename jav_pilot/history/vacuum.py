"""Compacting the task databases after cleanup."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from ..maintenance.lock import MaintenanceLockError, MaintenanceLocks
from .databases import integrity_check, path_identity, require_backup_database
from .errors import HistoryLifecycleConflictError, HistoryLifecycleValidationError
from .lifecycle_base import HistoryLifecycleBase


class HistoryVacuumMixin(HistoryLifecycleBase):
    def vacuum(self, target: object, *, backup_path: Path | str) -> dict[str, object]:
        clean_target = str(target or "").strip().lower()
        target_paths = {
            "web": self.web_path,
            "metadata": self.metadata_path,
            "library": self.library_path,
        }
        if clean_target not in target_paths:
            raise HistoryLifecycleValidationError("vacuum target is invalid")
        try:
            maintenance = self._maintenance_guard()
        except Exception as exc:
            raise HistoryLifecycleConflictError(
                "maintenance mode could not be verified"
            ) from exc
        if maintenance is not True:
            raise HistoryLifecycleConflictError(
                "vacuum requires verified maintenance mode"
            )
        try:
            workers_stopped = self._workers_stopped_guard()
        except Exception as exc:
            raise HistoryLifecycleConflictError(
                "operational worker state could not be verified"
            ) from exc
        if workers_stopped is not True:
            raise HistoryLifecycleConflictError(
                "vacuum requires all operational workers to be stopped"
            )
        self._verify_database_identities()
        active = self._active_work_counts()
        if any(active.values()):
            raise HistoryLifecycleConflictError(
                "vacuum requires all workers and reviews to be idle"
            )

        path = target_paths[clean_target]
        backup = Path(backup_path)
        maintenance_root = (
            path.parent.parent if path.parent.name == "data" else path.parent
        )
        try:
            with MaintenanceLocks((maintenance_root, backup.parent)):
                return self._vacuum_locked(clean_target, path, backup)
        except MaintenanceLockError as exc:
            raise HistoryLifecycleConflictError(
                "vacuum maintenance lock is unavailable"
            ) from exc

    def _vacuum_locked(
        self,
        clean_target: str,
        path: Path,
        backup: Path,
    ) -> dict[str, object]:
        self._verify_database_identities()
        try:
            manifest = self._backup_verifier(backup)
        except Exception as exc:
            raise HistoryLifecycleConflictError(
                "vacuum requires a verified backup"
            ) from exc
        require_backup_database(
            manifest,
            path,
            expected_revision=self._runtime_revision,
        )
        before_bytes = path.stat(follow_symlinks=False).st_size
        with closing(
            sqlite3.connect(path, timeout=30.0, isolation_level=None)
        ) as connection:
            connection.execute("PRAGMA busy_timeout = 30000")
            before_integrity = integrity_check(connection)
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise HistoryLifecycleConflictError(
                    "vacuum database checkpoint is busy"
                )
            connection.execute("VACUUM")
            after_integrity = integrity_check(connection)
        after_bytes = path.stat(follow_symlinks=False).st_size
        self._identities[path] = path_identity(path)
        return {
            "target": clean_target,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "reclaimed_bytes": max(0, before_bytes - after_bytes),
            "before_integrity": before_integrity,
            "after_integrity": after_integrity,
            "backup_verified": True,
        }
