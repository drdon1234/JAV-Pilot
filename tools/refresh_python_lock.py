from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Sequence


PRODUCTION_IMAGE = (
    "mcr.microsoft.com/playwright/python:v1.61.0-noble@"
    "sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69"
)
PRODUCTION_PLATFORM = "linux/amd64"
OSV_QUERY_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_WORKER_ENV = "JAV_PILOT_PYTHON_LOCK_WORKER"
_CANONICAL_SEPARATOR = re.compile(r"[-_.]+")
_LOCK_SAFE_VERSION = re.compile(r"^[^\s;\\]+$")
_OSV_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_OSV_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_OSV_PAGES = 100
_OSV_TIMEOUT_SECONDS = 20
_OSV_RETRY_SECONDS = (0.0, 1.0, 2.0)


class RefreshLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class WheelRecord:
    name: str
    version: str
    sha256: str


OsvRequest = Callable[[Mapping[str, object]], Mapping[str, object]]


def _canonical_name(value: str) -> str:
    return _CANONICAL_SEPARATOR.sub("-", value).lower()


def _load_verifier(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("python_lock_verifier", path)
    if spec is None or spec.loader is None:
        raise RefreshLockError("could not load the Python lock verifier")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, SyntaxError) as exc:
        raise RefreshLockError("could not load the Python lock verifier") from exc
    return module


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_platform(path: Path) -> str:
    stem = path.name[:-4] if path.name.endswith(".whl") else ""
    if "-" not in stem:
        raise RefreshLockError("downloaded wheel has an invalid filename")
    return stem.rsplit("-", 1)[1]


def _wheel_record(path: Path) -> WheelRecord:
    if not path.is_file() or path.suffix != ".whl":
        raise RefreshLockError("resolver returned a source or unsupported artifact")
    wheel_platform = _wheel_platform(path)
    if wheel_platform != "any" and not any(
        marker in wheel_platform for marker in ("x86_64", "amd64")
    ):
        raise RefreshLockError("resolver returned a non-amd64 wheel")
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_members = [
                member
                for member in archive.infolist()
                if member.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata_members) != 1:
                raise RefreshLockError(
                    "downloaded wheel must contain exactly one METADATA file"
                )
            member = metadata_members[0]
            if member.file_size > _MAX_METADATA_BYTES:
                raise RefreshLockError("downloaded wheel METADATA is unexpectedly large")
            metadata = BytesParser(policy=compat32).parsebytes(archive.read(member))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, RefreshLockError):
            raise
        raise RefreshLockError("downloaded wheel is unreadable") from exc
    raw_name = metadata.get("Name")
    raw_version = metadata.get("Version")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise RefreshLockError("downloaded wheel has no package name")
    if not isinstance(raw_version, str) or _LOCK_SAFE_VERSION.fullmatch(
        raw_version.strip()
    ) is None:
        raise RefreshLockError("downloaded wheel has an unsafe package version")
    return WheelRecord(
        name=_canonical_name(raw_name.strip()),
        version=raw_version.strip(),
        sha256=_hash_file(path),
    )


def _collect_wheels(directory: Path) -> dict[str, WheelRecord]:
    artifacts = sorted(directory.iterdir())
    if not artifacts:
        raise RefreshLockError("resolver returned no wheels")
    records: dict[str, WheelRecord] = {}
    for artifact in artifacts:
        record = _wheel_record(artifact)
        if record.name in records:
            raise RefreshLockError("resolver returned a duplicate package")
        records[record.name] = record
    return records


def _render_lock(
    direct_dependencies: Mapping[str, str],
    records: Mapping[str, WheelRecord],
    direct_fingerprint: str,
) -> str:
    missing = sorted(set(direct_dependencies) - set(records))
    mismatched = sorted(
        name
        for name, version in direct_dependencies.items()
        if records.get(name) is not None and records[name].version != version
    )
    if missing:
        raise RefreshLockError("resolved wheel closure is missing a direct dependency")
    if mismatched:
        raise RefreshLockError("resolved wheel disagrees with a direct dependency pin")
    lines = [
        "# Runtime lock generated by tools/refresh_python_lock.py.",
        "# Fixed target: Playwright Python 3.12 / linux-amd64.",
        "# Every entry is the wheel selected inside the pinned production image.",
        f"# direct-dependencies-sha256: {direct_fingerprint}",
        "--only-binary=:all:",
        "",
    ]
    for name in sorted(records):
        record = records[name]
        lines.extend(
            (
                f"{record.name}=={record.version} \\",
                f"    --hash=sha256:{record.sha256}",
            )
        )
    return "\n".join(lines) + "\n"


