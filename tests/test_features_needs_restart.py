"""Test the generic feature-flag registry needs_restart path.

A plugin feature flag declared with ``needs_restart=True`` (e.g. one whose
feature mounts blueprints at import time) must make ``POST /api/v1/features``
report ``needs_restart=True`` when the flag is flipped ON on a server that
booted with it OFF — because import-time blueprint registration cannot happen
retroactively.

This used to be hardcoded for ``trading_enabled``; trading now lives in the
standalone ``tofu-trading`` plugin, so we exercise the seam with a synthetic
registered flag instead. Base flags (debug_mode etc.) must never claim a
restart is needed.

Run:  pytest tests/test_features_needs_restart.py -v
"""
from __future__ import annotations

import pytest

from lib import feature_registry as fr


@pytest.fixture
def _synthetic_flag():
    """Register a synthetic needs_restart plugin flag, OFF at boot."""
    fr.register_feature_flag('TEST_PLUGIN_FEATURE', 'test_plugin_feature',
                             default=False, needs_restart=True, replace=True)
    fr.mark_boot_enabled('test_plugin_feature', False)
    try:
        import lib as _lib
        _lib.TEST_PLUGIN_FEATURE = False
        yield 'test_plugin_feature'
    finally:
        fr.unregister_feature_flag('test_plugin_feature')
        fr._BOOT_ENABLED.pop('test_plugin_feature', None)


@pytest.mark.api
class TestPluginFlagNeedsRestart:

    def test_flag_not_boot_enabled(self, _synthetic_flag):
        """Sanity-check the fixture's assumption."""
        assert fr.was_boot_enabled('test_plugin_feature') is False

    def test_enable_needs_restart_flag_signals_restart(self, flask_client, _synthetic_flag):
        """Flipping a needs_restart plugin flag ON at runtime → needs_restart=True."""
        flask_client.post('/api/v1/features', json={'test_plugin_feature': False})
        resp = flask_client.post('/api/v1/features', json={'test_plugin_feature': True})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert 'test_plugin_feature' in body['changed']
        assert body['needs_restart'] is True

    def test_disable_no_restart(self, flask_client, _synthetic_flag):
        """Disabling a needs_restart flag at runtime is safe — no restart."""
        flask_client.post('/api/v1/features', json={'test_plugin_feature': True})
        resp = flask_client.post('/api/v1/features', json={'test_plugin_feature': False})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert body['needs_restart'] is False

    def test_unrelated_base_flag_no_restart(self, flask_client):
        """Toggling a core base flag must not falsely claim a restart."""
        resp = flask_client.post('/api/v1/features', json={'debug_mode': True})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['needs_restart'] is False
        flask_client.post('/api/v1/features', json={'debug_mode': False})
