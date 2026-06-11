"""tests/test_orchestration_engine.py — FlowExecutor interpreter tests.

Uses a MOCK agent_runner so the control-flow logic (linear chain, loop
with verifier verdict, parallel fan-out + barrier join, branch, caps) is
fully exercised with zero LLM calls.
"""

import threading
import unittest

from lib.orchestration import layout_definition
from lib.orchestration_engine import FlowExecutor, FlowExecutionError, compile_plan


def _n(nid, **kw):
    d = {'id': nid}
    d.update(kw)
    return d


def _role(nid, role, **params):
    return _n(nid, type='role', role=role, params=params)


def _ctrl(nid, kind, **params):
    return _n(nid, type='control', kind=kind, params=params)


class _MockRunner:
    """Records calls; returns scripted outputs keyed by node id or role.

    ``tools`` maps node-id/role → list[str] of tool names the producer
    'called' that turn, so deliverables tracking can be exercised.
    """
    def __init__(self, outputs=None, verifier_script=None, tools=None):
        self.outputs = outputs or {}
        self.verifier_script = list(verifier_script or [])
        self.tools = tools or {}
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, node, context, iteration):
        with self.lock:
            self.calls.append({'id': node['id'], 'role': node.get('role'),
                               'context': context})
        role = node.get('role')
        # Only surface tool info when this test explicitly configured tools —
        # otherwise the result omits the key entirely so the engine treats
        # the turn as "tools unknown" (reported=False) and the
        # zero-deliverable guard never fires on legacy control-flow tests.
        has_tools = (node['id'] in self.tools) or (role in self.tools)
        tool_names = self.tools.get(node['id']) or self.tools.get(role) or []
        if role in ('critic', 'reviewer') and self.verifier_script:
            res = {'output': self.verifier_script.pop(0), 'status': 'completed', 'error': ''}
        else:
            out = self.outputs.get(node['id']) or self.outputs.get(role) or f'{role}-output'
            res = {'output': out, 'status': 'completed', 'error': ''}
        if has_tools:
            res['tool_names'] = tool_names
        return res


class LinearTest(unittest.TestCase):
    def _defn(self):
        return {'schema': 'tofu.orchestration/v1', 'name': 'Lin', 'nodes': [
            _ctrl('s', 'start'), _role('w', 'worker'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'e'}]}

    def test_linear_runs_one_agent(self):
        r = _MockRunner()
        out = FlowExecutor(self._defn(), agent_runner=r).run()
        self.assertTrue(out['ok'])
        self.assertEqual(out['agents_run'], 1)
        self.assertEqual([c['role'] for c in r.calls], ['worker'])
        self.assertIn('worker-output', out['final'])

    def test_context_threads_through(self):
        r = _MockRunner(outputs={'p': 'PLAN123', 'w': 'done'})
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'L', 'nodes': [
            _ctrl('s', 'start'), _role('p', 'planner'), _role('w', 'worker'),
            _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'p'}, {'from': 'p', 'to': 'w'},
                      {'from': 'w', 'to': 'e'}]}
        FlowExecutor(defn, agent_runner=r).run()
        worker_call = [c for c in r.calls if c['role'] == 'worker'][0]
        self.assertIn('PLAN123', worker_call['context'])


