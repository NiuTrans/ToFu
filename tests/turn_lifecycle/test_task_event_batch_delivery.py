"""Queued structural task events reach the Turn authority in batch transactions.

A backlog that ``append_event`` accepted behind a slow head persists through
the delivery FIFO's batch port: one ``turn.event.record_batch`` transaction
per storage-event budget chunk instead of one ``turn.event.record`` per event,
leaving the rows, revisions and sync events of the sequential path, and every
frame is pushed only after its commit. Outcomes map back like the single
path: a stale attempt revokes the whole frozen run, a live rejection retains
it for repair. Both authorities run every specification.
"""
import threading
import time
from dataclasses import replace

import pytest

from lib.agent_core import push
from lib.storage.errors import StorageError
from lib.tasks_pkg import event_delivery
from lib.tasks_pkg.manager import _events as manager
from tests.turn_lifecycle.test_task_event_delivery import (
    _task, _discard, storage as storage, turn_service as turn_service,
    owner as owner,
)
from tests.turn_lifecycle.test_turn_event_carried_task_event import _stored_event
from tests.turn_lifecycle.test_turn_event_record_batch import (
    _rows, _sync_changes, _sync_sequence, _turn,
)

pytestmark = pytest.mark.unit


class SlowTurnAuthority:
    """Slows every Turn event transaction, records commits and can hold the first."""

    def __init__(self, storage, monkeypatch, *, delay=0.0, hold=None):
        self.order, self.refused = [], []
        self.held, self.release = threading.Event(), threading.Event()
        original = storage.client.command
        pending_hold = [hold]

        def command(operation, payload, *args, **kwargs):
            if operation not in ('turn.event.record', 'turn.event.record_batch'):
                return original(operation, payload, *args, **kwargs)
            hold_here, pending_hold[0] = pending_hold[0], None
            if delay:
                time.sleep(delay)
            if hold_here == 'before_commit':
                self.held.set()
                assert self.release.wait(10)
            try:
                result = original(operation, payload, *args, **kwargs)
            except StorageError as exc:
                self.refused.append((operation, exc.code))
                raise
            entries = payload['entries'] if operation == 'turn.event.record_batch' else [payload]
            self.order.append((operation, [(entry.get('task_event') or {}).get('sequence')
                                           for entry in entries], payload))
            if hold_here == 'after_commit':
                self.held.set()
                assert self.release.wait(10)
            return result

        monkeypatch.setattr(storage.client, 'command', command)

    def commands(self, operation):
        return [sequences for name, sequences, _ in self.order if name == operation]


def _observe_pushes(storage, task, monkeypatch):
    """Every push of this task's frames, each checked durable at push time."""
    pushed, early = [], []

    def observe(channel, task_id, wire, **kwargs):
        if task_id != task['id']:
            return
        if _stored_event(storage, task_id, wire['seq']) is None:
            early.append(wire['seq'])
        pushed.append(wire['seq'])

    monkeypatch.setattr(push, 'push_event', observe)
    return pushed, early


def _emit_structural(task, first, count):
    """Append ``count`` structural frames; tool frames change the tool rows."""
    for index in range(first, first + count):
        round_num, step = index // 3 + 1, index % 3
        if step == 0:
            manager.append_event(task, {'type': 'phase', 'phase': 'tool',
                                        'detail': f'round {round_num}'})
            continue
        status = 'running' if step == 1 else 'done'
        rows = [row for row in task['toolRounds'] if row['roundNum'] != round_num]
        task['toolRounds'] = rows + [{
            'roundNum': round_num, 'toolCallId': f'call-{round_num}', 'toolName': 'read_file',
            'toolArgs': '{"path": "a.py"}', 'status': status,
            **({'toolContent': f'result {round_num}'} if status == 'done' else {})}]
        manager.append_event(task, {
            'type': 'tool_start' if step == 1 else 'tool_result', 'roundNum': round_num,
            'toolName': 'read_file', 'toolCallId': f'call-{round_num}', 'query': 'a.py',
            **({'results': [], 'status': 'done'} if step == 2 else {})})


