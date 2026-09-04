"""tests/test_orchestration_chat_flow_adapter.py — FlowExecutor→flow UI.

Drives a real FlowExecutor (mock agent runner) through the adapter and
asserts the emitted messages match the chat-flow display schema
(_isFlowPlanner / _flowIteration / _isFlowReview / _flowNextPhase).
"""

import unittest

import pytest

from lib.orchestration_chat_flow_adapter import FlowEventAdapter
from lib.orchestration_engine import FlowExecutor
from tests.support.orchestration_definitions import (
    build_verifier_loop_definition,
)


pytestmark = pytest.mark.unit


def test_flow_wire_policy_is_physically_separate_from_stateful_adapter():
    from pathlib import Path

    from lib.orchestration_chat_flow_projection import (
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
        'lib/orchestration_chat_flow_adapter.py').read_text()
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
                        'error': '', 'tool_names': ['write_file']}
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

    def test_task_done_discards_placeholder_and_never_persists_token(self):
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
        self.assertEqual(adapter.messages, [])
        terminal = streamed[-1]
        self.assertEqual(terminal['type'], 'flow_critic_msg')
        self.assertEqual(terminal['vuMsgId'], vu_msg_id)
        self.assertEqual(terminal['next_phase'], 'stop')
        self.assertTrue(terminal['discard'])
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

    def test_subagent_waiting_phase_end_to_end(self):
        """FULL path: a real SubAgent whose dispatch fires on_retry (the
        rate-limited cooldown wait) → engine _stream_sink → step_phase →
        adapter → wire ``phase`` event. Proves the live "waiting for model…"
        signal reaches the stream during a stall, with NO real LLM."""
        from lib.orchestration._builtin_definitions import (
            build_autopilot_definition,
        )
        from lib.swarm.protocol import SubAgentStatus

        streamed = []
        adapter = FlowEventAdapter(on_stream=streamed.append)

        def fake_dispatch(body, *, on_content=None, on_thinking=None,
                          abort_check=None, prefer_model='', log_prefix='',
                          on_retry=None, **kw):
            # Simulate the dispatcher entering the cooldown wait (rate-limited
            # strict_model) BEFORE the first token — exactly the 304s stall.
            if on_retry:
                on_retry(attempt=1,
                         reason='Waiting for model (rate-limited)',
                         status_code=429)
            # Then a token arrives and the turn finishes (no tool calls).
            if on_content:
                on_content('done analysing')
            return ({'role': 'assistant', 'content': 'done analysing'},
                    'stop', {'total_tokens': 5})

        def fake_build_body(**kw):
            return {'model': kw.get('model', 'm'), 'messages': []}

        # A SubAgent-backed runner that injects the DI mocks (so no real LLM).
        from lib.swarm.agent import SubAgent
        from lib.swarm.protocol import SubTaskSpec
        from lib.orchestration._execution_projection import render_role_brief
        from lib.orchestration._role_axes import resolve_emits

        def subagent_runner(node, ctx, it):
            role = node.get('role', 'general')
            spec = SubTaskSpec(role=role,
                               objective=render_role_brief(node) or 'go',
                               context=ctx)
            nid = node.get('id')
            emits = resolve_emits(node)

            def _sink(kind, chunk, *, phase='', **meta):
                # Mirror the engine's real _stream_sink mapping.
                ev = ({'type': 'step_phase', 'node_id': nid, 'role': role,
                       'emits': emits, 'phase': phase or 'working',
                       'detail': chunk, **meta}
                      if kind == 'phase' else
                      {'type': 'step_delta', 'node_id': nid, 'role': role,
                       'emits': emits, 'kind': kind, 'chunk': chunk})
                adapter.on_event(ev)

            agent = SubAgent(spec, parent_task={'id': 't', 'convId': 'c',
                                                'config': {}},
                             all_tools=[], model='m',
                             build_body_fn=fake_build_body,
                             dispatch_stream_fn=fake_dispatch,
                             stream_sink=_sink)
            r = agent.run()
            # Autopilot VU stops the loop on TASK_DONE.
            out = ('[VU: TASK_DONE]\n[PROGRESS: resolved=1 remaining=0]'
                   if role == 'virtual_user'
                   else (r.final_answer or ''))
            return {'output': out,
                    'status': SubAgentStatus.COMPLETED.value, 'error': ''}

        defn = build_autopilot_definition(max_iterations=1)
        FlowExecutor(defn, agent_runner=subagent_runner,
                     on_event=adapter.on_event).run()

        phases = [e for e in streamed if e.get('type') == 'phase'
                  and e.get('phase') in ('waiting_model', 'retrying')]
        self.assertTrue(phases, 'expected a waiting/retrying phase on the stream')
        # The pre-dispatch 'waiting_model' fired first.
        self.assertEqual(phases[0]['phase'], 'waiting_model')
        # The on_retry cooldown signal surfaced as a 'retrying' phase carrying
        # the 429 status (the rate-limited-stall signal — the whole point).
        retrying = [e for e in phases if e.get('phase') == 'retrying']
        self.assertTrue(retrying, 'expected a retrying phase from on_retry')
        self.assertEqual(retrying[0]['statusCode'], 429)
        # The worker also streamed its content as a delta (not the phase).
        self.assertTrue(any(e.get('type') == 'delta' and e.get('content')
                            for e in streamed))


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
        """End-to-end via the engine's REAL default SubAgent runner (no LLM):
        the engine's ``_stream_sink`` accumulates the FULL streamed thinking
        (not the 2000-char-capped ``reasoning_trace``) and puts it on BOTH the
        ``step_complete`` event and the durable ``step_trace`` entry.

        Patches ``lib.swarm.agent._default_dispatch_stream`` /
        ``_default_build_body`` so every node runs the real
        ``FlowExecutor._default_runner`` path (the production path) with no
        network. The mocked dispatch streams a >2000-char thinking blob; the
        critic's 'final out' classifies as STOP (loose fallback) → one
        iteration."""
        import lib.swarm.agent as agent_mod

        big_think = 'THINK-' * 1000  # 6000 chars > reasoning_trace 2000 cap

        def fake_dispatch(body, *, on_content=None, on_thinking=None,
                          abort_check=None, prefer_model='', log_prefix='',
                          on_retry=None, **kw):
            if on_thinking:
                on_thinking(big_think)
            if on_content:
                on_content('final out')
            return ({'role': 'assistant', 'content': 'final out'},
                    'stop', {'total_tokens': 3})

        def fake_build_body(**kw):
            return {'model': kw.get('model', 'm'), 'messages': []}

        events = []
        orig_dispatch = agent_mod._default_dispatch_stream
        orig_build = agent_mod._default_build_body
        agent_mod._default_dispatch_stream = fake_dispatch
        agent_mod._default_build_body = fake_build_body
        try:
            defn = build_verifier_loop_definition(max_iterations=1)
            # agent_runner=None → engine uses its built-in _default_runner
            # (the production code path under test).
            FlowExecutor(defn, agent_runner=None,
                         on_event=events.append,
                         all_tools=[], model='m').run()
        finally:
            agent_mod._default_dispatch_stream = orig_dispatch
            agent_mod._default_build_body = orig_build

        completes = [e for e in events if e.get('type') == 'step_complete']
        traces = [e for e in events if e.get('type') == 'step_trace']
        self.assertTrue(completes)
        self.assertTrue(traces)
        # The engine captured the FULL streamed thinking (not the 2000-char
        # reasoning_trace cap) onto step_complete + step_trace.
        complete_think = [e for e in completes if e.get('thinking')]
        self.assertTrue(complete_think,
                        'step_complete should carry accumulated thinking')
        self.assertIn(big_think, complete_think[0]['thinking'])
        self.assertGreater(len(complete_think[0]['thinking']), 2000)
        trace_think = [e for e in traces if e.get('thinking')]
        self.assertTrue(trace_think,
                        'step_trace should carry accumulated thinking')
        self.assertIn(big_think, trace_think[0]['thinking'])


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


if __name__ == '__main__':
    unittest.main()
