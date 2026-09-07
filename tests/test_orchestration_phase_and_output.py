"""tests/test_orchestration_phase_and_output.py — Stage 1 of the honest
flow-via-chat path (Option B foundation).

Covers:
  * ``first_executed_role`` / ``initial_phase_for_flow`` — static preview of
    the flow's opening role node and the chat phase it maps to (so a
    plannerless autopilot flow never advertises 'planning').
  * The engine now carries the FULL turn output on ``step_complete`` (not
    just the 200-char preview), and ``FlowEventAdapter`` consumes it —
    so a long worker/VU/critic turn is no longer truncated to 200 chars.
"""

import unittest
from pathlib import Path

import pytest

from lib.orchestration._builtin_definitions import (
    build_autopilot_definition,
)
from lib.orchestration._execution_projection import (
    first_executed_role, initial_phase_for_flow,
)
from lib.orchestration_chat_flow_adapter import FlowEventAdapter
from tests.support.orchestration_definitions import (
    build_verifier_loop_definition,
)


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


class ExecutionProjectionOwnershipTest(unittest.TestCase):
    def test_projection_has_one_physical_owner_outside_validator(self):
        projection = (ROOT / 'lib/orchestration/_execution_projection.py').read_text()
        validator = (ROOT / 'lib/orchestration/_validate.py').read_text()
        engine = (ROOT / 'lib/orchestration_engine.py').read_text()
        replan = (ROOT / 'lib/orchestration_replan_runtime.py').read_text()
        adapter = (
            ROOT / 'lib/orchestration_chat_flow_adapter.py').read_text()
        inspection = (ROOT / 'lib/orchestration/definition_inspection.py').read_text()

        self.assertIn('def render_role_brief(', projection)
        self.assertIn('def initial_phase_for_flow(', projection)
        self.assertNotIn('def render_role_brief(', validator)
        self.assertNotIn('def initial_phase_for_flow(', validator)
        self.assertIn(
            'from lib.orchestration._execution_projection import render_role_brief',
            replan,
        )
        self.assertNotIn('render_role_brief', engine)
        self.assertIn(
            'from lib.orchestration._execution_projection import _PLANNER_ROLES',
            adapter,
        )
        self.assertIn(
            'from lib.orchestration._execution_projection import initial_phase_for_flow',
            inspection,
        )
        self.assertLess(projection.count('\n'), 140)
        self.assertLess(validator.count('\n'), 400)


class FirstRoleTest(unittest.TestCase):
    def test_planner_flow_opens_on_planner(self):
        defn = build_verifier_loop_definition()
        node = first_executed_role(defn)
        self.assertIsNotNone(node)
        self.assertEqual(node.get('role'), 'planner')
        self.assertEqual(initial_phase_for_flow(defn), 'planning')

    def test_autopilot_opens_on_worker_not_planner(self):
        defn = build_autopilot_definition()
        node = first_executed_role(defn)
        self.assertIsNotNone(node)
        self.assertEqual(node.get('role'), 'worker')
        # The whole point: a plannerless flow must NOT report 'planning'.
        self.assertEqual(initial_phase_for_flow(defn), 'working')

    def test_verifier_first_flow_reviews(self):
        defn = {
            'schema': 'tofu.orchestration/v1',
            'name': 'CriticFirst',
            'nodes': [
                {'id': 'start', 'type': 'control', 'kind': 'start'},
                {'id': 'c', 'type': 'role', 'role': 'critic',
                 'params': {'objective': 'review'}},
                {'id': 'stop', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [
                {'from': 'start', 'to': 'c'},
                {'from': 'c', 'to': 'stop'},
            ],
        }
        self.assertEqual(initial_phase_for_flow(defn), 'reviewing')

    def test_empty_or_roleless_defaults_working(self):
        self.assertEqual(initial_phase_for_flow({}), 'working')
        self.assertIsNone(first_executed_role({'nodes': [], 'edges': []}))


class FullOutputAdapterTest(unittest.TestCase):
    def test_full_output_not_truncated(self):
        long = 'X' * 5000
        adapter = FlowEventAdapter()
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'preview': long[:200], 'output': long,
                          'emits': 'assistant', 'state_changing': 1})
        self.assertEqual(len(adapter.messages), 1)
        self.assertEqual(adapter.messages[0]['content'], long)

    def test_preview_fallback_for_old_engine(self):
        # An un-upgraded engine that omits 'output' falls back to preview.
        adapter = FlowEventAdapter()
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'preview': 'only-preview', 'emits': 'assistant'})
        self.assertEqual(adapter.messages[0]['content'], 'only-preview')

    def test_empty_output_is_honored_over_preview(self):
        # output='' is an explicit empty turn — must NOT fall back to preview.
        adapter = FlowEventAdapter()
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'preview': 'stale', 'output': '',
                          'emits': 'assistant'})
        self.assertEqual(adapter.messages[0]['content'], '')


