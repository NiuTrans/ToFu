"""Regression: a conversation whose ``activeTaskId`` points at a task that is
no longer running on the server (terminal status, or 404 = discarded /
TTL-evicted / never-finalized carrier) must SELF-HEAL — the stuck "等待中…"
placeholder is reclaimed (or its partial result is landed) and the running
predicate (``conv.activeTaskId`` + ``activeStreams``) is cleared — WITHOUT a
manual force-refresh.

WHY
---
The autopilot summarize carrier used to linger as a phantom ``status='running'``
task (see ``test_autopilot_summary_no_phantom.py`` for the backend fix). While
the backend fix stops NEW phantoms, the hard project rule is that any
non-input-box-driven flow must self-heal — so even a leftover/already-shipped
phantom (or any task that 404s because it was discarded/evicted) must recover
live. The SSE for such a task NEVER delivers a ``done``, so the bubble shows
"等待中…" forever until the user force-refreshes.

``_healStuckPlaceholder(convId, probe)`` (static/js/core/health_stream_timer.js)
is the recovery primitive wired into the existing silence-probe in
``_updateStreamTimerUI`` (it fires on a terminal poll status AND on a 404):
  • EMPTY ghost placeholder (no content/thinking/real round) → pop it, clear
    ``conv.activeTaskId`` + ``activeStreams``, twStop, persist, re-render.
  • Placeholder WITH accumulated content → abort the stale SSE with
    ``stream._probeAbort = true`` so ``_trySSE`` falls through to
    ``_pollFallback`` (lands the authoritative result) — reuses the existing
    probe→recover mechanism, no second system.

This harness loads the REAL shipped ``health_stream_timer.js`` under node,
stubs the window globals it touches, and drives ``_healStuckPlaceholder``
directly for both scenarios + the no-op guards.

Post-§7 streamBufs RETIREMENT: `twStop` calls `clearStreamSession(convId)`,
so assertions check `!streamSessions.has(cid)` instead of `!streamBufs.has(cid)`.

TWO levels of coverage:
  1. ``test_heal_stuck_placeholder`` drives ``_healStuckPlaceholder`` directly
     (the recovery PRIMITIVE) under bare node.
  2. ``test_probe_404_triggers_heal`` drives the REAL ``_updateStreamTimerUI``
     under jsdom with ``Api.chat.poll``→404 + ``Api.health.check``→ok and a
     stale ``_streamTimers`` entry, proving the live 404 → ``_healStuckPlaceholder``
     CALL SITE (not just the primitive) is wired and load-bearing.

SOURCE-LEVEL NEGATIVE CONTROLS (all proven by hand; restored byte-identical):
  • Comment out the ``_isEmptyGhost`` reclaim block in ``_healStuckPlaceholder``
    → ``test_heal_stuck_placeholder`` empty-ghost + 404 checks FAIL.
  • Replace the probe's ``if (probeResp.status === 404) { _healStuckPlaceholder(...) }``
    with a bare ``return`` → ``test_probe_404_triggers_heal`` FAILS (the live
    wiring is gone), while ``test_heal_stuck_placeholder`` stays green (the
    primitive is unaffected) — proving the call site is independently covered.

Runs the REAL shipped JS under node (primitive) / jsdom (call site); skips
cleanly when the deps aren't installed.
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

// ── Window-scope globals _healStuckPlaceholder / health_stream_timer touch ──
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Mutable test state.
let conversations = [];
let activeConvId = null;
const activeStreams = new Map();
global.conversations = conversations;
global.activeStreams = activeStreams;

// Spy counters.
const calls = { saveConversations: 0, renderChat: 0,
                renderConversationList: 0, convCachePut: 0, abort: 0 };
global.saveConversations = (cid) => { calls.saveConversations++; };
global.renderChat = (c) => { calls.renderChat++; };
global.renderConversationList = () => { calls.renderConversationList++; };
global.ConvCache = { put: (c) => { calls.convCachePut++; } };
global.normalizeErrorEnvelope = (e) => e;
global.escapeHtml = (s) => String(s == null ? '' : s);
global.showToast = () => {};

// ── §7 streamBufs RETIREMENT: uniform session stub (health_stream_timer
//    references streamSessions / getStreamSession / clearStreamSession
//    directly; these must exist before we eval the real file). ──
global.streamSessions = new Map();
global.getStreamSession = (cid) => { let s = global.streamSessions.get(cid); if (!s) { s = { phase: null }; global.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = (cid, p) => { if (!global.streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; global.getStreamSession(cid).phase = p; };
global.clearStreamSession = (cid) => { global.streamSessions.delete(cid); };

global.Api = { health: { check: async () => ({ ok: true }) },
               chat: { poll: async () => ({ ok: false, status: 404 }) } };
global._startOfflineRecoveryPolling = () => {};
global.AbortSignal = global.AbortSignal || { timeout: () => undefined };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/health_stream_timer.js (real)

if (typeof _healStuckPlaceholder !== 'function') {
  console.log('FAIL fn_exposed _healStuckPlaceholder missing'); process.exit(0);
}
check('fn_exposed', true);

// Helper: reset state.
function reset() {
  conversations.length = 0;
  activeStreams.clear();
  streamSessions.clear();
  for (const k of Object.keys(calls)) calls[k] = 0;
  activeConvId = null;
  global.activeConvId = null;
}

// ── Scenario 1: EMPTY ghost placeholder + terminal probe → RECLAIMED. ──
{
  reset();
  const conv = {
    id: 'conv-ghost-1', activeTaskId: 'task-phantom-1',
    messages: [
      { role: 'user', content: 'summarize this run' },
      // The stuck placeholder: empty assistant, no content/thinking/round.
      { role: 'assistant', content: '', thinking: '', toolRounds: [] },
    ],
  };
  conversations.push(conv);
  // §7: seed the live session (twStart does this in production, but
  // _healStuckPlaceholder doesn't call twStart — it reads streamSessions
  // indirectly via twStop → clearStreamSession). Create the session.
  getStreamSession('conv-ghost-1');
  const ctrl = { abort: () => { calls.abort++; } };
  activeStreams.set('conv-ghost-1', { controller: ctrl });

  const acted = _healStuckPlaceholder('conv-ghost-1', { status: 'done' });
  check('heal_empty_ghost_acted', acted === true);
  check('heal_empty_ghost_placeholder_popped', conv.messages.length === 1
        && conv.messages[conv.messages.length - 1].role === 'user');
  check('heal_empty_ghost_activeTaskId_cleared', conv.activeTaskId === null);
  check('heal_empty_ghost_activeStreams_cleared', !activeStreams.has('conv-ghost-1'));
  check('heal_empty_ghost_sse_aborted', calls.abort === 1);
  // §7: twStop → clearStreamSession deletes from streamSessions.
  check('heal_empty_ghost_session_cleared', !streamSessions.has('conv-ghost-1'));
  check('heal_empty_ghost_persisted', calls.saveConversations === 1);
  check('heal_empty_ghost_cleared_at_stamped', typeof conv._activeTaskClearedAt === 'number');
}

// ── Scenario 2: same EMPTY ghost but via a 404 probe → RECLAIMED. ──
{
  reset();
  const conv = {
    id: 'conv-ghost-404', activeTaskId: 'task-gone-1',
    messages: [
      { role: 'user', content: 'go' },
      { role: 'assistant', content: '', thinking: '', toolRounds: [] },
    ],
  };
  conversations.push(conv);
  const acted = _healStuckPlaceholder('conv-ghost-404', { notFound: true });
  check('heal_404_acted', acted === true);
  check('heal_404_placeholder_popped', conv.messages.length === 1);
  check('heal_404_activeTaskId_cleared', conv.activeTaskId === null);
}

// ── Scenario 3: placeholder WITH accumulated content → DON'T pop; abort the
//    stale SSE with _probeAbort so _trySSE → _pollFallback lands the result. ──
{
  reset();
  const conv = {
    id: 'conv-partial-1', activeTaskId: 'task-partial-1',
    messages: [
      { role: 'user', content: 'do work' },
      { role: 'assistant', content: 'I already wrote some real output.',
        thinking: '', toolRounds: [] },
    ],
  };
  conversations.push(conv);
  const ctrl = { abort: () => { calls.abort++; } };
  const stream = { controller: ctrl };
  activeStreams.set('conv-partial-1', stream);

  const acted = _healStuckPlaceholder('conv-partial-1', { status: 'done' });
  check('heal_partial_acted', acted === true);
  // Must NOT pop the message that has real content.
  check('heal_partial_msg_kept', conv.messages.length === 2
        && conv.messages[1].content.length > 0);
  // Must route to poll fallback via _probeAbort (not reclaim).
  check('heal_partial_probeAbort_set', stream._probeAbort === true);
  check('heal_partial_sse_aborted', calls.abort === 1);
  // activeTaskId is left for _pollFallback/finishStream to clear.
  check('heal_partial_activeTaskId_untouched', conv.activeTaskId === 'task-partial-1');
}

// ── Scenario 4: NO activeTaskId → no-op (false). ──
{
  reset();
  const conv = { id: 'conv-notask', activeTaskId: null,
                 messages: [{ role: 'assistant', content: '' }] };
  conversations.push(conv);
  check('no_task_noop', _healStuckPlaceholder('conv-notask', { status: 'done' }) === false);
}

// ── Scenario 5: unknown conv → no-op (false), no throw. ──
{
  reset();
  check('unknown_conv_noop', _healStuckPlaceholder('does-not-exist', { notFound: true }) === false);
}

// ── Scenario 6: a real tool round means work happened — the trailing
//    assistant is NOT an empty ghost, so it's treated as accumulated content
//    (route to poll), not reclaimed. ──
{
  reset();
  const conv = {
    id: 'conv-realround', activeTaskId: 'task-rr-1',
    messages: [
      { role: 'user', content: 'x' },
      { role: 'assistant', content: '', thinking: '',
        toolRounds: [{ status: 'done', toolName: 'run_command' }] },
    ],
  };
  conversations.push(conv);
  activeStreams.set('conv-realround', { controller: { abort: () => { calls.abort++; } } });
  const acted = _healStuckPlaceholder('conv-realround', { status: 'done' });
  check('realround_not_reclaimed', conv.messages.length === 2);
  check('realround_routed_to_poll', acted === true && calls.abort === 1);
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_heal_stuck_placeholder():
    harness = os.path.join(HERE, '_stuck_heal_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'health_stream_timer.js'),  # argv[2]
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
    assert not fails, 'stuck-placeholder self-heal failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS lines, got:\n{output}'


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ── Level 2: drive the REAL _updateStreamTimerUI so the 404 CALL SITE is proven ──
# This loads health_stream_timer.js under jsdom, installs a stale _streamTimers
# entry (silence past _SILENCE_THRESHOLD), stubs Api.chat.poll → 404 +
# Api.health.check → ok, then calls _updateStreamTimerUI and awaits the async
# probe. The probe's `if (probeResp.status === 404) { _healStuckPlaceholder(...) }`
# branch is the ONLY path that can reclaim the placeholder here, so a green
# assertion proves the wiring, not just the primitive.
_DRIVER_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatInner">' +
  '<div id="streaming-body"></div>' +
  '<span id="stream-elapsed-timer"></span></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
// Real timers are neutered — we invoke _updateStreamTimerUI by hand.
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
global.setTimeout = win.setTimeout = (fn) => (typeof fn === 'function' ? fn() : 0);
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = win.cancelAnimationFrame = () => {};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// State + stubs.
const conversations = [];
const activeStreams = new Map();
global.conversations = win.conversations = conversations;
global.activeStreams = win.activeStreams = activeStreams;

// ── §7 streamBufs RETIREMENT: uniform session stub ──
const streamSessions = new Map();
win.streamSessions = global.streamSessions = streamSessions;
win.getStreamSession = global.getStreamSession = (cid) => { let s = streamSessions.get(cid); if (!s) { s = { phase: null }; streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { streamSessions.delete(cid); };

global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = win.normalizeErrorEnvelope = (e) => e;
global.showToast = win.showToast = () => {};
global.saveConversations = win.saveConversations = () => {};
global.renderChat = win.renderChat = () => {};
global.renderConversationList = win.renderConversationList = () => {};
global.ConvCache = win.ConvCache = { put: () => {} };
global._startOfflineRecoveryPolling = win._startOfflineRecoveryPolling = () => {};
global.AbortSignal = win.AbortSignal || { timeout: () => undefined };

// Probe stubs: health OK (so the alive branch runs) + poll 404 (the path under test).
let pollCalls = 0;
global.Api = win.Api = {
  health: { check: async () => ({ ok: true }) },
  chat: { poll: async () => { pollCalls++; return { ok: false, status: 404 }; } },
};

// ONE eval scope for both files — a direct eval keeps `let`/`const` in its
// own declarative record, so separate evals would hide the monitor's
// _serverAlive / _HEALTH_CHECK_INTERVAL from the timer file (the browser
// bundle shares one lexical scope across concatenated scripts).
eval(fs.readFileSync(process.argv[4], 'utf8') + '\n;\n' +
     fs.readFileSync(process.argv[2], 'utf8'));  // monitor (argv[4]) + health_stream_timer.js (argv[2])

if (typeof _updateStreamTimerUI !== 'function') {
  console.log('FAIL fn_exposed _updateStreamTimerUI missing'); process.exit(0);
}

// The stuck conversation: a phantom activeTaskId + empty ghost placeholder.
const conv = {
  id: 'conv-live-404', activeTaskId: 'task-phantom-live',
  messages: [
    { role: 'user', content: 'summarize this run' },
    { role: 'assistant', content: '', thinking: '', toolRounds: [] },
  ],
};
conversations.push(conv);
activeStreams.set('conv-live-404', { controller: { abort: () => {} } });

// Make _updateStreamTimerUI take the probe path: active conv + stale timer
// (lastDataTime far enough back to exceed _SILENCE_THRESHOLD=20s) + a DOM timer.
global.activeConvId = win.activeConvId = 'conv-live-404';
// _streamTimers is module-internal; seed it via the public twStart then age it.
twStart('conv-live-404');
// twStart calls getStreamSession which creates the live session.

(async () => {
  // Drive the timer tick. _updateStreamTimerUI reads _streamTimers internally;
  // we can't reach it directly, so we monkey-age via repeated calls won't help.
  // Instead: the function computes silentSec from info.lastDataTime. twStart set
  // it to now, so we must age it. Expose by overriding Date.now temporarily.
  const _realNow = Date.now;
  Date.now = () => _realNow() + 60000;  // pretend 60s elapsed → silence > threshold
  try {
    _updateStreamTimerUI('conv-live-404');
    // The probe runs in a .then() chain; drain microtasks + the awaited fetches.
    for (let i = 0; i < 20; i++) { await Promise.resolve(); }
    await new Promise(r => (_realNow.call ? setTimeout(r, 0) : r()));
    for (let i = 0; i < 20; i++) { await Promise.resolve(); }
  } finally {
    Date.now = _realNow;
  }

  check('probe_polled', pollCalls >= 1);
  check('live404_placeholder_popped', conv.messages.length === 1
        && conv.messages[0].role === 'user');
  check('live404_activeTaskId_cleared', conv.activeTaskId === null);
  check('live404_activeStreams_cleared', !activeStreams.has('conv-live-404'));
  console.log(out.join('\n'));
})();
"""


