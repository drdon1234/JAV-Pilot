from __future__ import annotations

from typing import Literal, TypeAlias

from .quality import (
    QualityHeightError,
    height_matches_selected_quality,
    validate_quality_height,
)


ExistingPolicy: TypeAlias = Literal[
    "keep_both",
    "higher_quality",
    "overwrite",
    "skip",
]

LEGACY_EXISTING_POLICY: ExistingPolicy = "keep_both"
DEFAULT_EXISTING_POLICY: ExistingPolicy = "higher_quality"
USER_EXISTING_POLICIES = frozenset(("higher_quality", "overwrite", "skip"))
ALL_EXISTING_POLICIES = frozenset((*USER_EXISTING_POLICIES, "keep_both"))


def validate_existing_policy(
    value: object | None,
    *,
    default: ExistingPolicy = DEFAULT_EXISTING_POLICY,
    allow_legacy: bool = False,
) -> ExistingPolicy:
    policy = default if value is None else value
    allowed = ALL_EXISTING_POLICIES if allow_legacy else USER_EXISTING_POLICIES
    if not isinstance(policy, str) or policy not in allowed:
        raise ValueError("existing work policy is invalid")
    return policy  # type: ignore[return-value]


def is_strict_quality_upgrade(
    candidate_height: object,
    incumbent_height: object,
) -> bool:
    try:
        candidate = validate_quality_height(candidate_height)
        incumbent = validate_quality_height(incumbent_height)
    except QualityHeightError:
        return False
    if height_matches_selected_quality(incumbent, candidate):
        return False
    return candidate > incumbent
