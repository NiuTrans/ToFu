"""Direct contracts for the top-level graph execution lifecycle."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lib.orchestration_execution_runtime import OrchestrationExecutionRuntime
from lib.orchestration_graph import FlowExecutionError
from lib.orchestration_outcome import OrchestrationOutcomeLedger


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Abort(Exception):
    pass


class _Navigator:
    def find_start(self):
        return 'start'


class _Dataflow:
    def __init__(self):
        self.initial = None

    def set_initial_context(self, context):
        self.initial = context


class _Snapshots:
    def __init__(self, values):
        self.values = values

    def snapshot(self):
        return list(self.values)


def _runtime(walk, *, seed='baked request'):
    events = []
    calls = []
    dataflow = _Dataflow()
    outcomes = OrchestrationOutcomeLedger(lock=threading.Lock())
    ticks = iter((10.0, 12.25))

    def invoke(start, context):
        calls.append((start, context))
        return walk(start, context)

    runtime = OrchestrationExecutionRuntime(
        definition={'name': 'Release flow'},
        nodes={
            'start': {
                'id': 'start', 'type': 'control', 'kind': 'start',
                'params': {'seed': seed},
            },
            'stop': {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        },
        navigator=_Navigator(),
        dataflow=dataflow,
        outcomes=outcomes,
        transcript=_Snapshots([{'role': 'worker', 'output': 'done'}]),
        trace=_Snapshots([{'node_id': 'worker'}]),
        walk=invoke,
        agents_run=lambda: 3,
        emit=events.append,
        abort_errors=(_Abort,),
        clock=lambda: next(ticks),
    )
    return runtime, dataflow, outcomes, events, calls


def test_execution_runtime_owns_seed_events_and_detached_result_projection():
    runtime, dataflow, outcomes, events, calls = _runtime(
        lambda _start, context: context + ' -> final')
    outcomes.record_artifact({'node_id': 'artifact', 'path': 'report.md'})

    result = runtime.run()

    assert dataflow.initial == 'baked request'
    assert calls == [('start', 'baked request')]
    assert result == {
        'ok': True,
        'status': 'completed',
        'stop_reason': 'completed',
        'outcome': {
            'format': 'tofu.orchestration.outcome/v1',
            'category': 'success',
            'engine_status': 'completed',
            'lifecycle_status': 'done',
            'chat_status': 'done',
            'ok': True,
            'stop_reason': 'completed',
            'finish_reason': 'stop',
            'error': '',
        },
        'final': 'baked request -> final',
        'transcript': [{'role': 'worker', 'output': 'done'}],
        'trace': [{'node_id': 'worker'}],
        'loop_exits': [],
        'agents_run': 3,
        'artifacts': [{'node_id': 'artifact', 'path': 'report.md'}],
        'error': None,
    }
    assert events[0] == {
        'type': 'flow_start', 'name': 'Release flow', 'nodes': 2,
    }
    assert events[-1]['type'] == 'flow_complete'
    assert events[-1]['status'] == 'completed'
    assert events[-1]['agents_run'] == 3
    assert events[-1]['elapsed'] == 2.2


def test_explicit_context_wins_over_start_seed():
    runtime, dataflow, _outcomes, _events, calls = _runtime(
        lambda _start, context: context)

    result = runtime.run(initial_context='operator request')

    assert dataflow.initial == 'operator request'
    assert calls == [('start', 'operator request')]
    assert result['final'] == 'operator request'


@pytest.mark.parametrize(
    ('error', 'category', 'stop_reason', 'detail'),
    [
        (_Abort(), 'aborted', 'aborted', 'aborted'),
        (FlowExecutionError('broken graph'), 'failure', 'structural',
         'broken graph'),
        (ValueError('runner exploded'), 'failure', 'exception',
         'ValueError: runner exploded'),
    ],
)
def test_execution_runtime_classifies_abort_structural_and_unknown_failures(
    error, category, stop_reason, detail,
):
    def fail(_start, _context):
        raise error

    runtime, _dataflow, _outcomes, events, _calls = _runtime(fail)

    result = runtime.run(initial_context='request')

    assert result['ok'] is False
    assert result['outcome']['category'] == category
    assert result['stop_reason'] == stop_reason
    assert result['error'] == detail
    assert events[-1]['outcome']['category'] == category


def test_engine_run_is_a_thin_execution_runtime_facade():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_execution_runtime.py').read_text()
    method = engine.split('    def run(self, *, initial_context', 1)[1].split(
        '\n    @property', 1)[0]

    assert 'self._execution_runtime.run(' in method
    assert "'type': 'flow_start'" not in engine
    assert "'type': 'flow_complete'" not in engine
    assert "'type': 'flow_start'" in runtime
    assert "'type': 'flow_complete'" in runtime
    assert 'start_node.get(\'params\')' not in engine
    assert 'from lib.orchestration_engine import' not in runtime
