"""tests/test_capabilities_extensibility.py — the capabilities payload must
ADVERTISE the tool-registration surface, or it is undiscoverable.

A headless caller can register its own tools two ways (docs/CUSTOM_TOOLS.md +
docs/TOOL_PLUGINS.md):

  * per-request ``tools=[…]`` on ``/api/v1/agent/run`` (the ``custom__`` name
    contract; ``client`` / ``webhook`` / ``sandbox`` execution modes; the
    ``POST /api/v1/tasks/{id}/tool_result`` client-handoff callback);
  * operator-installed ``tofu.tools`` entry-point plugins, opted into per
    request via ``config.plugins``.

Neither was reflected anywhere in ``GET /api/v1/capabilities`` — an external
client had no programmatic way to learn the extension surface exists. This
suite gates the fix: the payload must carry an ``extensibility`` block
describing both, including that ``sandbox`` mode is reported DISABLED by
default and flips to enabled when ``TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX`` is set.
"""

from __future__ import annotations

import re
import unittest


class ExtensibilityCapabilitiesTest(unittest.TestCase):
    """Drive the real _build_capabilities() (pure function of registries +
    env), not a mocked payload."""

    def _build(self, *, sandbox_env=None):
        import os
        from unittest.mock import patch
        # Fresh build each call — the route caches, but _build_capabilities
        # itself is uncached and reads the env live.
        import routes.api_v1.capabilities as cap
        env = dict(os.environ)
        env.pop('TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX', None)
        if sandbox_env is not None:
            env['TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX'] = sandbox_env
        with patch.dict(os.environ, env, clear=True):
            return cap._build_capabilities()

    # ── the block exists and is shaped ──────────────────────────────

    def test_payload_has_extensibility_block(self):
        payload = self._build()
        self.assertIn('extensibility', payload,
                      'capabilities must advertise the tool-registration '
                      'surface under "extensibility"')
        ext = payload['extensibility']
        self.assertIn('custom_tools', ext)
        self.assertIn('plugins', ext)

    # ── per-request custom tools ─────────────────────────────────────

    def test_custom_tools_advertises_contract_and_modes(self):
        ext = self._build()['extensibility']
        ct = ext['custom_tools']
        # How a caller sends them + the endpoint that accepts them.
        self.assertEqual(ct.get('submit_via'), '/api/v1/agent/run')
        self.assertEqual(ct.get('request_field'), 'tools')
        # Name contract — an external caller must know the required prefix.
        self.assertTrue(ct.get('name_prefix', '').startswith('custom__'))
        self.assertTrue(ct.get('name_pattern'))
        # A representative valid name matches the advertised pattern.
        self.assertRegex('custom__get_weather', ct['name_pattern'])
        self.assertNotRegex('get_weather', ct['name_pattern'])
        # The client-handoff callback endpoint is discoverable.
        self.assertEqual(ct.get('result_callback'),
                         '/api/v1/tasks/{id}/tool_result')

    def test_custom_tools_modes_listed_with_enabled_flags(self):
        ct = self._build()['extensibility']['custom_tools']
        modes = ct.get('modes')
        self.assertIsInstance(modes, dict)
        for m in ('client', 'webhook', 'sandbox'):
            self.assertIn(m, modes, f'mode {m} must be advertised')
            self.assertIn('enabled', modes[m])
        # client + webhook are always available; sandbox is operator-gated.
        self.assertTrue(modes['client']['enabled'])
        self.assertTrue(modes['webhook']['enabled'])

    def test_sandbox_mode_disabled_by_default(self):
        ct = self._build()['extensibility']['custom_tools']
        self.assertFalse(ct['modes']['sandbox']['enabled'],
                         'sandbox RCE mode must be OFF unless the operator '
                         'sets TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX')

    def test_sandbox_mode_enabled_when_env_set(self):
        ct = self._build(sandbox_env='1')['extensibility']['custom_tools']
        self.assertTrue(ct['modes']['sandbox']['enabled'],
                        'sandbox must flip to enabled when '
                        'TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX=1')

    def test_custom_tools_limits_present(self):
        ct = self._build()['extensibility']['custom_tools']
        limits = ct.get('limits') or {}
        # Surface the caps a caller would otherwise hit as opaque 400s.
        self.assertIn('max_tools', limits)
        self.assertGreater(limits['max_tools'], 0)

    # ── entry-point plugins ──────────────────────────────────────────

    def test_plugins_block_lists_available_and_optin(self):
        ext = self._build()['extensibility']
        plugins = ext['plugins']
        # available: the map from available_plugins() (may be empty in test env)
        self.assertIn('available', plugins)
        self.assertIsInstance(plugins['available'], dict)
        # How to opt in per request.
        self.assertEqual(plugins.get('opt_in_field'), 'config.plugins')
        self.assertIn('env_default', plugins)  # TOFU_DEFAULT_TOOL_PLUGINS

    def test_plugins_available_matches_registry(self):
        """The advertised plugin map must equal available_plugins() — no drift,
        no hand-maintained duplicate."""
        from lib.tools.registry import available_plugins
        plugins = self._build()['extensibility']['plugins']
        self.assertEqual(plugins['available'], available_plugins())


if __name__ == '__main__':
    unittest.main()
