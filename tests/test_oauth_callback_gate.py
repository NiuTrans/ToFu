"""settings/oauth.js callback gate — origin + per-flow state validation.

ROOT CAUSE this guards: the global 'message' listener accepted ANY window's
postMessage carrying ``type: 'oauth_callback'`` and the BroadcastChannel
fallback accepted any same-origin broadcast, so an unrelated page could
inject a forged authorization code/state pair into a victim's login. The
listener now requires (a) a pending flow recorded at login start, (b) the
sender origin to be our loopback relay on the flow's callback port
(postMessage path), and (c) the echoed state to equal the flow's
server-minted nonce. The manual-paste path is separately flagged
``manual: true`` end-to-end so the server can distinguish it from the
automatic relay path.

DB-free; skips when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <input id="oauthClaudeManualUrl">
  <input id="oauthClaudeAuthUrl">
  <span id="oauthClaudeStatus"></span>
  <div id="oauthClaudeInfo"></div>
  <span id="oauthClaudeEmail"></span>
  <button id="oauthClaudeLoginBtn"></button>
  <button id="oauthClaudeLogoutBtn"></button>
  <div id="oauthClaudeManual"></div>
</body>`, { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.runtimeScope = win.runtimeScope = win;
global.localStorage = win.localStorage;
global.navigator = win.navigator;

win.t = global.t = (k) => k;
global.showAlert = win.showAlert = () => {};
global.showConfirm = win.showConfirm = async () => true;
global.escapeHtml = win.escapeHtml = (s) => String(s);
global.errorEnvelopeMessage = win.errorEnvelopeMessage = (e) => String(e || '');

let bcInstance = null;
global.BroadcastChannel = win.BroadcastChannel = class BroadcastChannel {
  constructor(name) { this.name = name; this.onmessage = null; bcInstance = this; }
  postMessage() {}
  close() {}
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // settings/oauth.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  const flush = () => new Promise((r) => setTimeout(r, 20));

  // ── Capture login completions without the network ──
  let completions = [];
  _completeLogin = function (provider, code, state, opts) {
    completions.push({ provider, code, state, opts });
  };
  function dispatch(origin, data) {
    win.dispatchEvent(new win.MessageEvent('message', { data, origin }));
  }
  const RELAY = 'http://localhost:54545';
  const RELAY_IP = 'http://127.0.0.1:54545';

  // (a) no pending flow → even a relay-shaped message is ignored
  dispatch(RELAY, { type: 'oauth_callback', provider: 'claude', code: 'c', state: 'st-1' });
  check('rejected_without_pending_flow', completions.length === 0);

  // (b) wrong origin → rejected even with matching state
  _oauthRecordPendingFlow('claude', 54545, 'st-1');
  dispatch('http://evil.example', { type: 'oauth_callback', provider: 'claude', code: 'c', state: 'st-1' });
  check('rejected_wrong_origin', completions.length === 0);

  // (c) right origin, wrong state → rejected
  dispatch(RELAY, { type: 'oauth_callback', provider: 'claude', code: 'c', state: 'forged' });
  check('rejected_state_mismatch', completions.length === 0);

  // (d) right origin (both spellings) + right state → accepted, not manual
  dispatch(RELAY, { type: 'oauth_callback', provider: 'claude', code: 'c1', state: 'st-1' });
  dispatch(RELAY_IP, { type: 'oauth_callback', provider: 'claude', code: 'c2', state: 'st-1' });
  check('accepted_relay_origin_localhost', completions.length === 2 &&
        completions[0].code === 'c1' && completions[0].opts === undefined);
  check('accepted_relay_origin_127', completions[1] && completions[1].code === 'c2');

  // (e) unrelated message types are ignored entirely
  completions = [];
  dispatch(RELAY, { type: 'other', provider: 'claude' });
  check('ignored_non_oauth_type', completions.length === 0);

  // (f) BroadcastChannel: no origin axis — state nonce is the whole gate
  check('broadcast_channel_armed', !!bcInstance);
  bcInstance.onmessage({ data: { type: 'oauth_callback', provider: 'claude', code: 'c3', state: 'forged' } });
  check('broadcast_rejected_state_mismatch', completions.length === 0);
  bcInstance.onmessage({ data: { type: 'oauth_callback', provider: 'claude', code: 'c3', state: 'st-1' } });
  check('broadcast_accepted_matching_state', completions.length === 1 && completions[0].code === 'c3');

  // (g) clearing the pending flow re-closes the gate
  completions = [];
  _oauthClearPendingFlow('claude');
  dispatch(RELAY, { type: 'oauth_callback', provider: 'claude', code: 'c', state: 'st-1' });
  check('rejected_after_clear', completions.length === 0);

  // (h) manual paste → _completeLogin receives { manual: true }
  document.getElementById('oauthClaudeManualUrl').value = 'code-xyz#st-9';
  _oauthManualSubmit('claude');
  check('manual_submit_parses_code_state', completions.length === 1 &&
        completions[0].code === 'code-xyz' && completions[0].state === 'st-9');
  check('manual_submit_flagged', !!(completions[0].opts && completions[0].opts.manual));

  // (i) _serverExchange forwards manual only when set
  let lastBody = null;
  global.Api = win.Api = { oauth: {
    callbackPost: async (body) => { lastBody = body; return { ok: true, status: 200, json: async () => ({ ok: true }) }; },
    callbackGet: async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }),
    loginPost: async () => ({ ok: true, status: 200, json: async () => ({
      auth_url: '', callback_port: 54545, redirect_mode: 'console',
      exchange: { state: 'st-login' } }) }),
    logoutPost: async () => ({ json: async () => ({ ok: true }) }),
  } };
  await _serverExchange('claude', 'c', 'st-1', true);
  check('server_exchange_marks_manual', lastBody && lastBody.manual === true);
  await _serverExchange('claude', 'c', 'st-1');
  check('server_exchange_default_not_manual', lastBody && !('manual' in lastBody));

  // (j) login records the pending flow (state nonce + port-derived origins)
  _oauthClearPendingFlow('claude');
  _oauthLogin('claude');
  await flush();
  const pending = _oauthPendingFlows.claude || {};
  check('login_records_pending_state', pending.state === 'st-login');
  check('login_records_pending_origins', Array.isArray(pending.origins) &&
        pending.origins.indexOf(RELAY) >= 0 && pending.origins.indexOf(RELAY_IP) >= 0);

  // (k) mid-flow reload: status projection re-arms the gate
  _oauthClearPendingFlow('claude');
  _updateOAuthCard('claude', { status: 'waiting_callback', exchange: { state: 'st-restored' } });
  check('reload_restores_pending', !!_oauthPendingFlows.claude &&
        _oauthPendingFlows.claude.state === 'st-restored' &&
        _oauthPendingFlows.claude.origins.indexOf(RELAY) >= 0);

  // (l) cancel/retry disarms the gate
  _oauthCancelAndRetry('claude');
  await flush();
  check('cancel_clears_pending', !_oauthPendingFlows.claude);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run(section_source: str) -> subprocess.CompletedProcess:
    # Unique per invocation (xdist workers share this directory).
    fd, harness = tempfile.mkstemp(
        prefix='_oauth_gate_harness_', suffix='.js', dir=HERE)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    src_fd, src_path = tempfile.mkstemp(
        prefix='_oauth_section_', suffix='.js', dir=HERE)
    with os.fdopen(src_fd, 'w', encoding='utf-8') as f:
        f.write(section_source)
    try:
        return subprocess.run(
            ['node', harness, src_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        for path in (harness, src_path):
            try:
                os.remove(path)
            except OSError:
                pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_oauth_callback_message_gate():
    from tests._runtime_sections import runtime_section
    proc = _run(runtime_section('settings/oauth.js'))
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'oauth callback gate failures:\n' + out
    assert out.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{out}'
