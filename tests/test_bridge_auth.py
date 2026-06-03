"""Bridge auth + CORS tests for routes/browser.py and routes/desktop.py.

Covers:
  * When TOFU_BRIDGE_SECRET is unset, bridge endpoints behave as before
    (200/204 on poll/commands/result/desktop_poll).
  * When set, those endpoints return 401 without the matching header,
    200 with it. Comparison is timing-safe (we exercise both wrong
    and right values).
  * Operator-facing endpoints (/api/v1/browser/status, /api/v1/browser/clients,
    /api/v1/browser/test, /api/browser/download, /api/v1/desktop/status) are
    NEVER gated — they're called from the same-origin frontend.
  * /api/browser/* responses no longer carry CORS wildcard headers.

Run:  pytest tests/test_bridge_auth.py -m api -v
"""
from __future__ import annotations

import pytest


SECRET = 'unit-test-bridge-secret-1234567890'
WRONG = 'unit-test-wrong-secret-0000000000'


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _set_secret(monkeypatch, value: str | None):
    """Set or clear TOFU_BRIDGE_SECRET (and the legacy alias) for one test."""
    if value is None:
        monkeypatch.delenv('TOFU_BRIDGE_SECRET', raising=False)
    else:
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', value)


# ═══════════════════════════════════════════════════════════
#  Default behaviour: unset env → no enforcement
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestBridgeAuthDisabledByDefault:
    """When TOFU_BRIDGE_SECRET is unset, every bridge endpoint works without
    the header (current LAN-only default). Regression guard."""

    def test_browser_poll_no_header(self, flask_client, monkeypatch):
        _set_secret(monkeypatch, None)
        resp = flask_client.post('/api/browser/poll', json={})
        assert resp.status_code == 200

    def test_browser_commands_no_header(self, flask_client, monkeypatch):
        _set_secret(monkeypatch, None)
        resp = flask_client.get('/api/browser/commands')
        assert resp.status_code == 200

    def test_browser_result_no_header(self, flask_client, monkeypatch):
        _set_secret(monkeypatch, None)
        # No id → 400 (not 401) — proves auth was skipped.
        resp = flask_client.post('/api/browser/result', json={})
        assert resp.status_code == 400

    def test_desktop_poll_no_header(self, flask_client, monkeypatch):
        _set_secret(monkeypatch, None)
        resp = flask_client.post('/api/desktop/poll', json={})
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════
#  Enforcement: set env → reject without header
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestBridgeAuthEnforced:
    """When TOFU_BRIDGE_SECRET is set, bridge endpoints require the header."""

    @pytest.mark.parametrize('path,method', [
        ('/api/browser/poll', 'POST'),
        ('/api/browser/commands', 'GET'),
        ('/api/browser/result', 'POST'),
        ('/api/desktop/poll', 'POST'),
    ])
    def test_missing_header_rejected(self, flask_client, monkeypatch, path, method):
        _set_secret(monkeypatch, SECRET)
        resp = flask_client.open(path, method=method, json={})
        assert resp.status_code == 401
        body = resp.get_json()
        assert body['error'] == 'bridge_auth_required'

    @pytest.mark.parametrize('path,method', [
        ('/api/browser/poll', 'POST'),
        ('/api/browser/commands', 'GET'),
        ('/api/browser/result', 'POST'),
        ('/api/desktop/poll', 'POST'),
    ])
    def test_wrong_header_rejected(self, flask_client, monkeypatch, path, method):
        _set_secret(monkeypatch, SECRET)
        resp = flask_client.open(path, method=method, json={},
                                 headers={'X-Bridge-Secret': WRONG})
        assert resp.status_code == 401

    def test_correct_header_passes_browser_poll(self, flask_client, monkeypatch):
        _set_secret(monkeypatch, SECRET)
        resp = flask_client.post('/api/browser/poll', json={},
                                 headers={'X-Bridge-Secret': SECRET})
        assert resp.status_code == 200

    def test_correct_header_passes_desktop_poll(self, flask_client, monkeypatch):
        _set_secret(monkeypatch, SECRET)
        resp = flask_client.post('/api/desktop/poll', json={},
                                 headers={'X-Bridge-Secret': SECRET})
        assert resp.status_code == 200

    def test_options_preflight_skips_auth(self, flask_client, monkeypatch):
        """OPTIONS preflight must not be auth-gated."""
        _set_secret(monkeypatch, SECRET)
        resp = flask_client.open('/api/browser/poll', method='OPTIONS')
        assert resp.status_code == 204


# ═══════════════════════════════════════════════════════════
#  Operator endpoints stay unauthenticated (same-origin UI)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestOperatorEndpointsNotGated:
    """UI status endpoints must NOT require X-Bridge-Secret — the
    same-origin frontend at static/js/main.js calls /api/v1/browser/status
    and /api/browser/download without one."""

    @pytest.mark.parametrize('path', [
        '/api/v1/browser/status',
        '/api/v1/browser/clients',
        '/api/v1/desktop/status',
    ])
    def test_no_bridge_secret_required(self, flask_client, monkeypatch, path):
        # Bridge secret is set, but these are operator-facing UI status
        # endpoints — they MUST NOT respond with the bridge_auth_required
        # JSON envelope. (They may still 401 under the global auth gate
        # in private/multi-user modes; the v1 routes are auth-gated. The
        # check we care about here is that the bridge-secret middleware
        # is not in the chain.)
        _set_secret(monkeypatch, SECRET)
        resp = flask_client.get(path)
        body = resp.get_json(silent=True) or {}
        assert body.get('error') != 'bridge_auth_required'


# ═══════════════════════════════════════════════════════════
#  CORS: wildcard removed
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestBrowserCorsWildcardRemoved:
    """B3: dropped the unconditional Access-Control-Allow-Origin: * on
    /api/browser/*. Chrome extensions use host_permissions, not CORS, so
    this header was never required and is a defense-in-depth hazard."""

    @pytest.mark.parametrize('path', [
        '/api/v1/browser/status',
        '/api/v1/browser/clients',
    ])
    def test_no_allow_origin_header(self, flask_client, monkeypatch, path):
        _set_secret(monkeypatch, None)
        resp = flask_client.get(path, headers={'Origin': 'https://evil.example.com'})
        # No CORS header → cross-origin reads blocked by browser. Same-origin
        # (the Tofu frontend itself) is unaffected because CORS isn't engaged.
        assert 'Access-Control-Allow-Origin' not in resp.headers
        assert 'Access-Control-Allow-Methods' not in resp.headers
        assert 'Access-Control-Allow-Headers' not in resp.headers
