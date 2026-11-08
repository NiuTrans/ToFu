"""API activity includes durable work even when it owns no task carrier."""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _sources(monkeypatch, rows, local=()):
    from lib import storage
    from lib.tasks_pkg.manager import _registry
    calls = []
    def query(operation, payload):
        assert operation == 'worker_job.active.list'
        calls.append(payload)
        return rows
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: SimpleNamespace(query=query))
    monkeypatch.setattr(_registry, 'chat_task_runtime', SimpleNamespace(snapshot=lambda: local))
    return calls


def row(task='durable', conv='remote', owner=7, created=1000):
    return dict(taskId=task, userId=owner, taskKind='conversation-attempt',
                conversationId=conv, status='running', createdAtMs=created)


def test_remote_work_remains_busy_without_local_liveness_clocks(monkeypatch):
    from lib.tasks_pkg.manager import list_running_tasks
    calls = _sources(monkeypatch, [row()])
    value = list_running_tasks(user_id=7)
    assert [item['taskId'] for item in value] == ['durable']
    assert value[0]['elapsed'] > 0
    assert calls == [{'user_id': 7, 'task_kinds': ['conversation-attempt']}]
    assert list_running_tasks('remote', user_id=7) == []


def test_live_and_durable_views_deduplicate_by_owner_and_conversation(monkeypatch):
    from lib.tasks_pkg.manager import list_running_tasks
    import time
    local = dict(id='local', convId='same', _userId=7, created_at=time.time(),
                 status='pending', config={})
    _sources(monkeypatch, [row(conv='same'), row('other', 'same', owner=8)], [local])
    assert {item['taskId'] for item in list_running_tasks()} == {'durable', 'other'}


def test_activity_error_cannot_be_interpreted_as_idle(monkeypatch):
    from lib.auto_restart import _restart_preconditions
    from lib.tasks_pkg.manager import list_running_tasks
    from lib import storage
    def unavailable(**kwargs):
        raise RuntimeError('authority unavailable')
    monkeypatch.setattr(storage, 'get_storage_client', unavailable)
    assert _restart_preconditions(None, list_running_tasks) == (False, 'check-error:RuntimeError')


def test_foreign_activity_response_is_refused_before_projection(monkeypatch):
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.manager import list_running_tasks
    _sources(monkeypatch, [row(owner=8)])
    with pytest.raises(StorageError):
        list_running_tasks(user_id=7)


@pytest.fixture
def steps_root(tmp_path, monkeypatch):
    from lib.runtime import protected_steps
    root = tmp_path / 'protected-steps'
    monkeypatch.setattr(protected_steps, 'protected_steps_root', lambda: str(root))
    return root


def test_a_protected_step_joins_the_restart_signal_but_never_owner_views(monkeypatch, steps_root):
    """git_land and export run outside conversations (terminal, background command,
    another agent); the restart guards still see them while they run."""
    import os
    from lib.runtime.protected_steps import protected_step
    from lib.tasks_pkg.manager import list_running_tasks
    _sources(monkeypatch, [])
    with protected_step('git_land', detail='feature/x') as marker:
        [entry] = list_running_tasks()
        assert entry['taskId'] == 'protected-step:' + marker['id'] and entry['convId'] == ''
        assert entry['protectedStep'] == 'git_land' and entry['marker'].endswith('.json')
        # The caller's own conversation is excluded, never a protected step.
        assert list_running_tasks('router-conversation') == [entry]
        # Owner-scoped projections (sidebar busy dots) show conversations only.
        assert list_running_tasks(user_id=7) == []
    assert list_running_tasks() == [] and os.listdir(steps_root) == []


def test_a_crashed_local_step_never_blocks_but_an_unobservable_one_does(monkeypatch, steps_root):
    import json
    import socket
    import subprocess
    import sys
    from lib.tasks_pkg.manager import list_running_tasks
    _sources(monkeypatch, [])
    finished = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'],
                              capture_output=True, text=True, check=True)
    steps_root.mkdir()
    base = {'version': 1, 'kind': 'git_land', 'startTime': None, 'startedAt': 1.0, 'detail': ''}
    (steps_root / 'dead.json').write_text(json.dumps(
        {**base, 'id': 'dead', 'host': socket.gethostname(), 'pid': int(finished.stdout)}))
    assert list_running_tasks() == []
    (steps_root / 'peer.json').write_text(json.dumps(
        {**base, 'id': 'peer', 'kind': 'export', 'host': 'another-host', 'pid': 1}))
    (steps_root / 'torn.json').write_text('{"id": "torn"')
    assert sorted(item['protectedStep'] for item in list_running_tasks()) == ['export', 'unknown']

