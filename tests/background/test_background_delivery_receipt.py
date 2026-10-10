"""A delivered background block and its queue removal are one authority commit."""
from __future__ import annotations

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg.orchestrator import _deferred_inbox_flush as flush
from tests.storage.test_storage_sidecar_contract import storage  # noqa: F401
from tests.turn_lifecycle.test_turn_event_carried_task_event import (
    turn_service, _running_task,  # noqa: F401
)

pytestmark = pytest.mark.unit


def _seed(turn_service, storage, monkeypatch):  # noqa: F811
    created, task = _running_task(turn_service, storage, 'receipt-c', 'receipt-task')
    task['convId'] = 'receipt-c'
    monkeypatch.setattr(turn_service, '_drain_queue_after_settlement', lambda *args: None)
    client = storage.client
    for qid, message in [('q-bg', {'_backgroundCommand': 'bg_1'}),
                         ('q-other', {'text': 'keep'})]:
        client.command('queue.enqueue', {
            'conv_id': 'receipt-c', 'user_id': 1, 'queue_id': qid,
            'kind': ('background_receipt' if qid == 'q-bg'
                     else 'workflow_step'), 'message': message,
        }, f'enqueue:{qid}')
    task['_bgcmd_inject_pending'] = [
        {'queueId': 'q-bg', 'commandId': 'bg_1', 'value': 'command result'}]
    monkeypatch.setattr(flush, 'append_event',
                        lambda task, event: turn_service.record_task_event(task, event))
    return created, task


def _snapshot(storage):  # noqa: F811
    return storage.client.query('turn.sync.snapshot', {
        'conversation_id': 'receipt-c', 'user_id': 1})


def _turn(snapshot, created):
    return next(t for t in snapshot['turns'] if t['turnId'] == created['turn']['turnId'])


def test_receipt_first_event_and_queue_removal_share_one_replay_commit(
        turn_service, storage, monkeypatch):  # noqa: F811
    created, task = _seed(turn_service, storage, monkeypatch)
    before = _snapshot(storage)
    flush.flush_deferred_peer_and_steer(task, round_num=2, tid='receipt')
    after = _snapshot(storage)
    assert [q['queueId'] for q in after['queueItems']] == ['q-other']
    assert after['queueItems'][0]['position'] == 1
    receipt = _turn(after, created)['projection']['_bgCommandInjects'][0]
    assert receipt['previews'][0]['queueId'] == 'q-bg'
    assert receipt['round'] == 3
    assert receipt == task['_bgCommandInjects'][0]
    assert '_bgcmd_inject_pending' not in task
    changes = storage.client.query('turn.sync.changes', {
        'conversation_id': 'receipt-c', 'user_id': 1, 'after': before['syncSequence']})
    assert len(changes['events']) == 1  # No separate queue-change render window.
    event = changes['events'][0]['payload']['event']
    assert event['payload']['removedQueueIds'] == ['q-bg']
    from lib.turn_projection_patch import apply_projection_patch
    projected = apply_projection_patch(
        _turn(before, created)['projection'], event['payload']['projectionPatch'])
    assert projected['_bgCommandInjects'] == [receipt]
    from lib.conversation_sync.validation import decode
    decode('AttemptEvent', event)


