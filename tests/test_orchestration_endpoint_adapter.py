"""tests/test_orchestration_endpoint_adapter.py — FlowExecutor→endpoint UI.

Drives a real FlowExecutor (mock agent runner) through the adapter and
asserts the emitted messages match endpoint mode's display schema
(_isEndpointPlanner / _epIteration / _isEndpointReview / _epNextPhase).
"""

import unittest

from lib.orchestration import build_endpoint_definition
from lib.orchestration_engine import FlowExecutor
from lib.orchestration_endpoint_adapter import EndpointEventAdapter


def test_endpoint_wire_policy_is_physically_separate_from_stateful_adapter():
    from pathlib import Path

    from lib.orchestration_endpoint_projection import (
        endpoint_emits_for_role,
        project_endpoint_next_phase,
        project_endpoint_phase_event,
        project_endpoint_turn_metadata,
    )

    assert EndpointEventAdapter._derive_emits is endpoint_emits_for_role
    assert project_endpoint_turn_metadata(
        'virtual_user', '', projection='autopilot',
        vu_msg_id='msg-1', vu_run_id='run-1',
    ) == {
        'flowProjection': 'autopilot',
        'turnRole': 'virtual_user',
        'emits': 'user',
        'vuMsgId': 'msg-1',
        'autopilotRunId': 'run-1',
    }
    assert project_endpoint_next_phase(
        'continue', pending_replan=True) == 'planner'
    assert project_endpoint_next_phase('[VERDICT: STOP]') == 'stop'
    assert project_endpoint_phase_event({
        'phase': 'retrying',
        'detail': 'rate limited',
        'attempt': 2,
        'status_code': 429,
        'detailKey': 'stream.phase.retryRateLimited',
        'detailArgs': {'seconds': 3},
    }) == {
        'type': 'phase',
        'phase': 'retrying',
        'detail': 'rate limited',
        'attempt': 2,
        'statusCode': 429,
        'detailKey': 'stream.phase.retryRateLimited',
        'detailArgs': {'seconds': 3},
    }

    adapter_source = Path(
        'lib/orchestration_endpoint_adapter.py').read_text()
    assert 'VERIFIER_ROLES' not in adapter_source
    assert 'build_phase(' not in adapter_source
    assert 'def _derive_emits' not in adapter_source


def _run(defn, runner):
    adapter = EndpointEventAdapter()
    FlowExecutor(defn, agent_runner=runner, on_event=adapter.on_event).run()
    return adapter.messages


class AdapterTest(unittest.TestCase):
    def test_endpoint_schema_shape(self):
        defn = build_endpoint_definition(max_iterations=5)
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

        planners = [m for m in msgs if m.get('_isEndpointPlanner')]
        workers = [m for m in msgs if m.get('_epIteration') and not m.get('_isEndpointReview')]
        critics = [m for m in msgs if m.get('_isEndpointReview')]

        self.assertEqual(len(planners), 1)
        self.assertEqual(planners[0]['role'], 'assistant')
        self.assertEqual(planners[0]['_epPlannerIteration'], 1)

        self.assertEqual(len(workers), 2)
        self.assertEqual([w['_epIteration'] for w in workers], [1, 2])
        self.assertEqual(workers[0]['role'], 'assistant')
        self.assertEqual(workers[0]['_epStateChangingCount'], 1)

        self.assertTrue(critics)
        self.assertEqual(critics[0]['role'], 'user')
        # final critic approved (STOP)
        self.assertTrue(critics[-1]['_epApproved'])
        self.assertEqual(critics[-1]['_epNextPhase'], 'stop')

    def test_replan_bumps_planner_iteration(self):
        defn = build_endpoint_definition(max_iterations=6)
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
        planners = [m for m in msgs if m.get('_isEndpointPlanner')]
        # initial planner + 1 replan → two planner messages, iterations 1 & 2
        self.assertEqual([p['_epPlannerIteration'] for p in planners], [1, 2])
        # the critic that triggered the replan points to 'planner'
        replan_critics = [m for m in msgs if m.get('_isEndpointReview')
                          and m.get('_epNextPhase') == 'planner']
        self.assertTrue(replan_critics)

    def test_zero_deliverable_emits_synthetic_critic(self):
        defn = build_endpoint_definition(max_iterations=5)
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
        self.assertEqual(synthetic[0]['_epNextPhase'], 'worker')

    def test_live_emit_callback(self):
        defn = build_endpoint_definition(max_iterations=3)
        emitted = []
        adapter = EndpointEventAdapter(emit=emitted.append)
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
            return {'output': 'x', 'status': 'completed', 'error': '', 'tool_names': ['write_file']}
        FlowExecutor(defn, agent_runner=runner, on_event=adapter.on_event).run()
        # emit callback saw the same messages as the accumulator
        self.assertEqual(len(emitted), len(adapter.messages))
        self.assertTrue(any(m.get('_isEndpointPlanner') for m in emitted))


