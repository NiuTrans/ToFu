"""Regression: the per-frame streaming render path (`_twFlush`) and the
cross-tab visibility flush must NOT paint the "等待中…" (Waiting…) placeholder
over already-checkpointed content.

WHY (post §7 streamBufs RETIREMENT)
-----------------------------------
The global `streamBufs` Map was DELETED. Content / thinking / toolRounds now
project straight from the message document (the trailing streaming assistant
message is the single source of truth). `phase` lives in the live session
slice (`streamSessions` in static/js/ui/stream_session.js). The shared
`_streamFrameArg(convId)` helper builds the `updateStreamingUI` payload reading
the DOCUMENT + session.phase, and BOTH `_twFlush` and the cross-tab
visibilitychange flush route through it.

Pre-§7 the bug was: `_twFlush` read `buf.content` RAW with no
message-checkpoint fallback, so an empty buffer wiped the persisted English
to "等待中…". Post-§7 the fallback IS the only path — `_streamFrameArg` reads
the document directly, so there is no "empty buffer" case to fall back FROM;
the document is always the source. This test now proves the paint path cannot
paint stale/empty over checkpointed content by asserting the frame EQUALS the
document, and that mutating the document changes the frame (the NEUTER against
the doc source).

This drives the REAL shipped `_twFlush` + `updateStreamingUI` under jsdom and
asserts:
  (1) `_streamFrameArg` with a live session + checkpointed doc projects the
      document content/thinking/toolRounds verbatim.
  (2) The full `_twFlush` frame with a live session + a checkpointed trailing
      assistant renders the real content and NO "等待中…" chip.
  (3) A GENUINELY empty stream (empty doc — e.g. right after twStart before any
      content) still shows "等待中…" (no false render).
  (4) NEUTER: mutating the document changes the frame — the paint path reads
      the doc, not a stale snapshot.
  (5) No live session → _streamFrameArg returns null (no throw).

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
// Render synchronously: rAF/timeout fire immediately so _twFlush renders now.
// rAF stub is NON-recursive: it records the pending callback but does NOT
// invoke it inline (the real rAF is async). Tests drive _twFlush() directly.
global.requestAnimationFrame = win.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = win.cancelAnimationFrame = () => {};
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
global.setTimeout = win.setTimeout = () => 0;   // _twFlush's own timeout fallback — inert
global.clearTimeout = win.clearTimeout = () => {};
// Ever-advancing clock so _twFlush's <33ms rate-cap never reschedules.
let _perfNow = 0;
global.performance = win.performance = { now: () => (_perfNow += 1000) };
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

// updateStreamingUI's deterministic no-op deps.
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

// getToolRoundsFromMsg is defined in core.js — provide the REAL contract.
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = function (msg) {
  if (msg && msg.toolRounds && msg.toolRounds.length > 0) return msg.toolRounds;
  return [];
};

// Health-timer deps (twStop / _setBubbleLiveness paths touch these).
win.showToast = global.showToast = () => {};
win.normalizeErrorEnvelope = global.normalizeErrorEnvelope = (e) => e;
win.Api = global.Api = { health: { check: async () => ({ ok: true }) },
                         chat: { poll: async () => ({ ok: true, status: 'running' }) } };
win._startOfflineRecoveryPolling = global._startOfflineRecoveryPolling = () => {};
win.saveConversations = global.saveConversations = () => {};
win.renderChat = global.renderChat = () => {};
win.renderConversationList = global.renderConversationList = () => {};
win.ConvCache = global.ConvCache = { put: () => {} };
win.AbortSignal = global.AbortSignal || { timeout: () => undefined };

win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [];
global.activeConvId = win.activeConvId = null;

// ── §7 streamBufs RETIREMENT: uniform session stub ──
win.streamSessions = global.streamSessions = new Map();
win.getStreamSession = global.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };

// Load the REAL shipped render funnel, in bundle order.
eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/streaming_ui.js  (updateStreamingUI)
eval(fs.readFileSync(process.argv[4], 'utf8'));  // core/health_stream_timer.js (_twFlush, _streamFrameArg, twStart, twUpdate)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _streamFrameArg !== 'function') { console.log('FAIL _streamFrameArg_exposed'); process.exit(0); }
if (typeof _twFlush !== 'function') { console.log('FAIL _twFlush_exposed'); process.exit(0); }
if (typeof updateStreamingUI !== 'function') { console.log('FAIL updateStreamingUI_exposed'); process.exit(0); }
check('symbols_exposed', true);

function _freshBody() {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body"></div></div>';
}
function _probe() {
  const body = document.getElementById('streaming-body');
  const contentZone = body ? body.querySelector('[data-zone="content"]') : null;
  const statusZone = body ? body.querySelector('[data-zone="status"]') : null;
  return {
    contentHtml: contentZone ? contentZone.innerHTML : '',
    hasWait: !!(statusZone && /stream-status/.test(statusZone.innerHTML)
                && /等待中…/.test(statusZone.innerHTML)),
  };
}
// §7: seed the DOCUMENT (assistant message) as the source + create a live
// session via getStreamSession (mirrors twStart). No streamBufs.
function _setup(convId, msgFields) {
  conversations.length = 0;
  streamSessions.clear();
  activeStreams.clear();
  const conv = { id: convId, activeTaskId: 't1', messages: [
    { role: 'user', content: 'go' },
    Object.assign({ role: 'assistant', content: '', thinking: '', toolRounds: [] }, msgFields || {}),
  ]};
  conversations.push(conv);
  getStreamSession(convId);   // create the live session (twStart's job)
  activeStreams.set(convId, { controller: { abort: () => {} }, taskId: 't1' });
  global.activeConvId = win.activeConvId = convId;
}

// ── (1) _streamFrameArg: live session + checkpointed doc → projects the doc. ──
{
  _setup('c-fallback', { content: 'checkpoint EN answer', thinking: 'ck think',
                         toolRounds: [{ roundNum: 1, status: 'done' }] });
  const arg = _streamFrameArg('c-fallback');
  check('frameArg_content_fallback', arg && arg.content === 'checkpoint EN answer');
  check('frameArg_thinking_fallback', arg && arg.thinking === 'ck think');
  check('frameArg_toolRounds_fallback', arg && arg.toolRounds && arg.toolRounds.length === 1);
}

// ── (2) THE FIX: _twFlush with a live session + checkpointed doc renders the
//    real content and does NOT paint 等待中…. ──
{
  _setup('c-twflush', { content: 'partial EN answer so far', thinking: '', toolRounds: [] });
  _freshBody();
  _twPendingConvId = 'c-twflush'; _twDirty = true;
  _twFlush();   // the REAL per-frame render path, driven directly
  const r = _probe();
  check('twflush_no_wait_over_checkpoint', r.hasWait === false);
  check('twflush_renders_checkpoint', /partial EN answer so far/.test(r.contentHtml));
}

// ── (3) Genuinely empty stream (empty doc) still shows 等待中… — the
//    projection must NOT fabricate content. ──
{
  _setup('c-empty', { content: '', thinking: '', toolRounds: [] });
  _freshBody();
  _twPendingConvId = 'c-empty'; _twDirty = true;
  _twFlush();
  const r = _probe();
  check('empty_stream_still_waits', r.hasWait === true && r.contentHtml === '');
}

// ── (4) NEUTER: the paint path reads the DOCUMENT, not a stale snapshot.
//    Mutate the doc and the frame MUST change — proving no stale buffer can
//    override the checkpointed content. ──
{
  _setup('c-live', { content: 'first content', thinking: '', toolRounds: [] });
  const arg1 = _streamFrameArg('c-live');
  check('frame_reads_doc_initial', arg1 && arg1.content === 'first content');
  // Mutate the document (the SSE delta path does this in production).
  const conv = conversations.find(c => c.id === 'c-live');
  conv.messages[1].content = 'LIVE delta so far';
  const arg2 = _streamFrameArg('c-live');
  check('frame_tracks_doc_mutation', arg2 && arg2.content === 'LIVE delta so far');
  _freshBody();
  _twPendingConvId = 'c-live'; _twDirty = true;
  _twFlush();
  const r = _probe();
  check('twflush_renders_mutated_doc', /LIVE delta so far/.test(r.contentHtml)
        && !/first content/.test(r.contentHtml));
}

// ── (5) No live session → _streamFrameArg returns null (no throw). ──
{
  conversations.length = 0; streamSessions.clear();
  check('no_session_null', _streamFrameArg('nope') === null);
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_twflush_msg_fallback_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             ROOT,                                                    # argv[2]
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),          # argv[3]
             os.path.join(JS_DIR, 'core', 'health_stream_timer.js'),  # argv[4]
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
    assert not fails, '_twFlush message-fallback failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'

    # ── Source-level guard: _twFlush must go through _streamFrameArg; and
    #    _streamFrameArg must project content from the document (ckpt). ──
    hst = os.path.join(JS_DIR, 'core', 'health_stream_timer.js')
    with open(hst, encoding='utf-8') as f:
        hst_src = f.read()
    assert 'function _streamFrameArg' in hst_src, (
        'regression: the shared _streamFrameArg render-payload helper is gone.')
    assert '(ckpt && ckpt.content)' in hst_src, (
        'regression: _streamFrameArg no longer projects content from the '
        'message document — an empty projection would wipe the bubble to "等待中…".')
    # _twFlush body must call the helper.
    fpos = hst_src.find('function _twFlush')
    assert fpos >= 0
    fbody = hst_src[fpos:fpos + 2600]
    assert '_streamFrameArg(renderCid)' in fbody, (
        'regression: _twFlush no longer routes through _streamFrameArg — the raw '
        'buf.content read that wiped checkpointed content back to "等待中…" is back.')

    # ── Source-level guard: cross-tab visibility flush also uses the fallback. ──
    cts = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
    with open(cts, encoding='utf-8') as f:
        cts_src = f.read()
    assert '_streamFrameArg(activeConvId)' in cts_src, (
        'regression: the cross-tab visibilitychange flush no longer applies the '
        'message-checkpoint fallback — switching a tab back into a mid-stream '
        'conv whose buffer is empty would paint "等待中…" over the checkpoint.')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_twflush_msg_fallback():
    _run()
