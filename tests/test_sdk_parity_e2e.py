# Incident anchor: born in commit 9ded44f5 — Baseline before agent_core leaf relocation
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""tests/test_sdk_parity_e2e.py — Server-backed integration tests
for the SDK-parity additions made in this round:

  1. ``TofuConfig`` OpenAPI schema is auto-generated from
     ``lib.agent_options.TofuOptions`` and exposes the new fields.
  2. UserPromptSubmit hooks fire when a chat task is created via the
     real route handler (no in-memory shortcuts).
  3. PreCompact hooks fire when the compaction pipeline runs against a
     task that exists in the live registry.
  4. ``maxBudgetUsd`` is accepted and round-trips through the API
     surface alongside other ``cfg`` fields.

All four tests boot the real ``server.py`` app the same way
``test_e2e_headless_api.py`` does and drive it via ``app.test_client()``.
This is the project convention for "server-started" tests — Hypercorn
itself isn't booted, but every blueprint, middleware, and route handler
is the production code path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest

import pytest

pytestmark = pytest.mark.api


# ── Boot one app instance per module ────────────────────────────────


_STATE = {'app': None, 'tmp': None, 'admin': None, 'user': None,
           'orig_keys_path': None, 'orig_usage_path': None}


def _setup_once():
    if _STATE['app'] is not None:
        return _STATE
    # ⚠️ DATA-LOSS GUARD (2026-06-28): imports server.py independently of the
    # conftest fixtures, so it must call the keystone DB guard itself before
    # touching the real app/DB.
    from tests.conftest import _assert_test_database
    _assert_test_database('test_sdk_parity_e2e._setup_once')
    _STATE['tmp'] = tempfile.TemporaryDirectory()
    tmp = _STATE['tmp'].name
    from lib import api_keys, usage_tracker
    _STATE['orig_keys_path'] = api_keys._STORE_PATH
    _STATE['orig_usage_path'] = usage_tracker._STORE_PATH
    api_keys._STORE_PATH = os.path.join(tmp, 'api_keys.json')
    api_keys._cache.clear()
    api_keys._cache_loaded = False
    usage_tracker._STORE_PATH = os.path.join(tmp, 'usage.json')
    usage_tracker._state.clear()
    usage_tracker._loaded = False
    os.environ['TUNNEL_TOKEN'] = ''

    import importlib.util
    spec = importlib.util.spec_from_file_location('server_sdk_parity', 'server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _STATE['app'] = mod.app

    from lib.api_keys import create_key
    _, _STATE['admin'] = create_key(name='sdk-parity-admin', scopes=[], admin=True)
    _, _STATE['user'] = create_key(
        name='sdk-parity-user',
        scopes=['chat', 'tasks', 'usage', 'capabilities'],
        rate_limit_rpm=120, rate_limit_tpd=0)
    return _STATE


def _teardown_once():
    if _STATE['app'] is None:
        return
    from lib import api_keys, usage_tracker
    api_keys._STORE_PATH = _STATE['orig_keys_path']
    api_keys._cache.clear()
    api_keys._cache_loaded = False
    usage_tracker._STORE_PATH = _STATE['orig_usage_path']
    usage_tracker._state.clear()
    usage_tracker._loaded = False
    _STATE['tmp'].cleanup()
    _STATE['app'] = None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _hdr(token):
    return {'Authorization': f'Bearer {token}'}


# ── Stub spawn_task so chat doesn't hit a real LLM ──


_STUB_INSTALLED = False


def _install_chat_stub():
    global _STUB_INSTALLED
    if _STUB_INSTALLED:
        return
    import lib.tasks_pkg as pkg
    from lib.tasks_pkg.manager import append_event

    def _fake_spawn(task):
        # Echo the latest user message back so tests can verify the prompt
        # actually reached the stub (= the real route + hooks ran first).
        msgs = task.get('messages') or []
        last_user = ''
        for m in reversed(msgs):
            if m.get('role') == 'user':
                c = m.get('content', '')
                if isinstance(c, str):
                    last_user = c
                break
        task['content'] = f'[stub] echo: {last_user[:120]}'
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        task['usage'] = {'input_tokens': max(1, len(last_user) // 4),
                          'output_tokens': 8}
        append_event(task, {'type': 'delta', 'content': task['content']})
        append_event(task, {'type': 'done', 'finishReason': 'stop',
                              'usage': task['usage']})

    pkg.spawn_task = _fake_spawn
    _STUB_INSTALLED = True


# ── Tests ───────────────────────────────────────────────────────────


class SDKParityE2ETest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _setup_once()
        _install_chat_stub()
        cls.app = _STATE['app']
        cls.user = _STATE['user']
        # Save the hook registries so we can clean up after each test.
        from lib.tasks_pkg import tool_hooks
        cls._hook_mod = tool_hooks

    @classmethod
    def tearDownClass(cls):
        _teardown_once()

    def setUp(self):
        # Snapshot all four hook registries before each test.
        self._snap = (
            list(self._hook_mod._pre_hooks),
            list(self._hook_mod._post_hooks),
            list(self._hook_mod._user_prompt_hooks),
            list(self._hook_mod._pre_compact_hooks),
        )

    def tearDown(self):
        pre, post, ups, pc = self._snap
        self._hook_mod._pre_hooks[:] = pre
        self._hook_mod._post_hooks[:] = post
        self._hook_mod._user_prompt_hooks[:] = ups
        self._hook_mod._pre_compact_hooks[:] = pc

    def _client(self):
        return self.app.test_client()

    # 1. ── OpenAPI schema exposes every TofuOptions field ─────────

    def test_openapi_tofu_config_includes_new_fields(self):
        async def go():
            r = await self._client().get('/api/openapi.json')
            self.assertEqual(r.status_code, 200)
            spec = json.loads(await r.get_data(as_text=True))
            schemas = spec.get('components', {}).get('schemas', {})
            tofu = schemas.get('TofuConfig')
            self.assertIsNotNone(tofu, 'TofuConfig schema missing from OpenAPI')
            props = tofu.get('properties', {})
            # Original well-known fields stay documented.
            for key in ('model', 'maxTokens', 'thinkingDepth', 'searchMode',
                         'projectPath'):
                self.assertIn(key, props, f'expected {key} in TofuConfig')
            # New fields from this round.
            self.assertIn('maxBudgetUsd', props,
                           'maxBudgetUsd missing from TofuConfig')
            self.assertEqual(props['maxBudgetUsd']['type'], 'number')
            # ≥30 properties — auto-generation should outpace the old
            # hand-maintained schema (which had 12).
            self.assertGreaterEqual(len(props), 30,
                'TofuConfig should now expose ≥30 fields, got %d' % len(props))

        _run(go())

    # 2. ── UserPromptSubmit hook fires through the live route ────

    def test_user_prompt_hook_runs_through_chat_route(self):
        observed = {'count': 0, 'orig': None}

        def hook(prompt, task):
            observed['count'] += 1
            observed['orig'] = prompt
            return '[REWRITTEN] ' + prompt

        self._hook_mod.register_user_prompt_hook(hook)

        async def go():
            r = await self._client().post(
                '/api/v1/chat/completions',
                headers=_hdr(self.user),
                json={
                    'model': 'qwen-plus',
                    'messages': [{'role': 'user', 'content': 'original prompt'}],
                    'stream': False,
                },
            )
            self.assertEqual(r.status_code, 200)
            body = json.loads(await r.get_data(as_text=True))
            # The stub echoes the (rewritten) latest user message.
            content = body['choices'][0]['message']['content']
            self.assertIn('[REWRITTEN]', content,
                'UserPromptSubmit rewrite did not propagate through the chat '
                'route. Got: %r' % content)

        _run(go())
        self.assertEqual(observed['count'], 1,
            'hook should fire exactly once per turn (got %d)' % observed['count'])
        self.assertEqual(observed['orig'], 'original prompt')

    # 3. ── PreCompact hook fires from the compaction pipeline ────

    def test_pre_compact_hook_fires_on_pipeline(self):
        # The pipeline is invoked from the orchestrator on the hot path;
        # we exercise it directly with a real task-shaped dict to keep
        # the test fast and deterministic — but the registration goes
        # through the public registry that the production path uses.
        captured = []

        def hook(messages, task):
            captured.append({'len': len(messages),
                              'tid': task.get('id', '')})

        self._hook_mod.register_pre_compact_hook(hook)
        from lib.tasks_pkg.compaction import run_compaction_pipeline
        # Run inside the app's context so any DB teardown_appcontext
        # callbacks registered by the pipeline don't leak `g` access
        # to subsequent unrelated tests (the real route handlers always
        # run inside an active app context).

        async def go():
            async with self.app.app_context():
                run_compaction_pipeline(
                    messages=[{'role': 'user', 'content': 'x'}],
                    current_round=0,
                    task={'id': 'tpc-001', 'convId': 'cpc-001',
                            'config': {}},
                )

        _run(go())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['tid'], 'tpc-001')
        self.assertEqual(captured[0]['len'], 1)

    # 4. ── maxBudgetUsd is accepted by the chat route ────────────

    def test_max_budget_usd_accepted_by_chat_route(self):
        # The stub shortcuts to done before any tool round, so the gate
        # never triggers — but the value must round-trip through the
        # request parsing without being rejected.  This confirms the
        # field is recognized by the parser.
        async def go():
            r = await self._client().post(
                '/api/v1/chat/completions',
                headers=_hdr(self.user),
                json={
                    'model': 'qwen-plus',
                    'messages': [{'role': 'user', 'content': 'hello'}],
                    'stream': False,
                    'config': {'maxBudgetUsd': 5.0},
                },
            )
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))

        _run(go())

    # 5. ── HookResult.modify rewrites tool args end-to-end ───────

    def test_modify_action_rewrites_args_in_place(self):
        # Verify the in-place mutation contract that downstream consumers
        # depend on.  This is the exact code path tool_dispatch.py uses
        # before invoking a tool.
        from lib.tasks_pkg.tool_hooks import HookResult, run_pre_hooks

        def rewrite(name, args, task):
            return HookResult(action='modify',
                              modified_args={'path': '/sandbox/' + args['path']})

        self._hook_mod.register_pre_hook(rewrite)
        args = {'path': 'foo.py'}
        result = run_pre_hooks('write_file', args, {})
        self.assertIsNone(result)  # modify alone does not block
        self.assertEqual(args['path'], '/sandbox/foo.py',
            'in-place mutation contract broken — downstream tools '
            'would see the original args')


if __name__ == '__main__':
    unittest.main()
