"""tests/test_frontend_stall_watch.py — the "no unannounced freeze" card.

Incident (2026-07-31, conv ms8bx7089s3268): a task hung 2.5h inside a
run_command with zero output. The user saw a hollow bubble — no phase text,
no finish tag — while ~1 event/6s kept arriving (all heartbeat self-ticks).
Nothing announced the freeze.

★★ REGIME SPLIT (owner ruling 2026-08-04, screenshot: a healthy 5min silent
tool execution flagged 「已停滞 · 静默 315s」): self-tick heartbeats only
EXIST while a tool is in flight (the backend ticker runs only while a tool
blocks), so "nothing but heartbeats" === "a tool is executing" — and silence
there is NORMAL (a find/grep over the FUSE mount legitimately runs minutes
with zero output; the tool row already counts "Running command… (Ns)" live;
the backend reaper owns the genuinely-wedged case at >30min with an explicit
error, pt_8524e0ec). The banner is therefore reserved for the TRUE freeze:
NO frames at all, not even a self-tick. This suite pins BOTH regimes.

Three seams, all pinned against the REAL shipped JS under jsdom (skips
cleanly when node+jsdom are absent):

  1. DETECTOR (ui/stall_watch.js): while heartbeat self-ticks FLOW, no
     banner however old the silence; when NOTHING arrives past the
     threshold the watch flags the task; a real event (or heartbeats
     resuming) self-heals; a terminal event tears the watch down. A
     replayed old frame seeds the silence floor (F5-safe) while its tick
     grants one flow-window of grace (no banner flash on re-attach).
  2. DETECTOR GATE (second harness, flow window neutered to 0): the SAME
     executing-tool feed MUST stall — proving regime A's suppression comes
     from the tick-flow gate, not from a broken detector.
  3. RENDER SEAM (ui/streaming_ui.js): a frozen stream paints the amber
     banner with a Stop affordance through the REAL updateStreamingUI —
     and an executing tool paints NOTHING.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_frontend_stall_watch.py -v
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


# ── Harness W: the detector itself (REAL stall_watch.js) ─────────────────
_HARNESS_WATCH = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win._STALL_WATCH_THRESHOLD_S = 300;
// (default flow window: 60s — fresh self-ticks mean "tool executing")
const calls = { twUpdate: 0, abortTask: [], abortConv: [] };
global.twUpdate = win.twUpdate = () => { calls.twUpdate++; };
global.Api = win.Api = { chat: {
  abortTask: (t) => { calls.abortTask.push(t); return Promise.resolve(); },
  abortConv: (c) => { calls.abortConv.push(c); return Promise.resolve(); },
} };
global.t = win.t = (k, p) => k + (p ? JSON.stringify(p) : '');
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};

// Bare eval (node scope) — globals resolve via `global`, window.* via `win`.
eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/stall_watch.js'), 'utf8'));

const NOW = Date.now();
const tid = 'task-stall-1';
const cid = 'conv-stall-1';
const out = [];
const S = () => win.stallWatchState(tid);

// ── Regime A (owner ruling 2026-08-04): a tool verifiably EXECUTING —
//    heartbeat self-ticks flowing — must NOT raise the banner, however old
//    the silence. A quiet find/grep over FUSE is normal; the tool row
//    already counts the seconds; the backend reaper owns the wedge case.
// 1. 500s of silence WITH fresh self-ticks → NOT stalled.
win.stallWatchFeed(cid, tid, { type: 'tool_start', emittedAt: NOW - 500000 });
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 15 });
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 30 });
win._stallWatchTick();
out.push(['tool_executing_no_banner', S().stalled === false]);

// 2. silentSecs stays honest — the watch still MEASURES (≈500s from the
//    tool_start emittedAt), it just doesn't alarm while ticks flow.
out.push(['silent_secs_honest', S().silentSecs >= 499 && S().silentSecs <= 510]);

// 3. A REAL output chunk mid-execution: still quiet, floor refreshed.
win.stallWatchFeed(cid, tid, { type: 'tool_progress', stream: 'stdout', chunk: 'x' });
out.push(['real_output_still_quiet', S().stalled === false]);

// 4. Self-ticks after the chunk: still quiet.
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 45 });
win._stallWatchTick();
out.push(['self_ticks_still_quiet', S().stalled === false]);

// 5. Terminal event tears the watch down.
win.stallWatchFeed(cid, tid, { type: 'done' });
out.push(['terminal_clears', S().stalled === false && S().silentSecs === 0]);

// ── Regime B (the preserved alarm): frames arrived, then NOTHING — not
//    even a heartbeat — past the threshold. The true unannounced freeze.
// 6. tool_start 500s ago, zero frames since → stalled.
const tid3 = 'task-stall-3';
win.stallWatchFeed(cid, tid3, { type: 'tool_start', emittedAt: NOW - 500000 });
win._stallWatchTick();
out.push(['no_frames_at_all_stalls', win.stallWatchState(tid3).stalled === true]);

// 7. The flip repainted (the banner paints without a new message).
out.push(['twupdate_on_flip', calls.twUpdate >= 1]);

// 8. Stop affordance on the frozen task: aborts the task + the conv, clears.
win.stallWatchStop(cid, tid3);
out.push(['stop_aborts', calls.abortTask.length === 1 && calls.abortTask[0] === tid3
                       && calls.abortConv.length === 1 && calls.abortConv[0] === cid]);
out.push(['stop_clears', win.stallWatchState(tid3).stalled === false]);

// 9. F5 grace: a replayed OLD self-tick seeds the silence floor from the
//    backend clock AND counts as a just-seen tick — re-attaching to a live
//    execution does not flash the banner while the next heartbeat is one
//    interval away.
const tid2 = 'task-stall-2';
win.stallWatchFeed(cid, tid2, { type: 'tool_progress', _selfTick: true,
                                emittedAt: NOW - 400000, elapsed: 6000 });
win._stallWatchTick();
out.push(['replay_grants_flow_grace', win.stallWatchState(tid2).stalled === false]);
out.push(['replay_seeds_floor', win.stallWatchState(tid2).silentSecs >= 399]);

let failed = 0;
for (const [name, ok] of out) {
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + name);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
"""