def _installed_records(site_packages: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(path=[str(site_packages)]):
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise RefreshLockError("installed wheel has no package name")
        name = _canonical_name(raw_name.strip())
        if name in records:
            raise RefreshLockError("wheel installation produced a duplicate package")
        records[name] = distribution.version
    return records


def _validate_installed_records(
    expected: Mapping[str, WheelRecord], installed: Mapping[str, str]
) -> None:
    expected_versions = {name: record.version for name, record in expected.items()}
    if dict(installed) != expected_versions:
        raise RefreshLockError(
            "installed wheel set is not the exact resolved dependency closure"
        )


def _osv_request(payload: Mapping[str, object]) -> Mapping[str, object]:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    request = urllib.request.Request(
        OSV_QUERY_BATCH_URL,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "jav-pilot-python-lock-refresh/1",
        },
        method="POST",
    )
    last_error: BaseException | None = None
    for delay in _OSV_RETRY_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(
                request, timeout=_OSV_TIMEOUT_SECONDS
            ) as response:
                if response.geturl() != OSV_QUERY_BATCH_URL:
                    raise RefreshLockError(
                        "OSV vulnerability service redirected unexpectedly"
                    )
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise RefreshLockError(
                        "OSV vulnerability service returned a non-JSON response"
                    )
                raw_response = response.read(_MAX_OSV_RESPONSE_BYTES + 1)
                if len(raw_response) > _MAX_OSV_RESPONSE_BYTES:
                    raise RefreshLockError(
                        "OSV vulnerability service response is too large"
                    )
            decoded = json.loads(raw_response)
            if not isinstance(decoded, dict):
                raise RefreshLockError(
                    "OSV vulnerability service response is malformed"
                )
            return decoded
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                break
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RefreshLockError(
                "OSV vulnerability service returned invalid JSON"
            ) from exc
    raise RefreshLockError("OSV vulnerability service is unavailable") from last_error


def _check_osv_vulnerabilities(
    records: Mapping[str, WheelRecord], *, request: OsvRequest = _osv_request
) -> int:
    if not records:
        raise RefreshLockError("cannot audit an empty dependency closure")
    pending: list[tuple[str, str, str | None]] = [
        (name, records[name].version, None) for name in sorted(records)
    ]
    seen_tokens: dict[tuple[str, str], set[str]] = {}
    page_count = 0
    while pending:
        page_count += 1
        if page_count > _MAX_OSV_PAGES:
            raise RefreshLockError("OSV vulnerability pagination did not terminate")
        queries: list[dict[str, object]] = []
        for name, version, page_token in pending:
            query: dict[str, object] = {
                "package": {"ecosystem": "PyPI", "name": name},
                "version": version,
            }
            if page_token is not None:
                query["page_token"] = page_token
            queries.append(query)
        response = request({"queries": queries})
        raw_results = response.get("results")
        if not isinstance(raw_results, list) or len(raw_results) != len(pending):
            raise RefreshLockError("OSV vulnerability response has invalid ordering")

        vulnerability_ids: set[str] = set()
        next_pending: list[tuple[str, str, str | None]] = []
        for (name, version, _), raw_result in zip(pending, raw_results, strict=True):
            if not isinstance(raw_result, dict):
                raise RefreshLockError("OSV vulnerability result is malformed")
            raw_vulnerabilities = raw_result.get("vulns", [])
            if not isinstance(raw_vulnerabilities, list):
                raise RefreshLockError("OSV vulnerability list is malformed")
            for vulnerability in raw_vulnerabilities:
                if not isinstance(vulnerability, dict):
                    raise RefreshLockError("OSV vulnerability record is malformed")
                vulnerability_id = vulnerability.get("id")
                modified = vulnerability.get("modified")
                if (
                    not isinstance(vulnerability_id, str)
                    or _OSV_ID.fullmatch(vulnerability_id) is None
                    or not isinstance(modified, str)
                    or not modified.strip()
                ):
                    raise RefreshLockError("OSV vulnerability record is malformed")
                vulnerability_ids.add(vulnerability_id)

            page_token = raw_result.get("next_page_token")
            if page_token is not None:
                if (
                    not isinstance(page_token, str)
                    or not page_token
                    or len(page_token) > 4096
                ):
                    raise RefreshLockError(
                        "OSV vulnerability pagination token is malformed"
                    )
                package_key = (name, version)
                package_tokens = seen_tokens.setdefault(package_key, set())
                if page_token in package_tokens:
                    raise RefreshLockError(
                        "OSV vulnerability pagination repeated a token"
                    )
                package_tokens.add(page_token)
                next_pending.append((name, version, page_token))
        if vulnerability_ids:
            identifiers = ", ".join(sorted(vulnerability_ids)[:20])
            suffix = " and more" if len(vulnerability_ids) > 20 else ""
            raise RefreshLockError(
                f"resolved dependency closure has known vulnerabilities: "
                f"{identifiers}{suffix}"
            )
        pending = next_pending
    return len(records)


