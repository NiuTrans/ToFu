"""Persistence port for Flow-backed chat turns."""

from pathlib import Path

import pytest

from lib.orchestration.chat_turn_persistence import (
    OrchestrationChatTurnPersistence,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _port(task, calls, *, fail_store=False):
    def store(owner, turns):
        calls.append(('store', owner, len(turns)))
        if fail_store:
            raise RuntimeError('database offline')

    def sync(owner, turns):
        calls.append(('sync', owner, len(turns)))
        return len(turns) - 1

    return OrchestrationChatTurnPersistence(
        task,
        store_turns=store,
        sync_turns=sync,
    )


def test_live_buffer_binding_drives_incremental_and_final_sync():
    task = {'id': 'task-one'}
    calls = []
    persistence = _port(task, calls)
    messages = [{'role': 'assistant', 'content': 'one'}]
    persistence.bind(messages)

    assert persistence(messages[0]) is True
    messages.append({'role': 'user', 'content': 'review'})
    assert persistence(messages[1]) is True
    assert persistence.finalize() is True

    assert [call[0] for call in calls] == [
        'store', 'sync',
        'store', 'sync',
        'store', 'sync',
    ]
    assert all(call[1] is task for call in calls)


def test_unbound_empty_and_rebinding_contracts_are_explicit():
    persistence = _port({'id': 'task-two'}, [])

    assert persistence({'role': 'assistant'}) is False
    assert persistence.finalize() is False
    with pytest.raises(TypeError):
        persistence.bind(())
    first = []
    persistence.bind(first)
    persistence.bind(first)
    with pytest.raises(RuntimeError):
        persistence.bind([])


def test_final_snapshot_failure_remains_nonfatal():
    task = {'id': 'task-three'}
    calls = []
    persistence = _port(task, calls, fail_store=True)
    messages = [{'role': 'assistant', 'content': 'partial'}]
    persistence.bind(messages)

    assert persistence(messages[0]) is False
    assert persistence.finalize() is False
    assert [call[0] for call in calls] == ['store', 'store']


def test_flow_runner_only_assembles_turn_persistence_ports():
    runner = (
        ROOT / 'lib' / 'orchestration' / 'chat_flow_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration' / 'chat_flow_runtime.py').read_text()
    completion = (
        ROOT / 'lib' / 'orchestration' / 'chat_completion.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'OrchestrationChatFlowRuntimePorts' in runtime
    assert 'OrchestrationChatTurnPersistence(' in runtime
    assert 'emit=turn_persistence' in runtime
    assert 'store_turns=ports.store_turns' in runtime
    assert 'turn_persistence.bind(adapter.messages)' in runtime
    assert 'turn_persistence=turn_persistence' in runtime
    assert 'self._turn_persistence.finalize()' in completion
    assert '_adapter_ref' not in runner + runtime


@pytest.mark.parametrize('failure', ['checkpoint', 'commit'])
@pytest.mark.parametrize('final', [False, True])
def test_durable_phase_failure_cannot_ack_or_discard_live_carrier(monkeypatch, failure, final):
    import threading
    from lib.orchestration.graph import FlowEventDeliveryError
    import lib.turn_lifecycle as lifecycle
    task = {'id': 'durable-phase', '_turnId': 'root', '_attemptId': 'attempt',
            'abort_event': threading.Event(), '_flowSyncedCount': 1,
            '_flowLiveTurn': {'turnId': 'child', 'attemptId': 'child-attempt'}}
    messages = [{'role': 'assistant', 'content': 'first'},
                {'role': 'user', 'content': 'partial'}]
    calls = []
    def checkpoint(owner, event):
        calls.append('checkpoint')
        return failure != 'checkpoint'
    def commit(owner, turns):
        calls.append('commit')
        raise RuntimeError('commit ACK lost')
    monkeypatch.setattr(lifecycle, 'record_task_event', checkpoint)
    port = OrchestrationChatTurnPersistence(task,
        store_turns=lambda *args: calls.append('store'), sync_turns=commit)
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError):
        port.finalize() if final else port(messages[-1])
    assert task['abort_event'].is_set()
    assert task['_flowSyncedCount'] == 1
    assert task['_flowLiveTurn']['turnId'] == 'child'
    assert port.messages() is messages
    assert calls == (['store', 'checkpoint'] if failure == 'checkpoint'
                     else ['store', 'checkpoint', 'commit'])


@pytest.mark.parametrize('changed', [False, 'content', 'terminal_metadata', 'identity'])
def test_lost_commit_ack_retries_only_exact_checkpointed_snapshot(monkeypatch, changed):
    import lib.turn_lifecycle as lifecycle
    from lib.orchestration.graph import FlowEventDeliveryError
    task = {'_turnId': 'root', '_attemptId': 'attempt', '_userId': 1,
            '_flowSyncedCount': 1, '_flowLiveTurn': {'turnId': 'child'}}
    messages = [{'content': 'first'}, {'content': 'full'}]
    calls = []
    def checkpoint(*args):
        calls.append('checkpoint')
        return calls.count('checkpoint') == 1
    def sync(owner, turns):
        calls.append('sync')
        if calls.count('sync') == 1:
            raise RuntimeError('ACK lost after commit')
        owner['_flowSyncedCount'] = len(turns)
        owner.pop('_flowLiveTurn')
    monkeypatch.setattr(lifecycle, 'record_task_event', checkpoint)
    port = OrchestrationChatTurnPersistence(task, store_turns=lambda *a: None, sync_turns=sync,
        retry_commit=lifecycle.retry_turn_commit)
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError):
        port.finalize()
    if changed:
        if changed == 'content':
            messages[-1]['content'] = 'different'
        elif changed == 'terminal_metadata':
            messages[-1]['finishReason'] = 'aborted'
        else:
            task['_flowLiveTurn']['attemptId'] = 'replacement-child'
        with pytest.raises(FlowEventDeliveryError):
            port.finalize()
        assert calls == ['checkpoint', 'sync', 'checkpoint']
    else:
        assert port.finalize()
        assert calls == ['checkpoint', 'sync', 'sync']
        assert task['_flowSyncedCount'] == 2


