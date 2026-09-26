from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 4173
WINDOWS_E2E_OWNER_PID = "JAV_PILOT_E2E_OWNER_PID"
WINDOWS_E2E_OWNER_TOKEN = "JAV_PILOT_E2E_OWNER_TOKEN"
WINDOWS_E2E_JOB_NAME = "JAV_PILOT_E2E_JOB_NAME"
WINDOWS_E2E_START_TIMEOUT_SECONDS = 15.0
WINDOWS_CREATE_NO_WINDOW = int(
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
)


def _is_windows() -> bool:
    return os.name == "nt"


class _WindowsRunLock:
    def __init__(self, handle: BinaryIO | None = None) -> None:
        self._handle = handle

    def __enter__(self) -> "_WindowsRunLock":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def _acquire_windows_run_lock() -> _WindowsRunLock:
    if not _is_windows():
        return _WindowsRunLock()
    import msvcrt

    path = Path(tempfile.gettempdir()) / "jav-pilot-e2e.lock"
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            "another JAV Pilot Playwright run is already active on Windows"
        ) from exc
    return _WindowsRunLock(handle)


@lru_cache(maxsize=1)
def _windows_process_query_api() -> tuple[object, ...]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    get_process_times = kernel32.GetProcessTimes
    close_handle = kernel32.CloseHandle
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return wintypes.FILETIME, ctypes.byref, open_process, get_process_times, close_handle


def _windows_process_token(pid: int) -> str | None:
    if not _is_windows() or pid <= 0:
        return None
    try:
        (
            filetime_type,
            by_reference,
            open_process,
            get_process_times,
            close_handle,
        ) = _windows_process_query_api()
        handle = open_process(0x1000, False, pid)  # type: ignore[operator]
        if not handle:
            return None
        try:
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
            return str(
                (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            )
        finally:
            close_handle(handle)  # type: ignore[operator]
    except (AttributeError, OSError, TypeError, ValueError):
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
    open_job = kernel32.OpenJobObjectW
    set_job_information = kernel32.SetInformationJobObject
    assign_process = kernel32.AssignProcessToJobObject
    is_process_in_job = kernel32.IsProcessInJob
    get_current_process = kernel32.GetCurrentProcess
    close_handle = kernel32.CloseHandle
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    open_job.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_job.restype = wintypes.HANDLE
    set_job_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_job_information.restype = wintypes.BOOL
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    is_process_in_job.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    is_process_in_job.restype = wintypes.BOOL
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        ExtendedLimitInformation,
        ctypes.sizeof,
        ctypes.byref,
        wintypes.BOOL,
        create_job,
        open_job,
        set_job_information,
        assign_process,
        is_process_in_job,
        get_current_process,
        close_handle,
    )


