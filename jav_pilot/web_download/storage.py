from __future__ import annotations

import shutil
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StorageReservationError(RuntimeError):
    pass


class _DiskUsage(Protocol):
    free: int


class StorageLease(Protocol):
    def try_resize(
        self,
        media_bytes: int,
        downloaded_bytes: int,
        *,
        exact: bool = False,
    ) -> bool: ...

    def release(self) -> None: ...


class StorageReservations(Protocol):
    def try_reserve(self) -> StorageLease | None: ...


class StorageReservation:
    def __init__(
        self,
        resize: Callable[[int, int, bool], bool],
        release: Callable[[], None],
    ) -> None:
        self._resize = resize
        self._release = release
        self._lock = threading.Lock()
        self._released = False

    def try_resize(
        self,
        media_bytes: int,
        downloaded_bytes: int,
        *,
        exact: bool = False,
    ) -> bool:
        with self._lock:
            if self._released:
                return False
            return self._resize(media_bytes, downloaded_bytes, exact)

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()

    def __enter__(self) -> "StorageReservation":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class WebDownloadStorageReservations:
    """Atomically budgets each worker's current per-filesystem disk peak."""

    def __init__(
        self,
        staging_path: str | Path,
        library_path: str | Path,
        *,
        min_free_bytes: int,
        initial_media_bytes: int,
        device_id: Callable[[Path], object] | None = None,
        disk_usage: Callable[[Path], _DiskUsage] = shutil.disk_usage,
        on_low_space: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self._staging = _regular_directory(Path(staging_path), "staging")
        self._library = _regular_directory(Path(library_path), "library")
        self._min_free_bytes = _nonnegative_int(min_free_bytes, "minimum free bytes")
        self._initial_media_bytes = _positive_int(
            initial_media_bytes, "initial media bytes"
        )
        self._device_id = device_id or _device_id
        self._disk_usage = disk_usage
        self._on_low_space = on_low_space
        self._lock = threading.Lock()
        self._leases: dict[str, _LeaseState] = {}
        self._staging_device = self._device_id(self._staging)
        self._library_device = self._device_id(self._library)
        self._volume_key_by_device = (
            {self._staging_device: "shared"}
            if self._staging_device == self._library_device
            else {
                self._staging_device: "staging",
                self._library_device: "library",
            }
        )
        paths: dict[object, list[Path]] = {}
        for device, path in (
            (self._staging_device, self._staging),
            (self._library_device, self._library),
        ):
            paths.setdefault(device, []).append(path)
        self._paths_by_device = {
            device: tuple(device_paths) for device, device_paths in paths.items()
        }

    @property
    def required_bytes(self) -> dict[object, int]:
        with self._lock:
            return self._aggregate_required(self._leases.values())

    def notification_volume_key(self, volume_key: str) -> str:
        if volume_key == "staging":
            device = self._staging_device
        elif volume_key == "library":
            device = self._library_device
        else:
            raise StorageReservationError("disk notification volume is invalid")
        return self._volume_key_by_device[device]

    def try_reserve(self) -> StorageReservation | None:
        lease_id = uuid.uuid4().hex
        initial = _LeaseState(self._initial_media_bytes, 0)
        shortfall: tuple[str, ...] = ()
        with self._lock:
            proposed = {**self._leases, lease_id: initial}
            shortfall = self._shortfall_volume_keys(proposed.values())
            if not shortfall:
                self._leases[lease_id] = initial
        if shortfall:
            self._notify_low_space(shortfall)
            return None
        return StorageReservation(
            lambda media_bytes, downloaded_bytes, exact: self._try_resize(
                lease_id,
                media_bytes,
                downloaded_bytes,
                exact=exact,
            ),
            lambda: self._release(lease_id),
        )

    def _try_resize(
        self,
        lease_id: str,
        media_bytes: int,
        downloaded_bytes: int,
        *,
        exact: bool,
    ) -> bool:
        clean_media = _positive_int(media_bytes, "media bytes")
        clean_downloaded = _nonnegative_int(downloaded_bytes, "downloaded bytes")
        if not isinstance(exact, bool):
            raise StorageReservationError("exact size flag is invalid")
        shortfall: tuple[str, ...] = ()
        with self._lock:
            current = self._leases.get(lease_id)
            if current is None:
                return False
            target_downloaded = max(current.downloaded_bytes, clean_downloaded)
            target = _LeaseState(
                media_bytes=(
                    max(clean_media, target_downloaded)
                    if exact
                    else max(current.media_bytes, clean_media, target_downloaded)
                ),
                downloaded_bytes=target_downloaded,
            )
            proposed = {**self._leases, lease_id: target}
            current_required = self._aggregate_required(self._leases.values())
            proposed_required = self._aggregate_required(proposed.values())
            devices = current_required.keys() | proposed_required.keys()
            if all(
                proposed_required.get(device, 0)
                <= current_required.get(device, 0)
                for device in devices
            ):
                self._leases[lease_id] = target
                return True
            shortfall = self._shortfall_volume_keys(proposed.values())
            if not shortfall:
                self._leases[lease_id] = target
        if shortfall:
            self._notify_low_space(shortfall)
            return False
        return True

    def _shortfall_volume_keys(
        self, states: Iterable[_LeaseState]
    ) -> tuple[str, ...]:
        required_by_device = self._aggregate_required(states)
        return tuple(
            sorted(
                self._volume_key_by_device[device]
                for device, required in required_by_device.items()
                if self._free_bytes(device) < self._min_free_bytes + required
            )
        )

    def _notify_low_space(self, volume_keys: tuple[str, ...]) -> None:
        callback = self._on_low_space
        if callback is None:
            return
        try:
            callback(volume_keys)
        except Exception:
            # The persistent outbox is best-effort here: notification failure must
            # never turn a capacity rejection into a worker or service failure.
            return

    def _aggregate_required(
        self, states: Iterable[_LeaseState]
    ) -> dict[object, int]:
        active = tuple(states)
        if not active:
            return {}
        remaining = sum(
            max(state.media_bytes - state.downloaded_bytes, 0)
            for state in active
        )
        # Resumable finalization retains segments while assembling TS and MP4.
        # The global finalize lock limits those two additional copies to one job.
        finalize = 2 * max(state.media_bytes for state in active)
        if self._staging_device == self._library_device:
            return {self._staging_device: remaining + finalize}
        return {
            self._staging_device: remaining + finalize,
            self._library_device: sum(state.media_bytes for state in active),
        }

    def _free_bytes(self, device: object) -> int:
        try:
            free_values = [
                int(self._disk_usage(path).free)
                for path in self._paths_by_device[device]
            ]
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            raise StorageReservationError(
                "download storage capacity could not be checked"
            ) from exc
        if not free_values or any(free < 0 for free in free_values):
            raise StorageReservationError(
                "download storage capacity could not be checked"
            )
        return min(free_values)

    def _release(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)


def _regular_directory(path: Path, name: str) -> Path:
    try:
        if path.is_symlink() or not path.is_dir():
            raise StorageReservationError(
                f"web download {name} path is not a regular directory"
            )
        return path.resolve(strict=True)
    except OSError as exc:
        raise StorageReservationError(
            f"web download {name} path is unavailable"
        ) from exc


def _device_id(path: Path) -> object:
    try:
        return path.stat().st_dev
    except OSError as exc:
        raise StorageReservationError(
            "download storage device could not be identified"
        ) from exc


@dataclass(frozen=True)
class _LeaseState:
    media_bytes: int
    downloaded_bytes: int


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageReservationError(f"{name} is invalid")
    return value


def _positive_int(value: object, name: str) -> int:
    clean = _nonnegative_int(value, name)
    if clean == 0:
        raise StorageReservationError(f"{name} is invalid")
    return clean
