"""Bounded, safe parsing of Kodi-style movie NFO files."""

from __future__ import annotations

import json
import os
import stat
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from xml.parsers import expat

from ..core.catalog_code import normalize_catalog_code
from .errors import MediaLibraryError, MediaLibraryUnavailableError
from .fields import dedupe, first, optional_string, string_tuple
from .filesystem import (
    is_linklike,
    regular_file_fingerprint,
    regular_root,
    require_within_root,
)
from .models import (
    MAX_NFO_BYTES,
    MAX_NFO_DEPTH,
    MAX_NFO_NODES,
    MAX_NFO_TEXT_BYTES,
    MutableMetrics,
)

__all__ = [
    "NfoMetadata",
    "parse_movie_nfo",
    "read_movie_nfo",
]


class _UnsafeNfoError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NfoMetadata:
    code: str | None
    code_key: str | None
    title: str | None
    release_date: str | None
    actors: tuple[str, ...]
    makers: tuple[str, ...]
    publishers: tuple[str, ...]
    tags: tuple[str, ...]
    series: tuple[str, ...]
    directors: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "code": self.code,
                "code_key": self.code_key,
                "title": self.title,
                "release_date": self.release_date,
                "actors": list(self.actors),
                "makers": list(self.makers),
                "publishers": list(self.publishers),
                "tags": list(self.tags),
                "series": list(self.series),
                "directors": list(self.directors),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def read_movie_nfo(path: Path, library_root: Path) -> NfoMetadata | None:
    root, _ = regular_root(library_root, MutableMetrics())
    target = Path(path)
    if not target.is_absolute():
        raise MediaLibraryError("media library NFO path must be absolute")
    require_within_root(target, root, directory=False)
    try:
        initial = target.lstat()
    except OSError as exc:
        raise MediaLibraryUnavailableError("media library NFO is unavailable") from exc
    if is_linklike(initial) or not stat.S_ISREG(initial.st_mode):
        return None
    if initial.st_size <= 0 or initial.st_size > MAX_NFO_BYTES:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if regular_file_fingerprint(opened) != regular_file_fingerprint(initial):
            return None
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                return None
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            return None
        final = os.fstat(descriptor)
        if regular_file_fingerprint(final) != regular_file_fingerprint(opened):
            return None
        return parse_movie_nfo(b"".join(chunks))
    except OSError as exc:
        raise MediaLibraryUnavailableError(
            "media library NFO could not be read"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def parse_movie_nfo(body: bytes) -> NfoMetadata | None:
    if (
        not isinstance(body, bytes)
        or not 0 < len(body) <= MAX_NFO_BYTES
        or b"\x00" in body
        or body.startswith(
            (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")
        )
    ):
        return None
    try:
        body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    parser = expat.ParserCreate()
    stack: list[str] = []
    values: dict[str, list[str]] = defaultdict(list)
    current_parts: list[str] | None = None
    current_field: str | None = None
    node_count = 0
    text_bytes = 0
    declared_encoding: str | None = None
    invalid = False
    actor_depth: int | None = None

    direct_fields = {
        "id",
        "uniqueid",
        "title",
        "premiered",
        "releasedate",
        "actor",
        "director",
        "genre",
        "tag",
        "studio",
        "set",
    }

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal node_count, invalid, current_parts, current_field, actor_depth
        node_count += 1
        if node_count > MAX_NFO_NODES or len(stack) >= MAX_NFO_DEPTH:
            raise _UnsafeNfoError("NFO structure budget exceeded")
        clean = name.casefold()
        if not stack and clean != "movie":
            invalid = True
        stack.append(clean)
        if len(stack) == 2 and clean in direct_fields and clean != "actor":
            current_field = clean
            current_parts = []
        elif len(stack) == 2 and clean == "actor":
            actor_depth = len(stack)
        elif (
            actor_depth is not None
            and len(stack) == actor_depth + 1
            and clean == "name"
        ):
            current_field = "actor"
            current_parts = []

    def data(value: str) -> None:
        nonlocal text_bytes, invalid
        encoded_length = len(value.encode("utf-8", errors="ignore"))
        text_bytes += encoded_length
        if text_bytes > MAX_NFO_TEXT_BYTES:
            raise _UnsafeNfoError("NFO text budget exceeded")
        if current_parts is not None:
            current_parts.append(value)

    def end(name: str) -> None:
        nonlocal invalid, current_parts, current_field, actor_depth
        clean = name.casefold()
        if not stack or stack[-1] != clean:
            invalid = True
            return
        if current_field is not None and (
            (len(stack) == 2 and clean == current_field)
            or (current_field == "actor" and clean == "name")
        ):
            text = _clean_nfo_text("".join(current_parts or ()))
            if text:
                values[current_field].append(text)
            current_parts = None
            current_field = None
        if clean == "actor" and actor_depth == len(stack):
            actor_depth = None
        stack.pop()

    def declaration(_version: str, encoding: str | None, _standalone: int) -> None:
        nonlocal declared_encoding
        declared_encoding = encoding

    def reject_doctype(*_args: object) -> None:
        raise _UnsafeNfoError("NFO document types are forbidden")

    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    try:
        parser.Parse(body, True)
    except (expat.ExpatError, UnicodeError, ValueError):
        return None
    if declared_encoding and declared_encoding.replace("_", "-").casefold() not in {
        "utf-8",
        "utf8",
    }:
        return None
    if invalid or stack:
        return None
    identities = [
        normalize_catalog_code(value, max_length=40)
        for field in ("id", "uniqueid")
        for value in values[field]
    ]
    identities = [value for value in identities if value is not None]
    if identities and any(value[1] != identities[0][1] for value in identities[1:]):
        return None
    identity = identities[0] if identities else None
    return NfoMetadata(
        code=identity[0] if identity else None,
        code_key=identity[1] if identity else None,
        title=first(values["title"]),
        release_date=first(values["premiered"]) or first(values["releasedate"]),
        actors=dedupe(values["actor"]),
        makers=dedupe(values["studio"]),
        publishers=(),
        tags=dedupe((*values["genre"], *values["tag"])),
        series=dedupe(values["set"]),
        directors=dedupe(values["director"]),
    )


def nfo_from_json(value: str) -> NfoMetadata:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MediaLibraryError("media library NFO metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise MediaLibraryError("media library NFO metadata is invalid")
    return NfoMetadata(
        code=optional_string(payload.get("code")),
        code_key=optional_string(payload.get("code_key")),
        title=optional_string(payload.get("title")),
        release_date=optional_string(payload.get("release_date")),
        actors=string_tuple(payload.get("actors")),
        makers=string_tuple(payload.get("makers")),
        publishers=string_tuple(payload.get("publishers")),
        tags=string_tuple(payload.get("tags")),
        series=string_tuple(payload.get("series")),
        directors=string_tuple(payload.get("directors")),
    )


def _clean_nfo_text(value: str) -> str | None:
    clean = " ".join(value.split())
    if not clean or len(clean.encode("utf-8")) > 16 * 1024:
        return None
    if any(ord(character) < 32 for character in clean):
        return None
    return clean
