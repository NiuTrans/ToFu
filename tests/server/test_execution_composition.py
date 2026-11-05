"""Managed application services prepare before drain and join in dependency order."""
import pytest

from tests.server import test_server_manager as existing

pytestmark = pytest.mark.unit
_scrub_ambient_server_env = existing._scrub_ambient_server_env


@pytest.fixture
def services(tmp_path, monkeypatch):
    project = existing._project(tmp_path)
    (project / 'contracts').mkdir()
    (project / 'contracts/worker_job_lifecycle_v1.json').write_text('{}')
    manager = existing.sm.LifecycleManager(str(project))
    calls = []
    monkeypatch.setattr(manager._native_storage, 'prepare',
                        lambda env: calls.append(('prepare-storage', dict(env))) or {'storage': 1})
    monkeypatch.setattr(manager._execution, 'prepare',
                        lambda env: calls.append(('prepare-execution', dict(env))) or {'execution': 1})
    monkeypatch.setattr(manager._native_storage, 'needs_replacement', lambda *args, **kwargs: False)
    monkeypatch.setattr(manager._execution, 'needs_replacement', lambda *args, **kwargs: False)
    monkeypatch.setattr(manager._native_storage, 'ensure',
                        lambda plan, env: calls.append(('ensure-storage', dict(env))) or str(project / 'connection.json'))
    monkeypatch.setattr(manager._execution, 'ensure',
                        lambda plan, env: calls.append(('ensure-execution', dict(env))) or '')
    monkeypatch.setattr(manager._native_storage, 'stop', lambda: calls.append(('stop-storage', {})))
    monkeypatch.setattr(manager._execution, 'stop', lambda: calls.append(('stop-execution', {})))
    return manager, calls


def test_default_preparation_pins_shared_endpoint_and_preserves_backend(services):
    manager, calls = services
    environment, storage, execution = manager._prepare_resident_services({
        'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb', 'TOFU_PROCESS_ROLE': 'all'})
    assert [name for name, _ in calls] == ['prepare-storage', 'prepare-execution']
    assert environment['TOFU_PERSONAL_STORAGE_BACKEND'] == 'tofudb'
    assert environment['TOFUDB_TRANSPORT'] == 'daemon'
    assert calls[1][1]['TOFU_STORAGE_CONNECTION_FILE'] == manager._native_storage.result_path()
    api = manager._ensure_resident_services(environment, storage, execution)
    assert api['TOFU_PROCESS_ROLE'] == 'api'
    assert calls[-1][1]['TOFU_STORAGE_CONNECTION_FILE'].endswith('connection.json')
    assert [name for name, _ in calls][-2:] == ['ensure-storage', 'ensure-execution']


@pytest.mark.parametrize('environment', [
    {'TOFUDB_TRANSPORT': 'embedded'}, {'TOFU_PROCESS_ROLE': 'api'},
    {'TOFU_PROCESS_ROLE': 'worker'}, {'TOFU_DEPLOYMENT_MODE': 'distributed'},
])
def test_explicit_topology_keeps_its_lifecycle_owner(services, environment):
    manager, calls = services
    prepared, storage, execution = manager._prepare_resident_services(environment)
    assert prepared == environment
    assert execution is None
    assert [name for name, _ in calls] == ['prepare-storage']


def test_storage_upgrade_joins_execution_before_releasing_storage(services, monkeypatch):
    manager, calls = services
    monkeypatch.setattr(manager._native_storage, 'needs_replacement', lambda *args, **kwargs: True)
    manager._ensure_resident_services({}, {'storage': 2}, {'execution': 2})
    assert [name for name, _ in calls] == [
        'stop-execution', 'stop-storage', 'ensure-storage', 'ensure-execution']


def test_execution_upgrade_keeps_storage_and_full_stop_joins_both(services, monkeypatch):
    manager, calls = services
    monkeypatch.setattr(manager._execution, 'needs_replacement', lambda *args, **kwargs: True)
    manager._ensure_resident_services({}, {'storage': 1}, {'execution': 2})
    assert [name for name, _ in calls] == ['stop-execution', 'ensure-storage', 'ensure-execution']
    calls.clear()
    manager._stop_resident_services()
    assert [name for name, _ in calls] == ['stop-execution', 'stop-storage']