class LoopTest(unittest.TestCase):
    def _endpoint(self):
        return {'schema': 'tofu.orchestration/v1', 'name': 'EP', 'nodes': [
            _ctrl('s', 'start'), _ctrl('l', 'loop', max_iterations=5),
            _role('w', 'worker', isolation='shared-context'),
            _role('c', 'critic'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'l'}, {'from': 'l', 'to': 'w'},
                      {'from': 'w', 'to': 'c'}, {'from': 'c', 'to': 'l'},
                      {'from': 'l', 'to': 'e'}]}

    def test_loop_stops_on_verdict_stop(self):
        # critic says CONTINUE once, then STOP → 2 iterations.
        r = _MockRunner(verifier_script=['needs work: CONTINUE', 'VERDICT: STOP'])
        out = FlowExecutor(self._endpoint(), agent_runner=r).run()
        self.assertTrue(out['ok'])
        worker_calls = [c for c in r.calls if c['role'] == 'worker']
        self.assertEqual(len(worker_calls), 2)

    def test_loop_respects_max_iterations(self):
        # critic always says CONTINUE (distinct text each turn so stuck-
        # detection doesn't fire) → capped at max_iterations=5.
        words = ('alpha beta gamma delta epsilon zeta eta theta iota kappa '
                 'lambda mu nu xi omicron pi rho sigma tau upsilon').split()
        r = _MockRunner(verifier_script=[f'CONTINUE {words[i]} {words[i+1]}' for i in range(15)])
        out = FlowExecutor(self._endpoint(), agent_runner=r,
                           max_iterations=99).run()
        worker_calls = [c for c in r.calls if c['role'] == 'worker']
        self.assertEqual(len(worker_calls), 5)  # node param caps at 5

    def test_global_max_iterations_caps_node_param(self):
        words = 'red orange yellow green blue indigo violet white black gray'.split()
        r = _MockRunner(verifier_script=[f'CONTINUE {words[i]} {words[i+1]}' for i in range(8)])
        out = FlowExecutor(self._endpoint(), agent_runner=r,
                           max_iterations=3).run()
        worker_calls = [c for c in r.calls if c['role'] == 'worker']
        self.assertEqual(len(worker_calls), 3)  # global cap wins


class SharedContextTest(unittest.TestCase):
    """The loop worker must accumulate its own prior attempt + verifier
    feedback across iterations (endpoint behavior), and fresh-context
    nodes must NOT."""

    def _endpoint(self, isolation):
        return {'schema': 'tofu.orchestration/v1', 'name': 'EP', 'nodes': [
            _ctrl('s', 'start'), _ctrl('l', 'loop', max_iterations=4),
            _role('w', 'worker', isolation=isolation),
            _role('c', 'critic'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'l'}, {'from': 'l', 'to': 'w'},
                      {'from': 'w', 'to': 'c'}, {'from': 'c', 'to': 'l'},
                      {'from': 'l', 'to': 'e'}]}

    def test_shared_worker_sees_prior_attempt_and_feedback(self):
        # worker outputs a unique marker each call; critic gives feedback then STOP.
        seq = {'i': 0}
        def runner(node, ctx, it):
            if node.get('role') == 'worker':
                seq['i'] += 1
                return {'output': f'ATTEMPT{seq["i"]}', 'status': 'completed',
                        'error': '', '_ctx': ctx}
            # critic
            verdict = 'CONTINUE: fix FOO' if seq['i'] < 2 else 'VERDICT: STOP'
            return {'output': verdict, 'status': 'completed', 'error': ''}
        seen = []
        def capture(node, ctx, it):
            r = runner(node, ctx, it)
            if node.get('role') == 'worker':
                seen.append(ctx)
            return r
        out = FlowExecutor(self._endpoint('shared-context'), agent_runner=capture).run()
        self.assertTrue(out['ok'])
        # 2nd worker call must contain its prior attempt + the critic feedback
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn('ATTEMPT1', seen[1])
        self.assertIn('previous attempt', seen[1].lower())
        self.assertIn('FOO', seen[1])
        self.assertIn('feedback', seen[1].lower())

    def test_fresh_worker_does_not_see_prior_attempt(self):
        seq = {'i': 0}
        seen = []
        def runner(node, ctx, it):
            if node.get('role') == 'worker':
                seq['i'] += 1
                seen.append(ctx)
                return {'output': f'ATTEMPT{seq["i"]}', 'status': 'completed', 'error': ''}
            verdict = 'CONTINUE: fix it' if seq['i'] < 2 else 'VERDICT: STOP'
            return {'output': verdict, 'status': 'completed', 'error': ''}
        FlowExecutor(self._endpoint('fresh-context'), agent_runner=runner).run()
        # fresh worker's 2nd call must NOT carry its own prior attempt marker
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotIn('previous attempt', seen[1].lower())