class _WindowsKillJob:
    def __init__(self, handle: object, name: str) -> None:
        self._handle = handle
        self.name = name

    def assign_process(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = getattr(process, "_handle", None)
        if not process_handle:
            raise RuntimeError("Windows E2E child process handle is unavailable")
        self._assign_handle(process_handle)

    def assign_current_process(self) -> None:
        get_current_process = _windows_job_api()[-2]
        self._assign_handle(get_current_process())  # type: ignore[operator]

    def _assign_handle(self, process_handle: object) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeError("Windows E2E process job is closed")
        assign_process = _windows_job_api()[-4]
        if not assign_process(handle, process_handle):  # type: ignore[operator]
            raise RuntimeError("could not assign the Windows E2E process job")

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        close_handle = _windows_job_api()[-1]
        close_handle(handle)  # type: ignore[operator]


def _create_windows_kill_job() -> _WindowsKillJob:
    (
        information_type,
        size_of,
        by_reference,
        _bool_type,
        create_job,
        _open_job,
        set_job_information,
        _assign_process,
        _is_process_in_job,
        _get_current_process,
        close_handle,
    ) = _windows_job_api()
    name = f"Local\\JavPilotE2E-{uuid.uuid4().hex}"
    handle = create_job(None, name)  # type: ignore[operator]
    if not handle:
        raise RuntimeError("could not create the Windows E2E process job")
    try:
        information = information_type()  # type: ignore[operator]
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not set_job_information(  # type: ignore[operator]
            handle,
            9,
            by_reference(information),  # type: ignore[operator]
            size_of(information),  # type: ignore[operator]
        ):
            raise RuntimeError("could not configure the Windows E2E process job")
    except Exception:
        close_handle(handle)  # type: ignore[operator]
        raise
    return _WindowsKillJob(handle, name)


def _current_process_is_in_windows_job(name: str) -> bool:
    if not _is_windows() or not name:
        return False
    (
        _information_type,
        _size_of,
        by_reference,
        bool_type,
        _create_job,
        open_job,
        _set_job_information,
        _assign_process,
        is_process_in_job,
        get_current_process,
        close_handle,
    ) = _windows_job_api()
    handle = open_job(0x0004, False, name)  # type: ignore[operator]
    if not handle:
        return False
    try:
        result = bool_type()  # type: ignore[operator]
        return bool(
            is_process_in_job(  # type: ignore[operator]
                get_current_process(),  # type: ignore[operator]
                handle,
                by_reference(result),  # type: ignore[operator]
            )
            and result.value
        )
    finally:
        close_handle(handle)  # type: ignore[operator]


def _inherited_windows_e2e_guard_is_valid() -> bool:
    if not _is_windows():
        return False
    owner = _windows_e2e_owner_identity()
    return owner is not None and _current_process_is_in_windows_job(
        os.environ.get(WINDOWS_E2E_JOB_NAME, "")
    )


def _windows_e2e_owner_identity() -> tuple[int, str] | None:
    if not _is_windows():
        return None
    try:
        owner_pid = int(os.environ.get(WINDOWS_E2E_OWNER_PID, ""))
    except ValueError:
        return None
    owner_token = os.environ.get(WINDOWS_E2E_OWNER_TOKEN, "")
    if (
        owner_pid <= 0
        or owner_pid != os.getppid()
        or not owner_token
        or _windows_process_token(owner_pid) != owner_token
    ):
        return None
    return owner_pid, owner_token


_DIRECT_WINDOWS_E2E_JOB: _WindowsKillJob | None = None
_DIRECT_WINDOWS_E2E_WATCHDOG: threading.Thread | None = None


def _watch_windows_e2e_parent(
    parent_pid: int,
    parent_token: str,
    job: _WindowsKillJob,
) -> None:
    while _windows_process_token(parent_pid) == parent_token:
        time.sleep(0.25)
    job.close()


def _ensure_current_windows_e2e_job() -> None:
    global _DIRECT_WINDOWS_E2E_JOB, _DIRECT_WINDOWS_E2E_WATCHDOG
    if not _is_windows() or _DIRECT_WINDOWS_E2E_JOB is not None:
        return
    parent_pid = os.getppid()
    parent_token = _windows_process_token(parent_pid)
    if not parent_token:
        raise RuntimeError("could not verify the Windows E2E parent identity")
    job = _create_windows_kill_job()
    try:
        job.assign_current_process()
    except Exception:
        job.close()
        raise
    _DIRECT_WINDOWS_E2E_JOB = job
    watchdog = threading.Thread(
        target=_watch_windows_e2e_parent,
        args=(parent_pid, parent_token, job),
        name="jav-pilot-e2e-parent-watchdog",
        daemon=True,
    )
    _DIRECT_WINDOWS_E2E_WATCHDOG = watchdog
    watchdog.start()


class _WindowsE2EProcessGuard:
    def __init__(self, lease: _WindowsRunLock | None = None) -> None:
        self._lease = lease

    def close(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is not None:
            lease.close()


def _acquire_windows_e2e_process_guard() -> _WindowsE2EProcessGuard:
    if not _is_windows() or _inherited_windows_e2e_guard_is_valid():
        return _WindowsE2EProcessGuard()
    lease = _acquire_windows_run_lock()
    try:
        _ensure_current_windows_e2e_job()
    except Exception:
        lease.close()
        raise
    return _WindowsE2EProcessGuard(lease)


def _require_fixture_port_available() -> None:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.25):
            pass
    except OSError:
        return
    raise RuntimeError(f"E2E fixture port {PORT} is already in use")


def _wait_for_server(process: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"E2E fixture server exited with status {process.returncode}"
            )
        try:
            with socket.create_connection((HOST, PORT), timeout=0.25):
                time.sleep(0.05)
                if process.poll() is None:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("E2E fixture server did not become ready")


def _validated_start_gate(path: Path) -> Path:
    gate = path.expanduser().resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        gate.parent != temporary_root
        or not gate.name.startswith("jav-pilot-e2e-start-")
        or gate.suffix != ".gate"
    ):
        raise RuntimeError("Windows E2E child start gate is invalid")
    return gate


