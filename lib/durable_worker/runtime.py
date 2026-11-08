"""One application-owned service for all qualified durable invocation kinds.

Entry points: create_service, ensure_workers_started, stop_workers. Composition
is lazy; one authority wake source serves chat and automation kinds. Domain
registrations retain effects and accounting, and shutdown joins before storage.
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
                   now_ms=None, environ=None, application=None):
    from runtime_guards import UNLIMITED, resolve_resource_budget
    from lib.automations.worker_registration import registrations
    from lib.conversation_sync.worker_execution import conversation_registration
    from lib.durable_worker.service import DurableWorkerService
    from lib.storage.worker_wake import StorageWorkerWakeSource
    policy = runtime_policy()
    if max_workers is None:
        max_workers = resolve_resource_budget(policy['capacityResource'], environ, maximum=UNLIMITED)
    if now_ms is None:
        now_ms = lambda: time.time_ns() // 1_000_000
    kinds = (*registrations(client_factory=client_factory, registry=registry,
                            now_ms=now_ms, policy=policy),
             conversation_registration(application=application))
    control = PrincipalContext.system(subject_id='durable-worker:' + uuid.uuid4().hex,
                                      scopes={policy['controlScope']})
    return DurableWorkerService(principal=control, client_factory=client_factory,
        registrations=kinds, max_workers=max_workers, now_ms=now_ms, policy=policy,
        wake_source=StorageWorkerWakeSource(client_factory, policy))


def ensure_workers_started(app, *, environ=None):
    key = runtime_policy()['extensionKey']
    with _composition_lock:
        service = app.extensions.get(key)
        if service is None or service.snapshot()['state'] in {'stopped', 'failed'}:
            service = create_service(environ=environ, application=app)
            app.extensions[key] = service
        return service.start()


def stop_workers(app):
    with _composition_lock:
        service = app.extensions.get(runtime_policy()['extensionKey'])
    if service is not None:
        service.stop()