class AutopilotProjectionContractTest(unittest.TestCase):
    """A VU graph uses endpoint transport without inheriting critic meaning."""

    def test_projection_is_derived_from_semantic_roles(self):
        from lib.orchestration import (
            build_autopilot_definition,
            chat_projection_for_flow,
        )

        self.assertEqual(
            chat_projection_for_flow(build_autopilot_definition()),
            'autopilot',
        )
        self.assertEqual(
            chat_projection_for_flow(build_endpoint_definition()),
            'endpoint',
        )
        # A richer VU graph may also contain a planner; VU identity wins.
        mixed = build_endpoint_definition()
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

    def test_virtual_user_live_identity_matches_persisted_turn(self):
        streamed = []
        adapter = EndpointEventAdapter(
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
        self.assertEqual(start['type'], 'endpoint_iteration')
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
        self.assertNotIn('_isEndpointReview', vu_msg)
        self.assertEqual(vu_msg['_msgId'], start['vuMsgId'])
        self.assertEqual(vu_msg['_autopilotRunId'], 'run-1')
        self.assertEqual(vu_msg['content'], 'Keep going')

    def test_task_done_discards_placeholder_and_never_persists_token(self):
        streamed = []
        adapter = EndpointEventAdapter(
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
        self.assertEqual(terminal['type'], 'endpoint_critic_msg')
        self.assertEqual(terminal['vuMsgId'], vu_msg_id)
        self.assertEqual(terminal['next_phase'], 'stop')
        self.assertTrue(terminal['discard'])
        self.assertEqual(terminal['content'], '')

    def test_step_phase_for_producer_becomes_wire_phase(self):
        """Engine ``step_phase`` for an assistant producer → wire ``phase``
        event on the live stream (the "waiting for model…" signal), and is
        NOT a delta (so it can't pollute the assistant content)."""
        streamed = []
        adapter = EndpointEventAdapter(on_stream=streamed.append)
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

    def test_step_phase_for_verifier_is_skipped(self):
        """A verifier (user-side) producer's phase would land on the wrong
        bubble — the adapter must drop it."""
        streamed = []
        adapter = EndpointEventAdapter(on_stream=streamed.append)
        adapter.on_event({'type': 'step_phase', 'node_id': 'vu',
                          'role': 'virtual_user', 'emits': 'user',
                          'phase': 'retrying', 'detail': 'Retrying…'})
        self.assertFalse(streamed)

    def test_subagent_waiting_phase_end_to_end(self):
        """FULL path: a real SubAgent whose dispatch fires on_retry (the
        rate-limited cooldown wait) → engine _stream_sink → step_phase →
        adapter → wire ``phase`` event. Proves the live "waiting for model…"
        signal reaches the stream during a stall, with NO real LLM."""
        from lib.orchestration import build_autopilot_definition
        from lib.swarm.protocol import SubAgentStatus

        streamed = []
        adapter = EndpointEventAdapter(on_stream=streamed.append)

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
        from lib.orchestration import render_role_brief, resolve_emits

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
            out = ('[VU: TASK_DONE]' if role == 'virtual_user'
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
      • the finalizing SSE events (``endpoint_planner_done`` /
        ``endpoint_critic_msg``) carry ``thinking``.
    Revert-proof: dropping ``thinking`` from any of those sites fails here.
    """

    def _drive(self):
        defn = build_endpoint_definition(max_iterations=3)
        streamed = []
        adapter = EndpointEventAdapter(on_stream=streamed.append)

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
        planner = [m for m in msgs if m.get('_isEndpointPlanner')][0]
        worker = [m for m in msgs
                  if m.get('_epIteration') and not m.get('_isEndpointReview')][0]
        critic = [m for m in msgs if m.get('_isEndpointReview')][-1]
        self.assertEqual(planner.get('thinking'), 'PLANNER-REASONING')
        self.assertEqual(worker.get('thinking'), 'WORKER-REASONING')
        self.assertEqual(critic.get('thinking'), 'CRITIC-REASONING')

    def test_finalize_sse_events_carry_thinking(self):
        _, streamed = self._drive()
        planner_done = [e for e in streamed
                        if e.get('type') == 'endpoint_planner_done']
        critic_msg = [e for e in streamed
                      if e.get('type') == 'endpoint_critic_msg']
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
        ``_default_build_body`` so every node node runs the real
        ``FlowExecutor._default_runner`` path (the production path) with no
        network. The mocked dispatch streams a >2000-char thinking blob; the
        critic's 'final out' classifies as STOP (loose fallback) → one
        iteration."""
        import lib.swarm.agent as agent_mod
        from lib.orchestration import build_endpoint_definition

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
            defn = build_endpoint_definition(max_iterations=1)
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


if __name__ == '__main__':
    unittest.main()