def _wait_for_start_gate(path: Path) -> None:
    gate = _validated_start_gate(path)
    deadline = time.monotonic() + WINDOWS_E2E_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            gate.unlink()
            return
        except FileNotFoundError:
            if (
                _is_windows()
                and os.environ.get(WINDOWS_E2E_OWNER_PID)
                and _windows_e2e_owner_identity() is None
            ):
                raise RuntimeError("Windows E2E runner parent exited before admission")
            time.sleep(0.01)
    raise RuntimeError("Windows E2E child was not admitted to its process job")


def _run_pytest_child(start_gate: Path) -> int:
    _wait_for_start_gate(start_gate)
    import pytest

    return int(pytest.main(["-q", "-m", "e2e", "test/python/tests/e2e"]))


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments[:1] == ["_pytest-child"]:
        child_parser = argparse.ArgumentParser(add_help=False)
        child_parser.add_argument("_command")
        child_parser.add_argument("--start-gate", type=Path, required=True)
        child_args = child_parser.parse_args(raw_arguments)
        return _run_pytest_child(child_args.start_gate)

    parser = argparse.ArgumentParser(
        description="Run isolated JAV Pilot Playwright acceptance tests."
    )
    parser.add_argument(
        "--browsers",
        default=os.environ.get("JAV_PILOT_E2E_BROWSERS", "chromium"),
        help="Comma-separated Playwright browser engines.",
    )
    args = parser.parse_args(raw_arguments)
    browsers = tuple(
        dict.fromkeys(part.strip().lower() for part in args.browsers.split(","))
    )
    if not browsers or any(
        browser not in {"chromium", "firefox", "webkit"} for browser in browsers
    ):
        parser.error("browsers must contain chromium, firefox, or webkit")
    with _acquire_windows_run_lock():
        if not _is_windows():
            return _run_serial(browsers)
        owner_token = _windows_process_token(os.getpid())
        if not owner_token:
            raise RuntimeError("could not verify the Windows E2E runner identity")
        job = _create_windows_kill_job()
        try:
            return _run_serial(
                browsers,
                windows_job=job,
                owner_token=owner_token,
            )
        finally:
            job.close()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_windows_pytest(
    environment: dict[str, str],
    windows_job: _WindowsKillJob,
) -> int:
    start_gate = Path(tempfile.gettempdir()) / (
        f"jav-pilot-e2e-start-{uuid.uuid4().hex}.gate"
    )
    runner = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tools.e2e_run",
            "_pytest-child",
            "--start-gate",
            str(start_gate),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        creationflags=WINDOWS_CREATE_NO_WINDOW,
    )
    try:
        windows_job.assign_process(runner)
        start_gate.touch(exist_ok=False)
        return int(runner.wait())
    finally:
        try:
            start_gate.unlink()
        except FileNotFoundError:
            pass
        _stop_process(runner)


def _run_serial(
    browsers: tuple[str, ...],
    *,
    windows_job: _WindowsKillJob | None = None,
    owner_token: str | None = None,
) -> int:
    fixture = PROJECT_ROOT / "test" / "support" / "e2e_fixture_server.py"
    if not fixture.is_file() or fixture.is_symlink():
        raise RuntimeError(
            "local E2E resources are unavailable: test/support/e2e_fixture_server.py"
        )
    environment = os.environ.copy()
    environment["JAV_PILOT_E2E_BASE_URL"] = f"http://{HOST}:{PORT}"
    environment["JAV_PILOT_E2E_BROWSERS"] = ",".join(browsers)
    environment["JAV_PILOT_E2E_SERIAL"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    if _is_windows():
        if windows_job is None or not owner_token:
            raise RuntimeError("Windows E2E process containment is unavailable")
        environment[WINDOWS_E2E_OWNER_PID] = str(os.getpid())
        environment[WINDOWS_E2E_OWNER_TOKEN] = owner_token
        environment[WINDOWS_E2E_JOB_NAME] = windows_job.name
    _require_fixture_port_available()
    popen_options: dict[str, object] = {}
    if _is_windows():
        popen_options["creationflags"] = WINDOWS_CREATE_NO_WINDOW
    server = subprocess.Popen(
        [
            sys.executable,
            str(fixture),
            "--port",
            str(PORT),
            "--static-root",
            str(PROJECT_ROOT / "frontend" / "dist"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        **popen_options,
    )
    try:
        if windows_job is not None:
            windows_job.assign_process(server)
        _wait_for_server(server)
        if _is_windows():
            return _run_windows_pytest(environment, windows_job)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                "e2e",
                "test/python/tests/e2e",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            **popen_options,
        )
        return int(completed.returncode)
    finally:
        _stop_process(server)


if __name__ == "__main__":
    raise SystemExit(main())