class DeliverablesTest(unittest.TestCase):
    """Engine ports endpoint's deliverables tracking + zero-deliverable guard."""

    def _endpoint(self, max_iter=5):
        return {'schema': 'tofu.orchestration/v1', 'name': 'EP', 'nodes': [
            _ctrl('s', 'start'), _ctrl('l', 'loop', max_iterations=max_iter),
            _role('w', 'worker', isolation='shared-context'),
            _role('c', 'critic'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'l'}, {'from': 'l', 'to': 'w'},
                      {'from': 'w', 'to': 'c'}, {'from': 'c', 'to': 'l'},
                      {'from': 'l', 'to': 'e'}]}

    def test_verifier_sees_deliverables_snapshot(self):
        # worker does only exploration (read_files) → critic context must
        # carry the zero-deliverable snapshot + hint.
        critic_ctx = []
        seq = {'w': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': 'looked around', 'status': 'completed',
                        'error': '', 'tool_names': ['read_files', 'grep_search']}
            if role == 'critic':
                critic_ctx.append(ctx)
                return {'output': 'VERDICT: STOP', 'status': 'completed', 'error': ''}
            return {'output': 'x', 'status': 'completed', 'error': ''}
        FlowExecutor(self._endpoint(), agent_runner=runner).run()
        self.assertTrue(critic_ctx)
        self.assertIn('Deliverables Snapshot', critic_ctx[0])
        self.assertIn('0 state-changing', critic_ctx[0])

    def test_zero_deliverable_guard_injects_directive(self):
        # Worker only explores (read_files) every turn; critic keeps saying
        # CONTINUE. After 2 zero-deliverable turns the guard must inject the
        # "start executing" directive into the worker's NEXT context, and it
        # must SKIP the critic that turn (synthetic continue).
        worker_ctx = []
        critic_calls = {'n': 0}
        events = []
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                worker_ctx.append(ctx)
                return {'output': 'analysis', 'status': 'completed',
                        'error': '', 'tool_names': ['read_files']}
            if role == 'critic':
                critic_calls['n'] += 1
                return {'output': 'CONTINUE: keep going', 'status': 'completed', 'error': ''}
            return {'output': 'x', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(max_iter=5), agent_runner=runner,
                           on_event=events.append).run()
        # The directive reached a later worker context (the core behavior).
        self.assertTrue(any('START EXECUTING' in c for c in worker_ctx))
        # The guard event fired after the zero-deliverable streak.
        guard_events = [e for e in events if e['type'] == 'zero_deliverable_guard']
        self.assertTrue(guard_events)
        self.assertGreaterEqual(guard_events[0]['streak'], 2)

    def test_real_work_does_not_trip_guard(self):
        # worker makes a write each turn; critic STOP on iter 2 → no guard,
        # stops normally at 2.
        seq = {'w': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': f'wrote{seq["w"]}', 'status': 'completed',
                        'error': '', 'tool_names': ['write_file']}
            if role == 'critic':
                return {'output': ('CONTINUE: keep going' if seq['w'] < 2 else 'VERDICT: STOP'),
                        'status': 'completed', 'error': ''}
            return {'output': 'x', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(max_iter=6), agent_runner=runner).run()
        self.assertEqual(seq['w'], 2)


