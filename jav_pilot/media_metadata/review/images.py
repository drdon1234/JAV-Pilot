"""Review artwork preparation."""

from __future__ import annotations

import hashlib
import io
from PIL import Image, ImageOps, UnidentifiedImageError
from dataclasses import dataclass, field

from ...config.source_catalog import METADATA_PROFILES
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
        with Image.open(io.BytesIO(image.body)) as source:
            normalized = ImageOps.exif_transpose(source)
            normalized.load()
            width, height = normalized.size
            if width <= 0 or height <= 0 or width * height > MAX_REVIEW_IMAGE_PIXELS:
                raise MediaMetadataReviewValidationError(
                    "metadata review image dimensions are invalid"
                )
            actual_kind = (
                "portrait"
                if height > width
                else "landscape"
                if width > height
                else "square"
            )
            if actual_kind != expected_kind:
                raise MediaMetadataReviewValidationError(
                    "metadata review image orientation does not match"
                )
            output = io.BytesIO()
            normalized.convert("RGB").save(
                output,
                format="JPEG",
                quality=92,
                optimize=True,
                progressive=True,
            )
    except MediaMetadataReviewValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MediaMetadataReviewValidationError(
            "metadata review image could not be decoded"
        ) from exc
    body = output.getvalue()
    if not 0 < len(body) <= MAX_REVIEW_IMAGE_BYTES:
        raise MediaMetadataReviewValidationError(
            "metadata review image could not be encoded safely"
        )
    return _PreparedImage(body=body, width=width, height=height, source_id=source_id)
