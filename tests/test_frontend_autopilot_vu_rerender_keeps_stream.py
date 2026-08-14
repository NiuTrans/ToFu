"""jsdom regression: a mid-stream re-render must NOT freeze the Autopilot VU
bubble on the "Autopilot starting…" warm-up label.

WHY
---
The Autopilot virtual-user reply streams in the USER lane through the SAME
substrate as the worker (`#streaming-msg` + `streamBufs` + `updateStreamingUI`),
so it should render live exactly like an agent turn. But `showStreamingUIForConv`
— the whole-list rebuild every mid-stream `renderChat` funnels into (Guard 1c,
chat_render.js, once `#streaming-msg` exists) — only recognised an
`assistant` / `_isEndpointReview` tail as OWNING the streaming bubble. When
autopilot fires, the streaming tail is a role=user VU placeholder
(`_isVirtualUser` + `_streamingVu`). So the rebuild painted the VU turn
STATICALLY (renderMessage → the frozen `vu-composing` "Autopilot starting…"
pulse) and never recreated a live `#streaming-msg`. Every subsequent
`autopilot_vu_event` frame then hit `_twFlush` with no `#streaming-body` and was
dropped — the bubble sat frozen on the warm-up label while the elapsed timer
kept ticking (the reported "Autopilot streaming is worse than the Agent bubble").

The fix adds a VU-tail arm to `showStreamingUIForConv`'s `_lastIsStreamingBubble`
selection + a `_streamingBubbleHTML('autopilot', …)` branch, so the rebuild
recreates a LIVE autopilot streaming bubble (id=streaming-msg) whose forwarded
phase/delta frames immediately replace the warm-up label.

This drives the REAL shipped `showStreamingUIForConv` (stream_lifecycle.js) +
its render deps under jsdom and asserts:
  1. after the rebuild a live `#streaming-msg` exists (not a static msg-N), and
  2. it carries the autopilot label + is targetable by the VU msgId, and
  3. a following `updateStreamingUI` phase frame renders live into it.

NC (bites): a shim that reverts the tail arm (VU tail NOT streaming) → the
rebuild produces a static bubble with NO `#streaming-msg` → assertion (1) fails.

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
RUNTIME = Path(ROOT) / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'


def _runtime_section(name: str) -> str:
    """Read one retained vanilla section from the shipped Vite runtime."""
    source = RUNTIME.read_text(encoding='utf-8')
    marker = f'/* ===== migrated source: {name} ===== */'
    start = source.find(marker)
    assert start >= 0, f'migrated runtime section not found: {name}'
    next_marker = source.find('/* ===== migrated source:', start + len(marker))
    end = next_marker if next_marker >= 0 else len(source)
    return ('var runtimeScope = typeof window !== "undefined" '
            '? window : globalThis;\n' + source[start:end])


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf'function {re.escape(name)}\s*\(', source)
    assert match, f'{name} not found in runtime source'
    brace = source.index('{', match.start())
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (e) {} return 0; };
global.setTimeout = win.setTimeout = (fn) => 0;   // suppress the 300ms deferred re-render
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
/* The reworked _streamingBubbleHTML resolves the role label via
 * t('autopilot.label') (i18n) — map it to the production value so the
 * /Autopilot/ identity assertion sees the real text. */
const _I18N = { 'autopilot.warming': 'Autopilot 启动中…', 'stream.phase.waiting': '等待中…',
                'autopilot.label': 'Autopilot' };
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win.formatClockTime = global.formatClockTime = () => '12:00';
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) { const v = vals[i]; out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v); }
  });
  return { toString() { return out; } };
};

const _noop = () => {};
const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false], ['_forceScrollToBottom', _noop],
  ['buildTurnNav', _noop], ['updateSendButton', _noop],
  ['_destroyLazyObserver', _noop], ['_ensureLazyObserver', _noop],
  ['_captureScrollAnchor', () => null], ['_restoreScrollAnchor', _noop],
  ['getToolRoundsFromMsg', (m) => (m && m.toolRounds) || []],
  ['renderMessage', (m, i) => '<div class="message" id="msg-' + i + '"><div class="message-body"></div></div>'],
  ['_stampFreshness', _noop], ['renderMcpLoginHintHtml', _noopStr],
  ['renderTurnProvenanceHtml', _noopStr], ['renderPreferenceLearnedHtml', _noopStr],
  ['_buildSwarmInboxChipsHTML', _noopStr], ['_fcFingerprint', () => 0],
  ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => ({ then: () => {} })],
  ['normalizeErrorEnvelope', (x) => x],
  ['Icon', (n) => '<svg data-icon="' + n + '"></svg>'],
  ['_convRenderFingerprint', () => 'fp'],
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; }
}
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<svg id="worker-avatar"></svg>';

win._INITIAL_RENDER = global._INITIAL_RENDER = 50;
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = 0;
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win.activeConvId = global.activeConvId = 'C1';

// Streaming substrate (mirror health_stream_timer.js, synchronous).
const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.twStart = global.twStart = (cid) => { streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null }); };
win.twUpdate = global.twUpdate = _noop;
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); };

// A VU placeholder tail (role=user, machine-authored, still streaming), exactly
// what _beginVuStreaming pushes and what a mid-stream renderChat rebuilds.
const conv = {
  id: 'C1',
  messages: [
    { role: 'user', content: 'do the thing' },
    { role: 'assistant', content: 'done, i think', done: true },
    { role: 'user', content: '', _isVirtualUser: true, _streamingVu: true, _msgId: 'vu-42', toolRounds: [] },
  ],
};
win.conversations = global.conversations = [conv];

// Carrier/parent handoff shape from the production incident: the live stream
// remains bound to an assistant-side control object while the actual visible
// owner is the trailing VU user message.  The VU tail must win for both lane
// identity and document projection; otherwise the rebuild produces an Agent
// bubble backed by this empty object.
const detachedCarrierAssistant = {
  role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'carrier-dummy',
};
win.activeStreams = global.activeStreams = new Map([['C1', {
  taskId: 'task-vu-carrier', assistantMsg: detachedCarrierAssistant,
  _detachedCarrier: 'autopilot-kick',
}]]);

// Load the REAL shipped render deps under one shared scope.
eval(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8'));
eval(fs.readFileSync(process.argv[5], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof showStreamingUIForConv !== 'function') { console.log('FAIL fn_exposed showStreamingUIForConv missing'); process.exit(0); }
check('fn_exposed', true);

// Seed the buffer (twStart normally does this on connect) so the render has a buffer.
twStart('C1');

// THE ACT: a mid-stream whole-list rebuild while the VU tail is streaming.
showStreamingUIForConv('C1');

// (1) A LIVE streaming bubble was recreated — NOT a static msg-N for the VU turn.
const sm = document.getElementById('streaming-msg');
check('live_streaming_msg_recreated', !!sm);
// (2) It is the autopilot lane (avatar/label) and targetable by the VU msgId.
const smHtml = sm ? sm.outerHTML : '';
check('bubble_is_autopilot', /Autopilot/.test(smHtml));
check('bubble_carries_vu_msgid', sm && sm.getAttribute('data-msg-id') === 'vu-42');
// The static VU msg-N must NOT exist (it would be a frozen duplicate).
check('no_static_vu_bubble', !document.getElementById('msg-2'));

// (3) A following forwarded phase frame renders LIVE into the recreated bubble.
updateStreamingUI({ content: '', thinking: '', toolRounds: [],
                    phase: { phase: 'working', detail: 'Autopilot：装配工具、准备工作区…' } });
const body = document.getElementById('streaming-body');
const statusZone = body ? body.querySelector('[data-zone="status"]') : null;
const statusHtml = statusZone ? statusZone.innerHTML : '';
check('phase_renders_live', /装配工具/.test(statusHtml));

console.log(out.join('\n'));
"""


