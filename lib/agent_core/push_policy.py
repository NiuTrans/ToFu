"""Push file-segment and idle worker reuse hints and the per-socket event bound.

No connection or byte admission quota is defined here. Existing launch
observations size file reuse; Demand workers start accepted encoding/reads
immediately and retire when idle. Frame jobs are short encode/file I/O, so
their idle reuse follows the fixed short blocking-job hint
(``TOFU_SYNC_WORKERS``) rather than agent-task concurrency. Retired Push limit overrides are
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
    workers = resolve_resource_budget('TOFU_SYNC_WORKERS', environment,
                                     minimum=1, maximum=512)
    return PushBudget(spool_segment_bytes=registry * 64 * 1024,
                      worker_reuse_target=workers)


def resolve_push_event_queue_capacity(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return the bound for one socket's reconstructible event backlog.

    An explicit ``TOFU_PUSH_EVENT_QUEUE_CAPACITY`` override is clamped to the
    documented 64..16384 envelope. The default is the envelope maximum: host
    size and worker hints never shrink it, and a stalled socket still cannot
    grow without limit.
    """
    environment = os.environ if environment is None else environment
    try:
        raw = str(environment.get('TOFU_PUSH_EVENT_QUEUE_CAPACITY') or '')
        override = int(raw) if raw.strip() else 0
    except (TypeError, ValueError, OverflowError):
        override = 0
    if override > 0:
        return max(64, min(16384, override))
    return 16384
