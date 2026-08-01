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

So every FAILED-send path must (a) clear `_sendInFlight` BEFORE its rescue
sync (otherwise the guard skips the PUT) and (b) mark the turn pending-sync
when that rescue PUT also fails (otherwise a poor-network refresh still loses
the message).

SHAPES THIS GUARD PINS (updated 2026-08-01, verdict: TEST DRIFT not product
bug — pt_ca1b3b2f53874ec8)
--------------------------------------------------------------------------------
There are TWO persisting catch branches, and since the startup-stop refactor
(pt_fa32a235) they carry the rescue pair in TWO different shapes:

  1. user-clicked-stop: `conv._sendInFlight = false;` then
     `await _userStopDuringStartup(conv, convId, { …, rescue: true })` — the
     branch's rescue pair MOVED into the shared helper
     (static/js/ui/send_button.js::_userStopDuringStartup), which awaits the
     sync and calls `markConvPendingSync` on failure when `opts.rescue` is
     set. Semantics preserved; only the inline shape changed. (This is why
     the retired count==2 exact-fragment assertion went red: it counted one
     inline shape and missed the helper shape entirely.)
  2. generic error: `conv._sendInFlight = false;` then the INLINE pair
     `const _synced = await syncConversationToServer(conv);` +
     `if (!_synced) markConvPendingSync(conv);` — unchanged.

CHECKS
------
(1) MECHANISM (drives the REAL shipped `syncConversationToServer`): with
    `_sendInFlight = true` the PUT is SKIPPED; with `_sendInFlight = false`
    the PUT FIRES.
(2) BRANCH SHAPES (source-level): the generic branch's inline pair appears
    exactly once, preceded by the clear; the user-stop branch calls the
    helper with `rescue: true`, preceded by the clear; the helper itself
    carries the gated rescue pair.
(3) HELPER BEHAVIOUR (drives the REAL `_userStopDuringStartup` under node):
    rescue:true + failed sync → pending-sync marked; rescue:true + ok sync →
    not marked; no rescue + failed sync → best-effort (not marked); the
    backend abort-hunt (`Api.chat.abortConv`) always fires; syncOpts pass
    through.
(4) TRIPLE NEUTER: (a) removing the inline pair drops its count to 0;
    (b) gating off the helper's rescue arm makes (3)'s pending check go RED;
    (c) dropping `rescue: true` from the call breaks the wiring assertion.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