@pytest.mark.parametrize('chunk', [None, 16])
def test_a_queued_structural_backlog_commits_in_one_transaction_per_budget_chunk(
        turn_service, storage, owner, monkeypatch, request, chunk):
    from lib.storage import storage_event_policy
    if chunk is not None:
        budget = replace(storage_event_policy.resolve_storage_event_budget(),
                         batch_max_events=chunk)
        monkeypatch.setattr(storage_event_policy, 'resolve_storage_event_budget',
                            lambda *args, **kwargs: budget)
    _, task = _task(turn_service, storage, 'batch-backlog')
    try:
        base = _turn(turn_service, task)['projectionRevision']
        sync_before = _sync_sequence(storage, task)
        authority = SlowTurnAuthority(storage, monkeypatch, delay=.02, hold='before_commit')
        pushed, early = _observe_pushes(storage, task, monkeypatch)
        _emit_structural(task, 0, 1)
        assert authority.held.wait(5)
        _emit_structural(task, 1, 63)
        costs = []
        # The barrier fixes its 64 awaited events before the head commits.
        assert event_delivery.flush_task_events(
            task, slow_after=0, on_slow=lambda pending: authority.release.set(),
            on_release=costs.append) == 64
        assert authority.commands('turn.event.record') == [[0]]
        sizes = [len(sequences) for sequences in authority.commands('turn.event.record_batch')]
        assert [sequence for sequences in authority.commands('turn.event.record_batch')
                for sequence in sequences] == list(range(1, 64))
        if request.node.callspec.params['storage'] == 'sqlite':
            assert authority.refused == []
            assert sizes == ([63] if chunk is None else [16, 16, 16, 15])
        else:
            # Native TofuDB refuses a composite commit that no longer fits one
            # ordinary commit. Only that interval splits: independently sized
            # siblings still preserve the exact FIFO rows checked above.
            assert {code for _, code in authority.refused} <= {'database_resource_exhausted'}
            assert all(0 < size <= (chunk or 63) for size in sizes)
        [cost] = costs
        assert cost.persist_calls == 2
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(64))
        turn = _turn(turn_service, task)
        assert turn['projectionRevision'] == base + 64
        assert [change['type'] for change in _sync_changes(storage, task, sync_before)] == [
            'attempt.event'] * 64
        assert [row['roundNum'] for row in turn['projection']['toolRounds']] == list(range(1, 22))
        assert pushed == list(range(64)) and early == []
        snapshot = owner.snapshot()
        assert snapshot['pending_events'] == snapshot['pending_batches'] == 0
        assert not task.get('aborted') and '_pushWithheldAt' not in task
    finally:
        _discard(task)


def test_a_lost_batch_acknowledgement_reconciles_the_frozen_run_exactly_once(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-lost-ack')
    try:
        base = _turn(turn_service, task)['projectionRevision']
        sync_before = _sync_sequence(storage, task)
        authority = SlowTurnAuthority(storage, monkeypatch, hold='before_commit')
        committed = storage.client.command
        lost = []

        def lose_first_batch_ack(operation, payload, *args, **kwargs):
            result = committed(operation, payload, *args, **kwargs)
            if operation == 'turn.event.record_batch' and not lost:
                lost.append(payload)
                raise StorageError('database_timeout', 'injected lost batch acknowledgement', True)
            return result

        monkeypatch.setattr(storage.client, 'command', lose_first_batch_ack)
        pushed, early = _observe_pushes(storage, task, monkeypatch)
        _emit_structural(task, 0, 1)
        assert authority.held.wait(5)
        _emit_structural(task, 1, 4)
        authority.release.set()
        event_delivery.flush_task_events(task)
        # The retry read the exact committed rows instead of resending them.
        assert len(lost) == 1
        assert authority.commands('turn.event.record_batch') == [[1, 2, 3, 4]]
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(5))
        assert _turn(turn_service, task)['projectionRevision'] == base + 5
        assert len(_sync_changes(storage, task, sync_before)) == 5
        assert pushed == list(range(5)) and early == []
        assert owner.snapshot()['pending_events'] == 0
    finally:
        _discard(task)


