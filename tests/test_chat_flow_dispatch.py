"""tests/test_chat_flow_dispatch.py — chat → FlowExecutor dispatch.

Covers the final convergence wiring added in routes/chat.py:
  * resolve_chat_flow_entry — precedence + flag gating (autopilot /
    user-selected flow).
  * resolve_chat_flow_definition — inline / builtin / stored resolution.
  * autopilot_via_flow_enabled — symmetric flag (default OFF).
  * run_autopilot_via_flow — end-to-end with the SubAgent runner stubbed
    (no real LLM), asserting worker→assistant / virtual_user→user turns and
    [VU: TASK_DONE] termination.
"""

import os
import threading
import unittest

import pytest


pytestmark = pytest.mark.unit


class FlagGateTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def tearDown(self):
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def test_autopilot_flag_default_off(self):
        from lib.orchestration_chat_flow_runner import autopilot_via_flow_enabled
        self.assertFalse(autopilot_via_flow_enabled())

    def test_autopilot_flag_explicit(self):
        from lib.orchestration_chat_flow_runner import autopilot_via_flow_enabled
        for v in ('1', 'true', 'YES', 'on'):
            os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = v
            self.assertTrue(autopilot_via_flow_enabled(), v)
        for v in ('0', 'false', '', 'nope'):
            os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = v
            self.assertFalse(autopilot_via_flow_enabled(), v)


class ResolveEntryTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def tearDown(self):
        os.environ.pop('TOFU_AUTOPILOT_VIA_FLOW', None)

    def _resolve(self, cfg):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_entry
        return resolve_chat_flow_entry(cfg)

    def test_no_selection_no_flags_returns_none(self):
        self.assertIsNone(self._resolve({}))
        self.assertIsNone(self._resolve({'autopilot': True}))      # flag off

    def test_autopilot_flag_routes_to_autopilot_runner(self):
        from lib.orchestration_chat_flow_runner import run_autopilot_via_flow
        os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = '1'
        self.assertIs(self._resolve({'autopilot': True}), run_autopilot_via_flow)

    def test_selected_flow_always_wins_no_flag_needed(self):
        from lib.orchestration_chat_flow_runner import run_flow_via_chat
        # EVERY dropdown flow selection routes to the engine flow path
        # unconditionally (the selection IS the opt-in). See
        # test_builtin_autopilot_routes_to_engine for the symmetry that the
        # "编排流程 → 目标模式" dropdown deliberately runs the FlowExecutor
        # autopilot (so engine bugs surface in the frontend).
        self.assertIs(self._resolve({'flowId': 'orch_x'}), run_flow_via_chat)
        self.assertIs(self._resolve({'flowDefinition': {'nodes': [1]}}),
                      run_flow_via_chat)
        self.assertIs(self._resolve({'flowBuiltin': 'autopilot'}),
                      run_flow_via_chat)

    def test_builtin_autopilot_routes_to_engine(self):
        # The "编排流程 → 目标模式" dropdown (flowBuiltin='autopilot') routes to
        # the FlowExecutor engine path (run_flow_via_chat), flag-INDEPENDENT —
        # the selection is the opt-in. There is NO Option-C rewrite: cfg is NOT
        # mutated to autopilot=True, and flowBuiltin is preserved so the engine
        # runs the autopilot graph.
        from lib.orchestration_chat_flow_runner import run_flow_via_chat
        cfg = {'flowBuiltin': 'autopilot'}
        self.assertIs(self._resolve(cfg), run_flow_via_chat)
        self.assertNotIn('autopilot', cfg)         # NO live-path rewrite
        self.assertEqual(cfg.get('flowBuiltin'), 'autopilot')  # selection preserved

    def test_builtin_autopilot_engine_route_independent_of_flag(self):
        # The TOFU_AUTOPILOT_VIA_FLOW flag governs the "模式" TOGGLE path
        # (config['autopilot']), NOT a dropdown flow selection. A dropdown
        # builtin:autopilot goes to the engine whether the flag is on or off.
        from lib.orchestration_chat_flow_runner import run_flow_via_chat
        for flag in ('0', '1'):
            os.environ['TOFU_AUTOPILOT_VIA_FLOW'] = flag
            cfg = {'flowBuiltin': 'autopilot'}
            self.assertIs(self._resolve(cfg), run_flow_via_chat, flag)
            self.assertNotIn('autopilot', cfg)


