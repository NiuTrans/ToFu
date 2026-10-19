"""Preparation reserves a correlation witness; execution/settlement still need admission."""
from copy import deepcopy

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused, inspect_journal
from lib.orchestration.invocation_recovery import RegisteredInvocationCompletion
from tests.orchestration.test_orchestration_execution_recovery import graph, role

pytestmark = pytest.mark.unit


def preparation():
    root = dict(id='root', _userId=7, status='running', config={})
    _ensure_execution_budget(root)
    child = dict(id='actor', _userId=7, convId='conversation', _parentTaskId='root',
        _archiveScope='invocation:7:actor', status='running', config={})
    inherit_execution_budget(root, child)
    writer = RegisteredInvocationCompletion(child)
    event = dict(type='execution_effect_prepared', key='root/worker#0', kind='agent',
        input_hash='a'*64, invocation_id=child['id'])
    event['invocation_completion'] = writer.prepare(event)
    return writer, event, child


def test_reserved_witness_does_not_admit_account_capture_or_settlement():
    writer, event, child = preparation()
    with pytest.raises(RecoveryRefused, match='no child admission'):
        writer.capture_account()
    with pytest.raises(RecoveryRefused, match='no registered admission'):
        writer.settle({})
    started = {key: value for key, value in event.items() if key != 'invocation_completion'}
    started['type'] = 'execution_effect_started'
    with pytest.raises(RecoveryRefused, match='differs from its preparation'):
        writer.bind(dict(started, input_hash='b'*64))
    assert writer.bind(started) == event['invocation_completion']
    assert writer.capture_account()['root_task_id'] == child['_executionBudget'].root_id
    with pytest.raises(RecoveryRefused, match='admission changed'):
        writer.bind(started)
    with pytest.raises(RecoveryRefused, match='preparation changed'):
        writer.prepare(event)


@pytest.mark.parametrize('damage', [None, 'nonce', 'invocation', 'root', 'missing'])
def test_durable_preparation_and_started_admission_must_keep_the_same_witness(damage):
    _, prepared, _ = preparation()
    definition = graph(role('worker')); events = []
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    journal.begin(definition, 'input')
    events.append(prepared)
    observed = inspect_journal(events, definition=definition, initial_context='input')
    assert observed.prepared_agents == {prepared['key']: prepared}
    assert not observed.pending_agents and observed.settled() == ({}, '')
    started = dict(deepcopy(prepared), type='execution_effect_started')
    if damage == 'nonce': started['invocation_completion']['nonce'] = 'b'*32
    elif damage == 'invocation': started['invocation_id'] = 'foreign'
    elif damage == 'root': started['invocation_completion']['account_root_task_id'] = 'foreign'
    elif damage == 'missing': started.pop('invocation_completion')
    events.append(started)
    if damage:
        with pytest.raises(RecoveryRefused, match='changed its preparation'):
            inspect_journal(events, definition=definition, initial_context='input')
    else:
        observed = inspect_journal(events, definition=definition, initial_context='input')
        assert not observed.prepared_agents and observed.pending_agents == {started['key']: started}
        with pytest.raises(RecoveryRefused, match='external effects are unknown'):
            observed.settled()
