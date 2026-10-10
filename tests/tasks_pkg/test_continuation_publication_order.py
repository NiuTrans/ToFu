"""Continuation publication precedes settlement-triggered model consumers."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.agent_core.events import EventType, build_event

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _task():
    from lib import turn_lifecycle as lifecycle
    conv = f'publication-order-{uuid.uuid4().hex}'
    created = lifecycle.create_turn_pair(
        conv, command_id=conv, input_projection={'content': 'request'},
        config={'model': 'gpt-4o'}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'publication'},
    )
    task_id = f'task-{uuid.uuid4().hex}'
    lifecycle.bind_task(created['attempt']['attemptId'], task_id, user_id=1)
    return {
        'id': task_id, '_attemptId': created['attempt']['attemptId'],
        '_turnId': created['turn']['turnId'], '_userId': 1, 'convId': conv,
        'status': 'done', 'content': 'answer', 'thinking': '',
        'finishReason': 'stop', 'config': {'model': 'gpt-4o'},
        'messages': [{'role': 'assistant', 'content': 'answer'}],
    }


def test_blocked_publication_holds_queue_and_terminal_return(chat_sidecar, monkeypatch):
    from lib import turn_lifecycle as lifecycle, message_queue
    from lib.tasks_pkg import model_replay

    task = _task()
    entered, release, consumed = threading.Event(), threading.Event(), threading.Event()
    order = []

    def publish(current):
        # Preparation must precede the authority transaction.
        turn = lifecycle.get_turn(current['convId'], current['_turnId'], user_id=1)
        assert turn['status'] != 'completed'
        order.append('preparing')
        entered.set()
        assert release.wait(10)
        order.append('published')

    def dispatch(conv, *, user_id):
        try:
            order.append('successor')
            lifecycle.build_api_messages(conv, '', {'model': 'gpt-4o'}, user_id=user_id)
        finally:
            consumed.set()
        return 'successor-task'

    monkeypatch.setattr(model_replay, 'prepare_completed_task', publish)
    monkeypatch.setattr(message_queue, 'get_queue_depth', lambda *a, **k: 1)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued', dispatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lifecycle.record_task_event, task,
                                 build_event(EventType.DONE, finishReason='stop'))
        try:
            assert entered.wait(10)
            assert not future.done(), 'done consumer overtook publication'
            assert not consumed.is_set(), 'queued successor overtook publication'
            assert order == ['preparing']
        finally:
            release.set()
        assert future.result(timeout=10) is True
        assert consumed.wait(10)
    assert order == ['preparing', 'published', 'successor']


@pytest.mark.parametrize('finish,expected_status,publishes', [
    ('stop', 'completed', True),
    ('aborted', 'interrupted', False),
    ('length', 'truncated', True),
])
def test_publication_failure_preserves_settlement_and_drain(
        chat_sidecar, monkeypatch, caplog, finish, expected_status, publishes):
    import logging
    caplog.set_level(logging.INFO, logger='lib.turn_lifecycle')
    from lib import turn_lifecycle as lifecycle
    from lib.tasks_pkg import model_replay
    task = _task()
    task['finishReason'] = finish
    task['aborted'] = finish == 'aborted'
    calls = []

    def fail_publish(current):
        calls.append('publish')
        raise OSError('injected continuation failure')

    monkeypatch.setattr(model_replay, 'prepare_completed_task', fail_publish)
    monkeypatch.setattr(lifecycle, '_drain_queue_after_settlement',
                        lambda task, conv, status: calls.append(status))
    event = build_event(EventType.DONE, finishReason=finish)
    assert lifecycle.record_task_event(task, event) is True
    assert calls == (['publish'] if publishes else []) + [expected_status]
    assert lifecycle.get_turn(task['convId'], task['_turnId'], user_id=1)['status'] == expected_status

    assert any('checkpoint_settled=' in record.message
               and task['_attemptId'] in record.message
               and task['_modelReplayPublishStatus'] in record.message
               for record in caplog.records)
    # Duplicate/stale settlement must not republish or drain again.
    assert lifecycle.record_task_event(task, event) is False
    assert calls == (['publish'] if publishes else []) + [expected_status]


from tests.model_routing.test_model_continuation_store import authority as authority_fixture  # noqa: E402

# Pytest discovers the fixture under its exported alias without importing a
# symbol shadowed by every fixture-injected test parameter.
authority = authority_fixture


def _prepared_task(authority, monkeypatch):
    from lib import storage
    from lib.storage.model_continuation import settled_witness
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: authority.client)
    task = _task()
    rows = authority.client.query('turn.list', {
        'conversation_id': task['convId'], 'user_id': 1})
    task['model'] = 'gpt-4o'
    task['config']['_modelReplayBase'] = [settled_witness(rows[0])]
    task['messages'] = [
        {'role': 'user', 'content': 'compacted input; raw request must not resurrect'},
        {'role': 'assistant', 'content': 'answer'}]
    return task


def _checkpoint(authority, task):
    from lib.storage.model_continuation import ModelContinuationStore
    return ModelContinuationStore(client=authority.client).get(
        user_id=1, conversation_id=task['convId'], lane_id='main')


def test_terminal_carrier_conflict_rolls_back_continuation(authority, monkeypatch):
    from lib import turn_lifecycle as lifecycle
    from lib.storage import StorageError
    task = _prepared_task(authority, monkeypatch)
    before = lifecycle.get_turn(task['convId'], task['_turnId'], user_id=1)
    authority.client.command('event.append', {
        'task_id': task['id'], 'sequence': 0,
        'event': {'type': 'delta', 'content': 'existing'}}, uuid.uuid4().hex)
    done = build_event(EventType.DONE, finishReason='stop')
    with pytest.raises(StorageError, match='conflict'):
        lifecycle.record_task_event(task, done, task_event={
            'task_id': task['id'], 'sequence': 0, 'event': done})
    assert _checkpoint(authority, task) is None
    after = lifecycle.get_turn(task['convId'], task['_turnId'], user_id=1)
    assert after['status'] == before['status']
    assert after['projectionRevision'] == before['projectionRevision']
    # Retry with an unoccupied carrier commits both halves, not stale cache state.
    assert lifecycle.record_task_event(task, done, task_event={
        'task_id': task['id'], 'sequence': 1, 'event': done}) == 'carried'
    assert task['_modelReplayPublishStatus'] == 'stored'
    assert _checkpoint(authority, task)['messages'] == task['messages']


@pytest.mark.parametrize('finish,status', [('stop', 'completed'), ('length', 'truncated')])
def test_post_commit_crash_restart_next_turn_does_not_resurrect(authority, monkeypatch, finish, status):
    from lib import turn_lifecycle as lifecycle
    from lib.tasks_pkg.model_replay import restore_prefix
    from lib.storage.model_continuation import settled_witness
    task = _prepared_task(authority, monkeypatch)

    def crash(*args):
        raise RuntimeError('injected crash before drain')

    monkeypatch.setattr(lifecycle, '_drain_queue_after_settlement', crash)
    task['finishReason'] = finish
    done = build_event(EventType.DONE, finishReason=finish)
    with pytest.raises(RuntimeError, match='before drain'):
        lifecycle.record_task_event(task, done)
    settled = lifecycle.get_turn(task['convId'], task['_turnId'], user_id=1)
    assert settled['status'] == status
    assert _checkpoint(authority, task)['turns'][-1] == settled_witness(settled)
    authority.stop()
    authority.start()
    assert lifecycle.record_task_event(task, done) is False
    lifecycle.create_turn_pair(
        task['convId'], command_id=uuid.uuid4().hex,
        input_projection={'content': 'next request'}, config={'model': 'gpt-4o'}, user_id=1)
    rows = authority.client.query('turn.list', {
        'conversation_id': task['convId'], 'user_id': 1})
    config = {'model': 'gpt-4o'}
    restored, covered = restore_prefix(rows, config, conversation_id=task['convId'], user_id=1)
    assert config['_modelReplayStatus'] == 'restored'
    assert covered == 2
    assert restored == task['messages']
    assert not any(message.get('content') == 'request' for message in restored)


@pytest.mark.parametrize('candidate,expected', [
    ({'projection': {}}, 'invalid_or_over_budget'),
    ({'status': 'unavailable'}, 'unavailable'),
])
def test_invalid_candidate_status_is_durable_without_history_loss(
        authority, monkeypatch, candidate, expected):
    from lib import turn_lifecycle as lifecycle
    from lib.tasks_pkg import model_replay
    task = _prepared_task(authority, monkeypatch)
    monkeypatch.setattr(model_replay, 'prepare_completed_task', lambda task: candidate)
    assert lifecycle.record_task_event(task, build_event(EventType.DONE, finishReason='stop'))
    assert task['_modelReplayPublishStatus'] == expected
    assert _checkpoint(authority, task) is None
    turn = lifecycle.get_turn(task['convId'], task['_turnId'], user_id=1)
    assert turn['status'] == 'completed' and turn['projection']['content'] == 'answer'
    events = authority.client.query('turn.events.list', {
        'attempt_id': task['_attemptId'], 'user_id': 1})
    assert events[-1]['payload']['modelContinuationStatus'] == expected