def test_failed_carrier_rolls_back_queue_receipt_and_replay_without_sidecar_leak(
        turn_service, storage, monkeypatch):  # noqa: F811
    created, task = _seed(turn_service, storage, monkeypatch)
    client = storage.client
    before = _snapshot(storage)
    client.command('event.append', {
        'task_id': task['id'], 'sequence': 0, 'event': {'type': 'delta', 'content': 'old'},
    }, 'conflicting-carrier')
    def conflicting_append(task, event):
        return turn_service.record_task_event(task, event, task_event={
            'task_id': task['id'], 'sequence': 0, 'event': event})
    monkeypatch.setattr(flush, 'append_event', conflicting_append)
    flush.flush_deferred_peer_and_steer(task, round_num=2, tid='receipt')
    after = _snapshot(storage)
    assert after['syncSequence'] == before['syncSequence']
    assert _turn(after, created)['projectionRevision'] == _turn(before, created)['projectionRevision']
    assert {q['queueId'] for q in after['queueItems']} == {'q-bg', 'q-other'}
    assert not task.get('_bgCommandInjects')
    assert task['_bgcmd_inject_pending']
    # An unrelated frame/checkpoint cannot publish the uncommitted receipt.
    turn_service.record_task_event(task, {'type': 'phase', 'phase': 'tool_exec'})
    assert not _turn(_snapshot(storage), created)['projection'].get('_bgCommandInjects')
    # Retry the frozen receipt, not another provider dispatch. Its round remains 3.
    monkeypatch.setattr(flush, 'append_event',
                        lambda task, event: turn_service.record_task_event(task, event))
    flush.flush_deferred_peer_and_steer(task, round_num=9, tid='receipt')
    assert _turn(_snapshot(storage), created)['projection']['_bgCommandInjects'][0]['round'] == 3
    assert '_bgcmd_delivery_event' not in task


def test_lost_ack_rebase_does_not_duplicate_or_erase_receipt(
        turn_service, storage, monkeypatch):  # noqa: F811
    created, task = _seed(turn_service, storage, monkeypatch)
    client = storage.client
    class LostAckClient:
        lost = False
        def command(self, operation, payload, *args, **kwargs):
            result = client.command(operation, payload, *args, **kwargs)
            if operation == 'turn.event.record' and payload.get('background_command_ack') and not self.lost:
                self.lost = True
                raise StorageError('database_timeout', 'lost ACK after commit')
            return result
        def query(self, *args, **kwargs):
            return client.query(*args, **kwargs)
    lost_ack_client = LostAckClient()
    monkeypatch.setattr(turn_service, '_turn_client', lambda **_: lost_ack_client)
    flush.flush_deferred_peer_and_steer(task, round_num=2, tid='receipt')
    # The bounded second commit attempt reuses the receipt without another
    # model call, adopts the committed block and repairs its local mirror.
    assert lost_ack_client.lost
    first = _turn(_snapshot(storage), created)['projection']['_bgCommandInjects']
    assert len(first) == 1
    flush.flush_deferred_peer_and_steer(task, round_num=5, tid='receipt')
    after = _snapshot(storage)
    assert _turn(after, created)['projection']['_bgCommandInjects'] == first
    assert [q['queueId'] for q in after['queueItems']] == ['q-other']
    assert '_bgcmd_inject_pending' not in task


def test_stale_attempt_never_acknowledges_delivery(turn_service, storage, monkeypatch):  # noqa: F811
    created, task = _seed(turn_service, storage, monkeypatch)
    turn_service.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    before = _snapshot(storage)
    flush.flush_deferred_peer_and_steer(task, round_num=2, tid='receipt')
    after = _snapshot(storage)
    assert after['syncSequence'] == before['syncSequence']
    assert {q['queueId'] for q in after['queueItems']} == {'q-bg', 'q-other'}
    assert not _turn(after, created)['projection'].get('_bgCommandInjects')
    assert task['_bgcmd_inject_pending']


def test_mismatched_execution_identity_preserves_both_halves(
        turn_service, storage, monkeypatch):  # noqa: F811
    created, task = _seed(turn_service, storage, monkeypatch)
    task['_bgcmd_inject_pending'][0]['commandId'] = 'different-command'
    before = _snapshot(storage)
    flush.flush_deferred_peer_and_steer(task, round_num=2, tid='receipt')
    after = _snapshot(storage)
    assert after['syncSequence'] == before['syncSequence']
    assert not _turn(after, created)['projection'].get('_bgCommandInjects')
    assert {q['queueId'] for q in after['queueItems']} == {'q-bg', 'q-other'}
    assert task['_bgcmd_inject_pending']