class ReplanTest(unittest.TestCase):
    """Engine ports endpoint's CONTINUE_PLANNER + PLAN_DEFECT gate."""

    def _endpoint(self, max_iter=6):
        return {'schema': 'tofu.orchestration/v1', 'name': 'EP', 'nodes': [
            _ctrl('s', 'start'), _role('p', 'planner'),
            _ctrl('l', 'loop', max_iterations=max_iter),
            _role('w', 'worker', isolation='shared-context'),
            _role('c', 'critic'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'p'}, {'from': 'p', 'to': 'l'},
                      {'from': 'l', 'to': 'w'}, {'from': 'w', 'to': 'c'},
                      {'from': 'c', 'to': 'l'}, {'from': 'l', 'to': 'e'}]}

    def test_valid_plan_defect_triggers_replan(self):
        # critic emits CONTINUE_PLANNER + PLAN_DEFECT once, then STOP →
        # planner must run TWICE (initial + 1 replan).
        seq = {'c': 0}
        events = []
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                seq['c'] += 1
                if seq['c'] == 1:
                    return {'output': '[PLAN_DEFECT: missing a build step]\n[VERDICT: CONTINUE_PLANNER]',
                            'status': 'completed', 'error': ''}
                return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
            return {'output': node.get('role') + '-out', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(), agent_runner=runner,
                           on_event=events.append).run()
        self.assertTrue(out['ok'])
        ran = [e['role'] for e in out['transcript']]
        self.assertEqual(ran.count('planner'), 2)   # initial + 1 replan
        self.assertTrue(any(e['type'] == 'replan' for e in events))

    def test_continue_planner_without_defect_downgrades(self):
        # CONTINUE_PLANNER with NO [PLAN_DEFECT] → treated as worker; planner
        # runs only once (initial).
        seq = {'c': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                seq['c'] += 1
                if seq['c'] < 3:
                    return {'output': '[VERDICT: CONTINUE_PLANNER]', 'status': 'completed', 'error': ''}
                return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
            return {'output': node.get('role') + '-out', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(), agent_runner=runner).run()
        ran = [e['role'] for e in out['transcript']]
        self.assertEqual(ran.count('planner'), 1)   # no replan

    def test_worker_rationalization_defect_downgrades(self):
        # PLAN_DEFECT reason that is really a worker complaint → no replan.
        seq = {'c': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                seq['c'] += 1
                if seq['c'] < 3:
                    return {'output': '[PLAN_DEFECT: worker did not finish the edits]\n[VERDICT: CONTINUE_PLANNER]',
                            'status': 'completed', 'error': ''}
                return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
            return {'output': node.get('role') + '-out', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(), agent_runner=runner).run()
        ran = [e['role'] for e in out['transcript']]
        self.assertEqual(ran.count('planner'), 1)   # defect rejected → no replan

    def test_replan_capped(self):
        # critic ALWAYS asks for a valid replan → capped at _MAX_REPLANS (3).
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                return {'output': '[PLAN_DEFECT: structural gap #%d]\n[VERDICT: CONTINUE_PLANNER]' % id(ctx),
                        'status': 'completed', 'error': ''}
            return {'output': node.get('role') + '-out', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(max_iter=20), agent_runner=runner,
                           max_iterations=20).run()
        ran = [e['role'] for e in out['transcript']]
        # initial planner + at most _MAX_REPLANS replans
        self.assertLessEqual(ran.count('planner'), 4)
        self.assertGreaterEqual(ran.count('planner'), 2)

    def test_replan_kill_switch(self):
        import os
        os.environ['TOFU_ENDPOINT_REPLAN'] = '0'
        try:
            seq = {'c': 0}
            def runner(node, ctx, it):
                role = node.get('role')
                if role == 'critic':
                    seq['c'] += 1
                    if seq['c'] < 3:
                        return {'output': '[PLAN_DEFECT: real structural gap]\n[VERDICT: CONTINUE_PLANNER]',
                                'status': 'completed', 'error': ''}
                    return {'output': '[VERDICT: STOP]', 'status': 'completed', 'error': ''}
                return {'output': node.get('role') + '-out', 'status': 'completed', 'error': ''}
            out = FlowExecutor(self._endpoint(), agent_runner=runner).run()
            ran = [e['role'] for e in out['transcript']]
            self.assertEqual(ran.count('planner'), 1)   # kill-switch → no replan
        finally:
            os.environ.pop('TOFU_ENDPOINT_REPLAN', None)


class StuckTest(unittest.TestCase):
    """Engine ports endpoint's _detect_stuck (repeating critic → break)."""

    def _endpoint(self, max_iter=10):
        return {'schema': 'tofu.orchestration/v1', 'name': 'EP', 'nodes': [
            _ctrl('s', 'start'), _ctrl('l', 'loop', max_iterations=max_iter),
            _role('w', 'worker', isolation='shared-context'),
            _role('c', 'critic'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'l'}, {'from': 'l', 'to': 'w'},
                      {'from': 'w', 'to': 'c'}, {'from': 'c', 'to': 'l'},
                      {'from': 'l', 'to': 'e'}]}

    def test_repeating_feedback_breaks_loop(self):
        # critic emits the SAME CONTINUE feedback every turn → stuck → break
        # well before the max_iterations=10 cap.
        events = []
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                return {'output': 'CONTINUE: please fix the same foo bar baz issue again',
                        'status': 'completed', 'error': ''}
            return {'output': 'work', 'status': 'completed', 'error': ''}
        out = FlowExecutor(self._endpoint(), agent_runner=runner,
                           on_event=events.append).run()
        self.assertTrue(any(e['type'] == 'stuck_detected' for e in events))
        worker_runs = sum(1 for e in events if e['type'] == 'step_start'
                          and e.get('role') == 'worker')
        self.assertLess(worker_runs, 10)   # broke early

    def test_varied_feedback_does_not_trip_stuck(self):
        # distinct feedback each turn → no stuck; stops on STOP at iter 3.
        seq = {'c': 0}
        msgs = ['CONTINUE: add the missing import in module alpha',
                'CONTINUE: now wire the new handler into the router table',
                'VERDICT: STOP']
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'critic':
                m = msgs[min(seq['c'], len(msgs) - 1)]; seq['c'] += 1
                return {'output': m, 'status': 'completed', 'error': ''}
            return {'output': 'work', 'status': 'completed', 'error': ''}
        events = []
        out = FlowExecutor(self._endpoint(), agent_runner=runner,
                           on_event=events.append).run()
        self.assertFalse(any(e['type'] == 'stuck_detected' for e in events))


class ParallelTest(unittest.TestCase):
    def _fanout(self):
        return {'schema': 'tofu.orchestration/v1', 'name': 'FO', 'nodes': [
            _ctrl('s', 'start'), _ctrl('p', 'parallel', max_concurrent=4),
            _role('r1', 'researcher'), _role('r2', 'researcher'),
            _role('r3', 'researcher'), _ctrl('b', 'barrier'),
            _role('sy', 'synthesizer'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'p'},
                      {'from': 'p', 'to': 'r1'}, {'from': 'p', 'to': 'r2'},
                      {'from': 'p', 'to': 'r3'},
                      {'from': 'r1', 'to': 'b'}, {'from': 'r2', 'to': 'b'},
                      {'from': 'r3', 'to': 'b'}, {'from': 'b', 'to': 'sy'},
                      {'from': 'sy', 'to': 'e'}]}

    def test_fanout_runs_all_branches_then_synth(self):
        r = _MockRunner(outputs={'r1': 'A', 'r2': 'B', 'r3': 'C', 'sy': 'MERGED'})
        out = FlowExecutor(self._fanout(), agent_runner=r).run()
        self.assertTrue(out['ok'])
        roles = sorted(c['role'] for c in r.calls)
        self.assertEqual(roles, ['researcher', 'researcher', 'researcher', 'synthesizer'])
        # synthesizer saw all three branch outputs in its context
        sy_call = [c for c in r.calls if c['role'] == 'synthesizer'][0]
        for tok in ('A', 'B', 'C'):
            self.assertIn(tok, sy_call['context'])
        self.assertEqual(out['agents_run'], 4)


class BranchAndCapsTest(unittest.TestCase):
    def test_branch_takes_first_edge(self):
        r = _MockRunner()
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'BR', 'nodes': [
            _ctrl('s', 'start'), _ctrl('br', 'branch'),
            _role('a', 'coder'), _role('b', 'writer'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'br'},
                      {'from': 'br', 'to': 'a'}, {'from': 'br', 'to': 'b'},
                      {'from': 'a', 'to': 'e'}, {'from': 'b', 'to': 'e'}]}
        out = FlowExecutor(defn, agent_runner=r).run()
        self.assertTrue(out['ok'])
        self.assertEqual([c['role'] for c in r.calls], ['coder'])

    def test_branch_classifier_routes_by_label(self):
        # classifier picks the second option by name; not the first edge.
        def runner(node, ctx, it):
            if node.get('role') == 'router':
                return {'output': 'I choose Writerpath', 'status': 'completed', 'error': ''}
            return {'output': node.get('role') + '-out', 'status': 'completed', 'error': ''}
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'BRC', 'nodes': [
            _ctrl('s', 'start'),
            {'id': 'br', 'type': 'control', 'kind': 'branch', 'params': {'classifier': 'router'}},
            {'id': 'a', 'type': 'role', 'role': 'coder', 'name': 'Coderpath', 'params': {}},
            {'id': 'b', 'type': 'role', 'role': 'writer', 'name': 'Writerpath', 'params': {}},
            _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'br'},
                      {'from': 'br', 'to': 'a'}, {'from': 'br', 'to': 'b'},
                      {'from': 'a', 'to': 'e'}, {'from': 'b', 'to': 'e'}]}
        out = FlowExecutor(defn, agent_runner=runner).run()
        self.assertTrue(out['ok'])
        # writer ran (chosen), coder did not
        ran = [e['role'] for e in out['transcript']]
        self.assertIn('writer', ran)
        self.assertNotIn('coder', ran)

    def test_invalid_definition_rejected(self):
        bad = {'name': '', 'nodes': [], 'edges': []}
        with self.assertRaises(FlowExecutionError):
            FlowExecutor(bad, agent_runner=_MockRunner())

    def test_agent_budget_enforced(self):
        # Long linear chain, budget of 2.
        nodes = [_ctrl('s', 'start')]
        edges = [{'from': 's', 'to': 'a0'}]
        for i in range(5):
            nodes.append(_role(f'a{i}', 'coder'))
            nxt = f'a{i+1}' if i < 4 else 'e'
            edges.append({'from': f'a{i}', 'to': nxt})
        nodes.append(_ctrl('e', 'stop'))
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'Long',
                'nodes': nodes, 'edges': edges}
        out = FlowExecutor(defn, agent_runner=_MockRunner(), max_agents=2).run()
        self.assertEqual(out['status'], 'failed')
        self.assertIn('budget', out['error'])

    def test_abort_stops_execution(self):
        flag = {'v': False}
        r = _MockRunner()
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'Ab', 'nodes': [
            _ctrl('s', 'start'), _role('a', 'coder'), _role('b', 'coder'),
            _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'a'}, {'from': 'a', 'to': 'b'},
                      {'from': 'b', 'to': 'e'}]}
        # abort after first agent
        orig = r.__call__
        def runner(node, ctx, it):
            flag['v'] = True
            return orig(node, ctx, it)
        out = FlowExecutor(defn, agent_runner=runner,
                           abort_check=lambda: flag['v']).run()
        self.assertEqual(out['status'], 'aborted')


