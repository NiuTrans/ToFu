"""Dynamic dependencies share ordinary Flow roles and publish before dispatch."""
import json
import threading

import pytest

from lib.orchestration.engine import FlowExecutor
from lib.orchestration.fanout_runtime import expansion_specs
from lib.orchestration.graph import FlowExecutionError
from lib.orchestration.durable_projection import DurableProjectionError

pytestmark = pytest.mark.unit


def definition(specs):
    return {'schema': 'tofu.orchestration/v1', 'name': 'Dynamic work', 'nodes': [
        {'id': 'start', 'type': 'control', 'kind': 'start'},
        {'id': 'work', 'type': 'control', 'kind': 'fanout',
         'params': {'specs_json': json.dumps(specs)}},
        {'id': 'stop', 'type': 'control', 'kind': 'stop'},
    ], 'edges': [{'from': 'start', 'to': 'work'}, {'from': 'work', 'to': 'stop'}]}


def test_ready_dependency_runs_before_unrelated_slow_branch():
    events = []
    dependency_started = threading.Event()
    specs = [{'id': name, 'objective': name, 'depends_on': deps} for name, deps in
             [('a', []), ('slow', []), ('after-a', ['a'])]]

    def runner(node, context, iteration):
        assert any(event['type'] == 'fanout_expanded' for event in events)
        name = node['params']['objective']
        if name == 'slow':
            assert dependency_started.wait(2), 'dependency incorrectly waited for barrier'
        if name == 'after-a':
            assert 'evidence-a' in context
            dependency_started.set()
        return {'status': 'completed', 'output': f'evidence-{name}'}

    result = FlowExecutor(definition(specs), agent_runner=runner,
                          on_event=events.append, max_parallel=2).run()
    assert result['status'] == 'completed', result
    assert len([e for e in events if e['type'] == 'step_complete']) == 3
    assert any(e['type'] == 'fanout_complete' for e in events)


@pytest.mark.parametrize('specs', [
    [{'id': 'a', 'objective': 'a', 'depends_on': ['missing']}],
    [{'id': 'a', 'objective': 'a', 'depends_on': ['a']}],
    [{'id': 'a', 'objective': 'a'}, {'id': 'a', 'objective': 'b'}],
    [{'id': 'a', 'objective': 'same'}, {'id': 'b', 'objective': 'same'}],
    [{'id': 'a', 'objective': 'a', 'tools': '*'}],
])
def test_invalid_expansion_never_dispatches(specs):
    with pytest.raises(FlowExecutionError):
        expansion_specs(json.dumps(specs), maximum=8)


def test_failed_expansion_commit_prevents_all_children():
    calls = []
    def emit(event):
        if event['type'] == 'fanout_expanded':
            raise DurableProjectionError('storage unavailable')
    executor = FlowExecutor(definition([{'id': 'a', 'objective': 'a'}]),
        agent_runner=lambda *args: calls.append(args), on_event=emit)
    try:
        result = executor.run()
    except DurableProjectionError:
        result = {'status': 'aborted'}
    assert result['status'] != 'completed'
    assert calls == []


def test_failed_dependency_does_not_dispatch_dependent_side_effects():
    calls = []
    def runner(node, context, iteration):
        calls.append(node['params']['objective'])
        return {'status': 'failed', 'error': 'no evidence'}
    result = FlowExecutor(definition([
        {'id': 'a', 'objective': 'a'},
        {'id': 'b', 'objective': 'b', 'depends_on': ['a']}]), agent_runner=runner).run()
    assert result['status'] != 'completed'
    assert calls == ['a']


def test_expansion_accepts_more_work_and_dependencies_than_old_wave_count():
    specs = [{'id': str(index), 'objective': f'work {index}'} for index in range(12)]
    specs[-1]['depends_on'] = [str(index) for index in range(11)]
    expanded = expansion_specs(json.dumps(specs), maximum=1)
    assert len(expanded) == 12
    assert expanded[-1].depends_on == [str(index) for index in range(11)]
