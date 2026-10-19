"""Launch-derived idle reuse and reconstructible artifact reclamation targets."""
from __future__ import annotations
from .contract import limit as limit

import os
from dataclasses import dataclass
from collections.abc import Mapping

from runtime_guards import resolve_resource_budget


def compile_memory_bytes(environment: Mapping[str, str] | None = None) -> int:
    """Optional explicit operator RSS ceiling; zero lets actual demand run."""
    values = os.environ if environment is None else environment
    raw = values.get('TOFU_PAPER_BUILD_COMPILE_MAX_MIB')
    if raw is None or str(raw).strip() == '':
        return 0
    try:
        mib = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('TOFU_PAPER_BUILD_COMPILE_MAX_MIB must be a nonnegative integer') from exc
    if mib < 0:
        raise ValueError('TOFU_PAPER_BUILD_COMPILE_MAX_MIB must be a nonnegative integer')
    return mib * 1024 * 1024


def validation_memory_bytes(environment: Mapping[str, str] | None = None) -> int:
    """Optional explicit operator memory ceiling; zero follows actual demand."""
    values = os.environ if environment is None else environment
    raw = values.get('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB')
    if raw is None or str(raw).strip() == '':
        return 0
    try:
        mib = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB must be a nonnegative integer') from exc
    if mib < 0:
        raise ValueError('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB must be a nonnegative integer')
    return mib * 1024 * 1024



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
