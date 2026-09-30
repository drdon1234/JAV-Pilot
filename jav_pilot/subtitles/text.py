"""Decode, validate and normalize downloaded subtitle files.

Everything here is a pure function over untrusted bytes: nothing is executed,
and the output is plain UTF-8 text in the same subtitle format.
"""

from __future__ import annotations

import codecs
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from opencc import OpenCC

from .models import MAX_SUBTITLE_BYTES, SCRIPTS

MIN_CUES = 20
MIN_HAN_RATIO = 0.30
MAX_KANA_RATIO = 0.05
_SCRIPT_SAMPLE_CHARS = 20_000

_TIME = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_CUE_TIMING_RE = re.compile(rf"^{_TIME}\s*-->\s*{_TIME}")
_ASS_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})[.:](\d{1,3})$")
_TAG_RE = re.compile(r"<[^>]*>|\{[^}]*\}")
_INDEX_RE = re.compile(r"^\d+$")
_VTT_BLOCKS = ("WEBVTT", "NOTE", "STYLE", "REGION")

_CONVERTERS: dict[str, OpenCC] = {}
_CONVERTERS_LOCK = threading.Lock()


class SubtitleTextError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ParsedSubtitle:
    format: str
    text: str
    cue_count: int
    dialogue: str
    last_end_ms: int


