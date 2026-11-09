"""Once a restart has stopped the old server, it always ends with a serving server.

2026-11-06: two restarts stopped the API worker, then refused while replacing
a resident host ("execution shutdown failed: TimeoutError:" at 14:11,
"native storage shutdown failed: clean close was not confirmed" at 22:40).
The manager latched ``crashloop`` and nothing served until a human restarted
it (42 and 69 minutes). These specs drive the real manager with a real API
worker and a real resident storage host whose stop outcome is injected.
"""
from __future__ import annotations

import logging
from pathlib import Path
import sys
import time

import pytest

from serverctl_pkg import native_storage_service, resident_service as resident
from serverctl_pkg.native_storage_service import NativeStorageService
from tests.server import test_server_manager as existing
from tests.support.owned_processes import processes_under, retire_processes_under

sm = existing.sm
_scrub_ambient_server_env = existing._scrub_ambient_server_env
ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.api,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process identity required'),
]


class ProbeStorage(NativeStorageService):
    """The real resident owner; only the host program and its plan are test doubles."""
    host_module = 'tests.support.resident_storage_probe'
    stop_budget = 30.0

    def __init__(self, project: str, python: str):
        super().__init__(project, python)
        self.generation = 0

    def host_matches(self, record: dict) -> bool:
        return resident.host_matches(record, host_module=self.host_module)

    def process_environment(self, environment):
        return {**super().process_environment(environment), 'PYTHONPATH': str(ROOT)}

    def prepare(self, environment):
        # Every restart prepares a new generation, so storage is replaced.
        self.generation += 1
        return {'backend': 'probe', 'generation': self.generation}

    def owns_lease(self, lease):
        return False

    def stop(self, timeout=None, **options):
        return super().stop(self.stop_budget if timeout is None else timeout, **options)


@pytest.fixture
def harness(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='server_manager')
    port = existing._free_port()
    project = existing._project(tmp_path, existing._WORKER)
    monkeypatch.setenv('PORT', str(port))
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    manager = sm.LifecycleManager(str(project), sys.executable, monitor_interval=0.2)
    storage = ProbeStorage(str(project), sys.executable)
    manager._native_storage = storage
    # Reconciliation's host recovery reads the module-level identity check.
    monkeypatch.setattr(native_storage_service, 'host_matches', storage.host_matches)
    try:
        yield manager, project, storage
    finally:
        (project / 'probe-stop-mode').write_text('clean')
        stopped = {}
        try:
            stopped = manager.stop(source='spec-teardown')
        finally:
            manager.close()
            leftovers = retire_processes_under(tmp_path)
        assert leftovers == [], f'processes outlived the test: {leftovers}; stop: {stopped}'


def serving(manager, *, other_than=None, timeout=60.0):
    """Drive reconciliation like the monitor until a healthy worker serves."""
    deadline = time.monotonic() + timeout
    current = {}
    while time.monotonic() < deadline:
        manager.reconcile()
        current = manager.status(probe_health=True)
        if (current.get('running') and current.get('health')
                and current.get('pid') != other_than):
            return current
        time.sleep(0.1)
    raise AssertionError(f'no serving worker within {timeout:.0f}s: {current}')


def start_serving(manager):
    started = manager.start(source='spec')
    assert started['ok'] is True, started
    return serving(manager)


def test_storage_boundary_violation_keeps_the_serving_generation_alive(harness, monkeypatch):
    from serverctl_pkg.execution_plan import prepare_plan
    manager, project, storage = harness
    first = start_serving(manager)
    old_storage = storage.record()
    (project / 'lib').mkdir()
    (project / 'lib/__init__.py').write_text('')
    (project / 'lib/invalid_owner.py').write_text(
        "import sqlite3\nconn = sqlite3.connect('application.db')\n")
    monkeypatch.setattr(manager._execution, 'enabled', lambda env: True)
    monkeypatch.setattr(manager._execution, 'prepare',
                        lambda env: prepare_plan(project, env, python=sys.executable))

    refused = manager.restart(source='spec')

    assert not refused['ok']
    assert 'storage cutover refused' in refused['message']
    current = manager.status(probe_health=True)
    assert current['pid'] == first['pid']
    assert current['running'] and current['health']
    assert current['lastRestart']['outcome'] == 'refused'
    assert storage.record()['serviceId'] == old_storage['serviceId']
    assert storage.host_matches(old_storage)
    assert not (project / 'application.db').exists()


