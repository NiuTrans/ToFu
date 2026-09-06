"""Shared machine policy for bounded producer/verifier loops.

Verifier loops and generic graph execution have different projections, but
their safety caps and zero-deliverable streak semantics must not drift. This
module is deliberately free of task, graph and persistence I/O.
"""

from __future__ import annotations


# A newly authored Flow loop starts at ten turns. The default executor
# permits two additional turns so the canonical Autopilot graph can use its
# intentional twelve-turn budget. Delivery adapters may explicitly raise the
# executor ceiling for non-Studio workloads.
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_EXECUTOR_MAX_ITERATIONS = 12
# Every authored/runtime loop remains bounded even when a caller provides an
# extreme override. Goal Mode uses a larger default within this shared hard
# ceiling; generic flows keep the lean default above.
MAX_EXECUTOR_MAX_ITERATIONS = 64
MAX_REPLANS = 3
MAX_ZERO_DELIVERABLE_TURNS = 2


def bounded_executor_iterations(
    value: object,
    *,
    default: int = DEFAULT_EXECUTOR_MAX_ITERATIONS,
) -> int:
    """Normalize one loop budget into the process-wide bounded range."""
    try:
        candidate = int(value) if value not in (None, '') else int(default)
    except (TypeError, ValueError):
        candidate = int(default)
    return min(MAX_EXECUTOR_MAX_ITERATIONS, max(1, candidate))


def advance_zero_deliverable_streak(
    current: int,
    *,
    reported: bool,
    state_changing: int,
) -> int:
    """Advance or reset a producer's consecutive zero-deliverable count."""
    if not reported:
        return 0
    return max(0, int(current)) + 1 if state_changing <= 0 else 0


def should_inject_zero_deliverable(streak: int) -> bool:
    """Return whether the shared convergence-nudge threshold was reached."""
    return streak >= MAX_ZERO_DELIVERABLE_TURNS


__all__ = [
    'DEFAULT_MAX_ITERATIONS',
    'DEFAULT_EXECUTOR_MAX_ITERATIONS',
    'MAX_EXECUTOR_MAX_ITERATIONS',
    'MAX_REPLANS',
    'MAX_ZERO_DELIVERABLE_TURNS',
    'advance_zero_deliverable_streak',
    'bounded_executor_iterations',
    'should_inject_zero_deliverable',
]
