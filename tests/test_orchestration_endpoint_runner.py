"""tests/test_orchestration_endpoint_runner.py — flagged cutover runner.

Covers the flag gate (default OFF), user-request extraction, and an
end-to-end run of run_endpoint_via_flow with the SubAgent runner monkey-
patched (so NO real LLM is called). Verifies endpoint-schema SSE events
reach the task and the task is finalized.
"""

import os
import threading
import unittest


class FlagGateTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop('TOFU_ENDPOINT_VIA_FLOW', None)

    def tearDown(self):
        os.environ.pop('TOFU_ENDPOINT_VIA_FLOW', None)

    def test_default_off(self):
        from lib.orchestration_endpoint_runner import endpoint_via_flow_enabled
        self.assertFalse(endpoint_via_flow_enabled())

    def test_explicit_values(self):
        from lib.orchestration_endpoint_runner import endpoint_via_flow_enabled
        for v in ('1', 'true', 'YES', 'on'):
            os.environ['TOFU_ENDPOINT_VIA_FLOW'] = v
            self.assertTrue(endpoint_via_flow_enabled(), v)
        for v in ('0', 'false', '', 'nonsense'):
            os.environ['TOFU_ENDPOINT_VIA_FLOW'] = v
            self.assertFalse(endpoint_via_flow_enabled(), v)


class RequestExtractTest(unittest.TestCase):
    def test_extracts_latest_user_text(self):
        from lib.orchestration_endpoint_runner import _extract_user_request
        task = {'messages': [
            {'role': 'user', 'content': 'first'},
            {'role': 'assistant', 'content': 'reply'},
            {'role': 'user', 'content': 'LATEST request'},
        ]}
        self.assertEqual(_extract_user_request(task), 'LATEST request')

    def test_extracts_multimodal_text(self):
        from lib.orchestration_endpoint_runner import _extract_user_request
        task = {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'part one'},
            {'type': 'image', 'source': {}},
            {'type': 'text', 'text': 'part two'},
        ]}]}
        self.assertIn('part one', _extract_user_request(task))
        self.assertIn('part two', _extract_user_request(task))


class EndToEndTest(unittest.TestCase):
    """run_endpoint_via_flow with the SubAgent runner stubbed."""

    def _make_task(self):
        return {
            'id': 'flowtask123456',
            'convId': 'conv1',
            'messages': [{'role': 'user', 'content': 'do the thing'}],
            'config': {'endpointMaxIterations': 3},
            'events': [],
            'events_lock': threading.Lock(),
            'content_lock': threading.Lock(),
            'toolRounds': [],
            'phase': 'tool',
        }

    def test_run_emits_endpoint_events_and_finalizes(self):
        import lib.orchestration_engine as eng
        from lib.orchestration_endpoint_runner import run_endpoint_via_flow

        # Stub the engine's default runner so no SubAgent / LLM is built.
        seq = {'w': 0}
        def fake_runner(self, node, context, iteration):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': f'work{seq["w"]}', 'status': 'completed',
                        'error': '', 'tool_names': ['write_file']}
            if role == 'critic':
                return {'output': ('CONTINUE: more' if seq['w'] < 2 else '[VERDICT: STOP]'),
                        'status': 'completed', 'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}

        # Avoid real tool assembly + DB persistence.
        import lib.orchestration_endpoint_runner as runner_mod
        orig_tools = runner_mod._build_tools_for_task
        runner_mod._build_tools_for_task = lambda task: ([], '', '')

        captured_events = []
        import lib.tasks_pkg.manager as mgr
        orig_append = mgr.append_event
        orig_persist = mgr.persist_task_result
        mgr.append_event = lambda task, event: captured_events.append(event)
        mgr.persist_task_result = lambda task: None

        # Stub the live endpoint DB-sync functions (no real DB in unit test);
        # capture the snapshots so we can assert per-turn persistence happens.
        import lib.tasks_pkg.endpoint as ep_mod
        orig_store = ep_mod._store_endpoint_turns_on_task
        orig_sync = ep_mod._sync_endpoint_turns_to_conversation
        orig_perturn = ep_mod._trigger_per_turn_auto_translate
        orig_safetynet = ep_mod._trigger_endpoint_auto_translate
        sync_snapshots = []
        perturn_calls = []
        safetynet_calls = []
        ep_mod._store_endpoint_turns_on_task = lambda task, turns: task.__setitem__('_endpoint_turns', list(turns))
        ep_mod._sync_endpoint_turns_to_conversation = (
            lambda task, turns: sync_snapshots.append(len(turns)) or len(turns) - 1)
        ep_mod._trigger_per_turn_auto_translate = (
            lambda task, turn_msg, msg_idx: perturn_calls.append((turn_msg.get('role'), msg_idx)))
        ep_mod._trigger_endpoint_auto_translate = (
            lambda task, turns: safetynet_calls.append(len(turns)))

        orig_default = eng.FlowExecutor._default_runner
        eng.FlowExecutor._default_runner = fake_runner
        try:
            task = self._make_task()
            run_endpoint_via_flow(task)
        finally:
            eng.FlowExecutor._default_runner = orig_default
            runner_mod._build_tools_for_task = orig_tools
            mgr.append_event = orig_append
            mgr.persist_task_result = orig_persist
            ep_mod._store_endpoint_turns_on_task = orig_store
            ep_mod._sync_endpoint_turns_to_conversation = orig_sync
            ep_mod._trigger_per_turn_auto_translate = orig_perturn
            ep_mod._trigger_endpoint_auto_translate = orig_safetynet

        # DB sync ran incrementally — at least once per emitted turn, and the
        # snapshot grew monotonically (planner=1 → worker=2 → critic=3 …).
        self.assertTrue(sync_snapshots)
        self.assertEqual(sync_snapshots, sorted(sync_snapshots))
        self.assertGreaterEqual(max(sync_snapshots), 3)

        # Per-turn auto-translate fired once per EMIT-callback sync (the final
        # block does one extra sync but fires the safety net, not per-turn) —
        # so per-turn count == emit syncs == all snapshots except the final.
        self.assertEqual(len(perturn_calls), len(sync_snapshots) - 1)
        # Each per-turn call used the DB msg_idx the sync returned (len-1).
        self.assertEqual([idx for _role, idx in perturn_calls],
                         [n - 1 for n in sync_snapshots[:-1]])
        # End-of-run safety net ran exactly once over the full turn list.
        self.assertEqual(len(safetynet_calls), 1)
        self.assertGreaterEqual(safetynet_calls[0], 3)

        types = [e.get('type') for e in captured_events]
        self.assertIn('endpoint_planner_done', types)
        self.assertIn('endpoint_iteration', types)   # worker turns
        self.assertIn('endpoint_critic_msg', types)
        self.assertIn('endpoint_complete', types)
        self.assertIn('done', types)
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['_endpoint_phase'], 'done')
        self.assertTrue(task.get('_endpoint_via_flow'))
        # converged content set from the flow's final output
        self.assertTrue(task['content'])


if __name__ == '__main__':
    unittest.main()
