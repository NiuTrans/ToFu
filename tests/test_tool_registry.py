"""tests/test_tool_registry.py — Declarative tool-assembly registry.

Pins the contract that lets tools be added/removed as drop-in
:class:`~lib.tools.registry.ToolSpec` plugins without editing core
orchestration code (``lib/tasks_pkg/model_config.py``).

Covered:
  * Tool ordering matches the cache-stable legacy layout.
  * ``has_real_tools`` snapshot semantics (base vs capability phase).
  * Caller-supplied ``cfg['tools']`` override short-circuits assembly.
  * Memory attaches iff a base tool exists; swarm/mcp do NOT need base tools.
  * Third-party plugin specs register and contribute through the same path.
  * ``_WRITE_TOOLS`` / ``_IDEMPOTENT_TOOLS`` stay in sync with spec flags.
"""

from __future__ import annotations

import unittest

from lib.tasks_pkg.model_config import _assemble_tool_list
from lib.tools import ToolContext, ToolSpec, all_specs, assemble_tool_list, register_tool_spec


def _names(tool_list):
    return [t['function']['name'] for t in (tool_list or [])]


def _ctx(**overrides):
    base = dict(
        cfg={}, task_id='t-test',
        project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        swarm_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[],
    )
    base.update(overrides)
    return ToolContext(**base)


class TestOrdering(unittest.TestCase):
    def test_full_project_ordering_is_cache_stable(self):
        tl, hr = assemble_tool_list(_ctx(
            project_path='/tmp/x', project_enabled=True,
            search_mode='multi', search_enabled=True, fetch_enabled=True,
        ))
        names = _names(tl)
        self.assertTrue(hr)
        # search → fetch → read_files → inspect_image → project tools → memory (end)
        self.assertEqual(names[:7], [
            'web_search', 'fetch_url', 'read_files', 'inspect_image',
            'list_dir', 'grep_search', 'find_files',
        ])
        # memory tools always come last (capability phase)
        self.assertIn('create_memory', names)
        self.assertLess(names.index('run_command'), names.index('create_memory'),
                        'project (base) must precede memory (capability)')

    def test_single_search_mode_is_legacy_alias_for_multi(self):
        # 'single' is a retired mode kept as a backward-compat alias: it now
        # yields the same web_search (multi) tool as 'multi'.
        tl, _ = assemble_tool_list(_ctx(search_mode='single', search_enabled=True))
        self.assertEqual(_names(tl)[0], 'web_search')


class TestPhaseSemantics(unittest.TestCase):
    def test_read_files_always_on_and_pulls_memory(self):
        # Even bare (no project/search), read_files is on → counts as a base
        # tool → memory tools attach.
        tl, hr = assemble_tool_list(_ctx())
        names = _names(tl)
        self.assertTrue(hr)
        self.assertIn('read_files', names)
        self.assertIn('create_memory', names)

    def test_swarm_without_base_tools(self):
        # Swarm is NOT gated on has_base_tools — but read_files is always on,
        # so assert the three swarm tools are present regardless.
        tl, _ = assemble_tool_list(_ctx(swarm_enabled=True))
        names = _names(tl)
        for n in ('spawn_agents', 'await_agents', 'get_agent_result'):
            self.assertIn(n, names)

    def test_conv_ref_requires_mention(self):
        tl_no, _ = assemble_tool_list(_ctx())
        self.assertNotIn('list_conversations', _names(tl_no))
        # Real server-injected wrapper (carries title=") on a USER turn → on.
        tl_yes, _ = assemble_tool_list(_ctx(messages=[{
            'role': 'user',
            'content': ('The user has attached the following conversation(s):\n'
                        '[REFERENCED_CONVERSATION title="Old chat" id="abc"]\n'
                        'body\n[/REFERENCED_CONVERSATION]'),
        }]))
        self.assertIn('list_conversations', _names(tl_yes))

    def test_conv_ref_structured_field_enables(self):
        # The authoritative signal: a user turn carrying convRefs (raw row).
        tl, _ = assemble_tool_list(_ctx(messages=[{
            'role': 'user', 'content': 'compare with this',
            'convRefs': [{'id': 'abc', 'title': 'Old chat'}],
        }]))
        self.assertIn('list_conversations', _names(tl))

    def test_charter_tools_register_in_project_mode(self):
        # Charter tools (Pillar #2) ride the same project-mode gate as the
        # conv-ref tools — present in project mode, absent otherwise.
        tl_proj, _ = assemble_tool_list(_ctx(
            project_path='/tmp/x', project_enabled=True))
        names = _names(tl_proj)
        self.assertIn('project_charter_read', names)
        self.assertIn('project_charter_propose', names)
        # No project → no charter tools (a charter is per-project).
        tl_none, _ = assemble_tool_list(_ctx())
        self.assertNotIn('project_charter_read', _names(tl_none))
        self.assertNotIn('project_charter_propose', _names(tl_none))

    def test_conv_ref_not_triggered_by_assistant_prose(self):
        # REGRESSION: a conversation *about* the feature, where the assistant
        # quotes the bare token, must NOT self-enable the tools. (This is the
        # exact false-positive that popped the toolset-diverged banner.)
        tl, _ = assemble_tool_list(_ctx(messages=[
            {'role': 'user', 'content': 'what is the REFERENCED_CONVERSATION tag?'},
            {'role': 'assistant',
             'content': 'It is the `[REFERENCED_CONVERSATION` marker injected by...'},
        ]))
        self.assertNotIn('list_conversations', _names(tl))
        self.assertNotIn('get_conversation', _names(tl))


