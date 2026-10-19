"""The ordinary interpreter reconstructs pending inputs without new effects."""
from copy import deepcopy
import json
import threading

import pytest

from lib.orchestration.continuation import FlowSuspended
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused, fingerprint
from lib.orchestration.recovery_frontier import inspect_pending_inputs
from tests.orchestration.test_orchestration_execution_recovery import graph, role

pytestmark = pytest.mark.unit


def control(name, kind):
    return dict(id=name, type='control', kind=kind)


def parallel_graph(count=3):
    nodes = [control('start', 'start'), role('prefix'), control('fork', 'parallel'),
        *(role('pending-'+str(i)) for i in range(count)), control('join', 'barrier'),
        role('after'), control('stop', 'stop')]
    edges = [('start', 'prefix'), ('prefix', 'fork'), ('join', 'after'), ('after', 'stop')]
    edges += [(a, b) for i in range(count) for a, b in [('fork', 'pending-'+str(i)), ('pending-'+str(i), 'join')]]
    return dict(schema='tofu.orchestration/v1', name='Complete frontier', nodes=nodes,
        edges=[dict(zip(('from', 'to'), pair)) for pair in edges])


def source(definition, *, conversation=False):
    events, calls = [], []
    policy = 'conversation' if conversation else 'handoff'
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)), worker_context_policy=policy)
    journal.begin(definition, 'original context')
    def runner(node, context, iteration):
        calls.append((node['id'], deepcopy(node), context, iteration))
        if node['id'] != 'prefix':
            raise FlowSuspended()
        if conversation:
            executor._default_runner_adapter.restore_worker_context(dict(
                messages=[dict(role='assistant', content='full verified model history')],
                task_state={'_todos': [{'id':'keep', 'status':'completed'}]}))
        return dict(output='original committed prefix')
    executor = FlowExecutor(definition, agent_runner=runner, execution_journal=journal,
        worker_context_policy=policy, max_parallel=1)
    with pytest.raises(FlowSuspended):
        executor.run(initial_context='original context')
    records = {event['key']: {k: deepcopy(v) for k, v in event.items() if k not in ('key', 'type')}
               for event in events if event['type'] == 'execution_effect_completed'}
    pending = {event['key']: deepcopy(event) for event in events
               if event['type'] == 'execution_effect_started' and event['key'] not in records}
    return records, pending, calls


def inspect(definition, records, pending, **options):
    return inspect_pending_inputs(definition, initial_context='original context', records=records,
        pending_agents=pending, maximum_bytes=options.pop('maximum_bytes', 4*1048576),
        max_iterations=40, max_parallel=1, **options)


@pytest.mark.parametrize('parallel,conversation', [(False, False), (True, False), (False, True), (True, True)])
def test_actual_interpreter_recovers_every_exact_pending_input_and_worker_snapshot(parallel, conversation):
    definition = parallel_graph() if parallel else graph(role('prefix'), role('pending'), role('after'))
    records, pending, calls = source(definition, conversation=conversation)
    before = deepcopy((definition, records, pending, calls))
    results = inspect(definition, records, pending, worker_context_policy='conversation' if conversation else 'handoff')
    assert before == (definition, records, pending, calls)
    assert set(results) == set(pending)
    for key, result in results.items():
        original = next(row for row in calls if row[0] == result.node['id'])
        assert (result.node, result.context, result.iteration) == original[1:]
        assert fingerprint([result.node, result.context]) == pending[key]['input_hash']
        if conversation:
            assert result.worker_context['messages'][0]['content'] == 'full verified model history'
            assert result.worker_context['task_state']['_todos'][0]['id'] == 'keep'
        else:
            assert result.worker_context is None
    assert not any(thread.name.startswith('flow-par') for thread in threading.enumerate())


@pytest.mark.parametrize('damage', ['hash', 'missing', 'extra', 'receipt', 'unconsumed', 'bytes', 'cancelled', 'definition'])
def test_inspection_refuses_changed_incomplete_or_cancelled_frontiers(damage):
    definition = parallel_graph()
    records, pending, _ = source(definition)
    options = {}
    if damage == 'hash': next(iter(pending.values()))['input_hash'] = '0'*64
    elif damage == 'missing': pending.pop(next(iter(pending)))
    elif damage == 'extra': pending['unreachable'] = deepcopy(next(iter(pending.values())))
    elif damage == 'receipt': next(iter(records.values()))['input_hash'] = '0'*64
    elif damage == 'unconsumed': records['unused'] = deepcopy(next(iter(records.values())))
    elif damage == 'bytes': options['maximum_bytes'] = 1
    elif damage == 'cancelled': options['abort_check'] = lambda: True
    else: definition['nodes'][3]['params']['objective'] = 'changed input'
    with pytest.raises(RecoveryRefused): inspect(definition, records, pending, **options)


