"""tests/test_chat_flow_dispatch.py — chat → FlowExecutor dispatch.

Covers the final convergence wiring added in routes/chat.py:
  * resolve_chat_flow_entry — precedence + flag gating (endpoint / autopilot
    / user-selected flow).
  * resolve_chat_flow_definition — inline / builtin / stored resolution.
  * autopilot_via_flow_enabled — symmetric flag (default OFF).
  * run_autopilot_via_flow — end-to-end with the SubAgent runner stubbed
    (no real LLM), asserting worker→assistant / virtual_user→user turns and
    [VU: TASK_DONE] termination.
"""

import os
import threading
import unittest


class FlagGateTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def tearDown(self):
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def test_autopilot_flag_default_off(self):
        from lib.orchestration_endpoint_runner import autopilot_via_flow_enabled
        self.assertFalse(autopilot_via_flow_enabled())

    def test_autopilot_flag_explicit(self):
        from lib.orchestration_endpoint_runner import autopilot_via_flow_enabled
        for v in ('1', 'true', 'YES', 'on'):
            os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = v
            self.assertTrue(autopilot_via_flow_enabled(), v)
        for v in ('0', 'false', '', 'nope'):
            os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = v
            self.assertFalse(autopilot_via_flow_enabled(), v)


class ResolveEntryTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop('TOFU_ENDPOINT_VIA_FLOW', None)
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def tearDown(self):
        os.environ.pop('TOFU_ENDPOINT_VIA_FLOW', None)
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def _resolve(self, cfg):
        from lib.orchestration_endpoint_runner import resolve_chat_flow_entry
        return resolve_chat_flow_entry(cfg)

    def test_no_selection_no_flags_returns_none(self):
        self.assertIsNone(self._resolve({}))
        self.assertIsNone(self._resolve({'endpointMode': True}))   # flag off
        self.assertIsNone(self._resolve({'autopilot': True}))      # flag off

    def test_endpoint_flag_routes_to_endpoint_runner(self):
        from lib.orchestration_endpoint_runner import run_endpoint_via_flow
        os.environ['TOFU_ENDPOINT_VIA_FLOW'] = '1'
        self.assertIs(self._resolve({'endpointMode': True}), run_endpoint_via_flow)

    def test_autopilot_flag_routes_to_autopilot_runner(self):
        from lib.orchestration_endpoint_runner import run_autopilot_via_flow
        os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = '1'
        self.assertIs(self._resolve({'autopilot': True}), run_autopilot_via_flow)

    def test_selected_flow_always_wins_no_flag_needed(self):
        from lib.orchestration_endpoint_runner import run_flow_via_chat
        # EVERY dropdown flow selection routes to the engine flow path
        # unconditionally (the selection IS the opt-in) — including BOTH
        # builtins. See test_builtin_autopilot_routes_to_engine for the
        # symmetry that the "编排流程 → 自动驾驶" dropdown deliberately runs the
        # FlowExecutor autopilot (so engine bugs surface in the frontend).
        self.assertIs(self._resolve({'flowId': 'orch_x'}), run_flow_via_chat)
        self.assertIs(self._resolve({'flowDefinition': {'nodes': [1]}}),
                      run_flow_via_chat)
        self.assertIs(self._resolve({'flowBuiltin': 'endpoint'}), run_flow_via_chat)

    def test_builtin_autopilot_routes_to_engine(self):
        # The "编排流程 → 自动驾驶" dropdown (flowBuiltin='autopilot') routes to
        # the FlowExecutor engine path (run_flow_via_chat), SYMMETRIC with
        # builtin:endpoint and flag-INDEPENDENT — the selection is the opt-in.
        # There is NO Option-C rewrite: cfg is NOT mutated to autopilot=True,
        # and flowBuiltin is preserved so the engine runs the autopilot graph.
        from lib.orchestration_endpoint_runner import run_flow_via_chat
        cfg = {'flowBuiltin': 'autopilot'}
        self.assertIs(self._resolve(cfg), run_flow_via_chat)
        self.assertNotIn('autopilot', cfg)         # NO live-path rewrite
        self.assertEqual(cfg.get('flowBuiltin'), 'autopilot')  # selection preserved

    def test_builtin_autopilot_symmetric_with_endpoint(self):
        # Both builtins resolve to the SAME engine entry point — the dropdown
        # treats autopilot and endpoint identically (both run on the engine).
        self.assertIs(self._resolve({'flowBuiltin': 'autopilot'}),
                      self._resolve({'flowBuiltin': 'endpoint'}))

    def test_builtin_autopilot_engine_route_independent_of_flag(self):
        # The TOFU_AUTOPILOT_VIA_FLOW flag governs the "模式" TOGGLE path
        # (config['autopilot']), NOT a dropdown flow selection. A dropdown
        # builtin:autopilot goes to the engine whether the flag is on or off.
        from lib.orchestration_endpoint_runner import run_flow_via_chat
        for flag in ('0', '1'):
            os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = flag
            cfg = {'flowBuiltin': 'autopilot'}
            self.assertIs(self._resolve(cfg), run_flow_via_chat, flag)
            self.assertNotIn('autopilot', cfg)

    def test_selected_flow_takes_precedence_over_endpoint(self):
        from lib.orchestration_endpoint_runner import run_flow_via_chat
        os.environ['TOFU_ENDPOINT_VIA_FLOW'] = '1'
        # both a flow selection AND endpointMode → flow wins
        self.assertIs(self._resolve({'flowBuiltin': 'endpoint',
                                     'endpointMode': True}),
                      run_flow_via_chat)