class EndpointAsFlowTest(unittest.TestCase):
    """The canonical endpoint definition must run faithfully on FlowExecutor:
    planner once, worker/critic loop with shared-context carry-forward, STOP
    on a clean verdict."""

    def test_canonical_endpoint_runs(self):
        from lib.orchestration import build_endpoint_definition
        defn = build_endpoint_definition(max_iterations=5)
        seq = {'w': 0}
        seen_worker_ctx = []
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                seen_worker_ctx.append(ctx)
                return {'output': f'work{seq["w"]}', 'status': 'completed', 'error': ''}
            if role == 'critic':
                return {'output': ('CONTINUE: ❌ not done' if seq['w'] < 2 else 'VERDICT: STOP'),
                        'status': 'completed', 'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}
        out = FlowExecutor(defn, agent_runner=runner).run()
        self.assertTrue(out['ok'], out.get('error'))
        ran = [e['role'] for e in out['transcript']]
        self.assertEqual(ran.count('planner'), 1)
        self.assertEqual(ran.count('worker'), 2)
        # shared-context worker carried its prior attempt into iteration 2
        self.assertIn('work1', seen_worker_ctx[1])

    def test_stop_with_unresolved_marker_is_overridden(self):
        # critic emits STOP but leaves a ❌ → must NOT stop on iteration 1.
        from lib.orchestration import build_endpoint_definition
        defn = build_endpoint_definition(max_iterations=4)
        seq = {'w': 0}
        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': f'w{seq["w"]}', 'status': 'completed', 'error': ''}
            if role == 'critic':
                # iteration 1: STOP but with a ❌ (override → continue);
                # iteration 2: clean STOP.
                return {'output': ('VERDICT: STOP ❌ one item left' if seq['w'] < 2
                                   else 'VERDICT: STOP'),
                        'status': 'completed', 'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}
        out = FlowExecutor(defn, agent_runner=runner).run()
        self.assertEqual(seq['w'], 2)  # did NOT stop on the dirty STOP


class HumanNodeTest(unittest.TestCase):
    """The human-in-the-loop gate (approve / input / notify), reusing the
    chat approval + ask-human primitives."""

    def _defn(self, mode):
        return {'schema': 'tofu.orchestration/v1', 'name': 'HG', 'nodes': [
            _ctrl('s', 'start'), _role('w', 'worker'),
            _ctrl('h', 'human', mode=mode), _role('w2', 'general'),
            _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'h'},
                      {'from': 'h', 'to': 'w2'}, {'from': 'w2', 'to': 'e'}]}

    def test_notify_is_non_blocking(self):
        events = []
        out = FlowExecutor(self._defn('notify'), agent_runner=_MockRunner(),
                           on_event=events.append).run()
        self.assertTrue(out['ok'])
        self.assertTrue(any(e['type'] == 'human_notify' for e in events))
        # downstream agent ran — gate did not block
        self.assertTrue(any(e['type'] == 'step_start' and e.get('role') == 'general'
                            for e in events))

    def test_approve_continues_when_approved(self):
        import time
        from lib.tasks_pkg.approval import resolve_write_approval
        events = []
        def _approve_when_asked(ev):
            events.append(ev)
            if ev.get('type') == 'human_request':
                # resolve in a separate thread so the blocking wait unblocks
                threading.Thread(target=lambda: (time.sleep(0.05),
                                 resolve_write_approval(ev['request_id'], True))).start()
        out = FlowExecutor(self._defn('approve'), agent_runner=_MockRunner(),
                           on_event=_approve_when_asked).run()
        self.assertTrue(out['ok'])
        self.assertTrue(any(e['type'] == 'human_resolved' and e.get('approved')
                            for e in events))
        self.assertTrue(any(e['type'] == 'step_start' and e.get('role') == 'general'
                            for e in events))

    def test_approve_rejected_halts_flow(self):
        from lib.tasks_pkg.approval import resolve_write_approval
        import time
        events = []
        def _reject_when_asked(ev):
            events.append(ev)
            if ev.get('type') == 'human_request':
                threading.Thread(target=lambda: (time.sleep(0.05),
                                 resolve_write_approval(ev['request_id'], False))).start()
        out = FlowExecutor(self._defn('approve'), agent_runner=_MockRunner(),
                           on_event=_reject_when_asked).run()
        # rejection unwinds the walk via abort path → not completed
        self.assertEqual(out['status'], 'aborted')
        # downstream agent must NOT have run
        self.assertFalse(any(e['type'] == 'step_start' and e.get('role') == 'general'
                             for e in events))

    def test_input_appends_answer_to_context(self):
        from lib.tasks_pkg.human_guidance import resolve_human_guidance
        import time
        events = []
        downstream_ctx = []
        def runner(node, ctx, it):
            if node.get('role') == 'general':
                downstream_ctx.append(ctx)
            return {'output': (node.get('role') or '') + '-out', 'status': 'completed', 'error': ''}
        def _answer_when_asked(ev):
            events.append(ev)
            if ev.get('type') == 'human_request':
                threading.Thread(target=lambda: (time.sleep(0.05),
                                 resolve_human_guidance(ev['request_id'], 'USE PLAN B'))).start()
        out = FlowExecutor(self._defn('input'), agent_runner=runner,
                           on_event=_answer_when_asked).run()
        self.assertTrue(out['ok'])
        self.assertTrue(downstream_ctx)
        self.assertIn('USE PLAN B', downstream_ctx[0])

    def test_plan_lists_human_step(self):
        plan = compile_plan(self._defn('approve'))
        self.assertTrue(plan['ok'])
        actions = [s['action'] for s in plan['steps']]
        self.assertIn('human-approve', actions)


class CompilePlanTest(unittest.TestCase):
    def test_plan_lists_steps(self):
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'EP', 'nodes': [
            _ctrl('s', 'start'), _ctrl('l', 'loop'),
            _role('w', 'worker'), _role('c', 'critic'), _ctrl('e', 'stop')],
            'edges': [{'from': 's', 'to': 'l'}, {'from': 'l', 'to': 'w'},
                      {'from': 'w', 'to': 'c'}, {'from': 'c', 'to': 'l'},
                      {'from': 'l', 'to': 'e'}]}
        plan = compile_plan(defn)
        self.assertTrue(plan['ok'])
        actions = [s['action'] for s in plan['steps']]
        self.assertIn('run-agent', actions)
        self.assertIn('loop-back', actions)

    def test_plan_rejects_invalid(self):
        plan = compile_plan({'name': '', 'nodes': [], 'edges': []})
        self.assertFalse(plan['ok'])


if __name__ == '__main__':
    unittest.main()