def test_chat_flow_selection_policy_has_one_physical_owner():
    from pathlib import Path

    import lib.orchestration_chat_flow_selection as selection
    import lib.orchestration_chat_flow_runner as runner

    runner_source = Path('lib/orchestration_chat_flow_runner.py').read_text()
    policy_source = Path(
        'lib/orchestration_chat_flow_selection.py').read_text()

    assert runner.autopilot_via_flow_enabled is selection.autopilot_via_flow_enabled
    assert selection.CHAT_FLOW_BUILTINS == frozenset({'autopilot'})
    assert "os.environ.get(name, '0')" in policy_source
    assert 'DefinitionServiceError' in policy_source
    assert 'os.environ.get' not in runner_source
    assert 'DefinitionServiceError' not in runner_source


def test_explicit_chat_flow_selection_does_not_evaluate_rollout_flags():
    from lib.orchestration_chat_flow_selection import (
        CHAT_FLOW_ENTRY_SELECTED,
        select_chat_flow_entry,
    )

    def unexpected_flag_read():
        raise AssertionError('explicit selections must bypass rollout flags')

    assert select_chat_flow_entry(
        {'flowId': 'orch_selected'},
        autopilot_enabled=unexpected_flag_read,
    ) == CHAT_FLOW_ENTRY_SELECTED


class ResolveDefinitionTest(unittest.TestCase):
    def test_inline_definition(self):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_definition
        d = {'schema': 'tofu.orchestration/v1', 'name': 'X',
             'nodes': [{'id': 's', 'type': 'control', 'kind': 'start'}], 'edges': []}
        defn, src = resolve_chat_flow_definition({'flowDefinition': d})
        self.assertEqual(defn, d)
        self.assertEqual(src, 'inline')

    def test_builtin_autopilot(self):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_definition
        defn, src = resolve_chat_flow_definition({'flowBuiltin': 'autopilot'})
        self.assertIsNotNone(defn)
        self.assertEqual(src, 'builtin:autopilot')
        self.assertEqual(defn['schema'], 'tofu.orchestration/v1')

    def test_unknown_builtin_returns_none(self):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_definition
        defn, src = resolve_chat_flow_definition({'flowBuiltin': 'nope'})
        self.assertIsNone(defn)
        self.assertEqual(src, '')

    def test_stored_id_resolved_via_loader(self):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_definition

        class Definitions:
            def resolve(self, *, inline=None, builtin='', stored_id='',
                        require_inline_nodes=False):
                self.request = {
                    'inline': inline,
                    'builtin': builtin,
                    'stored_id': stored_id,
                    'require_inline_nodes': require_inline_nodes,
                }
                definition = (
                    {'schema': 'tofu.orchestration/v1', 'name': 'X',
                     'nodes': [{'id': 's', 'type': 'control', 'kind': 'start'}],
                     'edges': []}
                    if stored_id == 'orch_known' else None)
                return type('Resolved', (), {
                    'definition': definition,
                    'source': f'stored:{stored_id}' if definition else '',
                })()

        service = Definitions()
        defn, src = resolve_chat_flow_definition(
            {'flowId': 'orch_known'}, definition_service=service)
        self.assertIsNotNone(defn)
        self.assertEqual(src, 'stored:orch_known')
        self.assertEqual(service.request, {
            'inline': None,
            'builtin': '',
            'stored_id': 'orch_known',
            'require_inline_nodes': True,
        })
        # unknown id → nothing
        defn2, src2 = resolve_chat_flow_definition(
            {'flowId': 'orch_missing'}, definition_service=service)
        self.assertIsNone(defn2)
        self.assertEqual(src2, '')

    def test_definition_service_failure_reports_unresolved_selection(self):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_definition
        from lib.orchestration.errors import DefinitionServiceError

        class OfflineDefinitions:
            def resolve(self, **_kwargs):
                raise DefinitionServiceError('offline')

        self.assertEqual(
            resolve_chat_flow_definition(
                {'flowId': 'orch_offline'},
                definition_service=OfflineDefinitions(),
            ),
            (None, ''),
        )

    def test_definition_programmer_error_is_not_mislabeled_as_missing(self):
        from lib.orchestration_chat_flow_runner import resolve_chat_flow_definition

        class BrokenDefinitions:
            def resolve(self, **_kwargs):
                raise RuntimeError('resolver contract bug')

        with self.assertRaisesRegex(RuntimeError, 'resolver contract bug'):
            resolve_chat_flow_definition(
                {'flowId': 'orch-broken'},
                definition_service=BrokenDefinitions(),
            )

    def test_chat_launch_reuses_definition_service_for_nested_flows(self):
        from pathlib import Path

        source = Path('lib/orchestration_chat_flow_runner.py').read_text()
        start = source.index('def run_flow_via_chat(')
        end = source.index('\ndef _run_flow_as_chat_task(', start)
        launch = source[start:end]

        self.assertIn(
            'owner_user_id, tenant_id = _task_repository_identity(task)',
            launch,
        )
        self.assertIn('definitions = _definition_service(', launch)
        self.assertIn('definition_service=definitions', launch)
        self.assertNotIn('OrchestrationDefinitionService.from_path', launch)


