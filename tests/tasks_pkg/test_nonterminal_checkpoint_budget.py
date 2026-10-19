"""Best-effort task diagnostics cannot spend terminal retry time at startup."""
from __future__ import annotations

import time

import pytest

from lib.storage import StorageError
from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
from lib.tasks_pkg.manager._persist import _upsert_task_row

pytestmark = pytest.mark.unit


def write(task, status):
    return _upsert_task_row(
        task, task['convId'], content='', thinking='', status=status,
        error_json=None, tr_json=None, meta_json=None,
    )


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_parent_witness_and_write_share_one_budget(monkeypatch, status):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    class Client:
        def query(self, operation, payload, *, deadline):
            calls.append((operation, deadline))
            clock[0] += .2
            if operation == 'conversation.get':
                assert payload['user_id'] == 7
                return {'id': 'conversation'}
            assert operation == 'task_results.replay_get'
            assert payload == {'key': 'task', 'user_id': 7}
            return None

        def command(self, operation, payload, command_id, **options):
            calls.append((operation, options['deadline']))
            assert payload['expected_version'] == 0
            assert payload['require_parent'] is True
            return {'owned': True, 'version': 1,
                    'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}

    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_: Client())
    assert write({'id': 'task', 'convId': 'conversation', '_userId': 7}, status)
    assert [budget for _, budget in calls] == pytest.approx([.5, .3, .1])


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_exhausted_preflight_does_not_begin_a_write(monkeypatch, status):
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    class Client:
        def query(self, operation, payload, *, deadline):
            assert operation == 'conversation.get'
            clock[0] += .6
            return {'id': payload['conv_id']}

        def command(self, *args, **kwargs):
            pytest.fail('checkpoint admitted after its entire budget expired')

    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_: Client())
    with pytest.raises(StorageError, match='budget exhausted') as error:
        write({'id': 'task', 'convId': 'conversation', '_userId': 7}, status)
    assert error.value.code == 'database_timeout'


@pytest.mark.parametrize('status, attempts', [('pending', 1), ('running', 1), ('done', 5)])
def test_only_terminal_write_retries_transient_pressure(monkeypatch, status, attempts):
    calls = []
    monkeypatch.setattr(time, 'sleep', lambda _: None)

    class Client:
        def command(self, operation, payload, command_id, **options):
            calls.append(options)
            if status == 'done' and len(calls) == attempts:
                return {'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                        'owned': True, 'version': 2}
            raise StorageError('database_busy', 'injected writer pressure')

    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_: Client())
    task = {'id': 'task', 'convId': '', '_userId': 7,
            '_taskResultVersion': 1,
            '_taskResultCheckpointGuard': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    if status == 'done':
        write(task, status)
        assert task['_taskResultVersion'] == 2
    else:
        with pytest.raises(StorageError, match='injected writer pressure'):
            write(task, status)
    assert len(calls) == attempts
    if status == 'done':
        assert all('deadline' not in options for options in calls)
    else:
        assert 0 < calls[0]['deadline'] <= .5