class ResolveDefinitionTest(unittest.TestCase):
    def test_inline_definition(self):
        from lib.orchestration_endpoint_runner import resolve_chat_flow_definition
        d = {'schema': 'tofu.orchestration/v1', 'name': 'X',
             'nodes': [{'id': 's', 'type': 'control', 'kind': 'start'}], 'edges': []}
        defn, src = resolve_chat_flow_definition({'flowDefinition': d})
        self.assertEqual(defn, d)
        self.assertEqual(src, 'inline')

    def test_builtin_endpoint_and_autopilot(self):
        from lib.orchestration_endpoint_runner import resolve_chat_flow_definition
        for name in ('endpoint', 'autopilot'):
            defn, src = resolve_chat_flow_definition({'flowBuiltin': name})
            self.assertIsNotNone(defn)
            self.assertEqual(src, f'builtin:{name}')
            self.assertEqual(defn['schema'], 'tofu.orchestration/v1')

    def test_unknown_builtin_returns_none(self):
        from lib.orchestration_endpoint_runner import resolve_chat_flow_definition
        defn, src = resolve_chat_flow_definition({'flowBuiltin': 'nope'})
        self.assertIsNone(defn)
        self.assertEqual(src, '')

    def test_stored_id_resolved_via_loader(self):
        import lib.orchestration_endpoint_runner as rm
        from lib.orchestration import build_endpoint_definition
        orig = rm._load_stored_definition
        rm._load_stored_definition = lambda fid: (build_endpoint_definition()
                                                  if fid == 'orch_known' else None)
        try:
            defn, src = rm.resolve_chat_flow_definition({'flowId': 'orch_known'})
            self.assertIsNotNone(defn)
            self.assertEqual(src, 'stored:orch_known')
            # unknown id → nothing
            defn2, src2 = rm.resolve_chat_flow_definition({'flowId': 'orch_missing'})
            self.assertIsNone(defn2)
        finally:
            rm._load_stored_definition = orig


