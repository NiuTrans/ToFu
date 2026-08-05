#!/usr/bin/env python3
"""jsdom regression: the VU bubble's「推理中 N 字符」counter must CLIMB with
the reasoning stream — never freeze at 0 字符.

THE BUG (2026-08-03, owner screenshots, conv msdcksqymtglha)
------------------------------------------------------------
An Autopilot VU turn showed「推理中 0 字符」for the whole 63s round, flipped
to「已发送给 kimi-k3，等待开始回复…」at the round boundary, then went back
to「推理中 0 字符」for the 86s second round — looking stuck while the server
was in fact streaming thousands of reasoning chars (app.log: R1 thinking=
6415chars → 2 tool calls, R2 thinking=5221chars → 882-char answer; TTFT
11.7s; nothing stuck server-side).

Root cause: the worker ingress carries the reasoning length on the phase
(``sse_pipeline.js``: ``setStreamPhase(convId, { phase: "thinking_active",
_thinkingLen: _roundThinkingLen })``), but the VU ingress set
``{ phase: "thinking_active" }`` with NO ``_thinkingLen``
(streaming_render.js delta branch). The phase row paints
``phase._thinkingLen || 0`` (streaming_ui.js) — so every Autopilot bubble
hard-wired the counter to 0 字符 for the entire round.

THE FIX (streaming_render.js):
  * delta branch accumulates ``vuMsg._roundThinkingLen`` and ships it as
    ``_thinkingLen`` on the thinking_active phase;
  * the phase branch resets it to 0 — worker parity (sse_pipeline.js resets
    ``_roundThinkingLen`` on every phase event; a round boundary restarts
    the count).

Drives the REAL shipped ``_handleAutopilotVuEvent`` (streaming_render.js) +
``stream_session.js`` + ``streaming_ui.js`` under jsdom and asserts:
  1. a thinking-delta envelope lands phase=thinking_active WITH
     ``_thinkingLen`` equal to the accumulated round chars;
  2. the bubble's status zone paints the climbing counter (3 字 → 8 字);
  3. round scoping: a phase envelope (the round transition) resets the
     counter — the next round's first delta counts from its own length,
     not the previous round's total.

NEUTER arm: dropping ``, _thinkingLen: vuMsg._roundThinkingLen`` from a
scratch copy turns the session/paint pins RED (counter frozen at 0 字) —
the field is load-bearing, not decoration.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_vu_thinking_counter.py
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
RENDER_JS = os.path.join(JS_DIR, 'ui', 'streaming_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const RENDER_SRC = process.argv[3];  // live file, or a scratch/HEAD copy
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (e) {} return 0; };
global.setTimeout = win.setTimeout = (fn) => 0;
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
/* i18n stub with REAL production templates + {var} interpolation. */
const _I18N = {
  'stream.phase.reasoning': '推理中',
  'stream.phase.chars': '{n} 字符',
  'stream.phase.waitingForModel': '已发送给 {model}，等待开始回复…',
  'autopilot.warming': 'Autopilot 启动中…',
  'autopilot.label': 'Autopilot',
};
win.t = global.t = (k, a) => {
  let s = (k in _I18N) ? _I18N[k] : k;
  if (a) for (const key of Object.keys(a)) s = s.split('{' + key + '}').join(String(a[key]));
  return s;
};
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
  ['renderConversationList', _noop],
  ['saveConversations', _noop],
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

win.activeStreams = global.activeStreams = new Map([['C1', { taskId: 'carrier-1' }]]);

/* Streaming substrate (mirror health_stream_timer.js, synchronous). */
const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.twStart = global.twStart = (cid) => { streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null }); };
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); };
win.twUpdate = global.twUpdate = (cid) => {
  const c = conversations.find(x => x.id === cid);
  const tail = c.messages[c.messages.length - 1];
  const sess = (typeof getStreamSession === 'function') ? getStreamSession(cid) : { phase: null };
  updateStreamingUI({ content: tail.content || '', thinking: tail.thinking || '',
                      toolRounds: tail.toolRounds || [], phase: sess.phase });
};

const conv = {
  id: 'C1',
  messages: [
    { role: 'user', content: 'do the thing' },
    { role: 'assistant', content: 'done, i think', done: true },
  ],
};
win.conversations = global.conversations = [conv];

// Load the REAL shipped modules under one shared scope.
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'stream_session.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_ui.js'), 'utf8'));
eval(fs.readFileSync(RENDER_SRC, 'utf8'));   // argv-selected streaming_render.js
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'stream_lifecycle.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _handleAutopilotVuEvent !== 'function') { console.log('FAIL fn_exposed _handleAutopilotVuEvent missing'); process.exit(0); }
check('fn_exposed', true);

twStart('C1');
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_start', vuMsgId: 'vu-1' });
const vuTail = conv.messages[conv.messages.length - 1];
check('vu_bubble_born', !!(vuTail && vuTail._isVirtualUser && vuTail._streamingVu));
showStreamingUIForConv('C1');
check('live_streaming_msg', !!document.getElementById('streaming-msg'));
const body1 = document.getElementById('streaming-body');

function think(text) {
  _handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-1',
                                  inner: { type: 'delta', thinking: text } });
}

// THE ACT — the incident's round 1: reasoning streams with zero visible prose.
think('abc');

// (1) Session phase carries the length (the field the paint path reads).
const p1 = getStreamSession('C1').phase;
check('phase_carries_thinking_len', !!(p1 && p1.phase === 'thinking_active' && p1._thinkingLen === 3));

// (2) The bubble paints the REAL count — never the frozen 0 字符.
twUpdate('C1');
const zone1 = body1 ? body1.querySelector('[data-zone="status"]') : null;
const html1 = zone1 ? zone1.innerHTML : '';
check('counter_paints_3', /推理中/.test(html1) && /3 字符/.test(html1) && !/0 字符/.test(html1));

// (3) It CLIMBS with the stream (the incident's core lie: stuck at 0).
think('defgh');
twUpdate('C1');
const p2 = getStreamSession('C1').phase;
const zone2 = body1 ? body1.querySelector('[data-zone="status"]') : null;
const html2 = zone2 ? zone2.innerHTML : '';
check('counter_climbs_8', !!(p2 && p2._thinkingLen === 8) && /8 字符/.test(html2));

// (4) Round scoping: the round-transition phase (waiting_model, exactly what
//     manager/_stream.py emits between R1 and R2) restarts the count — the
//     next round's first delta counts from ITS length, not R1's total.
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-1',
  inner: { type: 'phase', phase: 'waiting_model',
           detail: 'Sent to kimi-k3, waiting for it to start replying…',
           detailKey: 'stream.phase.waitingForModel',
           detailArgs: { model: 'kimi-k3' }, model: 'kimi-k3' } });
think('xy');
const p3 = getStreamSession('C1').phase;
check('round_transition_resets_counter', !!(p3 && p3.phase === 'thinking_active' && p3._thinkingLen === 2));

console.log(out.join('\n'));
"""


