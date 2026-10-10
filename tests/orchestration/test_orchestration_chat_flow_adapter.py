"""tests/orchestration/test_orchestration_chat_flow_adapter.py — FlowExecutor→flow UI.

Drives a real FlowExecutor (mock agent runner) through the adapter and
asserts the emitted messages match the chat-flow display schema
(_isFlowPlanner / _flowIteration / _isFlowReview / _flowNextPhase).
"""

import unittest

import pytest

from lib.orchestration.chat_flow_adapter import FlowEventAdapter
from lib.orchestration.engine import FlowExecutor
from tests.support.orchestration_definitions import (
    build_verifier_loop_definition,
)


pytestmark = pytest.mark.unit

# Fake edited-file markers in the tool_log fixtures — not real repo files.
_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/a.py', 'lib/b.py'}


def test_flow_wire_policy_is_physically_separate_from_stateful_adapter():
    from pathlib import Path

    from lib.orchestration.chat_flow_projection import (
        flow_emits_for_role,
        project_flow_next_phase,
        project_flow_phase_event,
        project_flow_turn_metadata,
    )

    assert FlowEventAdapter._derive_emits is flow_emits_for_role
    assert project_flow_turn_metadata(
        'virtual_user', '', projection='autopilot',
        vu_msg_id='msg-1', vu_run_id='run-1',
    ) == {
        'flowProjection': 'autopilot',
        'turnRole': 'virtual_user',
        'emits': 'user',
        'vuMsgId': 'msg-1',
        'autopilotRunId': 'run-1',
    }
    assert project_flow_next_phase(
        'continue', pending_replan=True) == 'planner'
    assert project_flow_next_phase('[VERDICT: STOP]') == 'stop'
    assert project_flow_phase_event({
        'phase': 'retrying',
        'detail': 'rate limited',
        'attempt': 2,
        'status_code': 429,
        'detailKey': 'stream.phase.retryRateLimited',
        'detailArgs': {'seconds': 3},
        'model': 'deepseek-v4-pro',
        'modelRoute': {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        },
    }) == {
        'type': 'phase',
        'phase': 'retrying',
        'detail': 'rate limited',
        'attempt': 2,
        'statusCode': 429,
        'detailKey': 'stream.phase.retryRateLimited',
        'detailArgs': {'seconds': 3},
        'model': 'deepseek-v4-pro',
        'modelRoute': {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        },
    }

    adapter_source = Path(
        'lib/orchestration/chat_flow_adapter.py').read_text()
    assert 'VERIFIER_ROLES' not in adapter_source
    assert 'build_phase(' not in adapter_source
    assert 'def _derive_emits' not in adapter_source


def _run(defn, runner):
    adapter = FlowEventAdapter()
    FlowExecutor(defn, agent_runner=runner, on_event=adapter.on_event).run()
    return adapter.messages


