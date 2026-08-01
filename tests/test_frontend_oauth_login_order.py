"""tests/test_frontend_oauth_login_order.py — login 交换顺序守卫（S2 翻转）。

WHY
---
In the desktop-egress era the SERVER exchange auto-routes through an
egress-capable desktop agent, so it must be tried FIRST (no CORS exposure,
works even when the server's own egress is geo-blocked). The browser
exchange (B1) only makes sense when the server failed with a geo-block /
network error / egress-unavailable — a 400/401 means the code is burned and
retrying it anywhere else just fails again. The pre-S2 order (browser-first)
would keep working but wastes the single-use code on a CORS-fragile path.

This harness evals the REAL static/js/settings/oauth.js in node, overrides
the four exchange seams with order-recording spies, and drives
_completeLogin through four scenarios asserting the exact call SEQUENCE:

  A  server succeeds                 → ['server', 'success']
  B  server 403, browser succeeds    → ['server', 'browser', 'store', 'success']
  C  server 400 (auth rejection)     → ['server', 'error']  (no browser retry)
  D  server 403, browser fails       → ['server', 'browser', 'curl']

NEUTER: restoring the browser-first order breaks the sequence assertions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
OAUTH_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'oauth.js')
NODE = shutil.which('node')

_ENTRY_NEEDLE = "  _serverExchange(provider, code, state)\n    .then(function(data) {"

_HARNESS_JS = r"""
const fs = require('fs');
// oauth.js 的 console.log/warn 会污染 stdout 的结果 JSON —— 全部转 stderr。
console.log = console.warn = console.error = (...a) => process.stderr.write(a.join(' ') + '\n');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
delete globalThis.BroadcastChannel;      // would keep node alive forever
globalThis.t = (k) => k;
globalThis.escapeHtml = (s) => String(s);
globalThis.showAlert = () => {};
globalThis.debugLog = () => {};
globalThis.document = { getElementById: () => null, createElement: () => ({ style: {}, classList: { toggle(){} }, querySelectorAll: () => [] }) };
eval(fs.readFileSync(process.argv[1], 'utf8'));

// ── Order-recording spies over the four seams ──
const seq = [];
const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
globalThis._serverExchange = (p, c, s) => {
  seq.push('server');
  return scenario.serverOk
    ? Promise.resolve({ ok: true, email: 'e@x' })
    : Promise.reject(Object.assign(new Error(scenario.serverErr || 'geo'), { _statusCode: scenario.serverStatus }));
};
globalThis._browserExchange = () => { seq.push('browser');
  return scenario.browserOk ? Promise.resolve({ access_token: 'tok' })
                            : Promise.reject(new Error('cors-or-network')); };
globalThis._storeBrowserToken = () => { seq.push('store');
  return Promise.resolve({ ok: true, email: 'e@x' }); };
globalThis._showCurlHelper = () => { seq.push('curl'); };
globalThis._updateOAuthCard = (p, st) => { if (st && st.status === 'success') seq.push('success');
                                           if (st && st.status === 'error') seq.push('error'); };
globalThis._autoConfigureOAuthProvider = () => {};

(async () => {
  _completeLogin('claude', 'CODE', 'STATE');
  await new Promise(r => setTimeout(r, 50));   // let the promise chain settle
  process.stdout.write(JSON.stringify(seq));
})();
"""


def _drive(scenario: dict, src: str | None = None) -> list[str]:
    if NODE is None:
        pytest.skip('node is required to execute oauth.js')
    path = OAUTH_JS
    tmp = None
    if src is not None:
        import tempfile
        tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                          encoding='utf-8')
        tmp.write(src)
        tmp.close()
        path = tmp.name
    try:
        proc = subprocess.run([NODE, '-e', _HARNESS_JS, path],
                              input=json.dumps(scenario),
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'harness failed: {proc.stderr[:800]}'
        return json.loads(proc.stdout)
    finally:
        if tmp is not None:
            os.unlink(tmp.name)


def test_server_success_short_circuits_browser():
    seq = _drive({'serverOk': True})
    assert seq == ['server', 'success'], seq


def test_geo_block_falls_back_to_browser():
    seq = _drive({'serverOk': False, 'serverStatus': 403, 'browserOk': True})
    assert seq == ['server', 'browser', 'store', 'success'], seq


def test_network_or_egress_unavailable_falls_back_to_browser():
    seq = _drive({'serverOk': False, 'serverStatus': 0, 'browserOk': True})
    assert seq == ['server', 'browser', 'store', 'success'], seq


def test_auth_rejection_never_retries_browser():
    for sc in (400, 401):
        seq = _drive({'serverOk': False, 'serverStatus': sc, 'browserOk': True})
        assert seq == ['server', 'error'], (sc, seq)


def test_both_paths_fail_lands_on_curl_helper():
    seq = _drive({'serverOk': False, 'serverStatus': 403, 'browserOk': False})
    assert seq == ['server', 'browser', 'curl'], seq


def test_NEUTER_browser_first_breaks_sequence():
    """Restore the pre-S2 browser-first order → the server-first pins must go red."""
    src = open(OAUTH_JS, encoding='utf-8').read()
    assert _ENTRY_NEEDLE in src, 'NEUTER anchor missing — test stale'
    neutered = src.replace(_ENTRY_NEEDLE,
                           "  _tryBrowser('neutered-order');\n"
                           "  Promise.resolve().then(function(data) {", 1)
    seq = _drive({'serverOk': True}, src=neutered)
    assert seq != ['server', 'success'], (
        'with the entry call neutered the sequence MUST differ — '
        'proving the real order is load-bearing')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