# ── Harness W2: the flow gate NEUTERED (window = 0) ─────────────────────
# NEUTER harness: with the flow window at 0 every tick counts as instantly
# stopped, so the SAME executing-tool feed as harness W MUST stall. This
# is what proves regime A's suppression is the tick-flow gate's doing —
# not a broken detector that silently NEVER fires. The replayed-floor pin
# (F5-safety) also lives here: with the window at 0 the seeded floor alone
# must cross the threshold.
_HARNESS_WATCH_GATE = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win._STALL_WATCH_THRESHOLD_S = 300;
win._STALL_WATCH_TICK_WINDOW_S = 0;   // every tick is INSTANTLY "stopped"
global.twUpdate = win.twUpdate = () => {};
global.Api = win.Api = { chat: { abortTask: () => Promise.resolve(), abortConv: () => Promise.resolve() } };
global.t = win.t = (k, p) => k + (p ? JSON.stringify(p) : '');
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};

eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/stall_watch.js'), 'utf8'));

const NOW = Date.now();
const cid = 'conv-gate-1';
const out = [];

// 1. GATE PROOF: byte-identical feed to harness W's regime-A case — old
//    silence + fresh self-ticks — but with the gate neutered the banner
//    FIRES. W's suppression therefore comes from _ticksFlowing.
const tid = 'task-gate-1';
win.stallWatchFeed(cid, tid, { type: 'tool_start', emittedAt: NOW - 500000 });
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 15 });
win._stallWatchTick();
out.push(['flow_gate_load_bearing', win.stallWatchState(tid).stalled === true]);