def test_missing_selected_flow_fails_closed_instead_of_running_endpoint(
        monkeypatch):
    import lib.orchestration_chat_flow_runner as runner
    import lib.tasks_pkg.manager as manager

    class MissingDefinitions:
        def resolve(self, **_kwargs):
            return type('Resolved', (), {
                'definition': None,
                'source': '',
            })()

    failed = []
    monkeypatch.setattr(
        runner, '_definition_service',
        lambda *_args, **_kwargs: MissingDefinitions())
    monkeypatch.setattr(
        runner,
        '_run_flow_as_chat_task',
        lambda *_args, **_kwargs: pytest.fail(
            'an unavailable selection must not run a fallback graph'),
    )
    monkeypatch.setattr(
        manager,
        'finalize_chat_task_error',
        lambda task, error, **kwargs: failed.append((task, error, kwargs)),
    )

    task = {
        'id': 'missing-flow-task-0001',
        '_userId': 1,
        'config': {'flowId': 'orch_deleted', 'model': 'test-model'},
    }
    runner.run_flow_via_chat(task)

    assert len(failed) == 1
    owner, error, kwargs = failed[0]
    assert owner is task
    assert error['kind'] == 'bad_request'
    assert error['retryable'] is False
    assert 'stored:orch_deleted' in error['detail']
    assert kwargs['flow_reason'] == 'definition_unavailable'
    assert task['_flow_label'] == 'flow(stored:orch_deleted)'


def test_flow_worker_crash_uses_shared_chat_terminal_boundary(monkeypatch):
    import lib.orchestration_chat_flow_runner as runner
    import lib.tasks_pkg.manager as manager

    failed = []
    monkeypatch.setattr(
        runner,
        '_execute_flow_as_chat_task',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('executor exploded')),
    )
    monkeypatch.setattr(
        manager,
        'finalize_chat_task_error',
        lambda task, error, **kwargs: failed.append((task, error, kwargs)),
    )
    task = {
        'id': 'crashed-flow-task-0001',
        'config': {'model': 'test-model'},
        'status': 'running',
    }

    assert runner._run_flow_as_chat_task(
        task, {'nodes': [], 'edges': []}, label='flow(stored:x)', max_iter=3,
    ) is None

    assert len(failed) == 1
    owner, error, kwargs = failed[0]
    assert owner is task
    assert error['kind'] == 'internal'
    assert error['context'] == 'orchestration-flow-fatal'
    assert error['model'] == 'test-model'
    assert 'executor exploded' in error['raw']
    assert kwargs['flow_reason'] == 'fatal'


class FlowChatContextParityTest(unittest.TestCase):
    def test_full_history_and_system_prompt_are_kept_on_separate_channels(self):
        from lib.orchestration_chat_flow_runner import (
            _build_flow_initial_context,
            _extract_system_prompt,
        )

        task = {'messages': [
            {'role': 'system', 'content': 'PROJECT RULES'},
            {'role': 'user', 'content': 'first requirement'},
            {'role': 'assistant', 'content': 'earlier implementation state'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'latest requirement'},
            ]},
        ]}
        context = _build_flow_initial_context(task)
        self.assertIn('[user] first requirement', context)
        self.assertIn('[assistant] earlier implementation state', context)
        self.assertIn('[user] latest requirement', context)
        self.assertNotIn('PROJECT RULES', context)
        self.assertEqual(_extract_system_prompt(task), 'PROJECT RULES')


