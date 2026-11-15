"""Validate a release tag against the checked-out, consistently versioned source."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def release_metadata(root: Path, tag: str) -> dict[str, str]:
    if not re.fullmatch(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', tag):
        raise ValueError('Release tag must be vMAJOR.MINOR.PATCH')
    version = tag[1:]
    source = (root / 'jav_pilot/__init__.py').read_text(encoding='utf-8')
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    package = json.loads((root / 'frontend/package.json').read_text(encoding='utf-8'))
    lock = json.loads((root / 'frontend/package-lock.json').read_text(encoding='utf-8'))
    docker = (root / 'Dockerfile').read_text(encoding='utf-8')
    label = re.search(r'org\.opencontainers\.image\.version="([^"]+)"', docker)
    versions = [match.group(1) if match else None, package.get('version'),
                lock.get('version'), lock.get('packages', {}).get('', {}).get('version'),
                label.group(1) if label else None]
    if any(value != version for value in versions):
        raise ValueError(f'Tag {tag} does not match application, frontend, lockfile and Dockerfile versions: {versions}')
    revision = git(root, 'rev-parse', 'HEAD')
    if git(root, 'rev-parse', f'refs/tags/{tag}^{{commit}}') != revision:
        raise ValueError('Release tag does not point to the checked-out commit')
    subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', revision, 'origin/main'], check=True)
    versions = [tuple(map(int, item[1:].split('.'))) for item in git(root, 'tag', '--list').splitlines()
                if re.fullmatch(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', item)]
    latest = tuple(map(int, version.split('.'))) == max(versions)
    return {'tag': tag, 'version': version, 'revision': revision,
            'image': f'drdon1234/jav-pilot:{version}', 'latest': str(latest).lower()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    metadata = release_metadata(Path(__file__).resolve().parents[1], args.tag)
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8', newline='\n') as output:
            output.write(''.join(f'{key}={value}\n' for key, value in metadata.items()))
    print(json.dumps(metadata))


if __name__ == '__main__':
    main()