@pytest.mark.parametrize('final', [False, True])
def test_missing_durable_commit_ack_blocks_phase_success(monkeypatch, final):
    import threading
    from lib.orchestration.graph import FlowEventDeliveryError
    task = {'_turnId': 'root', '_attemptId': 'attempt',
            '_flowLiveTurn': {'turnId': 'child'}, 'abort_event': threading.Event()}
    monkeypatch.setattr('lib.turn_lifecycle.record_task_event', lambda *args: True)
    port = OrchestrationChatTurnPersistence(
        task, store_turns=lambda *args: None, sync_turns=lambda *args: None)
    messages = [{'role': 'assistant', 'content': 'uncommitted'}]
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError, match='not acknowledged'):
        port.finalize() if final else port(messages[0])
    assert task['abort_event'].is_set()
    assert task['_flowLiveTurn'] == {'turnId': 'child'}
    assert port.messages() is messages


class _NeverAbort:
    def __init__(self):
        self.set_calls = 0

    def wait(self, timeout=None):
        return False

    def is_set(self):
        return False

    def set(self):
        self.set_calls += 1


@pytest.mark.parametrize('final', [False, True])
def test_transient_storage_stall_replays_the_checkpointed_snapshot(monkeypatch, final):
    """A deadline_elapsed from the authority earns a bounded in-place retry.

    The replay fence skips the already-checkpointed projection, so only the
    sync owner runs again; a non-transient verdict would raise unchanged.
    """
    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError
    task = {'id': 'transient-stall', '_turnId': 'root', '_attemptId': 'attempt',
            '_userId': 1, '_flowSyncedCount': 1,
            '_flowLiveTurn': {'turnId': 'child'}, 'abort_event': _NeverAbort()}
    messages = [{'content': 'first'}, {'content': 'full'}]
    calls = []
    def checkpoint(*args):
        calls.append('checkpoint')
        return True
    def sync(owner, turns):
        calls.append('sync')
        if calls.count('sync') == 1:
            raise StorageError('database_timeout', 'deadline_elapsed', False)
        owner['_flowSyncedCount'] = len(turns)
        owner.pop('_flowLiveTurn')
    monkeypatch.setattr(lifecycle, 'record_task_event', checkpoint)
    port = OrchestrationChatTurnPersistence(
        task, store_turns=lambda *a: None, sync_turns=sync,
        retry_commit=lifecycle.retry_turn_commit)
    port.bind(messages)
    assert (port.finalize() if final else port(messages[-1])) is True
    assert calls == ['checkpoint', 'sync', 'sync']
    assert task['_flowSyncedCount'] == 2


