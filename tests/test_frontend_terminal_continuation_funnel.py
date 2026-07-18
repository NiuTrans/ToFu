"""tests/test_frontend_terminal_continuation_funnel.py — regression for the
UNIFIED terminal-continuation funnel (`_runTerminalContinuation`).

WHY
---
`finishStream` (static/js/ui/stream_lifecycle.js) is the terminal handler that,
after clearing a conversation's running predicate (activeTaskId /
activeStreams), runs the CONTINUATION: attach any autopilot follow-up the
backend pre-spawned, and drain the server-side message queue. But the self-heal
reclaim `_healStuckPlaceholder` (static/js/core/health_stream_timer.js) cleared
the SAME predicate on its empty-ghost branch and `return true`d WITHOUT running
that continuation. So when a self-driving turn's terminal event was swallowed
(proxy drop / reaped task / TTL) and self-heal fired, a server-spawned autopilot
follow-up or queued message stayed invisible until a manual refresh — the
"autonomous flow must self-heal" invariant, violated.

THE FIX extracts the continuation into ONE funnel `_runTerminalContinuation`
and routes BOTH finishStream and the self-heal empty-ghost branch through it.
The funnel is SERVER-AUTHORITATIVE: the inline `_apPendingBaton` is a fast-path
only (present when a `done` event arrived and stamped it); on a swallowed-done
self-heal the baton was NEVER stamped, so the funnel MUST fall through to
`_checkForQueuedTask`, which probes `/api/chat/active` — the authority — and
discovers the follow-up regardless of any inline baton.

This harness slices the REAL logic verbatim out of the two shipped files (bites
the actual code, not a copy) and drives:

  Test A — the self-heal empty-ghost branch INVOKES the continuation funnel.
    NEUTER: remove the `_runTerminalContinuation(convId)` call (the old
    direct-clear-without-funnel) → the continuation is DROPPED. Proves the
    funnel call is load-bearing.

  Test B — the funnel is SERVER-AUTHORITATIVE: with NO inline baton
    (swallowed-done shape) it STILL reaches `_checkForQueuedTask`
    (/api/chat/active). With a baton + attach fn present it fast-paths to
    `_attachAutopilotFollowup` and early-returns. NEUTER: make the funnel
    early-return before the queue-poll on the no-baton path → the
    server-authoritative fall-through FAILS.
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
HEAL = os.path.join(ROOT, 'static', 'js', 'core', 'health_stream_timer.js')
LIFECYCLE = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_lifecycle.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_empty_ghost_block(src_text: str) -> str:
    """Slice the real empty-ghost reclaim block from _healStuckPlaceholder:
    from `if (_isEmptyGhost) {` through the matching `return true;` + `}`."""
    start = src_text.index('  if (_isEmptyGhost) {')
    # The block closes at the first `    return true;\n  }` after the start.
    end_marker = '    return true;\n  }'
    close = src_text.index(end_marker, start)
    return src_text[start:close + len(end_marker)]


def _extract_funnel_body(src_text: str) -> str:
    """Slice the body of `_runTerminalContinuation(convId)` — everything
    between its opening brace and the `}` right before the window export."""
    sig = 'function _runTerminalContinuation(convId) {\n'
    start = src_text.index(sig) + len(sig)
    end = src_text.index(
        "\n}\nif (typeof window !== 'undefined') window._runTerminalContinuation", start)
    return src_text[start:end]


# ── Harness A: does the self-heal empty-ghost branch call the funnel? ──
_HARNESS_A = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const BLOCK = fs.readFileSync(process.argv[2], 'utf8');   // real (or neutered) empty-ghost block

// The block starts at `if (_isEmptyGhost) {`; wrap it in a fn supplying the
// locals it reads. A `return true` inside is LEGAL under new Function.
const runGhostReclaim = new Function(
  'convId', 'probe', '_isEmptyGhost', 'conv', 'taskId',
  'activeStreams', 'activeConvId', 'twStop', 'saveConversations', 'ConvCache',
  'renderChat', 'renderConversationList', '_runTerminalContinuation', 'console',
  BLOCK + '\n  return false;'   // sentinel: block didn't return → not reached
);

function drive(opts) {
  const convId = 'c1';
  const conv = { id: convId, messages: [{ role: 'assistant', content: '' }],
                 activeTaskId: 'task-ghost-1' };
  const activeStreams = new Map();
  let continuationRan = false;
  let continuationConvId = null;
  const _runTerminalContinuation = opts.funnelPresent
    ? function (cid) { continuationRan = true; continuationConvId = cid; }
    : undefined;
  const consoleStub = { warn() {}, info() {}, error() {}, debug() {} };
  const ret = runGhostReclaim(
    convId, { status: 'done' }, true, conv, conv.activeTaskId,
    activeStreams, /*activeConvId*/ 'other', function () {}, function () {},
    { put() {} }, function () {}, function () {},
    _runTerminalContinuation, consoleStub);
  return { returnedTrue: ret === true, continuationRan, continuationConvId,
           predicateCleared: conv.activeTaskId === null };
}

// Case 1: funnel present → predicate cleared AND continuation invoked with convId.
(function () {
  const r = drive({ funnelPresent: true });
  check('ghost_returns_true', r.returnedTrue === true);
  check('ghost_clears_predicate', r.predicateCleared === true);
  check('ghost_runs_continuation', r.continuationRan === true);
  check('ghost_continuation_gets_convId', r.continuationConvId === 'c1');
})();

// Case 2: funnel missing (bundle-timing) → still clears + returns, no throw.
(function () {
  const r = drive({ funnelPresent: false });
  check('ghost_no_funnel_still_clears', r.predicateCleared === true);
  check('ghost_no_funnel_returns_true', r.returnedTrue === true);
})();

console.log(out.join('\n'));
"""