def _run_checked(
    command: Sequence[str], *, env: Mapping[str, str] | None = None
) -> None:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            env=None if env is None else dict(env),
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    except OSError as exc:
        raise RefreshLockError("could not execute a lock refresh subprocess") from exc
    if result.returncode != 0:
        raise RefreshLockError("lock refresh subprocess failed")


def _container_worker(pyproject: Path, verifier_path: Path, work: Path) -> bytes:
    if os.environ.get(_WORKER_ENV) != "1":
        raise RefreshLockError("container worker cannot run outside the refresh sandbox")
    if sys.platform != "linux" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        raise RefreshLockError("lock refresh requires a linux/amd64 container")
    if sys.version_info[:2] != (3, 12):
        raise RefreshLockError("lock refresh requires production Python 3.12")

    work.mkdir(parents=True, exist_ok=True)
    wheels = work / "wheels"
    site_packages = work / "site-packages"
    home = work / "home"
    temp = work / "tmp"
    for directory in (wheels, site_packages, home, temp):
        directory.mkdir()

    verifier = _load_verifier(verifier_path)
    try:
        direct_dependencies = verifier.project_requirements(pyproject)
        direct_fingerprint = verifier._direct_fingerprint(direct_dependencies)
    except Exception as exc:
        raise RefreshLockError("pyproject runtime dependencies are invalid") from exc

    direct_requirements = work / "direct-requirements.txt"
    direct_requirements.write_text(
        "".join(
            f"{name}=={direct_dependencies[name]}\n"
            for name in sorted(direct_dependencies)
        ),
        encoding="utf-8",
        newline="\n",
    )
    pip_environment = dict(os.environ)
    pip_environment.update(
        {
            "HOME": str(home),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_ROOT_USER_ACTION": "ignore",
            "TMPDIR": str(temp),
        }
    )
    _run_checked(
        (
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--progress-bar",
            "off",
            "--dest",
            str(wheels),
            "--requirement",
            str(direct_requirements),
        ),
        env=pip_environment,
    )
    records = _collect_wheels(wheels)
    candidate = work / "requirements.lock"
    candidate.write_text(
        _render_lock(direct_dependencies, records, direct_fingerprint),
        encoding="utf-8",
        newline="\n",
    )
    try:
        verifier.verify_python_lock(pyproject, candidate)
    except Exception as exc:
        raise RefreshLockError("generated lock failed structural verification") from exc

    _run_checked(
        (
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--find-links",
            str(wheels),
            "--require-hashes",
            "--only-binary=:all:",
            "--ignore-installed",
            "--no-compile",
            "--progress-bar",
            "off",
            "--target",
            str(site_packages),
            "--requirement",
            str(candidate),
        ),
        env=pip_environment,
    )
    _validate_installed_records(records, _installed_records(site_packages))
    closure_environment = dict(pip_environment)
    closure_environment["PYTHONPATH"] = str(site_packages)
    closure_environment["PYTHONNOUSERSITE"] = "1"
    _run_checked(
        (
            sys.executable,
            str(verifier_path),
            "--pyproject",
            str(pyproject),
            "--lock",
            str(candidate),
            "--verify-installed-closure",
        ),
        env=closure_environment,
    )
    audited_count = _check_osv_vulnerabilities(records)
    print(
        f"OSV vulnerability gate passed for {audited_count} packages",
        file=sys.stderr,
    )
    return candidate.read_bytes()


def _docker_mount(source: Path, target: str) -> str:
    source_text = str(source.resolve())
    if "," in source_text:
        raise RefreshLockError("Docker bind source cannot contain a comma")
    return f"type=bind,src={source_text},dst={target},readonly"


