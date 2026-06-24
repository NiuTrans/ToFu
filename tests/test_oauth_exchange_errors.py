"""tests/test_oauth_exchange_errors.py — accurate OAuth exchange error surfacing.

Regression guard: a 403 edge/geo block on the SERVER's egress must NOT be
reported to the user as "the code may have expired". The real upstream
status + reason must propagate from claude/codex exchange → manager → route.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.oauth.token_store import OAuthExchangeError
import lib.oauth.manager as mgr


class _FakeResp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text

    def json(self):
        return json.loads(self.text)


def _seed_flow(provider):
    mgr._active_flows[provider] = {
        'pkce': {'code_verifier': 'v'}, 'state': 'st', 'status': 'started',
    }


class TestClaudeExchangeErrors(unittest.TestCase):

    def test_403_is_geo_block_not_expired(self):
        from lib.oauth.claude import claude_exchange_code
        resp = _FakeResp(403, '{"error":{"type":"forbidden","message":"Request not allowed"}}')
        with mock.patch('lib.oauth.claude.http_post', return_value=resp):
            with self.assertRaises(OAuthExchangeError) as ctx:
                claude_exchange_code('code', 'verifier', state='st')
        e = ctx.exception
        self.assertEqual(e.status_code, 403)
        self.assertIn('not an expired code', str(e))
        self.assertIn('Request not allowed', str(e))

    def test_400_is_invalid_grant(self):
        from lib.oauth.claude import claude_exchange_code
        resp = _FakeResp(400, '{"error":"invalid_grant","error_description":"bad code"}')
        with mock.patch('lib.oauth.claude.http_post', return_value=resp):
            with self.assertRaises(OAuthExchangeError) as ctx:
                claude_exchange_code('code', 'verifier', state='st')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('expired or already been used', str(ctx.exception))

    def test_network_error_status_zero(self):
        from lib.oauth.claude import claude_exchange_code
        with mock.patch('lib.oauth.claude.http_post', side_effect=Exception('conn refused')):
            with self.assertRaises(OAuthExchangeError) as ctx:
                claude_exchange_code('code', 'verifier', state='st')
        self.assertEqual(ctx.exception.status_code, 0)


class TestManagerSurfacesRealReason(unittest.TestCase):

    def test_manager_returns_status_and_reason(self):
        _seed_flow('claude')
        resp = _FakeResp(403, '{"error":{"message":"Request not allowed"}}')
        with mock.patch('lib.oauth.claude.http_post', return_value=resp):
            res = mgr.exchange_code('claude', 'code', state='st')
        self.assertEqual(res.get('status_code'), 403)
        self.assertIn('not an expired code', res['error'])
        self.assertIn('detail', res)
        # The flow status is marked error with the real reason.
        self.assertEqual(mgr._active_flows['claude']['status'], 'error')

    def test_codex_403_region_block(self):
        _seed_flow('codex')
        resp = _FakeResp(403, '{"error":"unsupported_country_region_territory"}')
        with mock.patch('lib.oauth.codex.http_post', return_value=resp):
            res = mgr.exchange_code('codex', 'code', state='st')
        self.assertEqual(res.get('status_code'), 403)
        self.assertIn('region block', res['error'])


class TestBrowserStoreToken(unittest.TestCase):
    """B1 flow: the browser exchanges the code; store_token persists it."""

    def test_claude_build_exposes_exchange_params(self):
        from lib.oauth.claude import claude_build_auth_url
        f = claude_build_auth_url()
        ex = f['exchange']
        self.assertTrue(ex['token_url'].endswith('/oauth/token'))
        self.assertTrue(ex['code_verifier'])
        self.assertEqual(ex['style'], 'json')

    def test_codex_build_exposes_form_style(self):
        from lib.oauth.codex import codex_build_auth_url
        ex = codex_build_auth_url()['exchange']
        self.assertEqual(ex['style'], 'form')
        self.assertTrue(ex['code_verifier'])

    def test_store_token_persists_and_provisions(self):
        with mock.patch('lib.oauth.claude.save_token', return_value=True) as save, \
             mock.patch('lib.oauth.outbound.provision_oauth_provider', return_value=True) as prov:
            res = mgr.store_token('claude', {
                'access_token': 'sk-ant-oat01-XYZ', 'refresh_token': 'r', 'expires_in': 28800,
            })
        self.assertTrue(res['ok'])
        self.assertEqual(res['provider'], 'claude')
        save.assert_called_once()
        prov.assert_called_once_with('claude')

    def test_store_token_rejects_missing_access_token(self):
        res = mgr.store_token('claude', {'refresh_token': 'r'})
        self.assertIn('error', res)
        self.assertEqual(res['status_code'], 0)

    def test_store_token_unknown_provider(self):
        res = mgr.store_token('bogus', {'access_token': 'x'})
        self.assertIn('error', res)


if __name__ == '__main__':
    unittest.main()