SEND_JS = os.path.join(JS_DIR, 'main', 'main_send_pipeline.js')
SEND_BTN_JS = os.path.join(JS_DIR, 'ui', 'send_button.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r'function %s\s*\(' % re.escape(name), src)
    assert m, f'{name} not found in source'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


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

for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));  // bundle-order conv family via _conv_bundle_sources.conv_family_sources

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
    # Eval the WHOLE conv family via the drift-proof closure (see
    # _conv_bundle_sources.conv_family_sources).
    sys.path.insert(0, HERE)
    from _conv_bundle_sources import conv_family_sources
    extra_js = conv_family_sources()
    try:
        proc = subprocess.run(
            ['node', harness, *extra_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


# ── (3) HELPER-BEHAVIOUR harness: drive the REAL _userStopDuringStartup. ──
_HELPER_HARNESS = r"""
'use strict';
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const calls = { sync: [], pending: [], abortConv: [] };

let activeConvId = 'SOMEWHERE_ELSE';
const document = { getElementById: () => null };
const window = globalThis;
function _removeTranslatingBubble() {}
function saveConversations() {}
function buildTurnNav() {}
function markConvPendingSync(conv) { calls.pending.push(conv.id); }
const Api = { chat: { abortConv: (id) => { calls.abortConv.push(id); return Promise.resolve(); } } };
let _syncResult = true;
let _syncThrow = false;
function syncConversationToServer(conv, opts) {
  calls.sync.push({ id: conv.id, opts: opts || null });
  if (_syncThrow) return Promise.reject(new Error('sync blew up'));
  return Promise.resolve(_syncResult);
}

/* The REAL send_button.js (defines updateSendButton + the helper). */
__SEND_BTN_SRC__

(async () => {
  const conv = { id: 'c1', messages: [] };

  /* rescue:true + sync FAILS → the turn MUST be marked pending-sync (the
   * poor-network durability the whole guard exists for). */
  _syncResult = false;
  await _userStopDuringStartup(conv, 'c1', { rescue: true });
  check('rescue_failure_marks_pending', calls.pending.length === 1 && calls.pending[0] === 'c1');
  check('abortConv_hunts_backend_task', calls.abortConv.length === 1 && calls.abortConv[0] === 'c1');

  /* rescue:true + sync OK → nothing to retry. */
  calls.pending.length = 0; _syncResult = true;
  await _userStopDuringStartup(conv, 'c1', { rescue: true });
  check('rescue_success_no_pending', calls.pending.length === 0);

  /* no rescue (regen/edit shape) + sync FAILS → best-effort, no pending
   * mark — and the rejection must NOT escape (the `_syncP.catch(() => {})`
   * arm keeps the rollback path total). */
  calls.pending.length = 0;
  _syncResult = false; _syncThrow = false;
  await _userStopDuringStartup(conv, 'c1', { syncOpts: { allowTruncate: true } });
  check('no_rescue_no_pending', calls.pending.length === 0);
  check('syncopts_passthrough',
        calls.sync[calls.sync.length - 1].opts
        && calls.sync[calls.sync.length - 1].opts.allowTruncate === true);

  /* best-effort arm swallows a THROWING sync (never an unhandled rejection). */
  _syncThrow = true;
  await _userStopDuringStartup(conv, 'c1', {});
  check('besteffort_swallows_sync_throw', true);

  console.log(out.join('\n'));
})().catch(e => { console.log('HARNESS-ERROR ' + (e && e.stack || e)); });
"""


def _run_helper_harness(send_btn_src: str) -> str:
    script = _HELPER_HARNESS.replace('__SEND_BTN_SRC__', send_btn_src)
    proc = subprocess.run(['node', '-e', script], capture_output=True,
                          text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


# The generic-error branch's INLINE durable rescue pair (exact — the
# success-path / startAssistantResponse syncs don't capture into `_synced`).
_SYNC_FRAGMENT = 'const _synced = await syncConversationToServer(conv);\n      if (!_synced) markConvPendingSync(conv);'
# The user-stop branch delegates the SAME semantics to the shared startup-stop
# helper with rescue:true (the shape the retired count==2 assertion missed).
_USERSTOP_CALL = '_userStopDuringStartup(conv, convId, { userMsg, userMsgIdx, rescue: true })'
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


def test_catch_branch_rescue_shapes():
    """Source-level: BOTH persisting catch branches carry the durable rescue
    semantics in their CURRENT shapes, each preceded by the `_sendInFlight`
    clear; the shared helper carries the gated rescue pair."""
    with open(SEND_JS, encoding='utf-8') as f:
        src = f.read()

    # (a) generic-error branch: the inline pair exactly once, preceded by clear.
    count = src.count(_SYNC_FRAGMENT)
    assert count == 1, (
        'regression: the generic-error branch must carry the inline durable '
        f'rescue pair exactly once, found {count}. Without it a failed rescue '
        'PUT leaves the message non-durable → lost on refresh.')
    for m in re.finditer(re.escape(_SYNC_FRAGMENT), src):
        window = src[max(0, m.start() - 500):m.start()]
        assert _CLEAR_TOKEN in window, (
            'regression: the generic-branch rescue sync is NOT preceded by '
            '`conv._sendInFlight = false;` — the guard skips the PUT.')

    # (b) user-stop branch: routes through the helper with rescue:true,
    #     preceded by the clear.
    assert _USERSTOP_CALL in src, (
        'regression: the user-stop catch branch no longer calls '
        '_userStopDuringStartup with rescue:true — a user-stopped send on a '
        'poor network loses the pending-sync retry mark (message lost on '
        'refresh if the rescue PUT also fails).')
    call_at = src.index(_USERSTOP_CALL)
    assert _CLEAR_TOKEN in src[max(0, call_at - 500):call_at], (
        'regression: the user-stop rescue is NOT preceded by '
        '`conv._sendInFlight = false;` — the guard skips the PUT.')

    # (c) the helper itself: gated rescue pair present.
    with open(SEND_BTN_JS, encoding='utf-8') as f:
        btn_src = f.read()
    assert 'if (opts.rescue)' in btn_src, (
        'regression: _userStopDuringStartup lost its opts.rescue gate')
    assert 'const _synced = await _syncP;' in btn_src, (
        'regression: _userStopDuringStartup no longer awaits the rescue sync '
        'in its rescue arm')
    assert 'markConvPendingSync(conv);' in btn_src, (
        'regression: _userStopDuringStartup lost the pending-sync mark — the '
        'user-stop branch has no failed-PUT durability')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_user_stop_helper_rescue_behavior():
    """Behavioural: the REAL _userStopDuringStartup marks pending-sync exactly
    when rescue:true AND the sync fails; swallows best-effort failures; passes
    syncOpts through; always hunts the backend task."""
    output = _run_helper_harness(open(SEND_BTN_JS, encoding='utf-8').read())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'user-stop helper behaviour failures:\n' + output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_rescue_triple_neuter():
    """TRIPLE-NEUTER: proves each pin discriminates.
    (a) inline pair removed → count drops to 0;
    (b) helper rescue arm gated off → the behavioural pending check goes RED;
    (c) rescue:true dropped from the call → the wiring assertion goes RED."""
    with open(SEND_JS, encoding='utf-8') as f:
        src = f.read()
    # (a)
    assert src.count(_SYNC_FRAGMENT) == 1, 'inline fragment drifted — update the neuter target'
    neutered_a = src.replace(_SYNC_FRAGMENT, 'await syncConversationToServer(conv);')
    assert neutered_a.count(_SYNC_FRAGMENT) == 0, (
        'NEUTER-a did not bite: the inline rescue pair survived removal')
    # (c)
    neutered_c = src.replace(_USERSTOP_CALL,
                             '_userStopDuringStartup(conv, convId, { userMsg, userMsgIdx })')
    assert neutered_c != src and _USERSTOP_CALL not in neutered_c, (
        'NEUTER-c did not bite: rescue:true survived removal from the call')
    # (b)
    btn_src = open(SEND_BTN_JS, encoding='utf-8').read()
    neutered_b = btn_src.replace('if (opts.rescue) {', 'if (false && opts.rescue) {')
    assert neutered_b != btn_src, 'NEUTER-b replacement did not land'
    output = _run_helper_harness(neutered_b)
    assert 'FAIL rescue_failure_marks_pending' in output, (
        'NEUTER-b did not bite: pending-sync still marked with the rescue arm '
        'gated off — the behavioural harness does not discriminate:\n' + output)
    # …while the best-effort arm keeps working under the same neuter (anchor):
    assert 'PASS no_rescue_no_pending' in output, output
