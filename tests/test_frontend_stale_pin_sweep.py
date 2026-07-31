"""Regression: a BACKGROUND conversation whose ``activeTaskId`` is pinned but
whose backend task no longer runs must have its sidebar "busy" dot cleared
WITHOUT a manual force-refresh — the "sidebar dot outlives the work" incident.

WHY
---
A conv can hold ``conv.activeTaskId`` with NO ``activeStreams`` entry (its SSE
finished/dropped, or the pin was set on load without a live reconnect). If the
task then dies WITHOUT cleanly finalizing, the server-side reaper
(``reap_stuck_running_tasks``) flips it terminal and drops it from the in-memory
registry, so ``/api/v1/chat/active`` STOPS reporting it running. But the stream
timer's self-heal (``_healStuckPlaceholder`` via ``_updateStreamTimerUI``) is
gated on BOTH a live ``_streamTimers`` entry AND ``activeConvId === convId`` — so
a BACKGROUND orphan is never evaluated → the sidebar dot never clears.

Two coupled fixes are exercised here (both shipped JS, loaded real under node):
  1. ``convIsBusy(conv)`` (ui/conversation_list.js) — the SINGLE busy-predicate
     consumed by BOTH the sidebar (``_convStatusFlags``) and the composer
     (``updateSendButton`` in ui/send_button.js). They can no longer disagree
     about one conv.
  2. ``_reconcileStuckActiveTaskPins(activeTasks)`` (core/cross_tab_sync.js) — a
     conv-agnostic sweep (NOT activeConvId-gated, NOT _streamTimers-gated) that,
     for any conv with a pin + no live stream whose task is CONFIRMED absent from
     the ``/api/v1/chat/active`` running set, clears the pin via the extended
     ``_healStuckPlaceholder(convId, {background:true})`` branch.

COVERAGE
--------
  • Positive: background orphan pin (≠ activeConvId, no activeStreams) whose
    task is NOT in the running set → sweep clears the pin, ``convIsBusy`` flips
    false, and the composer (viewing a DIFFERENT idle conv) is unaffected.
  • Guard: a pin whose task IS still in the running set → NOT cleared (slow but
    alive; the server reaper is the sole "wedged" authority).
  • Guard: a probe failure (non-array) → touches nothing (fail-safe).
  • NEUTER: revert the sweep to a no-op → the stale pin persists (convIsBusy
    stays true) — proving the sweep is load-bearing, not a tautology.

Runs the REAL shipped JS under bare node; skips cleanly when node is absent.
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


# The harness loads the four real shipped files in bundle order and stubs the
# window globals they touch. argv[2..5] are the JS paths. ``NEUTER=1`` env var
# swaps _reconcileStuckActiveTaskPins for a no-op AFTER load to prove the
# assertion catches the regression.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Mutable state the real code reads ──
const conversations = [];
const activeStreams = new Map();
const streamBufs = new Map();
global.conversations = conversations;
global.activeStreams = activeStreams;
global.streamBufs = streamBufs;
global.streamSessions = new Map();
global.getStreamSession = global.getStreamSession = (cid) => { let s = global.streamSessions.get(cid); if (!s) { s = { phase: null }; global.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!global.streamSessions.has(cid) && !(typeof activeStreams !== "undefined" && activeStreams.has(cid))) return; global.getStreamSession(cid).phase = p; };
global.clearStreamSession = global.clearStreamSession = (cid) => { global.streamSessions.delete(cid); };
global.activeConvId = null;
global._branchStreams = new Map();
global._activeBranch = null;

// ── Spy-able render/persist stubs ──
const calls = { renderConversationList: 0, renderChat: 0, saveConversations: 0 };
global.renderConversationList = () => { calls.renderConversationList++; };
global.renderChat = () => { calls.renderChat++; };
global.saveConversations = () => { calls.saveConversations++; };
global.ConvCache = { put: () => {} };
global.normalizeErrorEnvelope = (e) => e;
global.escapeHtml = (s) => String(s == null ? '' : s);
global.showToast = () => {};
global._startOfflineRecoveryPolling = () => {};
global.debugLog = () => {};
global.getActiveConv = () => conversations.find(c => c.id === global.activeConvId) || null;
global.AbortSignal = global.AbortSignal || { timeout: () => undefined };
// _dispatchableQueueCount + _branchKey are referenced by updateSendButton.
global._dispatchableQueueCount = () => 0;
global._branchKey = (cid, mi, bi) => cid + ':' + mi + ':' + bi;
global.sendMessage = () => {};
// A DOM stub for the sendBtn element updateSendButton mutates.
const _btn = { className: '', innerHTML: '', onclick: null };
global.document = {
  getElementById: (id) => (id === 'sendBtn' ? _btn : null),
  // cross_tab_sync.js registers a visibilitychange listener at module load.
  addEventListener: () => {},
  visibilityState: 'visible',
};
// cross_tab_sync.js registers an 'online' listener on window at module load.
global.addEventListener = () => {};
// Api.chat.active is only used by _crossDeviceReconcile's timer, not the
// sweep-under-test (we call _reconcileStuckActiveTaskPins directly), but keep
// it present so eval of cross_tab_sync.js doesn't trip a reference.
global.Api = { chat: { active: async () => [] }, health: { check: async () => ({ ok: true }) } };

// Load real shipped files in bundle order (globals shared via window scope).
eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/conversation_list.js  (convIsBusy, _convStatusFlags)
eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/send_button.js         (updateSendButton)
eval(fs.readFileSync(process.argv[4], 'utf8'));  // core/health_stream_timer.js (_healStuckPlaceholder bg branch, twStart/twStop)
eval(fs.readFileSync(process.argv[5], 'utf8'));  // core/cross_tab_sync.js     (_reconcileStuckActiveTaskPins)

check('convIsBusy_exposed', typeof convIsBusy === 'function');
check('sweep_exposed', typeof _reconcileStuckActiveTaskPins === 'function');
check('healbg_exposed', typeof _healStuckPlaceholder === 'function');

// The real renderConversationList (from conversation_list.js) reads many more
// globals (sidebarSearchQuery, folders, DOM …) than this unit harness stubs —
// it is NOT under test here (convIsBusy + the sweep + the heal-bg branch are).
// Override the render/persist hooks with spies AFTER load so _healStuckPlaceholder
// exercises its REAL state-mutation logic (pin clear, settle, activeStreams
// delete) without dragging in the whole list-render surface. NOTE: these are
// FUNCTION DECLARATIONS in the eval'd scope, so a `global.x = …` assignment is
// shadowed by the hoisted decl — reassign the BARE identifier (indirect eval so
// the binding resolves to the eval'd decl, matching the memory harness rule).
renderConversationList = function () {};
renderChat = function () {};

// NEUTER: replace the sweep with a no-op to prove the assertions are load-bearing.
if (process.env.NEUTER === '1') {
  _reconcileStuckActiveTaskPins = function () { return 0; };
}

function reset() {
  conversations.length = 0;
  activeStreams.clear();
  streamBufs.clear();
  for (const k of Object.keys(calls)) calls[k] = 0;
}

// ── Scenario A: background orphan pin, task NOT running → swept clear. ──
{
  reset();
  // The VIEWED conv is idle (no pin) — the composer is about IT.
  const viewed = { id: 'conv-viewed-idle', activeTaskId: null,
                   messages: [{ role: 'assistant', content: 'done', finishReason: 'stop' }] };
  // The BACKGROUND conv carries a stale pin, NO activeStreams entry, and a
  // trailing assistant with partial content (the produced-then-wedged shape).
  const bg = { id: 'conv-bg-orphan', activeTaskId: 'task-wedged-1',
               messages: [
                 { role: 'user', content: 'go' },
                 { role: 'assistant', content: 'partial', thinking: '', toolRounds: [] },
               ] };
  conversations.push(viewed, bg);
  global.activeConvId = 'conv-viewed-idle';

  // Sanity BEFORE: sidebar sees bg as busy; viewed is idle.
  check('A_before_bg_busy', convIsBusy(bg) === true);
  check('A_before_viewed_idle', convIsBusy(viewed) === false);

  // /active reports NOTHING running (the task was reaped + dropped).
  const cleared = _reconcileStuckActiveTaskPins([]);

  if (process.env.NEUTER === '1') {
    // Neutered: the sweep is a no-op → the stale pin persists → still busy.
    check('A_NEUTER_pin_persists', bg.activeTaskId === 'task-wedged-1');
    check('A_NEUTER_still_busy', convIsBusy(bg) === true);
  } else {
    check('A_swept_one', cleared === 1);
    check('A_pin_cleared', bg.activeTaskId === null);
    check('A_bg_not_busy', convIsBusy(bg) === false);
    // Partial content settled with an honest finishReason (not popped).
    check('A_bg_settled', bg.messages.length === 2
          && bg.messages[1].finishReason === 'interrupted');
    // The VIEWED (idle) conv was never touched → composer correctly idle.
    check('A_viewed_untouched', convIsBusy(viewed) === false && viewed.activeTaskId === null);
    // Composer reflects the viewed conv: NOT in stop-btn state.
    global.activeConvId = 'conv-viewed-idle';
    updateSendButton();
    check('A_composer_idle', _btn.className.indexOf('stop-btn') === -1);
  }
}

// ── Scenario B: pin whose task IS still running → NOT cleared. ──
{
  reset();
  const bg = { id: 'conv-bg-alive', activeTaskId: 'task-alive-1',
               messages: [{ role: 'assistant', content: '', thinking: '', toolRounds: [] }] };
  conversations.push(bg);
  global.activeConvId = null;
  const cleared = _reconcileStuckActiveTaskPins([
    { id: 'task-alive-1', status: 'running', aborted: false },
  ]);
  check('B_alive_not_swept', cleared === 0);
  check('B_alive_pin_kept', bg.activeTaskId === 'task-alive-1');
  check('B_alive_still_busy', convIsBusy(bg) === true);
}

// ── Scenario C: probe failed (non-array) → touch nothing (fail-safe). ──
{
  reset();
  const bg = { id: 'conv-bg-failsafe', activeTaskId: 'task-x',
               messages: [{ role: 'assistant', content: 'partial' }] };
  conversations.push(bg);
  const cleared = _reconcileStuckActiveTaskPins(null);
  check('C_failsafe_no_clear', cleared === 0 && bg.activeTaskId === 'task-x');
}

// ── Scenario D: a conv WITH a live stream is never swept (owns its lifecycle). ──
{
  reset();
  const bg = { id: 'conv-live-stream', activeTaskId: 'task-live-stream',
               messages: [{ role: 'assistant', content: '' }] };
  conversations.push(bg);
  activeStreams.set('conv-live-stream', { controller: { abort: () => {} } });
  const cleared = _reconcileStuckActiveTaskPins([]);  // task not in running set…
  check('D_livestream_not_swept', cleared === 0 && bg.activeTaskId === 'task-live-stream');
}

console.log(out.join('\n'));
// cross_tab_sync.js registers a module-load setInterval that would keep the
// node process alive → force a clean exit after the synchronous checks.
process.exit(0);
"""


def _run_harness(neuter: bool):
    harness = os.path.join(HERE, '_stale_pin_sweep_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    env = dict(os.environ)
    if neuter:
        env['NEUTER'] = '1'
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'conversation_list.js'),      # argv[2]
             os.path.join(JS_DIR, 'ui', 'send_button.js'),            # argv[3]
             os.path.join(JS_DIR, 'core', 'health_stream_timer.js'),  # argv[4]
             os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'),       # argv[5]
             ],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_stale_pin_sweep_clears_background_orphan():
    proc = _run_harness(neuter=False)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'stale-pin sweep failures:\n' + output
    # 3 exposed + A(before2 + swept1 + cleared1 + not_busy1 + settled1 +
    #   viewed1 + composer1)=8 + B3 + C1 + D1 = 16
    assert output.count('PASS') >= 16, f'expected >=16 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_sweep_leaves_stale_pin():
    """NEUTER: with the sweep reverted to a no-op, the background orphan pin
    persists and convIsBusy stays true — proving the sweep is load-bearing."""
    proc = _run_harness(neuter=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'neuter control failures:\n' + output
    assert 'PASS A_NEUTER_pin_persists' in output, output
    assert 'PASS A_NEUTER_still_busy' in output, output
