"""Building review drafts from source snapshots, NFO files and edits."""

from __future__ import annotations

import math
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence

from ...core.catalog_code import normalize_catalog_code
from ..publish import MAX_NFO_BYTES
from .errors import MediaMetadataReviewValidationError
from .fields import (
    bounded_int,
    json_digest,
    json_load,
    optional_float,
    optional_int,
    optional_string,
    reject_sensitive_text,
    string_tuple,
    validated_code,
)
from .models import (
    FIELD_PRIORITIES,
    INVALID_XML_BYTES_RE,
    LIST_FIELDS,
    METADATA_FIELDS,
    RELEASE_DATE_RE,
    SENSITIVE_KEY_RE,
    SOURCE_FIELDS,
    DraftMetadata,
)

def latest_source_values(
    connection: sqlite3.Connection, review_id: str
) -> dict[str, dict[str, dict[str, object]]]:
    rows = connection.execute(
        """
        SELECT s.snapshot_id, s.source_id, s.fetched_at,
               v.field_name, v.value_json
        FROM media_metadata_review_values v
        JOIN media_metadata_review_snapshots s ON s.snapshot_id = v.snapshot_id
        WHERE s.review_id = ?
        ORDER BY s.fetched_at DESC, s.created_at DESC, s.rowid DESC
        """,
        (review_id,),
    ).fetchall()
    result: dict[str, dict[str, dict[str, object]]] = {}
    for row in rows:
        field_name = str(row["field_name"])
        source_id = str(row["source_id"])
        values = result.setdefault(field_name, {})
        if source_id in values:
            continue
        values[source_id] = {
            "value": json_load(str(row["value_json"])),
            "fetched_at": float(row["fetched_at"]),
            "snapshot_id": str(row["snapshot_id"]),
        }
    return result


def load_draft_rows(
    connection: sqlite3.Connection, review_id: str
) -> dict[str, dict[str, object]]:
    return {
        str(row["field_name"]): dict(row)
        for row in connection.execute(
            "SELECT * FROM media_metadata_review_drafts WHERE review_id = ?",
            (review_id,),
        ).fetchall()
    }


