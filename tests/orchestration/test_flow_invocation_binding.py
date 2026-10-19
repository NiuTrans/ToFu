"""A Flow admission locates its actual registered child before paid/effect work."""
from copy import deepcopy

import pytest

from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused, read_journal
from tests.orchestration.test_orchestration_execution_recovery import graph, role, Crash
from tests.turn_lifecycle.test_task_event_delivery import (
    storage as storage, turn_service as turn_service, owner as owner, _task, _discard,
)

pytestmark = pytest.mark.unit


def test_committed_agent_receipt_preserves_its_admitted_invocation():
    definition, events = graph(role('worker')), []
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    journal.begin(definition, 'input')
    def runner(node, context, iteration, *, before_dispatch):
        before_dispatch(invocation_id='registered-child')
        return {'output': 'done', 'status': 'completed'}
    journal.runner(runner, prepare_before_admission=True)(role('worker'), 'input', 0)
    started = next(event for event in events if event['type'] == 'execution_effect_started')
    completed = next(event for event in events if event['type'] == 'execution_effect_completed')
    assert started['invocation_id'] == completed['invocation_id'] == 'registered-child'
    records, parent = read_journal(events, definition=definition, initial_context='input')
    assert not parent and records[started['key']]['invocation_id'] == 'registered-child'
    completed['invocation_id'] = 'different-child'
    with pytest.raises(RecoveryRefused):
        read_journal(events, definition=definition, initial_context='input')


@pytest.mark.parametrize('fail_admission', [False, True])
def test_actual_registered_child_is_bound_before_execution(
        storage, turn_service, owner, monkeypatch, fail_admission):
    import lib.tasks_pkg.child_invocation as children
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    _, parent = _task(turn_service, storage, 'flow-child-binding')
    definition, events, entered = graph(role('worker')), [], []
    store = SidecarOrchestrationRunStore(1, client=lambda **_: storage.client)
    run_id = store.new_run_id()
    assert store.create_run(run_id, definition=definition, input_text='input')
    def emit(event):
        assert store.append_event(run_id, len(events) + 1, event)
        events.append(deepcopy(event))
        if fail_admission and event['type'] == 'execution_effect_started':
            raise OSError('lost Flow admission acknowledgement')
    journal = ExecutionJournal(emit)
    journal.begin(definition, 'input')
    def execute(child, *, policy):
        started = next(event for event in events if event['type'] == 'execution_effect_started')
        assert started['invocation_id'] == child['id']
        identity = storage.client.query('invocation.resolve_child', {
            'user_id': 1, 'archive_scope': child['_archiveScope']})
        assert identity['parent_task_id'] == parent['id']
        assert identity['child_task_id'] == started['invocation_id']
        entered.append(child['id'])
        raise Crash()
    monkeypatch.setattr(children, 'execute_invocation', execute)
    engine = FlowExecutor(definition, parent_task=parent, model='fixture',
        model_routing_policy='selected', on_event=emit, execution_journal=journal)
    try:
        with pytest.raises((Crash, RecoveryRefused)):
            engine.run(initial_context='input')
        assert bool(entered) is not fail_admission
        # Reopen the durable authority and its owner-scoped adapter. The witness
        # survives a lost ACK as well as a worker that entered model execution.
        storage.stop()
        storage.start()
        reopened = SidecarOrchestrationRunStore(1, client=lambda **_: storage.client)
        persisted = reopened.get_events(run_id)
        started = next(event for event in persisted
                       if event['type'] == 'execution_effect_started')
        assert started['invocation_id']
        assert next(event for event in events
                    if event['type'] == 'execution_effect_started')['invocation_id'] == started['invocation_id']
        other = SidecarOrchestrationRunStore(2, client=lambda **_: storage.client)
        if storage.status()['backend'] == 'tofudb':
            from lib.orchestration.run_store_port import OrchestrationRunStoreError
            with pytest.raises(OrchestrationRunStoreError) as denied:
                other.get_events(run_id)
            assert denied.value.__cause__.code == 'database_forbidden'
        else:
            assert other.get_events(run_id) == []
        # A locator alone never turns an interrupted agent into a replayable result.
        with pytest.raises(RecoveryRefused):
            read_journal(persisted, definition=definition, initial_context='input')
    finally:
        _discard(parent)


@pytest.mark.parametrize('locator', ['', 12, 'x' * 513])
def test_malformed_locator_never_reaches_agent_body(locator):
    definition, events, entered = graph(role('worker')), [], []
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    journal.begin(definition, 'input')
    def runner(node, context, iteration, *, before_dispatch):
        before_dispatch(invocation_id=locator)
        entered.append(True)
        return {'output': 'done', 'status': 'completed'}
    with pytest.raises(RecoveryRefused):
        journal.runner(runner, prepare_before_admission=True)(role('worker'), 'input', 0)
    assert entered == []
    assert not any(event['type'] == 'execution_effect_started' for event in events)


@pytest.mark.parametrize('field', ['parent_task_id', 'conversation_id', 'archive_scope'])
def test_changed_registration_prevents_flow_admission(
        storage, turn_service, owner, monkeypatch, field):
    import lib.tasks_pkg.child_invocation as children
    _, parent = _task(turn_service, storage, 'flow-child-denied')
    definition, events, entered = graph(role('worker')), [], []
    query = storage.client.query
    def altered(operation, payload, *args, **kwargs):
        result = query(operation, payload, *args, **kwargs)
        if operation == 'invocation.resolve_child':
            return {**result, field: 'foreign-registration'}
        return result
    monkeypatch.setattr(storage.client, 'query', altered)
    monkeypatch.setattr(children, 'execute_invocation', lambda *args, **kwargs: entered.append(True))
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    journal.begin(definition, 'input')
    engine = FlowExecutor(definition, parent_task=parent, model='fixture',
        model_routing_policy='selected', on_event=lambda event: None, execution_journal=journal)
    try:
        with pytest.raises(RecoveryRefused):
            engine.run(initial_context='input')
        assert entered == []
        assert not any(event['type'] == 'execution_effect_started' for event in events)
    finally:
        _discard(parent)
