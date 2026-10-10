"""Canonical scheduler execution-claim policy.

The manager and storage authorities share due-claim fencing. Keeping
them here prevents runtime documentation and durable admission logic from
drifting into separate policies.
"""

DUE_CLAIM_INTERVAL_SECONDS = 55


def timer_live_capacity() -> int:
    """Retired compatibility observation: zero means no live admission quota."""
    return 0


__all__ = [
    "DUE_CLAIM_INTERVAL_SECONDS",
    "timer_live_capacity",
]
