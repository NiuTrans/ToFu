"""tests/test_frontend_oauth_console_fallback.py — the escape hatch is REACHABLE.

The loopback redirect rests on an EXTERNAL fact we cannot verify from this
repo: whether Anthropic accepts ``http://localhost:54545/callback`` for our
client_id. If it ever refuses, a desktop user is hard-blocked — the console
page is what renders ``code#state``, and a loopback flow never reaches it, so
the manual paste box has nothing to paste. ``_oauthCancelAndRetry`` re-runs
the SAME decision, so retrying loops through the identical broken flow.

``TOFU_OAUTH_LOOPBACK=0`` is NOT an answer for the surface that matters most:
the desktop build ships as a packaged executable and its user has nowhere to
set an environment variable.

So this suite pins the property the backend tests are structurally blind to —
that a USER can get out — by driving the SHIPPED js. The previous batch in
this epic shipped a backend fix that the product path could not reach (the
payload the frontend actually sent lacked the fields the backend read); only
an end-to-end guard catches that class.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_OAUTH_JS = Path(runtime_section_path('settings/oauth.js'))
_API_JS = Path(runtime_section_path('api.js'))
_PANEL = _ROOT / 'static' / 'settings_panels' / 'oauth.html'
_LOCALES = tuple((_ROOT / 'frontend/src/i18n/locales' / f'{lang}.json')
                 for lang in ('zh', 'en'))


def _run_node(script: str) -> dict:
    """Execute a node harness and return its JSON verdict."""
    proc = subprocess.run(['node', '-e', script], capture_output=True,
                          text=True, timeout=60, cwd=str(_ROOT))
    if proc.returncode != 0:
        raise AssertionError('node harness failed:\n%s\n%s'
                             % (proc.stdout[-3000:], proc.stderr[-3000:]))
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith('{')]
    if not tail:
        raise AssertionError('harness printed no verdict:\n%s' % proc.stdout[-3000:])
    return json.loads(tail[-1])


# The harness loads the REAL oauth.js against a minimal DOM, so a rename or a
# dropped argument shows up here rather than in production.
_HARNESS = r'''
const fs = require('fs');
const src = fs.readFileSync(%(oauth_js)s, 'utf8');

const calls = [];
function el(id, extra) {
  const o = Object.assign({ id: id, style: { display: '' }, value: '',
                            onclick: null, textContent: '', disabled: false,
                            className: '',
                            classList: { contains: () => false, add(){}, remove(){}, toggle(){} },
                            querySelectorAll: () => [], appendChild(){} }, extra || {});
  o.classList.contains = (name) => String(o.className || '').split(/\s+/).includes(name);
  return o;
}
const nodes = {};
for (const id of ['oauthClaudeLoginBtn','oauthClaudeLogoutBtn','oauthClaudeStatus',
                  'oauthClaudeInfo','oauthClaudeEmail','oauthClaudeManual',
                  'oauthClaudeAuthUrl','oauthClaudeManualUrl','oauthClaudeEgress',
                  'oauthClaudeCodeHint','oauthClaudePasteRow',
                  'oauthClaudeLoopbackNote','oauthClaudeConsoleFallbackRow',
                  'oauthClaudeConsoleFallbackBtn',
                  'oauthCodexLoginBtn','oauthCodexLogoutBtn','oauthCodexStatus',
                  'oauthCodexInfo','oauthCodexEmail','oauthCodexManual',
                  'oauthCodexAuthUrl','oauthCodexManualUrl','oauthCodexEgress',
                  'oauthCodexDeviceBtn']) nodes[id] = el(id);

global.document = {
  getElementById: (id) => nodes[id] || null,
  createElement: () => el('tmp'),
  addEventListener: () => {},
};
global.window = { addEventListener: () => {}, open: () => {
  global._lastPopup = { closed: false };
  return global._lastPopup;
} };
global.screen = { width: 1200, height: 900 };
global.setInterval = (f) => { global._lastInterval = f; return 1; };
global.clearInterval = () => {};
global.setTimeout = (f) => { try { f(); } catch (e) {} return 0; };
global.BroadcastChannel = function () { this.onmessage = null; };
global.t = (k) => k;
global.fetch = (url, opts) => {
  calls.push({ verb: 'fetch', url: String(url),
               body: opts && opts.body ? String(opts.body) : '' });
  const payload = JSON.stringify({ access_token: 'sk-browser-x', expires_in: 3600 });
  return Promise.resolve({ ok: true, status: 200,
    text: () => Promise.resolve(payload),
    json: () => Promise.resolve(JSON.parse(payload)) });
};
global.alert = (m) => { calls.push({ verb: 'alert', msg: String(m) }); };
global.escapeHtml = (s) => String(s);
global.showAlert = (m) => { calls.push({ verb: 'showAlert', msg: String(m) }); };
global.showConfirm = async () => true;
global.debugLog = () => {};
global._loadServerConfig = () => {};
global._safeClipboardWrite = () => Promise.resolve();

const LOGIN_RESPONSE = %(login_response)s;
const STATUS_RESPONSE = %(status_response)s;
const CALLBACK_RESPONSE = %(callback_response)s;
global.Api = {
  oauth: {
    loginPost: (provider, preferConsole) => {
      calls.push({ verb: 'loginPost', provider, preferConsole: !!preferConsole });
      return Promise.resolve({ ok: true, status: 200,
                               json: () => Promise.resolve(LOGIN_RESPONSE) });
    },
    loginGet: (provider, preferConsole) => {
      calls.push({ verb: 'loginGet', provider, preferConsole: !!preferConsole });
      return Promise.resolve({ ok: true, status: 200,
                               json: () => Promise.resolve(LOGIN_RESPONSE) });
    },
    logoutPost: (provider) => {
      calls.push({ verb: 'logoutPost', provider });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    },
    status: () => Promise.resolve(STATUS_RESPONSE),
    callbackPost: (body) => {
      calls.push({ verb: 'callbackPost', provider: body && body.provider });
      return Promise.resolve(CALLBACK_RESPONSE);
    },
    storeToken: (provider, token) => {
      calls.push({ verb: 'storeToken', provider });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    },
    deviceLoginPost: (provider) => {
      calls.push({ verb: 'deviceLoginPost', provider });
      return Promise.resolve(global._deviceResponse);
    },
    deviceLoginGet: (provider) => {
      calls.push({ verb: 'deviceLoginGet', provider });
      return Promise.resolve(global._deviceResponse);
    },
    egressAgentGet: () => Promise.resolve({}),
  },
};

eval(src);

(async () => {
  %(body)s
})().catch(e => { console.log(JSON.stringify({ _error: String(e && e.stack || e) })); });
'''


def _harness(body: str, login_response: dict, status_response=None,
             callback_response=None) -> dict:
    return _run_node(_HARNESS % {
        'oauth_js': json.dumps(str(_OAUTH_JS)),
        'login_response': json.dumps(login_response),
        'status_response': json.dumps(status_response),
        'callback_response': json.dumps(callback_response),
        'body': body,
    })


_LOOPBACK_RESP = {'auth_url': 'https://claude.ai/oauth/authorize?x=1',
                  'status': 'started', 'provider': 'claude',
                  'callback_port': 54545, 'redirect_mode': 'loopback',
                  'exchange': {}}
_CONSOLE_RESP = {'auth_url': 'https://claude.ai/oauth/authorize?x=2',
                 'status': 'started', 'provider': 'claude',
                 'callback_port': 54545, 'redirect_mode': 'console',
                 'exchange': {}}
_CODEX_MANUAL_RESP = {
    'auth_url': 'https://auth.openai.com/oauth/authorize?x=3',
    'status': 'started', 'provider': 'codex',
    'callback_port': 1455, 'redirect_mode': 'manual',
    'exchange': {},
}


class TestCodexRemoteCallbackCapture(unittest.TestCase):

    def test_remote_flow_shows_callback_paste_box_immediately(self):
        v = _harness(
            """
            document.getElementById('oauthCodexManual').style.display = 'none';
            await _oauthLogin('codex');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              manual: document.getElementById('oauthCodexManual').style.display,
              authUrl: document.getElementById('oauthCodexAuthUrl').value,
            }));
            """, _CODEX_MANUAL_RESP)
        self.assertNotEqual(v['manual'], 'none')
        self.assertEqual(v['authUrl'], _CODEX_MANUAL_RESP['auth_url'])

    def test_complete_localhost_callback_url_preserves_code_and_state(self):
        v = _harness(
            """
            const captured = [];
            _completeLogin = (provider, code, state) =>
              captured.push({ provider, code, state });
            document.getElementById('oauthCodexManualUrl').value =
              'http://localhost:1455/auth/callback?code=ac_test&state=st_test';
            _oauthManualSubmit('codex');
            console.log(JSON.stringify({ captured }));
            """, _CODEX_MANUAL_RESP)
        self.assertEqual(v['captured'], [{
            'provider': 'codex', 'code': 'ac_test', 'state': 'st_test',
        }])

    def test_relay_messages_forward_state_into_csrf_validation(self):
        src = _OAUTH_JS.read_text(encoding='utf-8')
        self.assertGreaterEqual(
            src.count('_handleOAuthCode(data.provider, data.code, data.state)'),
            2)

    def test_device_transport_outage_switches_to_browser_login(self):
        v = _harness(
            """
            global._deviceResponse = {
              ok: false, status: 503,
              text: () => Promise.resolve(JSON.stringify({
                error: 'egress unavailable', status_code: 0,
              })),
            };
            const fallbacks = [];
            _oauthLogin = (provider) => fallbacks.push(provider);
            _oauthDeviceLogin('codex');
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls, fallbacks }));
            """, _CODEX_MANUAL_RESP)
        self.assertEqual(v['fallbacks'], ['codex'])
        alerts = [call for call in v['calls']
                  if call['verb'] == 'showAlert']
        self.assertEqual(len(alerts), 1)
        self.assertIn('settings.oauthDeviceFallback', alerts[0]['msg'])


class TestEscapeHatchIsReachable(unittest.TestCase):
    """A user stuck in a loopback flow can reach the console flow."""

    def test_loopback_recovery_appears_only_after_the_popup_closes(self):
        v = _harness(
            """
            document.getElementById('oauthClaudeManual').style.display = 'none';
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            const initialManual = document.getElementById('oauthClaudeManual').style.display;
            global._lastPopup.closed = true;
            global._lastInterval();
            console.log(JSON.stringify({
              initialManual: initialManual,
              recoveredManual: document.getElementById('oauthClaudeManual').style.display,
              fallbackVisible: document.getElementById('oauthClaudeConsoleFallbackRow').style.display !== 'none',
              handlerWired: typeof document.getElementById('oauthClaudeConsoleFallbackBtn').onclick === 'function',
              noteVisible: document.getElementById('oauthClaudeLoopbackNote').style.display !== 'none',
            }));
            """, _LOOPBACK_RESP)
        self.assertEqual(v['initialManual'], 'none',
                         'the happy path must not pre-assign recovery work')
        self.assertNotEqual(v['recoveredManual'], 'none',
                            'closing an unfinished popup must reveal recovery')
        self.assertTrue(v['fallbackVisible'])
        self.assertTrue(v['handlerWired'],
                        'the revealed control must not be a dead button')
        self.assertTrue(v['noteVisible'])

    def test_clicking_it_restarts_the_flow_with_prefer_console(self):
        """The decisive end-to-end property: the request carries the flag."""
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            global._lastPopup.closed = true;
            global._lastInterval();
            document.getElementById('oauthClaudeConsoleFallbackBtn').onclick();
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP)
        logins = [c for c in v['calls'] if c['verb'].startswith('login')]
        self.assertEqual(len(logins), 2, 'clicking must start a FRESH flow')
        self.assertFalse(logins[0]['preferConsole'])
        self.assertTrue(logins[1]['preferConsole'],
                        'the retry must pin the console callback, else it loops '
                        'straight back into the same broken flow')

    def test_fallback_helpers_are_top_level_not_nested(self):
        """They are invoked from the card — a nested decl is unreachable."""
        v = _harness(
            """
            console.log(JSON.stringify({
              apply: typeof _oauthApplyRedirectMode,
              fallback: typeof _oauthUseConsoleFallback,
            }));
            """, _LOOPBACK_RESP)
        self.assertEqual(v['apply'], 'function')
        self.assertEqual(v['fallback'], 'function')


class TestFlowIsDescribedTruthfully(unittest.TestCase):
    """The paste instructions are FALSE during a loopback flow."""

    def test_loopback_hides_the_paste_instructions(self):
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              hint: document.getElementById('oauthClaudeCodeHint').style.display,
              row: document.getElementById('oauthClaudePasteRow').style.display,
            }));
            """, _LOOPBACK_RESP)
        self.assertEqual(v['hint'], 'none',
                         'a loopback flow never renders a code to copy')
        self.assertEqual(v['row'], 'none')

    def test_console_flow_keeps_the_paste_box_and_hides_the_hatch(self):
        """Complement — the fix must not delete the manual flow it falls back to."""
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              hint: document.getElementById('oauthClaudeCodeHint').style.display,
              row: document.getElementById('oauthClaudePasteRow').style.display,
              hatch: document.getElementById('oauthClaudeConsoleFallbackRow').style.display,
              note: document.getElementById('oauthClaudeLoopbackNote').style.display,
            }));
            """, _CONSOLE_RESP)
        self.assertNotEqual(v['hint'], 'none')
        self.assertNotEqual(v['row'], 'none')
        self.assertEqual(v['hatch'], 'none',
                         'offering "switch to manual" while already manual is noise')
        self.assertEqual(v['note'], 'none')

    def test_plain_login_does_not_request_console(self):
        v = _harness(
            """
            await _oauthLogin('claude');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP)
        logins = [c for c in v['calls'] if c['verb'].startswith('login')]
        self.assertEqual(len(logins), 1)
        self.assertFalse(logins[0]['preferConsole'],
                         'the default must stay the automatic decision')


