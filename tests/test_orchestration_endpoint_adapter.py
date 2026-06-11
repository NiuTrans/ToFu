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


if __name__ == '__main__':
    unittest.main()
