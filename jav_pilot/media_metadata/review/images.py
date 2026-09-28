"""Review artwork preparation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ...config.source_catalog import METADATA_PROFILES
from ..images import MetadataImageError, _decode_as_jpeg
from .errors import MediaMetadataReviewValidationError
from .fields import enum, validated_source_id
from .models import IMAGE_KINDS, MAX_REVIEW_IMAGE_BYTES, MAX_REVIEW_IMAGE_PIXELS

@dataclass(frozen=True, slots=True)
class ReviewImage:
    body: bytes = field(repr=False)
    source_id: str


def prepare_review_image(
    image: ReviewImage, kind: object
) -> tuple[ReviewImage, dict[str, object]]:
    clean_kind = enum(kind, IMAGE_KINDS, "image kind")
    prepared = prepare_image(image, clean_kind)
    normalized = ReviewImage(body=prepared.body, source_id=prepared.source_id)
    return normalized, {
        "kind": clean_kind,
        "source_id": prepared.source_id,
        "sha256": hashlib.sha256(prepared.body).hexdigest(),
        "width": prepared.width,
        "height": prepared.height,
    }


@dataclass(frozen=True, slots=True)
class _PreparedImage:
    body: bytes = field(repr=False)
    width: int
    height: int
    source_id: str


def prepare_image(image: ReviewImage, expected_kind: str) -> _PreparedImage:
    if not isinstance(image, ReviewImage):
        raise MediaMetadataReviewValidationError("metadata review image is invalid")
    source_id = validated_source_id(image.source_id, (METADATA_PROFILES | {"manual"}))
    if (
        not isinstance(image.body, bytes)
        or not 0 < len(image.body) <= MAX_REVIEW_IMAGE_BYTES
    ):
        raise MediaMetadataReviewValidationError("metadata review image is invalid")
    try:
        # Shared bomb-safe decoder: the pixel limit is enforced from the
        # header before any pixel data is allocated.
        body, width, height = _decode_as_jpeg(
            image.body,
            max_pixels=MAX_REVIEW_IMAGE_PIXELS,
            max_output_bytes=MAX_REVIEW_IMAGE_BYTES,
        )
    except MetadataImageError as exc:
        raise MediaMetadataReviewValidationError(
            "metadata review image could not be decoded"
        ) from exc
    actual_kind = (
        "portrait" if height > width else "landscape" if width > height else "square"
    )
    if actual_kind != expected_kind:
        raise MediaMetadataReviewValidationError(
            "metadata review image orientation does not match"
        )
    return _PreparedImage(body=body, width=width, height=height, source_id=source_id)
