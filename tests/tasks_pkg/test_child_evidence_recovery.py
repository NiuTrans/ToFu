"""Child evidence survives ambiguous commits without replaying model/tool work."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg.child_invocation import create_child_invocation, run_child_invocation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('committed', [False, True])
@pytest.mark.parametrize('stop_during_wait', [False, True])
def test_child_snapshot_retry_keeps_exact_evidence_and_completes(
        monkeypatch, committed, stop_during_wait):
    from lib.tasks_pkg import child_invocation, event_log, snapshot_delta
    from lib.tasks_pkg.manager import append_event

    received = []
    child = create_child_invocation(
        {'id': 'recovery-parent', 'convId': 'recovery-conversation', '_userId': 71,
         '_transientRuntime': True, 'config': {'memoryEnabled': False}},
        model='fixture-model', tools=[], objective='Recover committed evidence',
        event_sink=lambda event: received.append(copy.deepcopy(event)), abort_check=lambda: False)
    # Exercise the actual evidence writer without unrelated archive admission.
    child['_invocationStorageFree'] = False
    projector = snapshot_delta.SnapshotProjector()
    monkeypatch.setattr(snapshot_delta, 'get_projector', lambda: projector)
    monkeypatch.setattr(snapshot_delta, 'forget_projector_task', projector.forget)
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0)

    rows = {}
    attempts = []
    failures_remaining = 2  # Both the batch and its direct append lose their ACK.

    def write(*, task_id, sequence, event, **kwargs):
        nonlocal failures_remaining
        assert task_id == child['_inspectorTaskId']
        frozen = json.dumps(event, sort_keys=True)
        attempts.append((sequence, frozen))
        previous = rows.get(sequence)
        if previous is not None and previous != frozen:
            raise StorageError('database_conflict', 'event key has a different payload')
        if sequence == 1 and failures_remaining:
            failures_remaining -= 1
            if committed:
                rows[sequence] = frozen
            if stop_during_wait:
                child['abort_event'].set()
                child['aborted'] = True
            raise StorageError('database_timeout', 'deadline_elapsed')
        rows[sequence] = frozen
        return {'inserted': previous is None}

    class Client:
        def command(self, operation, payload, command_id, **kwargs):
            assert operation == 'event.append'
            return write(**payload)

    monkeypatch.setattr(event_log, '_ensure_sidecar_batcher',
                        lambda: SimpleNamespace(append=write))
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: Client())
    tools = [{'type': 'function', 'function': {'name': 'read_files'}}]
    messages = [{'role': 'user', 'content': 'Original request'}]
    emitted = []
    executions = []

    def execute(task, *, policy):
        executions.append(task['id'])
        for round_num in range(1, 4):
            event = {'type': 'messages_snapshot', 'kind': 'request',
                     'model': 'fixture-model', 'label': f'Request {round_num}',
                     'roundNum': round_num, 'messages': copy.deepcopy(messages),
                     'tools': copy.deepcopy(tools)}
            append_event(task, event)
            emitted.append(copy.deepcopy(event))
            messages.append({'role': 'assistant', 'content': f'Completed work {round_num}'})
        task.update(status='done', finishReason='aborted' if stop_during_wait else 'stop')

    monkeypatch.setattr(child_invocation, 'execute_invocation', execute)
    assert run_child_invocation(child) is child
    assert executions == [child['id']]
    assert not child.get('_invocationDeliveryError')
    assert child['abort_event'].is_set() is stop_during_wait
    assert not child.get('_storageWaits')
    replayed = [payload for sequence, payload in attempts if sequence == 1]
    assert len(replayed) == 3 and len(set(replayed)) == 1
    assert len(rows) == 3
    rebuilt = snapshot_delta.rebuild_snapshots([
        {'type': 'messages_snapshot', 'payload': json.loads(payload)}
        for _, payload in sorted(rows.items())])
    assert [row['messages'] for row in rebuilt] == [e['messages'] for e in emitted]
    assert all(row['tools'] == tools for row in rebuilt)
    assert not any(row.get('degraded') for row in rebuilt)
    delivered = [e for e in received if e['type'] == 'messages_snapshot']
    assert [e['messages'] for e in delivered] == [e['messages'] for e in emitted]
    assert [e['seq'] for e in delivered] == [0, 1, 2]


@pytest.mark.parametrize('code', ['database_conflict', 'database_integrity', 'database_forbidden'])
def test_child_failure_retains_the_authority_verdict_in_technical_details(monkeypatch, code):
    from lib.tasks_pkg import child_invocation, event_log, invocation

    child = create_child_invocation(
        {'id': 'failed-parent', 'convId': 'failed-conversation', '_userId': 71,
         '_transientRuntime': True, 'config': {'memoryEnabled': False}},
        model='fixture-model', tools=[], objective='Preserve failure diagnostics',
        event_sink=lambda event: None, abort_check=lambda: False)
    child['_invocationStorageFree'] = False
    failure = StorageError(code, 'authority refused evidence', operation_id='evidence-operation')
    attempts = []

    def refuse(*args):
        attempts.append(1)
        raise event_log.EventDurabilityError('event was not durable') from failure

    monkeypatch.setattr(event_log, 'append_persistent_event', refuse)

    def execute(task, *, policy):
        # The append is accepted and refused on the child's evidence lane; the
        # handoff itself never raises. The terminal settle retries the exact
        # head once, then retains the verdict and its detail for the scheduler/UI.
        from lib.tasks_pkg.event_delivery import coordinator, task_event_backlog
        invocation.deliver_invocation_event(task, {'type': 'messages_snapshot', 'seq': 4})
        owner = coordinator()
        with owner.condition:
            assert owner.condition.wait_for(lambda: task_event_backlog(task).failed, timeout=2)
        task.update(status='done', finishReason='stop')

    monkeypatch.setattr(child_invocation, 'execute_invocation', execute)
    with pytest.raises(RuntimeError, match='Child invocation evidence delivery failed') as caught:
        run_child_invocation(child)
    assert code in str(caught.value) and 'evidence-operation' in str(caught.value)
    assert caught.value.__cause__.__cause__ is failure
    assert attempts == [1, 1] and child['abort_event'].is_set()
    assert '_invocationEvidenceLane' not in child


def test_child_unprintable_evidence_failure_preserves_original_cause(monkeypatch):
    from lib.tasks_pkg import child_invocation

    class UnprintableFailure(RuntimeError):
        def __str__(self):
            raise ValueError('broken diagnostic formatter')

    child = create_child_invocation(
        {'id': 'unprintable-parent', 'convId': '', '_userId': 71,
         '_transientRuntime': True, 'config': {'memoryEnabled': False}},
        model='fixture-model', tools=[], objective='Preserve failure identity',
        event_sink=lambda event: None, abort_check=lambda: False)
    failure = UnprintableFailure()

    def execute(task, *, policy):
        task.update(status='done', finishReason='stop', _invocationDeliveryError=failure)

    monkeypatch.setattr(child_invocation, 'execute_invocation', execute)
    with pytest.raises(RuntimeError, match='Child invocation evidence delivery failed') as caught:
        run_child_invocation(child)
    assert caught.value.__cause__ is failure
