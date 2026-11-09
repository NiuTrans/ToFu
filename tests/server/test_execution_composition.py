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
    monkeypatch.setattr(manager._native_storage, 'stop',
                        lambda **options: calls.append(('stop-storage', options)) or '')
    monkeypatch.setattr(manager._execution, 'stop',
                        lambda **options: calls.append(('stop-execution', options)) or '')
    monkeypatch.setattr(
        manager, '_wait_for_stopped_sidecar_release',
        lambda: calls.append(('wait-storage-lease', {})) or {'held': False})
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
        'stop-execution', 'stop-storage', 'wait-storage-lease',
        'ensure-storage', 'ensure-execution']


def test_storage_replacement_defers_while_the_authority_lease_is_held(
        services, monkeypatch):
    """A departed host's unreapable daemon keeps the lease; do not spawn into it."""
    manager, calls = services
    lease = {'held': True, 'kind': 'storage_authority_sql', 'pid': 4321,
             'holderVerified': True, 'label': 'Tofu-DB'}
    monkeypatch.setattr(manager._native_storage, 'needs_replacement', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        manager, '_wait_for_stopped_sidecar_release',
        lambda: calls.append(('wait-storage-lease', {})) or lease)
    monkeypatch.setattr(manager._native_storage, 'owns_lease', lambda value: False)
    with pytest.raises(existing.sm._StorageLeaseStillHeld, match='still held'):
        manager._ensure_resident_services({}, {'storage': 2}, None)
    assert [name for name, _ in calls] == [
        'stop-execution', 'stop-storage', 'wait-storage-lease']
    assert 'ensure-storage' not in [name for name, _ in calls]


def test_blocked_storage_lease_defers_spawn_without_latching_crashloop(
        services, monkeypatch):
    manager, calls = services
    lease = {'held': True, 'kind': 'storage_authority_sql', 'pid': 4321,
             'holderVerified': True, 'label': 'Tofu-DB'}
    monkeypatch.setattr(
        manager, '_ensure_resident_services',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            existing.sm._StorageLeaseStillHeld(lease)))
    result = manager._spawn(
        'manager-restart',
        prepared_environment={'TOFU_PROJECT_PATH': manager.project},
        prepared_storage_plan={'storage': 2},
        prepared_execution_plan=None,
        reset_failed_storage=True)
    assert result['ok'] is False
    assert result['blockedByStorageLease'] is True
    assert result['waitingForMaintenance'] is False
    assert manager._state['observed'] == 'conflict'
    assert manager._state['storageBlocker']['pid'] == 4321
    assert manager._state['storageBlocker']['resumeSource'] == 'manager-restart'
    assert 'holds the storage lease' in manager._state['lastError']


def test_execution_upgrade_keeps_storage_and_full_stop_joins_both(services, monkeypatch):
    manager, calls = services
    monkeypatch.setattr(manager._execution, 'needs_replacement', lambda *args, **kwargs: True)
    manager._ensure_resident_services({}, {'storage': 1}, {'execution': 2})
    assert [name for name, _ in calls] == ['stop-execution', 'ensure-storage', 'ensure-execution']
    calls.clear()
    manager._stop_resident_services()
    assert [name for name, _ in calls] == ['stop-execution', 'stop-storage']


@pytest.mark.parametrize('explicit', [True, False])
def test_only_an_explicit_replacement_may_retire_a_failed_executor_close(services, monkeypatch, explicit):
    manager, calls = services
    monkeypatch.setattr(manager._execution, 'needs_replacement', lambda *args, **kwargs: True)
    manager._ensure_resident_services({}, {'storage': 1}, {'execution': 2}, reset_failed=explicit)
    assert calls[0] == ('stop-execution', {'retire_failed_close': explicit})
    assert [name for name, _ in calls] == ['stop-execution', 'ensure-storage', 'ensure-execution']


@pytest.mark.parametrize('explicit', [True, False])
def test_only_an_explicit_replacement_may_retire_a_failed_storage_close(services, monkeypatch, explicit):
    manager, calls = services
    monkeypatch.setattr(manager._native_storage, 'needs_replacement', lambda *args, **kwargs: True)
    manager._ensure_resident_services({}, {'storage': 2}, None, reset_failed=explicit)
    assert ('stop-storage', {'retire_failed_close': explicit}) in calls
    calls.clear()
    manager._stop_resident_services()
    assert calls == [('stop-execution', {}), ('stop-storage', {})]


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


