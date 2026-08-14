"""Regression: the cookie-capture completion toast (cookie_capture_consent.js)
must fire on a push 'captured' frame — and nothing else may render.

WHY
---
The login-wall capture chain (lib/browser/cookie_capture.py) is AUTO-APPROVED
server-side (owner decision 2026-08-13 — the allow/deny banner was removed).
The frontend module's only job left is the completion toast: when a
background capture lands a session, the user must SEE "已保存 <domain> 的登录态，
重试即可抓取" — otherwise a walled fetch silently starts working one retry
later with zero visible explanation. This drives the REAL shipped JS under
node with a fake push/showToast, asserting OUTCOMES (captured → one toast
with the domain; any other frame → silence), not internals.

NEUTER: neuter the 'captured' gate in a COPY of the shipped file → the toast
check FAILS, proving the harness exercises the real frame-handling path.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_FILE = runtime_section_path('cookie_capture_consent.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.console = console;
// i18n: return the key WITH the placeholder so the shipped .replace()
// still fires — assertions can then check both the key and the domain.
global.t = (k) => k + ' {domain}';
const _toasts = [];
global.showToast = (msg, kind) => { _toasts.push({ msg, kind }); };

// ── Fake push: capture the subscriber so frames can be driven by hand ──
let _frameHandler = null;
global.pushSubscribe = (channel, taskId, fn) => {
  if (channel === 'cookie_capture') _frameHandler = fn;
};

global.document = {
  readyState: 'complete',
  addEventListener: () => {},
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL cookie_capture_consent.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Init self-fired on load (readyState='complete').
check('push_subscriber_registered', typeof _frameHandler === 'function');

// (A) A 'captured' frame toasts the i18n key with the domain.
_frameHandler({ type: 'captured', domain: 'your-llm-gateway.example.com', cookieCount: 7 });
check('captured_frame_toasts',
      _toasts.some(tst => tst.msg.includes('cc.captured') &&
                          tst.msg.includes('your-llm-gateway.example.com') &&
                          tst.kind === 'success'));

// (B) Any other frame (incl. the removed banner's 'request') is ignored.
_frameHandler({ type: 'request', id: 'cc_1', domain: 'x.example.com',
                url: 'https://x.example.com/' });
check('request_frame_ignored', _toasts.length === 1);
_frameHandler(null);
_frameHandler({});
_frameHandler('captured');
check('malformed_frames_ignored', _toasts.length === 1);

console.log(out.join('\n'));
"""


def _run_harness(js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_cc_consent_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, js_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_captured_frame_toasts_and_rest_ignored():
    proc = _run_harness(JS_FILE)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'capture-toast behavior failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_captured_frame_wiring_neuter(tmp_path):
    """NEUTER: break the 'captured' gate in a COPY. The toast check must
    FAIL — proving the harness drives the real frame-handling path."""
    with open(JS_FILE, encoding='utf-8') as f:
        src = f.read()
    needle = "if (!frame || frame.type !== 'captured') return;"
    assert needle in src, 'frame-gate fragment drifted — update the neuter target'
    copy = tmp_path / 'cc_consent_neutered.js'
    copy.write_text(src.replace(needle, 'if (true) return;', 1), encoding='utf-8')

    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL captured_frame_toasts' in output, (
        'NEUTER did not bite: toast still fired with the captured gate severed.\n' + output)

    with open(JS_FILE, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped file'
