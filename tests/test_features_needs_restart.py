"""Test for A14 — POST /api/features must report needs_restart=True
when trading is being enabled on a server that booted with it disabled.

The test session boots with TRADING_ENABLED=0 (see tests/conftest.py),
so routes.TRADING_ROUTES_REGISTERED is False. Toggling trading_enabled→True
must therefore tell the frontend a restart is required, since blueprint
registration is import-time only.

Run:  pytest tests/test_features_needs_restart.py -v
"""
from __future__ import annotations

import pytest


@pytest.mark.api
class TestTradingFeatureNeedsRestart:

    def test_routes_not_registered_at_boot(self):
        """Sanity-check the test fixture's assumption."""
        from routes import TRADING_ROUTES_REGISTERED
        assert TRADING_ROUTES_REGISTERED is False, (
            'expected the test session to boot with trading OFF — '
            'tests/conftest.py sets TRADING_ENABLED=0'
        )

    def test_enable_trading_signals_restart(self, flask_client):
        """Flipping trading on at runtime must return needs_restart=True."""
        # Force the off→on transition: start from a known False, then enable.
        flask_client.post('/api/v1/features', json={'trading_enabled': False})
        resp = flask_client.post('/api/v1/features', json={'trading_enabled': True})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert 'trading_enabled' in body['changed']
        assert body['needs_restart'] is True, (
            'trading_enabled flipped True on a server that booted with '
            'trading OFF — frontend must be told a restart is required'
        )

    def test_disable_trading_no_restart(self, flask_client):
        """Disabling trading at runtime is safe — the routes are already
        absent (or the feature flag check inside trading_page handles 404).
        Either way, no restart is needed."""
        # Make sure we have something to flip OFF.
        flask_client.post('/api/v1/features', json={'trading_enabled': True})
        resp = flask_client.post('/api/v1/features', json={'trading_enabled': False})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert body['needs_restart'] is False

    def test_unrelated_flag_no_restart(self, flask_client):
        """Toggling other feature flags must not falsely claim a restart
        is needed."""
        resp = flask_client.post('/api/v1/features', json={'debug_mode': True})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['needs_restart'] is False

        # Reset for other tests.
        flask_client.post('/api/v1/features', json={'debug_mode': False})
