"""Direct contracts for the orchestration verifier-loop runtime."""

import threading
from pathlib import Path

import pytest

from lib.orchestration_feedback import OrchestrationFeedbackState
from lib.orchestration_graph import GraphNavigator
from lib.orchestration_loop_runtime import (
    OrchestrationLoopAborted,
    OrchestrationLoopRuntime,
)
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration_progress import OrchestrationProgressLedger
from lib.orchestration_transcript import OrchestrationTranscript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _runtime(
    verifier_outputs,
    *,
    max_iterations=5,
    state_changing=1,
    abort_check=lambda: False,
    classify=None,
    node_max_iterations='runtime',
):
    lock = threading.Lock()
    events = []
    iterations = []
    replans = []
    nodes = {
        'planner': {'id': 'planner', 'type': 'role', 'role': 'planner'},
        'loop': {
            'id': 'loop',
            'type': 'control',
            'kind': 'loop',
            'params': ({'max_iterations': max_iterations}
                       if node_max_iterations == 'runtime'
                       else ({'max_iterations': node_max_iterations}
                             if node_max_iterations is not None else {})),
        },
        'worker': {'id': 'worker', 'type': 'role', 'role': 'worker'},
        'critic': {'id': 'critic', 'type': 'role', 'role': 'critic'},
        'stop': {'id': 'stop', 'type': 'control', 'kind': 'stop'},
    }
    edges = [
        {'from': 'planner', 'to': 'loop'},
        {'from': 'loop', 'to': 'worker'},
        {'from': 'worker', 'to': 'critic'},
        {'from': 'critic', 'to': 'loop'},
        {'from': 'loop', 'to': 'stop'},
    ]
    navigator = GraphNavigator.from_edges(nodes, edges)
    progress = OrchestrationProgressLedger(lock=lock)
    feedback = OrchestrationFeedbackState(lock=lock)
    outcomes = OrchestrationOutcomeLedger(lock=lock)
    transcript = OrchestrationTranscript(lock=lock)
    outputs = iter(verifier_outputs)

    def walk(_entry, context, *, stop_at):
        assert stop_at == 'loop'
        iteration = len(iterations)
        progress.record_producer({
            'node_id': 'worker',
            'role': 'worker',
            'sc_count': state_changing,
            'explore_count': 0 if state_changing else 1,
            'names': ['write_file'] if state_changing else [],
            'reported': True,
        })
        transcript.record(
            'critic',
            'critic',
            next(outputs),
            'completed',
            '',
            0,
        )
        return f'{context}|iteration-{iteration}'

    def run_replan(planner_id, context, defect, number):
        replans.append((planner_id, defect, number))
        return context + '|replanned'

    if classify is None:
        def classify(text, *, verifier_role=''):
            assert verifier_role == 'critic'
            return ('stop', None) if 'STOP' in text else ('worker', None)

    runtime = OrchestrationLoopRuntime(
        navigator=navigator,
        nodes=nodes,
        max_iterations=max_iterations,
        feedback=feedback,
        progress=progress,
        outcomes=outcomes,
        transcript=transcript,
        emit=events.append,
        abort_check=abort_check,
        walk=walk,
        run_replan=run_replan,
        classify_verdict=classify,
        progress_parser=lambda _text: (None, None),
        on_iteration_change=iterations.append,
    )
    return runtime, outcomes, feedback, events, iterations, replans


def test_loop_runtime_stops_and_records_the_real_exit_reason():
    runtime, outcomes, _feedback, events, iterations, _replans = _runtime([
        'CONTINUE: more work',
        'VERDICT: STOP',
    ])

    context, exit_node = runtime.run('loop', 'request')

    assert context == 'request|iteration-1|iteration-2'
    assert exit_node == 'stop'
    assert iterations == [1, 2, 0]
    assert outcomes.loop_exits_snapshot() == [{
        'node_id': 'loop',
        'reason': 'stop',
        'iterations': 2,
    }]
    assert [event['type'] for event in events[:3]] == [
        'loop_start', 'loop_iteration', 'loop_iteration',
    ]


def test_missing_loop_cap_uses_the_shared_authored_default_before_ceiling():
    runtime, _outcomes, _feedback, events, _iterations, _replans = _runtime(
        ['VERDICT: STOP'],
        max_iterations=12,
        node_max_iterations=None,
    )

    runtime.run('loop', 'request')

    assert events[0]['type'] == 'loop_start'
    assert events[0]['max_iterations'] == 10


def test_loop_runtime_zero_deliverable_guard_forces_a_new_iteration():
    runtime, outcomes, feedback, events, _iterations, _replans = _runtime(
        ['CONTINUE: first', 'CONTINUE: second', 'VERDICT: STOP'],
        max_iterations=4,
        state_changing=0,
    )

    runtime.run('loop', 'request')

    guards = [
        event for event in events
        if event['type'] == 'zero_deliverable_guard'
    ]
    assert guards and guards[0]['iteration'] == 2
    assert 'START EXECUTING' in feedback.pending_directive()
    assert outcomes.loop_exits_snapshot()[0]['iterations'] >= 3


def test_loop_runtime_routes_a_structural_verdict_through_replan_port():
    calls = {'count': 0}

    def classify(_text, *, verifier_role=''):
        calls['count'] += 1
        if calls['count'] == 1:
            return 'planner', 'missing build step'
        return 'stop', None

    runtime, outcomes, _feedback, events, _iterations, replans = _runtime(
        ['replan', 'done'],
        classify=classify,
    )

    context, _exit_node = runtime.run('loop', 'request')

    assert replans == [('planner', 'missing build step', 1)]
    assert '|replanned' in context
    assert any(event['type'] == 'replan' for event in events)
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'stop'


def test_loop_runtime_raises_a_transport_neutral_abort_signal():
    runtime, outcomes, _feedback, _events, iterations, _replans = _runtime(
        [],
        abort_check=lambda: True,
    )

    with pytest.raises(OrchestrationLoopAborted):
        runtime.run('loop', 'request')

    assert iterations == []
    assert outcomes.loop_exits_snapshot() == []


def test_engine_loop_method_is_a_thin_runtime_facade():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (ROOT / 'lib' / 'orchestration_loop_runtime.py').read_text()

    method = engine.split('    def _run_loop(', 1)[1].split(
        '    def _run_replan(', 1,
    )[0]
    assert 'self._loop_runtime.run(' in method
    assert 'zero_streak' not in method
    assert 'OrchestrationLoopRuntime(' in engine
    assert 'from lib.orchestration_engine import' not in runtime
    assert 'ThreadPoolExecutor' not in runtime
