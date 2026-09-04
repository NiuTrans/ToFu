"""tests/test_oauth_exchange_errors.py — accurate OAuth exchange error surfacing.

Regression guard: a 403 edge/geo block on the SERVER's egress must NOT be
reported to the user as "the code may have expired". The real upstream
status + reason must propagate from claude/codex exchange → manager → route.
"""

from __future__ import annotations

import asyncio
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
        'owner_user_id': 1, 'flow_id': f'test-{provider}',
    }


def _error_message(value):
    return value.get('message', '') if isinstance(value, dict) else str(value)


class TestClaudeExchangeErrors(unittest.TestCase):

    def test_403_is_geo_block_not_expired(self):
        from lib.oauth.claude import claude_exchange_code
        resp = _FakeResp(403, '{"error":{"type":"forbidden","message":"Request not allowed"}}')
        # 路由层打 direct 桩：本组测试钉的是「403 → geo 解释」的解释层，
        # 探测/选 agent 归 tests/test_desktop_egress.py 管。
        with mock.patch('lib.oauth.claude.http_post', return_value=resp), \
             mock.patch('lib.desktop.egress.route_request', return_value='direct'):
            with self.assertRaises(OAuthExchangeError) as ctx:
                claude_exchange_code('code', 'verifier', state='st')
        e = ctx.exception
        self.assertEqual(e.status_code, 403)
        self.assertIn('not an expired code', str(e))
        self.assertIn('Request not allowed', str(e))

    def test_400_is_invalid_grant(self):
        from lib.oauth.claude import claude_exchange_code
        resp = _FakeResp(400, '{"error":"invalid_grant","error_description":"bad code"}')
        with mock.patch('lib.oauth.claude.http_post', return_value=resp), \
             mock.patch('lib.desktop.egress.route_request', return_value='direct'):
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
        with mock.patch('lib.oauth.claude.http_post', return_value=resp), \
             mock.patch('lib.desktop.egress.route_request', return_value='direct'):
            res = mgr.exchange_code(
                'claude', 'code', state='st', owner_user_id=1)
        self.assertEqual(res.get('status_code'), 403)
        self.assertIn('not an expired code', _error_message(res['error']))
        self.assertEqual(res['error']['kind'], 'permission')
        self.assertIn('detail', res)
        # The flow status is marked error with the real reason.
        self.assertEqual(mgr._active_flows['claude']['status'], 'error')

    def test_codex_403_region_block(self):
        _seed_flow('codex')
        resp = _FakeResp(403, '{"error":"unsupported_country_region_territory"}')
        with mock.patch('lib.oauth.codex.http_post', return_value=resp), \
             mock.patch('lib.desktop.egress.route_request', return_value='direct'):
            res = mgr.exchange_code(
                'codex', 'code', state='st', owner_user_id=1)
        self.assertEqual(res.get('status_code'), 403)
        self.assertIn('region block', _error_message(res['error']))
        self.assertEqual(res['error']['kind'], 'permission')