class TestReloadRestoresTheEscapeHatch(unittest.TestCase):
    """A RELOADED page renders from _loadOAuthStatus, never from _oauthLogin.

    The escape hatch shipped in the previous batch was wired only in the
    login click handler — so the single most natural action of a stuck user
    (reload the page) silently removed it again: the card re-rendered from
    /api/v1/oauth/status showed only cancel-and-retry, which re-runs the
    same callback decision that just failed.
    """

    _WAIT_LOOPBACK = {'claude': {'status': 'waiting_callback',
                                 'redirect_mode': 'loopback',
                                 'auth_url': 'https://claude.ai/oauth/authorize?x=1',
                                 'authenticated': False, 'egress': None},
                      'codex': {'status': 'not_started', 'authenticated': False}}
    _WAIT_CONSOLE = {'claude': {'status': 'waiting_callback',
                                'redirect_mode': 'console',
                                'auth_url': 'https://claude.ai/oauth/authorize?x=2',
                                'authenticated': False, 'egress': None},
                     'codex': {'status': 'not_started', 'authenticated': False}}

    def test_reload_with_waiting_loopback_restores_box_and_hatch(self):
        v = _harness(
            """
            // The real card markup starts hidden — reproduce that.
            document.getElementById('oauthClaudeManual').style.display = 'none';
            await _loadOAuthStatus();
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              manual: document.getElementById('oauthClaudeManual').style.display,
              authUrl: document.getElementById('oauthClaudeAuthUrl').value,
              hatch: document.getElementById('oauthClaudeConsoleFallbackRow').style.display,
              wired: typeof document.getElementById('oauthClaudeConsoleFallbackBtn').onclick === 'function',
              note: document.getElementById('oauthClaudeLoopbackNote').style.display,
              paste: document.getElementById('oauthClaudePasteRow').style.display,
            }));
            """, _LOOPBACK_RESP, self._WAIT_LOOPBACK)
        self.assertNotEqual(v['manual'], 'none',
                            'the reloaded card must re-offer the manual box')
        self.assertEqual(v['authUrl'], 'https://claude.ai/oauth/authorize?x=1',
                         'the reloaded card must be able to re-open the popup')
        self.assertNotEqual(v['hatch'], 'none',
                            'the escape hatch must survive a reload')
        self.assertTrue(v['wired'])
        self.assertNotEqual(v['note'], 'none')
        self.assertEqual(v['paste'], 'none')

    def test_reload_with_waiting_console_keeps_paste_hides_hatch(self):
        v = _harness(
            """
            document.getElementById('oauthClaudeManual').style.display = 'none';
            await _loadOAuthStatus();
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({
              manual: document.getElementById('oauthClaudeManual').style.display,
              hatch: document.getElementById('oauthClaudeConsoleFallbackRow').style.display,
              paste: document.getElementById('oauthClaudePasteRow').style.display,
            }));
            """, _CONSOLE_RESP, self._WAIT_CONSOLE)
        self.assertNotEqual(v['manual'], 'none')
        self.assertNotEqual(v['paste'], 'none')
        self.assertEqual(v['hatch'], 'none')

    def test_synthetic_waiting_state_without_mode_leaves_box_alone(self):
        """The curl helper drives a synthetic waiting_callback with NO mode —
        the restore path must not clobber its manual box."""
        v = _harness(
            """
            document.getElementById('oauthClaudeManual').style.display = 'none';
            _updateOAuthCard('claude', { status: 'waiting_callback' });
            console.log(JSON.stringify({
              manual: document.getElementById('oauthClaudeManual').style.display,
            }));
            """, _LOOPBACK_RESP)
        self.assertEqual(v['manual'], 'none')

    def test_hatch_click_after_reload_still_carries_the_flag(self):
        """The whole point: reload, click the hatch, flag on the wire."""
        v = _harness(
            """
            document.getElementById('oauthClaudeManual').style.display = 'none';
            await _loadOAuthStatus();
            await new Promise(r => setImmediate(r));
            document.getElementById('oauthClaudeConsoleFallbackBtn').onclick();
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP, self._WAIT_LOOPBACK)
        logins = [c for c in v['calls'] if c['verb'].startswith('login')]
        self.assertEqual(len(logins), 1)
        self.assertTrue(logins[0]['preferConsole'])


class TestReloadRestoresExchangeParams(unittest.TestCase):
    """The exchange params are login-response state too — the reload kills them.

    On the desktop build the SERVER is the user's machine, so a geo-blocked
    server exchange leaves the BROWSER exchange as the only path. After a
    reload the frontend's ``_oauthExchangeParams`` is empty: the browser
    exchange rejects with no-exchange-params and the curl helper cannot even
    build its command — an unburned code with no path to redemption.
    """

    _EXCHANGE = {'token_url': 'https://token.test/oauth/token',
                 'client_id': 'cid',
                 'redirect_uri': 'http://localhost:54545/callback',
                 'code_verifier': 'VERIFIER_FROM_STATUS',
                 'state': 'st', 'style': 'json'}
    _WAIT_WITH_EXCHANGE = {'claude': {'status': 'waiting_callback',
                                      'redirect_mode': 'loopback',
                                      'auth_url': 'https://claude.ai/oauth/authorize?x=1',
                                      'authenticated': False, 'egress': None},
                           'codex': {'status': 'not_started', 'authenticated': False}}

    def _waiting(self, with_exchange=True):
        import copy
        st = copy.deepcopy(self._WAIT_WITH_EXCHANGE)
        if with_exchange:
            st['claude']['exchange'] = dict(self._EXCHANGE)
        return st

    # The geo-block response needs a callable text(); inject it via a small
    # prelude rather than json.
    _PRELUDE = """
    const GEO = { ok: false, status: 403,
                  text: () => Promise.resolve('access_denied: region blocked') };
    Api.oauth.callbackPost = (body) => {
      calls.push({ verb: 'callbackPost', provider: body && body.provider });
      return Promise.resolve(GEO);
    };
    """

    def test_reload_then_403_still_reaches_the_browser_exchange(self):
        """No _oauthLogin ran in this 'session' — the status projection is
        the ONLY source of exchange params."""
        st = self._waiting(with_exchange=True)
        v = _harness(
            self._PRELUDE + """
            document.getElementById('oauthClaudeManual').style.display = 'none';
            await _loadOAuthStatus();
            await new Promise(r => setImmediate(r));
            await _completeLogin('claude', 'THECODE', '');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP, st)
        fetches = [c for c in v['calls'] if c['verb'] == 'fetch']
        self.assertEqual(len(fetches), 1,
                         'the browser exchange must fire after a reload — '
                         'params restored from the status projection')
        self.assertEqual(fetches[0]['url'], 'https://token.test/oauth/token')
        self.assertIn('VERIFIER_FROM_STATUS', fetches[0]['body'],
                      'the verifier must come from the PROJECTION, not a '
                      'prior login click that never happened')
        self.assertTrue(any(c['verb'] == 'storeToken' for c in v['calls']))
        self.assertFalse(any(c['verb'] == 'showAlert' for c in v['calls']),
                         'no dead-end error dialog may appear')

    def test_without_projection_the_dead_end_returns(self):
        """Control: strip exchange from the projection and the OLD failure
        comes back — this proves the positive test can tell the two apart."""
        st = self._waiting(with_exchange=False)
        v = _harness(
            self._PRELUDE + """
            await _loadOAuthStatus();
            await new Promise(r => setImmediate(r));
            await _completeLogin('claude', 'THECODE', '');
            await new Promise(r => setImmediate(r));
            console.log(JSON.stringify({ calls: calls }));
            """, _LOOPBACK_RESP, st)
        self.assertEqual([c for c in v['calls'] if c['verb'] == 'fetch'], [],
                         'no params → the browser exchange cannot fire')
        self.assertTrue(any(c['verb'] == 'showAlert' for c in v['calls']),
                        'the old dead end surfaces as an error dialog')


