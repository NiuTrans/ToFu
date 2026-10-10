"""Launch-derived idle reuse and retirement for demand-created probe calls.

Task/cell worker values control idle reuse only. Queue capacity and total active
cell quota are zero (unlimited); timeout/attempt validation remains separate.
Uses the existing launch resource policy, never a second machine probe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from runtime_guards import resolve_resource_budget


PROVIDER_PROBE_MIN_TIMEOUT_SECONDS = 2
PROVIDER_PROBE_MAX_TIMEOUT_SECONDS = 120
PROVIDER_PROBE_MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class ProviderProbeBudget:
    """Idle targets plus explicit absence of task/cell count admission."""

    task_workers: int
    queue_capacity: int
    cell_workers_per_task: int
    total_cell_workers: int
    idle_seconds: int


def resolve_provider_probe_budget(
    environment: Mapping[str, str] | None = None,
) -> ProviderProbeBudget:
    """Derive idle reuse targets without introducing a second host probe."""
    idle_workers = resolve_resource_budget(
        'TOOL_MAX_PARALLEL_WORKERS', environment, minimum=1, maximum=2**31 - 1)
    task_workers = idle_workers
    cell_workers_per_task = idle_workers
    queue_capacity = 0
    total_cell_workers = 0
    resolved_environment = os.environ if environment is None else environment
    if str(resolved_environment.get(
            'TOFU_PROJECT_REFRESH_IDLE_SECONDS', '')).strip() == '0':
        idle_seconds = 0
    else:
        idle_seconds = resolve_resource_budget(
            'TOFU_PROJECT_REFRESH_IDLE_SECONDS',
            environment,
            minimum=15,
            maximum=3_600,
        )
    return ProviderProbeBudget(
        task_workers=task_workers,
        queue_capacity=queue_capacity,
        cell_workers_per_task=cell_workers_per_task,
        total_cell_workers=total_cell_workers,
        idle_seconds=idle_seconds,
    )


__all__ = [
    'PROVIDER_PROBE_MAX_ATTEMPTS',
    'PROVIDER_PROBE_MAX_TIMEOUT_SECONDS',
    'PROVIDER_PROBE_MIN_TIMEOUT_SECONDS',
    'ProviderProbeBudget',
    'resolve_provider_probe_budget',
]
