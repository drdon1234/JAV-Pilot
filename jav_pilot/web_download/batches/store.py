"""SQLite store for web download batches, rules and chains."""

from __future__ import annotations

from .store_auto import AutoBatchStoreMixin
from .store_base import BatchStoreBase
from .store_chains import BatchChainStoreMixin
from .store_commit import BatchCommitStoreMixin
from .store_lifecycle import BatchLifecycleStoreMixin
from .store_rules import BatchRuleStoreMixin


class WebDownloadBatchStore(
    BatchLifecycleStoreMixin,
    AutoBatchStoreMixin,
    BatchCommitStoreMixin,
    BatchChainStoreMixin,
    BatchRuleStoreMixin,
    BatchStoreBase,
):
    """SQLite store for web download batches, rules and chains.

    Each feature lives in its own mixin; the base owns the database,
    schema setup, batch reads and startup recovery they share.
    """
