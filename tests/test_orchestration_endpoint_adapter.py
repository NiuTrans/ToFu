"""tests/test_orchestration_endpoint_adapter.py — FlowExecutor→endpoint UI.

Drives a real FlowExecutor (mock agent runner) through the adapter and
asserts the emitted messages match endpoint mode's display schema
(_isEndpointPlanner / _epIteration / _isEndpointReview / _epNextPhase).
"""

import unittest

from lib.orchestration import build_endpoint_definition
from lib.orchestration_engine import FlowExecutor
from lib.orchestration_endpoint_adapter import EndpointEventAdapter


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
                               context=ctx, max_rounds=1)
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


if __name__ == '__main__':
    unittest.main()
