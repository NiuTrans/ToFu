"""Poor-network LOST-ACK correctness for the CAS rebase (_rebaseUnackedTail).

THE SCENARIO (the owner's exact fear)
-------------------------------------
On a poor signal the send `fetch` often SUCCEEDS server-side — the server
persists the user turn AND runs the task, storing a real assistant reply and
bumping `rev` — but the RESPONSE is lost. The client concludes the send failed,
appends a local ERROR-ONLY assistant bubble, marks pending-sync, and later
rescue-PUTs → CAS 409 → `_rebaseUnackedTail`. Two abnormal displays must NOT
happen:

  A. DUPLICATE user turn. The client's optimistic user msg and the server's
     persisted copy must be recognised as the same turn. Since the client now
     ships its `_msgId` on the send payload (main_send_pipeline.js) the server
     persists the SAME id; the rebase dedups on `_msgId`, and ALSO defensively
     on (role=user, timestamp) — the backend's own idempotency key.

  B. ERROR bubble coexisting with the real answer. A local error-ONLY assistant
     bubble is DROPPED when the server already has a real trailing assistant
     reply for the turn — the user sees the answer, not "answer + error".

Drives the REAL shipped `_rebaseUnackedTail` under node. Skips without node.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._conv_bundle_sources import JS_DIR, source_argv, sources_defining

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.console = console;
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.Api = { conversations: {} };
global.config = {};
global.activeConvId = null;
global.renderChat = function() {};

/* Eval every shipped file the bundle needs, in production order (resolved by
 * tests/_conv_bundle_sources.py — helpers BEFORE conversations.js). Hard-coding
 * the two paths here broke when the cluster moved in Epic-E slice 3. */
for (let i = 2; i < process.argv.length; i++) {
  eval(fs.readFileSync(process.argv[i], 'utf8'));
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _rebaseUnackedTail !== 'function') {
  console.log('FAIL fn_exposed _rebaseUnackedTail missing'); process.exit(0);
}

// ── Lost-ACK: server has the real user turn (SAME _msgId) + a real reply. ──
// Client (thought it failed) holds: same user turn + a local error-only bubble.
const serverMsgs = [
  { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
  { role: 'user', content: 'hello', timestamp: 2000, _msgId: 'm-user-turn' },
  { role: 'assistant', content: 'real answer from server', timestamp: 2100,
    finishReason: 'stop', _msgId: 'm-assist-real' },
];
const localMsgs = [
  { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
  { role: 'user', content: 'hello', timestamp: 2000, _msgId: 'm-user-turn' },
  { role: 'assistant', content: '', thinking: '', error: 'Request timed out',
    timestamp: 2050, toolRounds: [], _msgId: 'm-err-local' },
];

const merged = _rebaseUnackedTail(serverMsgs, localMsgs);
const ids = merged.map(m => m._msgId);
const users = merged.filter(m => m.role === 'user');
const errs = merged.filter(m => m.role === 'assistant' && m.error && !(m.content||'').trim());

// A. exactly ONE user turn for 'hello' — no duplicate.
check('no_duplicate_user', users.filter(u => u._msgId === 'm-user-turn').length === 1);
// B. the stale local error-only bubble is DROPPED.
check('error_bubble_dropped', !ids.includes('m-err-local') && errs.length === 0);
// the real server reply survives.
check('real_reply_kept', ids.includes('m-assist-real'));

// ── Defensive: same as A but the client user turn has a DIFFERENT _msgId
//    (old bundle mid-rollout) — timestamp fallback must still dedup it. ──
const localMsgsOldId = [
  { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
  { role: 'user', content: 'hello', timestamp: 2000, _msgId: 'DIFFERENT-id' },
];
const merged2 = _rebaseUnackedTail(serverMsgs, localMsgsOldId);
check('ts_fallback_dedup',
      merged2.filter(m => m.role === 'user' && m.timestamp === 2000).length === 1);

// ── Genuine un-acked append (server does NOT have it) still lands. ──
const merged3 = _rebaseUnackedTail(
  [{ role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' }],
  [{ role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
   { role: 'user', content: 'new', timestamp: 5000, _msgId: 'm-new' }]);
check('genuine_append_kept', merged3.some(m => m._msgId === 'm-new'));

// ── A local error bubble is KEPT when the server did NOT answer (real failure:
//    server has NO trailing real assistant) — the user must still see the error. ──
const merged4 = _rebaseUnackedTail(
  [{ role: 'user', content: 'hello', timestamp: 2000, _msgId: 'm-user-turn' }],
  [{ role: 'user', content: 'hello', timestamp: 2000, _msgId: 'm-user-turn' },
   { role: 'assistant', content: '', error: 'network down', timestamp: 2050,
     toolRounds: [], _msgId: 'm-err2' }]);
check('error_kept_when_no_server_reply', merged4.some(m => m._msgId === 'm-err2'));

console.log(out.join('\n'));
"""


def _run(override=None):
    harness = os.path.join(HERE, '_lost_ack_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    paths = source_argv('_rebaseUnackedTail', '_isErrorOnlyAssistant',
                        override=override)
    try:
        return subprocess.run(['node', harness, *paths],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_lost_ack_no_duplicate_no_stale_error():
    proc = _run()
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'lost-ACK failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_error_drop_is_load_bearing(tmp_path):
    """NEUTER: make _isErrorOnlyAssistant always return false → the stale error
    bubble is NO LONGER dropped → error_bubble_dropped FAILS. Proves the drop
    logic is load-bearing (not incidentally passing)."""
    # Epic-E slice 3 (b33d9d21) moved _rebaseUnackedTail + _isErrorOnlyAssistant
    # out of conversations.js — locate the classifier by SYMBOL so a further
    # move re-points automatically instead of leaving the neuter pointed at a
    # file that no longer defines it (a neuter that cannot bite reads as green).
    helpers_js = sources_defining('_isErrorOnlyAssistant')[0]
    with open(helpers_js, encoding='utf-8') as f:
        src = f.read()
    marker = 'function _isErrorOnlyAssistant(m) {'
    assert marker in src, 'neuter target not found'
    neutered = src.replace(marker, marker + '\n  return false;  // NEUTER', 1)
    nfile = tmp_path / 'conv_persist_helpers_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    rel = os.path.relpath(helpers_js, JS_DIR).replace(os.sep, '/')
    proc = _run(override={rel: str(nfile)})
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('error_bubble_dropped') is False, (
        'NEUTER did not bite: error bubble still dropped without the '
        'error-only classifier — drop logic not load-bearing.\n' + output)
    # The real reply must still survive (neuter only affects the error drop).
    assert lines.get('real_reply_kept') is True, output
