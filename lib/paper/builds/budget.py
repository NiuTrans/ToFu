"""Launch-derived idle reuse and reconstructible artifact ceilings."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from runtime_guards import resolve_resource_budget
from .contract import limit


def compile_memory_bytes(environment: Mapping[str, str] | None = None) -> int:
    """Aggregate TeX process-tree RSS, resolved from the existing launch probe."""
    return resolve_resource_budget('TOFU_PAPER_BUILD_COMPILE_MAX_MIB', environment,
        minimum=256, maximum=limit('compile_memory_bytes') // (1024 * 1024)) * 1024 * 1024


def validation_memory_bytes(environment: Mapping[str, str] | None = None) -> int:
    """One short-lived validator inside each demand-driven build worker."""
    return resolve_resource_budget('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB', environment,
                                   minimum=128, maximum=512) * 1024 * 1024


@dataclass(frozen=True)
class BuildBudget:
    workers: int
    queue_capacity: int  # Legacy configuration, never an admission ceiling.
    cache_bytes: int
    cache_records: int


def build_budget(environment: Mapping[str, str] | None = None) -> BuildBudget:
    def read(name, maximum):
        return resolve_resource_budget('TOFU_PAPER_BUILD_' + name, environment,
                                       minimum=1, maximum=maximum)
    return BuildBudget(read('WORKERS', 4), read('QUEUE_CAPACITY', 16),
                       read('CACHE_MAX_MIB', 2048) * 1024 * 1024,
                       read('CACHE_RECORDS', 128))