@pytest.mark.parametrize('changed_field, permitted', [
    ('sourceSha256', True), ('sourceFiles', True),
    ('python', False), ('configurationSha256', False),
    ('environmentFingerprint', False), ('format', False), ('unrecognized', False),
])
def test_only_source_drift_before_boot_can_reprepare_automatically(
        departed_execution, monkeypatch, changed_field, permitted):
    manager, calls, record = departed_execution
    original = {'format': 'tofu.execution-plan/v1', 'python': '/env/python',
        'sourceSha256': 'first', 'sourceFiles': 3, 'environmentFingerprint': 'installed',
        'configurationSha256': 'configuration'}
    record.update(state='failed', failureKind='source_changed_before_boot', plan=original)
    next_plan = {**original, changed_field: 4 if changed_field == 'sourceFiles' else 'changed'}
    monkeypatch.setattr(manager._execution, 'prepare', lambda env: next_plan)
    before = dict(manager._state['worker'])
    assert not manager._recover_execution_service()
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    assert not manager._recover_execution_service()
    assert manager._state['worker'] == before
    assert manager._state['failureHistory'] == [1000.0]
    assert bool(manager._state['executionRecovery'].get('requiresRestart')) is not permitted
    assert [name for name, _ in calls] == (
        ['prepare-storage', 'stop-execution', 'ensure-execution'] if permitted else [])


def test_preboot_source_refresh_cannot_replace_the_storage_generation(
        departed_execution, monkeypatch):
    manager, calls, record = departed_execution
    original = {'format': 'tofu.execution-plan/v1', 'python': '/env/python',
        'sourceSha256': 'first', 'sourceFiles': 3, 'environmentFingerprint': 'installed',
        'configurationSha256': 'configuration'}
    record.update(state='failed', failureKind='source_changed_before_boot', plan=original)
    monkeypatch.setattr(manager._execution, 'prepare',
                        lambda env: {**original, 'sourceSha256': 'changed'})
    monkeypatch.setattr(manager._native_storage, 'needs_replacement', lambda plan: True)
    assert not manager._recover_execution_service()
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    assert not manager._recover_execution_service()
    assert manager._state['executionRecovery']['requiresRestart']
    assert 'storage generation changed' in manager._state['lastError']
    assert [name for name, _ in calls] == ['prepare-storage']


def _still_exiting():
    from serverctl_pkg.resident_service import ServiceOwnerDraining
    raise ServiceOwnerDraining('resident service owner is still draining')


def test_departed_host_still_releasing_its_lease_is_retried_as_the_same_failure(
        departed_execution, monkeypatch):
    # A killed host's cmdline vanishes before its descriptors (and lease) close.
    manager, calls, _ = departed_execution
    assert not manager._recover_execution_service()
    recording_stop = manager._execution.stop
    monkeypatch.setattr(manager._execution, 'stop', _still_exiting)
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    assert not manager._recover_execution_service()
    assert not manager._state['executionRecovery'].get('requiresRestart')
    assert manager._state['failureHistory'] == [1000.0]
    monkeypatch.setattr(manager._execution, 'stop', recording_stop)
    monkeypatch.setattr(existing.sm, '_now', lambda: 1011.0)
    assert not manager._recover_execution_service()
    assert [name for name, _ in calls][-2:] == ['stop-execution', 'ensure-execution']


def test_lease_held_beyond_the_host_stop_budget_requires_explicit_restart(
        departed_execution, monkeypatch):
    from serverctl_pkg.restart_budget import NATIVE_SERVICE_STOP_SECONDS
    manager, calls, _ = departed_execution
    assert not manager._recover_execution_service()
    monkeypatch.setattr(manager._execution, 'stop', _still_exiting)
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0)
    assert not manager._recover_execution_service()
    monkeypatch.setattr(existing.sm, '_now', lambda: 1010.0 + NATIVE_SERVICE_STOP_SECONDS)
    assert not manager._recover_execution_service()
    assert manager._state['executionRecovery']['requiresRestart']
    assert 'still draining' in manager._state['lastError']
    assert 'ensure-execution' not in [name for name, _ in calls]


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
