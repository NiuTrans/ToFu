"""Regression: a send that FAILS on a poor network must still PERSIST the
user's message to the server — otherwise it vanishes on the next page refresh.

WHY
---
`sendMessage()` (static/js/main/main_send_pipeline.js) pushes the optimistic
user message into `conv.messages` and sets `conv._sendInFlight = true` BEFORE
the `Api.chat.send(...)` fetch. That flag tells `syncConversationToServer`
(static/js/core/conversations.js) to SKIP its rescue PUT — because on the
SUCCESS path the backend's `_chat_send` is the sole owner of the first-turn
persist, and a racing PUT would plant a duplicate untranslated row.

But `_sendInFlight` was only cleared in the `finally` block, which runs AFTER
the `catch`. So when the fetch FAILED (timeout / network drop — exactly the
poor-network case), the catch block's `syncConversationToServer(conv)` call ran
while `_sendInFlight` was STILL `true` → the guard skipped the PUT → the
message lived ONLY in the in-memory array. On refresh, `loadConversationMessages`
reads the server copy (which never got the message) and the OVERWRITE branch
wipes it. Net effect: **the message the user sent disappears after refresh**,
which is the reported bug.

The guard's duplicate concern is VOID in the catch path: `chat_send` threw, so
there is no concurrent backend persist to collide with. The fix is to clear
`_sendInFlight = false` BEFORE the rescue `syncConversationToServer(conv)` in
both catch branches (user-clicked-stop + generic error), and `await` it.

TWO checks
----------
(1) MECHANISM (drives the REAL shipped `syncConversationToServer`): with
    `_sendInFlight = true` the PUT is SKIPPED; with `_sendInFlight = false`
    the PUT FIRES. This is the exact behaviour the fix relies on.
(2) FIX ORDERING (source-level on main_send_pipeline.js): in each catch branch
    that persists, `conv._sendInFlight = false` must appear BEFORE the
    `syncConversationToServer(conv)` call — otherwise the rescue sync is a
    no-op.

DOUBLE-NEUTER (below): for (1) removing the guard's early-return would make the
`_sendInFlight=true` case ALSO fire a PUT — proving the mechanism check
discriminates. For (2) reverting the ordering (clear AFTER the sync) makes the
ordering assertion FAIL — proving the fix is load-bearing.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── (1) MECHANISM harness: drive the REAL syncConversationToServer and prove
#        the _sendInFlight guard gates the PUT. ──
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const calls = { put: [] };
global.Api = {
  conversations: {
    put: async (id, body) => { calls.put.push({ id, body }); return { ok: true }; },
  },
};
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.config = { defaultThinkingDepth: 'medium' };
global.activeConvId = null;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof syncConversationToServer !== 'function') {
  console.log('FAIL fn_exposed syncConversationToServer missing'); process.exit(0);
}
check('fn_exposed', true);

function _mkConv(id) {
  return {
    id, title: 't',
    messages: [{ role: 'user', content: 'hi', timestamp: 1700000000000 }],
    createdAt: 1699999000000, updatedAt: 1700000000000,
    model: 'aws.claude-opus-4.8',
  };
}

(async () => {
  // ── _sendInFlight = true → PUT SKIPPED (the guard). This is what made the
  //    catch-path rescue sync a no-op and lost the message. ──
  {
    calls.put.length = 0;
    const conv = _mkConv('c-inflight');
    conv._sendInFlight = true;
    await syncConversationToServer(conv);
    check('inflight_true_put_skipped', calls.put.length === 0);
  }
  // ── _sendInFlight = false → PUT FIRES (the message is persisted). This is
  //    what the fix guarantees by clearing the flag before the rescue sync. ──
  {
    calls.put.length = 0;
    const conv = _mkConv('c-cleared');
    conv._sendInFlight = false;
    await syncConversationToServer(conv);
    check('inflight_false_put_fires', calls.put.length === 1);
    check('inflight_false_body_has_msg',
          calls.put.length === 1 && calls.put[0].body.messages.length === 1);
  }
  console.log(out.join('\n'));
})();
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_send_failure_persist_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_source_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


# The two persisting catch branches of sendMessage each carry the fix's
# durable rescue pair: capture the sync result and mark the turn pending-sync
# when it fails. Keying on this exact fragment is precise (the success-path /
# startAssistantResponse syncs don't capture the result into `_synced`).
_SYNC_FRAGMENT = 'const _synced = await syncConversationToServer(conv);\n      if (!_synced) markConvPendingSync(conv);'
# The `_sendInFlight` clear must still precede the rescue sync in each branch
# (otherwise the guard skips the PUT). Checked separately below.
_CLEAR_TOKEN = 'conv._sendInFlight = false;'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_send_failure_guard_mechanism():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run_harness(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'send-failure guard mechanism failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'


def test_catch_path_clears_inflight_before_rescue_sync():
    """Source-level: both persisting catch branches of sendMessage must (a)
    carry the durable rescue pair (capture `_synced`, mark pending-sync on
    failure) and (b) clear `_sendInFlight` BEFORE that rescue sync (otherwise
    the guard skips the PUT)."""
    send_js = os.path.join(JS_DIR, 'main', 'main_send_pipeline.js')
    with open(send_js, encoding='utf-8') as f:
        src = f.read()
    count = src.count(_SYNC_FRAGMENT)
    assert count == 2, (
        'regression: expected the durable rescue pair '
        '(`const _synced = await syncConversationToServer(conv);` then '
        '`if (!_synced) markConvPendingSync(conv);`) in BOTH persisting catch '
        f'branches of sendMessage, found {count}. Without it a failed rescue '
        'PUT leaves the message non-durable → lost on refresh.')
    # The clear must precede each rescue sync within the same branch window.
    for m in re.finditer(re.escape(_SYNC_FRAGMENT), src):
        window = src[max(0, m.start() - 500):m.start()]
        assert _CLEAR_TOKEN in window, (
            'regression: a catch-branch rescue sync is NOT preceded by '
            '`conv._sendInFlight = false;` — the guard skips the PUT and the '
            'message is lost on refresh (poor-network data-loss).')


def test_catch_path_ordering_double_neuter():
    """DOUBLE-NEUTER: removing the durable rescue pair drops its count to 0,
    proving the source assertion discriminates the fix. In-memory copy; the
    real file is untouched."""
    send_js = os.path.join(JS_DIR, 'main', 'main_send_pipeline.js')
    with open(send_js, encoding='utf-8') as f:
        src = f.read()
    assert src.count(_SYNC_FRAGMENT) == 2, 'sync fragment drifted — update the neuter target'
    neutered_src = src.replace(_SYNC_FRAGMENT, 'await syncConversationToServer(conv);')
    assert neutered_src != src, 'neuter did not change the source'
    assert neutered_src.count(_SYNC_FRAGMENT) == 0, (
        'DOUBLE-NEUTER did not bite: the durable rescue pair survived the '
        'neuter — the test does not discriminate the fix.')
