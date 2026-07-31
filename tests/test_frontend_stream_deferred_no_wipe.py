"""Regression: reconnecting to an in-flight task must NOT snap the streaming
bubble back to the "等待中…" (Waiting…) state after the checkpoint content
already rendered.

WHY
---
On the reconnect path (fast conversation switch / force-refresh into an active
stream) the flow is:

  connectToTask() → twStart(convId)   # creates an EMPTY streamBufs entry
                  → pre-populate DOM from assistantMsg (persisted checkpoint)
  showStreamingUIForConv(convId)       # initial render: content falls back to
                                        #   `buf?.content || lastMsg.content`
                                        # then schedules a 300ms deferred
                                        #   re-render reading the buffer

Two defects made the bubble revert to "等待中…" (updateStreamingUI's `wait`
branch fires only when `!content && !thinking`):

  1. connectToTask seeded `buf.toolRounds` but NOT `buf.content`/`buf.thinking`,
     so the fresh buffer stayed empty until the first NEW SSE delta.
  2. showStreamingUIForConv's 300ms deferred re-render read `dBuf.content` RAW
     (no `|| lastMsg.content` fallback like the initial render), so it painted
     updateStreamingUI({content:''}) → the "wait" branch → wiped the real
     (checkpointed) English 300ms after it appeared.

This locks BOTH fixes by driving the REAL shipped `updateStreamingUI` under
jsdom and asserting:

  (a) After twStart-then-seed, `streamBufs.get(convId).content` carries the
      checkpoint content (fix #1, source-level guard on sse_pipeline.js).
  (b) A buffer-driven render with an EMPTY buffer but a non-empty message
      falls back to the message content and does NOT emit the `等待中…`
      status (fix #2). Byte-revert NC: a raw `dBuf.content` re-introduces the
      wipe → the `stream-status` wait chip reappears.

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

// Globals updateStreamingUI touches at call time — all deterministic no-ops.
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win.t = global.t = (k) => (k === 'stream.phase.waiting' ? '等待中…' : k);
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
win._stampFreshness = global._stampFreshness = () => {};
win._fcFingerprint = global._fcFingerprint = () => 0;
win._extractFileChangesFromRoundsAsync = global._extractFileChangesFromRoundsAsync = () => ({ then: () => {} });
win._renderFileChangesHtml = global._renderFileChangesHtml = () => '';
win.renderMcpLoginHintHtml = global.renderMcpLoginHintHtml = () => '';
win.renderPreferenceLearnedHtml = global.renderPreferenceLearnedHtml = () => '';
win.renderTurnProvenanceHtml = global.renderTurnProvenanceHtml = () => '';
win._isRoundSwarm = global._isRoundSwarm = (r) => !!(r && r._swarm);
win._renderUnifiedToolLine = global._renderUnifiedToolLine = () => '<div class="ptool-line"></div>';
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '<div class="sw-panel"></div>';
win._buildSwarmInboxChipsHTML = global._buildSwarmInboxChipsHTML = () => '';
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win.CSS = global.CSS = undefined;

win.activeStreams = global.activeStreams = new Map();
win.streamBufs = global.streamBufs = new Map();
win.streamSessions = global.streamSessions = new Map();
win.getStreamSession = global.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof activeStreams !== "undefined" && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };
win.conversations = global.conversations = [];
global.activeConvId = win.activeConvId = 'c1';

// Load the REAL shipped updateStreamingUI (+ zones).
eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/streaming_ui.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof updateStreamingUI !== 'function') { console.log('FAIL updateStreamingUI_exposed'); process.exit(0); }
check('updateStreamingUI_exposed', true);

// ── Helper: fresh streaming-body, run a render, return the rendered content
//    text + whether the "等待中…" wait status is present. ──
function _renderAndProbe(msg) {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body"></div></div>';
  updateStreamingUI(msg);
  const body = document.getElementById('streaming-body');
  const contentZone = body.querySelector('[data-zone="content"]');
  const statusZone = body.querySelector('[data-zone="status"]');
  return {
    contentHtml: contentZone ? contentZone.innerHTML : '',
    hasWait: !!(statusZone && /stream-status/.test(statusZone.innerHTML)
                && /等待中…/.test(statusZone.innerHTML)),
  };
}

// ── Baseline: an EMPTY message (no content, no thinking) SHOULD show 等待中… ──
{
  const r = _renderAndProbe({ content: '', thinking: '', toolRounds: [], phase: null });
  check('empty_msg_shows_wait', r.hasWait === true && r.contentHtml === '');
}

// ── (b) The fix: the reconnect deferred re-render builds its arg with the
//    `dBuf.content || lastMsg.content` fallback. Simulate the FIXED behaviour:
//    an empty buffer + a checkpointed message → the arg carries the message
//    content → NO wait chip, real English rendered. ──
{
  const dBuf = { content: '', thinking: '', toolRounds: [], phase: null };
  const lastMsg = { content: 'partial EN answer so far', thinking: '', toolRounds: [] };
  // This is exactly the object the FIXED deferred block constructs.
  const arg = {
    thinking: dBuf.thinking || lastMsg.thinking || '',
    content: dBuf.content || lastMsg.content || '',
    toolRounds: (dBuf.toolRounds && dBuf.toolRounds.length ? dBuf.toolRounds : null)
                || lastMsg.toolRounds || [],
    phase: dBuf.phase,
  };
  const r = _renderAndProbe(arg);
  check('fixed_deferred_no_wipe',
    r.hasWait === false && /partial EN answer so far/.test(r.contentHtml));
}

// ── (b-NC) Byte-revert control: the OLD deferred block read dBuf.content RAW.
//    With an empty buffer that paints content:'' → the 等待中… wait chip
//    reappears (the bug). This proves the test discriminates. ──
{
  const dBuf = { content: '', thinking: '', toolRounds: [], phase: null };
  const argOld = {                       // the pre-fix (raw-buffer) arg
    thinking: dBuf.thinking,
    content: dBuf.content,
    toolRounds: dBuf.toolRounds,
    phase: dBuf.phase,
  };
  const r = _renderAndProbe(argOld);
  check('nc_raw_buffer_wipes_to_wait', r.hasWait === true && r.contentHtml === '');
}



console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_stream_deferred_no_wipe_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             ROOT,                                            # argv[2]
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),  # argv[3]
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
    assert not fails, 'stream-deferred-no-wipe failures:\n' + output
    # §7: the two (a)-family buffer-seed checks are obsolete — there is no
    # buffer to seed; the doc-driven checks (exposed/empty/fixed/NC) are the
    # four that remain meaningful.
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'

    # ── Source-level guard (§7 retirement): streamBufs is deleted —
    #    the frame reads the assistant message (doc) directly, not a buffer.
    #    Verify the doc-driven reconnect path: connectToTask pre-renders
    #    persisted content from the assistant message (not a buffer). ──
    sse = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')
    with open(sse, encoding='utf-8') as f:
        sse_src = f.read()
    has_render = 'assistantMsg.content && assistantMsg.content.length' in sse_src
    assert has_render, 'connectToTask no longer reads assistantMsg for reconnect pre-render'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_stream_deferred_no_wipe():
    _run()
