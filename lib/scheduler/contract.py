"""Canonical scheduler capacity and execution-claim policy.

The manager consumes these values while the Sidecar enforces them. Keeping
them here prevents runtime documentation and durable admission logic from
drifting into separate policies.
"""

MAX_TASKS_PER_OWNER = 100
DUE_CLAIM_INTERVAL_SECONDS = 55


__all__ = ["DUE_CLAIM_INTERVAL_SECONDS", "MAX_TASKS_PER_OWNER"]
