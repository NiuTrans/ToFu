"""Application composition for owned automation worker lifecycle.

Entry points: create_service, ensure_workers_started, stop_workers. The serving
worker role installs three canonical kinds. Importing this module performs no
storage I/O or startup; action/model/CLI ports remain explicit registrations.
"""
import threading
import time
import uuid

from lib.identity import PrincipalContext
from .runtime_policy import runtime_policy

_composition_lock = threading.Lock()


def _storage(*, write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def create_service(*, client_factory=_storage, registry=None, max_workers=None,
                   now_ms=None, environ=None):
    from runtime_guards import MAX_TASK_CONCURRENCY, resolve_resource_budget
    from .clock import AutomationClockHandler
    from .event_delivery import AutomationEventHandler
    from .execution import AutomationExecutionHandler, presentation_registry
    from .worker_service import AutomationWorkerService

    policy = runtime_policy()
    if max_workers is None:
        max_workers = resolve_resource_budget(policy['capacityResource'], environ,
                                              maximum=MAX_TASK_CONCURRENCY)
    if now_ms is None:
        now_ms = lambda: int(time.time() * 1000)
    if registry is None:
        registry = presentation_registry()
    execution = AutomationExecutionHandler(client_factory=client_factory, registry=registry,
        now_ms=now_ms, poll_interval_ms=policy['actionWaitMilliseconds'])
    registrations = (
        AutomationClockHandler(client_factory=client_factory, now_ms=now_ms).registration(),
        AutomationEventHandler(client_factory=client_factory, now_ms=now_ms).registration(),
        execution.registration(),
    )
    control = PrincipalContext.system(subject_id='automation-worker:' + uuid.uuid4().hex,
                                      scopes={policy['controlScope']})
    return AutomationWorkerService(principal=control, client_factory=client_factory,
        registrations=registrations, max_workers=max_workers, now_ms=now_ms)


def ensure_workers_started(app, *, environ=None):
    key = runtime_policy()['extensionKey']
    with _composition_lock:
        service = app.extensions.get(key)
        if service is None or service.snapshot()['state'] in {'stopped', 'failed'}:
            service = create_service(environ=environ)
            app.extensions[key] = service
        return service.start()


def stop_workers(app):
    key = runtime_policy()['extensionKey']
    with _composition_lock:
        service = app.extensions.get(key)
    if service is not None:
        # The shutdown owner keeps storage live until all worker contexts exit.
        service.stop()