class AdapterTest(unittest.TestCase):
    def test_flow_schema_shape(self):
        defn = build_verifier_loop_definition(max_iterations=5)
        seq = {'w': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': f'work{seq["w"]}', 'status': 'completed',
                        'error': '', 'tool_log': [{
                            'tool': 'write_file', 'status': 'done',
                            'toolArgs': {'append': True, 'content': 'new line'},
                            'results': [{'writeOk': True}],
                        }]}
            if role == 'critic':
                return {'output': ('CONTINUE: more' if seq['w'] < 2 else '[VERDICT: STOP]'),
                        'status': 'completed', 'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}
        msgs = _run(defn, runner)

        planners = [m for m in msgs if m.get('_isFlowPlanner')]
        workers = [m for m in msgs if m.get('_flowIteration') and not m.get('_isFlowReview')]
        critics = [m for m in msgs if m.get('_isFlowReview')]

        self.assertEqual(len(planners), 1)
        self.assertEqual(planners[0]['role'], 'assistant')
        self.assertEqual(planners[0]['_flowPlannerIteration'], 1)

        self.assertEqual(len(workers), 2)
        self.assertEqual([w['_flowIteration'] for w in workers], [1, 2])
        self.assertEqual(workers[0]['role'], 'assistant')
        self.assertEqual(workers[0]['_flowStateChangingCount'], 1)

        self.assertTrue(critics)
        self.assertEqual(critics[0]['role'], 'user')
        # final critic approved (STOP)
        self.assertTrue(critics[-1]['_flowApproved'])
        self.assertEqual(critics[-1]['_flowNextPhase'], 'stop')

    def test_replan_bumps_planner_iteration(self):
        defn = build_verifier_loop_definition(max_iterations=6)
        seq = {'c': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                seq['c'] += 1
                if seq['c'] == 1:
                    return {'output': '[PLAN_DEFECT: missing build step]\n[VERDICT: CONTINUE_PLANNER]',
                            'status': 'completed', 'error': ''}
                return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
            if role == 'worker':
                return {'output': 'w', 'status': 'completed', 'error': '', 'tool_names': ['write_file']}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}
        msgs = _run(defn, runner)
        planners = [m for m in msgs if m.get('_isFlowPlanner')]
        # initial planner + 1 replan → two planner messages, iterations 1 & 2
        self.assertEqual([p['_flowPlannerIteration'] for p in planners], [1, 2])
        # the critic that triggered the replan points to 'planner'
        replan_critics = [m for m in msgs if m.get('_isFlowReview')
                          and m.get('_flowNextPhase') == 'planner']
        self.assertTrue(replan_critics)

    def test_zero_deliverable_emits_synthetic_critic(self):
        defn = build_verifier_loop_definition(max_iterations=5)
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                return {'output': 'analysis', 'status': 'completed',
                        'error': '', 'tool_names': ['read_files']}
            if role == 'critic':
                return {'output': 'CONTINUE: distinct words vary each time ' + str(id(ctx)),
                        'status': 'completed', 'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}
        msgs = _run(defn, runner)
        synthetic = [m for m in msgs if m.get('_isSyntheticCritic')]
        self.assertTrue(synthetic)
        self.assertEqual(synthetic[0]['_flowNextPhase'], 'worker')

    def test_live_emit_callback(self):
        defn = build_verifier_loop_definition(max_iterations=3)
        emitted = []
        adapter = FlowEventAdapter(emit=emitted.append)
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
            return {'output': 'x', 'status': 'completed', 'error': '', 'tool_names': ['write_file']}
        FlowExecutor(defn, agent_runner=runner, on_event=adapter.on_event).run()
        # emit callback saw the same messages as the accumulator
        self.assertEqual(len(emitted), len(adapter.messages))
        self.assertTrue(any(m.get('_isFlowPlanner') for m in emitted))


class AdapterToolRoundsTest(unittest.TestCase):
    """A node's bounded tool_log becomes standard chat toolRounds."""

    @staticmethod
    def _tool_log():
        return [
            # finished ok
            {'round': 1, 'tool': 'run_command',
             'args_brief': 'pytest -x', 'timestamp': 1700000000.0,
             'preview': '12 passed', 'preview_full_chars': 9,
             'preview_truncated': False, 'error': '',
             'error_full_chars': 0, 'error_truncated': False},
            # finished with an error
            {'round': 2, 'tool': 'write_file',
             'args_brief': 'Write lib/foo.py — add retry',
             'timestamp': 1700000001.0,
             'preview': '', 'preview_full_chars': 0,
             'preview_truncated': False,
             'error': 'PermissionError: denied',
             'error_full_chars': 22, 'error_truncated': False},
            # dispatched but never finished (run aborted mid-call)
            {'round': 3, 'tool': 'read_files',
             'args_brief': 'Read lib/foo.py',
             'timestamp': 1700000002.0, 'preview': ''},
            # old row whose preview was compacted away, size marker kept
            {'round': 4, 'tool': 'web_search',
             'args_brief': 'tofu release notes',
             'timestamp': 1700000003.0,
             'preview': '', 'preview_full_chars': 4123,
             'preview_truncated': True, 'error': '',
             'error_full_chars': 0, 'error_truncated': False},
        ]

    def _run_with_log(self, tool_log):
        defn = build_verifier_loop_definition(max_iterations=5)
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                result = {'output': 'done', 'status': 'completed',
                          'error': ''}
                if tool_log is not None:
                    result['tool_log'] = tool_log
                return result
            if role == 'critic':
                return {'output': '[VERDICT: STOP]', 'status': 'completed',
                        'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}
        return _run(defn, runner)

    def test_worker_message_projects_tool_log_into_tool_rounds(self):
        msgs = self._run_with_log(self._tool_log())
        workers = [m for m in msgs if m.get('_flowIteration')
                   and not m.get('_isFlowReview')]
        self.assertEqual(len(workers), 1)
        rounds = workers[0]['toolRounds']

        self.assertEqual([r['status'] for r in rounds],
                         ['done', 'error', 'aborted', 'done'])
        self.assertEqual([r['roundNum'] for r in rounds], [1, 2, 3, 4])
        self.assertEqual(len({r['toolCallId'] for r in rounds}), 4)

        self.assertEqual(rounds[0]['toolCallId'], 'flow-tool-1')

        done = rounds[0]
        self.assertEqual(done['toolName'], 'run_command')
        self.assertEqual(done['query'], 'pytest -x')
        self.assertEqual(done['toolContent'], '12 passed')
        self.assertEqual(done['llmRound'], 1)
        self.assertEqual(done['tStart'], 1700000000000)
        self.assertEqual(len(done['results']), 1)
        self.assertEqual(done['results'][0]['fetchedChars'], 9)
        self.assertTrue(done['results'][0]['fetched'])

        failed = rounds[1]
        self.assertEqual(failed['toolContent'], 'PermissionError: denied')
        self.assertNotIn('results', failed)

        aborted = rounds[2]
        self.assertNotIn('toolContent', aborted)
        self.assertNotIn('results', aborted)

        compacted = rounds[3]
        self.assertNotIn('toolContent', compacted)
        self.assertEqual(compacted['results'][0]['fetchedChars'], 4123)
        self.assertFalse(compacted['results'][0]['fetched'])

    def test_rounds_are_display_only_and_results_stay_a_list(self):
        msgs = self._run_with_log(self._tool_log())
        workers = [m for m in msgs if m.get('_flowIteration')
                   and not m.get('_isFlowReview')]
        for entry in workers[0]['toolRounds']:
            self.assertNotIn('preview', entry)
            self.assertNotIn('args_brief', entry)
            if 'results' in entry:
                self.assertIsInstance(entry['results'], list)

    def test_messages_without_tool_log_carry_no_tool_rounds_key(self):
        msgs = self._run_with_log(None)
        for msg in msgs:
            self.assertNotIn('toolRounds', msg)

class AutopilotProjectionContractTest(unittest.TestCase):
    """A VU graph uses flow transport without inheriting critic meaning."""

    def test_projection_is_derived_from_semantic_roles(self):
        from lib.orchestration._builtin_definitions import (
            build_autopilot_definition,
        )
        from lib.orchestration._chat_projection import chat_projection_for_flow

        self.assertEqual(
            chat_projection_for_flow(build_autopilot_definition()),
            'autopilot',
        )
        self.assertEqual(
            chat_projection_for_flow(build_verifier_loop_definition()),
            'critic',
        )
        # A richer VU graph may also contain a planner; VU identity wins.
        mixed = build_verifier_loop_definition()
        mixed['nodes'].append({
            'id': 'vu-extra', 'type': 'role', 'role': 'virtual_user',
        })
        self.assertEqual(chat_projection_for_flow(mixed), 'autopilot')
        generic = {
            'nodes': [{'id': 'writer', 'type': 'role', 'role': 'writer'}],
        }
        self.assertEqual(chat_projection_for_flow(generic), 'flow')
        nested = {
            'nodes': [{
                'id': 'box', 'type': 'subflow',
                'params': {'scope': 'isolated', 'definition':
                           build_autopilot_definition()},
            }],
        }
        self.assertEqual(chat_projection_for_flow(nested), 'autopilot')

    def test_tool_lifecycle_is_live_on_current_turn_and_terminal_id_matches(self):
        streamed = []
        adapter = FlowEventAdapter(
            on_stream=streamed.append,
            projection='autopilot',
            vu_flow=True,
            vu_run_id='run-tools',
        )
        adapter.on_event({
            'type': 'step_start', 'node_id': 'worker',
            'role': 'worker', 'emits': 'assistant',
        })
        for event_type, fields in (
            ('tool_start', {'query': 'Read a.py'}),
            ('tool_result', {'results': [], 'status': 'done'}),
            ('tool_complete', {'toolContent': 'ok'}),
        ):
            adapter.on_event({
                'type': 'step_tool_event', 'node_id': 'worker',
                'role': 'worker', 'emits': 'assistant',
                'event': {
                    'type': event_type, 'roundNum': 1,
                    'toolCallId': 'flow-tool-occurrence',
                    'toolName': 'read_files', **fields,
                },
            })
        adapter.on_event({
            'type': 'step_complete', 'node_id': 'worker',
            'role': 'worker', 'emits': 'assistant', 'output': 'done',
            'tool_log': [{
                'round': 1, 'tool': 'read_files',
                'tool_call_id': 'flow-tool-occurrence',
                'args_brief': 'Read a.py', 'preview': 'ok',
                'preview_full_chars': 2, 'error': '',
                'error_full_chars': 0, 'status': 'done',
            }],
        })

        lifecycle = [event for event in streamed
                     if event.get('type', '').startswith('tool_')]
        self.assertEqual([event['type'] for event in lifecycle], [
            'tool_start', 'tool_result', 'tool_complete'])
        self.assertTrue(all(
            event['flowProjection'] == 'autopilot'
            and event['turnRole'] == 'worker'
            and event['emits'] == 'assistant'
            for event in lifecycle))
        self.assertEqual(
            adapter.messages[0]['toolRounds'][0]['toolCallId'],
            'flow-tool-occurrence')

    def test_virtual_user_tool_lifecycle_carries_vu_identity(self):
        streamed = []
        adapter = FlowEventAdapter(
            on_stream=streamed.append,
            projection='autopilot', vu_flow=True, vu_run_id='run-vu-tools')
        adapter.on_event({
            'type': 'step_start', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
        })
        vu_msg_id = streamed[-1]['vuMsgId']
        adapter.on_event({
            'type': 'step_tool_event', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
            'event': {'type': 'tool_start', 'roundNum': 1,
                      'toolCallId': 'flow-vu-tool',
                      'toolName': 'project_board_read',
                      'query': 'Project board'},
        })
        event = streamed[-1]
        self.assertEqual(event['type'], 'tool_start')
        self.assertEqual(event['vuMsgId'], vu_msg_id)
        self.assertEqual(event['autopilotRunId'], 'run-vu-tools')
        self.assertEqual(event['turnRole'], 'virtual_user')
        self.assertEqual(event['emits'], 'user')

    def test_virtual_user_live_identity_matches_persisted_turn(self):
        streamed = []
        adapter = FlowEventAdapter(
            on_stream=streamed.append,
            projection='autopilot',
            vu_flow=True,
            vu_run_id='run-1',
        )
        adapter.on_event({
            'type': 'step_start', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
        })
        start = streamed[-1]
        self.assertEqual(start['type'], 'flow_iteration')
        self.assertEqual(start['flowProjection'], 'autopilot')
        self.assertEqual(start['turnRole'], 'virtual_user')
        self.assertTrue(start['vuMsgId'])
        self.assertEqual(start['autopilotRunId'], 'run-1')

        adapter.on_event({
            'type': 'step_delta', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
            'kind': 'content', 'chunk': 'Keep going',
        })
        delta = streamed[-1]
        self.assertEqual(delta['vuMsgId'], start['vuMsgId'])
        self.assertEqual(delta['turnRole'], 'virtual_user')

        adapter.on_event({
            'type': 'step_complete', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
            'output': '[PROGRESS: resolved=1 remaining=2]\nKeep going',
        })
        self.assertEqual(len(adapter.messages), 1)
        vu_msg = adapter.messages[0]
        self.assertTrue(vu_msg['_isVirtualUser'])
        self.assertNotIn('_isFlowReview', vu_msg)
        self.assertEqual(vu_msg['_msgId'], start['vuMsgId'])
        self.assertEqual(vu_msg['_autopilotRunId'], 'run-1')
        self.assertEqual(vu_msg['content'], 'Keep going')

    def test_task_done_settles_visible_phase_without_persisting_control_token(self):
        streamed = []
        adapter = FlowEventAdapter(
            on_stream=streamed.append,
            projection='autopilot',
            vu_flow=True,
            vu_run_id='run-2',
        )
        adapter.on_event({
            'type': 'step_start', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
        })
        vu_msg_id = streamed[-1]['vuMsgId']
        adapter.on_event({
            'type': 'step_complete', 'node_id': 'vu',
            'role': 'virtual_user', 'emits': 'user',
            'output': '[VU: TASK_DONE]',
        })
        self.assertEqual(len(adapter.messages), 1)
        self.assertEqual(adapter.messages[0]['content'], '')
        self.assertTrue(adapter.messages[0]['_flowLivePhase'])
        terminal = streamed[-1]
        self.assertEqual(terminal['type'], 'flow_critic_msg')
        self.assertEqual(terminal['vuMsgId'], vu_msg_id)
        self.assertEqual(terminal['next_phase'], 'stop')
        self.assertFalse(terminal.get('discard', False))
        self.assertEqual(terminal['content'], '')

    def test_step_phase_for_producer_becomes_wire_phase(self):
        """Engine ``step_phase`` for an assistant producer → wire ``phase``
        event on the live stream (the "waiting for model…" signal), and is
        NOT a delta (so it can't pollute the assistant content)."""
        streamed = []
        adapter = FlowEventAdapter(on_stream=streamed.append)
        adapter.on_event({'type': 'step_phase', 'node_id': 'worker',
                          'role': 'worker', 'emits': 'assistant',
                          'phase': 'waiting_model',
                          'detail': 'Sent to the model, waiting…'})
        phases = [e for e in streamed if e.get('type') == 'phase']
        self.assertEqual(len(phases), 1)
        self.assertEqual(phases[0]['phase'], 'waiting_model')
        self.assertIn('waiting', phases[0]['detail'].lower())
        # Never emitted as a delta (would pollute assistantMsg.content).
        self.assertFalse([e for e in streamed if e.get('type') == 'delta'])

    def test_step_complete_persists_actual_role_routed_model(self):
        adapter = FlowEventAdapter()
        route = {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        }
        adapter.on_event({
            'type': 'step_start', 'node_id': 'worker',
            'role': 'worker', 'emits': 'assistant',
        })
        adapter.on_event({
            'type': 'step_complete', 'node_id': 'worker',
            'role': 'worker', 'emits': 'assistant',
            'output': 'done', 'model': 'deepseek-v4-pro',
            'modelRoute': route,
        })

        self.assertEqual(len(adapter.messages), 1)
        message = adapter.messages[0]
        self.assertEqual(message['model'], 'deepseek-v4-pro')
        self.assertEqual(message['orchestration']['modelRoute'], route)

    def test_step_phase_for_verifier_is_skipped(self):
        """A verifier (user-side) producer's phase would land on the wrong
        bubble — the adapter must drop it."""
        streamed = []
        adapter = FlowEventAdapter(on_stream=streamed.append)
        adapter.on_event({'type': 'step_phase', 'node_id': 'vu',
                          'role': 'virtual_user', 'emits': 'user',
                          'phase': 'retrying', 'detail': 'Retrying…'})
        self.assertFalse(streamed)



class ThinkingPropagationTest(unittest.TestCase):
    """The orchestration-flow path must carry per-node thinking through
    finalize so the bubble's thinking block survives (the "thinking refreshes
    then disappears instantly" bug). Asserts:
      • the finalized MESSAGE dict (planner / worker / critic) carries
        ``thinking``;
      • the finalizing SSE events (``flow_planner_done`` /
        ``flow_critic_msg``) carry ``thinking``.
    Revert-proof: dropping ``thinking`` from any of those sites fails here.
    """

    def _drive(self):
        defn = build_verifier_loop_definition(max_iterations=3)
        streamed = []
        adapter = FlowEventAdapter(on_stream=streamed.append)

        def runner(node, ctx, it):
            role = node.get('role')
            # Mock runner mirrors the real _default_runner return shape,
            # INCLUDING the new 'thinking' key the engine now populates from
            # the accumulated stream chunks.
            if role == 'worker':
                return {'output': 'work done', 'status': 'completed',
                        'error': '', 'tool_names': ['write_file'],
                        'thinking': 'WORKER-REASONING'}
            if role == 'critic':
                return {'output': '[VERDICT: STOP]', 'status': 'completed',
                        'error': '', 'thinking': 'CRITIC-REASONING'}
            return {'output': 'PLAN', 'status': 'completed', 'error': '',
                    'thinking': 'PLANNER-REASONING'}

        FlowExecutor(defn, agent_runner=runner,
                     on_event=adapter.on_event).run()
        return adapter.messages, streamed

    def test_messages_carry_thinking(self):
        msgs, _ = self._drive()
        planner = [m for m in msgs if m.get('_isFlowPlanner')][0]
        worker = [m for m in msgs
                  if m.get('_flowIteration') and not m.get('_isFlowReview')][0]
        critic = [m for m in msgs if m.get('_isFlowReview')][-1]
        self.assertEqual(planner.get('thinking'), 'PLANNER-REASONING')
        self.assertEqual(worker.get('thinking'), 'WORKER-REASONING')
        self.assertEqual(critic.get('thinking'), 'CRITIC-REASONING')

    def test_finalize_sse_events_carry_thinking(self):
        _, streamed = self._drive()
        planner_done = [e for e in streamed
                        if e.get('type') == 'flow_planner_done']
        critic_msg = [e for e in streamed
                      if e.get('type') == 'flow_critic_msg']
        self.assertTrue(planner_done)
        self.assertTrue(critic_msg)
        self.assertEqual(planner_done[0].get('thinking'), 'PLANNER-REASONING')
        self.assertEqual(critic_msg[-1].get('thinking'), 'CRITIC-REASONING')

    def test_engine_step_complete_and_trace_carry_full_streamed_thinking(self):
        """Real graph → shared invocation → provider stream → terminal trace.

        Only context acquisition and provider transport are synthetic. Every
        role uses the ordinary preparation, stream, and settlement owners; a
        display preview must never replace its full reasoning transcript.
        """
        import copy
        from contextlib import ExitStack
        from unittest.mock import patch

        from lib.llm.stream_result import ProviderStreamResult
        from lib.tasks_pkg.manager import create_task, discard_task
        import lib.tasks_pkg.orchestrator._run as runner
        import lib.tasks_pkg.orchestrator._root_agent_loop as loop
        import lib.tasks_pkg.orchestrator._ports as ports
        import lib.tasks_pkg.manager._stream as stream

        big_think = 'THINK-' * 1000
        requests = []
        events = []

        def fake_dispatch(body, **kwargs):
            requests.append(copy.deepcopy(body))
            self.assertEqual(kwargs['owner_user_id'], 71)
            self.assertEqual(body['model'], 'fixture-model')
            kwargs['on_thinking'](big_think[:3000])
            kwargs['on_thinking'](big_think[3000:])
            output = 'Final answer.\n[VERDICT: STOP]'
            kwargs['on_content'](output)
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': output,
                 'reasoning_content': big_think},
                'stop', {'total_tokens': 3})

        def fake_build_body(model, messages, **kwargs):
            return {'model': model, 'messages': copy.deepcopy(messages),
                    'tools': copy.deepcopy(kwargs['tools'])}

        parent = create_task('', [], {
            'model': 'fixture-model', 'memoryEnabled': False, 'searchMode': 'off',
        }, user_id=71, transient=True, supersede=False)
        try:
            with ExitStack() as patches:
                for name in ('start_prefetches', 'stop_prefetches',
                             'maybe_run_memory_prefetch', 'inject_context_and_emit_chips'):
                    patches.enter_context(patch.object(runner, name, return_value=None))
                for name in ('run_round_message_hygiene', 'drain_and_inject_inbox'):
                    patches.enter_context(patch.object(loop, name, return_value=None))
                patches.enter_context(patch.object(ports, 'build_request_body', fake_build_body))
                patches.enter_context(patch.object(stream, 'dispatch_stream', fake_dispatch))
                result = FlowExecutor(
                    build_verifier_loop_definition(max_iterations=1),
                    parent_task=parent, on_event=events.append,
                    all_tools=[], model='fixture-model',
                    model_routing_policy='selected',
                ).run(initial_context='Verify the synthetic evidence.')
            self.assertTrue(result['ok'], result.get('error'))
            self.assertNotEqual(parent['status'], 'done')
        finally:
            discard_task(parent['id'])

        self.assertEqual(len(requests), 3)  # one planner, worker, critic
        completes = [e for e in events if e.get('type') == 'step_complete']
        traces = [e for e in events if e.get('type') == 'step_trace']
        self.assertEqual({e['role'] for e in completes}, {'planner', 'worker', 'critic'})
        self.assertEqual({e['role'] for e in traces}, {'planner', 'worker', 'critic'})
        for event in completes + traces:
            self.assertEqual(event['status'], 'completed', event.get('error'))
            self.assertEqual(event['thinking'], big_think)
        for event in completes:
            segments = event['outputProjection']['segments']
            self.assertEqual(''.join(segment.get('text', '') for segment in segments
                                     if segment['type'] == 'thinking'), big_think)


class AdapterErrorFieldTest(unittest.TestCase):
    """A failed leaf (e.g. the worker's LLM call died) must surface its real
    error on the durable message: finish_info renders msg.error as the
    terminal error tag instead of a bare ✓ over the placeholder text."""

    def _worker_step(self, adapter, **complete):
        adapter.on_event({'type': 'step_start', 'role': 'worker',
                          'emits': 'assistant'})
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'emits': 'assistant', 'thinking': '', **complete})

    def test_failed_worker_step_carries_error(self):
        adapter = FlowEventAdapter()
        self._worker_step(
            adapter, status='failed',
            error='LLM call failed at round 3: Bad request (HTTP 400)',
            output='[LLM error at round 3] No substantive answer was produced.',
            state_changing=0)
        workers = [m for m in adapter.messages
                   if m.get('role') == 'assistant']
        self.assertEqual(len(workers), 1)
        self.assertEqual(
            workers[0]['error'],
            'LLM call failed at round 3: Bad request (HTTP 400)')

    def test_healthy_worker_step_has_no_error_key(self):
        adapter = FlowEventAdapter()
        self._worker_step(adapter, status='completed', error='',
                          output='done', state_changing=1)
        self.assertEqual(len(adapter.messages), 1)
        self.assertNotIn('error', adapter.messages[0])

    def test_failed_planner_step_carries_error(self):
        adapter = FlowEventAdapter()
        adapter.on_event({'type': 'step_start', 'role': 'planner',
                          'emits': 'assistant'})
        adapter.on_event({'type': 'step_complete', 'role': 'planner',
                          'emits': 'assistant', 'thinking': '',
                          'status': 'failed', 'error': 'dispatch exhausted',
                          'output': ''})
        planners = [m for m in adapter.messages if m.get('_isFlowPlanner')]
        self.assertEqual(len(planners), 1)
        self.assertEqual(planners[0]['error'], 'dispatch exhausted')


