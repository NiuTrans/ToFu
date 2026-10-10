"""Bound request route residency from the existing launch resource budgets.

The pool stores candidates, not running model calls. A queued root, a live
child, or an ingress request can each own MAX_REQUEST_ROUTE_SLOTS candidates.
This policy reuses runtime_guards' cached probe and explicit worker overrides;
it never probes the machine independently or evicts a live execution's route.
"""
from __future__ import annotations

from collections.abc import Mapping
import os

from runtime_guards import MAX_TASK_CONCURRENCY, resolve_resource_budget


MAX_REQUEST_ROUTE_SLOTS = 64
_MAX_SLOT_CAPACITY = 1_048_576


def resolve_ephemeral_slot_capacity(environment: Mapping[str, str] | None = None) -> int:
    """Return a finite slot budget, including roots queued before admission."""
    env = os.environ if environment is None else environment
    try:
        override = int(env.get('TOFU_EPHEMERAL_SLOT_CAPACITY', ''))
    except (TypeError, ValueError, OverflowError):
        override = 0
    if override > 0:
        return max(MAX_REQUEST_ROUTE_SLOTS, min(_MAX_SLOT_CAPACITY, override))

    def budget(name: str, maximum: int, minimum: int = 1) -> int:
        return resolve_resource_budget(name, env, minimum=minimum, maximum=maximum)

    workers = budget('TOFU_AGENT_WORKERS', MAX_TASK_CONCURRENCY)
    roots = max(workers, budget('TOFU_MAX_INFLIGHT_TASKS', MAX_TASK_CONCURRENCY))
    # The serving executor admits eight queued roots per worker (8..512),
    # or an explicit finite queue up to 4096. These own routes before entry.
    queued = min(512, max(8, workers * 8))
    try:
        configured_queue = int(env.get('TOFU_AGENT_QUEUE_CAPACITY', ''))
    except (TypeError, ValueError, OverflowError):
        configured_queue = 0
    if configured_queue > 0:
        queued = min(4096, configured_queue)
    # Leave one execution per worker for replacement/settlement overlap;
    # the executor's actual stuck-replacement budget is smaller.
    children = budget('TOFU_SWARM_GLOBAL_WORKERS', 64, minimum=0)
    if not children:
        children = (budget('TOFU_SWARM_MAX_PARALLEL', 64)
                    * budget('TOFU_SWARM_SESSION_CAPACITY', 128))
    ingress = budget('TOFU_SYNC_WORKERS', 512)
    return min(_MAX_SLOT_CAPACITY, MAX_REQUEST_ROUTE_SLOTS * (
        roots + workers + queued + children + ingress))
