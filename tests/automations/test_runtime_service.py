"""Resource admission, control identity, and application lifecycle ownership."""
from types import SimpleNamespace
import pytest

from lib.automations.runtime import create_service
from lib.automations.runtime_policy import runtime_policy
from lib.automations.worker_service import AutomationWorkerService
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit


def test_runtime_contract_uses_launch_capacity_without_storage_or_thread_work():
    def forbidden(**kwargs):
        raise AssertionError('Composition opened storage before startup')
    service = create_service(client_factory=forbidden, environ={'TOFU_AGENT_WORKERS': '97'})
    assert service.snapshot() == {'state': 'new', 'capacity': 97, 'inflight': 0, 'highWater': 0}
    policy = runtime_policy()
    assert policy['heartbeatIntervalMilliseconds'] < policy['workerLeaseMilliseconds']
    assert policy['queueReceipts'] == 'no-idle-claim-receipts'
    assert service.stop()
    assert service.snapshot()['state'] == 'stopped'
    with pytest.raises(RuntimeError):
        service.start()


@pytest.mark.parametrize('principal', [
    PrincipalContext.user(subject_id='human', owner_user_id=41, scopes={'automations:work'}),
    PrincipalContext.system(subject_id='bound', owner_user_id=41, scopes={'automations:work'}),
    PrincipalContext.system(subject_id='admin', scopes={'admin'}),
    PrincipalContext.system(subject_id='tenant', tenant_id='foreign', scopes={'automations:work'}),
])
def test_worker_control_cannot_inherit_an_owner_or_ambient_caller(principal):
    with pytest.raises(PermissionError):
        AutomationWorkerService(principal=principal, client_factory=lambda **_: None,
            registrations=[], max_workers=2, now_ms=lambda: 1)


def test_application_installs_once_and_keeps_its_shutdown_owner(monkeypatch):
    from lib.automations import runtime
    calls = []
    class Service:
        running = False
        def snapshot(self):
            return {'state': 'running' if self.running else 'stopped'}
        def start(self):
            if self.running:
                return False
            self.running = True; calls.append('start'); return True
        def stop(self):
            self.running = False; calls.append('stop')
    instance = Service()
    monkeypatch.setattr(runtime, 'create_service', lambda **_: calls.append('create') or instance)
    app = SimpleNamespace(extensions={})
    assert runtime.ensure_workers_started(app)
    assert not runtime.ensure_workers_started(app)
    runtime.stop_workers(app)
    assert calls == ['create', 'start', 'stop']
    assert runtime.ensure_workers_started(app)
    runtime.stop_workers(app)
    assert calls == ['create', 'start', 'stop', 'create', 'start', 'stop']


@pytest.mark.parametrize('role,expected', [('all', True), ('worker', True), ('api', False), ('scheduler', False)])
def test_process_role_limits_worker_startup(monkeypatch, role, expected):
    from lib.automations import runtime
    from lib.server_background_services import start_background_services
    import lib.integration_control as integration
    import routes
    calls = []
    monkeypatch.setattr(runtime, 'ensure_workers_started', lambda *_, **__: calls.append('automation') or True)
    monkeypatch.setattr(integration, 'ensure_worker_started', lambda: False)
    monkeypatch.setattr(routes, 'start_registered_background_services', lambda *_, **__: None)
    start_background_services(SimpleNamespace(extensions={}), process_role=role,
        load_saved_proxy_config=lambda: None, bootstrap_personal_key=lambda: None,
        environ={'TOFU_AGENT_WORKERS': '2'})
    assert bool(calls) is expected
