"""Canonical scheduler capacity and execution-claim policy.

The manager consumes these values while the Sidecar enforces them. Keeping
them here prevents runtime documentation and durable admission logic from
drifting into separate policies.
"""

MAX_TASKS_PER_OWNER = 100
DUE_CLAIM_INTERVAL_SECONDS = 55


class TimerCapacityError(RuntimeError):
    """The process-wide live Timer Watcher budget is occupied."""


def timer_live_capacity() -> int:
    """Return the launch-probed live watcher cap with a hard ceiling."""
    from runtime_guards import resolve_resource_budget

    return resolve_resource_budget(
        'TOFU_TIMER_LIVE_CAP', maximum=64)


__all__ = [
    "DUE_CLAIM_INTERVAL_SECONDS",
    "MAX_TASKS_PER_OWNER",
    "TimerCapacityError",
    "timer_live_capacity",
]