class TestExchangeCsrfStateValidation(unittest.TestCase):
    """CSRF: exchange_code must REJECT a caller-supplied state that does not
    match the flow's recorded state, and must NEVER reach the token exchange."""

    def test_mismatched_state_is_rejected_without_exchanging(self):
        _seed_flow('claude')  # flow state = 'st'
        with mock.patch('lib.oauth.claude.claude_exchange_code') as ex:
            res = mgr.exchange_code(
                'claude', 'code', state='forged-state', owner_user_id=1)
        self.assertIn('error', res)
        self.assertIn('CSRF', _error_message(res['error']))
        ex.assert_not_called()  # the forged pair must never be exchanged
        self.assertEqual(mgr._active_flows['claude']['status'], 'error')

    def test_matching_state_proceeds_to_exchange(self):
        _seed_flow('claude')  # flow state = 'st'
        fake_token = {'email': 'u@x.com'}
        with mock.patch('lib.oauth.claude.claude_exchange_code',
                        return_value=fake_token) as ex, \
             mock.patch('lib.oauth.outbound.provision_oauth_provider',
                        return_value=True):
            res = mgr.exchange_code(
                'claude', 'code', state='st', owner_user_id=1)
        ex.assert_called_once()
        self.assertTrue(res.get('ok'))

    def test_omitted_state_falls_back_to_flow_state(self):
        # The manual copy-paste flow cannot echo state back; an omitted state
        # must still work (falls back to the flow's own state) — but ONLY
        # when the caller explicitly marks the exchange as manual.
        _seed_flow('claude')
        fake_token = {'email': 'u@x.com'}
        with mock.patch('lib.oauth.claude.claude_exchange_code',
                        return_value=fake_token) as ex, \
             mock.patch('lib.oauth.outbound.provision_oauth_provider',
                        return_value=True):
            res = mgr.exchange_code(
                'claude', 'code', owner_user_id=1, manual=True)
        ex.assert_called_once()
        self.assertTrue(res.get('ok'))

    def test_omitted_state_non_manual_is_rejected_without_exchanging(self):
        # The automatic relay path ALWAYS has the state channel — a stateless
        # non-manual callback is an injected message, not a flow completion.
        # The pending flow must NOT be marked error: the legitimate login is
        # unharmed, and an injected stateless request must not DoS it.
        _seed_flow('claude')
        with mock.patch('lib.oauth.claude.claude_exchange_code') as ex:
            res = mgr.exchange_code(
                'claude', 'code', owner_user_id=1)  # no state, not manual
        self.assertIn('error', res)
        self.assertIn('state missing', _error_message(res['error']))
        ex.assert_not_called()
        self.assertEqual(mgr._active_flows['claude']['status'], 'started')

    def test_manual_with_mismatched_state_is_still_rejected(self):
        # manual=True relaxes only the OMITTED state; a pasted URL/code#state
        # that carries the WRONG state is still a CSRF-shaped rejection.
        _seed_flow('claude')
        with mock.patch('lib.oauth.claude.claude_exchange_code') as ex:
            res = mgr.exchange_code('claude', 'code', state='forged-state',
                                    owner_user_id=1, manual=True)
        self.assertIn('error', res)
        self.assertIn('CSRF', _error_message(res['error']))
        ex.assert_not_called()

    def test_wrong_owner_cannot_consume_flow_or_use_its_egress(self):
        _seed_flow('claude')
        with mock.patch('lib.oauth.claude.claude_exchange_code') as exchange:
            res = mgr.exchange_code(
                'claude', 'code', state='st', owner_user_id=2)
        self.assertIn('No active OAuth flow', _error_message(res['error']))
        exchange.assert_not_called()
        self.assertEqual(mgr._active_flows['claude']['status'], 'started')


