from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Sequence


SCHEMA_VERSION = 2
PLATFORM = "linux/amd64"
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "revision",
        "sha_tag",
        "image_id",
        "platform",
        "archive_filename",
        "archive_sha256",
    }
)
_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_SHA_TAG_PATTERN = re.compile(
    r"(?P<repository>[a-z0-9][a-z0-9._:-]*"
    r"(?:/[a-z0-9][a-z0-9._-]*)*):(?P<tag>[0-9a-f]{40})"
)


class ImageBundleManifestError(ValueError):
    pass


def _require_fullmatch(pattern: re.Pattern[str], value: str, field: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ImageBundleManifestError(f"invalid {field}")


def _archive_sha256(path: Path) -> str:
    with path.open("rb") as archive:
        return hashlib.file_digest(archive, "sha256").hexdigest()


def create_manifest(
    *,
    revision: str,
    sha_tag: str,
    image_id: str,
    platform: str,
    archive: Path,
) -> dict[str, object]:
    _require_fullmatch(_REVISION_PATTERN, revision, "revision")
    _require_fullmatch(_DIGEST_PATTERN, image_id, "image ID")
    if platform != PLATFORM:
        raise ImageBundleManifestError("invalid platform")
    sha_tag_match = _SHA_TAG_PATTERN.fullmatch(sha_tag)
    if (
        sha_tag_match is None
        or sha_tag_match.group("tag") != revision
        or len(sha_tag) > 320
        or "//" in sha_tag
    ):
        raise ImageBundleManifestError("SHA tag does not match revision")

    expected_filename = f"jav-pilot-{revision}-linux-amd64.tar.zst"
    if archive.name != expected_filename or not archive.is_file():
        raise ImageBundleManifestError("invalid archive")

    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "sha_tag": sha_tag,
        "image_id": image_id,
        "platform": platform,
        "archive_filename": archive.name,
        "archive_sha256": _archive_sha256(archive),
    }
    if set(manifest) != MANIFEST_FIELDS:
        raise ImageBundleManifestError("manifest fields changed unexpectedly")
    return manifest


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    if set(manifest) != MANIFEST_FIELDS:
        raise ImageBundleManifestError("manifest must contain exactly seven fields")
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a self-contained JAV Pilot offline image manifest."
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument("--sha-tag", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = create_manifest(
            revision=args.revision,
            sha_tag=args.sha_tag,
            image_id=args.image_id,
            platform=args.platform,
            archive=args.archive,
        )
        write_manifest(args.output, manifest)
    except (ImageBundleManifestError, OSError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
