"""Direct contracts for the orchestration leaf-role runtime boundary."""

import threading
from pathlib import Path

import pytest

from lib.orchestration_budget import OrchestrationAgentBudget
from lib.orchestration_dataflow import OrchestrationDataflow
from lib.orchestration_feedback import OrchestrationFeedbackState
from lib.orchestration_graph import FlowExecutionError
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration_progress import OrchestrationProgressLedger
from lib.orchestration_role_runtime import OrchestrationRoleRuntime
from lib.orchestration_runner_result import OrchestrationAgentResult
from lib.orchestration_tool_usage import OrchestrationToolUsage
from lib.orchestration_trace import OrchestrationTraceRecorder
from lib.orchestration_transcript import OrchestrationTranscript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _runtime(runner, *, limit=3):
    lock = threading.Lock()
    events = []
    claimed = []
    ports = {
        'budget': OrchestrationAgentBudget(limit),
        'dataflow': OrchestrationDataflow(lock=lock),
        'feedback': OrchestrationFeedbackState(lock=lock),
        'progress': OrchestrationProgressLedger(lock=lock),
        'outcomes': OrchestrationOutcomeLedger(lock=lock),
        'transcript': OrchestrationTranscript(lock=lock),
    }
    ports['trace'] = OrchestrationTraceRecorder(
        emit=events.append,
        lock=lock,
    )
    runtime = OrchestrationRoleRuntime(
        budget=ports['budget'],
        runner=runner,
        dataflow=ports['dataflow'],
        feedback=ports['feedback'],
        progress=ports['progress'],
        outcomes=ports['outcomes'],
        trace_recorder=ports['trace'],
        transcript=ports['transcript'],
        emit=events.append,
        on_agent_claimed=lambda: claimed.append(True),
    )
    return runtime, ports, events, claimed


def test_role_runtime_owns_one_successful_leaf_lifecycle():
    typed = OrchestrationAgentResult(
        output='implemented',
        thinking='checked the contract',
        tool_usage=OrchestrationToolUsage(
            state_changing_tools=('write_file',),
            exploratory_tools=('read_file',),
            reported=True,
        ),
    )
    runtime, ports, events, claimed = _runtime(
        lambda _node, _context, _iteration: typed,
    )
    node = {
        'id': 'worker',
        'type': 'role',
        'role': 'worker',
        'name': 'Worker',
        'params': {},
    }

    context = runtime.run(node, 'request', iteration=2)

    assert context == 'request\n\n[worker]\nimplemented'
    assert claimed == [True]
    assert ports['budget'].used() == 1
    assert ports['transcript'].snapshot()[0] | {
        'elapsed': 0,
    } == {
        'node_id': 'worker',
        'role': 'worker',
        'output': 'implemented',
        'status': 'completed',
        'error': '',
        'elapsed': 0,
        'state_changing': 1,
        'exploratory': 1,
    }
    assert ports['progress'].latest_snapshot() == {
        'node_id': 'worker',
        'role': 'worker',
        'sc_count': 1,
        'explore_count': 1,
        'names': ['write_file'],
        'reported': True,
    }
    assert ports['dataflow'].output_snapshot() == {
        'worker': {'text': 'implemented'},
    }
    trace = ports['trace'].snapshot()[0]
    assert trace['iteration'] == 2
    assert trace['thinking'] == 'checked the contract'
    assert trace['state_changing_tools'] == ['write_file']
    assert events[-1]['state_changing'] == 1
    assert events[-1]['exploratory'] == 1
    assert events[-1]['state_changing_tools'] == ['write_file']
    assert [event['type'] for event in events] == [
        'step_start', 'step_trace', 'step_complete',
    ]


def test_role_runtime_normalizes_runner_crashes_into_node_failure():
    def crash(_node, _context, _iteration):
        raise RuntimeError('provider offline')

    runtime, ports, events, _claimed = _runtime(crash)
    node = {
        'id': 'critic',
        'type': 'role',
        'role': 'critic',
        'params': {},
    }

    context = runtime.run(node, 'request', iteration=0)

    assert context == 'request\n\n[critic]'
    assert ports['outcomes'].node_failures_snapshot() == [{
        'node_id': 'critic',
        'role': 'critic',
        'error': 'provider offline',
    }]
    assert ports['progress'].latest_snapshot() == {}
    assert events[-1]['status'] == 'failed'
    assert events[-1]['emits'] == 'user'


def test_role_runtime_enforces_the_shared_agent_budget_before_runner_start():
    starts = []
    runtime, _ports, _events, claimed = _runtime(
        lambda *_args: starts.append(True) or {'output': 'done'},
        limit=1,
    )
    node = {
        'id': 'worker',
        'type': 'role',
        'role': 'worker',
        'params': {},
    }

    runtime.run(node, '', iteration=0)
    with pytest.raises(FlowExecutionError, match='agent budget exhausted \\(1\\)'):
        runtime.run(node, '', iteration=0)

    assert starts == [True]
    assert claimed == [True]


def test_engine_role_method_is_a_thin_runtime_compatibility_facade():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    role_runtime = (
        ROOT / 'lib' / 'orchestration_role_runtime.py'
    ).read_text()

    method = engine.split('    def _run_role(', 1)[1].split(
        '    def _increment_agents_run(', 1,
    )[0]
    assert 'self._role_runtime.run(' in method
    assert 'self._runner(' not in method
    assert 'normalize_orchestration_agent_result(' not in method
    assert 'OrchestrationRoleRuntime(' in engine
    assert 'GraphNavigator' not in role_runtime
    assert 'def run(self, node: dict, context: str, *, iteration: int)' in role_runtime
