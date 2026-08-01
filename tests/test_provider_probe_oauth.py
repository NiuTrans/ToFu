#!/usr/bin/env python3
"""Backend test — the probe must NOT treat a subscription provider's
'oauth-managed' sentinel key as a real credential.

WHY
---
An OAuth subscription provider (oauth_claude / oauth_codex) stores the
SENTINEL api_key 'oauth-managed'; the live token is resolved per request.
Probing that sentinel literally returns a 401, and 401 maps to
'unauthorized' → ``recommend_disable=True`` — which would mark a perfectly
working subscription model as "should be disabled". The probe must instead
resolve the live token via ``lib.oauth.outbound.resolve_oauth_request`` and
report a NEUTRAL 'not_logged_in' verdict when no token exists.

WHAT IS GUARDED
------------------------------------------------------------------
  * An oauth provider cell calls ``resolve_oauth_request`` (never sends the
    sentinel). The request uses ``x-api-key``, NOT ``Authorization: Bearer``.
  * No usable token → verdict ``'not_logged_in'`` and
    ``recommend_disable is False``.
  * codex (stream-only) → ``SKIPPED`` (no non-stream probe) and not disabled.
  * A NORMAL (non-oauth) provider is NOT routed through the oauth branch —
    the sentinel logic must not leak onto key-based providers (complement).

NEUTER: strip the oauth branch → the sentinel key is probed and the cell
comes back 'unauthorized' + recommend_disable (red).
"""

from __future__ import annotations

import pytest

import lib.provider_probe as pp

pytestmark = pytest.mark.unit


class _FakeResp:
    def __init__(self, code, text='{}'):
        self.status_code = code
        self.text = text


def _patch_http(monkeypatch, captured, resp):
    import lib.http_client as hc
    def _post(url, json=None, headers=None, timeout=None, **kw):
        captured['url'] = url
        captured['json'] = json
        captured['headers'] = dict(headers or {})
        return resp
    monkeypatch.setattr(hc, 'http_post', _post, raising=False)
    return _post


def test_oauth_cell_resolves_live_token_and_uses_x_api_key(monkeypatch):
    """The probe resolves the real token and sends it as x-api-key."""
    captured = {}
    calls = {}

    def _resolve(oauth, body, extra_headers):
        calls['oauth'] = oauth
        # resolve_oauth_request returns (token, hdrs, body)
        return 'LIVE-TOKEN-123', {'anthropic-beta': 'claude-code-20250219'}, body

    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request', _resolve)
    monkeypatch.setattr('lib.oauth.outbound.claude_oauth_url', lambda u: u + '?beta=true')
    _patch_http(monkeypatch, captured, _FakeResp(200, '{}'))

    status, detail = pp.probe_one_cell(
        'https://api.anthropic.com/v1', 'oauth-managed', 'claude-opus-4-1',
        {}, 5, protocol='anthropic', oauth='claude')

    assert calls.get('oauth') == 'claude', 'resolve_oauth_request must be called'
    assert 'beta=true' in captured['url'], 'claude_oauth_url must add ?beta=true'
    hdrs = captured['headers']
    assert hdrs.get('x-api-key') == 'LIVE-TOKEN-123', 'token must ride x-api-key'
    assert 'Authorization' not in hdrs, 'subscription token must NOT use Bearer'
    assert hdrs.get('x-api-key') != 'oauth-managed', 'sentinel must never be sent'
    assert status == 'ok', f'200 must classify ok, got {status}: {detail}'


def test_oauth_no_token_maps_to_not_logged_in_never_disable(monkeypatch):
    """No live token → neutral 'not_logged_in', recommend_disable False."""
    def _resolve(oauth, body, extra_headers):
        raise RuntimeError('Claude subscription not logged in')

    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request', _resolve)

    status, detail = pp.probe_one_cell(
        'https://api.anthropic.com/v1', 'oauth-managed', 'claude-opus-4-1',
        {}, 5, protocol='anthropic', oauth='claude')

    assert status == 'not_logged_in', f'expected not_logged_in, got {status}'
    # The disable set must NOT contain the session verdict.
    assert status not in pp._PROBE_DISABLE_STATUSES, \
        'not_logged_in must never recommend disabling a subscription model'


def test_codex_probe_no_token_is_not_logged_in_never_disable(monkeypatch):
    """S4 changed the codex probe from SKIPPED to a REAL /responses probe.
    The sentinel key must still never be sent: the probe resolves the live
    token first, and with NO token the verdict is the NEUTRAL
    'not_logged_in' — never recommend-disable. (Hermetic: token resolution
    is mocked empty so no real network happens.)"""
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token',
                        lambda: None)
    status, detail = pp.probe_one_cell(
        'https://chatgpt.com/backend-api/codex', 'oauth-managed', 'gpt-5-codex',
        {}, 5, protocol='openai', oauth='codex')
    assert status == 'not_logged_in'
    assert status not in pp._PROBE_DISABLE_STATUSES


def test_normal_provider_not_routed_through_oauth(monkeypatch):
    """COMPLEMENT: a key-based provider must NOT call resolve_oauth_request."""
    called = {'n': 0}

    def _resolve(oauth, body, extra_headers):
        called['n'] += 1
        raise AssertionError('normal provider must not touch oauth resolution')

    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request', _resolve)
    captured = {}
    _patch_http(monkeypatch, captured, _FakeResp(200, '{}'))

    status, _ = pp.probe_one_cell(
        'https://gw.example.com/v1', 'real-key-abc', 'kimi-k3',
        {}, 5, protocol='openai', oauth='')

    assert called['n'] == 0, 'oauth resolution must not fire for key providers'
    assert captured['headers'].get('Authorization') == 'Bearer real-key-abc'
    assert status == 'ok'


def test_neuter_no_oauth_branch_sentinel_probed_and_disabled(monkeypatch):
    """NEUTER: without the oauth branch the sentinel key yields a 401 that
    recommends disable — proving the branch is what protects subscription
    models from being falsely flagged."""
    captured = {}
    _patch_http(monkeypatch, captured, _FakeResp(401, '{"error":"invalid api key"}'))

    # oauth='' is exactly what the pre-fix code path did (no branch).
    status, detail = pp.probe_one_cell(
        'https://api.anthropic.com/v1', 'oauth-managed', 'claude-opus-4-1',
        {}, 5, protocol='anthropic', oauth='')

    assert status == 'unauthorized'
    assert status in pp._PROBE_DISABLE_STATUSES, \
        'a 401 on the sentinel key DOES recommend disable — the bug the oauth ' \
        'branch prevents'


def test_probe_cell_multi_forwards_oauth_to_chat_surface(monkeypatch):
    """probe_cell_multi must forward oauth to the default chat fn."""
    seen = {}
    def _fake(base_url, api_key, model_id, extra_headers, timeout, protocol,
              oauth=''):
        seen['oauth'] = oauth
        return 'ok', 'HTTP 200'
    monkeypatch.setattr(pp, 'probe_one_cell', _fake)
    status, _ = pp.probe_cell_multi('https://x/v1', 'k', 'm', {}, 5,
                                    attempts=1, oauth='claude')
    assert seen.get('oauth') == 'claude'
    assert status == 'ok'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
