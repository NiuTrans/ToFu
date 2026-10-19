"""An effect handler enters only after its exact intent crosses the selected store."""
import json
import time

import pytest

from tests.turn_lifecycle.test_task_event_delivery import (
    storage as storage, turn_service as turn_service, owner as owner, _task, _discard, _stored_event,
)
from tests.tools.test_tool_effect_recovery import invoke
from lib.tasks_pkg import event_delivery
from lib.tasks_pkg.executor._effects import begin_tool_effect, ToolEffectDurabilityError
from lib.tasks_pkg.manager import _events as manager

pytestmark = pytest.mark.unit


def make_row(task):
    row = dict(roundNum=1, toolName='perform', toolCallId='call', status='running',
               taskId=task['id'], attemptId=task['_attemptId'])
    task['toolRounds'] = [row]
    return row


def stored_effect(turn_service, task):
    turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
    return turn['projection']['toolRounds'][0]['executionEffect']


def test_intent_is_durable_at_handler_entry_and_lost_reply_is_not_replayed(
        storage, turn_service, owner):
    _, task = _task(turn_service, storage, 'effect-enter')
    row, entered = make_row(task), []
    def handler(*args):
        persisted = stored_effect(turn_service, task)
        assert persisted == row['executionEffect']
        assert persisted['state'] == 'entered'
        assert _stored_event(storage, task['id'], 0)['event']['executionEffect'] == persisted
        entered.append(persisted['operationId'])
        raise TimeoutError('remote effect committed but reply lost')
    try:
        result = invoke(task, row, handler)
        event_delivery.flush_task_events(task)
        assert len(entered) == 1
        assert stored_effect(turn_service, task)['state'] == 'unknown'
        assert json.loads(result[1])['error']['retryable'] is False
    finally:
        _discard(task)


@pytest.mark.parametrize('fault', ['before_commit', 'lost_ack', 'coalesced', 'suppressed', 'unrecognized_ack'])
def test_no_durable_ack_means_no_handler_entry(storage, turn_service, owner, monkeypatch, fault):
    _, task = _task(turn_service, storage, 'effect-fault')
    row, entered = make_row(task), []
    original = turn_service.record_task_event
    def record(task, wire, *args, **kwargs):
        if wire['type'] != 'tool_effect':
            return original(task, wire, *args, **kwargs)
        if fault in ('coalesced', 'unrecognized_ack'):
            return 'coalesced' if fault == 'coalesced' else {'unexpected': True}
        if fault == 'lost_ack':
            original(task, wire, *args, **kwargs)
        raise RuntimeError('injected effect intent failure')
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    # Keep the exact failed-entry witness on disk; a final result frame would
    # legitimately explain not_started, obscuring this crash-window assertion.
    monkeypatch.setattr('lib.tasks_pkg.executor._execute._finalize_tool_round', lambda *a, **kw: None)
    if fault == 'suppressed':
        task['_suppressEvents'] = True
    try:
        invoke(task, row, lambda *args: entered.append('unsafe'))
        assert entered == []
        assert row['executionEffect']['state'] == 'not_started'
        stored = _stored_event(storage, task['id'], 0)
        if fault == 'lost_ack':
            assert stored_effect(turn_service, task)['state'] == 'entered'
            assert stored['event']['executionEffect']['operationId'] == row['executionEffect']['operationId']
        elif fault not in ('coalesced', 'unrecognized_ack'):
            assert stored is None
    finally:
        _discard(task)


def test_oversize_circuit_cannot_drop_effect_evidence(storage, turn_service, owner):
    _, task = _task(turn_service, storage, 'effect-oversize')
    row = make_row(task)
    task['_turnProjectionOversizeRetryAt'] = time.monotonic() + 3600
    try:
        begin_tool_effect(task, 'perform', 'call', 1, row)
        assert stored_effect(turn_service, task) == row['executionEffect']
    finally:
        _discard(task)


def test_stop_during_durable_barrier_never_enters_handler(storage, turn_service, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'effect-stop')
    row, entered = make_row(task), []
    original = turn_service.record_task_event
    def record(task, wire, *args, **kwargs):
        result = original(task, wire, *args, **kwargs)
        if wire['type'] == 'tool_effect':
            task['aborted'] = True
            task['abort_event'].set()
        return result
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    try:
        invoke(task, row, lambda *args: entered.append('unsafe'))
        event_delivery.flush_task_events(task)
        assert entered == []
        assert row['executionEffect']['state'] == 'not_started'
    finally:
        _discard(task)


def test_transient_execution_never_requests_a_store(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a: pytest.fail('transient store access'))
    row = {}
    begin_tool_effect({'id': 'transient'}, 'perform', 'call', 1, row)
    assert row['executionEffect']['scope'] == 'process'


def test_cold_storage_restart_preserves_entered_operation(storage, turn_service, owner):
    _, task = _task(turn_service, storage, 'effect-cold')
    row = make_row(task)
    begin_tool_effect(task, 'perform', 'call', 1, row)
    original = dict(row['executionEffect'])
    conv_id, turn_id = task['convId'], task['_turnId']
    _discard(task)
    storage.stop()
    storage.start()
    # Read through a new authority process/client, without the task or projection cache.
    recovered = turn_service.get_turn(conv_id, turn_id, user_id=1)
    assert recovered['projection']['toolRounds'][0]['executionEffect'] == original
    assert original['state'] == 'entered'


def test_previous_sibling_commit_cannot_attest_a_refused_effect(storage, turn_service, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'effect-sibling')
    row = make_row(task)
    manager.append_event(task, {'type': 'phase', 'phase': 'calling'})
    event_delivery.flush_task_events(task)
    assert _stored_event(storage, task['id'], 0) is not None
    original = turn_service.record_task_event
    def record(task, wire, *args, **kwargs):
        if wire['type'] == 'tool_effect':
            raise RuntimeError('current intent refused')
        return original(task, wire, *args, **kwargs)
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    try:
        with pytest.raises(ToolEffectDurabilityError):
            begin_tool_effect(task, 'perform', 'call', 1, row)
        assert row['executionEffect']['state'] == 'not_started'
        assert _stored_event(storage, task['id'], 1) is None
    finally:
        _discard(task)
