from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from jav_pilot.library.archive import plan_archive_layout  # noqa: E402

FIXTURE_CODES = tuple(f"FXT-{index:03d}" for index in range(1, 10))
ACTIVE_STATUSES = frozenset(
    {"locating", "validating", "downloading", "verifying", "archiving"}
)
PRODUCTION_ROOTS = frozenset(
    {"/app/data", "/downloads/jav", "/downloads/jav-web", "/media/JAV"}
)
RUN_SENTINEL = ".jav-pilot-isolated-load-root"
STATE_FILE = "fault-state.json"
STATUS_FILE = "workload-status.json"
TELEMETRY_FILE = "workload-telemetry.jsonl"
CLEANUP_AUDIT_FILE = "process-cleanup.jsonl"
WORKER_METRICS_DIR = "worker-metrics"
DATABASE_FILE = "web_downloads.sqlite3"
MAX_WORKER_METRIC_FILES = 1024
WINDOWS_MATRIX_OPT_IN = "JAV_PILOT_ALLOW_WINDOWS_FAULT_MATRIX"
WINDOWS_PROCESS_EXIT_TIMEOUT_SECONDS = 10.0
WINDOWS_WORKLOAD_START_TIMEOUT_SECONDS = 15.0
_SEGMENT_URL_RE = re.compile(
    r"^https://surrit\.com/load/(FXT-[0-9]{3})/g([12])/([0-9]{6})\.ts"
)


class FaultMatrixError(RuntimeError):
    pass


class FaultMatrixAlreadyRunning(FaultMatrixError):
    pass


class FaultMatrixPlatformSkip(FaultMatrixError):
    pass


class FaultMatrixAssertionError(FaultMatrixError):
    def __init__(self, message: str, report: dict[str, object]) -> None:
        self.report = report
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    parent_pid: int
    rss_bytes: int
    state: str
    command: str
    process_token: str
    measured: bool = True


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    process_token: str
    command: str


@dataclass(frozen=True, slots=True)
class MatrixProfile:
    mode: str
    load_seconds: float
    segment_count: int
    segment_bytes: int
    segment_delay_seconds: float
    baseline_seconds: float
    sqlite_lock_seconds: float
    io_fault_seconds: float
    low_disk_seconds: float
    post_fault_seconds: float
    idle_seconds: float
    convergence_seconds: float
    startup_seconds: float
    recovery_seconds: float

    @classmethod
    def for_mode(cls, mode: str) -> "MatrixProfile":
        if mode == "ci":
            return cls(
                mode="ci",
                load_seconds=12.0,
                # A representative 40-minute HLS shape with a 48-second
                # simulated transfer keeps faults active without excessive I/O.
                segment_count=2_400,
                segment_bytes=1_024,
                segment_delay_seconds=0.02,
                baseline_seconds=0.8,
                sqlite_lock_seconds=0.3,
                io_fault_seconds=0.4,
                low_disk_seconds=0.8,
                post_fault_seconds=1.2,
                idle_seconds=0.5,
                convergence_seconds=30.0,
                startup_seconds=30.0,
                recovery_seconds=15.0,
            )
        if mode == "release":
            return cls(
                mode="release",
                load_seconds=30.0 * 60.0,
                segment_count=15_000,
                segment_bytes=1_024,
                segment_delay_seconds=0.12,
                baseline_seconds=60.0,
                sqlite_lock_seconds=10.0,
                io_fault_seconds=15.0,
                low_disk_seconds=15.0,
                post_fault_seconds=10.0 * 60.0,
                idle_seconds=5.0 * 60.0,
                convergence_seconds=120.0,
                startup_seconds=120.0,
                recovery_seconds=120.0,
            )
        raise FaultMatrixError("load mode must be ci or release")

    def validate(self) -> None:
        if self.mode not in {"ci", "release"}:
            raise FaultMatrixError("invalid load profile mode")
        if self.mode == "release" and self.load_seconds != 30.0 * 60.0:
            raise FaultMatrixError("release load profile must run for 30 minutes")
        if self.mode == "release" and self.post_fault_seconds != 10.0 * 60.0:
            raise FaultMatrixError("release recovery sample must run for 10 minutes")
        if self.mode == "release" and self.idle_seconds != 5.0 * 60.0:
            raise FaultMatrixError("release idle sample must run for 5 minutes")
        if not 1 <= self.segment_count <= 100_000:
            raise FaultMatrixError("fixture segment count is invalid")
        if not 64 <= self.segment_bytes <= 1024 * 1024:
            raise FaultMatrixError("fixture segment size is invalid")
        for value in (
            self.load_seconds,
            self.segment_delay_seconds,
            self.baseline_seconds,
            self.sqlite_lock_seconds,
            self.io_fault_seconds,
            self.low_disk_seconds,
            self.post_fault_seconds,
            self.idle_seconds,
            self.convergence_seconds,
            self.startup_seconds,
            self.recovery_seconds,
        ):
            if not math.isfinite(value) or value <= 0:
                raise FaultMatrixError("fixture timing is invalid")

    def worker_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "segment_count": self.segment_count,
            "segment_bytes": self.segment_bytes,
            "segment_delay_seconds": self.segment_delay_seconds,
        }


