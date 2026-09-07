"""Launch-derived residency policy for the unified Push WebSocket.

Responsibility
--------------
Bound process/owner connections and every per-client event backlog by both
items and serialized bytes. Defaults reuse the launch-time browser-registry
and SSE observations from :mod:`runtime_guards`; this module performs no
second host probe. Operator overrides remain finite and hard-clamped.

Entry point
-----------
``resolve_push_budget`` returns the complete local Push residency envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from runtime_guards import resolve_resource_budget


_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PushBudget:
    """Finite process, owner, and per-socket Push residency envelope."""

    client_capacity: int
    owner_client_capacity: int
    event_queue_capacity: int
    event_queue_byte_capacity: int
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
        value = int(str(environment.get(name, "") or default))
    except (TypeError, ValueError, OverflowError):
        value = default
    if value <= 0:
        value = default
    return max(minimum, min(maximum, value))


def resolve_push_budget(
    environment: Mapping[str, str] | None = None,
) -> PushBudget:
    """Resolve Push limits from the existing launch-time resource profile."""
    resolved_environment = os.environ if environment is None else environment
    registry_capacity = resolve_resource_budget(
        "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY",
        resolved_environment,
        minimum=16,
        maximum=4_096,
    )
    live_stream_capacity = resolve_resource_budget(
        "TOFU_MAX_SSE_PER_PRINCIPAL",
        resolved_environment,
        minimum=1,
        maximum=128,
    )
    client_capacity = _bounded_override(
        resolved_environment,
        "TOFU_PUSH_CLIENT_CAPACITY",
        max(16, min(256, registry_capacity // 2)),
        minimum=4,
        maximum=256,
    )
    owner_client_capacity = _bounded_override(
        resolved_environment,
        "TOFU_PUSH_OWNER_CLIENT_CAPACITY",
        min(client_capacity, live_stream_capacity),
        minimum=1,
        maximum=128,
    )
    owner_client_capacity = min(owner_client_capacity, client_capacity)
    event_queue_capacity = _bounded_override(
        resolved_environment,
        "TOFU_PUSH_EVENT_QUEUE_CAPACITY",
        max(256, min(1_000, registry_capacity * 8)),
        minimum=16,
        maximum=4_096,
    )
    event_queue_mib = _bounded_override(
        resolved_environment,
        "TOFU_PUSH_EVENT_QUEUE_MAX_MIB",
        max(4, min(16, registry_capacity // 128)),
        minimum=1,
        maximum=16,
    )
    event_max_mib = _bounded_override(
        resolved_environment,
        "TOFU_PUSH_EVENT_MAX_MIB",
        max(1, event_queue_mib // 2),
        minimum=1,
        maximum=8,
    )
    event_max_mib = min(event_max_mib, event_queue_mib)
    return PushBudget(
        client_capacity=client_capacity,
        owner_client_capacity=owner_client_capacity,
        event_queue_capacity=event_queue_capacity,
        event_queue_byte_capacity=event_queue_mib * _MIB,
        event_max_bytes=event_max_mib * _MIB,
    )


__all__ = ["PushBudget", "resolve_push_budget"]
