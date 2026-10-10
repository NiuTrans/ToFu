"""Launch-derived Push file-segment and idle worker reuse hints.

No connection, event-count, byte or frame admission quota is defined here.
Existing launch observations size file reuse; Demand workers start accepted
encoding/reads immediately and retire when idle. Retired Push limit overrides
are intentionally ignored.
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