class RunTraceTest(unittest.TestCase):
    """The engine accumulates a per-node run trace with the resolved brief."""

    def _run(self, defn, runner):
        from lib.orchestration_engine import FlowExecutor
        ex = FlowExecutor(defn, agent_runner=runner)
        result = ex.run(initial_context='do the thing')
        return result, ex

    def test_trace_captures_brief_input_output_per_node(self):
        defn = build_verifier_loop_definition(max_iterations=3)
        seq = {'w': 0}

        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': 'WORKED ' + 'y' * 50, 'status': 'completed',
                        'error': '', 'tool_names': ['write_file']}
            if role == 'critic':
                return {'output': ('CONTINUE' if seq['w'] < 2 else '[VERDICT: STOP]'),
                        'status': 'completed', 'error': ''}
            return {'output': 'THE PLAN', 'status': 'completed', 'error': ''}

        result, ex = self._run(defn, runner)
        trace = result['trace']
        self.assertTrue(trace)
        # trace also exposed via the live property + monotonic seq.
        self.assertEqual([e['seq'] for e in ex.trace], list(range(1, len(trace) + 1)))

        roles = [e['role'] for e in trace]
        self.assertIn('planner', roles)
        self.assertIn('worker', roles)
        self.assertIn('critic', roles)

        planner_entry = next(e for e in trace if e['role'] == 'planner')
        # The resolved brief is the rendered role prompt the node ran with.
        self.assertTrue(planner_entry['brief'])
        self.assertEqual(planner_entry['output'], 'THE PLAN')
        # Worker turns carry their loop iteration + state-changing tools.
        worker_entries = [e for e in trace if e['role'] == 'worker']
        self.assertTrue(all(e['iteration'] >= 1 for e in worker_entries))
        self.assertEqual(worker_entries[0]['state_changing_tools'], ['write_file'])
        # The verifier's message axis is recorded as user-side.
        critic_entry = next(e for e in trace if e['role'] == 'critic')
        self.assertEqual(critic_entry['emits'], 'user')

    def test_trace_output_is_bounded(self):
        from lib.orchestration_engine import _TRACE_OUTPUT_CHARS
        defn = {
            'schema': 'tofu.orchestration/v1', 'name': 'Big',
            'nodes': [
                {'id': 'start', 'type': 'control', 'kind': 'start'},
                {'id': 'w', 'type': 'role', 'role': 'worker',
                 'params': {'objective': 'go'}},
                {'id': 'stop', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 'start', 'to': 'w'}, {'from': 'w', 'to': 'stop'}],
        }
        huge = 'Z' * (_TRACE_OUTPUT_CHARS + 5000)

        def runner(node, ctx, it):
            return {'output': huge, 'status': 'completed', 'error': ''}

        result, _ = self._run(defn, runner)
        entry = result['trace'][0]
        self.assertEqual(len(entry['output']), _TRACE_OUTPUT_CHARS)
        self.assertTrue(entry['output_truncated'])