def latest_images(
    connection: sqlite3.Connection, review_id: str
) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {}
    rows = connection.execute(
        "SELECT * FROM media_metadata_review_images WHERE review_id = ? "
        "ORDER BY fetched_at DESC, created_at DESC, rowid DESC",
        (review_id,),
    ).fetchall()
    for row in rows:
        kind = str(row["kind"])
        source_id = str(row["source_id"])
        values = result.setdefault(kind, {})
        if source_id in values:
            continue
        values[source_id] = {
            "image_id": str(row["image_id"]),
            "sha256": str(row["sha256"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "fetched_at": float(row["fetched_at"]),
        }
    return result


def resolve_field(
    field_name: str,
    code: str,
    source_values: Mapping[str, Mapping[str, Mapping[str, object]]],
    draft: Mapping[str, object],
    *,
    ignore_lock: bool = False,
) -> tuple[object, str]:
    if (
        not ignore_lock
        and bool(draft.get("locked", 0))
        and draft.get("locked_value_json") is not None
    ):
        return (
            json_load(str(draft["locked_value_json"])),
            str(draft.get("locked_source") or "locked"),
        )
    if draft.get("manual_json") is not None:
        return json_load(str(draft["manual_json"])), "manual"
    selected = str(draft.get("selected_source") or "")
    field_sources = source_values.get(field_name, {})
    if selected:
        selected_value = field_sources.get(selected)
        return (
            selected_value.get("value") if selected_value is not None else None,
            selected,
        )
    for source_id in FIELD_PRIORITIES[field_name]:
        source_value = field_sources.get(source_id)
        if source_value is not None:
            return source_value.get("value"), source_id
    if field_name == "title":
        return code, "default"
    if field_name in LIST_FIELDS:
        return [], "default"
    return None, "default"


def draft_metadata(review: Mapping[str, object]) -> DraftMetadata:
    fields = review.get("fields")
    if not isinstance(fields, Mapping):
        raise MediaMetadataReviewValidationError("metadata review draft is invalid")

    def value(field_name: str) -> object:
        field = fields.get(field_name)
        if not isinstance(field, Mapping):
            raise MediaMetadataReviewValidationError("metadata review field is invalid")
        return field.get("final_value")

    title = str(value("title") or review["code"]).strip()
    if not title:
        raise MediaMetadataReviewValidationError("metadata review title is empty")
    return DraftMetadata(
        code=str(review["code"]),
        title=title,
        original_title=optional_string(value("original_title")),
        release_date=optional_string(value("release_date")),
        duration_minutes=optional_int(value("duration_minutes")),
        rating=optional_float(value("rating")),
        makers=string_tuple(value("makers")),
        publishers=string_tuple(value("publishers")),
        series=string_tuple(value("series")),
        directors=string_tuple(value("directors")),
        actors=string_tuple(value("actors")),
        tags=string_tuple(value("tags")),
        description=optional_string(value("description")),
    )


def compute_draft_digest(review: Mapping[str, object]) -> str:
    fields = review.get("fields")
    if not isinstance(fields, Mapping):
        raise MediaMetadataReviewValidationError("metadata review fields are invalid")
    payload = {
        field_name: {
            "value": fields[field_name]["final_value"],
            "source": fields[field_name]["final_source"],
            "locked": fields[field_name]["locked"],
        }
        for field_name in METADATA_FIELDS
    }
    return json_digest(payload)


def normalized_snapshot_fields(
    source_id: str, fields: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(fields, Mapping):
        raise MediaMetadataReviewValidationError(
            "metadata review source snapshot must be an object"
        )
    normalized: dict[str, object] = {}
    allowed = SOURCE_FIELDS[source_id]
    for raw_name, raw_value in fields.items():
        field_name = validated_field_name(raw_name)
        if field_name not in allowed:
            raise MediaMetadataReviewValidationError(
                "metadata review source is not allowed for this field"
            )
        value = normalize_field_value(field_name, raw_value, allow_none=True)
        if value is not None:
            normalized[field_name] = value
    return normalized


def normalize_field_value(
    field_name: str, value: object, *, allow_none: bool
) -> object:
    if value is None:
        if allow_none:
            return None
        raise MediaMetadataReviewValidationError("metadata review value is missing")
    if field_name in LIST_FIELDS:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise MediaMetadataReviewValidationError(
                "metadata review list field is invalid"
            )
        output: list[str] = []
        seen: set[str] = set()
        if len(value) > 100:
            raise MediaMetadataReviewValidationError(
                "metadata review list field has too many values"
            )
        for item in value:
            clean = _clean_text(item, maximum=500)
            if not clean:
                continue
            key = clean.casefold()
            if key not in seen:
                seen.add(key)
                output.append(clean)
        return output
    if field_name == "duration_minutes":
        return bounded_int(value, "duration", 1, 24 * 60)
    if field_name == "rating":
        if isinstance(value, bool):
            raise MediaMetadataReviewValidationError(
                "metadata review rating is invalid"
            )
        try:
            rating = float(value)
        except (TypeError, ValueError) as exc:
            raise MediaMetadataReviewValidationError(
                "metadata review rating is invalid"
            ) from exc
        if not math.isfinite(rating) or not 0 <= rating <= 10:
            raise MediaMetadataReviewValidationError(
                "metadata review rating is invalid"
            )
        return rating
    maximum = 64 * 1024 if field_name == "description" else 2000
    clean = _clean_text(value, maximum=maximum)
    if not clean:
        return None if allow_none else ""
    if field_name == "release_date" and not RELEASE_DATE_RE.fullmatch(clean):
        raise MediaMetadataReviewValidationError(
            "metadata review release date is invalid"
        )
    return clean


def _clean_text(value: object, *, maximum: int) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean.encode("utf-8")) > maximum or any(ord(item) < 32 for item in clean):
        raise MediaMetadataReviewValidationError("metadata review text is invalid")
    reject_sensitive_text(clean, "metadata value")
    return clean


def validated_field_name(value: object) -> str:
    clean = str(value or "").strip()
    if clean not in METADATA_FIELDS:
        raise MediaMetadataReviewValidationError("metadata review field is invalid")
    if SENSITIVE_KEY_RE.search(clean):
        raise MediaMetadataReviewValidationError("metadata review field is sensitive")
    return clean


def source_choice(field_name: str, value: object) -> str | None:
    clean = str(value or "").strip().lower()
    if clean in {"", "auto"}:
        return None
    allowed = {source for source, fields in SOURCE_FIELDS.items() if field_name in fields}
    if clean not in allowed:
        raise MediaMetadataReviewValidationError(
            "metadata review source choice is invalid"
        )
    return clean


def parse_movie_nfo(body: bytes, expected_code: str) -> dict[str, object]:
    if (
        not body
        or len(body) > MAX_NFO_BYTES
        or b"\x00" in body
        or INVALID_XML_BYTES_RE.search(body)
    ):
        raise MediaMetadataReviewValidationError("metadata review NFO is unsafe")
    try:
        text = body.decode("utf-8-sig")
        root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise MediaMetadataReviewValidationError(
            "metadata review NFO is invalid"
        ) from exc
    if root.tag != "movie" or sum(1 for _ in root.iter()) > 2000:
        raise MediaMetadataReviewValidationError("metadata review NFO is invalid")
    expected = validated_code(expected_code)[1]
    identities = [
        node.text for node in root.findall("./id") if node.text and node.text.strip()
    ]
    identities.extend(
        node.text
        for node in root.findall("./uniqueid")
        if node.text
        and node.text.strip()
        and str(node.attrib.get("type") or "").strip().casefold() == "jav"
    )
    normalized = [normalize_catalog_code(item, max_length=40) for item in identities]
    if not normalized or any(
        item is None or item[1] != expected for item in normalized
    ):
        raise MediaMetadataReviewValidationError(
            "metadata review NFO catalog code does not match"
        )

    def first(*names: str) -> str | None:
        for name in names:
            node = root.find(f"./{name}")
            if node is not None and node.text and node.text.strip():
                return node.text
        return None

    def all_text(name: str) -> list[str]:
        return [
            str(node.text)
            for node in root.findall(f"./{name}")
            if node.text and node.text.strip()
        ]

    values: dict[str, object] = {
        "title": first("title"),
        "original_title": first("originaltitle"),
        "release_date": first("premiered", "releasedate"),
        "duration_minutes": first("runtime"),
        "rating": first("rating"),
        "makers": all_text("studio"),
        "publishers": [],
        "series": all_text("set"),
        "directors": all_text("director"),
        "actors": [
            str(name.text)
            for actor in root.findall("./actor")
            if (name := actor.find("./name")) is not None
            and name.text
            and name.text.strip()
        ],
        "tags": [*all_text("genre"), *all_text("tag")],
        "description": first("plot", "outline"),
    }
    return {
        field_name: normalized_value
        for field_name, raw_value in values.items()
        if (
            normalized_value := normalize_field_value(
                field_name, raw_value, allow_none=True
            )
        )
        is not None
        and normalized_value != []
    }