# ── Harness B: is the funnel server-authoritative (no-baton → queue poll)? ──
_HARNESS_B = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const BODY = fs.readFileSync(process.argv[2], 'utf8');   // real (or neutered) funnel body

// setTimeout is stubbed to run synchronously so the queue-poll fires in-test.
const funnel = new Function(
  'convId', 'conversations', '_findAutopilotPendingCarrier', '_attachAutopilotFollowup',
  '_dispatchableQueueCount', 'activeConvId', 'document', 'formatClockTime',
  '_streamingBubbleHTML', 'isNearBottom', 'scrollToBottom', 'setTimeout',
  '_checkForQueuedTask', 'console',
  BODY
);

function drive(opts) {
  const convId = 'c1';
  const conv = { id: convId, messages: [] };
  if (opts.baton) conv._apPendingBaton = { nextTaskId: 'next-1', vuMessage: { content: 'go' } };
  const conversations = [conv];
  const _findAutopilotPendingCarrier = (c) => (
    c && c._apPendingBaton
      ? { msg: { _autopilotPending: c._apPendingBaton }, idx: -1, _convLevel: true }
      : null
  );
  let attachCalled = false, queuePollReached = false;
  const _attachAutopilotFollowup = opts.attachPresent
    ? function () { attachCalled = true; } : undefined;
  const _checkForQueuedTask = function () { queuePollReached = true; };
  const stub = () => {};
  const consoleStub = { warn() {}, info() {}, error() {}, debug() {} };
  funnel(convId, conversations, _findAutopilotPendingCarrier, _attachAutopilotFollowup,
         /*_dispatchableQueueCount*/ () => 0, /*activeConvId*/ 'other',
         /*document*/ { getElementById: () => null }, /*formatClockTime*/ () => '',
         /*_streamingBubbleHTML*/ () => '', /*isNearBottom*/ () => false,
         /*scrollToBottom*/ stub, /*setTimeout*/ (fn) => { fn(); },
         _checkForQueuedTask, consoleStub);
  return { attachCalled, queuePollReached, batonCleared: !conv._apPendingBaton };
}

// Case 1: NO baton (swallowed-done shape) → server-authoritative fall-through
//         to the queue poll (/api/chat/active), no attach.
(function () {
  const r = drive({ baton: false, attachPresent: true });
  check('no_baton_reaches_queue_poll', r.queuePollReached === true);
  check('no_baton_no_attach', r.attachCalled === false);
})();