def build_docker_command(docker: str, input_directory: Path) -> list[str]:
    return [
        docker,
        "run",
        "--rm",
        "--pull=missing",
        "--platform",
        PRODUCTION_PLATFORM,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs",
        "/work:rw,nosuid,nodev,size=2g",
        "--env",
        f"{_WORKER_ENV}=1",
        "--env",
        "HOME=/work/home",
        "--env",
        "TMPDIR=/work/tmp",
        "--mount",
        _docker_mount(input_directory, "/input"),
        "--entrypoint",
        "python",
        PRODUCTION_IMAGE,
        "/input/refresh_python_lock.py",
        "--container-worker",
        "--pyproject",
        "/input/pyproject.toml",
        "--verifier",
        "/input/verify_python_lock.py",
        "--work",
        "/work",
    ]


def _run_container(command: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(list(command), check=False, stdout=subprocess.PIPE)
    except OSError as exc:
        raise RefreshLockError("could not start Docker") from exc
    if result.returncode != 0:
        raise RefreshLockError("container dependency resolution failed")
    if not result.stdout:
        raise RefreshLockError("container returned an empty lock")
    return result.stdout


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The replacement has already completed atomically. Some network and
        # synthetic filesystems do not support directory fsync, so this flush
        # is deliberately best-effort and must not turn success into a failure.
        return


def _atomic_publish(candidate: Path, output: Path) -> bool:
    content = candidate.read_bytes()
    if output.exists() and output.read_bytes() == content:
        return False
    try:
        with candidate.open("rb+") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RefreshLockError("could not flush the candidate lock") from exc
    try:
        os.replace(candidate, output)
    except OSError as exc:
        raise RefreshLockError(
            "atomic lock replacement failed; configure the system temp directory "
            "on the output filesystem"
        ) from exc
    _fsync_directory(output.parent)
    return True


def refresh_lock(
    *, pyproject: Path, output: Path, verifier_path: Path, docker: str
) -> bool:
    pyproject = pyproject.resolve()
    output = output.resolve()
    verifier_path = verifier_path.resolve()
    script_path = Path(__file__).resolve()
    for path, label in (
        (pyproject, "pyproject"),
        (verifier_path, "Python lock verifier"),
        (script_path, "Python lock refresher"),
    ):
        if not path.is_file():
            raise RefreshLockError(f"{label} does not exist")
    if not output.parent.is_dir():
        raise RefreshLockError("lock output directory does not exist")

    docker_executable = shutil.which(docker)
    if docker_executable is None:
        raise RefreshLockError("Docker CLI is not installed or not on PATH")
    with tempfile.TemporaryDirectory(prefix="jav-pilot-python-lock-") as temp_name:
        temp_root = Path(temp_name)
        if os.stat(temp_root).st_dev != os.stat(output.parent).st_dev:
            raise RefreshLockError(
                "system temp and lock output must be on the same filesystem for "
                "atomic replacement"
            )
        input_directory = temp_root / "input"
        input_directory.mkdir()
        shutil.copyfile(pyproject, input_directory / "pyproject.toml")
        shutil.copyfile(verifier_path, input_directory / "verify_python_lock.py")
        shutil.copyfile(script_path, input_directory / "refresh_python_lock.py")
        command = build_docker_command(docker_executable, input_directory)
        candidate = temp_root / "requirements.lock"
        candidate.write_bytes(_run_container(command))

        verifier = _load_verifier(input_directory / "verify_python_lock.py")
        try:
            verifier.verify_python_lock(
                input_directory / "pyproject.toml",
                candidate,
            )
        except Exception as exc:
            raise RefreshLockError(
                "container returned a lock that failed host verification"
            ) from exc
        return _atomic_publish(candidate, output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh the reproducible production Python wheel lock"
    )
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--output", type=Path, default=Path("requirements.lock"))
    parser.add_argument(
        "--verifier",
        type=Path,
        default=Path("tools/verify_python_lock.py"),
    )
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--container-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work", type=Path, default=Path("/work"), help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.container_worker:
            sys.stdout.buffer.write(
                _container_worker(args.pyproject, args.verifier, args.work)
            )
            return 0
        changed = refresh_lock(
            pyproject=args.pyproject,
            output=args.output,
            verifier_path=args.verifier,
            docker=args.docker,
        )
    except RefreshLockError as exc:
        print(f"python lock refresh failed: {exc}", file=sys.stderr)
        return 1
    print("python runtime lock refreshed" if changed else "python runtime lock unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