class RequestExtractTest(unittest.TestCase):
    def test_extracts_latest_user_text(self):
        from lib.orchestration_chat_flow_runner import _extract_user_request
        task = {'messages': [
            {'role': 'user', 'content': 'first'},
            {'role': 'assistant', 'content': 'reply'},
            {'role': 'user', 'content': 'LATEST request'},
        ]}
        self.assertEqual(_extract_user_request(task), 'LATEST request')

    def test_extracts_multimodal_text(self):
        from lib.orchestration_chat_flow_runner import _extract_user_request
        task = {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'part one'},
            {'type': 'image', 'source': {}},
            {'type': 'text', 'text': 'part two'},
        ]}]}
        self.assertIn('part one', _extract_user_request(task))
        self.assertIn('part two', _extract_user_request(task))


class AutopilotE2ETest(unittest.TestCase):
    """run_autopilot_via_flow with the SubAgent runner stubbed (no LLM)."""

    def _make_task(self):
        return {
            'id': 'autoflowtask01',
            '_userId': 1,
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
        import lib.orchestration_chat_flow_runner as runner_mod
        from lib.orchestration_chat_flow_runner import run_autopilot_via_flow

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

        import lib.orchestration_chat_turn_sync as turn_sync
        saved = (turn_sync.store_flow_turns_on_task,
                 turn_sync.sync_flow_turns_to_conversation)
        turn_sync.store_flow_turns_on_task = lambda task, turns: None
        turn_sync.sync_flow_turns_to_conversation = lambda task, turns: len(turns) - 1

        orig_default = eng.FlowExecutor._default_runner
        eng.FlowExecutor._default_runner = fake_runner
        captured_turns = []
        adapter_cls = None
        try:
            # Capture the adapter's produced messages to assert roles.
            import lib.orchestration_chat_flow_adapter as ad_mod
            real_emit_holder = {}
            orig_adapter = ad_mod.FlowEventAdapter

            class _SpyAdapter(orig_adapter):
                def _push(self, msg):
                    captured_turns.append((msg.get('role'),
                                           msg.get('_isFlowReview', False),
                                           msg.get('_isVirtualUser', False)))
                    return super()._push(msg)
            ad_mod.FlowEventAdapter = _SpyAdapter
            try:
                task = self._make_task()
                run_autopilot_via_flow(task)
            finally:
                ad_mod.FlowEventAdapter = orig_adapter
        finally:
            eng.FlowExecutor._default_runner = orig_default
            runner_mod._build_tools_for_task = orig_tools
            mgr.append_event, mgr.persist_task_result = orig_append, orig_persist
            (turn_sync.store_flow_turns_on_task,
             turn_sync.sync_flow_turns_to_conversation) = saved

        # VU stopped the loop after the 2nd reply.
        self.assertEqual(vu['n'], 2)
        # Turns alternate worker(assistant) → vu(user) → worker. The
        # terminal VU sentinel is control-plane only, matching standalone
        # Autopilot: it stops the graph and cancels the eager live placeholder
        # without becoming a visible/persisted user message.
        self.assertEqual(captured_turns,
                         [('assistant', False, False), ('user', False, True),
                          ('assistant', False, False)])
        types = [e.get('type') for e in captured]
        self.assertIn('flow_iteration', types)   # worker (assistant) turns
        self.assertIn('flow_critic_msg', types)   # VU (user) turns
        terminal_vu = [e for e in captured
                       if e.get('type') == 'flow_critic_msg'
                       and e.get('turnRole') == 'virtual_user'
                       and e.get('next_phase') == 'stop']
        self.assertEqual(len(terminal_vu), 1)
        self.assertTrue(terminal_vu[0].get('discard'))
        self.assertIn('done', types)
        self.assertEqual(task['status'], 'done')
        self.assertTrue(task.get('flow_mode'))
        self.assertEqual(task.get('_flow_label'), 'autopilot')


if __name__ == '__main__':
    unittest.main()
