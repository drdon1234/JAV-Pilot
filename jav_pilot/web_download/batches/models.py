"""Batch requests, planned items and the limits that bound them."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ..policy import DEFAULT_EXISTING_POLICY
from ..variant import DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY, MissavVariant

MAX_BATCH_ITEMS = 64

DISCOVERY_LIMIT = MAX_BATCH_ITEMS + 1
DEFAULT_BATCH_PAGE_BUDGET = 16
ABSOLUTE_MAX_BATCH_PAGES = 64


ITEM_QUALITY_STRATEGIES = ("highest", "selected")

SERIES_DISCOVERY_PROVENANCE = "series_discovery"
RESOURCE_SEARCH_SELECTION_PROVENANCE = "resource_search_selection"


BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BatchRequest:
    mode: str
    code_or_prefix: str
    prefix: str
    suffix_width: int | None
    start: str | None
    end: str | None
    max_height: int
    existing_policy: str = DEFAULT_EXISTING_POLICY
    variant_priority: tuple[MissavVariant, ...] = DEFAULT_WEB_DOWNLOAD_VARIANT_PRIORITY
    provenance_type: str = SERIES_DISCOVERY_PROVENANCE
    auto_commit: bool = False


@dataclass(frozen=True)
class SelectedBatchItem:
    code: str
    code_key: str
    available_variants: tuple[MissavVariant, ...]
    variant: MissavVariant


@dataclass(frozen=True)
class SelectedBatchPlan:
    items: tuple[SelectedBatchItem, ...]
    source_session_id: str
    source_revision: int
    max_height: int
    existing_policy: str
    variant_priority: tuple[MissavVariant, ...]
    quality_strategy: str
    default_height: int
    selected_code_keys: frozenset[str] | None
    library_revision: str | None
    rule_id: str | None
    rule_revision: int | None


@dataclass(frozen=True)
class BatchItemIntent:
    code_key: str
    variant: MissavVariant
    quality_strategy: str
    requested_height: int


@dataclass(frozen=True)
class ExistingWork:
    output_path: str | None
    job_id: str | None
    verified_height: int | None
    selected_height: int | None
    variant: MissavVariant | None = None


@dataclass(frozen=True)
class LibraryDeduplicationSnapshot:
    code_keys: frozenset[str]
    completed_job_ids: frozenset[str]
    existing_by_code: Mapping[str, ExistingWork] = field(default_factory=dict)
    observed_completed_job_ids: frozenset[str] | None = None
    resolved_library_root: Path | None = None
    library_root_identity: tuple[int, int] | None = None
    tree_revision: str | None = None
    index_revision: str | None = None
    index_generation_id: str | None = None
    index_database_path: Path | None = None
    index_root_key: str | None = None
    variant_keys: frozenset[tuple[str, MissavVariant]] = frozenset()
    existing_by_variant: Mapping[tuple[str, MissavVariant], ExistingWork] = field(
        default_factory=dict
    )
    unknown_code_keys: frozenset[str] = frozenset()
