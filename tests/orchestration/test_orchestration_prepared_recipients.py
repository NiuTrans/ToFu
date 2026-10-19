"""Exact recovery occurrences retain normal admission, context and completion."""
from copy import deepcopy
from dataclasses import replace
import json
import threading

import pytest

from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner
from lib.orchestration.continuation import FlowSuspended
from lib.agent_core.execution_budget import _ensure_execution_budget
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused
from lib.orchestration.prepared_recipients import prepare_flow_recipients
from tests.orchestration.test_orchestration_agent_runner import _parent
from tests.orchestration.test_orchestration_execution_recovery import graph, role
from tests.orchestration.test_orchestration_recovery_frontier import source, inspect, parallel_graph

pytestmark = pytest.mark.unit


def definition_for(shape):
    if shape == 'isolated':
        return graph(dict(id='sub', type='subflow', params={'scope': 'isolated',
            'definition': graph(role('prefix'), role('pending'), role('after'))}))
    if shape == 'fanout':
        return graph(role('prefix'), dict(id='expand', type='control', kind='fanout', params={
            'specs_json': json.dumps([dict(id='a', role='general', objective='first pending'),
                dict(id='b', role='general', objective='second pending'),
                dict(id='later', role='general', objective='after pending', depends_on=['a'])])}))
    if shape == 'nested_parallel':
        definition = parallel_graph(2)
        definition['nodes'][3] = dict(id='pending-0', type='subflow', params={
            'scope': 'isolated', 'definition': parallel_graph(2)})
        return definition
    if shape == 'parallel':
        return parallel_graph(3)
    return graph(role('prefix'), role('pending'), role('after'))


def record_conversation_source(definition, monkeypatch):
    # Exercise actual nested context owners; the generic pure-interpreter oracle
    # intentionally has no ordinary invocation history to carry across subflows.
    def shared(definition):
        for node in definition['nodes']:
            if node['type'] == 'role':
                node.setdefault('params', {})['isolation'] = 'shared-context'
            elif node['type'] == 'subflow':
                shared(node['params']['definition'])
    shared(definition)
    parent = _parent()
    parent['messages'] = [dict(role='user', content='Original objective')]
    config = OrchestrationAgentRunnerConfig(parent_task=parent, model='chosen',
        model_routing_policy='selected', worker_context_policy='conversation')
    events = []
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)), worker_context_policy='conversation')
    journal.begin(definition, 'original context')
    ordinary = OrchestrationInvocationRunner.__call__
    def invoke(self, node, context, iteration, *, before_dispatch=None):
        if node['id'] != 'prefix':
            before_dispatch()
            raise FlowSuspended()
        return ordinary(self, node, context, iteration, before_dispatch=before_dispatch)
    def prefix(task, *, policy):
        task.update(status='done', content='original committed prefix', finishReason='stop',
            streamState='provider_finished', _messages_include_current_run=True,
            messages=[dict(role='assistant', content='full verified model history')],
            _todos=[dict(id='keep', status='completed')])
    with monkeypatch.context() as patch:
        patch.setattr(OrchestrationInvocationRunner, '__call__', invoke)
        patch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', prefix)
        with pytest.raises(FlowSuspended):
            FlowExecutor(definition, execution_journal=journal,
                **config.executor_options()).run(initial_context='original context')
    records = {row['key']: {key: value for key, value in row.items() if key not in ('type', 'key')}
               for row in events if row['type'] == 'execution_effect_completed'}
    pending = {row['key']: row for row in events if row['type'] == 'execution_effect_started'
               and row['key'] not in records}
    return records, pending, []


@pytest.mark.parametrize('shape,conversation', [
    ('linear', False), ('linear', True), ('parallel', False), ('isolated', True),
    ('fanout', False), ('nested_parallel', False)])
