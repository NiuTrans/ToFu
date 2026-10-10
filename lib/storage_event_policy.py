"""Launch-derived residency and frame budgets for durable event batching.

Responsibility
--------------
Resolve application-side event residency hints and RPC composition bounds.
The policy reuses the launch-probed SQLite writer queue capacity; it performs
no second host probe and never changes event durability or ordering.

Entry point
-----------
``resolve_storage_event_budget`` returns batcher RAM/file representation thresholds, and the per-RPC composition bound
required by the 64 MiB storage.v1 frame. StorageEventBatcher never treats its
count or RAM hints as permission to refuse an event.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from runtime_guards import resolve_resource_budget


_MIB = 1024 * 1024
STORAGE_EVENT_BATCH_MAX_EVENTS = 500
STORAGE_EVENT_BATCH_FRAME_HARD_MIB = 60
STORAGE_EVENT_FRAME_HEADROOM_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class StorageEventBudget:
    """Residency hints and the independent active-frame envelope."""

    queue_capacity: int  # Retired count-admission readback, always zero.
    queue_byte_capacity: int
    batch_max_events: int
    batch_max_bytes: int
    event_max_bytes: int


def _bounded_override(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(str(environment.get(name, '') or default))
    except (TypeError, ValueError, OverflowError):
        value = default
    if value <= 0:
        value = default
    return max(minimum, min(maximum, value))


def resolve_storage_event_budget(
    environment: Mapping[str, str] | None = None,
) -> StorageEventBudget:
    """Resolve RAM representation and active-frame sizing from the launch writer hint."""
    resolved_environment = os.environ if environment is None else environment
    writer_queue_capacity = resolve_resource_budget(
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY',
        resolved_environment,
        minimum=4,
        maximum=1_024,
    )
    queue_mib = _bounded_override(
        resolved_environment,
        'TOFU_STORAGE_EVENT_QUEUE_MAX_MIB',
        max(64, min(512, writer_queue_capacity * 4)),
        minimum=64,
        maximum=1_024,
    )
    batch_mib = _bounded_override(
        resolved_environment,
        'TOFU_STORAGE_EVENT_BATCH_MAX_MIB',
        STORAGE_EVENT_BATCH_FRAME_HARD_MIB,
        minimum=1,
        maximum=STORAGE_EVENT_BATCH_FRAME_HARD_MIB,
    )
    batch_max_bytes = min(batch_mib, queue_mib) * _MIB
    return StorageEventBudget(
        queue_capacity=0,
        queue_byte_capacity=queue_mib * _MIB,
        batch_max_events=STORAGE_EVENT_BATCH_MAX_EVENTS,
        batch_max_bytes=batch_max_bytes,
        event_max_bytes=max(
            1,
            batch_max_bytes - STORAGE_EVENT_FRAME_HEADROOM_BYTES,
        ),
    )


__all__ = [
    'STORAGE_EVENT_BATCH_FRAME_HARD_MIB',
    'STORAGE_EVENT_BATCH_MAX_EVENTS',
    'STORAGE_EVENT_FRAME_HEADROOM_BYTES',
    'StorageEventBudget',
    'resolve_storage_event_budget',
]