def _initial_state() -> dict[str, object]:
    return {
        "generation": {code: 1 for code in FIXTURE_CODES},
        "fast": False,
        "io_fault": False,
        "low_disk": False,
        "memory_probe": 0,
        "shutdown": False,
    }


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    try:
        temporary.write_text(raw + "\n", encoding="ascii", newline="\n")
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, *, attempts: int = 5) -> dict[str, object]:
    for attempt in range(attempts):
        try:
            payload = json.loads(path.read_text(encoding="ascii"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            if attempt + 1 >= attempts:
                raise FaultMatrixError(f"could not read fixture state {path.name}")
            time.sleep(0.01)
            continue
        if isinstance(payload, dict):
            return payload
        raise FaultMatrixError(f"fixture state {path.name} is invalid")
    raise AssertionError("unreachable")


@lru_cache(maxsize=1)
def _windows_mutex_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    wait_for_single_object = kernel32.WaitForSingleObject
    release_mutex = kernel32.ReleaseMutex
    close_handle = kernel32.CloseHandle
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    release_mutex.argtypes = [wintypes.HANDLE]
    release_mutex.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return create_mutex, wait_for_single_object, release_mutex, close_handle


def _fault_matrix_mutex_identity() -> tuple[str, Path]:
    return (
        "Global\\JavPilotWebDownloadFaultMatrix",
        Path(tempfile.gettempdir()) / "jav-pilot-fault-matrix.owner.json",
    )


class FaultMatrixRunLease:
    def __init__(
        self,
        *,
        handle: object | None = None,
        owner_path: Path | None = None,
        owner_token: str = "",
    ) -> None:
        self._handle = handle
        self._owner_path = owner_path
        self._owner_token = owner_token
        self._closed = False

    def __enter__(self) -> "FaultMatrixRunLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        _create, _wait, release_mutex, close_handle = _windows_mutex_api()
        try:
            if self._owner_path is not None:
                try:
                    owner = _read_json(self._owner_path, attempts=1)
                except FaultMatrixError:
                    owner = {}
                if (
                    owner.get("pid") == os.getpid()
                    and owner.get("process_token") == self._owner_token
                ):
                    try:
                        self._owner_path.unlink()
                    except FileNotFoundError:
                        pass
        finally:
            try:
                release_mutex(handle)  # type: ignore[operator]
            finally:
                close_handle(handle)  # type: ignore[operator]


def _acquire_fault_matrix_run_lease() -> FaultMatrixRunLease:
    if os.name != "nt":
        return FaultMatrixRunLease()
    create_mutex, wait_for_single_object, _release, close_handle = _windows_mutex_api()
    mutex_name, owner_path = _fault_matrix_mutex_identity()
    handle = create_mutex(None, False, mutex_name)  # type: ignore[operator]
    if not handle:
        raise FaultMatrixError("could not create the Windows fault-matrix mutex")
    wait_result = int(wait_for_single_object(handle, 0))  # type: ignore[operator]
    if wait_result == 0x00000102:
        close_handle(handle)  # type: ignore[operator]
        try:
            owner = _read_json(owner_path, attempts=1)
        except FaultMatrixError:
            owner = {}
        owner_pid = owner.get("pid")
        owner_started = owner.get("started_at")
        detail = (
            f" (owner pid={owner_pid}, started_at={owner_started})"
            if owner_pid and owner_started
            else ""
        )
        raise FaultMatrixAlreadyRunning(
            "another Windows fault matrix is already running" + detail
        )
    if wait_result not in {0x00000000, 0x00000080}:
        close_handle(handle)  # type: ignore[operator]
        raise FaultMatrixError("could not acquire the Windows fault-matrix mutex")

    owner_token = _process_token(os.getpid()) or "unknown"
    try:
        _atomic_json(
            owner_path,
            {
                "pid": os.getpid(),
                "process_token": owner_token,
                "started_at": time.time(),
                "command": subprocess.list2cmdline([sys.executable, *sys.argv])[:2048],
            },
        )
    except Exception:
        _create, _wait, release_mutex, close_handle = _windows_mutex_api()
        try:
            release_mutex(handle)  # type: ignore[operator]
        finally:
            close_handle(handle)  # type: ignore[operator]
        raise
    return FaultMatrixRunLease(
        handle=handle,
        owner_path=owner_path,
        owner_token=owner_token,
    )


def _windows_fault_matrix_enabled() -> bool:
    return os.environ.get(WINDOWS_MATRIX_OPT_IN, "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }


class FaultState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cached = _initial_state()

    def read(self) -> dict[str, object]:
        with self._lock:
            try:
                self._cached = _read_json(self.path, attempts=2)
            except FaultMatrixError:
                pass
            return dict(self._cached)

    def update(self, **fields: object) -> dict[str, object]:
        with self._lock:
            current = _read_json(self.path)
            current.update(fields)
            _atomic_json(self.path, current)
            self._cached = current
            return dict(current)

    def fault_active(self) -> bool:
        state = self.read()
        return bool(state.get("io_fault") or state.get("low_disk"))


def deterministic_segment(
    code: str,
    generation: int,
    index: int,
    size: int,
) -> bytes:
    seed = hashlib.sha256(f"{code}|g{generation}|{index}".encode("ascii")).digest()
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def reference_digest(
    code: str,
    generation: int,
    segment_count: int,
    segment_bytes: int,
) -> str:
    digest = hashlib.sha256()
    for index in range(segment_count):
        digest.update(deterministic_segment(code, generation, index, segment_bytes))
    return digest.hexdigest()


def fixture_manifest(code: str, generation: int, segment_count: int) -> bytes:
    lines = [
        "#EXTM3U",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:1",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for index in range(segment_count):
        lines.extend(("#EXTINF:1,", f"g{generation}/{index:06d}.ts"))
    lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode("ascii")


class WorkerMetrics:
    def __init__(self, root: Path, state: FaultState) -> None:
        self.root = root
        self.state = state
        self.boot_id = uuid.uuid4().hex
        self.path = root / WORKER_METRICS_DIR / f"{os.getpid()}-{self.boot_id}.json"
        self.started_at = time.time()
        self.process_token = _process_token(os.getpid())
        self.code: str | None = None
        self.status = "running"
        self.bytes_served = 0
        self.segment_commits = 0
        self.checkpoint_flushes = 0
        self.resegment_resets = 0
        self._last_persist_at = 0.0
        self._lock = threading.Lock()
        self.persist(force=True)

    def set_code(self, code: str) -> None:
        with self._lock:
            self.code = code
        self.persist(force=True)

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self.bytes_served += count

    def committed(self) -> None:
        with self._lock:
            self.segment_commits += 1

    def flushed(self) -> None:
        with self._lock:
            self.checkpoint_flushes += 1
        self.persist(force=True)

    def reset_resume(self) -> None:
        with self._lock:
            self.resegment_resets += 1
        self.persist(force=True)

    def finish(self, status: str) -> None:
        with self._lock:
            self.status = status
        self.persist(force=True)

    def persist(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_persist_at < 0.1:
                return
            self._last_persist_at = now
            payload: dict[str, object] = {
                "boot_id": self.boot_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "process_token": self.process_token,
                "updated_at": time.time(),
                "updated_monotonic": now,
                "code": self.code,
                "status": self.status,
                "bytes_served": self.bytes_served,
                "segment_commits": self.segment_commits,
                "checkpoint_flushes": self.checkpoint_flushes,
                "resegment_resets": self.resegment_resets,
            }
        _atomic_json(self.path, payload)


class FixtureResponse:
    def __init__(
        self,
        url: str,
        body: bytes,
        *,
        metrics: WorkerMetrics,
        delay: Callable[[], float],
        is_media: bool,
    ) -> None:
        self.url = url
        self.body = body
        self.metrics = metrics
        self.delay = delay
        self.is_media = is_media
        self.offset = 0
        self._delayed = False

    def __enter__(self) -> "FixtureResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self.is_media and not self._delayed:
            self._delayed = True
            delay = self.delay()
            if delay > 0:
                time.sleep(delay)
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        if self.is_media and chunk:
            self.metrics.add_bytes(len(chunk))
        return chunk


class FixtureTransport:
    def __init__(
        self,
        profile: Mapping[str, object],
        state: FaultState,
        metrics: WorkerMetrics,
    ) -> None:
        self.segment_count = int(profile["segment_count"])
        self.segment_bytes = int(profile["segment_bytes"])
        self.segment_delay = float(profile["segment_delay_seconds"])
        self.state = state
        self.metrics = metrics

    def capture(self, code: str, **_: object) -> object:
        from jav_pilot.missav.models import ManifestRequest

        if code not in FIXTURE_CODES:
            raise FaultMatrixError("fixture received an unknown catalog code")
        self.metrics.set_code(code)
        return ManifestRequest(
            url=f"https://surrit.com/load/{code}/media.m3u8?token=ephemeral",
            headers={},
            page_url=f"https://missav.test/{code.lower()}",
            selected_height=720,
        )

    def downloader_type(self) -> type:
        transport = self

        class FixtureDownloader:
            def __init__(self, options: object) -> None:
                del options

            def __enter__(self) -> "FixtureDownloader":
                return self

            def __exit__(self, *_: object) -> bool:
                return False

            def urlopen(self, request: object) -> FixtureResponse:
                return transport.open(str(getattr(request, "url", "")))

        return FixtureDownloader

    def open(self, url: str) -> FixtureResponse:
        if url.startswith("https://surrit.com/load/") and ".m3u8" in url:
            code = url.split("/", 5)[4]
            state = self.state.read()
            generations = state.get("generation")
            generation = (
                int(generations.get(code, 1)) if isinstance(generations, dict) else 1
            )
            body = fixture_manifest(code, generation, self.segment_count)
            return FixtureResponse(
                url,
                body,
                metrics=self.metrics,
                delay=lambda: 0.0,
                is_media=False,
            )
        match = _SEGMENT_URL_RE.match(url)
        if match is None:
            raise OSError("fixture rejected an unknown media URL")
        code, raw_generation, raw_index = match.groups()
        generation = int(raw_generation)
        index = int(raw_index)
        if index >= self.segment_count:
            raise OSError("fixture segment index is out of range")
        body = deterministic_segment(code, generation, index, self.segment_bytes)

        def delay() -> float:
            return 0.0 if self.state.read().get("fast") else self.segment_delay

        return FixtureResponse(
            url,
            body,
            metrics=self.metrics,
            delay=delay,
            is_media=True,
        )


def _copy_remux(source: Path, target: Path) -> Path:
    temporary = target.with_name(f".{target.name}.fixture.part")
    if temporary.exists():
        temporary.unlink()
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)
    source.unlink()
    return target


def _run_fixture_worker(root: Path, profile: Mapping[str, object]) -> int:
    from jav_pilot.net.network_guard import PublicHostResolver
    from jav_pilot.web_download import hls as web_download_hls
    from jav_pilot.web_download import progressive as web_download_progressive
    from jav_pilot.web_download import resume as web_download_resume
    from jav_pilot.web_download import worker as web_download_worker

    state = FaultState(root / STATE_FILE)
    metrics = WorkerMetrics(root, state)
    transport = FixtureTransport(profile, state, metrics)
    original_commit = web_download_resume.ResumeStore.commit_segment
    original_flush = web_download_resume.ResumeStore.flush
    original_discard = web_download_resume._discard_resume_state
    original_disk_usage = shutil.disk_usage

    def commit_segment(store: object, index: int, **kwargs: object) -> Path:
        if state.read().get("io_fault"):
            raise OSError("injected isolated staging I/O fault")
        result = original_commit(store, index, **kwargs)
        metrics.committed()
        return result

    def flush(store: object, *, force: bool = False) -> None:
        pending_before = len(getattr(store, "_pending", {}))
        original_flush(store, force=force)
        pending_after = len(getattr(store, "_pending", {}))
        if pending_before and not pending_after:
            metrics.flushed()

    def discard_resume(*args: object, **kwargs: object) -> None:
        original_discard(*args, **kwargs)
        metrics.reset_resume()

    def disk_usage(path: object) -> object:
        actual = original_disk_usage(path)
        if not state.read().get("low_disk"):
            return actual
        return type(actual)(actual.total, actual.total, 0)

    exit_code = 1
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    web_download_worker,
                    "capture_manifest",
                    side_effect=transport.capture,
                )
            )
            # Patch each name in every worker module that looks it up.
            for module in (web_download_hls, web_download_progressive):
                stack.enter_context(
                    patch.object(
                        module,
                        "worker_youtube_dl_type",
                        return_value=transport.downloader_type(),
                    )
                )
            stack.enter_context(
                patch.object(
                    PublicHostResolver,
                    "is_public",
                    return_value=True,
                )
            )
            stack.enter_context(
                patch.object(
                    web_download_hls,
                    "_remux_local_video",
                    side_effect=_copy_remux,
                )
            )
            for module in (web_download_worker, web_download_hls):
                stack.enter_context(
                    patch.object(module, "verify_video", return_value=720)
                )
            stack.enter_context(
                patch.object(
                    web_download_resume.ResumeStore,
                    "commit_segment",
                    new=commit_segment,
                )
            )
            stack.enter_context(
                patch.object(
                    web_download_resume.ResumeStore,
                    "flush",
                    new=flush,
                )
            )
            stack.enter_context(
                patch.object(
                    web_download_resume,
                    "_discard_resume_state",
                    new=discard_resume,
                )
            )
            stack.enter_context(
                patch.object(shutil, "disk_usage", new=disk_usage)
            )
            exit_code = web_download_worker.main()
            return exit_code
    finally:
        metrics.finish("completed" if exit_code == 0 else "exited")


class WorkloadMetrics:
    def __init__(self) -> None:
        self.boot_id = uuid.uuid4().hex
        self.db_progress_writes = 0
        self.worker_process_peak = 0
        self._lock = threading.Lock()

    def progress_write(self) -> None:
        with self._lock:
            self.db_progress_writes += 1

    def observe_workers(self, count: int) -> None:
        with self._lock:
            self.worker_process_peak = max(self.worker_process_peak, count)

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.db_progress_writes, self.worker_process_peak


class WorkerMetricSnapshotCache:
    def __init__(self, root: Path) -> None:
        self.directory = root / WORKER_METRICS_DIR
        self._entries: dict[
            Path,
            tuple[tuple[int, int, int, int], dict[str, object]],
        ] = {}

    def snapshot(self) -> tuple[dict[str, object], ...]:
        if not self.directory.exists():
            self._entries.clear()
            return ()
        paths = tuple(sorted(self.directory.glob("*.json"), key=lambda path: path.name))
        if len(paths) > MAX_WORKER_METRIC_FILES:
            raise FaultMatrixError("worker metric file limit exceeded")
        current_paths = set(paths)
        for stale in self._entries.keys() - current_paths:
            self._entries.pop(stale, None)

        payloads: list[dict[str, object]] = []
        for path in paths:
            try:
                before = _metric_file_identity(path)
            except OSError:
                self._entries.pop(path, None)
                continue
            cached = self._entries.get(path)
            if cached is not None and cached[0] == before:
                payloads.append(cached[1])
                continue
            try:
                payload = _read_json(path, attempts=1)
                after = _metric_file_identity(path)
            except (FaultMatrixError, OSError):
                self._entries.pop(path, None)
                continue
            if before != after:
                self._entries.pop(path, None)
                continue
            self._entries[path] = (after, payload)
            payloads.append(payload)
        return tuple(payloads)


def _metric_file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns)


def _worker_metric_payloads(root: Path) -> list[dict[str, object]]:
    directory = root / WORKER_METRICS_DIR
    if not directory.exists():
        return []
    payloads: list[dict[str, object]] = []
    for path in directory.glob("*.json"):
        try:
            payload = _read_json(path, attempts=1)
        except FaultMatrixError:
            continue
        payloads.append(payload)
    return payloads


@lru_cache(maxsize=1)
def _windows_process_query_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    get_exit_code = kernel32.GetExitCodeProcess
    get_process_times = kernel32.GetProcessTimes
    terminate_process = kernel32.TerminateProcess
    close_handle = kernel32.CloseHandle
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        wintypes.DWORD,
        wintypes.FILETIME,
        ctypes.byref,
        open_process,
        get_exit_code,
        get_process_times,
        terminate_process,
        close_handle,
    )


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            (
                dword_type,
                _filetime_type,
                by_reference,
                open_process,
                get_exit_code,
                _get_process_times,
                _terminate_process,
                close_handle,
            ) = _windows_process_query_api()
            handle = open_process(0x1000, False, pid)  # type: ignore[operator]
            if not handle:
                return False
            try:
                exit_code = dword_type()  # type: ignore[operator]
                return bool(
                    get_exit_code(  # type: ignore[operator]
                        handle,
                        by_reference(exit_code),  # type: ignore[operator]
                    )
                    and int(exit_code.value) == 259
                )
            finally:
                close_handle(handle)  # type: ignore[operator]
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_token(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            (
                dword_type,
                filetime_type,
                by_reference,
                open_process,
                get_exit_code,
                get_process_times,
                _terminate_process,
                close_handle,
            ) = _windows_process_query_api()
            handle = open_process(0x1000, False, pid)  # type: ignore[operator]
            if not handle:
                return None
            try:
                exit_code = dword_type()  # type: ignore[operator]
                if (
                    not get_exit_code(  # type: ignore[operator]
                        handle,
                        by_reference(exit_code),  # type: ignore[operator]
                    )
                    or int(exit_code.value) != 259
                ):
                    return None
                created = filetime_type()  # type: ignore[operator]
                exited = filetime_type()  # type: ignore[operator]
                kernel = filetime_type()  # type: ignore[operator]
                user = filetime_type()  # type: ignore[operator]
                if not get_process_times(  # type: ignore[operator]
                    handle,
                    by_reference(created),  # type: ignore[operator]
                    by_reference(exited),  # type: ignore[operator]
                    by_reference(kernel),  # type: ignore[operator]
                    by_reference(user),  # type: ignore[operator]
                ):
                    return None
                value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                return str(value)
            finally:
                close_handle(handle)  # type: ignore[operator]
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    if not _pid_is_alive(pid):
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        remainder = raw.rsplit(") ", 1)[1]
        return remainder.split()[19]
    except (FileNotFoundError, IndexError, OSError, UnicodeDecodeError):
        return None


@lru_cache(maxsize=1)
def _windows_job_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    set_job_information = kernel32.SetInformationJobObject
    assign_process = kernel32.AssignProcessToJobObject
    terminate_job = kernel32.TerminateJobObject
    close_handle = kernel32.CloseHandle
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_job_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_job_information.restype = wintypes.BOOL
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_job.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        ExtendedLimitInformation,
        ctypes.sizeof,
        ctypes.byref,
        create_job,
        set_job_information,
        assign_process,
        terminate_job,
        close_handle,
    )