class LiveStreamingTest(unittest.TestCase):
    """The adapter emits live SSE events (iteration → deltas → finalize)."""

    def test_step_start_opens_bubble_before_deltas(self):
        sse = []
        adapter = FlowEventAdapter(on_stream=sse.append)
        # Worker node: start → two deltas → complete.
        adapter.on_event({'type': 'step_start', 'role': 'worker',
                          'emits': 'assistant', 'node_id': 'w'})
        adapter.on_event({'type': 'step_delta', 'role': 'worker',
                          'emits': 'assistant', 'kind': 'content', 'chunk': 'Hel'})
        adapter.on_event({'type': 'step_delta', 'role': 'worker',
                          'emits': 'assistant', 'kind': 'content', 'chunk': 'lo'})
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'emits': 'assistant', 'output': 'Hello', 'preview': 'Hello',
                          'state_changing': 0})
        types = [e['type'] for e in sse]
        # iteration FIRST (bubble), then deltas, NO finalize for a worker turn.
        self.assertEqual(types[0], 'flow_iteration')
        self.assertEqual(sse[0]['phase'], 'working')
        self.assertEqual(sse[0]['iteration'], 1)
        self.assertEqual(types[1:], ['delta', 'delta'])
        self.assertEqual(''.join(e.get('content', '') for e in sse if e['type'] == 'delta'),
                         'Hello')

    def test_planner_finalizes_with_planner_done(self):
        sse = []
        adapter = FlowEventAdapter(on_stream=sse.append)
        adapter.on_event({'type': 'step_start', 'role': 'planner',
                          'emits': 'assistant', 'node_id': 'p'})
        adapter.on_event({'type': 'step_delta', 'role': 'planner',
                          'emits': 'assistant', 'kind': 'content', 'chunk': 'PLAN'})
        adapter.on_event({'type': 'step_complete', 'role': 'planner',
                          'emits': 'assistant', 'output': 'PLAN', 'preview': 'PLAN'})
        types = [e['type'] for e in sse]
        self.assertEqual(types[0], 'flow_iteration')
        self.assertEqual(sse[0]['phase'], 'planning')
        self.assertIn('delta', types)
        self.assertEqual(types[-1], 'flow_planner_done')
        self.assertEqual(sse[-1]['content'], 'PLAN')

    def test_verifier_finalizes_with_critic_msg(self):
        sse = []
        adapter = FlowEventAdapter(on_stream=sse.append)
        adapter.on_event({'type': 'step_start', 'role': 'virtual_user',
                          'emits': 'user', 'node_id': 'vu'})
        adapter.on_event({'type': 'step_complete', 'role': 'virtual_user',
                          'emits': 'user', 'output': 'keep going', 'preview': 'keep going'})
        types = [e['type'] for e in sse]
        self.assertEqual(types[0], 'flow_iteration')
        self.assertEqual(sse[0]['phase'], 'reviewing')
        self.assertEqual(types[-1], 'flow_critic_msg')
        self.assertEqual(sse[-1]['next_phase'], 'worker')

    def test_thinking_delta_routed_as_thinking(self):
        sse = []
        adapter = FlowEventAdapter(on_stream=sse.append)
        adapter.on_event({'type': 'step_start', 'role': 'worker',
                          'emits': 'assistant', 'node_id': 'w'})
        adapter.on_event({'type': 'step_delta', 'role': 'worker',
                          'emits': 'assistant', 'kind': 'thinking', 'chunk': 'hmm'})
        deltas = [e for e in sse if e['type'] == 'delta']
        self.assertEqual(deltas[0].get('thinking'), 'hmm')
        self.assertNotIn('content', deltas[0])

    def test_emit_and_stream_are_independent_channels(self):
        msgs, sse = [], []
        adapter = FlowEventAdapter(emit=msgs.append, on_stream=sse.append)
        adapter.on_event({'type': 'step_start', 'role': 'worker',
                          'emits': 'assistant', 'node_id': 'w'})
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'emits': 'assistant', 'output': 'done', 'preview': 'done'})
        # emit fired once (DB) with the message; stream carries the iteration.
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]['content'], 'done')
        self.assertTrue(any(e['type'] == 'flow_iteration' for e in sse))


if __name__ == '__main__':
    unittest.main()