# ── Level 3: the WAKE sweep. A backgrounded/locked tablet FREEZES the per-second
#    timer, so silence detection never fires during the freeze and the SSE socket
#    dies while the task keeps running server-side. On wake, _probeAllStuckStreamsOnWake
#    must walk _streamTimers and, for a still-running task, either abort this tab's
#    stale SSE reader with _probeAbort (→ connectToTask resumes via Last-Event-ID)
#    or reconnect fresh via connectToTask when no live stream exists in this tab.
_WAKE_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const conversations = [];
const activeStreams = new Map();
global.conversations = conversations;
global.activeStreams = activeStreams;

// ── §7 streamBufs RETIREMENT: uniform session stub ──
const streamSessions = new Map();
global.streamSessions = streamSessions;
global.getStreamSession = (cid) => { let s = streamSessions.get(cid); if (!s) { s = { phase: null }; streamSessions.set(cid, s); } return s; };
global.setStreamPhase = (cid, p) => { if (!streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; global.getStreamSession(cid).phase = p; };
global.clearStreamSession = (cid) => { streamSessions.delete(cid); };

global.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = (e) => e;
global.showToast = () => {};
global.saveConversations = () => {};
global.renderChat = () => {};
global.renderConversationList = () => {};
global.ConvCache = { put: () => {} };
global._startOfflineRecoveryPolling = () => {};
global.AbortSignal = global.AbortSignal || { timeout: () => undefined };
global.activeConvId = null;

// Health OK; poll → still running (the case the wake sweep must reconnect).
global.Api = {
  health: { check: async () => ({ ok: true }) },
  chat: { poll: async () => ({ ok: true, json: async () => ({ status: 'running' }) }) },
};
// Spy connectToTask (fresh-reconnect branch).
let connectCalls = [];
global.connectToTask = (cid, tid) => { connectCalls.push([cid, tid]); };
// Neuter real timers so twStart's setInterval doesn't actually run.
global.setInterval = () => 0;
global.clearInterval = () => {};

// ONE eval scope for both files — see the level-2 note (let/const do not
// escape a direct eval; the bundle shares one lexical scope).
eval(fs.readFileSync(process.argv[3], 'utf8') + '\n;\n' +
     fs.readFileSync(process.argv[2], 'utf8'));  // monitor (argv[3]) + health_stream_timer.js (argv[2])

check('wake_fn_exposed', typeof _probeAllStuckStreamsOnWake === 'function'
      && typeof _probeStuckStream === 'function');

// ── Scenario A: still-running task WITH a live SSE stream in this tab →
//    wake sweep aborts the stale reader with _probeAbort (resume-via-cursor). ──
async function _scenarioA() {
  const conv = { id: 'wake-live', activeTaskId: 'task-wake-live',
    messages: [{ role: 'user', content: 'go' },
               { role: 'assistant', content: 'partial…', thinking: '', toolRounds: [] }] };
  conversations.push(conv);
  let aborted = 0;
  const stream = { controller: { signal: {}, abort: () => { aborted++; } } };
  activeStreams.set('wake-live', stream);
  twStart('wake-live');   // seeds _streamTimers entry (lastDataTime = now)

  const _realNow = Date.now;
  Date.now = () => _realNow() + 60000;  // 60s of silence → past threshold
  try { _probeAllStuckStreamsOnWake('test'); } finally { Date.now = _realNow; }

  // Drain the async probe (health.then → poll → running branch).
  for (let i = 0; i < 40; i++) await Promise.resolve();
  check('wakeA_probeAbort_set', stream._probeAbort === true);
  check('wakeA_sse_aborted', aborted === 1);
  check('wakeA_no_fresh_connect', connectCalls.length === 0);
}

// ── Scenario B: still-running task with NO live stream in this tab →
//    wake sweep reconnects fresh via connectToTask. ──
async function _scenarioB() {
  const conv = { id: 'wake-nostream', activeTaskId: 'task-wake-nostream',
    messages: [{ role: 'user', content: 'go' },
               { role: 'assistant', content: '', thinking: '', toolRounds: [] }] };
  conversations.push(conv);
  twStart('wake-nostream');
  activeStreams.delete('wake-nostream');   // no live stream in this tab

  const _realNow = Date.now;
  Date.now = () => _realNow() + 60000;
  try { _probeAllStuckStreamsOnWake('test'); } finally { Date.now = _realNow; }
  for (let i = 0; i < 40; i++) await Promise.resolve();
  check('wakeB_fresh_connect', connectCalls.some(c => c[0] === 'wake-nostream'
        && c[1] === 'task-wake-nostream'));
}

(async () => {
  await _scenarioA();
  await _scenarioB();
  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_wake_probe_reconnects_stuck_stream():
    harness = os.path.join(HERE, '_wake_probe_harness.js')
    with open(harness, 'w') as f:
        f.write(_WAKE_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'health_stream_timer.js'),
             os.path.join(JS_DIR, 'core', 'backend_offline_monitor.js')],
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
    assert not fails, 'wake-probe reconnect failures:\n' + output
    assert output.count('PASS') >= 5, f'expected >=5 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_probe_404_triggers_heal():
    harness = os.path.join(HERE, '_stuck_heal_driver_harness.js')
    with open(harness, 'w') as f:
        f.write(_DRIVER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'health_stream_timer.js'),   # argv[2]
             ROOT,                                                      # argv[3]
             os.path.join(JS_DIR, 'core', 'backend_offline_monitor.js'),# argv[4]
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
    assert not fails, 'live 404 → heal call-site failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'
