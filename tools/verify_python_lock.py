from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement


_EXACT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)$"
)
_LOCK_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s\\]+)\s+\\$"
)
_HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
_DIRECT_FINGERPRINT = re.compile(
    r"^# direct-dependencies-sha256: (?P<digest>[0-9a-f]{64})$"
)
_PYTHON_SPECIFIER = re.compile(
    r"^(?P<operator>~=|==|!=|<=|>=|<|>)(?P<version>[0-9]+\.[0-9]+)$"
)
_PRODUCTION_PYTHON = (3, 12)


class PythonLockError(RuntimeError):
    pass


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _direct_fingerprint(dependencies: dict[str, str]) -> str:
    payload = "".join(
        f"{name}=={dependencies[name]}\n" for name in sorted(dependencies)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _python_requirement_allows_production(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    for raw_specifier in value.split(","):
        match = _PYTHON_SPECIFIER.fullmatch(raw_specifier.strip())
        if match is None:
            return False
        candidate = tuple(int(part) for part in match.group("version").split("."))
        operator = match.group("operator")
        if operator == ">=" and not _PRODUCTION_PYTHON >= candidate:
            return False
        if operator == ">" and not _PRODUCTION_PYTHON > candidate:
            return False
        if operator == "<=" and not _PRODUCTION_PYTHON <= candidate:
            return False
        if operator == "<" and not _PRODUCTION_PYTHON < candidate:
            return False
        if operator == "==" and not _PRODUCTION_PYTHON == candidate:
            return False
        if operator == "!=" and not _PRODUCTION_PYTHON != candidate:
            return False
        if operator == "~=":
            compatible_prefix = candidate[:1]
            if _PRODUCTION_PYTHON < candidate or (
                _PRODUCTION_PYTHON[: len(compatible_prefix)] != compatible_prefix
            ):
                return False
    return True


def project_requirements(path: Path) -> dict[str, str]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PythonLockError("pyproject is unreadable or invalid") from exc
    raw_project = payload.get("project")
    if not isinstance(raw_project, dict) or not _python_requirement_allows_production(
        raw_project.get("requires-python")
    ):
        raise PythonLockError("pyproject does not support production Python 3.12")
    raw_dependencies = (
        raw_project.get("dependencies") if isinstance(raw_project, dict) else None
    )
    if not isinstance(raw_dependencies, list) or not raw_dependencies:
        raise PythonLockError("pyproject runtime dependencies are missing")
    dependencies: dict[str, str] = {}
    for value in raw_dependencies:
        if not isinstance(value, str):
            raise PythonLockError("pyproject runtime dependency is invalid")
        match = _EXACT_REQUIREMENT.fullmatch(value.strip())
        if match is None:
            raise PythonLockError("pyproject runtime dependencies must be exact pins")
        name = _canonical_name(match.group("name"))
        if name in dependencies:
            raise PythonLockError("pyproject contains a duplicate runtime dependency")
        dependencies[name] = match.group("version")
    return dependencies


def locked_requirements(path: Path) -> tuple[dict[str, str], str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PythonLockError("runtime lock is unreadable") from exc
    dependencies: dict[str, str] = {}
    binary_only = False
    direct_fingerprint: str | None = None
    line_index = 0
    while line_index < len(lines):
        raw_line = lines[line_index]
        line = raw_line.strip()
        line_index += 1
        if not line:
            continue
        fingerprint_match = _DIRECT_FINGERPRINT.fullmatch(line)
        if fingerprint_match is not None:
            if direct_fingerprint is not None:
                raise PythonLockError("runtime lock repeats the direct fingerprint")
            direct_fingerprint = fingerprint_match.group("digest")
            continue
        if line.startswith("#"):
            continue
        if line == "--only-binary=:all:":
            if binary_only:
                raise PythonLockError("runtime lock repeats the binary-only policy")
            binary_only = True
            continue
        entry = _LOCK_ENTRY.fullmatch(line)
        if entry is not None:
            name = _canonical_name(entry.group("name"))
            if name in dependencies:
                raise PythonLockError("runtime lock contains a duplicate package")
            if line_index >= len(lines) or _HASH.fullmatch(
                lines[line_index].strip()
            ) is None:
                raise PythonLockError(
                    "every locked package must have exactly one adjacent SHA-256 hash"
                )
            dependencies[name] = entry.group("version")
            line_index += 1
            continue
        raise PythonLockError("runtime lock contains an unsupported line")
    if not binary_only:
        raise PythonLockError("runtime lock must enforce the binary-only policy")
    if not dependencies:
        raise PythonLockError("runtime lock contains no packages")
    if direct_fingerprint is None:
        raise PythonLockError("runtime lock has no direct dependency fingerprint")
    return dependencies, direct_fingerprint


def verify_python_lock(pyproject: Path, lock: Path) -> tuple[int, int]:
    project = project_requirements(pyproject)
    locked, direct_fingerprint = locked_requirements(lock)
    missing = sorted(set(project) - set(locked))
    mismatched = sorted(
        name for name, version in project.items() if locked.get(name) != version
    )
    if missing:
        raise PythonLockError("runtime lock is missing a direct dependency")
    if mismatched:
        raise PythonLockError("runtime lock disagrees with pyproject")
    if direct_fingerprint != _direct_fingerprint(project):
        raise PythonLockError("runtime lock direct dependency fingerprint is stale")
    return len(project), len(locked)


def verify_installed_closure(pyproject: Path, lock: Path) -> int:
    project = project_requirements(pyproject)
    locked, _ = locked_requirements(lock)
    pending = list(project)
    reachable: set[str] = set()
    while pending:
        requested_name = pending.pop()
        if requested_name in reachable:
            continue
        try:
            distribution = importlib.metadata.distribution(requested_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise PythonLockError("a locked runtime package is not installed") from exc
        installed_name = _canonical_name(distribution.metadata["Name"])
        if installed_name != requested_name:
            raise PythonLockError("an installed runtime package has an unexpected name")
        expected_version = locked.get(installed_name)
        if expected_version is None or distribution.version != expected_version:
            raise PythonLockError("an installed runtime package disagrees with the lock")
        reachable.add(installed_name)
        for raw_requirement in distribution.requires or ():
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement as exc:
                raise PythonLockError(
                    "an installed runtime package has invalid dependency metadata"
                ) from exc
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
            dependency_name = _canonical_name(requirement.name)
            if dependency_name not in reachable:
                pending.append(dependency_name)
    locked_names = set(locked)
    if reachable != locked_names:
        raise PythonLockError(
            "runtime lock is not the exact installed dependency closure"
        )
    return len(reachable)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the production Python lock")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--lock", type=Path, default=Path("requirements.lock"))
    parser.add_argument("--print-direct-fingerprint", action="store_true")
    parser.add_argument("--verify-installed-closure", action="store_true")
    args = parser.parse_args()
    if args.print_direct_fingerprint:
        print(_direct_fingerprint(project_requirements(args.pyproject)))
        return 0
    direct_count, locked_count = verify_python_lock(args.pyproject, args.lock)
    if args.verify_installed_closure:
        installed_count = verify_installed_closure(args.pyproject, args.lock)
        if installed_count != locked_count:
            raise PythonLockError("installed runtime closure count is inconsistent")
    print(f"python lock ok: {direct_count} direct, {locked_count} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
