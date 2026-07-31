#!/usr/bin/env python3
"""The ``latestLiveTaskId`` wire: done-frame stamp → conv field → attach.

WHY (production incident 2026-07-25)
------------------------------------
The pt_8dc03017 cutover closed the parent's SSE stream promptly at
status-flip and documented "the client discovers the successor via the
conv→latest-task supersede index". In reality NOTHING wrote
``conv._latestLiveTaskId`` on the client (zero writers repo-wide) and no
terminal frame carried the index — the attach reducer shipped in
``_runTerminalContinuation`` was dead code. A queued send behind the
invisible VU window then sat in the bar with no attach until F5.

The backend now stamps ``latestLiveTaskId`` on terminal frames
(tests/test_late_done_successor_stamp.py). This suite pins the frontend
half of the wire:

  1. ``_stampLatestLiveTask(conv, ev)`` (stream_lifecycle.js) records the
     successor id from a done / LATE-done frame onto the conv.
  2. The SSE done branch (sse_pipeline.js) CALLS it for every terminal
     frame (static pin — the cross-file seam).
  3. ``_runTerminalContinuation``'s supersede reducer CONSUMES the stamp
     (so it can never re-fire stale) and attaches via connectToTask AFTER
     reloading messages (a VU-synthesized user turn must land before the
     successor's stream opens).
  4. No-stamp / self-stamp cases keep the reducer's original no-op and
     idempotent contracts (the sibling chain-attach suite stays green).

NEUTER (manual A/B): deleting the ``_stampLatestLiveTask(...)`` call from
sse_pipeline.js turns the static pin red; deleting the helper turns the
jsdom harness red — together they prove the wire is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const MODE = process.argv[3] || 'WIRE_ATTACH';   // WIRE_ATTACH | NO_STAMP | SELF_STAMP
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.t = global.t = (k) => k;
win.formatClockTime = global.formatClockTime = () => '12:00';

// ── Record every sink the reducer can reach. ──
const calls = { connect: [], queued: [], attachBaton: [], load: [] };
win.connectToTask = global.connectToTask = (cid, tid) => { calls.connect.push([cid, tid]); };
win._checkForQueuedTask = global._checkForQueuedTask = (cid) => { calls.queued.push(cid); };
win._attachAutopilotFollowup = global._attachAutopilotFollowup = (cid, p) => { calls.attachBaton.push([cid, p]); };
win._findAutopilotPendingCarrier = global._findAutopilotPendingCarrier = () => null;
win._dispatchableQueueCount = global._dispatchableQueueCount = () => 0;
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '';
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
win.loadConversationMessages = global.loadConversationMessages =
  (cid) => { calls.load.push(cid); return Promise.resolve(null); };

const activeStreams = new Map();
win.activeStreams = global.activeStreams = activeStreams;
win.activeConvId = global.activeConvId = 'C1';

const conv = { id: 'C1', messages: [], activeTaskId: null };
win.conversations = global.conversations = [conv];

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'stream_lifecycle.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _stampLatestLiveTask !== 'function') {
  console.log('FAIL stamp_fn_exposed _stampLatestLiveTask missing'); process.exit(0);
}
check('stamp_fn_exposed', true);

if (MODE === 'WIRE_ATTACH') {
  // 1. A terminal frame carrying the successor stamps the conv.
  _stampLatestLiveTask(conv, { type: 'done', latestLiveTaskId: 'succ-vu-1' });
  check('stamp_recorded', conv._latestLiveTaskId === 'succ-vu-1');
  // 2. The continuation consumes the stamp and attaches (after a reload).
  _runTerminalContinuation('C1');
  setTimeout(() => {
    check('attach_connects_successor', calls.connect.length === 1
          && calls.connect[0][0] === 'C1' && calls.connect[0][1] === 'succ-vu-1');
    check('stamp_consumed', !conv._latestLiveTaskId);
    check('reload_before_attach', calls.load.length === 1 && calls.load[0] === 'C1');
    check('attach_short_circuits_queued', calls.queued.length === 0);
    console.log(out.join('\n'));
    process.exit(0);
  }, 700);
} else if (MODE === 'NO_STAMP') {
  // No wire field → reducer stays a no-op → legacy queued-check path runs.
  _runTerminalContinuation('C1');
  setTimeout(() => {
    check('no_stamp_no_attach', calls.connect.length === 0);
    check('no_stamp_falls_through_to_queued', calls.queued.length === 1);
    console.log(out.join('\n'));
    process.exit(0);
  }, 700);
} else if (MODE === 'SELF_STAMP') {
  // The stamp names the task we are ALREADY on → never re-attach.
  conv.activeTaskId = 'succ-vu-1';
  activeStreams.set('C1', {});
  _stampLatestLiveTask(conv, { type: 'done', latestLiveTaskId: 'succ-vu-1' });
  _runTerminalContinuation('C1');
  setTimeout(() => {
    check('self_stamp_no_reattach', calls.connect.length === 0);
    console.log(out.join('\n'));
    process.exit(0);
  }, 700);
}
"""


def _run(mode: str):
    harness = os.path.join(HERE, '_latest_live_task_wire_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, mode],
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
def test_wire_attach_stamp_consumed_and_connected():
    output = _run('WIRE_ATTACH')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'latestLiveTaskId wire-attach guard failed:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_no_stamp_keeps_reducer_noop():
    output = _run('NO_STAMP')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'no-stamp no-op contract failed:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_self_stamp_never_reattaches():
    output = _run('SELF_STAMP')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'self-stamp idempotency contract failed:\n' + output


# ────────────────────── static wire pins (no node needed) ──────────────────────

def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_done_branch_calls_stamp_helper():
    """The cross-file seam: the SSE done branch MUST invoke the stamp helper
    for every terminal frame (LATE done rides the same branch). Deleting the
    call silently kills the whole wire while every harness above stays green
    (they drive the helper directly) — this pin is the NEUTER tripwire."""
    src = _read(os.path.join('static', 'js', 'ui', 'sse_pipeline.js'))
    assert '_stampLatestLiveTask(' in src, (
        'sse_pipeline.js done branch must call _stampLatestLiveTask(conv, ev) '
        '— without it conv._latestLiveTaskId has no writer and the attach '
        'reducer is dead code (the 2026-07-25 silent-queue incident)'
    )


def test_reducer_consumes_the_stamp():
    """The supersede block must CONSUME conv._latestLiveTaskId on attach
    (assign null) — a stale stamp would re-attach to a long-dead successor
    on every later terminal continuation."""
    src = _read(os.path.join('static', 'js', 'ui', 'stream_lifecycle.js'))
    assert 'function _stampLatestLiveTask' in src, 'helper missing'
    assert '_latestLiveTaskId = null' in src, (
        'the supersede reducer must consume (null out) the stamp on attach'
    )
