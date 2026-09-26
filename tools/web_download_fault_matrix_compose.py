from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPOSITORY_ROOT / "tests" / "load" / "compose.yaml"


class ComposeLoadError(RuntimeError):
    pass


def compose_commands(mode: str, project: str) -> tuple[list[str], list[str]]:
    if mode not in {"ci", "release"}:
        raise ComposeLoadError("load mode must be ci or release")
    if not project.startswith("jav-pilot-qa04-"):
        raise ComposeLoadError("isolated Compose project name is invalid")
    base = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project]
    up = [
        *base,
        "up",
        "--build",
        "--abort-on-container-exit",
        "--exit-code-from",
        "fault-matrix",
    ]
    down = [*base, "down", "--volumes", "--remove-orphans"]
    return up, down


def run_compose(mode: str, artifacts: Path) -> dict[str, object]:
    if shutil.which("docker") is None:
        raise ComposeLoadError("Docker CLI is unavailable")
    artifacts = artifacts.expanduser().resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    project = f"jav-pilot-qa04-{uuid.uuid4().hex[:12]}"
    up, down = compose_commands(mode, project)
    environment = dict(os.environ)
    environment["JAV_PILOT_LOAD_MODE"] = mode
    environment["JAV_PILOT_LOAD_ARTIFACTS"] = str(artifacts)
    try:
        config = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                project,
                "config",
                "--quiet",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60.0,
        )
        if config.returncode != 0:
            raise ComposeLoadError("isolated load Compose configuration is invalid")
        result = subprocess.run(
            up,
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=45.0 * 60.0 if mode == "release" else 15.0 * 60.0,
        )
        if result.returncode != 0:
            raise ComposeLoadError("isolated load container failed")
        report_path = artifacts / "report.json"
        try:
            payload = json.loads(report_path.read_text(encoding="ascii"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComposeLoadError("isolated load report is unavailable") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ComposeLoadError("isolated load report did not pass")
        return payload
    finally:
        subprocess.run(
            down,
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120.0,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated QA-04 matrix through temporary Compose volumes"
    )
    parser.add_argument("--mode", choices=("ci", "release"), required=True)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=REPOSITORY_ROOT / "test-artifacts" / "load",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_compose(args.mode, args.artifacts)
    except ComposeLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
