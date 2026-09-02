"""tests/test_oauth_claude_loopback.py — Claude loopback callback on desktop.

Claude accepts EITHER callback style on the same client_id:

  * ``https://console.anthropic.com/oauth/code/callback`` — Anthropic renders
    ``code#state`` and the user copy-pastes it back. Works anywhere.
  * ``http://localhost:54545/callback`` — the relay captures the code
    silently. Only resolves when the browser is on the same machine as the
    relay, i.e. the packaged desktop app.

Ground truth for the loopback being accepted at all: CLIProxyAPI drives the
SAME client_id (9d1c250a-e61b-44d9-88ed-5944d1962f5e) with
``RedirectURI = "http://localhost:54545/callback"``
(internal/auth/claude/anthropic_auth.go). So manual paste was never a
protocol requirement — it was the only path we had wired.

What these tests pin, in order of how much they'd cost to get wrong:

  1. The redirect is CONDITIONAL. A remote deployment must keep the console
     callback, or its users get sent to a localhost URL on their OWN machine
     where nothing listens.
  2. Bind-first. The loopback is advertised only if the port is actually
     ours. A busy port degrades to console+paste rather than stranding the
     user at connection-refused holding an unredeemable code.
  3. authorize/exchange redirect_uri agree BYTE-FOR-BYTE. OAuth rejects any
     disagreement with ``invalid_grant``, and the two are now computed at
     different times, so this is the seam most likely to rot.
"""

from __future__ import annotations

import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

import lib.oauth.manager as mgr
from lib.oauth.claude import CLAUDE_OAUTH_CONFIG, claude_build_auth_url

_CONSOLE = CLAUDE_OAUTH_CONFIG['redirect_uri']
_LOOPBACK = CLAUDE_OAUTH_CONFIG['redirect_uri_local']


def _redirect_in(auth_url: str) -> str:
    """The redirect_uri actually advertised in a built authorize URL."""
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(auth_url).query).get('redirect_uri', [''])[0]


class TestAuthUrlRedirectSelection(unittest.TestCase):
    """claude_build_auth_url advertises the redirect it was asked for."""

    def test_default_is_console_callback(self):
        f = claude_build_auth_url()
        self.assertEqual(f['redirect_uri'], _CONSOLE)
        self.assertEqual(_redirect_in(f['auth_url']), _CONSOLE)

    def test_loopback_when_requested(self):
        f = claude_build_auth_url(use_loopback=True)
        self.assertEqual(f['redirect_uri'], _LOOPBACK)
        self.assertEqual(_redirect_in(f['auth_url']), _LOOPBACK)

    def test_browser_exchange_params_track_the_same_redirect(self):
        # The B1 browser-side exchange and the B2 curl helper both read
        # exchange.redirect_uri. If it kept pointing at the console while the
        # authorize used loopback, every fallback path would invalid_grant.
        for loopback, expected in ((False, _CONSOLE), (True, _LOOPBACK)):
            f = claude_build_auth_url(use_loopback=loopback)
            self.assertEqual(f['exchange']['redirect_uri'], expected)
            self.assertEqual(_redirect_in(f['auth_url']), expected)


class TestLoopbackGate(unittest.TestCase):
    """_loopback_callback_ok: desktop-only, with an explicit override."""

    def test_source_run_is_not_loopback_eligible(self):
        from lib.oauth.manager._flow import _loopback_callback_ok
        with mock.patch('sys.frozen', False, create=True), \
             mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('TOFU_OAUTH_LOOPBACK', None)
            self.assertFalse(_loopback_callback_ok())

    def test_frozen_desktop_is_loopback_eligible(self):
        from lib.oauth.manager._flow import _loopback_callback_ok
        import os
        with mock.patch('sys.frozen', True, create=True):
            os.environ.pop('TOFU_OAUTH_LOOPBACK', None)
            self.assertTrue(_loopback_callback_ok())

    def test_env_override_forces_both_directions(self):
        from lib.oauth.manager._flow import _loopback_callback_ok
        with mock.patch('sys.frozen', False, create=True), \
             mock.patch.dict('os.environ', {'TOFU_OAUTH_LOOPBACK': '1'}):
            self.assertTrue(_loopback_callback_ok())
        with mock.patch('sys.frozen', True, create=True), \
             mock.patch.dict('os.environ', {'TOFU_OAUTH_LOOPBACK': '0'}):
            self.assertFalse(_loopback_callback_ok())