// 2. FLOOR: a replayed old self-tick seeds lastReal from the BACKEND clock
//    (F5-safety). Gate neutered ⇒ the seeded floor alone must stall.
const tid2 = 'task-gate-2';
win.stallWatchFeed(cid, tid2, { type: 'tool_progress', _selfTick: true,
                                emittedAt: NOW - 400000, elapsed: 6000 });
out.push(['replay_seeds_floor', win.stallWatchState(tid2).stalled === true]);

let failed = 0;
for (const [name, ok] of out) {
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + name);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
"""


# ── Harness R: the render seam (REAL updateStreamingUI) ──────────────────
_HARNESS_RENDER = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="streaming-msg"><div id="streaming-body">
    <div data-zone="tool"></div><div data-zone="thinking"></div>
    <div data-zone="content"></div><div data-zone="status"></div>
  </div></div></body>`, { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win._STALL_WATCH_THRESHOLD_S = 300;
global.twUpdate = win.twUpdate = () => {};
global.Api = win.Api = { chat: { abortTask: () => Promise.resolve(), abortConv: () => Promise.resolve() } };
global.t = win.t = (k, p) => (p && p.n != null) ? k + ':' + p.n : k;
global.escapeHtml = win.escapeHtml = (s) => String(s);
global.Icon = win.Icon = () => '';
global.renderMarkdown = win.renderMarkdown = (s) => s;
global.isNearBottom = win.isNearBottom = () => false;
global.scrollToBottom = win.scrollToBottom = () => {};
global.debugLog = win.debugLog = () => {};
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
const cid = 'conv-render-1';
global.conversations = win.conversations = [{ id: cid, activeTaskId: 'task-render-1', messages: [] }];
let _activeConvId = cid;
Object.defineProperty(global, 'activeConvId', { get: () => _activeConvId });
Object.defineProperty(win, 'activeConvId', { get: () => _activeConvId });

eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/stall_watch.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/streaming_ui.js'), 'utf8'));

const out = [];
const zone = win.document.querySelector('[data-zone="status"]');
const NOW = Date.now();

// ── Regime A (the owner-ruling scenario): a tool EXECUTING silently —
//    heartbeats flowing — paints NO banner even at 600s of silence.
win.stallWatchFeed(cid, 'task-render-1', { type: 'tool_start', emittedAt: NOW - 600000 });
win.stallWatchFeed(cid, 'task-render-1', { type: 'tool_progress', _selfTick: true, elapsed: 15 });
win._stallWatchTick();
updateStreamingUI({ content: 'partial', thinking: '', toolRounds: [] });
out.push(['tool_executing_no_banner', zone.innerHTML.indexOf('stream-phase-stalled') === -1]);

// ── Regime B: the TRUE freeze (frames, then NOTHING) paints the banner.
win.conversations[0].activeTaskId = 'task-render-2';
win.stallWatchFeed(cid, 'task-render-2', { type: 'tool_start', emittedAt: NOW - 600000 });
win._stallWatchTick();
updateStreamingUI({ content: 'partial', thinking: '', toolRounds: [] });
const html = zone.innerHTML;
out.push(['banner_painted', html.indexOf('stream-phase-stalled') !== -1]);
out.push(['banner_has_stop', html.indexOf('stream-stalled-stop') !== -1]);
out.push(['banner_shows_silence', html.indexOf('stream.stalled.banner:') !== -1]);
out.push(['phase_key_stalled', zone.getAttribute('data-phase-key') === 'stalled']);

// Self-heal: real output + repaint → banner gone.
win.stallWatchFeed(cid, 'task-render-2', { type: 'tool_progress', stream: 'stdout', chunk: 'y' });
updateStreamingUI({ content: 'partial more', thinking: '', toolRounds: [] });
const html2 = zone.innerHTML;
out.push(['banner_self_heals_off', html2.indexOf('stream-phase-stalled') === -1]);

let failed = 0;
for (const [name, ok] of out) {
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + name);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
"""


