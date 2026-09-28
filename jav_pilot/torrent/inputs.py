from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote

from ..core.catalog_code import normalize_catalog_code
from .qbittorrent import PROBE_METADATA_TEXT_MAX_BYTES
from .magnet import MagnetError, parse_magnet
from ..core.models import MagnetInfo


MAX_DOWNLOAD_INPUTS = 50
MAX_BATCH_INPUT_BYTES = 512 * 1024
MAX_MAGNET_URI_BYTES = 16 * 1024
MAX_THUNDER_URI_BYTES = 24 * 1024

_HEX_BTIH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32_BTIH_RE = re.compile(r"^[A-Z2-7a-z]{32}$")
_BTIH_IN_MAGNET_RE = re.compile(
    r"(?:^|[?&])xt=urn:btih:([0-9a-fA-F]{40}|[A-Z2-7a-z]{32})(?:&|$)",
    flags=re.IGNORECASE,
)
_CATALOG_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:"
    r"FC2[-._ ]?(?:PPV[-._ ]?)?\d{2,9}"
    r"|"
    r"[A-Z]{2,12}[-._ ]\d{2,8}"
    r")(?![A-Z0-9])",
    flags=re.IGNORECASE,
)

DownloadInputSource = Literal["magnet", "thunder", "btih"]
DownloadInputMetadataStatus = Literal[
    "not_requested",
    "pending",
    "ready",
    "unavailable",
    "restricted",
]

_VIDEO_SUFFIXES = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm", ".wmv"}
)


class DownloadInputError(ValueError):
    pass


@dataclass(frozen=True)
class DownloadInputFile:
    index: int
    name: str
    size: int

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "name": self.name, "size": self.size}


@dataclass(frozen=True)
class ParsedDownloadInput:
    source_type: DownloadInputSource
    magnet: MagnetInfo
    catalog_code: str | None = None
    metadata_status: DownloadInputMetadataStatus = "not_requested"
    torrent_name: str | None = None
    total_size: int | None = None
    file_count: int | None = None
    files: tuple[DownloadInputFile, ...] = ()
    files_truncated: bool = False
    content_error: str | None = None

    @property
    def requires_confirmation(self) -> bool:
        return self.catalog_code is None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_type": self.source_type,
            "info_hash": self.magnet.info_hash,
            "display_name": self.torrent_name or self.magnet.display_name,
            "catalog_code": self.catalog_code,
            "requires_confirmation": self.requires_confirmation,
        }
        if self.metadata_status != "not_requested":
            payload.update(
                {
                    "metadata_status": self.metadata_status,
                    "torrent_name": self.torrent_name,
                    "total_size": self.total_size,
                    "file_count": self.file_count,
                    "files": [item.to_dict() for item in self.files],
                    "files_truncated": self.files_truncated,
                    "content_error": self.content_error,
                }
            )
        return payload


@dataclass(frozen=True)
class DownloadInputIssue:
    input: str
    error: str
    index: int

    def to_dict(self) -> dict[str, object]:
        return {
            "input": self.input,
            "error": self.error,
            "index": self.index,
        }


@dataclass(frozen=True)
class ParsedDownloadBatch:
    items: tuple[ParsedDownloadInput, ...]
    errors: tuple[DownloadInputIssue, ...]
    duplicate_count: int = 0

    @property
    def requires_confirmation(self) -> bool:
        return any(item.requires_confirmation for item in self.items)

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "errors": [issue.to_dict() for issue in self.errors],
            "count": len(self.items),
            "duplicate_count": self.duplicate_count,
            "requires_confirmation": self.requires_confirmation,
        }


def parse_download_inputs(
    value: object,
    *,
    max_items: int = MAX_DOWNLOAD_INPUTS,
) -> ParsedDownloadBatch:
    safe_limit = _validated_max_items(max_items)
    values = _input_values(value, safe_limit)

    items: list[ParsedDownloadInput] = []
    errors: list[DownloadInputIssue] = []
    item_indexes: dict[str, int] = {}
    duplicate_count = 0
    for index, raw_value in enumerate(values):
        summary = _input_summary(raw_value)
        if not isinstance(raw_value, str):
            errors.append(
                DownloadInputIssue(
                    input=summary,
                    error="download input must be a string",
                    index=index,
                )
            )
            continue
        candidate = raw_value.strip()
        if not candidate:
            errors.append(
                DownloadInputIssue(
                    input=summary,
                    error="download input is empty",
                    index=index,
                )
            )
            continue
        try:
            parsed = _parse_download_input(candidate)
        except DownloadInputError as exc:
            errors.append(
                DownloadInputIssue(input=summary, error=str(exc), index=index)
            )
            continue
        item_index = item_indexes.get(parsed.magnet.info_hash)
        if item_index is not None:
            duplicate_count += 1
            if _input_information_rank(parsed) > _input_information_rank(
                items[item_index]
            ):
                items[item_index] = parsed
            continue
        item_indexes[parsed.magnet.info_hash] = len(items)
        items.append(parsed)

    return ParsedDownloadBatch(
        items=tuple(items),
        errors=tuple(errors),
        duplicate_count=duplicate_count,
    )