class TestStartFlowBindFirst(unittest.TestCase):
    """The bind RESULT — not the desktop gate alone — picks the redirect."""

    def setUp(self):
        mgr._active_flows.pop('claude', None)
        mgr._active_flows.pop('codex', None)

    def test_desktop_with_free_port_uses_loopback_and_runs_relay(self):
        fake_server = mock.MagicMock()
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay',
                        return_value=fake_server) as bind, \
             mock.patch('lib.oauth.manager._flow._run_relay_server') as run, \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('claude')

        bind.assert_called_once()
        self.assertEqual(_redirect_in(res['auth_url']), _LOOPBACK)
        self.assertEqual(mgr._active_flows['claude']['redirect_uri'], _LOOPBACK)
        # The relay thread must actually be started, with the pre-bound server
        # handed over — otherwise nothing is listening on the port we just
        # advertised.
        th.assert_called_once()
        self.assertIs(th.call_args.kwargs['kwargs']['server'], fake_server)
        self.assertIs(th.call_args.kwargs['target'], run)
        # The handler needs the real state to enforce its CSRF check; it was
        # bound before the state existed.
        self.assertEqual(fake_server.RequestHandlerClass.expected_state,
                         mgr._active_flows['claude']['state'])

    def test_busy_port_degrades_to_console_and_starts_no_relay(self):
        """The regression that would strand a user mid-login."""
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay',
                        return_value=None), \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('claude')

        self.assertEqual(_redirect_in(res['auth_url']), _CONSOLE)
        self.assertEqual(mgr._active_flows['claude']['redirect_uri'], _CONSOLE)
        th.assert_not_called()

    def test_non_desktop_never_binds_and_keeps_console(self):
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=False), \
             mock.patch('lib.oauth.manager._flow._bind_relay') as bind, \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('claude')

        bind.assert_not_called()  # no port touched on a shared server
        self.assertEqual(_redirect_in(res['auth_url']), _CONSOLE)
        th.assert_not_called()

    def test_remote_codex_uses_manual_callback_capture_without_binding(self):
        """A server-side listener cannot receive a remote browser's localhost."""
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=False), \
             mock.patch('lib.oauth.manager._flow._bind_relay') as bind, \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('codex')
        bind.assert_not_called()
        th.assert_not_called()
        self.assertEqual(res['redirect_mode'], 'manual')
        self.assertEqual(mgr._active_flows['codex']['redirect_mode'],
                         'manual')

    def test_local_codex_binds_before_returning_authorization_url(self):
        """CLIProxyAPI parity: the callback listener is ready before open."""
        fake_server = mock.MagicMock()
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay',
                        return_value=fake_server) as bind, \
             mock.patch('lib.oauth.manager._flow._run_relay_server') as run, \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('codex')
        bind.assert_called_once_with(
            'codex', 1455, mgr._active_flows['codex']['state'])
        th.assert_called_once()
        self.assertIs(th.call_args.kwargs['kwargs']['server'], fake_server)
        self.assertIs(th.call_args.kwargs['target'], run)
        self.assertEqual(res['redirect_mode'], 'loopback')


