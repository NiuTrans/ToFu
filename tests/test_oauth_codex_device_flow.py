#!/usr/bin/env python3
"""tests/test_oauth_codex_device_flow.py — Codex device-authorization flow.

Regression guard for the remote-deployment login fix: the loopback callback
(localhost:1455) can never work when the browser and the Tofu server are on
different machines, so the deviceauth API (user code + server-side polling)
is the permanent path. Pins:

  * authorize-URL parity params (prompt=login, id_token_add_organizations,
    codex_cli_simplified_flow — CLIProxyAPI openai_auth.go),
  * usercode request parsing (user_code / usercode, interval str|int),
  * poll pending (403/404) vs success vs real errors,
  * exchange echoing the DEVICE redirect_uri (not the localhost one),
  * manager flow lifecycle: start → poll thread → success finalization,
    cancel via stop/logout, status projection, per-flow 15-min expiry,
  * the /api/v1/oauth/device-login route envelope.

All HTTP is faked at the lib.oauth.codex egress seam — no network.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

import lib.oauth.manager as mgr
from lib.oauth.token_store import OAuthExchangeError


class _FakeResp:
    def __init__(self, status, payload=None, text=''):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (
            json.dumps(payload) if payload is not None else '')

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def _reset_flows():
    with mgr._flows_lock:
        for flow in mgr._active_flows.values():
            ev = flow.get('cancel_event')
            if ev is not None:
                ev.set()
        mgr._active_flows.clear()


def _wait_status(provider, targets, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with mgr._flows_lock:
            st = mgr._active_flows.get(provider, {}).get('status')
        if st in targets:
            return st
        time.sleep(0.02)
    with mgr._flows_lock:
        return mgr._active_flows.get(provider, {}).get('status')


class TestAuthUrlParity(unittest.TestCase):
    def tearDown(self):
        _reset_flows()

    def test_parity_params_present(self):
        from lib.oauth.codex import codex_build_auth_url
        url = codex_build_auth_url()['auth_url']
        self.assertIn('prompt=login', url)
        self.assertIn('id_token_add_organizations=true', url)
        self.assertIn('codex_cli_simplified_flow=true', url)


class TestUserCodeRequest(unittest.TestCase):
    def tearDown(self):
        _reset_flows()

    def test_parses_alt_key_and_string_interval(self):
        from lib.oauth.codex import codex_device_request_user_code
        resp = _FakeResp(200, {'device_auth_id': 'daid_1',
                               'usercode': 'ABCD-EFGH', 'interval': '3'})
        with mock.patch('lib.oauth.codex._oauth_http_post_json',
                        return_value=resp):
            out = codex_device_request_user_code()
        self.assertEqual(out['device_auth_id'], 'daid_1')
        self.assertEqual(out['user_code'], 'ABCD-EFGH')
        self.assertEqual(out['interval'], 3)

    def test_missing_fields_raise(self):
        from lib.oauth.codex import codex_device_request_user_code
        resp = _FakeResp(200, {'device_auth_id': 'daid_1'})
        with mock.patch('lib.oauth.codex._oauth_http_post_json',
                        return_value=resp):
            with self.assertRaises(OAuthExchangeError):
                codex_device_request_user_code()

    def test_http_error_raises_with_status(self):
        from lib.oauth.codex import codex_device_request_user_code
        resp = _FakeResp(403, {'error': 'unsupported_country_region_territory'})
        with mock.patch('lib.oauth.codex._oauth_http_post_json',
                        return_value=resp):
            with self.assertRaises(OAuthExchangeError) as ctx:
                codex_device_request_user_code()
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn('region block', str(ctx.exception))


class TestDevicePoll(unittest.TestCase):
    def tearDown(self):
        _reset_flows()

    def test_pending_on_403_404(self):
        from lib.oauth.codex import codex_device_poll_token
        for status in (403, 404):
            with mock.patch('lib.oauth.codex._oauth_http_post_json',
                            return_value=_FakeResp(status, {'error': 'pending'})):
                self.assertIsNone(codex_device_poll_token('d', 'u'))

    def test_success_returns_code_and_verifier(self):
        from lib.oauth.codex import codex_device_poll_token
        resp = _FakeResp(200, {'authorization_code': 'ac_x',
                               'code_verifier': 'cv_x',
                               'code_challenge': 'cc_x'})
        with mock.patch('lib.oauth.codex._oauth_http_post_json',
                        return_value=resp):
            out = codex_device_poll_token('d', 'u')
        self.assertEqual(out['authorization_code'], 'ac_x')
        self.assertEqual(out['code_verifier'], 'cv_x')

    def test_real_error_raises(self):
        from lib.oauth.codex import codex_device_poll_token
        with mock.patch('lib.oauth.codex._oauth_http_post_json',
                        return_value=_FakeResp(500, {'error': 'boom'})):
            with self.assertRaises(OAuthExchangeError) as ctx:
                codex_device_poll_token('d', 'u')
        self.assertEqual(ctx.exception.status_code, 500)


class TestExchangeRedirectOverride(unittest.TestCase):
    def tearDown(self):
        _reset_flows()

    def test_device_redirect_is_echoed(self):
        from lib.oauth import codex as codex_mod
        captured = {}

        def _fake_post(url, payload, **kw):
            captured.update(payload)
            return _FakeResp(200, {'access_token': 'at', 'refresh_token': 'rt',
                                   'id_token': '', 'expires_in': 3600})

        with mock.patch.object(codex_mod, '_oauth_http_post', _fake_post), \
             mock.patch.object(codex_mod, 'save_token', return_value=True):
            token = codex_mod.codex_exchange_code(
                'code', 'verifier',
                redirect_uri=codex_mod.CODEX_OAUTH_CONFIG['device_redirect_uri'])
        self.assertIsNotNone(token)
        self.assertEqual(
            captured['redirect_uri'],
            'https://auth.openai.com/deviceauth/callback')

    def test_default_redirect_unchanged(self):
        from lib.oauth import codex as codex_mod
        captured = {}

        def _fake_post(url, payload, **kw):
            captured.update(payload)
            return _FakeResp(200, {'access_token': 'at', 'id_token': ''})

        with mock.patch.object(codex_mod, '_oauth_http_post', _fake_post), \
             mock.patch.object(codex_mod, 'save_token', return_value=True):
            codex_mod.codex_exchange_code('code', 'verifier')
        self.assertEqual(captured['redirect_uri'],
                         'http://localhost:1455/auth/callback')


class TestManagerDeviceFlow(unittest.TestCase):
    def tearDown(self):
        _reset_flows()

    def test_happy_path_completes_and_provisions(self):
        fake_token = {'email': 'u@x.com', 'access_token': 'at'}
        with mock.patch('lib.oauth.codex.codex_device_request_user_code',
                        return_value={'device_auth_id': 'd', 'user_code': 'CODE-1',
                                      'interval': 1}), \
             mock.patch('lib.oauth.codex.codex_device_poll_token',
                        return_value={'authorization_code': 'ac',
                                      'code_verifier': 'cv',
                                      'code_challenge': 'cc'}), \
             mock.patch('lib.oauth.codex.codex_exchange_code',
                        return_value=fake_token) as ex, \
             mock.patch('lib.oauth.outbound.provision_oauth_provider',
                        return_value=True) as prov, \
             mock.patch('lib.oauth.codex_catalog.trigger_codex_catalog_refresh'):
            out = mgr.start_device_flow('codex', owner_user_id=1)
            self.assertEqual(out['user_code'], 'CODE-1')
            self.assertEqual(out['verification_url'],
                             'https://auth.openai.com/codex/device')
            st = _wait_status('codex', {'success'})
            self.assertEqual(st, 'success')
            ex.assert_called_once()
            # The exchange MUST echo the deviceauth redirect, never localhost.
            self.assertEqual(
                ex.call_args.kwargs.get('redirect_uri'),
                'https://auth.openai.com/deviceauth/callback')
            self.assertEqual(ex.call_args.kwargs.get('user_id'), '1')
            # Flow status flips to 'success' BEFORE provisioning runs in the
            # poll thread — wait for the call itself, not just the status.
            deadline = time.time() + 5
            while not prov.called and time.time() < deadline:
                time.sleep(0.02)
            prov.assert_called_once_with('codex', owner_user_id=1)

        status = mgr.get_oauth_status('codex')
        self.assertEqual(status['status'], 'success')
        self.assertEqual(status['email'], 'u@x.com')

    def test_status_projection_exposes_device_info(self):
        with mock.patch('lib.oauth.codex.codex_device_request_user_code',
                        return_value={'device_auth_id': 'd', 'user_code': 'CODE-9',
                                      'interval': 30}), \
             mock.patch('lib.oauth.codex.codex_device_poll_token',
                        return_value=None):
            mgr.start_device_flow('codex', owner_user_id=1)
            status = mgr.get_oauth_status('codex')
        self.assertEqual(status['device']['user_code'], 'CODE-9')
        self.assertEqual(
            status['device']['verification_url'],
            'https://auth.openai.com/codex/device')
        self.assertNotIn('device_auth_id', status['device'])
        self.assertEqual(status['redirect_mode'], 'device')

    def test_stop_cancels_poll_thread(self):
        with mock.patch('lib.oauth.codex.codex_device_request_user_code',
                        return_value={'device_auth_id': 'd', 'user_code': 'C',
                                      'interval': 30}), \
             mock.patch('lib.oauth.codex.codex_device_poll_token',
                        return_value=None):
            mgr.start_device_flow('codex', owner_user_id=1)
            mgr.stop_device_flow('codex')
            # Poll thread sleeps on cancel.wait — setting the event wakes it
            # immediately; the flow must NOT auto-complete afterwards.
            time.sleep(0.2)
            with mgr._flows_lock:
                st = mgr._active_flows['codex']['status']
        self.assertIn(st, ('started', 'waiting_callback'))

    def test_device_flow_survives_loopback_expiry_window(self):
        # A device flow waiting past the 300s loopback timeout must NOT be
        # auto-expired — its own expires_at (15 min) governs.
        with mgr._flows_lock:
            mgr._active_flows['codex'] = {
                'status': 'waiting_callback', 'flow_type': 'device',
                'started_at': time.time() - 400,
                'expires_at': time.time() + 500,
                'device': {'user_code': 'C',
                           'verification_url': 'https://auth.openai.com/codex/device'},
            }
        with mock.patch('lib.oauth.token_store.load_token', return_value=None), \
             mock.patch('lib.oauth.outbound.managed_oauth_provider_status',
                        return_value={}):
            status = mgr.get_oauth_status('codex')
        self.assertEqual(status['status'], 'waiting_callback')
        self.assertEqual(status['device']['user_code'], 'C')

    def test_unknown_provider_rejected(self):
        out = mgr.start_device_flow('claude', owner_user_id=1)
        self.assertIn('error', out)

    def test_new_loopback_flow_stops_device_flow(self):
        with mock.patch('lib.oauth.codex.codex_device_request_user_code',
                        return_value={'device_auth_id': 'd', 'user_code': 'C',
                                      'interval': 30}), \
             mock.patch('lib.oauth.codex.codex_device_poll_token',
                        return_value=None):
            mgr.start_device_flow('codex', owner_user_id=1)
            with mgr._flows_lock:
                ev = mgr._active_flows['codex']['cancel_event']
        with mock.patch('lib.oauth.codex.codex_build_auth_url',
                        return_value={'auth_url': 'https://x', 'state': 's',
                                      'pkce': {'code_verifier': 'v'},
                                      'callback_port': 1455, 'exchange': {}}), \
             mock.patch('lib.oauth.manager._relay._run_relay_server'):
            import lib.oauth.manager._flow as flow_mod
            with mock.patch.object(flow_mod.threading, 'Thread') as th:
                th.return_value.start = lambda: None
                mgr.start_oauth_flow('codex', owner_user_id=1)
        self.assertTrue(ev.is_set())

    def test_replaced_device_flow_cannot_redeem_late_poll_result(self):
        from lib.oauth.manager._device import _device_poll_loop

        with mgr._flows_lock:
            mgr._active_flows['codex'] = {
                'flow_id': 'new-generation',
                'status': 'started',
                'owner_user_id': 1,
            }
        late_result = {
            'authorization_code': 'late-code',
            'code_verifier': 'late-verifier',
        }
        with mock.patch('lib.oauth.codex.codex_device_poll_token',
                        return_value=late_result), \
             mock.patch('lib.oauth.codex.codex_exchange_code') as exchange:
            _device_poll_loop(
                'codex', 'old-device', 'OLD-CODE', 1,
                threading.Event(), 'old-generation', user_id='1')
        exchange.assert_not_called()
        with mgr._flows_lock:
            self.assertEqual(
                mgr._active_flows['codex']['status'], 'started')


class TestDeviceLoginRoute(unittest.TestCase):
    def tearDown(self):
        _reset_flows()

    def test_route_envelope(self):
        import asyncio
        import quart as _quart
        sys.modules.setdefault('flask', _quart)
        from quart import Quart
        if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
            Quart.default_config = {**Quart.default_config,
                                    'PROVIDE_AUTOMATIC_OPTIONS': True}
        app = Quart(__name__)
        from routes.api_v1.oauth import api_v1_oauth_bp
        app.register_blueprint(api_v1_oauth_bp)

        # The real app attaches g.auth_ctx in a before_request hook the bare
        # test app lacks — synthesize the same local-admin context here.
        @app.before_request
        def _fake_auth():
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()

        fake = {'status': 'started', 'provider': 'codex',
                'user_code': 'CODE-7',
                'verification_url': 'https://auth.openai.com/codex/device',
                'interval': 5, 'expires_in': 900}

        async def _run():
            with mock.patch('lib.oauth.manager.start_device_flow',
                            return_value=fake) as start:
                client = app.test_client()
                resp = await client.post('/api/v1/oauth/device-login',
                                         json={'provider': 'codex'})
                self.assertEqual(resp.status_code, 200)
                body = json.loads(await resp.get_data(as_text=True))
                self.assertTrue(body['ok'])
                self.assertEqual(body['user_code'], 'CODE-7')
                # GET query variant (proxy fallback parity)
                resp2 = await client.get(
                    '/api/v1/oauth/device-login?provider=codex')
                self.assertEqual(resp2.status_code, 200)
                self.assertEqual(start.call_count, 2)
                self.assertEqual(
                    start.call_args.kwargs['owner_user_id'], 1)

        asyncio.run(_run())

    def test_route_rejects_unknown_provider(self):
        import asyncio
        import quart as _quart
        sys.modules.setdefault('flask', _quart)
        from quart import Quart
        if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
            Quart.default_config = {**Quart.default_config,
                                    'PROVIDE_AUTOMATIC_OPTIONS': True}
        app = Quart(__name__)
        from routes.api_v1.oauth import api_v1_oauth_bp
        app.register_blueprint(api_v1_oauth_bp)

        @app.before_request
        def _fake_auth():
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()

        async def _run():
            client = app.test_client()
            resp = await client.post('/api/v1/oauth/device-login',
                                     json={'provider': 'claude'})
            self.assertEqual(resp.status_code, 400)

        asyncio.run(_run())

    def test_route_rejects_malformed_body_before_start(self):
        import asyncio
        import quart as _quart
        sys.modules.setdefault('flask', _quart)
        from quart import Quart
        if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
            Quart.default_config = {**Quart.default_config,
                                    'PROVIDE_AUTOMATIC_OPTIONS': True}
        app = Quart(__name__)
        from lib.http_error_handlers import register_http_error_handlers
        from routes.api_v1.oauth import api_v1_oauth_bp
        app.register_blueprint(api_v1_oauth_bp)
        register_http_error_handlers(app)

        @app.before_request
        def _fake_auth():
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()

        async def _run():
            with mock.patch('lib.oauth.manager.start_device_flow') as start:
                client = app.test_client()
                malformed = await client.post(
                    '/api/v1/oauth/device-login',
                    data='{"provider":',
                    headers={'Content-Type': 'application/json'},
                )
                self.assertEqual(malformed.status_code, 400)
                wrong_type = await client.post(
                    '/api/v1/oauth/device-login',
                    json={'provider': ['codex']},
                )
                self.assertEqual(wrong_type.status_code, 400)
                start.assert_not_called()

        asyncio.run(_run())

    def test_transport_outage_is_503_for_browser_fallback(self):
        import asyncio
        import quart as _quart
        sys.modules.setdefault('flask', _quart)
        from quart import Quart
        if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
            Quart.default_config = {**Quart.default_config,
                                    'PROVIDE_AUTOMATIC_OPTIONS': True}
        app = Quart(__name__)
        from routes.api_v1.oauth import api_v1_oauth_bp
        app.register_blueprint(api_v1_oauth_bp)

        @app.before_request
        def _fake_auth():
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()

        async def _run():
            failure = {
                'error': 'subscription egress temporarily unreachable',
                'status_code': 0,
                'detail': '',
            }
            with mock.patch('lib.oauth.manager.start_device_flow',
                            return_value=failure):
                client = app.test_client()
                resp = await client.post('/api/v1/oauth/device-login',
                                         json={'provider': 'codex'})
            self.assertEqual(resp.status_code, 503)
            body = json.loads(await resp.get_data(as_text=True))
            self.assertEqual(body['status_code'], 0)

        asyncio.run(_run())


if __name__ == '__main__':
    unittest.main()
