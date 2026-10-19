"""Launch-derived Push file-segment, idle worker reuse, and event-queue bounds.

No connection or byte admission quota is defined here. Existing launch
observations size file reuse; Demand workers start accepted encoding/reads
immediately and retire when idle. Retired Push limit overrides are
intentionally ignored except ``TOFU_PUSH_EVENT_QUEUE_CAPACITY``, which
bounds one reconstructible per-socket event backlog before disconnect.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
import os
from runtime_guards import resolve_resource_budget


@dataclass(frozen=True, slots=True)
class PushBudget:
    spool_segment_bytes: int
    worker_reuse_target: int


def resolve_push_budget(environment: Mapping[str, str] | None = None) -> PushBudget:
    environment = os.environ if environment is None else environment
    registry = resolve_resource_budget('TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
                                      environment, minimum=16, maximum=4096)
    workers = resolve_resource_budget('TOFU_AGENT_WORKERS', environment,
                                     minimum=1, maximum=65536)
    return PushBudget(spool_segment_bytes=registry * 64 * 1024,
                      worker_reuse_target=workers)


def resolve_push_event_queue_capacity(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return one probe-derived bound for a socket's reconstructible backlog.

    An explicit ``TOFU_PUSH_EVENT_QUEUE_CAPACITY`` override is clamped to the
    documented 64..16384 envelope.  Otherwise the budget follows useful local
    task concurrency: 256 queued events per probed agent worker with a floor
    above the historical 1,000-frame burst envelope, so larger hosts absorb
    longer bursts while a stalled socket can never grow without limit.
    Probe failure keeps the lean 8,192 floor.
    """
    environment = os.environ if environment is None else environment
    try:
        raw = str(environment.get('TOFU_PUSH_EVENT_QUEUE_CAPACITY') or '')
        override = int(raw) if raw.strip() else 0
    except (TypeError, ValueError, OverflowError):
        override = 0
    if override > 0:
        return max(64, min(16384, override))
    workers = resolve_resource_budget('TOFU_AGENT_WORKERS', environment,
                                     minimum=1, maximum=65536)
    return max(8192, min(16384, workers * 256))