def test_first_pending_worker_retains_an_uninitialized_context_for_normal_launch_preparation():
    definition = graph(role('pending'))
    records, pending, _ = source(definition, conversation=True)
    result = inspect(definition, records, pending, worker_context_policy='conversation')
    assert next(iter(result.values())).worker_context == dict(messages=None, task_state={})


def test_inspection_journal_cannot_publish_or_run_unsettled_operations():
    emitted, operated = [], []
    journal = ExecutionJournal(emitted.append, unsettled_observer=lambda **kwargs: None)
    with pytest.raises(FlowSuspended):
        journal.call('agent', 'pending', 0, [{}, 'input'], lambda: operated.append(True))
    assert not emitted and not operated
    with pytest.raises(RecoveryRefused, match='cannot publish'):
        journal.begin(graph(role('pending')), 'input')
    assert journal.abort_requested() and not emitted


@pytest.mark.parametrize('shape', ['isolated', 'fanout', 'nested_parallel'])
def test_nested_and_dynamic_frontiers_use_original_scopes_without_waiting_for_all_workers(shape):
    if shape == 'isolated':
        child = graph(role('prefix'), role('pending'))
        definition = graph(dict(id='sub', type='subflow', params={'scope':'isolated', 'definition':child}))
    elif shape == 'fanout':
        definition = graph(role('prefix'), dict(id='expand', type='control', kind='fanout', params={
            'specs_json':json.dumps([dict(id='a', role='general', objective='first pending'),
                dict(id='b', role='general', objective='second pending'),
                dict(id='later', role='general', objective='after pending', depends_on=['a'])])}))
    else:
        child = parallel_graph(2)
        definition = parallel_graph(2)
        definition['nodes'][3] = dict(id='pending-0', type='subflow', params={'scope':'isolated', 'definition':child})
    records, pending, calls = source(definition)
    results = inspect(definition, records, pending)
    assert set(results) == set(pending)
    assert len(results) == (1 if shape == 'isolated' else 2 if shape == 'fanout' else 3)
    assert all(fingerprint([row.node, row.context]) == pending[key]['input_hash'] for key, row in results.items())
    assert not any(row.node['id'].endswith(':later') for row in results.values())


def test_loop_inspection_replays_prior_verdict_before_matching_the_later_iteration():
    definition = dict(schema='tofu.orchestration/v1', name='Loop frontier', nodes=[
        control('s', 'start'), dict(id='l', type='control', kind='loop', params={'max_iterations':3}),
        role('worker'), dict(id='critic', type='role', role='critic'), control('e', 'stop')],
        edges=[dict(zip(('from','to'), pair)) for pair in
            [('s','l'),('l','worker'),('worker','critic'),('critic','l'),('l','e')]])
    events = []
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    journal.begin(definition, 'original context')
    def runner(node, context, iteration):
        if iteration == 2: raise FlowSuspended()
        if node['id'] == 'critic': return dict(output='[VERDICT: CONTINUE]')
        return dict(output='first deliverable', tool_log=[dict(toolName='write_file', status='done',
            toolContent='File created: fixture.txt', results=[dict(writeOk=True)])])
    with pytest.raises(FlowSuspended):
        FlowExecutor(definition, agent_runner=runner, execution_journal=journal).run(initial_context='original context')
    records = {event['key']: {k:v for k,v in event.items() if k not in ('key','type')}
        for event in events if event['type'] == 'execution_effect_completed'}
    pending = {event['key']: event for event in events if event['type'] == 'execution_effect_started'
        and event['key'] not in records}
    results = inspect(definition, records, pending)
    assert len(results) == 1
    row = next(iter(results.values()))
    assert row.node['id'] == 'worker' and row.iteration == 2
    assert fingerprint([row.node,row.context]) == pending[row.key]['input_hash']