def test_missing_shared_authority_refuses_before_any_stop(services, monkeypatch):
    manager, calls = services
    monkeypatch.setattr(manager._native_storage, 'prepare', lambda env: None)
    with pytest.raises(RuntimeError, match='shared storage'):
        manager._prepare_resident_services({})
    assert calls == []


@pytest.fixture
def departed_execution(services, monkeypatch):
    manager, calls = services
    record = {'serviceId': 'first', 'state': 'ready', 'plan': {'execution': 1}}
    monkeypatch.setattr(manager._execution, 'record', lambda: dict(record))
    monkeypatch.setattr(manager._execution, 'host_matches', lambda value: value.get('alive', False))
    monkeypatch.setattr(manager, '_worker_environment', lambda *args: {})
    manager._state['worker'] = {'pid': 987654, 'spawnedAt': 42}
    monkeypatch.setattr(existing.sm, '_now', lambda: 1000.0)
    return manager, calls, record


def test_departed_executor_is_charged_once_without_retiring_api(departed_execution):
    manager, calls, _ = departed_execution
    before = dict(manager._state['worker'])
    assert not manager._recover_execution_service()
    assert not manager._recover_execution_service()
    assert manager._state['worker'] == before
    assert manager._state['failureHistory'] == [1000.0]
    assert calls == []


def test_due_execution_recovery_preserves_storage_and_api(departed_execution, monkeypatch):
    manager, calls, _ = departed_execution
    assert not manager._recover_execution_service()
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    assert not manager._recover_execution_service()
    assert [name for name, _ in calls] == [
        'prepare-execution', 'stop-execution', 'ensure-execution']
    assert manager._state['worker']['pid'] == 987654
    assert calls[-1][1]['TOFU_STORAGE_CONNECTION_FILE'] == manager._native_storage.result_path()


def test_repeated_executor_generations_share_the_existing_crash_budget(departed_execution, monkeypatch):
    manager, calls, record = departed_execution
    manager.max_failures = 2
    assert not manager._recover_execution_service()
    record['serviceId'] = 'second'
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    assert not manager._recover_execution_service()
    assert manager._state['observed'] == 'crashloop'
    monkeypatch.setattr(existing.sm, '_now', lambda: 2000.0)
    assert not manager._recover_execution_service()
    assert len(manager._state['failureHistory']) == 2
    assert calls == []


def test_recovery_refuses_changed_code_until_explicit_restart(departed_execution, monkeypatch):
    manager, calls, _ = departed_execution
    assert not manager._recover_execution_service()
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    monkeypatch.setattr(manager._execution, 'prepare', lambda env: {'execution': 2})
    assert not manager._recover_execution_service()
    assert 'changed' in manager._state['lastError']
    assert manager._state['executionRecovery']['requiresRestart']
    assert calls == []


def test_live_executor_boot_does_not_rehash_or_clear_failure_history(departed_execution):
    manager, calls, record = departed_execution
    manager._state['failureHistory'] = [999.0]
    record.update(alive=True, state='starting')
    assert not manager._recover_execution_service()
    assert calls == []
    record['state'] = 'ready'
    assert manager._recover_execution_service()
    assert manager._state['failureHistory'] == [999.0]


def test_api_readiness_cannot_hide_missing_executor(departed_execution, monkeypatch):
    manager, _, _ = departed_execution
    manager._state['desired'] = 'running'
    monkeypatch.setattr(existing.sm, 'read_lock_status', lambda *args: {'running': True, 'pid': 987654})
    monkeypatch.setattr(manager, '_identity_error', lambda *args: None)
    monkeypatch.setattr(manager, '_port_conflict', lambda *args: (False, None))
    monkeypatch.setattr(manager, '_http_probe', lambda *args: {'liveness': True, 'ready': True})
    snapshot = manager.status(probe_health=True)
    assert snapshot['liveness'] is True
    assert snapshot['ready'] is False
    assert snapshot['observed'] == 'degraded'
    assert 'execution' in snapshot['lastError']