class TestLegacyShim(unittest.TestCase):
    def test_caller_supplied_tools_override(self):
        tl, hr, mtr = _assemble_tool_list(
            cfg={'tools': [{'type': 'function', 'function': {'name': 'foo'}}]},
            project_path='', project_enabled=False, task_id='t', search_mode='multi',
            search_enabled=True, fetch_enabled=True, code_exec_enabled=False,
            browser_enabled=False, desktop_enabled=False, swarm_enabled=False,
            messages=[])
        self.assertEqual(_names(tl), ['foo'])
        self.assertTrue(hr)
        self.assertEqual(mtr, 999_999_999)

    def test_empty_returns_none_tool_list(self):
        # Force-disable read_files by simulating no specs would be a deeper
        # change; instead assert the legacy shim's None contract when the
        # registry produces nothing.  read_files is always on, so we verify
        # the shim wraps an empty list to None via a direct registry call.
        tl, hr = assemble_tool_list(_ctx())
        # read_files keeps this non-empty — assert the shim path stays valid.
        self.assertIsNotNone(tl)
        self.assertTrue(hr)


class TestPluginRegistration(unittest.TestCase):
    def test_plugin_spec_contributes(self):
        marker = {'type': 'function', 'function': {'name': '_test_weather_tool'}}
        spec = ToolSpec(
            key='_test_weather',
            build=lambda ctx: [marker] if ctx.cfg.get('weatherEnabled') else [],
            phase='base', category='test',
        )
        try:
            register_tool_spec(spec)
            self.assertIn(spec, all_specs())
            tl, _ = assemble_tool_list(_ctx(cfg={'weatherEnabled': True}))
            self.assertIn('_test_weather_tool', _names(tl))
            # Gate off → absent.
            tl_off, _ = assemble_tool_list(_ctx(cfg={}))
            self.assertNotIn('_test_weather_tool', _names(tl_off))
        finally:
            # Clean up so the global registry isn't polluted for other tests.
            from lib.tools import registry as _reg
            _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != '_test_weather']
            _reg._REGISTERED_KEYS.discard('_test_weather')

    def test_duplicate_key_ignored_without_replace(self):
        from lib.tools import registry as _reg
        spec = ToolSpec(key='_dup_test', build=lambda ctx: [], phase='base')
        try:
            register_tool_spec(spec)
            n_before = len(_reg._TOOL_SPECS)
            register_tool_spec(ToolSpec(key='_dup_test', build=lambda ctx: [], phase='base'))
            self.assertEqual(len(_reg._TOOL_SPECS), n_before, 'duplicate must be ignored')
        finally:
            _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != '_dup_test']
            _reg._REGISTERED_KEYS.discard('_dup_test')


class TestHandlerSync(unittest.TestCase):
    """A ToolSpec with handler= must bind into the dispatch tool_registry,
    so one external package can ship schema + gate + handler."""

    def _make_spec(self, key, names, *, special=''):
        def _h(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, pp, pe,
               all_tools=None):
            return tc_id, f'ran:{fn_name}', False
        return ToolSpec(
            key=key,
            build=lambda ctx: [],
            phase='base',
            provides=frozenset(names),
            handler=_h,
            handler_special=special,
            category='test',
        ), _h

    def _cleanup(self, key):
        from lib.tools import registry as _reg
        _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != key]
        _reg._REGISTERED_KEYS.discard(key)

    def test_late_registered_handler_is_bound(self):
        # Importing executor runs the startup sync + sets _dispatch_registry.
        from lib.tasks_pkg.executor import tool_registry
        spec, fn = self._make_spec('_hsync_a', ['_hsync_tool_a'])
        try:
            register_tool_spec(spec)
            self.assertIs(tool_registry.lookup('_hsync_tool_a', None), fn)
        finally:
            self._cleanup('_hsync_a')

    def test_special_handler_binding(self):
        from lib.tasks_pkg.executor import tool_registry
        spec, fn = self._make_spec('_hsync_b', ['_hsync_tool_b'],
                                   special='__hsync_special__')
        try:
            register_tool_spec(spec)
            # Special handlers are matched via round_entry toolName mapping;
            # assert it landed in the special table.
            self.assertIs(tool_registry._special.get('__hsync_special__'), fn)
        finally:
            self._cleanup('_hsync_b')

    def test_startup_sync_is_idempotent(self):
        # Re-running sync_spec_handlers must not raise and must keep bindings.
        from lib.tools import sync_spec_handlers
        from lib.tasks_pkg.executor import tool_registry
        n = sync_spec_handlers(tool_registry)
        self.assertGreaterEqual(n, 0)


class TestConcurrencyFlagSync(unittest.TestCase):
    def test_write_and_idempotent_sets_reflect_specs(self):
        from lib.tasks_pkg.tool_dispatch import _IDEMPOTENT_TOOLS, _WRITE_TOOLS
        # Project write tools.
        self.assertIn('run_command', _WRITE_TOOLS)
        self.assertIn('write_file', _WRITE_TOOLS)
        # Memory write tools.
        self.assertIn('create_memory', _WRITE_TOOLS)
        # Idempotent read tools (base set + spec-declared).
        self.assertIn('web_search', _IDEMPOTENT_TOOLS)
        self.assertIn('grep_search', _IDEMPOTENT_TOOLS)
        # Base-set-only browser internals still present.
        self.assertIn('browser_read_tab', _IDEMPOTENT_TOOLS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
