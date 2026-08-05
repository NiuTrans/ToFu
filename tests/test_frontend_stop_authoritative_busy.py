"""tests/test_frontend_stop_authoritative_busy.py — regression for the
"Stop-shaped composer button whose click is a total no-op" dead-click hole.

WHY
---
``updateSendButton``'s busy predicate is the UNION in ``convIsBusy`` — a conv
is busy when a local stream exists, OR ``conv.activeTaskId`` is pinned, OR the
server-authoritative ``conv._authoritativeActiveTaskIds`` set is non-empty
(sibling device generating, or this tab's frame stream lost its local handle
while the server still runs the task).

But the Stop click cascade (static/js/ui/send_button.js) only had handlers
for the first two: Priority 3 aborted the local stream, else fired
``Api.chat.abortTask(conv.activeTaskId)``. When busy came ONLY from the
authoritative set, the handler body fell through BOTH branches — a complete
no-op: no abort request ever left the browser, not even a log line. The
user saw a red Stop button that swallowed every click — the same defect
class as the "连接中… window dead-click" fixed in pt_fa32a2351b3840ad,
and the second half of the "暂停按钮要点多次才生效" report (the first
half — the server never broadcasting the idle projection on user-Stop —
was fixed in 92055d60, which also made duplicate aborts re-broadcast the
idle frame, so firing a per-tid abort here doubles as a corrective nudge
for a client that missed the first frame).

THE FIX
-------
A new tail branch: when no local handle exists but
``conv._authoritativeActiveTaskIds`` names live server tasks, fire
``Api.chat.abortTask(tid)`` for each (idempotent server-side) + a greppable
console.info. When NOTHING is busy by any predicate (state flipped between
render and click), reconcile the stale Stop shape immediately via
``updateSendButton()`` instead of leaving it lit.

This harness loads the REAL shipped ``ui/send_button.js`` (eval'd verbatim —
any drift in the shipped logic is picked up), stubs the window globals, and
drives four scenarios.

SOURCE-LEVEL NEGATIVE CONTROL (proven by hand; restored byte-identical):
  • Delete the new ``else if (conv && conv._authoritativeActiveTaskIds …)``
    branch → scenario A's ``abort_fired_for_authoritative_tid`` /
    ``abort_fired_for_every_authoritative_tid`` checks FAIL (the click
    returns to a silent no-op) while scenarios C/D/E stay green.
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

// ── Fake DOM: a single #sendBtn whose shape + onclick we can inspect/drive ──
const btn = { innerHTML: '', className: '', onclick: null };
global.document = { getElementById: (id) => (id === 'sendBtn' ? btn : null) };

// ── Window-scope globals updateSendButton reads ──
global.pendingMessageQueue = new Map();
global.activeStreams = new Map();
global._branchStreams = new Map();
global._activeBranch = null;
global.activeConvId = 'conv-x';
global.conversations = [];
let _activeConv = null;
global.getActiveConv = () => _activeConv;
/* Mirror the REAL union predicate (computeConvBusy): local stream OR
 * activeTaskId OR the server-authoritative set. The shipped convIsBusy
 * delegates there; the badge harness stubs it the same way. */
global.convIsBusy = (c) => !!c && (
  activeStreams.has(c.id) || !!c.activeTaskId ||
  !!(c._authoritativeActiveTaskIds && c._authoritativeActiveTaskIds.size > 0));
global.sendMessage = () => { out.push('FAIL sendMessage_called_during_stop'); };
global.renderConversationList = () => {};
global._branchKey = (c, m, b) => c + ':' + m + ':' + b;
global._finishBranchStream = () => {};
global.twStop = () => {};
let _finishStreamCalls = [];
global.finishStream = (cid) => { _finishStreamCalls.push(cid); };
// Spy: every abort request the cascade sends to the server.
let _abortCalls = [];
global.Api = { chat: { abortTask: (tid) => { _abortCalls.push(tid); } } };

// argv[2] = real ui/send_button.js
eval(fs.readFileSync(process.argv[2], 'utf8'));
check('updateSendButton_exposed', typeof updateSendButton === 'function');

function reset(btnShapeOnly) {
  _abortCalls = [];
  _finishStreamCalls = [];
  activeStreams.clear();
  _branchStreams.clear();
  global._activeBranch = null;
  pendingMessageQueue.clear();
  if (!btnShapeOnly) { btn.innerHTML = ''; btn.className = ''; btn.onclick = null; }
}
function isStopShape() { return btn.className.indexOf('stop-btn') !== -1; }

// ══ Scenario A (THE HOLE): busy ONLY via the authoritative set — no local
//    stream, no activeTaskId (sibling device generating on this conv). ══
reset();
const convA = {
  id: 'conv-x', activeTaskId: null,
  _authoritativeActiveTaskIds: new Set(['task-auth-1', 'task-auth-2']),
  messages: [{ role: 'assistant', content: 'partial' }],
};
_activeConv = convA;
conversations.length = 0; conversations.push(convA);
updateSendButton();
check('A_button_stop_shaped', isStopShape() && typeof btn.onclick === 'function');
btn.onclick();
check('A_abort_fired_for_authoritative_tid',
      _abortCalls.indexOf('task-auth-1') !== -1);
check('A_abort_fired_for_every_authoritative_tid',
      _abortCalls.length === 2 && _abortCalls.indexOf('task-auth-2') !== -1);
check('A_no_finishStream_local_teardown', _finishStreamCalls.length === 0);

// ══ Scenario B: duplicate click re-fires (corrective re-broadcast nudge —
//    the server treats a duplicate abort as idempotent + re-emits the idle
//    projection, so a second click must ALSO send, never swallow). ══
btn.onclick();
check('B_second_click_resends_abort',
      _abortCalls.length === 4 && _abortCalls[2] === 'task-auth-1');

// ══ Scenario C: genuinely idle (set emptied by a frame between render and
//    click) — no abort, no throw, and the stale Stop shape reconciles NOW. ══
reset();
const convC = {
  id: 'conv-x', activeTaskId: null,
  _authoritativeActiveTaskIds: new Set(),
  messages: [{ role: 'assistant', content: 'done' }],
};
_activeConv = convC;
conversations.length = 0; conversations.push(convC);
// Render Stop shape from a STALE busy state, then clear the set before the
// click (a frame landed in between) — the click must reconcile, not fire.
convC._authoritativeActiveTaskIds = new Set(['task-stale']);
updateSendButton();
check('C_precondition_stop_shaped', isStopShape());
convC._authoritativeActiveTaskIds = new Set();
btn.onclick();
check('C_no_abort_when_nothing_busy', _abortCalls.length === 0);
check('C_shape_reconciled_to_send', !isStopShape());

// ══ Scenario D (regression guard): local stream present → the stream abort
//    path still wins (controller aborted, stream taskId fired, NOT the
//    authoritative branch). ══
reset();
let _ctrlAborted = false;
const convD = {
  id: 'conv-x', activeTaskId: 'task-local',
  _authoritativeActiveTaskIds: new Set(['task-auth-9']),
  messages: [{ role: 'assistant', content: 'partial' }],
};
_activeConv = convD;
conversations.length = 0; conversations.push(convD);
activeStreams.set('conv-x', {
  taskId: 'task-local',
  controller: { abort() { _ctrlAborted = true; }, signal: { aborted: false } },
});
updateSendButton();
btn.onclick();
check('D_stream_controller_aborted', _ctrlAborted === true);
check('D_stream_task_aborted_not_authoritative',
      _abortCalls.length === 1 && _abortCalls[0] === 'task-local');

// ══ Scenario E (regression guard): activeTaskId pin, no stream → existing
//    pin branch unchanged (fires the pin, clears it, finishStream runs). ══
reset();
const convE = {
  id: 'conv-x', activeTaskId: 'task-pin',
  _authoritativeActiveTaskIds: new Set(['task-auth-9']),
  messages: [{ role: 'assistant', content: 'partial' }],
};
_activeConv = convE;
conversations.length = 0; conversations.push(convE);
updateSendButton();
btn.onclick();
check('E_pin_task_aborted', _abortCalls.length === 1 && _abortCalls[0] === 'task-pin');
check('E_pin_cleared', convE.activeTaskId === null);
check('E_finishStream_ran', _finishStreamCalls.length === 1);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_stop_click_with_authoritative_only_busy_fires_abort():
    harness = os.path.join(HERE, '_stop_authoritative_busy_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'send_button.js'),
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
    assert not fails, 'stop-button authoritative-busy failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS lines, got:\n{output}'
