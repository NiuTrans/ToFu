"""Terminal persistence retries one frozen outcome, then publishes its original frame."""
from copy import deepcopy

import pytest

from lib.agent_core.task_runtime import TaskRuntime
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


def running(persist, name):
    runtime = TaskRuntime(name, terminal_persist=persist)
    task = runtime.create(user_id=7, task_id=name)
    runtime.mark_running(name)
    return runtime, task


@pytest.mark.parametrize('mutation', ['caller', 'persistence_port'])
def test_lost_ack_keeps_nested_candidate_and_terminal_fields_frozen(mutation):
    attempts = []
    def persist(task, candidate):
        attempts.append(deepcopy(candidate))
        if mutation == 'persistence_port':
            candidate['result']['rows'].append('port mutation')
            candidate['status'] = 'error'
        if len(attempts) == 1:
            raise StorageError('database_timeout', 'commit acknowledged late')
    runtime, task = running(persist, 'frozen-terminal-' + mutation)
    result = {'rows': ['accepted']}
    fields = {'download': {'artifact': 'original'}, 'type': 'untrusted-type'}
    task['artifact_quality'] = {'degraded': False, 'labels': ['original']}
    with pytest.raises(StorageError):
        runtime.finish(task['id'], result=result, terminal_event_fields=fields)
    if mutation == 'caller':
        result['rows'].append('caller mutation')
        fields['download']['artifact'] = 'changed'
        task['artifact_quality']['labels'].append('changed')
    assert runtime.poll(task['id'])['status'] == 'done'
    assert attempts[0] == attempts[1]
    assert task['result'] == {'rows': ['accepted']}
    assert task['artifact_quality']['labels'] == ['original']
    assert task['events'][-1]['type'] == 'done'
    assert task['events'][-1]['download'] == {'artifact': 'original'}
    assert not task.get('_terminalPersistencePending')
    assert not task.get('_terminalPersistenceCandidate')


def test_explicit_false_ack_keeps_terminal_pending_until_a_real_ack():
    attempts = []
    def persist(_, candidate):
        attempts.append(deepcopy(candidate))
        return False if len(attempts) == 1 else None
    runtime, task = running(persist, 'false-terminal-ack')
    with pytest.raises(RuntimeError, match='not acknowledged'):
        runtime.finish(task['id'], result={'accepted': True})
    assert task['status'] == 'running'
    assert not task['_executionTerminalizing']
    assert task['_terminalPersistencePending']
    assert runtime.finish(task['id'])
    assert attempts[0] == attempts[1]
    assert task['status'] == 'done'


def test_snapshot_failure_releases_terminalization_guard_without_repeating_cleanup():
    attempts, released = [], []
    def persist(_, candidate):
        attempts.append(deepcopy(candidate))
    runtime, task = running(persist, 'uncopyable-terminal')
    task['_executionSession'].hold_resource('route', lambda _: released.append('route'))
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise ValueError('cannot freeze terminal outcome')
    with pytest.raises(ValueError, match='cannot freeze'):
        runtime.finish(task['id'], result={'value': Uncopyable()})
    assert not task['_executionTerminalizing']
    assert not task.get('_terminalPersistenceCandidate')
    assert runtime.finish(task['id'], result={'value': 'valid'})
    assert released == ['route']
    assert attempts == [{'status': 'done', 'result': {'value': 'valid'}, 'error': None,
                         'artifact_quality': None, 'finished_at': task['finished_at']}]


def test_pending_terminal_cannot_bypass_a_withdrawn_durability_port():
    attempts = []
    def persist(_, candidate):
        attempts.append(deepcopy(candidate))
        if len(attempts) == 1:
            raise StorageError('database_busy', 'not acknowledged')
    runtime, task = running(persist, 'withdrawn-terminal-port')
    with pytest.raises(StorageError):
        runtime.finish(task['id'], result={'accepted': True})
    runtime.terminal_persist = None
    with pytest.raises(RuntimeError, match='requires its durability port'):
        runtime.finish(task['id'])
    assert task['status'] == 'running' and task['_terminalPersistencePending']
    assert not task['_executionTerminalizing']
    runtime.terminal_persist = persist
    assert runtime.finish(task['id'])
    assert attempts[0] == attempts[1]
