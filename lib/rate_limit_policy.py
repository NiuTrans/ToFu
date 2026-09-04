"""Launch-probed resident budgets for process-local rate-limit state.

Entry points
------------
``rate_limit_memory_bucket_capacity`` bounds retained endpoint/client pairs.
``rate_limit_memory_event_capacity`` derives the exact-timestamp envelope from
that single operator knob. Counter semantics remain in ``rate_limit_store``.
"""

from __future__ import annotations

from runtime_guards import resolve_resource_budget


RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY = 16_384
RATE_LIMIT_MEMORY_EVENT_HARD_CAPACITY = 1_048_576
RATE_LIMIT_MEMORY_EVENTS_PER_BUCKET_BUDGET = 128


def rate_limit_memory_bucket_capacity() -> int:
    """Return the finite number of resident endpoint/client buckets."""
    return resolve_resource_budget(
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY',
        minimum=64,
        maximum=RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY,
    )


def rate_limit_memory_event_capacity(
    bucket_capacity: int | None = None,
) -> int:
    """Return the derived process-wide exact-timestamp capacity."""
    resolved_buckets = (
        rate_limit_memory_bucket_capacity()
        if bucket_capacity is None else max(1, int(bucket_capacity))
    )
    return min(
        RATE_LIMIT_MEMORY_EVENT_HARD_CAPACITY,
        resolved_buckets * RATE_LIMIT_MEMORY_EVENTS_PER_BUCKET_BUDGET,
    )


__all__ = [
    'RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY',
    'RATE_LIMIT_MEMORY_EVENT_HARD_CAPACITY',
    'RATE_LIMIT_MEMORY_EVENTS_PER_BUCKET_BUDGET',
    'rate_limit_memory_bucket_capacity',
    'rate_limit_memory_event_capacity',
]
