"""Task start waits out a congested storage authority instead of failing.

2026-11-06 18:03, lane 746148a3 (worker-415ace7ac6e49c68f25d02f1): after
1,507 s of workspace preparation the start failed with "the storage service was
temporarily unavailable during startup (database_timeout): deadline_elapsed".
The authority's queue waits were 25-28 s at the time. The workspace
registration (``integration.workspace.register``) ran under the integration
state repository's fixed 5 s deadline, and nothing on the start path waited
out a transient refusal. The admission rolled back, and the retained checkout
left a terminal intent that stayed pending for the rest of the evening.

A transient refusal now waits under the persistence-wait contract (ACK, owner
Stop, shutdown), and the deadline follows the authority's observed service
time with the old 5 s as its floor.
"""
import subprocess
import threading
from pathlib import Path

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.agent_core.storage_errors import StorageError
from lib.tasks_pkg import persistence_wait

pytestmark = pytest.mark.unit


def git(root, *args):
    cp = subprocess.run(['git', '-C', str(root), *args], text=True,
                        capture_output=True, timeout=20)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **k: None)
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    monkeypatch.setattr(persistence_wait, '_BASE_DELAY_SECONDS', 0.01)
    monkeypatch.setattr(persistence_wait, '_MAX_DELAY_SECONDS', 0.05)
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Storage liveness')
    git(root, 'config', 'user.email', 'liveness@example.invalid')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    try:
        yield root
    finally:
        install_runtime_for_test(None)


def _congested():
    return StorageError('database_timeout', 'deadline_elapsed')


def _task(task_id):
    return {'id': task_id, 'convId': 'worker-conversation', '_userId': 1,
            'status': 'running', 'abort_event': threading.Event()}


def test_registration_waits_out_a_congested_authority_and_starts(repo, monkeypatch):
    original = state.register_workspace
    attempts = []

    def congested_twice(**payload):
        attempts.append(payload)
        if len(attempts) <= 2:
            raise _congested()
        return original(**payload)

    monkeypatch.setattr(state, 'register_workspace', congested_twice)
    task = _task('slow-storage')

    created = ic.create_task_workspace(str(repo), 'slow-storage', user_id=1, task=task)

    assert Path(created['workspacePath']).is_dir()
    assert len(attempts) == 3
    # The exact frozen operation is replayed: one payload, one command identity.
    assert attempts[0] == attempts[2]
    assert state.find_workspace(str(repo.resolve()), 'slow-storage', user_id=1) is not None


def test_stop_during_the_storage_wait_cancels_the_start(repo, monkeypatch):
    from lib.llm.llm_errors import AbortedError
    task = _task('stopped-storage')
    attempts = []

    def congested(**payload):
        attempts.append(payload)
        task['abort_event'].set()  # The owner presses Stop while it waits.
        raise _congested()

    monkeypatch.setattr(state, 'register_workspace', congested)

    with pytest.raises(AbortedError):
        ic.create_task_workspace(str(repo), 'stopped-storage', user_id=1, task=task)
    assert len(attempts) == 1
    # Registration never committed: the unowned checkout is rolled back.
    assert state.find_workspace(str(repo.resolve()), 'stopped-storage', user_id=1) is None


def test_integration_state_deadline_follows_the_observed_service_time(monkeypatch):
    from lib.storage.deadline_policy import (
        observe_storage_service, reset_storage_service_observations,
    )
    deadlines = []

    class Client:
        def command(self, operation, payload, command_id, *, deadline):
            deadlines.append(deadline)
            return {'origin': {}}

        def query(self, operation, payload, *, deadline):
            deadlines.append(deadline)

    monkeypatch.setattr(state, 'get_storage_client', lambda **kw: Client())
    reset_storage_service_observations()
    try:
        state.set_workspace_meta(user_id=1, project_root='/p', task_id='t', patch={}, now=1.0)
        # A congested authority: every recent call took 25 s.
        for _ in range(8):
            observe_storage_service('command', 'integration.workspace.set_meta', 25.0)
            observe_storage_service('query', 'integration.workspace.get', 25.0)
        state.set_workspace_meta(user_id=1, project_root='/p', task_id='t', patch={}, now=2.0)
        state.find_workspace('/p', 't', user_id=1)
    finally:
        reset_storage_service_observations()
    assert deadlines[0] == 5.0
    assert deadlines[1] > 25.0 and deadlines[2] > 25.0
