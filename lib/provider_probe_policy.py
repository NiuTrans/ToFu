"""Resource policy for server-owned provider capability probes.

Responsibility
--------------
Derive one finite process budget from the launch-time read-only tool budget.
The probe engine owns paid/network calls, while routes own request validation
and snapshots.  Keeping the arithmetic here prevents route, worker, and test
defaults from drifting apart.

Entry point
-----------
``resolve_provider_probe_budget`` returns task workers, pending task capacity,
per-task cell workers, and idle retirement.  Total live cell workers never
exceed the shared ``TOOL_MAX_PARALLEL_WORKERS`` envelope (hard-capped at 8 for
this optional diagnostic capability).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from runtime_guards import resolve_resource_budget


# Bound request construction without making the provider catalogue itself a
# validity constraint.  The matrix is an explicitly requested diagnostic whose
# live call concurrency is already capped below; 400 made an ordinary 474-row,
# two-key catalogue fail before doing any work (1,182 routed cells).  Keep a
# finite, independently testable ceiling large enough for current catalogues.
PROVIDER_PROBE_MAX_CELLS = 4_096
PROVIDER_PROBE_MIN_TIMEOUT_SECONDS = 2
PROVIDER_PROBE_MAX_TIMEOUT_SECONDS = 120
PROVIDER_PROBE_MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class ProviderProbeBudget:
    """Finite process residency and call-concurrency envelope."""

    task_workers: int
    queue_capacity: int
    cell_workers_per_task: int
    total_cell_workers: int
    idle_seconds: int


def resolve_provider_probe_budget(
    environment: Mapping[str, str] | None = None,
) -> ProviderProbeBudget:
    """Derive a balanced probe lane without introducing a second host probe."""
    total_cell_workers = min(8, resolve_resource_budget(
        'TOOL_MAX_PARALLEL_WORKERS',
        environment,
        minimum=1,
        maximum=32,
    ))
    # Personal profiles run one provider at a time.  The distributed profile
    # reaches eight shared workers and may overlap two providers fairly while
    # preserving the same aggregate call ceiling.
    task_workers = 2 if total_cell_workers >= 8 else 1
    cell_workers_per_task = max(1, total_cell_workers // task_workers)
    queue_capacity = max(4, min(32, total_cell_workers * 2))
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
    'PROVIDER_PROBE_MAX_CELLS',
    'PROVIDER_PROBE_MAX_TIMEOUT_SECONDS',
    'PROVIDER_PROBE_MIN_TIMEOUT_SECONDS',
    'ProviderProbeBudget',
    'resolve_provider_probe_budget',
]
