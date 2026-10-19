"""Progress batching preserves every frame under the selected authority."""
import threading

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg.manager import _events as manager
from lib.agent_core import push
from tests.turn_lifecycle.test_task_event_delivery import _task, _discard

pytest_plugins = ['tests.turn_lifecycle.test_task_event_delivery']
from tests.turn_lifecycle.test_turn_event_carried_task_event import _stored_event

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('lost_ack', [False, True])
def test_progress_backlog_uses_one_atomic_batch_and_retries_exact_frames(
        turn_service, storage, owner, monkeypatch, lost_ack):
    created, task = _task(turn_service, storage, 'batch-progress')
    entered, release = threading.Event(), threading.Event()
    original = turn_service.record_task_event
    calls, published = [], []
    failed_once = False
    def record(task, wire, **kwargs):
        nonlocal failed_once
        if wire['seq'] == 0:
            entered.set()
            assert release.wait(3)
        calls.append(wire['seq'])
        outcome = original(task, wire, **kwargs)
        if lost_ack and wire['seq'] == 63 and not failed_once:
            failed_once = True
            assert published == [0], 'unknown batch ACK became visible before reconciliation'
            raise StorageError('database_timeout', 'injected lost batch ACK')
        return outcome
    def publish(channel, task_id, wire, **kwargs):
        if wire.get('type') != 'delta':
            # The persistence owner may emit a retrying presentation hint.
            return
        stored = _stored_event(storage, task_id, wire['seq'])
        assert stored['event']['content'] == wire['content']
        published.append(wire['seq'])
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    monkeypatch.setattr(push, 'push_event', publish)
    try:
        for index in range(64):
            task['content'] = ''.join(str(i) + ' ' for i in range(index + 1))
            manager.append_event(task, {'type': 'delta', 'content': str(index) + ' '})
            if index == 0:
                assert entered.wait(2)
        assert owner.snapshot()['pending_events'] == 64
        assert owner.snapshot()['pending_batches'] == 2
        assert published == []
        release.set()
        lane = task['_eventDeliveryLane']
        if lost_ack:
            # The shared persistence owner reconciles a transient lost ACK
            # internally. The exact same frozen batch must be retried; it need
            # not first escape as lane debt before the caller can join it.
            owner.flush(lane, recover=False)
        else:
            owner.flush(lane)
        assert calls == ([0, 63, 63] if lost_ack else [0, 63])
        assert published == list(range(64))
        turn = turn_service.get_turn(task['convId'], created['turn']['turnId'], user_id=1)
        assert turn['projection']['content'] == task['content']
        rows = storage.client.query('event.list', {'task_id': task['id'], 'after_sequence': -1, 'limit': 100})
        assert [row['sequence'] for row in rows] == list(range(64))
        assert [row['event']['content'] for row in rows] == [str(i) + ' ' for i in range(64)]
        assert owner.snapshot()['pending_bytes'] == 0
    finally:
        release.set()
        _discard(task)


@pytest.mark.parametrize('invalid', ['conflict', 'foreign_task', 'sequence_gap', 'structural', 'byte_limit', 'count_limit'])
def test_batch_rejection_rolls_back_projection_and_all_new_frames(
        turn_service, storage, invalid):
    created, task = _task(turn_service, storage, 'batch-rollback')
    try:
        before = turn_service.get_turn(task['convId'], created['turn']['turnId'], user_id=1)
        prefix = [
            {'task_id': task['id'], 'sequence': 0, 'event': {'type': 'delta', 'content': 'a'}},
            {'task_id': task['id'], 'sequence': 1, 'event': {'type': 'delta', 'content': 'b'}},
        ]
        if invalid == 'conflict':
            storage.client.command('event.append', {
                'task_id': task['id'], 'sequence': 1,
                'event': {'type': 'delta', 'content': 'existing different value'}}, None)
        elif invalid == 'foreign_task':
            prefix[0]['task_id'] = 'another-owner-task'
        elif invalid == 'sequence_gap':
            prefix[0]['sequence'] = 9
        elif invalid == 'structural':
            prefix[0]['event']['type'] = 'round_end'
        elif invalid == 'byte_limit':
            prefix[0]['event']['content'] = 'x' * (1024 * 1024)
        else:
            prefix = [{'task_id': task['id'], 'sequence': i,
                       'event': {'type': 'delta', 'content': 'x'}} for i in range(500)]
        task['content'] = 'abc'
        with pytest.raises(StorageError):
            turn_service.record_task_event(task, {'type': 'delta', 'content': 'c'}, task_event={
                'task_id': task['id'], 'sequence': len(prefix), 'event': {'type': 'delta', 'content': 'c'},
                'preceding_events': prefix})
        after = turn_service.get_turn(task['convId'], created['turn']['turnId'], user_id=1)
        assert after['projectionRevision'] == before['projectionRevision']
        assert after['projection'] == before['projection']
        rows = storage.client.query('event.list', {'task_id': task['id'], 'after_sequence': -1, 'limit': 10})
        assert [row['sequence'] for row in rows] == ([1] if invalid == 'conflict' else [])
    finally:
        _discard(task)


def test_missing_batch_acknowledgement_withholds_visibility_and_retains_evidence(
        turn_service, storage, owner, monkeypatch):
    created, task = _task(turn_service, storage, 'batch-echo')
    original = storage.client.command
    def command(operation, payload, *args, **kwargs):
        result = original(operation, payload, *args, **kwargs)
        if operation == 'turn.event.record' and payload.get('task_event', {}).get('preceding_events'):
            result = dict(result)
            result.pop('task_event_count', None)
        return result
    monkeypatch.setattr(storage.client, 'command', command)
    try:
        with pytest.raises(StorageError, match='complete progress event batch'):
            turn_service.record_task_event(task, {'type': 'delta', 'content': 'b'}, task_event={
                'task_id': task['id'], 'sequence': 1, 'event': {'type': 'delta', 'content': 'b'},
                'preceding_events': [{'task_id': task['id'], 'sequence': 0,
                                      'event': {'type': 'delta', 'content': 'a'}}]})
        assert task.get('_eventVisibleThrough', -1) == -1
    finally:
        _discard(task)
