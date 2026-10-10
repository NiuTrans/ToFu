"""Launch-derived residency policy for in-process task replay.

Responsibility
--------------
Bound each :class:`TaskRuntime` terminal-record target and each task's
reconstructible event tail by both event count and serialized bytes. Defaults
reuse the launch-time browser-registry and active-task observations from
:mod:`runtime_guards`; this module performs no second host probe. Operator
overrides remain finite and hard-clamped. Productive active tasks remain under
their separate admission/lifecycle authority and are never evicted here.

Entry point
-----------
``resolve_task_runtime_retention_budget`` returns the complete per-runtime and
per-task retention envelope. ``resolve_chat_task_terminal_ttl_seconds`` owns
the hot chat terminal-record lifetime now that durable cold replay exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

from runtime_guards import resolve_resource_budget


_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TaskRuntimeRetentionBudget:
    """Finite registry and per-task replay residency envelope."""

    task_capacity: int
    event_capacity: int
    replay_byte_capacity: int
    event_max_bytes: int

    @property
    def replay_hard_capacity(self) -> int:
        """Maximum bytes one task can retain, including one large event."""
        return max(self.replay_byte_capacity, self.event_max_bytes)


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


def resolve_task_runtime_retention_budget(
    environment: Mapping[str, str] | None = None,
) -> TaskRuntimeRetentionBudget:
    """Resolve task replay limits from the existing launch-time profile."""
    resolved_environment = os.environ if environment is None else environment
    registry_capacity = resolve_resource_budget(
        "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY",
        resolved_environment,
        minimum=16,
        maximum=4_096,
    )
    active_task_capacity = resolve_resource_budget(
        "TOFU_MAX_INFLIGHT_TASKS",
        resolved_environment,
        minimum=1,
        maximum=256,
    )
    task_capacity = _bounded_override(
        resolved_environment,
        "TOFU_TASK_RUNTIME_TASK_CAPACITY",
        max(32, min(512, registry_capacity)),
        minimum=8,
        maximum=1_024,
    )
    event_capacity = _bounded_override(
        resolved_environment,
        "TOFU_TASK_RUNTIME_EVENT_CAPACITY",
        max(512, min(4_096, registry_capacity * 16)),
        minimum=64,
        maximum=8_192,
    )
    replay_capacity_mib = _bounded_override(
        resolved_environment,
        "TOFU_TASK_RUNTIME_REPLAY_MAX_MIB",
        max(2, min(8, active_task_capacity)),
        minimum=1,
        maximum=16,
    )
    event_max_mib = _bounded_override(
        resolved_environment,
        "TOFU_TASK_RUNTIME_EVENT_MAX_MIB",
        max(4, min(16, replay_capacity_mib * 2)),
        minimum=1,
        maximum=16,
    )
    return TaskRuntimeRetentionBudget(
        task_capacity=task_capacity,
        event_capacity=event_capacity,
        replay_byte_capacity=replay_capacity_mib * _MIB,
        event_max_bytes=event_max_mib * _MIB,
    )


def resolve_chat_task_terminal_ttl_seconds(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Resolve the hot chat terminal-record lifetime from launch policy."""
    resolved_environment = os.environ if environment is None else environment
    return resolve_resource_budget(
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS',
        resolved_environment,
        minimum=60,
        maximum=86_400,
    )


__all__ = [
    "TaskRuntimeRetentionBudget",
    "resolve_chat_task_terminal_ttl_seconds",
    "resolve_task_runtime_retention_budget",
]
