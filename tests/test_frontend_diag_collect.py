"""Guard harness for the one-click diagnostics collector.

WHY
---
``static/js/diag_collect.js`` exposes ``window.__tofuCollectDiagnostics()`` — the
JSON blob the Android WebView's "Copy diagnostics" FAB copies to the clipboard.
It is the tool we rely on to root-cause the tablet "stuck on Fetching messages"
bug (issue #1): its live GET probe reports whether the conversation body
actually arrives over the ``/proxy/…/`` tunnel.

This tool is used PRECISELY when the SPA is wedged — so if it silently rots
(dropped from the bundle, or the collector starts returning invalid JSON, or the
tunnel-abort signal breaks), nobody would notice in normal use. This guard locks
three contracts:

  1. ``diag_collect.js`` stays in ``lib/js_bundler.py``'s ``_BUNDLE_FILES`` AND
     loads before ``main.js`` (§3.2.1 — a top-level module dropped from the
     bundle is silently a no-op in production).
  2. ``window.__tofuCollectDiagnostics()`` returns valid JSON carrying the key
     fields (liveGetProbe / activeConv / windowConfig / recentLog / userAgent).
  3. NEUTER: on the tunnel-wedge path (fetch never resolves → 15s abort), the
     probe reports ``aborted === true`` — the decisive signal for the tunnel
     hypothesis. If someone breaks the timeout/abort logic the button would give
     a misleading "success"; this test must catch that.

The JSON-contract + NEUTER checks run the REAL shipped JS under node; they skip
cleanly when node isn't installed. The bundler-ordering check is pure Python and
always runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return shutil.which('node') is not None


# ── Contract 1: bundler registration + ordering (pure Python, always runs) ──

def test_diag_collect_in_bundle_before_main():
    from lib.js_bundler import _BUNDLE_FILES

    assert 'diag_collect.js' in _BUNDLE_FILES, (
        "diag_collect.js dropped from _BUNDLE_FILES — the diagnostics collector "
        "would silently vanish from the production bundle (§3.2.1). Re-add it.")
    assert 'main.js' in _BUNDLE_FILES, 'main.js missing from _BUNDLE_FILES (bundle broken)'
    assert _BUNDLE_FILES.index('diag_collect.js') < _BUNDLE_FILES.index('main.js'), (
        "diag_collect.js must load BEFORE main.js so window.__tofuCollectDiagnostics "
        "is defined by the time the app boots.")


# ── Contracts 2 & 3: run the REAL shipped collector under node ──

_HARNESS = r"""
const fs = require('fs');
const collectorPath = process.argv[2];
const mode = process.argv[3];  // 'healthy' | 'wedged'

// Minimal browser-ish globals the collector reads (all guarded in the source,
// but we supply realistic values so the blob is meaningful).
global.window = global;
global.location = { href: 'https://host/proxy/15000/', pathname: '/proxy/15000/' };
global.navigator = { userAgent: 'Mozilla/5.0 (Linux; Android 10; K) Chrome/150 Safari' };
global.performance = { now: () => Date.now() };
global.document = {
  documentElement: { style: { getPropertyValue: () => '812px' } },
  getElementById: (id) => id === 'chatInner'
    ? { textContent: 'Loading conversation… Fetching 1500 messages from server' } : null,
  querySelector: () => ({ getAttribute: () => 'static/js/bundle-deadbeef.js' }),
};
global.activeConvId = 'mrma0rx6djqayp';
global.conversations = [{
  id: 'mrma0rx6djqayp', _needsLoad: true, messages: [], _serverMsgCount: 1500, _windowed: false,
}];
global.convWindowParam = () => '60';
global.BASE_PATH = '';
global.window.TOFU_CONV_WINDOW = undefined;
global.window.__tofuDiagRing = ['t [warn] SSE failed: network error', 't [warn] Falling back to polling'];

// Real AbortController wired so abort() flips a flag our stub fetch observes.
global.AbortController = class {
  constructor() { this.signal = { __aborted: false }; }
  abort() { this.signal.__aborted = true; }
};