class AutopilotE2ETest(unittest.TestCase):
    """run_autopilot_via_flow with the SubAgent runner stubbed (no LLM)."""

    def _make_task(self):
        return {
            'id': 'autoflowtask01',
            'convId': 'conv1',
            'messages': [{'role': 'user', 'content': 'keep working'}],
            'config': {'autopilotMaxIterations': 4},
            'events': [],
            'events_lock': threading.Lock(),
            'content_lock': threading.Lock(),
            'toolRounds': [],
            'phase': 'tool',
        }

    def test_autopilot_run_emits_user_and_assistant_turns(self):
        import lib.orchestration_engine as eng
        import lib.orchestration_endpoint_runner as runner_mod
        from lib.orchestration_endpoint_runner import run_autopilot_via_flow

        vu = {'n': 0}
        def fake_runner(self, node, context, iteration):
            role = node.get('role')
            if role == 'virtual_user':
                vu['n'] += 1
                out = '[VU: TASK_DONE]' if vu['n'] >= 2 else 'keep going'
                return {'output': out, 'status': 'completed', 'error': ''}
            return {'output': f'work{iteration}', 'status': 'completed',
                    'error': '', 'tool_names': ['write_file']}

        orig_tools = runner_mod._build_tools_for_task
        runner_mod._build_tools_for_task = lambda task: ([], '', '')

        captured = []
        import lib.tasks_pkg.manager as mgr
        orig_append, orig_persist = mgr.append_event, mgr.persist_task_result
        mgr.append_event = lambda task, event: captured.append(event)
        mgr.persist_task_result = lambda task: None

        import lib.tasks_pkg.endpoint as ep_mod
        saved = (ep_mod._store_endpoint_turns_on_task,
                 ep_mod._sync_endpoint_turns_to_conversation,
                 ep_mod._trigger_per_turn_auto_translate,
                 ep_mod._trigger_endpoint_auto_translate)
        ep_mod._store_endpoint_turns_on_task = lambda task, turns: None
        ep_mod._sync_endpoint_turns_to_conversation = lambda task, turns: len(turns) - 1
        ep_mod._trigger_per_turn_auto_translate = lambda task, m, i: None
        ep_mod._trigger_endpoint_auto_translate = lambda task, turns: None

        orig_default = eng.FlowExecutor._default_runner
        eng.FlowExecutor._default_runner = fake_runner
        captured_turns = []
        adapter_cls = None
        try:
            # Capture the adapter's produced messages to assert roles.
            import lib.orchestration_endpoint_adapter as ad_mod
            real_emit_holder = {}
            orig_adapter = ad_mod.EndpointEventAdapter

            class _SpyAdapter(orig_adapter):
                def _push(self, msg):
                    captured_turns.append((msg.get('role'),
                                           msg.get('_isEndpointReview', False),
                                           msg.get('_isVirtualUser', False)))
                    return super()._push(msg)
            ad_mod.EndpointEventAdapter = _SpyAdapter
            try:
                task = self._make_task()
                run_autopilot_via_flow(task)
            finally:
                ad_mod.EndpointEventAdapter = orig_adapter
        finally:
            eng.FlowExecutor._default_runner = orig_default
            runner_mod._build_tools_for_task = orig_tools
            mgr.append_event, mgr.persist_task_result = orig_append, orig_persist
            (ep_mod._store_endpoint_turns_on_task,
             ep_mod._sync_endpoint_turns_to_conversation,
             ep_mod._trigger_per_turn_auto_translate,
             ep_mod._trigger_endpoint_auto_translate) = saved

        # VU stopped the loop after the 2nd reply.
        self.assertEqual(vu['n'], 2)
        # Turns alternate worker(assistant) → vu(user) → worker → vu. VU user
        # turns are stamped ``_isVirtualUser`` (a routable lane, NO endpoint
        # marker — see EndpointEventAdapter._mark_user_side), NOT
        # ``_isEndpointReview`` (the endpoint critic's display-only lane).
        self.assertEqual(captured_turns,
                         [('assistant', False, False), ('user', False, True),
                          ('assistant', False, False), ('user', False, True)])
        types = [e.get('type') for e in captured]
        self.assertIn('endpoint_iteration', types)   # worker (assistant) turns
        self.assertIn('endpoint_critic_msg', types)   # VU (user) turns
        self.assertIn('done', types)
        self.assertEqual(task['status'], 'done')
        self.assertTrue(task.get('_endpoint_via_flow'))
        self.assertEqual(task.get('_flow_label'), 'autopilot')


if __name__ == '__main__':
    unittest.main()