def _run():
    with tempfile.TemporaryDirectory(prefix='tofu-vu-rerender-') as tmp:
        temp_root = Path(tmp)
        harness = temp_root / 'harness.js'
        harness.write_text(_HARNESS, encoding='utf-8')
        section_paths = []
        for index, name in enumerate((
            'ui/streaming_ui.js',
            'ui/streaming_render.js',
            'ui/stream_lifecycle.js',
        )):
            path = temp_root / f'section-{index}.js'
            path.write_text(_runtime_section(name), encoding='utf-8')
            section_paths.append(str(path))
        proc = subprocess.run(
            ['node', str(harness), ROOT, *section_paths],
            capture_output=True, text=True, timeout=60,
        )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_vu_tail_rerender_recreates_live_streaming_bubble():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot-vu-rerender failures:\n' + output
    # fn_exposed + live_streaming_msg_recreated + bubble_is_autopilot
    # + bubble_carries_vu_msgid + no_static_vu_bubble + phase_renders_live = 6
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


_FRAME_HARNESS = r"""
'use strict';
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const streamSessions = new Map([['C1', {
  phase: { phase: 'working', detail: 'Autopilot is preparing tools…' },
}]]);
const vuTail = {
  role: 'user', content: 'VISIBLE-VU-CONTENT', thinking: '', toolRounds: [],
  _msgId: 'vu-42', _isVirtualUser: true, _streamingVu: true,
};
const conversations = [{ id: 'C1', messages: [
  { role: 'assistant', content: 'settled worker' }, vuTail,
] }];
const detachedCarrier = {
  role: 'assistant', content: '', thinking: '', toolRounds: [],
  _msgId: 'carrier-dummy',
};
const activeStreams = new Map([['C1', {
  taskId: 'task-vu-carrier', assistantMsg: detachedCarrier,
}]]);
function getToolRoundsFromMsg(msg) { return (msg && msg.toolRounds) || []; }
function _resolveAssistantById(conv, msgId) {
  return conv.messages.find(msg => msg && msg._msgId === msgId) || null;
}
__FRAME_FUNCTION__
const frame = _streamFrameArg('C1');
check('vu_document_wins_over_detached_assistant_carrier',
      frame && frame.content === 'VISIBLE-VU-CONTENT');
check('backend_phase_survives_frame_projection',
      frame && frame.phase && frame.phase.detail === 'Autopilot is preparing tools…');
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_vu_stream_frame_projects_vu_tail_over_detached_carrier():
    """The coalesced timer must repaint from the VU document, not from the
    empty assistant-side carrier left behind during the parent→VU handoff."""
    health = _runtime_section('core/health_stream_timer.js')
    frame_fn = _extract_js_function(health, '_streamFrameArg')
    proc = subprocess.run(
        ['node', '-'],
        input=_FRAME_HARNESS.replace('__FRAME_FUNCTION__', frame_fn),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not fails, 'VU stream-frame projection failures:\n' + output



def test_source_guard_vu_tail_arm_present():
    """Source-level guard: showStreamingUIForConv must recognise a VU-streaming
    tail as owning the streaming bubble AND emit the autopilot bubble for it.
    This is what stops a mid-stream rebuild from freezing the VU turn on the
    static warm-up pulse."""
    src = _runtime_section('ui/stream_lifecycle.js')
    assert '_last._isVirtualUser && _last._streamingVu' in src, (
        'regression: showStreamingUIForConv no longer counts a streaming VU tail '
        'as _lastIsStreamingBubble — a mid-stream renderChat rebuild will paint '
        'the VU turn statically and freeze it on "Autopilot starting…".')
    assert '_vuTailOwnsStream ? lastMsg : (_boundMsg || lastMsg)' in src, (
        'regression: showStreamingUIForConv lets an assistant-bound carrier '
        'override the trailing VU document as _smSrc — the live bubble changes '
        'back to Agent and projects an empty control object as “Waiting…”.')
    health = _runtime_section('core/health_stream_timer.js')
    assert 'last._isVirtualUser && last._streamingVu' in health, (
        'regression: the coalesced stream frame no longer projects from the '
        'live VU document, so phase repaints fall back to Agent · Waiting…')
    assert "_streamingBubbleHTML('autopilot'" in src, (
        'regression: showStreamingUIForConv no longer emits the autopilot '
        'streaming bubble for a VU tail.')
