"""tests/test_frontend_stall_watch.py — the "no unannounced freeze" card.

Incident (2026-07-31, conv ms8bx7089s3268): a task hung 2.5h inside a
run_command with zero output. The user saw a hollow bubble — no phase text,
no finish tag — while ~1 event/6s kept arriving (all heartbeat self-ticks).
Nothing announced the freeze.

The fix has three seams, all pinned here against the REAL shipped JS under
jsdom (skips cleanly when node+jsdom are absent):

  1. DETECTOR (ui/stall_watch.js): self-tick frames NEVER refresh the
     liveness floor; real output does; past the threshold the watch flags
     the task; a real event self-heals; a terminal event tears the watch
     down. Replayed frames (backend emittedAt) seed the floor — F5-safe.
  2. FEED SEAM (ui/sse_pipeline.js): every dispatched event reaches
     stallWatchFeed — a ratchet + a behavioral check via the dispatcher's
     own __sse_test__ seam would both catch a stripped feed.
  3. RENDER SEAM (ui/streaming_ui.js): a stalled watch paints the amber
     banner with a Stop affordance into the status zone — driven here
     through the REAL updateStreamingUI.

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

// 1. Self-ticks ONLY, clock aged past threshold → stalled.
win.stallWatchFeed(cid, tid, { type: 'tool_start', emittedAt: NOW - 500000 });
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 15 });
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 30 });
win._stallWatchTick();
out.push(['self_ticks_only_stalls', S().stalled === true]);

// 2. silentSecs is honest (≈500s from the tool_start emittedAt).
out.push(['silent_secs_honest', S().silentSecs >= 499 && S().silentSecs <= 510]);

// 3. A REAL output chunk self-heals the card.
win.stallWatchFeed(cid, tid, { type: 'tool_progress', stream: 'stdout', chunk: 'x' });
out.push(['real_output_self_heals', S().stalled === false]);

// 4. Self-ticks after the heal do NOT re-stall immediately.
win.stallWatchFeed(cid, tid, { type: 'tool_progress', _selfTick: true, elapsed: 45 });
win._stallWatchTick();
out.push(['self_tick_no_instant_restall', S().stalled === false]);

// 5. Terminal event tears the watch down.
win.stallWatchFeed(cid, tid, { type: 'done' });
out.push(['terminal_clears', S().stalled === false && S().silentSecs === 0]);

// 6. F5 shape: replay whose FIRST frame is a self-tick with an old
//    backend clock — the floor seeds from emittedAt (not from now), so the
//    stall base survives the reload.
const tid2 = 'task-stall-2';
win.stallWatchFeed(cid, tid2, { type: 'tool_progress', _selfTick: true,
                                emittedAt: NOW - 400000, elapsed: 6000 });
out.push(['replay_seeds_floor', win.stallWatchState(tid2).stalled === true]);

// 7. Stop affordance: aborts the task + the conv, clears the watch.
win.stallWatchStop(cid, tid2);
out.push(['stop_aborts', calls.abortTask.length === 1 && calls.abortTask[0] === tid2
                       && calls.abortConv.length === 1 && calls.abortConv[0] === cid]);
out.push(['stop_clears', win.stallWatchState(tid2).stalled === false]);

// 8. twUpdate fired on the flip (the banner paints without a new message).
out.push(['twupdate_on_flip', calls.twUpdate >= 1]);

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

// Stall the task, then drive the REAL updateStreamingUI (declared by the
// eval above, same harness scope).
const NOW = Date.now();
win.stallWatchFeed(cid, 'task-render-1', { type: 'tool_start', emittedAt: NOW - 600000 });
win.stallWatchFeed(cid, 'task-render-1', { type: 'tool_progress', _selfTick: true, elapsed: 15 });
win._stallWatchTick();
updateStreamingUI({ content: 'partial', thinking: '', toolRounds: [] });

const zone = win.document.querySelector('[data-zone="status"]');
const html = zone ? zone.innerHTML : '';
const out = [];
out.push(['banner_painted', html.indexOf('stream-phase-stalled') !== -1]);
out.push(['banner_has_stop', html.indexOf('stream-stalled-stop') !== -1]);
out.push(['banner_shows_silence', html.indexOf('stream.stalled.banner:') !== -1]);
out.push(['phase_key_stalled', zone.getAttribute('data-phase-key') === 'stalled']);

// Self-heal: real output + repaint → banner gone.
win.stallWatchFeed(cid, 'task-render-1', { type: 'tool_progress', stream: 'stdout', chunk: 'y' });
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