def _run_harness(harness: str) -> tuple[bool, str]:
    if not _node_deps_available():
        pytest.skip('node/jsdom not installed')
    probe = os.path.join(ROOT, 'node_modules', '.tmp_stall_watch_harness.js')
    with open(probe, 'w', encoding='utf-8') as f:
        f.write(harness)
    try:
        p = subprocess.run(['node', probe, ROOT], capture_output=True,
                           text=True, timeout=120)
    finally:
        os.unlink(probe)
    output = (p.stdout + p.stderr)
    return (p.returncode == 0), output


@pytest.mark.unit
class TestStallWatchDetector:
    def test_detector_contract(self):
        ok, output = _run_harness(_HARNESS_WATCH)
        assert ok, 'stall-watch detector harness failures:\n' + output

    def test_flow_gate_is_load_bearing(self):
        """NEUTER proof: flow window 0 ⇒ the executing-tool feed stalls.

        Without this pair, a regression that breaks the DETECTOR (never
        fires at all) would keep harness W green — regime A's 'no banner'
        assertions pass trivially when the watch is dead. The gate must be
        what suppresses the banner, and this harness proves it.
        """
        ok, output = _run_harness(_HARNESS_WATCH_GATE)
        assert ok, 'stall-watch flow-gate harness failures:\n' + output


@pytest.mark.unit
class TestStallWatchRenderSeam:
    def test_banner_paints_and_self_heals(self):
        ok, output = _run_harness(_HARNESS_RENDER)
        assert ok, 'stall-watch render-seam harness failures:\n' + output


@pytest.mark.unit
class TestStallWatchWiringRatchets:
    """Source-scan wiring pins (the 'anchor the behaviour-bearing text'
    family): if a seam is stripped, the ratchet goes red even when every
    behavioural harness above was itself neutered."""

    def test_feed_seam_present_in_dispatch(self):
        src = open(os.path.join(ROOT, 'static/js/ui/sse_pipeline.js'),
                   encoding='utf-8').read()
        assert 'stallWatchFeed(convId, taskId, ev)' in src, (
            'dispatchSSEEvent lost the stall-watch feed seam — the detector '
            'goes blind while staying green')

    def test_render_seam_present_in_streaming_ui(self):
        src = open(os.path.join(ROOT, 'static/js/ui/streaming_ui.js'),
                   encoding='utf-8').read()
        assert 'stallWatchState(_swTaskId)' in src
        assert 'stream.stalled.banner' in src
        assert 'stallWatchStop(activeConvId' in src

    def test_flow_gate_present_in_detector(self):
        """The regime split (owner ruling 2026-08-04) is load-bearing source:
        a 'simplification' that drops the tick-flow gate re-opens the exact
        noise this ruling removed — and harness W's no-banner assertions
        would still pass if the detector were merely dead, so pin the gate."""
        src = open(os.path.join(ROOT, 'static/js/ui/stall_watch.js'),
                   encoding='utf-8').read()
        assert '_TICK_FLOW_WINDOW_S' in src, (
            'stall_watch.js lost the tick-flow window — the regime split is gone')
        assert '_ticksFlowing' in src, (
            'stall_watch.js lost the tick-flow gate — the banner fires during '
            'healthy command execution again (the 2026-08-04 ruling)')

    def test_i18n_keys_shipped(self):
        src = open(os.path.join(ROOT, 'static/js/i18n.js'),
                   encoding='utf-8').read()
        for key in ("'stream.stalled.banner'", "'stream.stalled.stop'"):
            assert key in src, f'{key} missing from i18n.js'

    def test_css_class_shipped(self):
        src = open(os.path.join(ROOT, 'static/styles.css'),
                   encoding='utf-8').read()
        assert '.stream-phase-stalled' in src
        assert '.stream-stalled-stop' in src

    def test_bundle_registers_module(self):
        src = open(os.path.join(ROOT, 'lib/js_bundler.py'),
                   encoding='utf-8').read()
        assert "'ui/stall_watch.js'" in src, (
            'stall_watch.js is not in _BUNDLE_FILES — the served bundle '
            'never contains the detector (the _BUNDLE_FILES freeze lesson)')