def _run_raw(render_src: str) -> str:
    """Run the harness against an arbitrary streaming_render.js (live file,
    HEAD copy for failing-first, or a neutered scratch) without asserting."""
    harness = os.path.join(HERE, '_vu_thinking_counter_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, render_src],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_PINS = (
    'fn_exposed',
    'vu_bubble_born',
    'live_streaming_msg',
    'phase_carries_thinking_len',
    'counter_paints_3',
    'counter_climbs_8',
    'round_transition_resets_counter',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_vu_thinking_counter_climbs():
    """Fixed code: the VU reasoning counter is carried on the phase, paints
    the real count, climbs, and restarts per round."""
    output = _run_raw(RENDER_JS)
    for name in _PINS:
        assert f'PASS {name}' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_missing_thinking_len_freezes_counter():
    """NEUTER: strip `, _thinkingLen: vuMsg._roundThinkingLen` from a scratch
    copy — the counter pins MUST go red (0 字符 frozen returns), proving the
    field is what stands between us and the incident."""
    src = open(RENDER_JS, encoding='utf-8').read()
    fix_token = ', _thinkingLen: vuMsg._roundThinkingLen'
    assert fix_token in src, (
        'fix token missing from streaming_render.js — the guard no longer '
        'has anything to pin; re-check the fix.')
    import tempfile
    neutered = src.replace(fix_token, '', 1)
    assert neutered != src
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        scratch = fh.name
        fh.write(neutered)
    try:
        output = _run_raw(scratch)
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    assert 'FAIL phase_carries_thinking_len' in output, (
        'NEUTER ineffective: without _thinkingLen the session pin did NOT '
        'go red — the guard would pass on the buggy code too:\n' + output)
    assert 'FAIL counter_climbs_8' in output, (
        'NEUTER ineffective on the paint pin:\n' + output)


def test_source_guard_vu_phase_carries_thinking_len():
    """Source-level guard: the VU delta branch must keep shipping
    _thinkingLen on thinking_active (worker parity with sse_pipeline.js),
    and the VU phase branch must keep the per-round reset."""
    with open(RENDER_JS, encoding='utf-8') as f:
        src = f.read()
    assert 'setStreamPhase(convId, { phase: "thinking_active", _thinkingLen: vuMsg._roundThinkingLen })' in src, (
        'regression: the VU thinking_active phase no longer carries '
        '_thinkingLen — Autopilot bubbles freeze at 推理中 0 字符 for whole '
        'rounds (the 2026-08-03 owner-screenshot incident).')
    assert 'vuMsg._roundThinkingLen = 0;' in src, (
        'regression: the VU phase branch lost the round-scoped counter '
        'reset — the counter would accumulate across rounds instead of '
        'restarting at each round transition (worker parity).')


if __name__ == '__main__':
    for fn in (test_vu_thinking_counter_climbs,
               test_NEUTER_missing_thinking_len_freezes_counter,
               test_source_guard_vu_phase_carries_thinking_len):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:400])
