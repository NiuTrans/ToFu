"""Direct contracts for the isolated-subflow runtime boundary."""

import threading
from pathlib import Path

import pytest

from lib.orchestration_budget import OrchestrationAgentBudget
from lib.orchestration_dataflow import OrchestrationDataflow
from lib.orchestration_graph import FlowExecutionError
from lib.orchestration_outcome import OrchestrationOutcomeLedger
from lib.orchestration_subflow_runtime import (
    OrchestrationSubflowAborted,
    OrchestrationSubflowRuntime,
)
from lib.orchestration_trace import OrchestrationTraceRecorder
from lib.orchestration_transcript import OrchestrationTranscript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Child:
    def __init__(self, result, *, agents_run=2):
        self._result = result
        self._agents_run = agents_run
        self.initial_context = None

    @property
    def agents_run(self):
        return self._agents_run

    def run(self, *, initial_context=''):
        self.initial_context = initial_context
        return self._result


def _runtime(result, *, limit=5, depth=0, resolver=None):
    lock = threading.Lock()
    events = []
    children = []
    counted = []
    ports = {
        'budget': OrchestrationAgentBudget(limit),
        'dataflow': OrchestrationDataflow(lock=lock),
        'outcomes': OrchestrationOutcomeLedger(lock=lock),
        'transcript': OrchestrationTranscript(lock=lock),
    }
    ports['trace'] = OrchestrationTraceRecorder(
        emit=events.append,
        lock=lock,
    )

    def factory(definition):
        child = _Child(result)
        child.definition = definition
        children.append(child)
        return child

    runtime = OrchestrationSubflowRuntime(
        budget=ports['budget'],
        depth=depth,
        resolver=resolver,
        child_executor_factory=factory,
        dataflow=ports['dataflow'],
        outcomes=ports['outcomes'],
        trace_recorder=ports['trace'],
        transcript=ports['transcript'],
        emit=events.append,
        on_child_agents=counted.append,
    )
    return runtime, ports, events, children, counted


def _node(**params):
    return {
        'id': 'box',
        'type': 'subflow',
        'role': 'general',
        'name': 'Nested work',
        'params': params or {'definition': {'schema': 'child'}},
    }


def test_subflow_runtime_owns_the_successful_black_box_membrane():
    result = {
        'ok': True,
        'status': 'completed',
        'final': 'scratchpad ending in verifier',
        'transcript': [
            {'role': 'worker', 'output': 'actual deliverable'},
            {'role': 'critic', 'output': 'VERDICT: STOP'},
        ],
    }
    runtime, ports, events, children, counted = _runtime(result)

    context = runtime.run(_node(), 'upstream only', iteration=3)

    assert context == 'upstream only\n\n[general]\nactual deliverable'
    assert children[0].initial_context == 'upstream only'
    assert children[0].definition == {'schema': 'child'}
    assert counted == [2]
    assert ports['transcript'].snapshot()[0]['output'] == 'actual deliverable'
    assert ports['trace'].snapshot()[0]['iteration'] == 3
    assert ports['trace'].snapshot()[0]['subflow'] is True
    assert ports['dataflow'].output_snapshot() == {
        'box': {'text': 'actual deliverable'},
    }
    assert [event['type'] for event in events] == [
        'step_start', 'step_trace', 'step_complete',
    ]
    assert events[-1]['state_changing'] == 0
    assert events[-1]['exploratory'] == 0
    assert events[-1]['state_changing_tools'] == []


def test_subflow_runtime_resolves_references_and_records_incomplete_exit():
    result = {
        'ok': False,
        'status': 'completed',
        'stop_reason': 'max_iterations',
        'final': 'partial',
        'transcript': [{'role': 'worker', 'output': 'partial'}],
    }
    runtime, ports, _events, children, _counted = _runtime(
        result,
        resolver=lambda reference: {'schema': reference},
    )

    runtime.run(_node(ref='saved-flow'), '', iteration=0)

    assert children[0].definition == {'schema': 'saved-flow'}
    assert ports['outcomes'].loop_exits_snapshot() == [{
        'node_id': 'box',
        'reason': 'max_iterations',
        'iterations': 0,
    }]


def test_subflow_runtime_surfaces_failure_after_parent_visible_records():
    runtime, ports, events, _children, counted = _runtime({
        'ok': False,
        'status': 'failed',
        'error': 'child graph invalid',
        'final': '',
        'transcript': [],
    })

    with pytest.raises(FlowExecutionError, match='child graph invalid'):
        runtime.run(_node(), 'request', iteration=0)

    assert counted == [2]
    assert ports['transcript'].snapshot()[0]['status'] == 'failed'
    assert events[-1]['type'] == 'step_complete'
    assert events[-1]['status'] == 'failed'


def test_subflow_runtime_translates_child_abort_without_publishing_a_turn():
    runtime, ports, events, _children, counted = _runtime({
        'ok': False,
        'status': 'aborted',
        'final': '',
        'transcript': [],
    })

    with pytest.raises(OrchestrationSubflowAborted):
        runtime.run(_node(), 'request', iteration=0)

    assert counted == [2]
    assert ports['transcript'].snapshot() == []
    assert [event['type'] for event in events] == ['step_start']


def test_engine_subflow_method_is_a_thin_runtime_facade():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_subflow_runtime.py'
    ).read_text()

    method = engine.split(
        '    def _run_subflow_isolated(', 1,
    )[1].split('    def _make_isolated_child(', 1)[0]
    assert 'self._subflow_runtime.run(' in method
    assert 'child_executor_factory=self._make_isolated_child' in engine
    assert 'FlowExecutor(' not in runtime
    assert 'GraphNavigator' not in runtime
    assert 'child_executor.agents_run' in runtime