@pytest.mark.parametrize('mode, retired', [
    ('stale', 'clean close was not confirmed'),
    ('timeout', 'TimeoutError: native daemon drain did not finish'),
])
def test_unconfirmed_storage_close_still_ends_with_a_serving_server(harness, caplog, mode, retired):
    manager, project, storage = harness
    first = start_serving(manager)
    old_host = storage.record()
    (project / 'probe-stop-mode').write_text(mode)

    result = manager.restart(source='spec')

    assert result['ok'] is True, result
    current = serving(manager, other_than=first['pid'])
    assert current['observed'] == 'running'
    assert storage.record()['serviceId'] != old_host['serviceId']
    assert storage.host_matches(storage.record())
    outcome = current['lastRestart']
    assert outcome['outcome'] == 'spawned'
    assert any(retired in close['detail'] and close['service'] == 'native storage'
               for close in outcome['uncleanCloses'])
    assert 'crash recovery' in caplog.text and retired in caplog.text


def test_storage_host_past_its_stop_budget_is_killed_and_replaced(harness, caplog):
    manager, project, storage = harness
    first = start_serving(manager)
    old_host = storage.record()
    storage.stop_budget = 2.0
    (project / 'probe-stop-mode').write_text('hang')

    result = manager.restart(source='spec')

    assert result['ok'] is True, result
    current = serving(manager, other_than=first['pid'])
    assert not resident.process_matches(old_host['pid'], old_host['processStartTime'])
    assert storage.record()['serviceId'] != old_host['serviceId']
    closes = current['lastRestart']['uncleanCloses']
    assert any('killed' in close['detail'] for close in closes)


def test_a_failure_after_stop_is_retried_until_a_server_serves(harness, monkeypatch, caplog):
    manager, project, storage = harness
    first = start_serving(manager)
    launches = []
    real_ensure = storage.ensure

    def replacement_fails_once(plan, environment):
        launches.append(plan)
        if len(launches) == 1:
            raise RuntimeError('injected: replacement storage host failed to launch')
        return real_ensure(plan, environment)

    monkeypatch.setattr(storage, 'ensure', replacement_fails_once)

    result = manager.restart(source='spec')

    assert result['ok'] is True, result
    assert result.get('retrying') is True
    assert manager.status()['observed'] != 'crashloop'
    current = serving(manager, other_than=first['pid'])
    assert len(launches) == 2
    assert current['recentFailureCount'] == 1
    assert current['lastRestart']['outcome'] == 'spawned'
    assert 'injected: replacement storage host failed to launch' in caplog.text


def test_a_refusal_keeps_the_old_server_serving_and_is_recorded(harness, monkeypatch):
    manager, project, storage = harness
    first = start_serving(manager)
    refusal = 'production lifecycle refused: inherited pytest context (TOFU_TESTING)'

    with monkeypatch.context() as patch:
        patch.setattr(sm, 'production_server_environment_error', lambda *_args: refusal)
        result = manager.restart(source='spec')

    assert result['ok'] is False
    assert refusal in result['message']
    current = serving(manager)
    assert current['pid'] == first['pid']
    assert current['desired'] == 'running'
    assert current['lastRestart']['outcome'] == 'refused'


def test_a_resident_host_owned_elsewhere_refuses_before_the_old_server_stops(harness):
    manager, project, storage = harness
    first = start_serving(manager)
    record = storage.record()
    resident.write_record(storage.state_path, {**record, 'host': 'another-host'})

    result = manager.restart(source='spec')

    assert result['ok'] is False
    assert 'another host' in result['message']
    current = serving(manager)
    assert current['pid'] == first['pid']
    # Restore so teardown's full stop owns the host again.
    resident.write_record(storage.state_path, record)


def test_harness_finds_a_worker_that_outlived_its_owner(tmp_path):
    """The teardown check itself: a detached process under the test dir is found and retired."""
    import subprocess
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                               cwd=tmp_path, start_new_session=True)
    try:
        assert [found['pid'] for found in processes_under(tmp_path)] == [process.pid]
        assert [found['pid'] for found in retire_processes_under(tmp_path)] == [process.pid]
        assert processes_under(tmp_path) == []
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