class TestExchangeEchoesAuthorizeRedirect(unittest.TestCase):
    """redirect_uri must survive authorize → exchange byte-for-byte."""

    def setUp(self):
        mgr._active_flows.pop('claude', None)

    def _seed(self, redirect_uri):
        mgr._active_flows['claude'] = {
            'pkce': {'code_verifier': 'v'}, 'state': 'st',
            'status': 'started', 'redirect_uri': redirect_uri,
        }

    def test_loopback_flow_exchanges_with_loopback(self):
        self._seed(_LOOPBACK)
        with mock.patch('lib.oauth.claude.claude_exchange_code',
                        return_value={'email': 'u@x.com'}) as ex, \
             mock.patch('lib.oauth.outbound.provision_oauth_provider',
                        return_value=True):
            mgr.exchange_code('claude', 'code', state='st')
        self.assertEqual(ex.call_args.kwargs['redirect_uri'], _LOOPBACK)

    def test_console_flow_exchanges_with_console(self):
        self._seed(_CONSOLE)
        with mock.patch('lib.oauth.claude.claude_exchange_code',
                        return_value={'email': 'u@x.com'}) as ex, \
             mock.patch('lib.oauth.outbound.provision_oauth_provider',
                        return_value=True):
            mgr.exchange_code('claude', 'code', state='st')
        self.assertEqual(ex.call_args.kwargs['redirect_uri'], _CONSOLE)

    def test_legacy_flow_without_redirect_falls_back_to_console(self):
        # A flow dict persisted before this change carries no redirect_uri.
        # It must still exchange, using the console value it was authorized
        # with — not crash and not silently switch to loopback.
        self._seed_legacy = mgr._active_flows['claude'] = {
            'pkce': {'code_verifier': 'v'}, 'state': 'st', 'status': 'started',
        }
        captured = {}

        def _fake(code, verifier, state='', user_id='', redirect_uri=''):
            captured['redirect_uri'] = redirect_uri
            return {'email': 'u@x.com'}

        with mock.patch('lib.oauth.claude.claude_exchange_code', _fake), \
             mock.patch('lib.oauth.outbound.provision_oauth_provider',
                        return_value=True):
            res = mgr.exchange_code('claude', 'code', state='st')
        self.assertTrue(res.get('ok'))
        # Empty → claude_exchange_code applies its console default.
        self.assertEqual(captured['redirect_uri'], '')

    def test_exchange_default_redirect_is_console(self):
        """The default in claude_exchange_code itself, not just the caller."""
        from lib.oauth.claude import claude_exchange_code

        class _R:
            status_code = 200
            text = '{}'

            def json(self):
                return {'access_token': 'sk-ant-oat01-x', 'expires_in': 100}

        with mock.patch('lib.oauth.claude.http_post', return_value=_R()) as post, \
             mock.patch('lib.desktop.egress.route_request', return_value='direct'), \
             mock.patch('lib.oauth.claude.save_token', return_value=True):
            claude_exchange_code('code', 'verifier', state='st')
        self.assertEqual(post.call_args.kwargs['json']['redirect_uri'], _CONSOLE)

    def test_exchange_sends_the_loopback_when_given(self):
        from lib.oauth.claude import claude_exchange_code

        class _R:
            status_code = 200
            text = '{}'

            def json(self):
                return {'access_token': 'sk-ant-oat01-x', 'expires_in': 100}

        with mock.patch('lib.oauth.claude.http_post', return_value=_R()) as post, \
             mock.patch('lib.desktop.egress.route_request', return_value='direct'), \
             mock.patch('lib.oauth.claude.save_token', return_value=True):
            claude_exchange_code('code', 'verifier', state='st',
                                 redirect_uri=_LOOPBACK)
        self.assertEqual(post.call_args.kwargs['json']['redirect_uri'], _LOOPBACK)


class TestPreferConsoleEscapeHatch(unittest.TestCase):
    """The user-facing way OUT of a loopback flow.

    Whether Anthropic honours the loopback redirect for this client is an
    EXTERNAL fact. If it refuses, a desktop user is stranded: the console
    page is what renders the code, so a loopback flow leaves the manual
    paste box with nothing to paste. ``prefer_console`` is the product-level
    escape hatch; ``TOFU_OAUTH_LOOPBACK`` cannot serve that role because a
    packaged .exe user has nowhere to set an environment variable.
    """

    def setUp(self):
        mgr._active_flows.pop('claude', None)

    def test_prefer_console_skips_the_bind_entirely(self):
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay') as bind, \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('claude', prefer_console=True)

        bind.assert_not_called()
        self.assertEqual(_redirect_in(res['auth_url']), _CONSOLE)
        self.assertEqual(mgr._active_flows['claude']['redirect_uri'], _CONSOLE)
        th.assert_not_called()
        self.assertEqual(res['redirect_mode'], 'console')

    def test_default_still_takes_the_loopback_on_desktop(self):
        """Complement: the hatch must not become the new default."""
        fake_server = mock.MagicMock()
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay',
                        return_value=fake_server), \
             mock.patch('threading.Thread'):
            res = mgr.start_oauth_flow('claude')
        self.assertEqual(_redirect_in(res['auth_url']), _LOOPBACK)
        self.assertEqual(res['redirect_mode'], 'loopback')

    def test_redirect_mode_is_reported_on_every_claude_flow(self):
        """The UI describes the flow from this field — it must never be absent."""
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=False), \
             mock.patch('threading.Thread'):
            res = mgr.start_oauth_flow('claude')
        self.assertEqual(res['redirect_mode'], 'console')

    def test_prefer_console_is_inert_for_codex(self):
        """Codex has ONE registered redirect — the flag must not break it."""
        fake_server = mock.MagicMock()
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay',
                        return_value=fake_server), \
             mock.patch('threading.Thread') as th:
            res = mgr.start_oauth_flow('codex', prefer_console=True)
        th.assert_called_once()
        self.assertEqual(res['redirect_mode'], 'loopback')


