"""tests/test_frontend_autopilot_dispatch_fallthrough.py — regression for the
SECOND autopilot silent-drop vector (the dispatch decision, not carrier
resolution).

WHY
---
`test_frontend_autopilot_baton_survives_splice.py` proves the carrier is
RESOLVED after a splice.  But `finishStream` (`static/js/ui/stream_lifecycle.js`)
then has to DISPATCH it, and that half had no test.  The decision block is:

    const _apCarrier = ... ;
    if (_apCarrier) {
      ...consume baton...
      if (typeof _attachAutopilotFollowup === 'function') {
        _attachAutopilotFollowup(convId, _autopilotPending);
        return;                    // ← dispatched, stop
      }
      console.warn(...);           // ← fn missing: DO NOT return
    }
    // ...falls through to the queue-poll self-heal (/api/chat/active)...

The historical bug was a bare `return` in the missing-fn case → a
bundle-timing miss silently dropped the baton with no fallback.  The fix
removes that `return` so control falls through to the queue-poll path, which
self-heals (the backend already spawned the follow-up).  If a future edit
re-adds the `return`, nothing currently fails.

This harness slices the REAL decision block verbatim out of the shipped
`stream_lifecycle.js` (bites the actual logic, not a copy), wraps it in a
minimal function providing the surrounding locals, and drives two states:
  • `_attachAutopilotFollowup` present  → dispatch fires + early-return
    (queue-poll NOT reached).
  • `_attachAutopilotFollowup` undefined → dispatch does NOT fire, control
    FALLS THROUGH to the queue-poll marker (baton not silently dropped).

NEUTER (proven to bite; source restored byte-identical):
  • Re-insert a bare `return;` after the missing-fn `console.warn` → the
    fall-through assertion FAILS (queue-poll no longer reached when the fn
    is absent).  Proves the removed `return` is load-bearing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SRC = runtime_section_path('ui/stream_lifecycle.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_block(src_text: str) -> str:
    """Slice the real dispatch-decision block from stream_lifecycle.js:
    from `const _apCarrier =` through the closing `}` of the `if (_apCarrier)`
    that contains the missing-fn `console.warn`."""
    start = src_text.index('const _apCarrier = conv ? _findAutopilotPendingCarrier(conv) : null;')
    # The block ends at the first line that is exactly '  }' AFTER the
    # console.warn(...) missing-fn message.
    warn_at = src_text.index("[Autopilot] _attachAutopilotFollowup unavailable", start)
    # Find the closing of the console.warn statement, then the next '  }\n'.
    close = src_text.index('\n  }\n', warn_at)
    return src_text[start:close + len('\n  }')]


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const BLOCK = fs.readFileSync(process.argv[2], 'utf8');   // real (or neutered) decision block

// Run the sliced block inside a function that supplies finishStream's locals.
// `queuePollReached` flips true iff control falls through past the block
// (i.e. reaches the code that in finishStream is the queue-poll self-heal).
// Build the finishStream tail ONCE from the real block text.  `new Function`
// (unlike eval) makes a `return` inside the block LEGAL — it returns from this
// wrapper, exactly as it returns from finishStream.  The sentinel assignment
// AFTER the block only runs when the block did NOT return (the queue-poll
// fall-through).  The block's free variables are passed as parameters so it
// binds to our stubs, not globals.
const finishStreamTail = new Function(
  'convId', 'conv', '_findAutopilotPendingCarrier', '_attachAutopilotFollowup',
  'console', 'sentinel',
  BLOCK + '\n  sentinel.queuePollReached = true;'
);

function runDispatch(opts) {
  const convId = 'c1';
  const conv = opts.conv;
  let attachCalled = false;
  const sentinel = { queuePollReached: false };
  const _findAutopilotPendingCarrier = () => (
    conv._apPendingBaton
      ? { msg: { _autopilotPending: conv._apPendingBaton }, idx: -1, _convLevel: true }
      : null
  );
  const _attachAutopilotFollowup = opts.attachPresent
    ? function (_c, _p) { attachCalled = true; }
    : undefined;
  const consoleStub = { warn() {}, info() {}, error() {} };

  finishStreamTail(convId, conv, _findAutopilotPendingCarrier,
                   _attachAutopilotFollowup, consoleStub, sentinel);

  return { attachCalled, queuePollReached: sentinel.queuePollReached,
           batonCleared: !conv._apPendingBaton };
}

// ── Case 1: attach fn PRESENT → dispatch fires, early-return, no queue poll. ──
(function () {
  const r = runDispatch({ conv: { id: 'c1', messages: [], _apPendingBaton: {
    nextTaskId: 'task-next-123', vuMessage: { content: 'go' } } }, attachPresent: true });
  check('present_dispatches', r.attachCalled === true);
  check('present_early_returns', r.queuePollReached === false);
  check('present_clears_baton', r.batonCleared === true);
})();

// ── Case 2: attach fn UNDEFINED → NO dispatch, FALLS THROUGH to queue poll. ──
(function () {
  const r = runDispatch({ conv: { id: 'c2', messages: [], _apPendingBaton: {
    nextTaskId: 'task-next-123', vuMessage: { content: 'go' } } }, attachPresent: false });
  check('missing_no_dispatch', r.attachCalled === false);
  check('missing_falls_through_to_queue_poll', r.queuePollReached === true);
  check('missing_still_clears_baton', r.batonCleared === true);
})();

// ── Case 3: no carrier at all → falls through (normal non-autopilot finish). ──
(function () {
  const r = runDispatch({ conv: { id: 'c3', messages: [] }, attachPresent: true });
  check('no_carrier_falls_through', r.queuePollReached === true);
  check('no_carrier_no_dispatch', r.attachCalled === false);
})();

console.log(out.join('\n'));
"""


def _run(block_text: str) -> str:
    block_js = os.path.join(HERE, '_ap_dispatch_block.js')
    harness = os.path.join(HERE, '_ap_dispatch_harness.js')
    with open(block_js, 'w', encoding='utf-8') as f:
        f.write(block_text)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
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


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_dispatch_decision_positive():
    with open(SRC, encoding='utf-8') as f:
        block = _extract_block(f.read())
    output = _run(block)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'dispatch-decision failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_dispatch_decision_neuter_bites():
    """Re-insert the old bare `return;` in the missing-fn case → the
    fall-through must break, proving the removed return is load-bearing."""
    with open(SRC, encoding='utf-8') as f:
        block = _extract_block(f.read())

    # NC: put a `return;` right after the missing-fn console.warn statement.
    neutered = re.sub(
        r"(\[Autopilot\] _attachAutopilotFollowup unavailable[^\n]*\n"
        r"(?:[^\n]*\n)*?\s*\);)",
        r"\1\n    return;",
        block, count=1,
    )
    assert neutered != block, 'neuter did not modify the block'
    out = _run(neutered)
    assert 'FAIL missing_falls_through_to_queue_poll' in out, \
        'NC (bare return in missing-fn path) should FAIL the fall-through:\n' + out
    # The present-fn path still early-returns fine under the neuter.
    assert 'PASS present_early_returns' in out, \
        'present path should be unaffected by the neuter:\n' + out

    # Sanity: real block passes the fall-through.
    out_real = _run(block)
    assert 'PASS missing_falls_through_to_queue_poll' in out_real, \
        'real block should PASS the fall-through:\n' + out_real