// Case 2: baton present + attach fn present → fast-path attach, early-return,
//         queue poll NOT reached, baton consumed.
(function () {
  const r = drive({ baton: true, attachPresent: true });
  check('baton_fastpath_attaches', r.attachCalled === true);
  check('baton_fastpath_early_returns', r.queuePollReached === false);
  check('baton_consumed', r.batonCleared === true);
})();

// Case 3: baton present but attach fn MISSING → NO attach, falls through to
//         the server-authoritative queue poll (baton not silently dropped).
(function () {
  const r = drive({ baton: true, attachPresent: false });
  check('baton_missing_fn_falls_through', r.queuePollReached === true);
  check('baton_missing_fn_no_attach', r.attachCalled === false);
})();

console.log(out.join('\n'));
"""


def _run(harness_src: str, block_text: str, tag: str) -> str:
    block_js = os.path.join(HERE, f'_tcf_block_{tag}.js')
    harness = os.path.join(HERE, f'_tcf_harness_{tag}.js')
    with open(block_js, 'w', encoding='utf-8') as f:
        f.write(block_text)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, block_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (block_js, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


# ═══════════════════════════════════════════════════════════════════════════
#  Test A — self-heal empty-ghost branch routes through the funnel
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_self_heal_invokes_continuation_funnel():
    with open(HEAL, encoding='utf-8') as f:
        block = _extract_empty_ghost_block(f.read())
    # Sanity: the real block actually contains the funnel call.
    assert '_runTerminalContinuation(convId)' in block, \
        'the shipped empty-ghost branch must call _runTerminalContinuation'
    output = _run(_HARNESS_A, block, 'a_real')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'self-heal→funnel failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_self_heal_neuter_drops_continuation():
    """Remove the funnel call (the old direct-clear-without-funnel) → the
    continuation must NOT run, proving the call is load-bearing."""
    with open(HEAL, encoding='utf-8') as f:
        block = _extract_empty_ghost_block(f.read())
    neutered = re.sub(
        r"\n\s*if \(typeof _runTerminalContinuation === 'function'\) \{\n"
        r"\s*_runTerminalContinuation\(convId\);\n\s*\}",
        '', block, count=1)
    assert neutered != block, 'neuter did not remove the funnel call'
    out = _run(_HARNESS_A, neutered, 'a_neuter')
    assert 'FAIL ghost_runs_continuation' in out, \
        'NC (funnel call removed) should FAIL the continuation assertion:\n' + out
    # Predicate-clear is unaffected by the neuter (that was always there).
    assert 'PASS ghost_clears_predicate' in out, out


# ═══════════════════════════════════════════════════════════════════════════
#  Test B — the funnel is server-authoritative
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_funnel_is_server_authoritative():
    with open(LIFECYCLE, encoding='utf-8') as f:
        body = _extract_funnel_body(f.read())
    output = _run(_HARNESS_B, body, 'b_real')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'funnel server-authority failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_funnel_neuter_breaks_no_baton_fallthrough():
    """Inject an early `return;` on the no-baton path (before the queue poll)
    → the server-authoritative fall-through must break, proving the queue-poll
    is what makes a swallowed-done self-heal actually recover."""
    with open(LIFECYCLE, encoding='utf-8') as f:
        body = _extract_funnel_body(f.read())
    # NC: return right before the queue-poll section marker.
    marker = '  const _hasQueued = (typeof _dispatchableQueueCount'
    assert marker in body, 'queue-poll marker not found for neuter'
    neutered = body.replace(marker, '  return;\n' + marker, 1)
    out = _run(_HARNESS_B, neutered, 'b_neuter')
    assert 'FAIL no_baton_reaches_queue_poll' in out, \
        'NC (early return before queue poll) should FAIL the no-baton fall-through:\n' + out
    # The baton fast-path still early-returns fine under this neuter.
    assert 'PASS baton_fastpath_attaches' in out, out