def enrich_download_inputs_from_probe(
    batch: ParsedDownloadBatch,
    probe_items: object,
) -> ParsedDownloadBatch:
    if not isinstance(probe_items, list):
        raise DownloadInputError("metadata probe items are invalid")
    by_hash: dict[str, dict[str, object]] = {}
    for raw_item in probe_items:
        if not isinstance(raw_item, dict):
            raise DownloadInputError("metadata probe items are invalid")
        info_hash = str(raw_item.get("info_hash") or "").strip().lower()
        if not _HEX_BTIH_RE.fullmatch(info_hash) or info_hash in by_hash:
            raise DownloadInputError("metadata probe items are invalid")
        by_hash[info_hash] = raw_item

    enriched = tuple(
        _enrich_download_input(item, by_hash.get(item.magnet.info_hash))
        for item in batch.items
    )
    return ParsedDownloadBatch(
        items=enriched,
        errors=batch.errors,
        duplicate_count=batch.duplicate_count,
    )


def _input_information_rank(item: ParsedDownloadInput) -> tuple[bool, bool]:
    return item.catalog_code is not None, bool(item.magnet.display_name)


def _validated_max_items(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_DOWNLOAD_INPUTS:
        raise DownloadInputError(
            f"max_items must be between 1 and {MAX_DOWNLOAD_INPUTS}"
        )
    return value


def _input_values(value: object, max_items: int) -> tuple[object, ...]:
    if isinstance(value, str):
        _require_batch_size(len(value.encode("utf-8", errors="replace")))
        values: tuple[object, ...] = tuple(value.split(None, max_items))
    elif isinstance(value, (list, tuple)):
        if len(value) > max_items:
            raise DownloadInputError(f"provide at most {max_items} download inputs")
        byte_count = sum(
            len(item.encode("utf-8", errors="replace")) + 1
            for item in value
            if isinstance(item, str)
        )
        _require_batch_size(byte_count)
        values = tuple(value)
    else:
        raise DownloadInputError("download inputs must be text or a list")

    if not values:
        raise DownloadInputError("at least one download input is required")
    if len(values) > max_items:
        raise DownloadInputError(f"provide at most {max_items} download inputs")
    return values


def _require_batch_size(byte_count: int) -> None:
    if byte_count > MAX_BATCH_INPUT_BYTES:
        raise DownloadInputError("download input batch is too large")


def _parse_download_input(value: str) -> ParsedDownloadInput:
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise DownloadInputError(
            "download input cannot contain whitespace or control characters"
        )
    try:
        byte_count = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DownloadInputError("download input is not valid UTF-8 text") from exc
    if byte_count > MAX_THUNDER_URI_BYTES:
        raise DownloadInputError("download input is too long")

    lowered = value.lower()
    if lowered.startswith("magnet:?"):
        magnet = _parse_magnet_uri(value)
        source_type: DownloadInputSource = "magnet"
    elif lowered.startswith("thunder://"):
        magnet = _parse_thunder_uri(value)
        source_type = "thunder"
    elif _is_btih(value):
        magnet = _magnet_from_hash(value)
        source_type = "btih"
    else:
        raise DownloadInputError(
            "download input must be a magnet URI, thunder URI, or BTIH hash"
        )

    return ParsedDownloadInput(
        source_type=source_type,
        magnet=magnet,
        catalog_code=_catalog_code_from_name(magnet.display_name),
    )


def _parse_magnet_uri(value: str) -> MagnetInfo:
    if len(value.encode("utf-8")) > MAX_MAGNET_URI_BYTES:
        raise DownloadInputError("magnet URI is too long")
    try:
        magnet = parse_magnet(value)
    except MagnetError as exc:
        raise DownloadInputError(str(exc)) from exc
    _require_safe_magnet_fields(magnet)
    btih_hashes = _normalized_magnet_btihs(magnet)
    if len(btih_hashes) != 1:
        raise DownloadInputError("magnet URI contains conflicting urn:btih hashes")
    if any(
        str(item).lower().startswith("urn:btmh:")
        for item in magnet.params.get("xt", ())
    ):
        raise DownloadInputError("hybrid v1/v2 magnets are not supported")
    return magnet


def _parse_thunder_uri(value: str) -> MagnetInfo:
    if len(value.encode("utf-8")) > MAX_THUNDER_URI_BYTES:
        raise DownloadInputError("thunder URI is too long")
    payload = value[len("thunder://") :]
    if not payload:
        raise DownloadInputError("thunder URI payload is empty")
    try:
        encoded = payload.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DownloadInputError("thunder URI payload must be strict Base64") from exc
    unpadded = encoded.rstrip(b"=")
    if len(encoded) - len(unpadded) > 2 or b"=" in unpadded or len(unpadded) % 4 == 1:
        raise DownloadInputError("thunder URI payload must be strict Base64")
    padded = unpadded + b"=" * (-len(unpadded) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DownloadInputError("thunder URI payload must be strict Base64") from exc
    if base64.b64encode(decoded).rstrip(b"=") != unpadded:
        raise DownloadInputError("thunder URI payload must be canonical Base64")
    if (
        len(decoded) <= 4
        or not decoded.startswith(b"AA")
        or not decoded.endswith(b"ZZ")
    ):
        raise DownloadInputError(
            "thunder URI payload must wrap its target with AA and ZZ"
        )
    try:
        inner = decoded[2:-2].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadInputError("thunder URI target must be valid UTF-8 text") from exc
    if (
        not inner
        or inner != inner.strip()
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in inner
        )
    ):
        raise DownloadInputError("thunder URI target is invalid")
    if inner.lower().startswith("magnet:?"):
        return _parse_magnet_uri(inner)
    if _is_btih(inner):
        return _magnet_from_hash(inner)
    raise DownloadInputError("thunder URI target must be a magnet URI or BTIH hash")


def _is_btih(value: str) -> bool:
    return bool(_HEX_BTIH_RE.fullmatch(value) or _BASE32_BTIH_RE.fullmatch(value))


def _magnet_from_hash(value: str) -> MagnetInfo:
    try:
        parsed = parse_magnet(f"magnet:?xt=urn:btih:{value}")
        return parse_magnet(f"magnet:?xt=urn:btih:{parsed.info_hash}")
    except MagnetError as exc:
        raise DownloadInputError(str(exc)) from exc


def _require_safe_magnet_fields(magnet: MagnetInfo) -> None:
    values = [magnet.display_name]
    for key, items in magnet.params.items():
        values.append(key)
        values.extend(items)
    if any(_has_control_characters(value) for value in values if value is not None):
        raise DownloadInputError(
            "magnet URI parameters cannot contain control characters"
        )


def _normalized_magnet_btihs(magnet: MagnetInfo) -> set[str]:
    hashes: set[str] = set()
    for value in magnet.params.get("xt", ()):
        text = str(value)
        if not text.lower().startswith("urn:btih:"):
            continue
        hashes.add(_normalize_btih_value(text[9:]))
    return hashes


def _normalize_btih_value(value: str) -> str:
    clean = unquote(value).strip()
    if _HEX_BTIH_RE.fullmatch(clean):
        return clean.lower()
    if _BASE32_BTIH_RE.fullmatch(clean):
        try:
            return base64.b32decode(clean.upper()).hex()
        except (binascii.Error, ValueError) as exc:
            raise DownloadInputError("invalid urn:btih info hash") from exc
    raise DownloadInputError("invalid urn:btih info hash")


def _has_control_characters(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in value
    )


def _catalog_code_from_name(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    match = _CATALOG_CODE_RE.match(value.strip().upper())
    if match is None:
        return None
    normalized = normalize_catalog_code(
        re.sub(r"[-._ ]+", "-", match.group(0)),
        max_length=40,
    )
    return normalized[0] if normalized is not None else None


def catalog_code_from_download_metadata(
    torrent_name: object,
    file_names: object = (),
) -> str | None:
    primary = _catalog_code_from_name(torrent_name)
    if primary is not None:
        return primary
    if not isinstance(file_names, (list, tuple)):
        return None
    candidates: set[str] = set()
    for value in file_names:
        if not isinstance(value, str) or not value:
            continue
        name = PurePosixPath(value).name
        if PurePosixPath(name).suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        code = _catalog_code_from_name(name)
        if code is not None:
            candidates.add(code)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _enrich_download_input(
    item: ParsedDownloadInput,
    raw_probe: dict[str, object] | None,
) -> ParsedDownloadInput:
    if raw_probe is None:
        return item
    raw_status = str(raw_probe.get("metadata_status") or "pending").strip()
    if raw_status not in {
        "pending",
        "ready",
        "unavailable",
        "restricted",
    }:
        raise DownloadInputError("metadata probe status is invalid")
    metadata_status: DownloadInputMetadataStatus = raw_status  # type: ignore[assignment]
    torrent_name = _optional_probe_text(
        raw_probe.get("torrent_name"),
        PROBE_METADATA_TEXT_MAX_BYTES,
    )
    content_error = _optional_probe_text(
        raw_probe.get("content_error"),
        PROBE_METADATA_TEXT_MAX_BYTES,
    )
    raw_files = raw_probe.get("files", [])
    if not isinstance(raw_files, list) or len(raw_files) > 200:
        raise DownloadInputError("metadata probe file list is invalid")
    files: list[DownloadInputFile] = []
    indexes: set[int] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise DownloadInputError("metadata probe file list is invalid")
        index = _required_probe_integer(raw_file.get("index"), "file index")
        size = _required_probe_integer(raw_file.get("size"), "file size")
        name = _optional_probe_text(
            raw_file.get("name"),
            PROBE_METADATA_TEXT_MAX_BYTES,
        )
        if name is None or index in indexes:
            raise DownloadInputError("metadata probe file list is invalid")
        indexes.add(index)
        files.append(DownloadInputFile(index=index, name=name, size=size))

    total_size = _optional_probe_integer(raw_probe.get("total_size"), "total size")
    file_count = _optional_probe_integer(raw_probe.get("file_count"), "file count")
    if file_count is not None and file_count < len(files):
        raise DownloadInputError("metadata probe file count is invalid")
    files_truncated = raw_probe.get("files_truncated", False)
    if type(files_truncated) is not bool:
        raise DownloadInputError("metadata probe truncation flag is invalid")
    probe_catalog_code = _optional_probe_catalog_code(raw_probe.get("catalog_code"))
    if metadata_status != "ready" and probe_catalog_code is not None:
        raise DownloadInputError("metadata probe catalog code is invalid")
    raw_requires_confirmation = raw_probe.get("requires_confirmation")
    if raw_requires_confirmation is not None:
        if type(raw_requires_confirmation) is not bool or raw_requires_confirmation != (
            probe_catalog_code is None
        ):
            raise DownloadInputError("metadata probe confirmation flag is invalid")
    metadata_catalog_code = None
    if metadata_status == "ready":
        metadata_catalog_code = (
            probe_catalog_code
            or catalog_code_from_download_metadata(
                torrent_name,
                [file.name for file in files],
            )
        )
    catalog_code = item.catalog_code or metadata_catalog_code
    return replace(
        item,
        catalog_code=catalog_code,
        metadata_status=metadata_status,
        torrent_name=torrent_name,
        total_size=total_size,
        file_count=file_count,
        files=tuple(files),
        files_truncated=files_truncated,
        content_error=content_error,
    )


def _optional_probe_text(value: object, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DownloadInputError("metadata probe text is invalid")
    clean = value.strip()
    if (
        not clean
        or len(clean.encode("utf-8", errors="replace")) > max_bytes
        or _has_control_characters(clean)
    ):
        raise DownloadInputError("metadata probe text is invalid")
    return clean


def _optional_probe_catalog_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DownloadInputError("metadata probe catalog code is invalid")
    clean = value.strip()
    normalized = normalize_catalog_code(clean, max_length=40)
    if value != clean or normalized is None or normalized[0] != clean:
        raise DownloadInputError("metadata probe catalog code is invalid")
    return clean


def _required_probe_integer(value: object, field: str) -> int:
    parsed = _optional_probe_integer(value, field)
    if parsed is None:
        raise DownloadInputError(f"metadata probe {field} is invalid")
    return parsed


def _optional_probe_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise DownloadInputError(f"metadata probe {field} is invalid")
    return value


def _input_summary(value: object) -> str:
    if not isinstance(value, str):
        return "<non-string>"
    clean = " ".join(value.strip().split())
    if not clean:
        return "<empty>"
    lowered = clean.lower()
    if lowered.startswith("magnet:?"):
        match = _BTIH_IN_MAGNET_RE.search(clean)
        if match is not None:
            btih = match.group(1)
            return f"magnet:btih:{btih[:12].lower()}..."
        return "magnet URI"
    if lowered.startswith("thunder://"):
        return f"thunder URI [{_summary_digest(clean)}]"
    if _is_btih(clean):
        return clean.lower()
    if (
        len(clean) <= 80
        and re.fullmatch(r"[A-Za-z0-9._:/+\-=]+", clean) is not None
        and not any(character in clean for character in "?#&")
    ):
        return clean
    return f"download input [{_summary_digest(clean)}]"


def _summary_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


__all__ = [
    "DownloadInputError",
    "DownloadInputFile",
    "DownloadInputIssue",
    "ParsedDownloadBatch",
    "ParsedDownloadInput",
    "catalog_code_from_download_metadata",
    "enrich_download_inputs_from_probe",
    "parse_download_inputs",
]
