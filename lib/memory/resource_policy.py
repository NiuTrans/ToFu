"""Launch-probed resident budgets for the Memory metadata read model."""

from __future__ import annotations

from runtime_guards import resolve_resource_budget


MEMORY_METADATA_CACHE_HARD_CAPACITY = 16_384
MEMORY_METADATA_CACHE_HARD_MIB = 128
_MIB = 1024 * 1024


def memory_metadata_cache_budget() -> tuple[int, int]:
    """Return the bounded ``(entries, estimated resident bytes)`` budget."""
    entries = resolve_resource_budget(
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY',
        minimum=64,
        maximum=MEMORY_METADATA_CACHE_HARD_CAPACITY,
    )
    max_mib = resolve_resource_budget(
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB',
        minimum=1,
        maximum=MEMORY_METADATA_CACHE_HARD_MIB,
    )
    return entries, max_mib * _MIB


__all__ = [
    'MEMORY_METADATA_CACHE_HARD_CAPACITY',
    'MEMORY_METADATA_CACHE_HARD_MIB',
    'memory_metadata_cache_budget',
]