// Speed up the 15s hard timeout so the wedged test finishes fast, without
// touching any other timers.
const realSetTimeout = setTimeout;
global.setTimeout = (fn, ms) => realSetTimeout(fn, ms === 15000 ? 150 : ms);

// The collector routes the live probe through the UNIFIED API client
// (window.Api.conversations.getResponse), NOT a hand-built fetch — so the
// harness stubs that seam. onError:'throw' + timeout:0 are what let the
// collector's own 15s abort remain the sole deadline.
if (mode === 'wedged') {
  // Tunnel that never returns the body → only aborts when the timeout fires.
  global.Api = { conversations: {
    getResponse: (id, opts) => new Promise((resolve, reject) => {
      const sig = opts && opts.signal;
      const iv = setInterval(() => {
        if (sig && sig.__aborted) {
          clearInterval(iv);
          const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
        }
      }, 5);
    }),
  } };
} else {
  global.Api = { conversations: {
    getResponse: () => Promise.resolve({
      status: 200,
      text: () => Promise.resolve(JSON.stringify({
        windowed: true, totalCount: 1500, messages: new Array(60).fill({ role: 'user' }),
      })),
    }),
  } };
}

eval(fs.readFileSync(collectorPath, 'utf8'));

if (typeof window.__tofuCollectDiagnostics !== 'function') {
  console.log('RESULT ' + JSON.stringify({ error: 'collector not exposed' }));
  process.exit(0);
}
Promise.resolve(window.__tofuCollectDiagnostics()).then((s) => {
  // Must be a STRING of valid JSON. Re-stringify a marker so the Python side
  // can assert both that it parsed here AND inspect fields.
  let parsed;
  try { parsed = JSON.parse(s); }
  catch (e) { console.log('RESULT ' + JSON.stringify({ error: 'invalid JSON: ' + e.message })); process.exit(0); }
  console.log('RESULT ' + JSON.stringify({
    ok: true,
    isString: (typeof s === 'string'),
    keys: Object.keys(parsed),
    probe: parsed.liveGetProbe,
    ua: parsed.userAgent,
    recentLogLen: (parsed.recentLog || []).length,
  }));
}, (e) => {
  console.log('RESULT ' + JSON.stringify({ error: 'promise rejected: ' + (e && e.message) }));
});
"""


def _run(mode: str) -> dict:
    import json
    harness = os.path.join(HERE, '_diag_collect_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'diag_collect.js'), mode],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith('RESULT ')), None)
    assert line, f'no RESULT line in harness output:\n{proc.stdout}'
    return json.loads(line[len('RESULT '):])


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_collector_returns_valid_json_with_key_fields():
    res = _run('healthy')
    assert res.get('ok'), f'collector did not produce valid JSON: {res}'
    assert res['isString'], 'collector must return a JSON STRING'
    for field in ('liveGetProbe', 'activeConv', 'windowConfig', 'recentLog', 'userAgent'):
        assert field in res['keys'], f'diagnostics blob missing key field {field!r}: {res["keys"]}'
    # Healthy path: probe reports a real body arrived, windowed.
    probe = res['probe']
    assert probe.get('httpStatus') == 200, f'expected 200 in healthy probe: {probe}'
    assert probe.get('serverSaysWindowed') is True, f'expected windowed body: {probe}'
    assert res['recentLogLen'] >= 2, 'recentLog ring not attached'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NEUTER_tunnel_wedge_probe_reports_aborted():
    """The decisive tunnel-hypothesis signal: when the body never arrives, the
    probe MUST report aborted=true. If the abort/timeout logic breaks, the FAB
    would falsely look 'successful' — this test locks that signal."""
    res = _run('wedged')
    assert res.get('ok'), f'collector did not produce valid JSON on wedge path: {res}'
    probe = res['probe']
    assert probe.get('aborted') is True, (
        "wedged-tunnel probe must report aborted=true (the core issue-#1 tunnel "
        f"signal) — got: {probe}")
    assert probe.get('failed') is True, f'wedged probe should be marked failed: {probe}'


if __name__ == '__main__':
    test_diag_collect_in_bundle_before_main()
    if _node_available():
        test_collector_returns_valid_json_with_key_fields()
        test_NEUTER_tunnel_wedge_probe_reports_aborted()
    print('PASS')