class AdapterModifiedFilesTest(unittest.TestCase):
    """A flow node's edit-tool markers must become the durable message's
    modifiedFileList/modifiedFiles — the settled file-changes block derives
    from them exactly like a normal chat turn's journal derive."""

    def _worker_step(self, adapter, **complete):
        adapter.on_event({'type': 'step_start', 'role': 'worker',
                          'emits': 'assistant'})
        adapter.on_event({'type': 'step_complete', 'role': 'worker',
                          'emits': 'assistant', 'thinking': '', **complete})

    def test_worker_step_with_edit_markers_stamps_modified_files(self):
        adapter = FlowEventAdapter()
        self._worker_step(
            adapter, status='completed', output='done', state_changing=2,
            tool_log=[
                {'round': 1, 'tool': 'write_file',
                 'args_brief': 'a.py', 'preview': 'ok',
                 'preview_full_chars': 2,
                 'edited_path': 'lib/a.py', 'edited_action': 'written'},
                {'round': 2, 'tool': 'edit_file',
                 'args_brief': 'b.py', 'preview': 'ok',
                 'preview_full_chars': 2,
                 'edited_path': 'lib/b.py', 'edited_action': 'edited'},
                # Last write per path wins; first-appearance order kept.
                {'round': 3, 'tool': 'apply_diff',
                 'args_brief': 'b.py', 'preview': 'ok2',
                 'preview_full_chars': 3,
                 'edited_path': 'lib/b.py', 'edited_action': 'patched'},
                {'round': 4, 'tool': 'read_files',
                 'args_brief': 'Read a.py', 'preview': 'ok',
                 'preview_full_chars': 2},
            ])
        self.assertEqual(len(adapter.messages), 1)
        msg = adapter.messages[0]
        self.assertEqual(msg['modifiedFiles'], 2)
        self.assertEqual(msg['modifiedFileList'], [
            {'action': 'written', 'path': 'lib/a.py'},
            {'action': 'patched', 'path': 'lib/b.py'},
        ])

    def test_worker_step_without_edit_markers_omits_modified_files(self):
        """Negative control: tool-less / read-only turns stay byte-identical
        (no empty modifiedFileList/modifiedFiles keys leak onto the wire)."""
        adapter = FlowEventAdapter()
        self._worker_step(
            adapter, status='completed', output='done', state_changing=0,
            tool_log=[{'round': 1, 'tool': 'read_files',
                       'args_brief': 'Read a.py', 'preview': 'ok',
                       'preview_full_chars': 2}])
        self.assertEqual(len(adapter.messages), 1)
        self.assertNotIn('modifiedFileList', adapter.messages[0])
        self.assertNotIn('modifiedFiles', adapter.messages[0])

if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_orchestration_chat_flow_adapter.__main__')
    unittest.main()