def test_prepared_recipients_complete_through_actual_graph_context_and_journal(monkeypatch, shape, conversation):
    definition = definition_for(shape)
    policy = 'conversation' if conversation else 'handoff'
    records, pending, _ = (record_conversation_source(definition, monkeypatch) if conversation
                           else source(definition))
    inputs = inspect(definition, records, pending, worker_context_policy=policy)
    parent = _parent()
    parent['messages'] = [dict(role='user', content='Original objective')]
    _ensure_execution_budget(parent)
    config = OrchestrationAgentRunnerConfig(parent_task=parent, model='chosen',
        model_routing_policy='selected', worker_context_policy=policy)
    emitted, calls, prepared_ids = [], [], set()
    ready = threading.Barrier(len(inputs))
    def execute(task, *, policy):
        calls.append(task['id'])
        if task['id'] in prepared_ids:
            ready.wait(5)  # Fixture native whole-set readiness; never production coordination.
            if conversation:
                assert 'full verified model history' in str(task['messages'])
                assert task['_todos'][0]['status'] == 'completed'
        elif conversation:
            assert 'Recovered pending output' in str(task['messages'])
            assert task['_todos'][0]['status'] == 'verified'
        task.update(status='done', content='Recovered pending output', finishReason='stop',
            streamState='provider_finished', _messages_include_current_run=True)
        task['messages'].append(dict(role='assistant', content='Recovered pending output'))
        if conversation:
            task['_todos'] = [dict(id='keep', status='verified')]
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', execute)
    journal = ExecutionJournal(lambda event: emitted.append(deepcopy(event)), records=records,
        worker_context_policy=policy)
    journal.begin(definition, 'original context')
    with prepare_flow_recipients(inputs, config=config, emit=lambda event: None,
                                 abort_check=lambda: False) as recipients:
        children = dict(recipients.children)
        prepared_ids.update(task['id'] for task in children.values())
        assert not calls and len(prepared_ids) == len(inputs)
        recipients.bind(journal)
        result = FlowExecutor(definition, execution_journal=journal,
            max_parallel=1, **config.executor_options()).run(initial_context='original context')
        assert result['ok'], result
        journal.verify_consumed()
    assert len(calls) == len(set(calls)) and prepared_ids <= set(calls)
    for key, item in inputs.items():
        sequence = [event['type'] for event in emitted if event.get('key') == key]
        assert sequence == ['execution_effect_prepared', 'execution_effect_started', 'execution_effect_completed']
        receipt = next(event for event in emitted if event.get('key') == key
                       and event['type'] == 'execution_effect_completed')
        assert receipt['input_hash'] == item.input_hash
        if conversation:
            assert receipt['result']['worker_context']['task_state']['_todos'][0]['status'] == 'verified'
            assert 'Recovered pending output' in str(receipt['result']['worker_context']['messages'])
    assert not parent['_executionBudget'].continuity_snapshot()['active_child_task_ids']
    assert all(child['_invocationPublicationClosed'] for child in children.values())
    with pytest.raises(RecoveryRefused, match='closed'):
        _ = recipients.children


@pytest.mark.parametrize('damage', ['hash', 'context', 'model', 'new_leaf', 'missing'])
def test_changed_preparation_or_dispatch_never_calls_provider(monkeypatch, damage):
    definition = definition_for('linear')
    records, pending, _ = record_conversation_source(definition, monkeypatch)
    inputs = inspect(definition, records, pending, worker_context_policy='conversation')
    parent = _parent()
    parent['messages'] = [dict(role='user', content='Original objective')]
    config = OrchestrationAgentRunnerConfig(parent_task=parent, model='chosen',
        model_routing_policy='selected', worker_context_policy='conversation')
    key = next(iter(inputs))
    if damage == 'hash':
        inputs[key] = replace(inputs[key], context='changed without original digest')
    elif damage == 'context':
        inputs[key] = replace(inputs[key], worker_context=dict(messages=[], task_state={}))
    calls, events = [], []
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', lambda *a, **k: calls.append(a))
    with pytest.raises((RecoveryRefused, ValueError)):
        with prepare_flow_recipients(inputs, config=config, emit=lambda event: None,
                                     abort_check=lambda: False) as recipients:
            journal = ExecutionJournal(events.append, records=records, worker_context_policy='conversation')
            journal.begin(definition, 'original context')
            recipients.bind(journal)
            options = config.executor_options()
            if damage == 'model':
                options['model'] = 'foreign'
            if damage == 'new_leaf':
                definition = graph(role('uninspected'), *definition['nodes'][1:-1])
            if damage == 'missing':
                pass  # Exiting a never-consumed prepared set must reject all children.
            else:
                FlowExecutor(definition, execution_journal=journal, **options).run(initial_context='original context')
    assert not calls
    assert not any(event['type'] == 'execution_effect_started' for event in events)
    if '_executionBudget' in parent:
        assert not parent['_executionBudget'].continuity_snapshot()['active_child_task_ids']


@pytest.mark.parametrize('damage', ['source_hash', 'source_duplicate', 'source_missing', 'used_journal'])
def test_source_binding_and_dispatch_owner_refuse_mismatched_inventory(damage):
    definition = parallel_graph(2)
    records, pending, _ = source(definition)
    inputs = inspect(definition, records, pending)
    config = OrchestrationAgentRunnerConfig(parent_task=_parent(), model='chosen', model_routing_policy='selected')
    for index, row in enumerate(pending.values()):
        row['invocation_id'] = 'source-'+str(index)
    with pytest.raises(RecoveryRefused):
        with prepare_flow_recipients(inputs, config=config, emit=lambda event: None,
                                     abort_check=lambda: False) as recipients:
            first = next(iter(pending))
            if damage == 'source_hash':
                pending[first]['input_hash'] = '0'*64
            elif damage == 'source_duplicate':
                for row in pending.values():
                    row['invocation_id'] = 'same'
            elif damage == 'source_missing':
                pending.pop(first)
            else:
                journal = ExecutionJournal(lambda event: None)
                journal.call('tool', 'already-entered', 0, {}, lambda: 'result')
                recipients.bind(journal)
            recipients.source_bindings(pending)
