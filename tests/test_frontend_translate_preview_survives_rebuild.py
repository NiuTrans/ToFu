"""Regression: the live per-round translation preview must survive a
mid-stream full re-render of the streaming bubble.

WHY
---
The incremental translator pushes a `running`/`partial` frame after each tool
round's prose segment is translated; `_renderStreamingTranslatePreview`
(translation.js) paints it into the `#streaming-msg` bubble, routed by the
bubble's `data-msg-id`. The bubble is initially stamped with the assistant
message's `_msgId` (send/regen path), but EVERY other bubble-rebuild path
(`showStreamingUIForConv`, the SSE reconnect paths) historically called
`_streamingBubbleHTML` WITHOUT the 4th `msgId` arg. So the moment any code
triggered a full re-render during streaming (a translate toggle, KaTeX load,
`_resumePendingTranslations` → `renderChat`, …) the rebuilt bubble lost its
`data-msg-id`, and `_renderStreamingTranslatePreview` early-returned forever
after — the Chinese stopped filling in until the task ended, even though the
backend kept translating and committed correctly at the end. That is the
"md-content keeps growing but translation never appears mid-stream" symptom.

This harness loads the REAL shipped JS under jsdom and locks three contracts:

  (a) After `showStreamingUIForConv` rebuilds the bubble, a partial frame for
      the streaming message's `_msgId` PAINTS (was: dropped, data-msg-id null).
  (b) `showStreamingUIForConv` itself repaints the stashed `_translatePartial`
      immediately on rebuild — the Chinese-so-far survives WITHOUT waiting for
      the next push frame (closes the 20-40s blank window between rounds).
  (c) The rebuilt `#streaming-msg`'s `data-msg-id` equals the message's
      `_msgId` for the worker / planner / critic branches.
  (d) The `connectToTask` reconnect path (sse_pipeline.js) — which rebuilds
      `#streaming-body` via `_body.innerHTML = _html` on page-reload-into-
      active-stream — ALSO repaints the stashed `_translatePartial` after the
      rebuild, same as (b). Same bug-class, second code path. Guarded both at
      the SOURCE level (the shipped reconnect block must call the repaint) and
      at RUNTIME (a reconnect-style rebuilt body repaints the stash with no
      new frame).

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.

ZONE NOTE (2026-07-07 auto-translate unification): the live Chinese-so-far's
canonical paint target is the `translatedPrimary` zone (streaming_ui.js seeds
it in every rebuilt body); the legacy `translatePreview` slot is still seeded
but kept only as a degrade target for zone-less bodies (the baseline + reconnect
paths below). Zone queries therefore go through _previewZone(): translatedPrimary
first, translatePreview fallback — and one explicit pin
(rebuild_paints_into_translatedPrimary) locks the modern canonical routing so
the helper can't mask a future re-route.
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

// ── Globals the streaming-UI + translation code touch at call time ──
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// formatClockTime (core.js) — new shared dep of showStreamingUIForConv +
// _streamingBubbleHTML (Commit-2 dedup). Deterministic stub.
win.formatClockTime = global.formatClockTime = () => '12:00';
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => s;
win.t = global.t = (k, o) => k + (o && o.n != null ? (':' + o.n) : '');
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
// safeHtml tagged-template + raw() — _streamingBubbleHTML uses them.
win.raw = global.raw = (s) => ({ __raw: String(s) });
function _safeHtml(strings, ...vals) {
  let out = '';
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < vals.length) {
      const v = vals[i];
      out += (v && v.__raw !== undefined) ? v.__raw : global.escapeHtml(v);
    }
  }
  return { toString: () => out };
}
win.safeHtml = global.safeHtml = _safeHtml;
// branding SVG constants referenced by _streamingBubbleHTML
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<svg></svg>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<svg></svg>';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg></svg>';

// Cluster-C externals showStreamingUIForConv touches — all no-ops.
win.activeStreams = global.activeStreams = new Map();
win.streamBufs = global.streamBufs = new Map();
win.buildTurnNav = global.buildTurnNav = () => {};
win._forceScrollToBottom = global._forceScrollToBottom = () => {};
win.updateSendButton = global.updateSendButton = () => {};
win._destroyLazyObserver = global._destroyLazyObserver = () => {};
win._ensureLazyObserver = global._ensureLazyObserver = () => {};
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
// `_INITIAL_RENDER` is a top-level `const` in streaming_render.js. Under eval
// in this harness's function scope it does NOT leak to the global the way the
// real concatenated bundle shares it, so showStreamingUIForConv (which reads
// it) would throw ReferenceError. Predefine the global with the shipped value.
global._INITIAL_RENDER = win._INITIAL_RENDER = 20;
// `_lazyConvId` is a top-level `let` in streaming_render.js:794 — same eval
// scoping issue: stream_lifecycle.js reads it at :39 and crashes without the
// shared global the bundle provides. Predefine with the shipped initial value.
global._lazyConvId = win._lazyConvId = null;

// Load the REAL shipped JS in dependency order (shared window scope).
eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/streaming_ui.js (zones + updateStreamingUI)
eval(fs.readFileSync(process.argv[4], 'utf8'));  // ui/streaming_render.js (_streamingBubbleHTML + _INITIAL_RENDER + renderMessage helpers)
eval(fs.readFileSync(process.argv[5], 'utf8'));  // ui/stream_lifecycle.js (showStreamingUIForConv)
eval(fs.readFileSync(process.argv[6], 'utf8'));  // translation.js (engine)
eval(fs.readFileSync(process.argv[7], 'utf8'));  // ui/translation_render.js (_renderStreamingTranslatePreview — relocated)

// renderMessage is defined in chat_render.js (not loaded). showStreamingUIForConv
// calls it for prior messages — stub a minimal version AFTER the evals.
win.renderMessage = global.renderMessage = (m, i) =>
  '<div class="message" id="msg-' + i + '">' + global.escapeHtml((m && m.content) || '') + '</div>';

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const fn = (typeof _renderStreamingTranslatePreview === 'function')
  ? _renderStreamingTranslatePreview : win._renderStreamingTranslatePreview;
if (typeof fn !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
if (typeof showStreamingUIForConv !== 'function') { console.log('FAIL showStreamingUIForConv_exposed'); process.exit(0); }
check('fns_exposed', true);

const MSGID = 'tmp_d3e2c72c';
global.activeConvId = win.activeConvId = 'c1';

// Canonical-first zone lookup: the 2026-07-07 unification paints the live
// Chinese into data-zone="translatedPrimary"; data-zone="translatePreview" is
// the legacy degrade slot (zone-less bodies only).
function _previewZone(root) {
  return root.querySelector('[data-zone="translatedPrimary"] .md-content')
      || root.querySelector('[data-zone="translatePreview"] .md-content');
}

// ── Baseline sanity: a bubble WITH matching data-msg-id paints. ──
const inner = document.getElementById('chatInner');
inner.innerHTML = '<div class="message" id="streaming-msg" data-msg-id="' + MSGID + '">'
  + '<div class="message-body" id="streaming-body"></div></div>';
const basePaint = fn('c1', MSGID, 'baseline 译文');
// Baseline pins the LEGACY degrade path: the hand-stamped bubble has no zones,
// so fn must create + paint translatePreview.
const _baseZone = document.querySelector('#streaming-msg [data-zone="translatePreview"] .md-content');
check('baseline_paints', basePaint === true
  && !!_baseZone && /baseline 译文/.test(_baseZone.innerHTML));

// ── (a)+(c): showStreamingUIForConv rebuilds the bubble; it must KEEP the
//    msgId, and a subsequent partial frame must paint. ──
function _mkConv(role) {
  // role drives which streaming-bubble branch fires.
  const assistant = { role: 'assistant', content: 'partial EN answer so far', _msgId: MSGID };
  if (role === 'planner') assistant._isEndpointPlanner = true;
  const msgs = [{ role: 'user', content: 'q', _msgId: 'u1' }];
  if (role === 'critic') {
    msgs.push({ role: 'user', content: 'crit', _msgId: MSGID, _isEndpointReview: true });
  } else {
    msgs.push(assistant);
  }
  return { id: 'c1', messages: msgs };
}

for (const role of ['worker', 'planner', 'critic']) {
  const conv = _mkConv(role);
  global.conversations = win.conversations = [conv];
  global.activeStreams = win.activeStreams = new Map([['c1', { toolRounds: [] }]]);
  global.streamBufs = win.streamBufs = new Map([['c1', { content: 'partial EN answer so far', thinking: '', toolRounds: [], phase: null }]]);
  document.getElementById('chatInner').innerHTML = '';

  showStreamingUIForConv('c1');

  const sm = document.getElementById('streaming-msg');
  check('rebuild_has_msgid_' + role, !!sm && sm.getAttribute('data-msg-id') === MSGID);

  // After rebuild, a partial frame for this msgId must paint (the bug: it didn't).
  const painted = fn('c1', MSGID, role + ' 第N轮译文');
  const zone = sm && _previewZone(sm);
  check('rebuild_partial_paints_' + role, painted === true
    && !!zone && new RegExp(role + ' 第N轮译文').test(zone.innerHTML));
  // The modern canonical routing: the paint must land in translatedPrimary
  // (the rebuilt body is zone-seeded), not the legacy translatePreview slot.
  const primary = sm && sm.querySelector('[data-zone="translatedPrimary"] .md-content');
  check('rebuild_paints_into_translatedPrimary_' + role, !!primary
    && new RegExp(role + ' 第N轮译文').test(primary.innerHTML));
}

// ── (b): showStreamingUIForConv repaints the stashed _translatePartial on
//    rebuild WITHOUT any new push frame. ──
{
  const conv = _mkConv('worker');
  conv.messages[1]._translatePartial = '已翻译到这里的译文';   // stash on the assistant msg
  global.conversations = win.conversations = [conv];
  global.activeStreams = win.activeStreams = new Map([['c1', { toolRounds: [] }]]);
  global.streamBufs = win.streamBufs = new Map([['c1', { content: 'partial EN answer so far', thinking: '', toolRounds: [], phase: null }]]);
  document.getElementById('chatInner').innerHTML = '';

  showStreamingUIForConv('c1');   // NO fn() call afterwards — rebuild must repaint on its own

  const zone = _previewZone(document.getElementById('streaming-msg'));
  check('rebuild_repaints_stashed_partial', !!zone && /已翻译到这里的译文/.test(zone.innerHTML));
}

// ── (d) connectToTask reconnect: the shipped reconnect block rebuilds
//    #streaming-body via `_body.innerHTML = _html` then repaints the stashed
//    _translatePartial. We reproduce that EXACT DOM sequence here (real
//    _streamingBubbleHTML stamps the msgId; real _renderStreamingTranslate
//    Preview does the repaint) and assert the Chinese survives with NO new
//    frame. The source-level guard in the pytest body proves the shipped
//    reconnect block actually performs this repaint (negative control). ──
{
  const assistantMsg = { role: 'assistant', content: 'resumed EN content', _msgId: MSGID,
                         _translatePartial: '重连后已恢复的译文' };
  global.conversations = win.conversations = [{ id: 'c1', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' }, assistantMsg ] }];
  const inner2 = document.getElementById('chatInner');
  inner2.innerHTML = '';
  // Mirror sse_pipeline.js:285 — stamp the rebuilt bubble with the msgId.
  inner2.insertAdjacentHTML('beforeend',
    _streamingBubbleHTML('worker', 'Resuming…', '12:00', assistantMsg._msgId || null));
  // Mirror sse_pipeline.js:305 — wipe + pre-populate the body.
  const _body = document.getElementById('streaming-body');
  _body.innerHTML = '<div class="md-content">' + renderMarkdown(assistantMsg.content) + '</div>'
    + '<div class="stream-status"><div class="pulse"></div> Resuming…</div>';
  check('recon_bubble_has_msgid',
    document.getElementById('streaming-msg').getAttribute('data-msg-id') === MSGID);
  // Mirror the NEW repaint the fix added right after _body.innerHTML.
  if (assistantMsg._translatePartial && assistantMsg._msgId
      && typeof _renderStreamingTranslatePreview === 'function') {
    _renderStreamingTranslatePreview('c1', assistantMsg._msgId, assistantMsg._translatePartial);
  }
  const rzone = _previewZone(document.getElementById('streaming-msg'));
  check('recon_repaints_stashed_partial', !!rzone && /重连后已恢复的译文/.test(rzone.innerHTML));
}

// ── translatePreview is a FIXED zone created by _ensureStreamZones (survives
//    a body rebuild even before any fn() call). ──
{
  const body = document.createElement('div');
  body.id = 'streaming-body';
  document.getElementById('chatInner').innerHTML = '';
  document.getElementById('chatInner').appendChild(body);
  global.conversations = win.conversations = [{ id: 'c1', messages: [] }];
  updateStreamingUI({ content: 'x', thinking: '', toolRounds: [], phase: null });
  check('translatePreview_is_fixed_zone', !!body.querySelector('[data-zone="translatePreview"]'));
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_translate_preview_rebuild_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             ROOT,                                                  # argv[2]
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),        # argv[3]
             os.path.join(JS_DIR, 'ui', 'streaming_render.js'),    # argv[4]
             os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'),    # argv[5]
             os.path.join(JS_DIR, 'translation.js'),               # argv[6]
             os.path.join(JS_DIR, 'ui', 'translation_render.js'),   # argv[7]
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
    assert not fails, 'translate-preview-survives-rebuild failures:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'

    # ── Source-level guard for Risk B (connectToTask reconnect, contract d) ──
    # The runtime case above reproduces the DOM sequence, but only the SHIPPED
    # reconnect block performing the repaint closes the bug. Assert the repaint
    # call sits inside the reconnect pre-populate block (right after the
    # `_body.innerHTML = _html` assignment). This is the negative control:
    # reverting the shipped repaint makes THIS assertion fail.
    sse = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')
    with open(sse, encoding='utf-8') as f:
        sse_src = f.read()
    anchor = '_body.innerHTML = _html;'
    pos = sse_src.find(anchor)
    assert pos >= 0, 'reconnect pre-populate block not found in sse_pipeline.js'
    window = sse_src[pos:pos + 900]
    assert '_renderStreamingTranslatePreview(convId, assistantMsg._msgId' in window, (
        'Risk B regression: the connectToTask reconnect block no longer repaints '
        'assistantMsg._translatePartial after rebuilding #streaming-body — the '
        'Chinese-so-far will blank on page-reload-into-active-stream until the '
        'next push frame.')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_translate_preview_survives_rebuild():
    _run()
