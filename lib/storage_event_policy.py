"""Launch-derived residency and frame budgets for durable event batching.

Responsibility
--------------
Bound application-side durable event objects before the sole Sidecar writer.
The policy reuses the launch-probed SQLite writer queue capacity; it performs
no second host probe and never changes event durability or ordering.

Entry point
-----------
``resolve_storage_event_budget`` returns pending item/byte ceilings plus the
smaller per-RPC batch ceiling required by the 64 MiB storage.v1 frame.
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
    """Finite pending-object and active-frame envelope."""

    queue_capacity: int
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
    """Resolve one event lane from the existing writer waiting-job budget."""
    resolved_environment = os.environ if environment is None else environment
    writer_queue_capacity = resolve_resource_budget(
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY',
        resolved_environment,
        minimum=4,
        maximum=1_024,
    )
    queue_capacity = _bounded_override(
        resolved_environment,
        'TOFU_STORAGE_EVENT_QUEUE_CAPACITY',
        max(128, min(4_096, writer_queue_capacity * 32)),
        minimum=32,
        maximum=8_192,
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
        queue_capacity=queue_capacity,
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
