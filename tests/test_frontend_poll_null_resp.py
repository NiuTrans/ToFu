"""tests/test_frontend_poll_null_resp.py — regression for the client-error
log line ``[poll] Cannot read properties of null (reading 'status')``.

WHY
---
``Api.chat.poll`` (static/js/api.js) is declared with ``onError:'null'``, so a
network failure (VS Code tunnel drop / fetch throws) resolves to ``null`` — NOT
a Response. ``_pollFallback`` (static/js/ui/sse_poll_fallback.js) guarded the
success path with ``if (!resp || !resp.ok)`` but then fell through to
``throw new Error(`Poll HTTP ${resp.status}`)`` — dereferencing ``.status`` on
the ``null`` ``resp``. That deref ITSELF throws
``TypeError: Cannot read properties of null (reading 'status')``, which the
catch block then forwarded to the server via ``_reportClientError`` at ERROR
level (routes/common.py ``client_error`` logs non-``[debuglog]`` messages as
ERROR). So a benign tunnel blip surfaced as a scary null-deref in
``logs/error.log`` — masking the real cause (network) and polluting the log.

THE FIX
-------
``throw new Error(resp ? `Poll HTTP ${resp.status}` : 'Poll network error (no response)')``
— when ``resp`` is null, raise a clean network-failure message that feeds the
existing circuit breaker instead of a misleading TypeError.

This harness loads the REAL shipped ``sse_poll_fallback.js`` under bare node,
stubs the window globals it reads, makes ``Api.chat.poll`` return ``null`` once,
captures the message handed to ``_reportClientError`` on the first poll error,
then aborts the stream so the loop exits.

SOURCE-LEVEL NEGATIVE CONTROL (proven by hand; restored byte-identical):
  • Revert the throw to the old ``throw new Error(`Poll HTTP ${resp.status}`)``
    → the captured message becomes the null-deref TypeError
    ``Cannot read properties of null`` and the assertion FAILS.
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
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Window-scope globals _pollFallback reads ──
global.conversations = [];
global.activeConvId = 'conv-net';
global.streamBufs = new Map();
global.twUpdate = () => {};
global.twStop = () => {};
global.finishStream = () => {};
global.saveConversations = () => {};
global.renderChat = () => {};
global.showToast = () => {};
global.debugLog = () => {};
global._startOfflineRecoveryPolling = () => {};
// Health check is irrelevant here (we abort before the circuit breaker), but
// must exist. Never let it flip to "dead" and enter the 2-min wait.
global._checkServerHealth = async () => true;
global._lastHealthCheck = 0;

// Capture what gets reported to the server.
const reported = [];
global._reportClientError = (msg) => { reported.push(String(msg)); };

// The stream + controller. We abort it after the first failed poll so the
// while(true) loop exits deterministically (no real timers needed — the
// aborted-check at the top of the next iteration returns).
const stream = { controller: { signal: { aborted: false } } };

// Api.chat.poll returns null (network failure swallowed by onError:'null').
// After the first call we flip the abort flag so the loop exits next lap.
let pollCalls = 0;
global.Api = { chat: { poll: async () => {
  pollCalls++;
  stream.controller.signal.aborted = true;  // abort AFTER this null result
  return null;
} } };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/sse_poll_fallback.js (real)

if (typeof _pollFallback !== 'function') {
  console.log('FAIL fn_exposed _pollFallback missing'); process.exit(0);
}
check('fn_exposed', true);

(async () => {
  const conv = { id: 'conv-net', activeTaskId: 'task-net-1', messages: [] };
  conversations.push(conv);
  const assistantMsg = { content: '', thinking: '' };
  await _pollFallback('conv-net', 'task-net-1', stream, assistantMsg);

  check('poll_called', pollCalls >= 1);
  check('reported_once', reported.length >= 1);
  const msg = reported[0] || '';
  // The whole point: the reported error must NOT be the null-deref TypeError.
  check('no_null_deref', !/cannot read propert/i.test(msg));
  // It should be the clean network-failure message the fix introduces.
  check('clean_network_msg', /network error/i.test(msg));
  console.log('REPORTED: ' + msg);
  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_poll_null_response_reports_clean_network_error():
    harness = os.path.join(HERE, '_poll_null_resp_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_poll_fallback.js'),  # argv[2]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'poll null-response handling failures:\n' + output
    assert output.count('PASS') >= 5, f'expected >=5 PASS lines, got:\n{output}'
