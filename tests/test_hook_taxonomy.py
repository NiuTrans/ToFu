"""tests/test_hook_taxonomy.py — Hook taxonomy expansion contract tests.

Covers the three additions made to ``lib/tasks_pkg/tool_hooks.py``:

  1. ``HookResult.modified_args`` is now honored end-to-end (the args dict
     is mutated in place so every downstream consumer sees the rewrite).
  2. ``UserPromptSubmit`` hooks fire once per ``create_task`` and may
     rewrite the latest user message.
  3. ``PreCompact`` hooks fire from ``run_compaction_pipeline`` BEFORE
     any layer mutates the messages list.
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit

# Boot the Flask→Quart shim BEFORE any of our lib.* imports below.  The
# shim lives in server.py at module load — without it, importing
# lib.tasks_pkg.manager pulls in the REAL flask, then later server-backed
# tests that swap to Quart end up with stale flask.g references in
# lib/database/_core.py that fire "Working outside of application
# context" during teardown.
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location(
    'server_for_shim_hook_test', 'server.py')
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
del _spec, _mod, _importlib_util

from lib.tasks_pkg.tool_hooks import (
    HookResult,
    _post_hooks,
    _pre_compact_hooks,
    _pre_hooks,
    _user_prompt_hooks,
    register_pre_compact_hook,
    register_pre_hook,
    register_user_prompt_hook,
    run_pre_compact_hooks,
    run_pre_hooks,
    run_user_prompt_hooks,
)


def _save_registries():
    return (list(_pre_hooks), list(_post_hooks),
            list(_user_prompt_hooks), list(_pre_compact_hooks))


def _restore_registries(snap):
    pre, post, ups, pc = snap
    _pre_hooks[:] = pre
    _post_hooks[:] = post
    _user_prompt_hooks[:] = ups
    _pre_compact_hooks[:] = pc


class _RegistrySandbox(unittest.TestCase):
    """Snapshot global registries so each test's mutations don't leak."""

    def setUp(self):
        self._snap = _save_registries()

    def tearDown(self):
        _restore_registries(self._snap)


class TestPreToolModifyAction(_RegistrySandbox):

    def test_modify_args_in_place(self):
        def rewrite(name, args, task):
            return HookResult(action='modify',
                              modified_args={'path': '/safe/' + args['path']})
        register_pre_hook(rewrite)
        args = {'path': 'foo.py'}
        run_pre_hooks('write_file', args, {})
        self.assertEqual(args['path'], '/safe/foo.py')

    def test_modify_then_block(self):
        order = []

        def rewrite(name, args, task):
            order.append('rewrite')
            return HookResult(action='modify',
                              modified_args={'path': 'rewritten'})

        def block_after(name, args, task):
            order.append('block')
            # Should observe the rewritten args
            assert args['path'] == 'rewritten', args
            return HookResult(action='block', message='no')

        register_pre_hook(rewrite)
        register_pre_hook(block_after)
        args = {'path': 'orig'}
        result = run_pre_hooks('x', args, {})
        self.assertEqual(order, ['rewrite', 'block'])
        self.assertEqual(result.action, 'block')
        self.assertEqual(args['path'], 'rewritten')

    def test_modify_with_added_and_removed_keys(self):
        def rewrite(name, args, task):
            return HookResult(action='modify',
                              modified_args={'a': 1, 'c': 3})
        register_pre_hook(rewrite)
        args = {'a': 0, 'b': 2}
        run_pre_hooks('x', args, {})
        self.assertEqual(args, {'a': 1, 'c': 3})

    def test_modify_ignored_if_not_dict(self):
        def bad(name, args, task):
            return HookResult(action='modify', modified_args=None)
        register_pre_hook(bad)
        args = {'a': 1}
        run_pre_hooks('x', args, {})
        self.assertEqual(args, {'a': 1})


class TestUserPromptSubmit(_RegistrySandbox):

    def test_no_hooks_passthrough(self):
        out = run_user_prompt_hooks('hello', {})
        self.assertEqual(out, 'hello')

    def test_single_hook_rewrites(self):
        register_user_prompt_hook(lambda p, t: p.upper())
        out = run_user_prompt_hooks('hello', {})
        self.assertEqual(out, 'HELLO')

    def test_chained_hooks(self):
        register_user_prompt_hook(lambda p, t: p + ' first')
        register_user_prompt_hook(lambda p, t: p + ' second')
        out = run_user_prompt_hooks('start', {})
        self.assertEqual(out, 'start first second')

    def test_returning_none_passes_through(self):
        register_user_prompt_hook(lambda p, t: None)
        register_user_prompt_hook(lambda p, t: p + '!')
        out = run_user_prompt_hooks('hi', {})
        self.assertEqual(out, 'hi!')

    def test_hook_exception_swallowed(self):
        def boom(p, t):
            raise RuntimeError('intentional')
        register_user_prompt_hook(boom)
        register_user_prompt_hook(lambda p, t: p + '+')
        out = run_user_prompt_hooks('x', {})
        self.assertEqual(out, 'x+')


# NOTE: Integration coverage of UserPromptSubmit through the real
# manager.create_task path lives in tests/test_sdk_parity_e2e.py
# (test_user_prompt_hook_runs_through_chat_route).  We intentionally do
# NOT call manager.create_task from a unit test — it imports the full
# task runtime + thread pool + database layer, polluting Werkzeug's
# application context and breaking subsequent server-backed tests.


class TestPreCompact(_RegistrySandbox):

    def test_no_hooks_no_op(self):
        _pre_compact_hooks[:] = []
        messages = [{'role': 'user', 'content': 'x'}]
        result = run_pre_compact_hooks(messages, {})
        self.assertIsNone(result)
        self.assertEqual(messages, [{'role': 'user', 'content': 'x'}])

    def test_hook_observes_messages(self):
        captured = []
        register_pre_compact_hook(lambda msgs, task: captured.append(len(msgs)))
        run_pre_compact_hooks([{'role': 'user'}, {'role': 'assistant'}], {})
        self.assertEqual(captured, [2])

    def test_hook_exception_swallowed(self):
        def boom(msgs, task):
            raise RuntimeError('intentional')
        register_pre_compact_hook(boom)
        # Should not raise.
        run_pre_compact_hooks([], {})

# NOTE: Integration coverage of run_compaction_pipeline calling the
# PreCompact hook lives in tests/test_sdk_parity_e2e.py
# (test_pre_compact_hook_fires_on_pipeline).  Calling the real pipeline
# from a unit test triggers DB teardown_appcontext callbacks that
# pollute Werkzeug for subsequent server-backed tests.


if __name__ == '__main__':
    unittest.main()
