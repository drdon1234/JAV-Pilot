"""Build a reproducible Linux image from the committed source tree (run in WSL)."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, encoding="utf-8", stdout=subprocess.PIPE if capture else None)
    return (result.stdout or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push", action="store_true", help="publish the version tag and latest after verification")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run("git", "-C", str(root), "diff", "--exit-code", "HEAD", "--")
    revision = run("git", "-C", str(root), "rev-parse", "HEAD", capture=True)
    source = run("git", "-C", str(root), "show", f"{revision}:jav_pilot/__init__.py", capture=True)
    match = re.search(r'__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"', source)
    if not match:
        raise RuntimeError("Committed application version is missing")
    version = match[1]
    tag = f"drdon1234/jav-pilot:{version}"
    with tempfile.TemporaryDirectory(prefix="jav-pilot-release-") as directory:
        archive = Path(directory) / "source.tar"
        context = Path(directory) / "source"
        context.mkdir()
        run("git", "-C", str(root), "archive", "--format=tar", f"--output={archive}", revision)
        with tarfile.open(archive) as stream:
            stream.extractall(context, filter="data")
        run("docker", "build", "--platform", "linux/amd64", "--build-arg", f"VCS_REF={revision}", "-t", tag, str(context))
    image = json.loads(run("docker", "image", "inspect", tag, capture=True))[0]
    labels = image["Config"]["Labels"]
    if labels.get("org.opencontainers.image.revision") != revision or labels.get("org.opencontainers.image.version") != version:
        raise RuntimeError("Built image identity does not match committed source")
    if f"JAV_PILOT_REVISION={revision}" not in image["Config"]["Env"]:
        raise RuntimeError("Runtime cache revision does not match committed source")
    installed = run("docker", "run", "--rm", "--entrypoint", "python", tag, "-c", "from jav_pilot import __version__; print(__version__)", capture=True)
    if installed != version:
        raise RuntimeError("Runtime application version does not match image")
    run("docker", "run", "--rm", "--entrypoint", "python", tag, "-m", "pip", "check")
    if args.push:
        run("docker", "push", tag)
        run("docker", "tag", tag, "drdon1234/jav-pilot:latest")
        run("docker", "push", "drdon1234/jav-pilot:latest")
    print(json.dumps({"image": tag, "revision": revision, "image_id": image["Id"], "published": args.push}), flush=True)


if __name__ == "__main__":
    main()