_WINDOWS_JOB_HANDLE_ATTRIBUTE = "_jav_pilot_windows_job_handle"
_WORKLOAD_IDENTITY_ATTRIBUTE = "_jav_pilot_workload_identity"
_CLEANUP_AUDIT_LOCK = threading.Lock()
_CLEANUP_STATE = threading.Condition(threading.Lock())
_CLEANUP_RESULTS: dict[tuple[int, str], str] = {}


def _attach_windows_kill_job(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        return
    (
        information_type,
        size_of,
        by_reference,
        create_job,
        set_job_information,
        assign_process,
        _terminate_job,
        close_handle,
    ) = _windows_job_api()
    handle = create_job(None, None)  # type: ignore[operator]
    if not handle:
        raise FaultMatrixError("could not create the Windows workload job")
    try:
        information = information_type()  # type: ignore[operator]
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not set_job_information(  # type: ignore[operator]
            handle,
            9,
            by_reference(information),  # type: ignore[operator]
            size_of(information),  # type: ignore[operator]
        ):
            raise FaultMatrixError("could not configure the Windows workload job")
        process_handle = getattr(process, "_handle", None)
        if not process_handle or not assign_process(  # type: ignore[operator]
            handle, process_handle
        ):
            raise FaultMatrixError("could not assign the workload to its Windows job")
    except Exception:
        close_handle(handle)  # type: ignore[operator]
        raise
    setattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, handle)


def _terminate_windows_job(process: subprocess.Popen[bytes]) -> bool:
    handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    if handle is None:
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    (
        _information_type,
        _size_of,
        _by_reference,
        _create_job,
        _set_job_information,
        _assign_process,
        terminate_job,
        close_handle,
    ) = _windows_job_api()
    try:
        if not terminate_job(handle, 1):  # type: ignore[operator]
            raise FaultMatrixError("could not terminate the Windows workload job")
        return True
    finally:
        close_handle(handle)  # type: ignore[operator]


def _close_windows_job(process: subprocess.Popen[bytes]) -> None:
    handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    if handle is None:
        return
    setattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    close_handle = _windows_job_api()[-1]
    close_handle(handle)  # type: ignore[operator]


def _remember_workload_identity(
    process: subprocess.Popen[bytes], command: Sequence[str]
) -> ProcessIdentity:
    token = _process_token(process.pid)
    if not token:
        raise FaultMatrixError("could not verify the workload process identity")
    identity = ProcessIdentity(
        pid=process.pid,
        process_token=token,
        command=subprocess.list2cmdline(list(command))[:2048],
    )
    setattr(process, _WORKLOAD_IDENTITY_ATTRIBUTE, identity)
    return identity


def _workload_identity(process: subprocess.Popen[bytes]) -> ProcessIdentity:
    identity = getattr(process, _WORKLOAD_IDENTITY_ATTRIBUTE, None)
    if not isinstance(identity, ProcessIdentity) or identity.pid != process.pid:
        raise FaultMatrixError("workload process identity is unavailable")
    if identity.pid in {os.getpid(), os.getppid()}:
        raise FaultMatrixError("refusing to clean the current process tree")
    expected_script = str(Path(__file__).resolve()).casefold()
    command = identity.command.casefold()
    if expected_script not in command or "_workload" not in command:
        raise FaultMatrixError("refusing to clean a non-workload process tree")
    return identity


def _identity_is_current(identity: ProcessIdentity) -> bool:
    return (
        identity.pid > 0
        and identity.pid not in {os.getpid(), os.getppid()}
        and _process_token(identity.pid) == identity.process_token
    )


def _windows_handle_token_api() -> tuple[object, ...]:
    (
        _dword_type,
        filetime_type,
        by_reference,
        _open_process,
        _get_exit_code,
        get_process_times,
        _terminate_process,
        _close_handle,
    ) = _windows_process_query_api()
    return filetime_type, by_reference, get_process_times


def _popen_identity_is_current(
    process: subprocess.Popen[bytes], identity: ProcessIdentity
) -> bool:
    if process.pid != identity.pid or process.poll() is not None:
        return False
    if os.name != "nt":
        return _identity_is_current(identity)
    filetime_type, by_reference, get_process_times = _windows_handle_token_api()
    created = filetime_type()  # type: ignore[operator]
    exited = filetime_type()  # type: ignore[operator]
    kernel = filetime_type()  # type: ignore[operator]
    user = filetime_type()  # type: ignore[operator]
    handle = getattr(process, "_handle", None)
    if not handle or not get_process_times(  # type: ignore[operator]
        handle,
        by_reference(created),  # type: ignore[operator]
        by_reference(exited),  # type: ignore[operator]
        by_reference(kernel),  # type: ignore[operator]
        by_reference(user),  # type: ignore[operator]
    ):
        return False
    token = str((int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime))
    return token == identity.process_token


def _append_cleanup_audit(
    root: Path,
    *,
    identity: ProcessIdentity,
    result: str,
    strategy: str,
    duration_seconds: float | None = None,
    detail: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "timestamp": time.time(),
        "creator_pid": os.getpid(),
        "creator_process_token": _process_token(os.getpid()) or "unknown",
        "creator_command": subprocess.list2cmdline([sys.executable, *sys.argv])[:2048],
        "target_pid": identity.pid,
        "target_process_token": identity.process_token,
        "target_command": identity.command,
        "strategy": strategy,
        "result": result,
    }
    if duration_seconds is not None:
        payload["duration_seconds"] = round(max(0.0, duration_seconds), 3)
    if detail:
        payload["detail"] = detail[:500]
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    path = root / CLEANUP_AUDIT_FILE
    with _CLEANUP_AUDIT_LOCK:
        with path.open("a", encoding="ascii", newline="\n") as handle:
            handle.write(raw + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _cleanup_audit_events(root: Path) -> list[dict[str, object]]:
    path = root / CLEANUP_AUDIT_FILE
    try:
        if path.stat().st_size > 256 * 1024:
            return []
        lines = path.read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []
    events: list[dict[str, object]] = []
    for line in lines[:64]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _fault_matrix_error_with_cleanup_audit(
    root: Path,
    message: str,
) -> FaultMatrixError:
    error = FaultMatrixError(message)
    setattr(error, "cleanup_events", _cleanup_audit_events(root))
    return error


def _descendant_ids(parent_by_pid: Mapping[int, int], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, parent_pid in parent_by_pid.items():
        children.setdefault(parent_pid, []).append(pid)
    descendants: set[int] = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children.get(pid, ()))
    return descendants


def _parse_linux_process_stat(raw: str) -> tuple[int, int, str, str, str]:
    prefix, remainder = raw.rsplit(") ", 1)
    pid_text, command = prefix.split(" (", 1)
    fields = remainder.split()
    if len(fields) < 20:
        raise ValueError("process stat is incomplete")
    return (
        int(pid_text),
        int(fields[1]),
        fields[0],
        fields[19],
        command,
    )


def _linux_process_snapshot(
    root_pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> tuple[ProcessRecord, ...]:
    identities: dict[int, tuple[int, str, str, str]] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid, parent_pid, state, token, command = _parse_linux_process_stat(
                (entry / "stat").read_text(encoding="ascii")
            )
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        identities[pid] = (parent_pid, state, token, command)

    wanted = _descendant_ids(
        {pid: identity[0] for pid, identity in identities.items()},
        root_pid,
    )
    records: list[ProcessRecord] = []
    for pid in sorted(wanted):
        identity = identities.get(pid)
        if identity is None:
            continue
        parent_pid, state, token, fallback_command = identity
        process_root = proc_root / str(pid)
        try:
            current = _parse_linux_process_stat(
                (process_root / "stat").read_text(encoding="ascii")
            )
            if current[:4] != (pid, parent_pid, state, token):
                continue
            status = (process_root / "status").read_text(encoding="ascii")
            match = re.search(r"^VmRSS:\s+([0-9]+)\s+kB$", status, re.MULTILINE)
            if match is None:
                raise ValueError("process RSS is unavailable")
            with (process_root / "cmdline").open("rb") as handle:
                raw_command = handle.read(8192)
            command = (
                raw_command.replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
                .strip()
            )
            records.append(
                ProcessRecord(
                    pid=pid,
                    parent_pid=parent_pid,
                    rss_bytes=int(match.group(1)) * 1024,
                    state=state,
                    command=command or fallback_command,
                    process_token=token,
                )
            )
        except (OSError, UnicodeDecodeError, ValueError):
            records.append(
                ProcessRecord(
                    pid=pid,
                    parent_pid=parent_pid,
                    rss_bytes=0,
                    state=state,
                    command=fallback_command,
                    process_token=token,
                    measured=False,
                )
            )
    return tuple(records)


@lru_cache(maxsize=1)
def _windows_toolhelp_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    process_first = kernel32.Process32FirstW
    process_next = kernel32.Process32NextW
    close_handle = kernel32.CloseHandle
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        ProcessEntry32W,
        ctypes.sizeof,
        ctypes.byref,
        create_snapshot,
        process_first,
        process_next,
        close_handle,
        ctypes.c_void_p(-1).value,
    )


def _windows_process_entries() -> tuple[tuple[int, int, str], ...]:
    (
        entry_type,
        size_of,
        by_reference,
        create_snapshot,
        process_first,
        process_next,
        close_handle,
        invalid_handle,
    ) = _windows_toolhelp_api()
    snapshot = create_snapshot(0x00000002, 0)  # type: ignore[operator]
    if not snapshot or snapshot == invalid_handle:
        return ()
    entries: list[tuple[int, int, str]] = []
    try:
        entry = entry_type()  # type: ignore[operator]
        entry.dwSize = size_of(entry)  # type: ignore[operator, union-attr]
        available = process_first(  # type: ignore[operator]
            snapshot,
            by_reference(entry),  # type: ignore[operator]
        )
        while available:
            entries.append(
                (
                    int(entry.th32ProcessID),
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile),
                )
            )
            available = process_next(  # type: ignore[operator]
                snapshot,
                by_reference(entry),  # type: ignore[operator]
            )
    finally:
        close_handle(snapshot)  # type: ignore[operator]
    return tuple(entries)


@lru_cache(maxsize=1)
def _windows_process_detail_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    open_process = kernel32.OpenProcess
    get_exit_code = kernel32.GetExitCodeProcess
    get_process_times = kernel32.GetProcessTimes
    close_handle = kernel32.CloseHandle
    get_process_memory_info = psapi.GetProcessMemoryInfo
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        ProcessMemoryCounters,
        ctypes.sizeof,
        ctypes.byref,
        wintypes.DWORD,
        wintypes.FILETIME,
        open_process,
        get_exit_code,
        get_process_times,
        get_process_memory_info,
        close_handle,
    )