def test_transient_storage_stall_exhaustion_still_fails_closed(monkeypatch):
    import lib.turn_lifecycle as lifecycle
    from lib.orchestration.graph import FlowEventDeliveryError
    from lib.storage.errors import StorageError
    task = {'id': 'transient-down', '_turnId': 'root', '_attemptId': 'attempt',
            '_userId': 1, '_flowSyncedCount': 1,
            '_flowLiveTurn': {'turnId': 'child'}, 'abort_event': _NeverAbort()}
    messages = [{'content': 'full'}]
    monkeypatch.setattr(lifecycle, 'record_task_event', lambda *args: True)
    calls = []
    def sync(owner, turns):
        calls.append('sync')
        raise StorageError('database_timeout', 'deadline_elapsed', False)
    port = OrchestrationChatTurnPersistence(
        task, store_turns=lambda *a: None, sync_turns=sync,
        retry_commit=lifecycle.retry_turn_commit)
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError, match='Could not commit'):
        port.finalize()
    # Bounded retries then the designed fail-closed path: the live carrier
    # stays put for recovery, nothing is acknowledged or discarded.
    assert calls == ['sync'] * 4
    assert task['abort_event'].set_calls == 1
    assert task['_flowLiveTurn'] == {'turnId': 'child'}
    assert port.messages() is messages


def test_non_transient_commit_failure_is_not_retried(monkeypatch):
    import lib.turn_lifecycle as lifecycle
    from lib.orchestration.graph import FlowEventDeliveryError
    task = {'id': 'verdict', '_turnId': 'root', '_attemptId': 'attempt',
            '_userId': 1, '_flowSyncedCount': 1,
            '_flowLiveTurn': {'turnId': 'child'}, 'abort_event': _NeverAbort()}
    messages = [{'content': 'full'}]
    monkeypatch.setattr(lifecycle, 'record_task_event', lambda *args: True)
    calls = []
    def sync(owner, turns):
        calls.append('sync')
        raise RuntimeError('commit ACK lost')
    port = OrchestrationChatTurnPersistence(
        task, store_turns=lambda *a: None, sync_turns=sync,
        retry_commit=lifecycle.retry_turn_commit)
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError):
        port.finalize()
    assert calls == ['sync']


def test_lost_ack_retains_only_unacknowledged_suffix(monkeypatch):
    """Large committed history is not recopied for every new phase."""
    import lib.turn_lifecycle as lifecycle
    from lib.orchestration.graph import FlowEventDeliveryError

    class CommittedPayload(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('committed payload must not be recopied')

    task = {'_turnId': 'root', '_attemptId': 'attempt', '_userId': 1,
            '_flowSyncedCount': 64, '_flowLiveTurn': {'turnId': 'child'}}
    messages = [CommittedPayload(content='x' * 8000) for _ in range(64)]
    messages.append({'content': 'unacknowledged'})
    checkpoints, calls = [], []
    monkeypatch.setattr(lifecycle, 'record_task_event', lambda *a: checkpoints.append(1) or True)

    def sync(owner, turns):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('commit ACK lost')
        owner['_flowSyncedCount'] = len(turns)
        owner.pop('_flowLiveTurn')

    port = OrchestrationChatTurnPersistence(task, store_turns=lambda *a: None, sync_turns=sync)
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError):
        port.finalize()
    assert port._pending_commit[1] == [{'content': 'unacknowledged'}]
    assert port._pending_commit[1][0] is not messages[-1]
    assert port.finalize()
    assert len(checkpoints) == 1
    assert len(calls) == 2
    assert port._pending_commit is None


def test_fully_synced_final_replay_still_fences_last_phase():
    from lib.orchestration.graph import FlowEventDeliveryError

    task = {'_turnId': 'root', '_attemptId': 'attempt', '_userId': 1,
            '_flowSyncedCount': 2}
    messages = [{'content': 'committed prefix'}, {'content': 'last phase'}]
    calls = []

    def sync(owner, turns):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('final ACK lost')
        assert port._pending_commit[1] == [{'content': 'changed after lost ACK'}]
        return True

    port = OrchestrationChatTurnPersistence(task, store_turns=lambda *a: None, sync_turns=sync)
    port.bind(messages)
    with pytest.raises(FlowEventDeliveryError):
        port.finalize()
    assert port._pending_commit[1] == [{'content': 'last phase'}]
    messages[-1]['content'] = 'changed after lost ACK'
    assert port.finalize()
    assert port._pending_commit is None
