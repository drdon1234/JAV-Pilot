from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit

SCRIPTS = ("zh-CN", "zh-TW")
FORMATS = ("srt", "ass", "ssa", "vtt")
PROVIDER_IDS = ("xunlei", "subtitlecat")
MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
MAX_DURATION_MS = 24 * 3600 * 1000


@dataclass(frozen=True, slots=True)
class SubtitleCandidate:
    provider: str
    file_name: str
    download_url: str
    format: str | None
    declared_script: str | None
    duration_ms: int | None
    machine_translated: bool

    @property
    def candidate_id(self) -> str:
        digest = hashlib.sha256(self.download_url.encode("utf-8")).hexdigest()[:20]
        return f"{self.provider}-{digest}"

    def to_record(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "file_name": self.file_name,
            "download_url": self.download_url,
            "format": self.format,
            "declared_script": self.declared_script,
            "duration_ms": self.duration_ms,
            "machine_translated": self.machine_translated,
        }

    @classmethod
    def from_record(cls, value: object) -> "SubtitleCandidate":
        if not isinstance(value, dict):
            raise ValueError("subtitle candidate record is invalid")
        provider = value.get("provider")
        file_name = value.get("file_name")
        url = value.get("download_url")
        subtitle_format = value.get("format")
        script = value.get("declared_script")
        duration = value.get("duration_ms")
        machine = value.get("machine_translated")
        if provider not in PROVIDER_IDS:
            raise ValueError("subtitle candidate provider is invalid")
        if not isinstance(file_name, str) or not file_name.strip() or len(file_name) > 255:
            raise ValueError("subtitle candidate name is invalid")
        if not isinstance(url, str) or len(url) > 2048 or urlsplit(url).scheme != "https":
            raise ValueError("subtitle candidate URL is invalid")
        if subtitle_format is not None and subtitle_format not in FORMATS:
            raise ValueError("subtitle candidate format is invalid")
        if script is not None and script not in SCRIPTS:
            raise ValueError("subtitle candidate script is invalid")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 0 < duration <= MAX_DURATION_MS
        ):
            raise ValueError("subtitle candidate duration is invalid")
        if not isinstance(machine, bool):
            raise ValueError("subtitle candidate translation flag is invalid")
        return cls(
            provider=provider,
            file_name=file_name,
            download_url=url,
            format=subtitle_format,
            declared_script=script,
            duration_ms=duration,
            machine_translated=machine,
        )