def _windows_process_details(pid: int) -> tuple[int, str, str, bool]:
    (
        counter_type,
        size_of,
        by_reference,
        dword_type,
        filetime_type,
        open_process,
        get_exit_code,
        get_process_times,
        get_process_memory_info,
        close_handle,
    ) = _windows_process_detail_api()
    access = 0x0400 | 0x1000 | 0x0010
    handle = open_process(access, False, pid)  # type: ignore[operator]
    if not handle:
        return 0, "", "unknown", False
    try:
        exit_code = dword_type()  # type: ignore[operator]
        created = filetime_type()  # type: ignore[operator]
        exited = filetime_type()  # type: ignore[operator]
        kernel = filetime_type()  # type: ignore[operator]
        user = filetime_type()  # type: ignore[operator]
        counters = counter_type()  # type: ignore[operator]
        counters.cb = size_of(counters)  # type: ignore[operator, union-attr]
        exit_code_available = bool(
            get_exit_code(handle, by_reference(exit_code))  # type: ignore[operator]
        )
        if not exit_code_available:
            return 0, "", "unknown", False
        if int(exit_code.value) != 259:
            return 0, "", "exited", True
        complete = bool(
            get_process_times(  # type: ignore[operator]
                handle,
                by_reference(created),  # type: ignore[operator]
                by_reference(exited),  # type: ignore[operator]
                by_reference(kernel),  # type: ignore[operator]
                by_reference(user),  # type: ignore[operator]
            )
            and get_process_memory_info(  # type: ignore[operator]
                handle,
                by_reference(counters),  # type: ignore[operator]
                counters.cb,
            )
        )
        token = (
            str((int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime))
            if complete
            else ""
        )
        return int(counters.WorkingSetSize), token, "running", complete
    finally:
        close_handle(handle)  # type: ignore[operator]


def _windows_process_snapshot(
    root_pid: int,
    *,
    entries: Sequence[tuple[int, int, str]] | None = None,
    detail_reader: Callable[[int], tuple[int, str, str, bool]] | None = None,
) -> tuple[ProcessRecord, ...]:
    current_entries = (
        tuple(entries) if entries is not None else _windows_process_entries()
    )
    wanted = _descendant_ids(
        {pid: parent_pid for pid, parent_pid, _command in current_entries},
        root_pid,
    )
    read_details = detail_reader or _windows_process_details
    records: list[ProcessRecord] = []
    for pid, parent_pid, command in sorted(current_entries):
        if pid not in wanted:
            continue
        rss_bytes, token, state, measured = read_details(pid)
        if str(state).strip().casefold() == "exited":
            continue
        records.append(
            ProcessRecord(
                pid=pid,
                parent_pid=parent_pid,
                rss_bytes=max(0, int(rss_bytes)),
                state=str(state),
                command=str(command),
                process_token=str(token),
                measured=bool(measured),
            )
        )
    return tuple(records)


def _process_snapshot(root_pid: int) -> tuple[ProcessRecord, ...]:
    if os.name == "nt":
        return _windows_process_snapshot(root_pid)
    return _linux_process_snapshot(root_pid)


def _process_role(
    record: ProcessRecord,
    *,
    root_pid: int,
    registered_worker_pids: frozenset[int],
) -> str:
    if record.pid == root_pid:
        return "workload"
    command = record.command.casefold()
    if any(
        token in command
        for token in ("chromium", "chrome", "msedge", "playwright", "xvfb")
    ):
        return "browser"
    if "ffmpeg" in command or "ffprobe" in command:
        return "ffmpeg"
    if record.pid in registered_worker_pids or any(
        token in command
        for token in ("web_download_worker", "web-download-worker", " _worker")
    ):
        return "worker"
    return "other"


def _process_tree_status(
    root_pid: int,
    *,
    registered_worker_pids: Sequence[int] = (),
) -> dict[str, object]:
    records = _process_snapshot(root_pid)
    registered = frozenset(int(pid) for pid in registered_worker_pids)
    counts = {role: 0 for role in ("workload", "worker", "browser", "ffmpeg", "other")}
    rss_by_role = dict.fromkeys(counts, 0)
    zombies = 0
    root_token = ""
    for record in records:
        role = _process_role(
            record,
            root_pid=root_pid,
            registered_worker_pids=registered,
        )
        counts[role] += 1
        rss_by_role[role] += record.rss_bytes
        zombies += int(record.state.casefold() in {"z", "zombie"})
        if record.pid == root_pid:
            root_token = record.process_token
    complete = (
        bool(root_token)
        and bool(records)
        and all(record.measured and bool(record.process_token) for record in records)
    )
    return {
        "rss_bytes": sum(record.rss_bytes for record in records),
        "pid_count": len(records),
        "root_process_token": root_token,
        "process_tree_complete": complete,
        "unmeasured_processes": sum(not record.measured for record in records),
        "process_counts": counts,
        "process_rss_bytes": rss_by_role,
        "worker_processes": counts["worker"],
        "browser_processes": counts["browser"],
        "ffmpeg_processes": counts["ffmpeg"],
        "other_processes": counts["other"],
        "zombie_processes": zombies,
    }


def _process_tree_is_idle(status: Mapping[str, object]) -> bool:
    counts = status.get("process_counts")
    if not isinstance(counts, Mapping):
        return False
    pid_ceiling = int(status.get("pid_sample_max", status.get("pid_count", 0)))
    return (
        bool(status.get("process_tree_complete"))
        and pid_ceiling == 1
        and all(
            int(counts.get(role, 0)) == expected
            for role, expected in (
                ("workload", 1),
                ("worker", 0),
                ("browser", 0),
                ("ffmpeg", 0),
                ("other", 0),
            )
        )
        and int(status.get("zombie_processes", 0)) == 0
    )


def _worker_payload_is_alive(payload: Mapping[str, object]) -> bool:
    if payload.get("status") != "running":
        return False
    try:
        pid = int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    token = payload.get("process_token")
    return isinstance(token, str) and bool(token) and _process_token(pid) == token


def _running_worker_pids(
    root: Path,
    *,
    payloads: Sequence[Mapping[str, object]] | None = None,
) -> tuple[int, ...]:
    return tuple(
        identity.pid for identity in _running_worker_identities(root, payloads=payloads)
    )


