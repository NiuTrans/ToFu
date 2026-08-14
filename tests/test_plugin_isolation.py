"""tests/test_plugin_isolation.py — Multi-tenant tool-plugin visibility gate.

Pins the contract that third-party ``tofu.tools`` entry-point plugins are
**process-global** once installed but only EXPOSED to a request when explicitly
allow-listed — so a plugin installed for one tenant of a shared (e.g. headless
``/api/v1/agent/run``) server can't leak its tool schema into another tenant's
request.

Covered:
  * ``resolve_enabled_plugins`` resolution order: cfg['plugins'] →
    TOFU_DEFAULT_TOOL_PLUGINS env → fail-closed.
  * The ``'*'`` wildcard maps to ``None`` (gate fully open).
  * ``assemble_tool_list`` hides a plugin spec unless allow-listed, and NEVER
    hides a built-in.
  * ``discover_plugin_specs`` auto-stamps source='plugin' + plugin_name onto
    specs a plugin registers (the author needn't set them).
  * ``available_plugins`` introspection lists installed plugins, not built-ins.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from lib.tools import (
    ToolContext,
    ToolSpec,
    assemble_tool_list,
    available_plugins,
    resolve_enabled_plugins,
)
from lib.tools import registry as _reg


def _names(tool_list):
    return [t['function']['name'] for t in (tool_list or [])]


def _ctx(enabled_plugins=set(), **overrides):  # noqa: B006 — explicit default
    base = dict(
        cfg={}, task_id='t-iso',
        project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        swarm_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[],
        enabled_plugins=enabled_plugins,
    )
    base.update(overrides)
    return ToolContext(**base)


class _PluginSpecMixin:
    """Register a fake plugin spec the way discover_plugin_specs would —
    i.e. stamped with source='plugin' + plugin_name."""

    PLUGIN = '_iso_plugin'
    KEY = '_iso_plugin_spec'
    TOOL = '_iso_plugin_tool'

    def _register_fake_plugin(self):
        spec = ToolSpec(
            key=self.KEY,
            build=lambda ctx: [{'type': 'function',
                                'function': {'name': self.TOOL}}],
            phase='base', category='test',
            source='plugin', plugin_name=self.PLUGIN,
        )
        _reg.register_tool_spec(spec)

    def _cleanup(self):
        _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != self.KEY]
        _reg._REGISTERED_KEYS.discard(self.KEY)


class TestResolveEnabledPlugins(unittest.TestCase):
    def test_absent_is_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_DEFAULT_TOOL_PLUGINS', None)
            self.assertEqual(resolve_enabled_plugins({}), set())

    def test_request_cfg_wins_over_env(self):
        with mock.patch.dict(os.environ,
                             {'TOFU_DEFAULT_TOOL_PLUGINS': 'envonly'}):
            self.assertEqual(resolve_enabled_plugins({'plugins': ['a']}), {'a'})

    def test_env_default_when_cfg_absent(self):
        with mock.patch.dict(os.environ,
                             {'TOFU_DEFAULT_TOOL_PLUGINS': 'a, b'}):
            self.assertEqual(resolve_enabled_plugins({}), {'a', 'b'})

    def test_wildcard_maps_to_none(self):
        self.assertIsNone(resolve_enabled_plugins({'plugins': '*'}))
        self.assertIsNone(resolve_enabled_plugins({'plugins': ['*']}))
        with mock.patch.dict(os.environ,
                             {'TOFU_DEFAULT_TOOL_PLUGINS': '*'}):
            self.assertIsNone(resolve_enabled_plugins({}))

    def test_empty_string_is_fail_closed(self):
        self.assertEqual(resolve_enabled_plugins({'plugins': ''}), set())
        self.assertEqual(resolve_enabled_plugins({'plugins': []}), set())


class TestVisibilityGate(_PluginSpecMixin, unittest.TestCase):
    def setUp(self):
        self._register_fake_plugin()

    def tearDown(self):
        self._cleanup()

    def test_hidden_when_fail_closed(self):
        tl, _ = assemble_tool_list(_ctx(enabled_plugins=set()))
        self.assertNotIn(self.TOOL, _names(tl))
        # Built-in read_files is unaffected by the gate.
        self.assertIn('read_files', _names(tl))

    def test_visible_when_allow_listed(self):
        ctx = _ctx(enabled_plugins={self.PLUGIN})
        assemble_tool_list(ctx)
        self.assertIn(self.TOOL, _names(ctx.enabled_tool_catalog))

    def test_visible_when_gate_open(self):
        ctx = _ctx(enabled_plugins=None)
        assemble_tool_list(ctx)
        self.assertIn(self.TOOL, _names(ctx.enabled_tool_catalog))

    def test_other_plugin_name_does_not_unlock(self):
        tl, _ = assemble_tool_list(_ctx(enabled_plugins={'some_other'}))
        self.assertNotIn(self.TOOL, _names(tl))

    def test_builtins_never_gated(self):
        # Even with an empty allow-list, every built-in is still present.
        tl, hr = assemble_tool_list(_ctx(enabled_plugins=set()))
        self.assertTrue(hr)
        self.assertIn('read_files', _names(tl))


class TestEmptyPluginNameFailsClosed(unittest.TestCase):
    KEY = '_iso_noname_spec'
    TOOL = '_iso_noname_tool'

    def tearDown(self):
        _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS if s.key != self.KEY]
        _reg._REGISTERED_KEYS.discard(self.KEY)

    def test_plugin_with_blank_name_hidden_unless_open(self):
        # A misconfigured plugin spec (source='plugin' but plugin_name='') must
        # never be silently exposed under a non-open allow-list.
        spec = ToolSpec(
            key=self.KEY,
            build=lambda ctx: [{'type': 'function',
                                'function': {'name': self.TOOL}}],
            phase='base', source='plugin', plugin_name='',
        )
        _reg.register_tool_spec(spec)
        tl_closed, _ = assemble_tool_list(_ctx(enabled_plugins={'anything'}))
        self.assertNotIn(self.TOOL, _names(tl_closed))
        ctx_open = _ctx(enabled_plugins=None)
        assemble_tool_list(ctx_open)
        self.assertIn(self.TOOL, _names(ctx_open.enabled_tool_catalog))


class TestAutoStamping(unittest.TestCase):
    """discover_plugin_specs hands the plugin a wrapper that stamps
    source/plugin_name, so the author doesn't have to."""

    def test_register_fn_stamps_provenance(self):
        captured = {}

        def fake_register(register_tool_spec):
            # Author registers a plain spec with NO source/plugin_name set.
            spec = ToolSpec(key='_stamp_test',
                            build=lambda ctx: [], phase='base')
            register_tool_spec(spec)
            captured['after'] = next(
                s for s in _reg._TOOL_SPECS if s.key == '_stamp_test')

        class _EP:
            name = '_stamp_plugin'

            def load(self):
                return fake_register

        try:
            with mock.patch('importlib.metadata.entry_points',
                            return_value=[_EP()]):
                n = _reg.discover_plugin_specs()
            self.assertEqual(n, 1)
            self.assertEqual(captured['after'].source, 'plugin')
            self.assertEqual(captured['after'].plugin_name, '_stamp_plugin')
        finally:
            _reg._TOOL_SPECS[:] = [s for s in _reg._TOOL_SPECS
                                   if s.key != '_stamp_test']
            _reg._REGISTERED_KEYS.discard('_stamp_test')


class TestAvailablePlugins(_PluginSpecMixin, unittest.TestCase):
    def test_lists_plugins_not_builtins(self):
        self._register_fake_plugin()
        try:
            avail = available_plugins()
            self.assertIn(self.PLUGIN, avail)
            self.assertIn(self.KEY, avail[self.PLUGIN])
            # Built-in keys never appear.
            self.assertNotIn('search', avail)
            self.assertNotIn('memory', avail)
        finally:
            self._cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