class TestCallbackRouteManualFlag(unittest.TestCase):
    """routes/oauth.py: the manual flag must reach exchange_code, and a
    pasted callback_url must imply manual + yield its state parameter."""

    def _app(self):
        from quart import Quart
        if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
            Quart.default_config = {**Quart.default_config,
                                    'PROVIDE_AUTOMATIC_OPTIONS': True}
        from routes.oauth import oauth_bp
        app = Quart(__name__)
        app.register_blueprint(oauth_bp)

        @app.before_request
        def _fake_auth():
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()

        return app

    def _capture(self, method, url, json_body=None):
        app = self._app()
        captured = {}

        def _fake(provider, code, state='', *, owner_user_id, manual=False):
            captured.update(provider=provider, code=code, state=state,
                            owner_user_id=owner_user_id, manual=manual)
            return {'ok': True, 'provider': provider}

        async def _t():
            with mock.patch('lib.oauth.manager.exchange_code', _fake):
                client = app.test_client()
                if method == 'POST':
                    resp = await client.post(url, json=json_body)
                else:
                    resp = await client.get(url)
                return resp.status_code

        status = asyncio.run(_t())
        return status, captured

    def test_post_body_manual_flag_forwarded(self):
        status, captured = self._capture(
            'POST', '/api/oauth/callback',
            {'provider': 'claude', 'code': 'c', 'state': 's', 'manual': True})
        self.assertEqual(status, 200)
        self.assertEqual(captured, {'provider': 'claude', 'code': 'c',
                                    'state': 's', 'owner_user_id': 1,
                                    'manual': True})

    def test_post_body_default_is_not_manual(self):
        status, captured = self._capture(
            'POST', '/api/oauth/callback',
            {'provider': 'claude', 'code': 'c', 'state': 's'})
        self.assertEqual(status, 200)
        self.assertIs(captured['manual'], False)

    def test_get_query_manual_flag_forwarded(self):
        status, captured = self._capture(
            'GET', '/api/oauth/callback?provider=claude&code=c&state=s&manual=1')
        self.assertEqual(status, 200)
        self.assertIs(captured['manual'], True)

    def test_callback_url_implies_manual_and_yields_state(self):
        from urllib.parse import quote
        inner = 'http://localhost:1455/auth/callback?code=abc&state=xyz'
        status, captured = self._capture(
            'GET', '/api/oauth/callback?provider=codex&callback_url='
                   + quote(inner, safe=''))
        self.assertEqual(status, 200)
        self.assertEqual(captured, {'provider': 'codex', 'code': 'abc',
                                    'state': 'xyz', 'owner_user_id': 1,
                                    'manual': True})

    def test_mutations_reject_bad_bodies_before_oauth_operations(self):
        """Shared parsing errors stay 400s and never reach OAuth services."""
        from lib.http_error_handlers import register_http_error_handlers

        app = self._app()
        register_http_error_handlers(app)
        operations = (
            'lib.oauth.manager.start_oauth_flow',
            'lib.oauth.manager.exchange_code',
            'lib.oauth.manager.store_token',
            'lib.oauth.manager.logout_oauth',
        )

        async def _t():
            client = app.test_client()
            with mock.patch(operations[0]) as login, \
                 mock.patch(operations[1]) as callback, \
                 mock.patch(operations[2]) as store, \
                 mock.patch(operations[3]) as logout:
                for path in (
                    '/api/oauth/login',
                    '/api/oauth/callback',
                    '/api/oauth/store-token',
                    '/api/oauth/logout',
                ):
                    malformed = await client.post(
                        path,
                        data='{"provider":',
                        headers={'Content-Type': 'application/json'},
                    )
                    self.assertEqual(malformed.status_code, 400, path)
                    malformed_body = json.loads(
                        await malformed.get_data(as_text=True))
                    self.assertFalse(malformed_body['ok'])

                    wrong_type = await client.post(
                        path, json={'provider': ['codex']})
                    self.assertEqual(wrong_type.status_code, 400, path)
                for operation in (login, callback, store, logout):
                    operation.assert_not_called()

        asyncio.run(_t())


class TestBrowserStoreToken(unittest.TestCase):
    """B1 flow: the browser exchanges the code; store_token persists it."""

    def setUp(self):
        _seed_flow('claude')

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
            }, owner_user_id=1)
        self.assertTrue(res['ok'])
        self.assertEqual(res['provider'], 'claude')
        save.assert_called_once()
        prov.assert_called_once_with('claude', owner_user_id=1)

    def test_store_token_rejects_missing_access_token(self):
        res = mgr.store_token(
            'claude', {'refresh_token': 'r'}, owner_user_id=1)
        self.assertIn('error', res)
        self.assertEqual(res['status_code'], 0)

    def test_store_token_unknown_provider(self):
        res = mgr.store_token(
            'bogus', {'access_token': 'x'}, owner_user_id=1)
        self.assertIn('error', res)


if __name__ == '__main__':
    unittest.main()
