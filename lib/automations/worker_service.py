"""Automation policy binding for the shared durable worker service.

The common owner retains demand execution, claim horizons and joined shutdown;
this domain supplies its control identity, timings and wake operations.
"""
from lib.durable_worker.service import DurableWorkerService
from .runtime_policy import runtime_policy


class AutomationWorkerService(DurableWorkerService):
    def __init__(self, *, principal, client_factory, registrations, max_workers, now_ms):
        super().__init__(principal=principal, client_factory=client_factory,
            registrations=registrations, max_workers=max_workers, now_ms=now_ms,
            policy=runtime_policy())
