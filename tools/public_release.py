"""Validate and export the public source tree without local state or Git history."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    '.dockerignore', '.env.example', '.gitattributes', '.gitignore',
    'Dockerfile', 'LICENSE', 'PRODUCT.md', 'README.md', 'SECURITY.md',
    'docker-compose.yml', 'pyproject.toml', 'requirements.lock',
)
FRONTEND_FILES = (
    'index.html', 'package.json', 'package-lock.json', 'tsconfig.json',
    'vite.config.ts', 'vitest.config.ts',
)
TREES = {
    'jav_pilot': {'.py'},
    'deploy': {'.yml', '.example'},
    'ops': {'.py', '.yml'},
    'tools': {'.py'},
    'docs': {'.md', '.svg', '.png', '.webp'},
    'frontend/src': {'.ts', '.tsx', '.css'},
    'frontend/public': {'.svg', '.png', '.webp', '.ico', '.woff2'},
}
OLD_NAME = re.compile(
    r'(?i)\b(?:jav[-_]media[-_]m[v]p|jav[-_]m[v]p)(?=\b|_)|\bjavm[v]p|\bm[v]p\b'
)
SECRETS = (
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    re.compile(r'https?://[^\s/"\']+:[^\s/"\']+@'),
)


class PublicReleaseError(ValueError):
    pass


def public_sources(root: Path) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    candidates = [root / name for name in ROOT_FILES]
    candidates.extend(root / 'frontend' / name for name in FRONTEND_FILES)
    for name, extensions in TREES.items():
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise PublicReleaseError(f'public source directory is missing or unsafe: {name}')
        for path in directory.rglob('*'):
            if '__pycache__' in path.parts:
                continue
            if path.is_symlink():
                raise PublicReleaseError(f'public source symlink is forbidden: {path.relative_to(root)}')
            if path.is_file() and path.suffix in extensions:
                candidates.append(path)
    sources: dict[str, bytes] = {}
    for path in sorted(candidates):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise PublicReleaseError(f'public source file is missing or unsafe: {relative}')
        data = path.read_bytes()
        if path.suffix not in {'.png', '.webp', '.ico', '.woff2'}:
            try:
                content = data.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise PublicReleaseError(f'public text must be UTF-8: {relative}') from exc
            if OLD_NAME.search(content):
                raise PublicReleaseError(f'obsolete project name: {relative}')
            if any(pattern.search(content) for pattern in SECRETS):
                raise PublicReleaseError(f'possible credential material: {relative}')
        sources[relative] = data
    return sources


def export_sources(sources: dict[str, bytes], output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents replacing an artifact the caller has not reviewed.
    with output.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(sources.items()):
                info = zipfile.ZipInfo(f'jav-pilot/{name}', date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='create a new public source ZIP at this path')
    args = parser.parse_args()
    try:
        sources = public_sources(ROOT)
        result: dict[str, object] = {'ok': True, 'files': len(sources)}
        if args.output is not None:
            result['sha256'] = export_sources(sources, args.output)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (PublicReleaseError, OSError) as exc:
        parser.exit(1, f'Public source export failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