def test_a_stale_attempt_revokes_the_whole_frozen_run_without_publishing_it(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-stale-run')
    try:
        authority = SlowTurnAuthority(storage, monkeypatch, hold='after_commit')
        pushed, _ = _observe_pushes(storage, task, monkeypatch)
        _emit_structural(task, 0, 1)
        assert authority.held.wait(5)
        _emit_structural(task, 1, 5)
        # The attempt settles behind this executor while its head is in flight.
        stale = {**task}
        stale.pop('_turnProjectionState', None)
        stale.pop('_turnProjectionStateLock', None)
        stale['status'] = 'done'
        try:
            assert turn_service.record_task_event(stale, {'type': 'done', 'finishReason': 'stop'})
        finally:
            authority.release.set()
        event_delivery.flush_task_events(task)
        assert authority.commands('turn.event.record') == [[0], [None]]
        assert authority.commands('turn.event.record_batch') == [[1, 2, 3, 4, 5]]
        assert pushed == [0]
        assert task['aborted'] and task['_eventDeliveryRevoked']
        assert [row['sequence'] for row in _rows(storage, task)] == [0]
        snapshot = owner.snapshot()
        assert snapshot['pending_events'] == snapshot['pending_bytes'] == 0
        assert snapshot['revoked_events'] == 5
    finally:
        _discard(task)


def test_a_live_rejection_retains_the_exact_frozen_run_for_repair(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-live-rejection')
    try:
        authority = SlowTurnAuthority(storage, monkeypatch, hold='before_commit')
        pushed, early = _observe_pushes(storage, task, monkeypatch)
        original = turn_service.record_task_events
        rejecting = True
        attempts = []

        def record(task, entries):
            attempts.append(list(entries))
            return [False] * len(entries) if rejecting else original(task, entries)

        monkeypatch.setattr(turn_service, 'record_task_events', record)
        _emit_structural(task, 0, 1)
        assert authority.held.wait(5)
        _emit_structural(task, 1, 3)
        authority.release.set()
        lane = task['_eventDeliveryLane']
        with pytest.raises(event_delivery.EventDeliveryError):
            owner.flush(lane, recover=False)
        assert owner.snapshot()['pending_events'] == 3
        assert not task.get('_eventDeliveryRevoked') and not task.get('aborted')
        assert task['_pushWithheldCount'] == 1
        assert [row['sequence'] for row in _rows(storage, task)] == [0]
        rejecting = False
        event_delivery.flush_task_events(task)
        assert len(attempts) == 2
        assert all(first is retried for first, retried in zip(*attempts))
        assert [row['sequence'] for row in _rows(storage, task)] == [0, 1, 2, 3]
        assert authority.commands('turn.event.record_batch') == [[1, 2, 3]]
        assert pushed == [0, 1, 2, 3] and early == []
        assert owner.snapshot()['pending_events'] == 0 and '_pushWithheldAt' not in task
    finally:
        _discard(task)


def test_progress_frames_inside_a_batch_are_each_committed_and_published(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-progress')
    try:
        authority = SlowTurnAuthority(storage, monkeypatch, hold='before_commit')
        pushed, early = _observe_pushes(storage, task, monkeypatch)
        _emit_structural(task, 0, 1)
        assert authority.held.wait(5)
        _emit_structural(task, 1, 1)
        for text in ('a', 'b', 'c'):
            task['content'] += text
            manager.append_event(task, {'type': 'delta', 'content': text})
        _emit_structural(task, 5, 1)
        authority.release.set()
        event_delivery.flush_task_events(task)
        assert authority.commands('turn.event.record_batch') == [[1, 4, 5]]
        [batch] = [payload for name, _, payload in authority.order
                   if name == 'turn.event.record_batch']
        assert [row['sequence'] for row in batch['entries'][1]['task_event']['preceding_events']
                ] == [2, 3]
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(6))
        assert [row['event'].get('content') for row in _rows(storage, task)[2:5]] == ['a', 'b', 'c']
        assert _turn(turn_service, task)['projection']['content'] == task['content']
        assert pushed == list(range(6)) and early == []
    finally:
        _discard(task)


def test_a_terminal_barrier_commits_the_queued_run_before_its_own_transaction(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'batch-terminal')
    authority = SlowTurnAuthority(storage, monkeypatch, hold='before_commit')
    finished = threading.Event()

    def finish():
        task.update(status='done', finishReason='stop')
        manager.append_event(task, {'type': 'done', 'finishReason': 'stop'})
        finished.set()

    terminal = threading.Thread(target=finish)
    try:
        _emit_structural(task, 0, 1)
        assert authority.held.wait(5)
        _emit_structural(task, 1, 3)
        terminal.start()
        # The terminal barrier drains the queued run before its own record.
        assert not finished.wait(.05)
        authority.release.set()
        terminal.join(10)
        assert finished.is_set()
        assert [(name, sequences) for name, sequences, _ in authority.order] == [
            ('turn.event.record', [0]), ('turn.event.record_batch', [1, 2, 3]),
            ('turn.event.record', [4])]
        assert _turn(turn_service, task)['status'] == 'completed'
        assert [row['sequence'] for row in _rows(storage, task)] == list(range(5))
    finally:
        authority.release.set()
        if terminal.is_alive():
            terminal.join(10)
        _discard(task)
