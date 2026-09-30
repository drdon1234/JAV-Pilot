"""Rank subtitle candidates before anything is downloaded."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .matching import code_matches
from .models import FORMATS, SubtitleCandidate

HUMAN_SCORE = 40
DURATION_SCORE = 20
SCRIPT_SCORE = 10
OTHER_SCRIPT_SCORE = 5
FORMAT_SCORES = {"srt": 5, "ass": 3, "ssa": 3, "vtt": 1}
DURATION_TOLERANCE_MS = 120_000
DURATION_TOLERANCE_RATIO = 0.03
DURATION_REJECT_RATIO = 0.10


@dataclass(frozen=True, slots=True)
class MatchFacts:
    code: str
    target_script: str
    local_duration_ms: int | None
    allow_machine_translated: bool


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: SubtitleCandidate
    score: int
    rejected: str | None
    duration_delta_ms: int | None


def rank(candidates: Sequence[SubtitleCandidate], facts: MatchFacts) -> list[ScoredCandidate]:
    """Accepted candidates best first (ties keep source order), then rejected ones."""

    accepted: list[tuple[int, ScoredCandidate]] = []
    rejected: list[ScoredCandidate] = []
    for index, candidate in enumerate(candidates):
        scored = _score(candidate, facts)
        if scored.rejected is None:
            accepted.append((index, scored))
        else:
            rejected.append(scored)
    accepted.sort(key=lambda item: (-item[1].score, item[0]))
    return [scored for _index, scored in accepted] + rejected


def _score(candidate: SubtitleCandidate, facts: MatchFacts) -> ScoredCandidate:
    local = facts.local_duration_ms
    delta = (
        abs(candidate.duration_ms - local)
        if candidate.duration_ms is not None and local is not None
        else None
    )

    def reject(reason: str) -> ScoredCandidate:
        return ScoredCandidate(candidate, 0, reason, delta)

    if not code_matches(candidate.file_name, facts.code):
        return reject("code_mismatch")
    if candidate.format not in FORMATS:
        return reject("format_unsupported")
    if candidate.machine_translated and not facts.allow_machine_translated:
        return reject("machine_translation_disabled")
    if delta is not None and local is not None and delta > local * DURATION_REJECT_RATIO:
        return reject("duration_mismatch")

    score = 0 if candidate.machine_translated else HUMAN_SCORE
    if (
        delta is not None
        and local is not None
        and delta <= max(DURATION_TOLERANCE_MS, local * DURATION_TOLERANCE_RATIO)
    ):
        score += DURATION_SCORE
    if candidate.declared_script == facts.target_script:
        score += SCRIPT_SCORE
    elif candidate.declared_script is not None:
        score += OTHER_SCRIPT_SCORE
    score += FORMAT_SCORES[str(candidate.format)]
    return ScoredCandidate(candidate, score, None, delta)
