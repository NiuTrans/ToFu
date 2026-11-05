"""A failed cache write retains the exact settled frontier, never new authority."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.goal_runs.context_recovery import publish_worker_context
from lib.goal_runs.worker_context import GoalWorkerContext

pytestmark = pytest.mark.unit


@pytest.fixture
def publication(monkeypatch):
    task = {'_userId': 7, 'convId': 'conversation', '_attemptId': 'attempt',
            '_turnId': 'settled', 'config': {'model': 'fixture'},
            '_goalWorkerCheckpoint': {
                'messages': [{'role': 'user', 'content': 'objective'},
                             {'role': 'assistant', 'content': 'verified evidence'}],
                'task_state': {'_todos': [{'id': '1', 'status': 'completed'}]}}}
    context = {'supported': True, 'turns': [{
        'turnId': 'settled', 'laneId': 'main', 'status': 'completed',
        'projection': {'content': 'verified evidence'}}]}
    store = Mock()
    store.read_context.return_value = context
    store.put.return_value = True
    monkeypatch.setattr('lib.storage.model_continuation.ModelContinuationStore', lambda: store)
    return task, store


@pytest.mark.parametrize('failure_at', ['read_context', 'put'])
def test_retry_keeps_settled_snapshot_and_does_not_rebind_witness(publication, failure_at):
    task, store = publication
    snapshot = task['_goalWorkerCheckpoint']
    method = getattr(store, failure_at)
    method.side_effect = OSError('lost acknowledgement')
    with pytest.raises(OSError, match='lost acknowledgement'):
        publish_worker_context(task)
    assert task['_goalWorkerCheckpoint'] is snapshot
    if failure_at == 'put':
        first = deepcopy(store.put.call_args.kwargs['projection'])
        store.read_context.return_value['turns'][0]['projection']['content'] = 'edited'
    method.side_effect = None
    assert publish_worker_context(task)
    assert '_goalWorkerCheckpoint' not in task
    if failure_at == 'put':
        assert store.put.call_args.kwargs['projection'] == first
        assert store.read_context.call_count == 1


def test_old_snapshot_cannot_be_published_under_a_later_visible_phase(publication):
    task, store = publication
    store.put.side_effect = OSError('lost acknowledgement')
    with pytest.raises(OSError):
        publish_worker_context(task)
    task['_turnVisibleRunTurnIds'] = ['settled', 'newer']
    store.put.side_effect = None
    assert publish_worker_context(task) is False
    assert store.put.call_count == 1
    assert task['_goalContextRecoveryStatus'] == 'authority_changed'
    assert '_goalWorkerCheckpoint' not in task


def test_replacement_snapshot_survives_an_older_publication_ack(publication):
    task, store = publication
    replacement = deepcopy(task['_goalWorkerCheckpoint'])
    def replace(**kwargs):
        task['_goalWorkerCheckpoint'] = replacement
        return True
    store.put.side_effect = replace
    assert publish_worker_context(task)
    assert task['_goalWorkerCheckpoint'] is replacement


def test_worker_publication_snapshot_is_detached_before_reviewer_mutation():
    from lib.orchestration.agent_runner import OrchestrationInvocationRunner
    parent = {'convId': 'conversation', '_attemptId': 'attempt'}
    runner = object.__new__(OrchestrationInvocationRunner)
    runner._config = SimpleNamespace(parent_task=parent)
    runner._worker_context = GoalWorkerContext()
    child = {'id': 'worker', 'status': 'done', 'finishReason': 'stop',
             '_messages_include_current_run': True,
             'messages': [{'role': 'assistant', 'content': 'completed evidence'}],
             '_todos': [{'id': '1', 'content': 'Verify', 'status': 'completed'}]}
    runner._complete_invocation(child, conversation_worker=True, role='worker', model_route={})
    snapshot = deepcopy(parent['_goalWorkerCheckpoint'])
    runner._worker_context.review('Review requests more work')
    runner._worker_context.task_state['_todos'][0]['status'] = 'pending'
    assert parent['_goalWorkerCheckpoint'] == snapshot
