"""jsdom guard for the autopilot supersede-index attach reducer
(epic pt_8dc030176bad450b, build-order STEP 2).

WHAT THIS PINS
--------------
`_runTerminalContinuation` (static/js/ui/stream_lifecycle.js) gains a reducer at
the TOP: after any turn's terminal continuation, if the conv's
server-authoritative latest live task (`conv._latestLiveTaskId`) is a DIFFERENT
pending/running task than the one we are on, attach to it via `connectToTask` —
the transport-agnostic "attach to the conv's newer live task after done" rule
that REPLACES the hand-carried baton on cutover (design §4).

Three behaviors are guarded:
  1. NO-OP TODAY — when `_latestLiveTaskId` is absent (the current world, VU still
     runs with convId='' and is invisible to the index), the reducer short-
     circuits and control falls through to the existing baton fast-path. This is
     why step 2 can ship BEFORE the human-gated backend cutover with zero risk.
  2. ATTACH — when `_latestLiveTaskId` is present and differs from activeTaskId
     (the post-cutover world: backend advanced the index to the VU/follow-up
     BEFORE emitting done, per HB-1), the reducer calls connectToTask(convId, id).
  3. IDEMPOTENT — when `_latestLiveTaskId` equals the task we are already on (an
     active stream / activeTaskId), it does NOT re-attach, so a done observed on
     BOTH sse and a poll-fallback cannot double-attach (design §5, hazard 2).

Drives the REAL shipped `_runTerminalContinuation` under jsdom with connectToTask
+ _checkForQueuedTask stubbed to RECORD calls. Skips cleanly when node + jsdom
aren't installed.
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
STREAM_LIFECYCLE = runtime_section_path('ui/stream_lifecycle.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const MODE = process.argv[4] || 'ABSENT';   // ABSENT | ATTACH | IDEMPOTENT
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.t = global.t = (k) => k;
win.formatClockTime = global.formatClockTime = () => '12:00';

// ── Record connectToTask + _checkForQueuedTask calls (the two continuation
//    sinks). The reducer under test must route to connectToTask; the legacy
//    fall-through routes to _checkForQueuedTask. ──
const calls = { connect: [], queued: [], attachBaton: [] };
win.connectToTask = global.connectToTask = (cid, tid) => { calls.connect.push([cid, tid]); };
win._checkForQueuedTask = global._checkForQueuedTask = (cid) => { calls.queued.push(cid); };
win._attachAutopilotFollowup = global._attachAutopilotFollowup = (cid, p) => { calls.attachBaton.push([cid, p]); };
win._findAutopilotPendingCarrier = global._findAutopilotPendingCarrier = () => null;
win._dispatchableQueueCount = global._dispatchableQueueCount = () => 0;
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '';
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};

// activeStreams: has() reflects whether we hold a live stream for the conv.
const activeStreams = new Map();
win.activeStreams = global.activeStreams = activeStreams;
win.activeConvId = global.activeConvId = 'C1';

const conv = { id: 'C1', messages: [], activeTaskId: 'parent-task' };
win.conversations = global.conversations = [conv];

// Configure per MODE:
if (MODE === 'ABSENT') {
  // No _latestLiveTaskId → reducer must NO-OP → legacy path (queued check) runs.
  // (activeTaskId cleared as finishStream would have done just before.)
  conv.activeTaskId = null;
} else if (MODE === 'ATTACH') {
  // Post-cutover: index advanced to the VU successor before done. We are NOT on
  // a stream for it → reducer must attach.
  conv.activeTaskId = null;
  conv._latestLiveTaskId = 'vu-task-xyz';
} else if (MODE === 'IDEMPOTENT') {
  // The latest live task IS the one we are already streaming → must NOT attach.
  conv._latestLiveTaskId = 'vu-task-xyz';
  conv.activeTaskId = 'vu-task-xyz';
  activeStreams.set('C1', {});
}

// stream_lifecycle.js references many symbols from sibling modules at runtime;
// stub the ones _runTerminalContinuation's body can reach so eval + call don't
// throw. (We only exercise _runTerminalContinuation.)
eval(fs.readFileSync(process.argv[3], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _runTerminalContinuation !== 'function') {
  console.log('FAIL fn_exposed _runTerminalContinuation missing'); process.exit(0);
}
check('fn_exposed', true);

_runTerminalContinuation('C1');

// The reducer's setTimeout(_checkForQueuedTask) is async; flush it.
setTimeout(() => {
  if (MODE === 'ABSENT') {
    // No index-driven attach; legacy queued-check path runs (after its delay).
    check('absent_no_index_attach', calls.connect.length === 0);
    check('absent_falls_through_to_queued', calls.queued.length === 1);
  } else if (MODE === 'ATTACH') {
    check('attach_called_connect', calls.connect.length === 1
          && calls.connect[0][0] === 'C1' && calls.connect[0][1] === 'vu-task-xyz');
    // Reducer returns BEFORE scheduling the queued check.
    check('attach_short_circuits_queued', calls.queued.length === 0);
  } else if (MODE === 'IDEMPOTENT') {
    // Already on that task → no re-attach; falls through to the legacy path.
    check('idempotent_no_reattach', calls.connect.length === 0);
  }
  console.log(out.join('\n'));
  process.exit(0);
}, 700);
"""


def _run(mode: str):
    harness = os.path.join(HERE, '_autopilot_chain_attach_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, STREAM_LIFECYCLE, mode],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_reducer_is_noop_when_index_field_absent():
    """STEP-2 SAFETY: with `_latestLiveTaskId` absent (today's world), the
    reducer short-circuits — no index-driven attach — and control falls through
    to the existing baton/queued path. This is why shipping the reducer before
    the backend cutover cannot regress the current behavior."""
    output = _run('ABSENT')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reducer no-op guard failed:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_reducer_attaches_to_newer_live_task():
    """Post-cutover: `_latestLiveTaskId` names a DIFFERENT pending/running task
    (the VU successor the backend advanced the index to BEFORE emitting done,
    HB-1) → the reducer attaches via connectToTask and short-circuits the legacy
    queued path. This is the whole handoff, index-driven, no baton."""
    output = _run('ATTACH')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reducer attach guard failed:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_reducer_idempotent_when_already_on_task():
    """`_latestLiveTaskId` equals the task we are already streaming → the reducer
    must NOT re-attach, so a done observed on BOTH sse and a poll-fallback cannot
    double-attach (design §5, hazard 2 — idempotency)."""
    output = _run('IDEMPOTENT')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reducer idempotency guard failed:\n' + output
