"""Resource and upstream-attempt policy for outbound webhook delivery.

Responsibility
--------------
Derive one finite subscription, resident delivery, byte, and retry envelope
from the launch-probed client-registry budget.  The route owns validation and
the worker owns delivery; neither may invent a second default or treat an item
count as protection against an arbitrarily large event.

Entry point
-----------
``resolve_webhook_budget`` returns the complete process envelope.  Dedicated
operator overrides are optional and hard-clamped; their defaults reuse the
single launch-time resource observation already installed by
``runtime_guards``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from runtime_guards import resolve_resource_budget


_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class WebhookBudget:
    """Finite process residency and outbound-attempt envelope."""

    subscription_capacity: int
    owner_subscription_capacity: int
    queue_capacity: int
    retry_capacity: int
    queue_byte_capacity: int
    retry_byte_capacity: int
    event_max_bytes: int
    max_attempts: int
    subscription_cache_seconds: float


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


def resolve_webhook_budget(
    environment: Mapping[str, str] | None = None,
) -> WebhookBudget:
    """Resolve bounded webhook state without performing another host probe."""
    resolved_environment = os.environ if environment is None else environment
    registry_capacity = resolve_resource_budget(
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
        resolved_environment,
        minimum=16,
        maximum=4_096,
    )
    subscription_capacity = _bounded_override(
        resolved_environment,
        'TOFU_WEBHOOK_SUBSCRIPTION_CAPACITY',
        min(2_048, registry_capacity),
        minimum=1,
        maximum=4_096,
    )
    queue_capacity = _bounded_override(
        resolved_environment,
        'TOFU_WEBHOOK_QUEUE_CAPACITY',
        max(64, min(2_048, registry_capacity * 2)),
        minimum=16,
        maximum=4_096,
    )
    buffer_mib = _bounded_override(
        resolved_environment,
        'TOFU_WEBHOOK_BUFFER_MAX_MIB',
        max(16, min(256, registry_capacity // 4)),
        minimum=2,
        maximum=512,
    )
    total_buffer_bytes = buffer_mib * _MIB
    requested_event_kib = _bounded_override(
        resolved_environment,
        'TOFU_WEBHOOK_EVENT_MAX_KIB',
        1_024 if registry_capacity > 256 else 512,
        minimum=64,
        maximum=4_096,
    )
    # Both the immediate queue and delayed heap must be able to admit one
    # maximum event while their combined allocation stays within the buffer.
    event_max_bytes = min(
        requested_event_kib * 1_024,
        total_buffer_bytes // 2,
    )
    retry_byte_capacity = max(
        event_max_bytes,
        total_buffer_bytes // 3,
    )
    queue_byte_capacity = total_buffer_bytes - retry_byte_capacity
    max_attempts = _bounded_override(
        resolved_environment,
        'TOFU_WEBHOOK_MAX_ATTEMPTS',
        5,
        minimum=1,
        maximum=8,
    )
    return WebhookBudget(
        subscription_capacity=subscription_capacity,
        owner_subscription_capacity=min(256, subscription_capacity),
        queue_capacity=queue_capacity,
        retry_capacity=max(16, min(2_048, queue_capacity // 2)),
        queue_byte_capacity=queue_byte_capacity,
        retry_byte_capacity=retry_byte_capacity,
        event_max_bytes=event_max_bytes,
        max_attempts=max_attempts,
        subscription_cache_seconds=1.0,
    )


__all__ = ['WebhookBudget', 'resolve_webhook_budget']
