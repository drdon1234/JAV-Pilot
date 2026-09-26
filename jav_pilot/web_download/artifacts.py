from __future__ import annotations

import hashlib


_ARTIFACT_KINDS = frozenset({"backup", "failed", "part"})


def replacement_artifact_name(
    target_name: str,
    job_id: str,
    kind: str,
) -> str:
    """Return a bounded same-directory artifact name for an archive target."""

    if kind not in _ARTIFACT_KINDS:
        raise ValueError("archive replacement artifact kind is invalid")
    target_digest = hashlib.sha256(target_name.encode("utf-8")).hexdigest()[:16]
    return f".jav-pilot-{job_id}.{target_digest}.{kind}"
