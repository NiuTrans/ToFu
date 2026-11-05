"""Automation kind registrations for the shared application worker service.

This domain retains clock/event/run semantics and explicit action ports; the
durable_worker runtime owns process composition, one wake source and shutdown.
"""


def registrations(*, client_factory, registry, now_ms, policy):
    from .clock import AutomationClockHandler
    from .event_delivery import AutomationEventHandler
    from .execution import AutomationExecutionHandler
    from lib.project_dispatch.responsibilities_adapter import responsibility_registry
    if registry is None:
        registry = responsibility_registry(client_factory=client_factory, now_ms=now_ms)
    execution = AutomationExecutionHandler(client_factory=client_factory, registry=registry,
        now_ms=now_ms, poll_interval_ms=policy['actionWaitMilliseconds'])
    return (
        AutomationClockHandler(client_factory=client_factory, now_ms=now_ms).registration(),
        AutomationEventHandler(client_factory=client_factory, now_ms=now_ms).registration(),
        execution.registration(),
    )