def _running_worker_identities(
    root: Path,
    *,
    payloads: Sequence[Mapping[str, object]] | None = None,
) -> tuple[ProcessIdentity, ...]:
    identities: dict[tuple[int, str], ProcessIdentity] = {}
    current_payloads = _worker_metric_payloads(root) if payloads is None else payloads
    for payload in current_payloads:
        if not _worker_payload_is_alive(payload):
            continue
        try:
            pid = int(payload["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        token = str(payload.get("process_token") or "")
        if not token:
            continue
        identity = ProcessIdentity(
            pid=pid,
            process_token=token,
            command=f"fixture-worker:{str(payload.get('code') or 'unknown')[:80]}",
        )
        identities[(pid, token)] = identity
    return tuple(
        identities[key] for key in sorted(identities, key=lambda item: item[0])
    )


def _mark_worker_pid(
    root: Path,
    pid: int,
    status: str,
    *,
    expected_token: str | None = None,
) -> None:
    directory = root / WORKER_METRICS_DIR
    if not directory.exists():
        return
    for path in directory.glob(f"{pid}-*.json"):
        try:
            payload = _read_json(path, attempts=1)
        except FaultMatrixError:
            continue
        if (
            expected_token is not None
            and payload.get("process_token") != expected_token
        ):
            continue
        payload["status"] = status
        payload["updated_at"] = time.time()
        _atomic_json(path, payload)


def _aggregate_worker_metrics(
    root: Path,
    *,
    payloads: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, int]:
    totals = {
        "bytes_served": 0,
        "segment_commits": 0,
        "checkpoint_flushes": 0,
        "resegment_resets": 0,
    }
    current_payloads = _worker_metric_payloads(root) if payloads is None else payloads
    for payload in current_payloads:
        for name in totals:
            try:
                totals[name] += max(0, int(payload.get(name, 0)))
            except (TypeError, ValueError):
                continue
    return totals


def _code_metric(root: Path, code: str, name: str) -> int:
    total = 0
    for payload in _worker_metric_payloads(root):
        if payload.get("code") != code:
            continue
        try:
            total += max(0, int(payload.get(name, 0)))
        except (TypeError, ValueError):
            continue
    return total


def _active_metric_codes(
    root: Path,
    name: str,
    minimum: int,
    *,
    started_after: float,
    payloads: Sequence[Mapping[str, object]] | None = None,
) -> frozenset[str]:
    reached: set[str] = set()
    current_payloads = _worker_metric_payloads(root) if payloads is None else payloads
    for payload in current_payloads:
        code = payload.get("code")
        if not isinstance(code, str) or not _worker_payload_is_alive(payload):
            continue
        try:
            started_at = float(payload.get("started_at", 0.0))
            value = int(payload.get(name, 0))
        except (TypeError, ValueError):
            continue
        if started_at >= started_after and value >= minimum:
            reached.add(code)
    return frozenset(reached)


@lru_cache(maxsize=1)
def _windows_process_memory_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return (
        ProcessMemoryCounters,
        ctypes.sizeof,
        ctypes.byref,
        kernel32.GetCurrentProcess,
        psapi.GetProcessMemoryInfo,
    )


def _rss_bytes() -> int:
    if os.name == "nt":
        try:
            (
                counter_type,
                size_of,
                by_reference,
                get_current_process,
                get_process_memory_info,
            ) = _windows_process_memory_api()
            counters = counter_type()  # type: ignore[operator]
            counters.cb = size_of(counters)  # type: ignore[operator, union-attr]
            if get_process_memory_info(  # type: ignore[operator]
                get_current_process(),  # type: ignore[operator]
                by_reference(counters),  # type: ignore[operator]
                counters.cb,
            ):
                return int(counters.WorkingSetSize)
        except (AttributeError, OSError, TypeError, ValueError):
            return 0
        return 0
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
        match = re.search(r"^VmRSS:\s+([0-9]+)\s+kB$", status, re.MULTILINE)
        return int(match.group(1)) * 1024 if match else 0
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return 0


def _collected_rss_bytes() -> int:
    gc.collect()
    return _rss_bytes()


def _write_telemetry(handle: TextIO, payload: Mapping[str, object]) -> None:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    handle.write(raw + "\n")
    handle.flush()


def _filesystem_config(root: Path) -> object:
    from jav_pilot.web_download.config import WebDownloadConfig

    data = root / "data"
    staging = root / "staging"
    library = root / "library"
    for path in (data, staging, library):
        path.mkdir(parents=True, exist_ok=True)
    config = WebDownloadConfig(
        enabled=True,
        database_path=data / DATABASE_FILE,
        staging_path="/qa/staging",
        library_path="/qa/library",
        max_concurrency=8,
        min_free_bytes=0,
        initial_media_bytes=8 * 1024 * 1024,
        max_file_bytes=64 * 1024 * 1024,
    )
    object.__setattr__(config, "staging_path", str(staging))
    object.__setattr__(config, "library_path", str(library))
    return config


def _run_workload(root: Path, profile: Mapping[str, object]) -> int:
    from jav_pilot.missav.browser_gate import MISSAV_BROWSER_GATE
    from jav_pilot.web_download.job_store import WebDownloadStore
    from jav_pilot.web_download.manager import WebDownloadManager
    from jav_pilot.web_download.runner import SubprocessWebDownloadRunner

    state = FaultState(root / STATE_FILE)
    metrics = WorkloadMetrics()
    config = _filesystem_config(root)
    script = Path(__file__).resolve()
    worker_profile = json.dumps(profile, ensure_ascii=True, separators=(",", ":"))
    runner = SubprocessWebDownloadRunner(
        command=(
            sys.executable,
            str(script),
            "_fixture-worker",
            "--root",
            str(root),
            "--profile-json",
            worker_profile,
        ),
        cancel_grace_seconds=2.0,
        capture_retry_delays=(),
    )

    class InstrumentedStore(WebDownloadStore):
        def update(self, job_id: str, **fields: object) -> dict[str, object]:
            if any(
                name in fields
                for name in (
                    "progress",
                    "downloaded_bytes",
                    "total_bytes",
                    "speed",
                    "eta",
                )
            ):
                metrics.progress_write()
            return super().update(job_id, **fields)

    store = InstrumentedStore(config.database_path)
    manager = WebDownloadManager(
        config,
        store=store,
        runner=runner,
        start_workers=False,
        storage_reservations=None,
    )
    if manager.count() == 0:
        identifiers = iter(f"{index:032x}" for index in range(1, 10))
        manager._id_factory = lambda: next(identifiers)
        for code in FIXTURE_CODES:
            manager.start(code, f"qa04-{code.lower()}", 720)
    manager.start_workers()
    last_retry: dict[str, float] = {}
    worker_metric_cache = WorkerMetricSnapshotCache(root)
    telemetry_path = root / TELEMETRY_FILE
    status_path = root / STATUS_FILE
    telemetry_interval_seconds = _workload_telemetry_interval(profile)
    telemetry_handle: TextIO | None = None
    completed_memory_probe = -1
    exit_code = 0
    try:
        telemetry_handle = telemetry_path.open("a", encoding="ascii", newline="\n")
        while True:
            current_state = state.read()
            # The controller only needs queue state. The public manager view also
            # probes every completed archive and would make the measurement loop,
            # rather than the download manager, dominate long-idle RSS.
            jobs = store.list(limit=100)
            if not state.fault_active():
                now = time.monotonic()
                for job in jobs:
                    if job.get("status") != "failed":
                        continue
                    job_id = str(job["job_id"])
                    if now - last_retry.get(job_id, 0.0) < 0.25:
                        continue
                    last_retry[job_id] = now
                    try:
                        manager.retry(job_id)
                    except Exception:
                        pass
                jobs = store.list(limit=100)
            counts: dict[str, int] = {}
            for job in jobs:
                status = str(job["status"])
                counts[status] = counts.get(status, 0) + 1
            worker_payloads = worker_metric_cache.snapshot()
            worker_pids = _running_worker_pids(root, payloads=worker_payloads)
            worker_totals = _aggregate_worker_metrics(
                root,
                payloads=worker_payloads,
            )
            requested_memory_probe = int(current_state.get("memory_probe", 0))
            if requested_memory_probe != completed_memory_probe:
                gc.collect()
                completed_memory_probe = requested_memory_probe
            process_tree = _process_tree_status(
                os.getpid(),
                registered_worker_pids=worker_pids,
            )
            metrics.observe_workers(int(process_tree["worker_processes"]))
            progress_writes, worker_peak = metrics.snapshot()
            payload: dict[str, object] = {
                "boot_id": metrics.boot_id,
                "pid": os.getpid(),
                "timestamp": time.time(),
                "active": sum(counts.get(status, 0) for status in ACTIVE_STATUSES),
                "queued": counts.get("queued", 0),
                "failed": counts.get("failed", 0),
                "completed": counts.get("completed", 0),
                "worker_threads": sum(
                    thread.name.startswith("jav-web-download-")
                    and not thread.name.startswith("jav-web-download-output-")
                    for thread in threading.enumerate()
                ),
                "worker_process_peak": worker_peak,
                "browser_gate_active": MISSAV_BROWSER_GATE.active_count,
                "browser_gate_peak": MISSAV_BROWSER_GATE.active_peak,
                "db_progress_writes": progress_writes,
                "memory_probe": completed_memory_probe,
                **process_tree,
                **worker_totals,
            }
            _atomic_json(status_path, payload)
            _write_telemetry(telemetry_handle, payload)
            if current_state.get("shutdown"):
                break
            if counts.get("completed", 0) == len(FIXTURE_CODES):
                time.sleep(telemetry_interval_seconds)
                continue
            time.sleep(telemetry_interval_seconds)
    except BaseException:
        exit_code = 1
        raise
    finally:
        stopped = manager.shutdown(timeout=10.0)
        if not stopped:
            exit_code = 1
        final_process_tree = _process_tree_status(
            os.getpid(),
            registered_worker_pids=_running_worker_pids(root),
        )
        final_payload = {
            "boot_id": metrics.boot_id,
            "pid": os.getpid(),
            "timestamp": time.time(),
            "stopped": stopped,
            "browser_gate_peak": MISSAV_BROWSER_GATE.active_peak,
            "final": True,
            **final_process_tree,
        }
        if telemetry_handle is not None:
            try:
                _write_telemetry(telemetry_handle, final_payload)
            finally:
                telemetry_handle.close()
    return exit_code


def _workload_telemetry_interval(profile: Mapping[str, object]) -> float:
    mode = profile.get("mode")
    if mode == "ci":
        return 0.2
    if mode == "release":
        return 1.0
    raise FaultMatrixError("load mode must be ci or release")


def _wait_for_workload_start(gate: Path) -> None:
    deadline = time.monotonic() + WINDOWS_WORKLOAD_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            gate.unlink()
            return
        except FileNotFoundError:
            time.sleep(0.01)
    raise FaultMatrixError("Windows workload was not admitted by its owner")


def _start_workload(root: Path, profile: MatrixProfile) -> subprocess.Popen[bytes]:
    environment = dict(os.environ)
    environment["JAV_PILOT_MISSAV_BROWSER_CONCURRENCY"] = "2"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_workload",
        "--root",
        str(root),
        "--profile-json",
        json.dumps(profile.worker_payload(), ensure_ascii=True, separators=(",", ":")),
    ]
    start_gate: Path | None = None
    if os.name == "nt":
        start_gate = root / f".workload-start-{uuid.uuid4().hex}.gate"
        command.extend(("--start-gate", str(start_gate)))
    log_path = root / "workload.log"
    log = log_path.open("ab", buffering=0)
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "cwd": str(REPOSITORY_ROOT),
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    finally:
        log.close()
    identity: ProcessIdentity | None = None
    try:
        identity = _remember_workload_identity(process, command)
        _attach_windows_kill_job(process)
        if start_gate is not None:
            start_gate.touch(exist_ok=False)
    except Exception as start_error:
        terminated = False
        cleanup_error: subprocess.TimeoutExpired | None = None
        try:
            if os.name == "nt":
                try:
                    terminated = _terminate_windows_job(process)
                except FaultMatrixError:
                    terminated = False
            if not terminated and process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=WINDOWS_PROCESS_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired as exc:
                    cleanup_error = exc
        finally:
            _close_windows_job(process)
            if start_gate is not None:
                try:
                    start_gate.unlink()
                except FileNotFoundError:
                    pass
        audit_identity = identity or ProcessIdentity(
            pid=process.pid,
            process_token="unavailable",
            command=subprocess.list2cmdline(command)[:2048],
        )
        _append_cleanup_audit(
            root,
            identity=audit_identity,
            result="failed" if cleanup_error is not None else "completed",
            strategy="windows-startup-job" if os.name == "nt" else "process-group",
            detail=(
                "startup cleanup timed out"
                if cleanup_error is not None
                else type(start_error).__name__
            ),
        )
        if cleanup_error is not None:
            raise _fault_matrix_error_with_cleanup_audit(
                root,
                "workload startup cleanup did not terminate its root process",
            ) from cleanup_error
        raise
    return process


def _status(root: Path) -> dict[str, object]:
    return _read_json(root / STATUS_FILE)


def _request_memory_probe(
    root: Path,
    state: FaultState,
    process: subprocess.Popen[bytes],
    timeout: float,
    *,
    sample_count: int = 3,
) -> dict[str, object]:
    if not 1 <= sample_count <= 9:
        raise FaultMatrixError("memory sample count is invalid")
    deadline = time.monotonic() + timeout
    samples: list[dict[str, object]] = []
    for _ in range(sample_count):
        requested = int(state.read().get("memory_probe", 0)) + 1
        requested_at = time.time()
        state.update(memory_probe=requested)
        remaining = max(0.1, deadline - time.monotonic())
        sample = _wait_until(
            "collected workload RSS sample",
            lambda: (
                payload
                if (payload := _status(root)).get("memory_probe") == requested
                and payload.get("pid") == process.pid
                and float(payload.get("timestamp", 0.0)) >= requested_at
                else None
            ),
            remaining,
            process=process,
        )
        samples.append(dict(sample))  # type: ignore[arg-type]

    identities = {
        (
            str(sample.get("boot_id") or ""),
            int(sample.get("pid", 0)),
            str(sample.get("root_process_token") or ""),
        )
        for sample in samples
    }
    if len(identities) != 1 or any(not value for value in next(iter(identities))):
        raise FaultMatrixError("memory samples crossed workload process boots")
    if any(
        not bool(sample.get("process_tree_complete"))
        or not isinstance(sample.get("process_counts"), Mapping)
        for sample in samples
    ):
        raise FaultMatrixError("memory process tree sample was incomplete")
    rss_values = sorted(int(sample.get("rss_bytes", 0)) for sample in samples)
    pid_values = sorted(int(sample.get("pid_count", 0)) for sample in samples)
    roles = ("workload", "worker", "browser", "ffmpeg", "other")
    process_counts = {
        role: max(
            int(sample["process_counts"].get(role, 0))  # type: ignore[union-attr]
            for sample in samples
        )
        for role in roles
    }
    result = dict(samples[-1])
    result.update(
        {
            "rss_bytes": rss_values[len(rss_values) // 2],
            "rss_sample_count": len(rss_values),
            "rss_sample_min_bytes": rss_values[0],
            "rss_sample_max_bytes": rss_values[-1],
            "pid_count": pid_values[len(pid_values) // 2],
            "pid_sample_min": pid_values[0],
            "pid_sample_max": pid_values[-1],
            "process_counts": process_counts,
            "zombie_processes": max(
                int(sample.get("zombie_processes", 0)) for sample in samples
            ),
            "unmeasured_processes": max(
                int(sample.get("unmeasured_processes", 0)) for sample in samples
            ),
        }
    )
    return result


def _wait_until(
    description: str,
    predicate: Callable[[], object],
    timeout: float,
    *,
    process: subprocess.Popen[bytes] | None = None,
) -> object:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise FaultMatrixError(
                f"workload exited while waiting for {description}: {process.returncode}"
            )
        try:
            value = predicate()
        except (FaultMatrixError, OSError, sqlite3.Error) as exc:
            last_error = exc
        else:
            if value:
                return value
        time.sleep(0.05)
    suffix = f": {last_error}" if last_error is not None else ""
    raise FaultMatrixError(f"timed out waiting for {description}{suffix}")


def _wait_admission(
    root: Path,
    process: subprocess.Popen[bytes],
    timeout: float,
    *,
    after_timestamp: float = 0.0,
) -> dict[str, object]:
    return _wait_until(
        "eight active tasks and one queued task",
        lambda: (
            payload
            if (payload := _status(root)).get("active") == 8
            and payload.get("queued") == 1
            and payload.get("worker_processes") == 8
            and payload.get("pid") == process.pid
            and float(payload.get("timestamp", 0.0)) >= after_timestamp
            else None
        ),
        timeout,
        process=process,
    )  # type: ignore[return-value]


def _wait_metric(
    root: Path,
    name: str,
    minimum: int,
    timeout: float,
    process: subprocess.Popen[bytes],
) -> int:
    return int(
        _wait_until(
            f"worker metric {name} >= {minimum}",
            lambda: (
                value
                if (value := int(_aggregate_worker_metrics(root).get(name, 0)))
                >= minimum
                else 0
            ),
            timeout,
            process=process,
        )
    )


def _throughput_sample(
    root: Path,
    process: subprocess.Popen[bytes],
    seconds: float,
) -> float:
    cache = WorkerMetricSnapshotCache(root)

    def metric_points(*, require_alive: bool) -> dict[str, tuple[float, int]]:
        points: dict[str, tuple[float, int]] = {}
        for payload in cache.snapshot():
            if payload.get("status") != "running":
                continue
            boot_id = payload.get("boot_id")
            if not isinstance(boot_id, str) or not boot_id:
                continue
            if require_alive and not _worker_payload_is_alive(payload):
                continue
            try:
                updated_at = float(payload["updated_monotonic"])
                bytes_served = int(payload.get("bytes_served", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(updated_at) or updated_at <= 0 or bytes_served < 0:
                continue
            points[boot_id] = (updated_at, bytes_served)
        return points

    initial = metric_points(require_alive=True)
    if len(initial) != 8:
        raise FaultMatrixError(
            "throughput sample requires exactly eight live fixture workers"
        )

    observations: dict[str, list[tuple[float, int]]] = {
        boot_id: [] for boot_id in initial
    }

    def record_progress() -> None:
        for boot_id, point in metric_points(require_alive=False).items():
            if boot_id not in observations:
                continue
            updated_at, bytes_served = point
            initial_updated_at, initial_bytes = initial[boot_id]
            previous = observations[boot_id][-1] if observations[boot_id] else None
            if updated_at <= initial_updated_at:
                continue
            if bytes_served < initial_bytes or (
                previous is not None and bytes_served < previous[1]
            ):
                raise FaultMatrixError(
                    "fixture worker byte counter regressed during throughput sample"
                )
            if previous is not None and updated_at <= previous[0]:
                continue
            if bytes_served == (previous[1] if previous is not None else initial_bytes):
                continue
            observations[boot_id].append(point)

    # Checkpoint metrics are intentionally persisted in 16-segment batches. Align
    # every worker to its next completed batch so short CI samples do not compare
    # arbitrary, asynchronous batch phases at their endpoints.
    alignment_deadline = time.monotonic() + max(5.0, min(seconds, 30.0))
    while any(not points for points in observations.values()):
        if process.poll() is not None:
            raise FaultMatrixError("workload exited during throughput sample")
        record_progress()
        remaining = alignment_deadline - time.monotonic()
        if any(not points for points in observations.values()) and remaining <= 0:
            raise FaultMatrixError(
                "timed out aligning fixture worker throughput samples"
            )
        if any(not points for points in observations.values()):
            time.sleep(min(0.05, max(0.01, remaining)))

    deadline = time.monotonic() + seconds
    while True:
        if process.poll() is not None:
            raise FaultMatrixError("workload exited during throughput sample")
        record_progress()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, max(0.01, remaining)))

    final = metric_points(require_alive=True)
    if final.keys() != initial.keys():
        raise FaultMatrixError("fixture workers changed during throughput sample")

    rates: list[float] = []
    for points in observations.values():
        if len(points) < 2:
            raise FaultMatrixError(
                "fixture worker produced too few durable throughput samples"
            )
        first_updated_at, first_bytes = points[0]
        last_updated_at, last_bytes = points[-1]
        elapsed = last_updated_at - first_updated_at
        transferred = last_bytes - first_bytes
        if elapsed <= 0 or transferred <= 0:
            raise FaultMatrixError("fixture worker throughput sample was empty")
        rates.append(transferred / elapsed)
    return sum(rates)


def _terminate_identity(identity: ProcessIdentity) -> bool:
    if identity.pid in {os.getpid(), os.getppid()}:
        raise FaultMatrixError("refusing to terminate the current process tree")
    if os.name == "nt":
        try:
            (
                dword_type,
                filetime_type,
                by_reference,
                open_process,
                get_exit_code,
                get_process_times,
                terminate_process,
                close_handle,
            ) = _windows_process_query_api()
            handle = open_process(0x1001, False, identity.pid)  # type: ignore[operator]
            if not handle:
                return False
            try:
                exit_code = dword_type()  # type: ignore[operator]
                created = filetime_type()  # type: ignore[operator]
                exited = filetime_type()  # type: ignore[operator]
                kernel = filetime_type()  # type: ignore[operator]
                user = filetime_type()  # type: ignore[operator]
                if (
                    not get_exit_code(  # type: ignore[operator]
                        handle,
                        by_reference(exit_code),  # type: ignore[operator]
                    )
                    or int(exit_code.value) != 259
                ):
                    return False
                if not get_process_times(  # type: ignore[operator]
                    handle,
                    by_reference(created),  # type: ignore[operator]
                    by_reference(exited),  # type: ignore[operator]
                    by_reference(kernel),  # type: ignore[operator]
                    by_reference(user),  # type: ignore[operator]
                ):
                    return False
                token = str(
                    (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                )
                if token != identity.process_token:
                    return False
                return bool(
                    terminate_process(handle, 1)  # type: ignore[operator]
                )
            finally:
                close_handle(handle)  # type: ignore[operator]
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    if not _identity_is_current(identity):
        return False
    try:
        os.kill(identity.pid, signal.SIGKILL)
    except OSError:
        return False
    return True


def _kill_one_worker(root: Path, code: str) -> int:
    candidates: list[tuple[float, ProcessIdentity]] = []
    for payload in _worker_metric_payloads(root):
        if payload.get("code") != code or not _worker_payload_is_alive(payload):
            continue
        try:
            pid = int(payload["pid"])
            started_at = float(payload.get("started_at", 0.0))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        token = str(payload.get("process_token") or "")
        if not token:
            continue
        candidates.append(
            (
                started_at,
                ProcessIdentity(
                    pid=pid,
                    process_token=token,
                    command=f"fixture-worker:{code}",
                ),
            )
        )
    if not candidates:
        raise FaultMatrixError(f"no active worker found for {code}")
    identity = max(candidates, key=lambda candidate: (candidate[0], candidate[1].pid))[
        1
    ]
    _terminate_identity(identity)
    _wait_until(
        f"worker {identity.pid} to exit",
        lambda: not _identity_is_current(identity),
        5.0,
    )
    _mark_worker_pid(
        root,
        identity.pid,
        "killed",
        expected_token=identity.process_token,
    )
    return identity.pid


def _fallback_tree_identities(
    root_identity: ProcessIdentity,
    records: Sequence[ProcessRecord],
    workers: Sequence[ProcessIdentity],
) -> tuple[ProcessIdentity, ...]:
    records_by_pid = {record.pid: record for record in records}
    if len(records_by_pid) != len(records):
        raise FaultMatrixError("fallback process tree contains duplicate PIDs")
    root_record = records_by_pid.get(root_identity.pid)
    if (
        root_record is None
        or not root_record.measured
        or root_record.process_token != root_identity.process_token
    ):
        raise FaultMatrixError("fallback process tree root identity is incomplete")

    def creation_time(record: ProcessRecord) -> int:
        if not record.measured or not record.process_token:
            raise FaultMatrixError(
                "fallback process tree contains an unverified identity"
            )
        try:
            value = int(record.process_token)
        except (TypeError, ValueError) as exc:
            raise FaultMatrixError(
                "fallback process tree contains an invalid creation time"
            ) from exc
        if value <= 0:
            raise FaultMatrixError(
                "fallback process tree contains an invalid creation time"
            )
        return value

    creation_time(root_record)
    identities: dict[tuple[int, str], ProcessIdentity] = {}
    depths: dict[int, int] = {root_identity.pid: 0}
    for record in records:
        if record.pid == root_identity.pid:
            continue
        cursor = record
        seen: set[int] = set()
        depth = 0
        while cursor.pid != root_identity.pid:
            if cursor.pid in seen:
                raise FaultMatrixError("fallback process tree contains a parent cycle")
            seen.add(cursor.pid)
            parent = records_by_pid.get(cursor.parent_pid)
            if parent is None:
                raise FaultMatrixError(
                    "fallback process tree parent identity is incomplete"
                )
            if creation_time(cursor) < creation_time(parent):
                raise FaultMatrixError(
                    "fallback process tree contains a stale parent PID"
                )
            cursor = parent
            depth += 1
        depths[record.pid] = depth
        identity = ProcessIdentity(
            pid=record.pid,
            process_token=record.process_token,
            command=record.command[:2048],
        )
        identities[(identity.pid, identity.process_token)] = identity

    for worker in workers:
        record = records_by_pid.get(worker.pid)
        if record is None or record.process_token != worker.process_token:
            if _identity_is_current(worker):
                raise FaultMatrixError(
                    "fallback worker identity is outside the verified process tree"
                )
            continue
        identities[(worker.pid, worker.process_token)] = worker
    return tuple(
        sorted(
            identities.values(),
            key=lambda identity: (depths[identity.pid], identity.pid),
            reverse=True,
        )
    )


def _kill_workload_tree(root: Path, process: subprocess.Popen[bytes]) -> None:
    identity = _workload_identity(process)
    cleanup_key = (identity.pid, identity.process_token)
    with _CLEANUP_STATE:
        state = _CLEANUP_RESULTS.get(cleanup_key)
        if state == "completed":
            return
        if state == "failed":
            raise _fault_matrix_error_with_cleanup_audit(
                root,
                "workload cleanup previously failed",
            )
        if state == "running":
            deadline = time.monotonic() + WINDOWS_PROCESS_EXIT_TIMEOUT_SECONDS
            while _CLEANUP_RESULTS.get(cleanup_key) == "running":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _fault_matrix_error_with_cleanup_audit(
                        root,
                        "workload cleanup is already in progress",
                    )
                _CLEANUP_STATE.wait(timeout=min(0.1, remaining))
            if _CLEANUP_RESULTS.get(cleanup_key) == "completed":
                return
            raise _fault_matrix_error_with_cleanup_audit(
                root,
                "workload cleanup previously failed",
            )
        _CLEANUP_RESULTS[cleanup_key] = "running"

    started = time.monotonic()
    strategy = "windows-job-object" if os.name == "nt" else "process-group"
    worker_identities = _running_worker_identities(root)
    records: tuple[ProcessRecord, ...] = ()
    succeeded = False
    try:
        if process.poll() is not None:
            _close_windows_job(process)
        else:
            if not _popen_identity_is_current(process, identity):
                raise FaultMatrixError(
                    "workload process identity changed before cleanup"
                )
            records = _process_snapshot(identity.pid)
            root_record = next(
                (record for record in records if record.pid == identity.pid),
                None,
            )
            if root_record is not None and (
                not root_record.measured
                or root_record.process_token != identity.process_token
            ):
                raise FaultMatrixError(
                    "workload snapshot identity changed before cleanup"
                )

            if os.name == "nt":
                try:
                    if not _terminate_windows_job(process):
                        raise FaultMatrixError("Windows workload job is unavailable")
                except FaultMatrixError:
                    strategy = "verified-process-fallback"
                    if process.poll() is None:
                        process.kill()
                    verified_children = _fallback_tree_identities(
                        identity, records, worker_identities
                    )
                    for child in verified_children:
                        _terminate_identity(child)
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                for worker in worker_identities:
                    _terminate_identity(worker)

            try:
                process.wait(timeout=WINDOWS_PROCESS_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if not _popen_identity_is_current(process, identity):
                    process.wait(timeout=1.0)
                else:
                    process.kill()
                    process.wait(timeout=2.0)

        remaining_workers = {
            (worker.pid, worker.process_token): worker
            for worker in (*worker_identities, *_running_worker_identities(root))
        }
        for worker in remaining_workers.values():
            _terminate_identity(worker)
        _wait_until(
            "killed workload worker processes to exit",
            lambda: not any(
                _identity_is_current(worker) for worker in remaining_workers.values()
            ),
            WINDOWS_PROCESS_EXIT_TIMEOUT_SECONDS,
        )
        for worker in remaining_workers.values():
            _mark_worker_pid(
                root,
                worker.pid,
                "killed",
                expected_token=worker.process_token,
            )
        succeeded = True
        _append_cleanup_audit(
            root,
            identity=identity,
            result="completed",
            strategy=strategy,
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:
        _append_cleanup_audit(
            root,
            identity=identity,
            result="failed",
            strategy=strategy,
            duration_seconds=time.monotonic() - started,
            detail=type(exc).__name__,
        )
        if isinstance(exc, subprocess.TimeoutExpired):
            raise _fault_matrix_error_with_cleanup_audit(
                root,
                "workload process tree cleanup timed out",
            ) from exc
        raise
    finally:
        _close_windows_job(process)
        with _CLEANUP_STATE:
            _CLEANUP_RESULTS[cleanup_key] = "completed" if succeeded else "failed"
            _CLEANUP_STATE.notify_all()


def _graceful_restart(
    root: Path,
    state: FaultState,
    process: subprocess.Popen[bytes],
    profile: MatrixProfile,
) -> subprocess.Popen[bytes]:
    state.update(shutdown=True)
    try:
        process.wait(timeout=profile.recovery_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_workload_tree(root, process)
        raise FaultMatrixError("workload did not stop for service restart") from exc
    if process.returncode != 0:
        raise FaultMatrixError("workload failed during service restart")
    _close_windows_job(process)
    _wait_no_workers(root)
    state.update(shutdown=False)
    replacement = _start_workload(root, profile)
    _wait_admission(root, replacement, profile.startup_seconds)
    return replacement


def _hold_sqlite_lock(database: Path, seconds: float) -> None:
    connection = sqlite3.connect(database, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN EXCLUSIVE")
        time.sleep(seconds)
        connection.commit()
    finally:
        connection.close()


def _database_counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database, timeout=5.0)
    try:
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM web_download_jobs GROUP BY status"
        ).fetchall()
        return {str(status): int(count) for status, count in rows}
    finally:
        connection.close()


def _wait_completed(
    database: Path,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> None:
    try:
        _wait_until(
            "all fixture downloads to complete",
            lambda: _database_counts(database).get("completed", 0)
            == len(FIXTURE_CODES),
            timeout,
            process=process,
        )
    except FaultMatrixError as exc:
        try:
            counts = _database_counts(database)
        except sqlite3.Error:
            raise exc
        summary = (
            ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "none"
        )
        raise FaultMatrixError(f"{exc}; final fixture states: {summary}") from exc


def _telemetry_rows(root: Path) -> list[dict[str, object]]:
    path = root / TELEMETRY_FILE
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="ascii").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _telemetry_max(root: Path, name: str) -> int:
    values: list[int] = []
    for payload in _telemetry_rows(root):
        try:
            values.append(int(payload.get(name, 0)))
        except (TypeError, ValueError):
            continue
    return max(values, default=0)


def _telemetry_sum_by_boot(root: Path, name: str) -> int:
    maxima: dict[str, int] = {}
    for payload in _telemetry_rows(root):
        boot_id = str(payload.get("boot_id") or "")
        if not boot_id:
            continue
        try:
            value = int(payload.get(name, 0))
        except (TypeError, ValueError):
            continue
        maxima[boot_id] = max(maxima.get(boot_id, 0), value)
    return sum(maxima.values())


def _sqlite_integrity(database: Path) -> str:
    connection = sqlite3.connect(database, timeout=5.0)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "missing"
    finally:
        connection.close()


def _archive_validation(root: Path, profile: MatrixProfile) -> dict[str, object]:
    library = root / "library"
    files = sorted(path for path in library.rglob("*") if path.is_file())
    expected_paths = {_fixture_archive_path(library, code) for code in FIXTURE_CODES}
    actual_paths = set(files)
    mismatches: list[str] = []
    for code in FIXTURE_CODES:
        path = _fixture_archive_path(library, code)
        if not path.is_file():
            mismatches.append(code)
            continue
        generation = 2 if code == "FXT-001" else 1
        expected = reference_digest(
            code,
            generation,
            profile.segment_count,
            profile.segment_bytes,
        )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            mismatches.append(code)
    return {
        "file_count": len(files),
        "expected_file_count": len(expected_paths),
        "duplicate_or_extra_files": sorted(
            path.relative_to(library).as_posix()
            for path in actual_paths - expected_paths
        ),
        "missing_files": sorted(
            path.relative_to(library).as_posix()
            for path in expected_paths - actual_paths
        ),
        "digest_mismatches": mismatches,
        "reference_digest_match": not mismatches and actual_paths == expected_paths,
    }


def _fixture_archive_path(library: Path, code: str) -> Path:
    layout = plan_archive_layout(
        code=code,
        title=None,
        release_date=None,
        suffix=".mp4",
        variant="original",
    )
    return library.joinpath(*layout.relative_media_path.parts)


def _staging_residue(root: Path) -> list[str]:
    staging = root / "staging"
    if not staging.exists():
        return []
    return sorted(path.relative_to(staging).as_posix() for path in staging.rglob("*"))


def _wait_no_workers(root: Path, timeout: float = 10.0) -> None:
    _wait_until(
        "fixture worker processes to exit",
        lambda: not _running_worker_pids(root),
        timeout,
    )


def _prepare_run_root(base: Path | None) -> tuple[Path, Callable[[], None]]:
    if base is None:
        temporary = tempfile.TemporaryDirectory(prefix="jav-pilot-qa04-")
        root = Path(temporary.name)

        def cleanup() -> None:
            temporary.cleanup()

    else:
        base = base.expanduser().resolve()
        base_posix = base.as_posix()
        if any(
            base_posix == production or base_posix.startswith(f"{production}/")
            for production in PRODUCTION_ROOTS
        ):
            raise FaultMatrixError(
                "production paths cannot be used by the load fixture"
            )
        base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="qa04-", dir=base))

        def cleanup() -> None:
            if (root / RUN_SENTINEL).is_file():
                shutil.rmtree(root)

    (root / RUN_SENTINEL).write_text("isolated\n", encoding="ascii", newline="\n")
    (root / WORKER_METRICS_DIR).mkdir()
    _atomic_json(root / STATE_FILE, _initial_state())
    return root, cleanup


def run_fault_matrix(
    profile: MatrixProfile,
    *,
    work_root: Path | None = None,
    keep_work: bool = False,
) -> dict[str, object]:
    profile.validate()
    if os.name == "nt" and not _windows_fault_matrix_enabled():
        raise FaultMatrixPlatformSkip(
            "the full eight-way fault matrix is Linux-CI-only on Windows; "
            f"set {WINDOWS_MATRIX_OPT_IN}=1 only for an explicitly isolated run"
        )
    with _acquire_fault_matrix_run_lease():
        return _run_fault_matrix_owned(
            profile,
            work_root=work_root,
            keep_work=keep_work,
        )


def _run_fault_matrix_owned(
    profile: MatrixProfile,
    *,
    work_root: Path | None = None,
    keep_work: bool = False,
) -> dict[str, object]:
    root, cleanup = _prepare_run_root(work_root)
    state = FaultState(root / STATE_FILE)
    process: subprocess.Popen[bytes] | None = None
    faults: list[dict[str, object]] = []
    load_started_at = 0.0
    rss_baseline = 0
    pid_baseline = 0
    baseline_memory_identity: tuple[str, int, str] | None = None
    baseline_rss_range = (0, 0)
    try:
        initial_worker_marker = time.time()
        process = _start_workload(root, profile)
        admission = _wait_admission(root, process, profile.startup_seconds)
        load_started_at = time.monotonic()
        _wait_until(
            "a durable checkpoint batch for every active fixture",
            lambda: len(
                _active_metric_codes(
                    root,
                    "checkpoint_flushes",
                    1,
                    started_after=initial_worker_marker,
                )
            )
            >= 8,
            profile.startup_seconds,
            process=process,
        )
        baseline_bps = _throughput_sample(root, process, profile.baseline_seconds)
        if baseline_bps <= 0:
            raise FaultMatrixError("fault-free throughput baseline was empty")
        generation = dict(state.read().get("generation", {}))
        generation["FXT-001"] = 2
        state.update(generation=generation)
        started = time.monotonic()
        _kill_workload_tree(root, process)
        faults.append(
            {
                "name": "service_process_tree_sigkill",
                "recovered_seconds": None,
            }
        )
        process = _start_workload(root, profile)
        _wait_admission(root, process, profile.startup_seconds)
        _wait_metric(
            root,
            "resegment_resets",
            1,
            profile.recovery_seconds,
            process,
        )
        faults[-1]["recovered_seconds"] = round(time.monotonic() - started, 3)

        started = time.monotonic()
        killed_worker = _kill_one_worker(root, "FXT-002")
        recovery_marker = time.time()
        _wait_admission(
            root,
            process,
            profile.recovery_seconds,
            after_timestamp=recovery_marker,
        )
        faults.append(
            {
                "name": "worker_sigkill",
                "worker_pid": killed_worker,
                "recovered_seconds": round(time.monotonic() - started, 3),
            }
        )

        started = time.monotonic()
        _hold_sqlite_lock(
            root / "data" / DATABASE_FILE,
            profile.sqlite_lock_seconds,
        )
        _wait_admission(
            root,
            process,
            profile.recovery_seconds,
            after_timestamp=time.time(),
        )
        faults.append(
            {
                "name": "sqlite_exclusive_lock",
                "recovered_seconds": round(time.monotonic() - started, 3),
            }
        )

        failed_before = int(_status(root).get("failed", 0))
        started = time.monotonic()
        state.update(io_fault=True)
        _wait_until(
            "isolated staging I/O failure",
            lambda: int(_status(root).get("failed", 0)) > failed_before,
            profile.recovery_seconds,
            process=process,
        )
        time.sleep(profile.io_fault_seconds)
        state.update(io_fault=False)
        _wait_admission(
            root,
            process,
            profile.recovery_seconds,
            after_timestamp=time.time(),
        )
        faults.append(
            {
                "name": "nas_io_short_disconnect",
                "recovered_seconds": round(time.monotonic() - started, 3),
            }
        )

        failed_before = int(_status(root).get("failed", 0))
        started = time.monotonic()
        state.update(low_disk=True)
        _wait_until(
            "low disk failure",
            lambda: int(_status(root).get("failed", 0)) > failed_before,
            profile.recovery_seconds,
            process=process,
        )
        time.sleep(profile.low_disk_seconds)
        state.update(low_disk=False)
        _wait_admission(
            root,
            process,
            profile.recovery_seconds,
            after_timestamp=time.time(),
        )
        faults.append(
            {
                "name": "low_disk",
                "recovered_seconds": round(time.monotonic() - started, 3),
            }
        )

        started = time.monotonic()
        restart_worker_marker = time.time()
        process = _graceful_restart(root, state, process, profile)
        _wait_until(
            "a post-restart durable checkpoint batch for every active fixture",
            lambda: len(
                _active_metric_codes(
                    root,
                    "checkpoint_flushes",
                    1,
                    started_after=restart_worker_marker,
                )
            )
            >= 8,
            profile.recovery_seconds,
            process=process,
        )
        faults.append(
            {
                "name": "service_restart",
                "recovered_seconds": round(time.monotonic() - started, 3),
            }
        )

        post_fault_bps = _throughput_sample(
            root,
            process,
            profile.post_fault_seconds,
        )
        throughput_ratio = post_fault_bps / baseline_bps
        if throughput_ratio < 0.70:
            raise FaultMatrixError(
                "post-fault throughput ratio "
                f"{throughput_ratio:.3f} is below 0.70 "
                f"(baseline={baseline_bps:.3f} B/s, "
                f"post_fault={post_fault_bps:.3f} B/s)"
            )

        remaining = profile.load_seconds - (time.monotonic() - load_started_at)
        if remaining > 0:
            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise FaultMatrixError("workload exited during the load window")
                time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))

        state.update(fast=True, io_fault=False, low_disk=False)
        convergence_started = time.monotonic()
        _wait_completed(
            root / "data" / DATABASE_FILE,
            process,
            profile.convergence_seconds,
        )
        convergence_seconds = time.monotonic() - convergence_started
        if convergence_seconds > 120.0:
            raise FaultMatrixError("fault recovery exceeded 120 seconds")

        # Compare two quiescent samples from the same workload boot. An active
        # eight-worker baseline would otherwise let every worker leak at idle.
        quiescence_started_at = time.time()
        _wait_until(
            "workload descendants to quiesce",
            lambda: (
                payload
                if (payload := _status(root)).get("pid") == process.pid
                and float(payload.get("timestamp", 0.0)) >= quiescence_started_at
                and _process_tree_is_idle(payload)
                else None
            ),
            profile.recovery_seconds,
            process=process,
        )
        baseline_status = _request_memory_probe(
            root,
            state,
            process,
            profile.recovery_seconds,
        )
        if not _process_tree_is_idle(baseline_status):
            raise FaultMatrixError("memory baseline process tree was not idle")
        rss_baseline = int(baseline_status.get("rss_bytes", 0))
        pid_baseline = int(baseline_status.get("pid_count", 0))
        baseline_memory_identity = (
            str(baseline_status.get("boot_id") or ""),
            int(baseline_status.get("pid", 0)),
            str(baseline_status.get("root_process_token") or ""),
        )
        baseline_rss_range = (
            int(baseline_status.get("rss_sample_min_bytes", 0)),
            int(baseline_status.get("rss_sample_max_bytes", 0)),
        )
        time.sleep(profile.idle_seconds)
        end_status = _request_memory_probe(
            root,
            state,
            process,
            profile.recovery_seconds,
        )
        rss_end = int(end_status.get("rss_bytes", 0))
        pid_end = int(end_status.get("pid_count", 0))
        end_memory_identity = (
            str(end_status.get("boot_id") or ""),
            int(end_status.get("pid", 0)),
            str(end_status.get("root_process_token") or ""),
        )
        memory_boot_consistent = (
            baseline_memory_identity is not None
            and baseline_memory_identity == end_memory_identity
        )
        end_rss_range = (
            int(end_status.get("rss_sample_min_bytes", 0)),
            int(end_status.get("rss_sample_max_bytes", 0)),
        )

        state.update(shutdown=True)
        process.wait(timeout=profile.recovery_seconds)
        if process.returncode != 0:
            raise FaultMatrixError("workload failed during final shutdown")
        _close_windows_job(process)
        _wait_no_workers(root)
        process = None

        database = root / "data" / DATABASE_FILE
        archive = _archive_validation(root, profile)
        staging_residue = _staging_residue(root)
        worker_totals = _aggregate_worker_metrics(root)
        progress_writes = _telemetry_sum_by_boot(root, "db_progress_writes")
        worker_peak = _telemetry_max(root, "worker_processes")
        worker_thread_peak = _telemetry_max(root, "worker_threads")
        browser_gate_peak = _telemetry_max(root, "browser_gate_peak")
        browser_process_peak = _telemetry_max(root, "browser_processes")
        ffmpeg_process_peak = _telemetry_max(root, "ffmpeg_processes")
        other_process_peak = _telemetry_max(root, "other_processes")
        checkpoint_budget = math.ceil(worker_totals["segment_commits"] / 4) + 64
        progress_write_budget = math.ceil(worker_totals["segment_commits"] / 4) + 128
        process_tree_complete = bool(
            baseline_status.get("process_tree_complete")
            and end_status.get("process_tree_complete")
        )
        rss_budget_ok = (
            memory_boot_consistent
            and process_tree_complete
            and rss_baseline > 0
            and rss_end > 0
            and rss_end <= math.ceil(rss_baseline * 1.15)
        )
        pid_budget_ok = (
            memory_boot_consistent
            and pid_baseline == 1
            and pid_end == 1
            and pid_end <= math.ceil(pid_baseline * 1.15)
            and _process_tree_is_idle(baseline_status)
            and _process_tree_is_idle(end_status)
        )
        zombie_processes = int(end_status.get("zombie_processes", 0))
        report: dict[str, object] = {
            "ok": True,
            "mode": profile.mode,
            "profile": asdict(profile),
            "admission": {
                "active": int(admission["active"]),
                "queued": int(admission["queued"]),
            },
            "faults": faults,
            "process_cleanup": {
                "strategy": (
                    "windows-job-object" if os.name == "nt" else "process-group"
                ),
                "events": _cleanup_audit_events(root),
            },
            "recovery": {
                "convergence_seconds": round(convergence_seconds, 3),
                "limit_seconds": 120,
            },
            "integrity": {
                "sqlite": _sqlite_integrity(database),
                "staging_residue": staging_residue,
                "resegment_resets": worker_totals["resegment_resets"],
                **archive,
            },
            "limits": {
                "worker_process_peak": worker_peak,
                "worker_thread_peak": worker_thread_peak,
                "browser_gate_peak": browser_gate_peak,
                "browser_process_peak": browser_process_peak,
                "ffmpeg_process_peak": ffmpeg_process_peak,
                "other_process_peak": other_process_peak,
                "idle_descendant_processes": max(0, pid_end - 1),
                "zombie_processes": zombie_processes,
                "segment_commits": worker_totals["segment_commits"],
                "checkpoint_flushes": worker_totals["checkpoint_flushes"],
                "checkpoint_flush_budget": checkpoint_budget,
                "db_progress_writes": progress_writes,
                "db_progress_write_budget": progress_write_budget,
            },
            "performance": {
                "baseline_bytes_per_second": round(baseline_bps, 3),
                "post_fault_bytes_per_second": round(post_fault_bps, 3),
                "throughput_ratio": round(throughput_ratio, 3),
                "rss_baseline_bytes": rss_baseline,
                "rss_baseline_sample_min_bytes": baseline_rss_range[0],
                "rss_baseline_sample_max_bytes": baseline_rss_range[1],
                "rss_end_bytes": rss_end,
                "rss_end_sample_min_bytes": end_rss_range[0],
                "rss_end_sample_max_bytes": end_rss_range[1],
                "rss_same_process_boot": memory_boot_consistent,
                "rss_budget_ok": rss_budget_ok,
                "pid_baseline": pid_baseline,
                "pid_end": pid_end,
                "pid_budget_ok": pid_budget_ok,
                "process_tree_complete": process_tree_complete,
                "baseline_process_counts": baseline_status.get("process_counts", {}),
                "end_process_counts": end_status.get("process_counts", {}),
            },
        }
        checks = {
            "admission": report["admission"] == {"active": 8, "queued": 1},
            "archive_digest": archive["reference_digest_match"] is True,
            "sqlite_integrity": _sqlite_integrity(database) == "ok",
            "staging_empty": not staging_residue,
            "resegment_reset": worker_totals["resegment_resets"] >= 1,
            "worker_process_limit": worker_peak <= 8,
            "worker_thread_limit": worker_thread_peak <= 8,
            "browser_gate_limit": browser_gate_peak <= 2,
            "browser_process_limit": browser_process_peak <= 2,
            "checkpoint_batching": (
                worker_totals["checkpoint_flushes"] <= checkpoint_budget
            ),
            "progress_write_batching": progress_writes <= progress_write_budget,
            "resource_measurement": (
                process_tree_complete
                and rss_baseline > 0
                and rss_end > 0
                and pid_baseline > 0
                and pid_end > 0
            ),
            "idle_process_tree": _process_tree_is_idle(end_status),
            "rss_same_process_boot": memory_boot_consistent,
            "rss_budget": rss_budget_ok,
            "pid_budget": pid_budget_ok,
            "zombie_processes": zombie_processes == 0,
            "recovery_budget": all(
                float(fault["recovered_seconds"] or 121.0) <= 120.0 for fault in faults
            ),
        }
        failed_checks = sorted(name for name, passed in checks.items() if not passed)
        if failed_checks:
            report["ok"] = False
            _atomic_json(root / "failed-report.json", report)
            raise FaultMatrixAssertionError(
                "fault matrix release assertions failed: " + ", ".join(failed_checks),
                report,
            )
        return report
    except subprocess.TimeoutExpired as exc:
        raise _fault_matrix_error_with_cleanup_audit(
            root,
            "workload process wait timed out",
        ) from exc
    finally:
        active_error = sys.exc_info()[1]
        try:
            if process is not None and process.poll() is None:
                try:
                    state.update(
                        shutdown=True,
                        fast=True,
                        io_fault=False,
                        low_disk=False,
                    )
                    process.wait(timeout=5.0)
                except (FaultMatrixError, OSError, subprocess.TimeoutExpired):
                    _kill_workload_tree(root, process)
            if process is not None:
                _close_windows_job(process)
            for worker in _running_worker_identities(root):
                _terminate_identity(worker)
        finally:
            if isinstance(active_error, FaultMatrixError):
                setattr(active_error, "cleanup_events", _cleanup_audit_events(root))
            if not keep_work:
                cleanup()


def _parse_profile(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FaultMatrixError("worker profile is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FaultMatrixError("worker profile must be an object")
    expected = {
        "mode",
        "segment_count",
        "segment_bytes",
        "segment_delay_seconds",
    }
    if set(payload) != expected:
        raise FaultMatrixError("worker profile fields are invalid")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated JAV Pilot eight-way Web download fault matrix"
    )
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run")
    run.add_argument("--mode", choices=("ci", "release"), required=True)
    run.add_argument("--output", type=Path)
    run.add_argument("--work-root", type=Path)
    run.add_argument("--keep-work", action="store_true")
    workload = subparsers.add_parser("_workload", help=argparse.SUPPRESS)
    workload.add_argument("--root", type=Path, required=True)
    workload.add_argument("--profile-json", required=True)
    workload.add_argument("--start-gate", type=Path)
    fixture_worker = subparsers.add_parser("_fixture-worker", help=argparse.SUPPRESS)
    fixture_worker.add_argument("--root", type=Path, required=True)
    fixture_worker.add_argument("--profile-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "_workload":
            if args.start_gate is not None:
                _wait_for_workload_start(args.start_gate.resolve())
            return _run_workload(args.root.resolve(), _parse_profile(args.profile_json))
        if args.command == "_fixture-worker":
            return _run_fixture_worker(
                args.root.resolve(),
                _parse_profile(args.profile_json),
            )
        if args.command != "run":
            raise FaultMatrixError("the run command is required")
        profile = MatrixProfile.for_mode(args.mode)
        report = run_fault_matrix(
            profile,
            work_root=args.work_root,
            keep_work=args.keep_work,
        )
        raw = json.dumps(report, ensure_ascii=True, sort_keys=True)
        if args.output is not None:
            _atomic_json(args.output.expanduser().resolve(), report)
        print(raw)
        return 0
    except FaultMatrixPlatformSkip as exc:
        output = getattr(args, "output", None)
        skipped_report = {
            "ok": False,
            "skipped": True,
            "mode": getattr(args, "mode", None),
            "platform": sys.platform,
            "error": str(exc),
        }
        if output is not None:
            _atomic_json(output.expanduser().resolve(), skipped_report)
        print(
            json.dumps(skipped_report, ensure_ascii=True, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except FaultMatrixError as exc:
        output = getattr(args, "output", None)
        failure_report = (
            exc.report
            if isinstance(exc, FaultMatrixAssertionError)
            else {
                "ok": False,
                "mode": getattr(args, "mode", None),
                "error": str(exc),
            }
        )
        cleanup_events = getattr(exc, "cleanup_events", None)
        if isinstance(cleanup_events, list) and cleanup_events:
            failure_report["process_cleanup"] = {
                "events": cleanup_events,
            }
        if output is not None:
            _atomic_json(output.expanduser().resolve(), failure_report)
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