class TestWiringRatchet(unittest.TestCase):
    """Static pins for the parts the DOM harness cannot observe."""

    def test_api_layer_forwards_the_flag_on_both_transports(self):
        src = _API_JS.read_text(encoding='utf-8')
        m = re.search(r'loginPost:.*?loginGet:.*?\}\),', src, re.S)
        self.assertIsNotNone(m, 'oauth login helpers not found in api.js')
        block = m.group(0)
        self.assertIn('prefer_console: true', block,
                      'POST body must carry the flag')
        self.assertIn("prefer_console: '1'", block,
                      'GET query must carry it too — proxies force the GET '
                      'fallback on exactly the deployments that need it')

    def test_card_markup_has_the_ids_the_renderer_toggles(self):
        html = _PANEL.read_text(encoding='utf-8')
        for node_id in ('oauthClaudeCodeHint', 'oauthClaudePasteRow',
                        'oauthClaudeLoopbackNote',
                        'oauthClaudeConsoleFallbackRow',
                        'oauthClaudeConsoleFallbackBtn'):
            self.assertIn('id="%s"' % node_id, html,
                          'renderer toggles #%s but the card never defines it'
                          % node_id)

    def test_new_strings_are_translated(self):
        locales = [json.loads(path.read_text(encoding='utf-8'))
                   for path in _LOCALES]
        for key in ('settings.oauthLoopbackNote', 'settings.oauthUseConsole'):
            self.assertTrue(all(locale.get(key) for locale in locales),
                            'missing i18n key %s' % key)


if __name__ == '__main__':
    unittest.main()