class TestStatusProjectionFeedsTheReload(unittest.TestCase):
    """get_oauth_status is the ONLY input a reloaded page renders from.

    A mode/auth_url that lives only in the login RESPONSE dies with it: the
    reloaded card re-renders from this projection, so without them it can
    neither restore the truthful instructions + escape hatch nor re-open the
    popup.
    """

    def setUp(self):
        mgr._active_flows.pop('claude', None)

    def test_active_loopback_flow_projects_mode_and_url(self):
        fake_server = mock.MagicMock()
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('lib.oauth.manager._flow._bind_relay',
                        return_value=fake_server), \
             mock.patch('threading.Thread'), \
             mock.patch('lib.oauth.token_store.load_token', return_value=None):
            res = mgr.start_oauth_flow('claude')
            st = mgr.get_oauth_status('claude')
        self.assertEqual(st['status'], 'started')
        self.assertEqual(st['redirect_mode'], 'loopback')
        self.assertEqual(st['auth_url'], res['auth_url'])
        # The browser exchange is the ONLY path when the desktop server is
        # geo-blocked — its params must survive a reload via this projection.
        self.assertEqual(st['exchange']['token_url'], res['exchange']['token_url'])
        self.assertEqual(st['exchange']['code_verifier'],
                         res['exchange']['code_verifier'])

    def test_console_flow_projects_console_mode(self):
        with mock.patch('lib.oauth.manager._flow._loopback_callback_ok',
                        return_value=True), \
             mock.patch('threading.Thread'), \
             mock.patch('lib.oauth.token_store.load_token', return_value=None):
            mgr.start_oauth_flow('claude', prefer_console=True)
            st = mgr.get_oauth_status('claude')
        self.assertEqual(st['redirect_mode'], 'console')
        self.assertTrue(st['auth_url'])

    def test_no_flow_projects_no_mode(self):
        with mock.patch('lib.oauth.token_store.load_token', return_value=None):
            st = mgr.get_oauth_status('claude')
        self.assertIsNone(st['redirect_mode'])
        self.assertIsNone(st['auth_url'])
        self.assertIsNone(st['exchange'])


class TestLoginRouteCarriesTheFlag(unittest.TestCase):
    """The flag has to survive BOTH transports the frontend uses.

    The client falls back to GET when a proxy refuses POST to an unknown
    path — so a flag parsed only out of the JSON body would be silently
    inert on exactly the deployments that need the fallback most.
    """

    def _call(self, **kw):
        import asyncio
        import server  # noqa: F401 — builds the Quart app + blueprints
        app = server.app
        seen = {}

        def _fake_start(provider, prefer_console=False):
            seen['provider'] = provider
            seen['prefer_console'] = prefer_console
            return {'auth_url': 'https://x/', 'status': 'started',
                    'provider': provider, 'callback_port': 1,
                    'redirect_mode': 'console', 'exchange': {}}

        async def _go():
            client = app.test_client()
            with mock.patch('lib.oauth.manager.start_oauth_flow', _fake_start):
                return await client.post('/api/oauth/login', json=kw) \
                    if kw.pop('_post', True) else None

        with mock.patch('lib.oauth.manager.start_oauth_flow', _fake_start):
            asyncio.run(_go())
        return seen

    def test_post_body_flag_reaches_the_flow(self):
        seen = self._call(provider='claude', prefer_console=True)
        self.assertTrue(seen['prefer_console'])

    def test_post_without_flag_defaults_to_auto(self):
        seen = self._call(provider='claude')
        self.assertFalse(seen['prefer_console'])

    def test_get_querystring_flag_reaches_the_flow(self):
        import asyncio
        import server  # noqa: F401
        app = server.app
        seen = {}

        def _fake_start(provider, prefer_console=False):
            seen['prefer_console'] = prefer_console
            return {'auth_url': 'https://x/', 'status': 'started',
                    'provider': provider, 'callback_port': 1,
                    'redirect_mode': 'console', 'exchange': {}}

        async def _go():
            client = app.test_client()
            await client.get('/api/oauth/login?provider=claude&prefer_console=1')

        with mock.patch('lib.oauth.manager.start_oauth_flow', _fake_start):
            asyncio.run(_go())
        self.assertTrue(seen['prefer_console'],
                        'the GET transport is the proxy fallback — the flag '
                        'must not be inert there')


if __name__ == '__main__':
    unittest.main()