def decode_subtitle(raw: bytes) -> str:
    if not raw:
        raise SubtitleTextError("empty")
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise SubtitleTextError("too_large")
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(bom):
            try:
                return raw[len(bom):].decode(encoding)
            except UnicodeDecodeError as exc:
                raise SubtitleTextError("undecodable") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    decoded: list[str] = []
    for encoding in ("gb18030", "big5"):
        try:
            decoded.append(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    if not decoded:
        raise SubtitleTextError("undecodable")
    # GB18030 decodes almost any byte string, Big5 files included, into rare
    # characters; the plausible decoding is the one made of common text.
    return max(decoded, key=_common_text_ratio)


def detect_format(text: str) -> str | None:
    head = text.lstrip("﻿ \t\n")
    if head.startswith("WEBVTT"):
        return "vtt"
    if "[Script Info]" in text[:4096] or "[Events]" in text:
        return "ass" if "[V4+ Styles]" in text or "v4.00+" in text[:4096] else "ssa"
    if any(_CUE_TIMING_RE.match(line.strip()) for line in text.split("\n")[:400]):
        return "srt"
    return None


def parse_subtitle(raw: bytes) -> ParsedSubtitle:
    text = decode_subtitle(raw).replace("\r\n", "\n").replace("\r", "\n")
    subtitle_format = detect_format(text)
    if subtitle_format is None:
        raise SubtitleTextError("format_unknown")
    if subtitle_format in {"ass", "ssa"}:
        cue_count, dialogue, last_end_ms = _parse_ass(text)
    else:
        cue_count, dialogue, last_end_ms = _parse_cues(text)
    if cue_count < MIN_CUES:
        raise SubtitleTextError("too_few_cues")
    return ParsedSubtitle(subtitle_format, text, cue_count, dialogue, last_end_ms)


def is_chinese(dialogue: str) -> bool:
    han = kana = letters = 0
    for character in dialogue:
        if "一" <= character <= "鿿" or "㐀" <= character <= "䶿":
            han += 1
            letters += 1
        elif "぀" <= character <= "ヿ":
            kana += 1
            letters += 1
        elif character.isalpha():
            letters += 1
    if letters == 0:
        return False
    return han / letters >= MIN_HAN_RATIO and kana / letters <= MAX_KANA_RATIO


def detect_script(dialogue: str) -> str:
    sample = dialogue[:_SCRIPT_SAMPLE_CHARS]
    traditional_marks = _changed_characters(sample, _converter("t2s").convert(sample))
    simplified_marks = _changed_characters(sample, _converter("s2t").convert(sample))
    return "zh-TW" if traditional_marks > simplified_marks else "zh-CN"


def convert_script(parsed: ParsedSubtitle, target: str) -> tuple[str, str]:
    """Return the subtitle text in ``target`` script and that script."""

    if target not in SCRIPTS:
        raise ValueError("subtitle script is invalid")
    if detect_script(parsed.dialogue) == target:
        return parsed.text, target
    converter = _converter("t2s" if target == "zh-CN" else "s2t")
    if parsed.format in {"ass", "ssa"}:
        return _convert_ass(parsed.text, converter.convert), target
    return converter.convert(parsed.text), target


def encode_subtitle(text: str) -> bytes:
    clean = text if text.endswith("\n") else f"{text}\n"
    return clean.encode("utf-8")


def _parse_cues(text: str) -> tuple[int, str, int]:
    count = 0
    last_end_ms = 0
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        match = _CUE_TIMING_RE.match(stripped)
        if match is not None:
            count += 1
            last_end_ms = max(last_end_ms, _cue_time_ms(match.groups()[4:8]))
            continue
        if not stripped or _INDEX_RE.fullmatch(stripped) or stripped.startswith(_VTT_BLOCKS):
            continue
        lines.append(_TAG_RE.sub("", stripped))
    return count, "\n".join(lines), last_end_ms


def _parse_ass(text: str) -> tuple[int, str, int]:
    count = 0
    last_end_ms = 0
    lines: list[str] = []
    for fields, values in _ass_dialogues(text):
        end = _ass_time_ms(values[fields.index("end")].strip())
        if end is None:
            continue
        count += 1
        last_end_ms = max(last_end_ms, end)
        body = values[fields.index("text")]
        lines.append(_TAG_RE.sub("", body).replace("\\N", " ").replace("\\n", " "))
    return count, "\n".join(lines), last_end_ms


def _ass_dialogues(text: str) -> Iterator[tuple[list[str], list[str]]]:
    in_events = False
    fields: list[str] | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events:
            continue
        lowered = stripped.lower()
        if lowered.startswith("format:"):
            fields = [item.strip().lower() for item in stripped[7:].split(",")]
            continue
        if not lowered.startswith("dialogue:"):
            continue
        if not fields or "end" not in fields or fields[-1] != "text":
            raise SubtitleTextError("format_unknown")
        values = stripped[9:].split(",", len(fields) - 1)
        if len(values) == len(fields):
            yield fields, values


def _convert_ass(text: str, convert: Callable[[str], str]) -> str:
    in_events = False
    fields: list[str] | None = None
    output: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
        elif in_events and stripped.lower().startswith("format:"):
            fields = [item.strip().lower() for item in stripped[7:].split(",")]
        elif in_events and fields and stripped.lower().startswith("dialogue:"):
            prefix, _separator, rest = line.partition(":")
            values = rest.split(",", len(fields) - 1)
            if len(values) == len(fields):
                values[-1] = convert(values[-1])
                line = f"{prefix}:{','.join(values)}"
        output.append(line)
    return "\n".join(output)


def _cue_time_ms(parts: tuple[str | None, ...]) -> int:
    hours, minutes, seconds, fraction = parts
    return (
        int(hours or 0) * 3_600_000
        + int(minutes or 0) * 60_000
        + int(seconds or 0) * 1000
        + int(str(fraction or "0").ljust(3, "0")[:3])
    )


def _ass_time_ms(value: str) -> int | None:
    match = _ASS_TIME_RE.match(value)
    if match is None:
        return None
    # ASS stores centiseconds; the helper pads "90" to 900 ms.
    return _cue_time_ms(match.groups())


def _changed_characters(before: str, after: str) -> int:
    return sum(1 for left, right in zip(before, after) if left != right)


def _common_text_ratio(text: str) -> float:
    if not text:
        return 0.0
    common = sum(
        1
        for character in text
        if character.isascii()
        or "一" <= character <= "鿿"
        or "　" <= character <= "〿"
        or "＀" <= character <= "￯"
        or character in "“”‘’…—·"
    )
    return common / len(text)


def _converter(config: str) -> OpenCC:
    with _CONVERTERS_LOCK:
        converter = _CONVERTERS.get(config)
        if converter is None:
            converter = OpenCC(config)
            _CONVERTERS[config] = converter
        return converter
