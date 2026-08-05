"""jsdom regression: a VU carrier's 429-retry phase stream must paint「限流
重试中 · 第 N 次」live in the VU bubble — and refresh with each attempt.

WHY (pt_a21cd6eb 交付②)
-----------------------
2026-08-01 incident (conv ms9ow2ttm0gnu0): the autopilot VU carrier spent
~75 minutes / 3900+ cycles in a 429 retry loop. The backend emitted a
retrying PHASE event per cycle (`manager/_stream.py::_on_retry`), the VU
carrier contract forwarded them (7154+ persisted `autopilot_vu_event`
frames), yet the user stared at a bare empty bubble with no「限流重试中」
signal — the conversation looked interrupted-for-no-reason.

The render chain exists by design: envelope → `_handleAutopilotVuEvent`
phase branch → `setStreamPhase` → `updateStreamingUI`'s retrying branch
(i18n `stream.phase.retryRateLimited`, repaint keyed on attempt). Until
this harness, NO test pinned that chain for the VU envelope end-to-end.

Drives the REAL shipped `_handleAutopilotVuEvent` (streaming_render.js) +
`stream_session.js` + `streaming_ui.js` under jsdom and asserts:
  1. a retrying phase envelope lands in the stream session (detailKey /
     attempt / statusCode survive the whitelist);
  2. the bubble paints the localized「第 1 次」chip;
  3. the NEXT beat repaints to「第 2 次」(attempt-keyed refresh);
  4. the sidebar rate-limit mirror derives {model, attempt} from the
     VU-driven phase (`convRateLimitPhase`);
  5. honest-label negative: a quota wait (keyBalanceExhausted) does NOT
     count as 限流 for the sidebar mirror;
  6. raw-`detail` fallback still paints when no detailKey is shipped.

NC (bites): dropping the phase branch's field whitelist in
streaming_render.js (attempt/statusCode/detailKey stripped) breaks (1)(3);
dropping the retrying branch in streaming_ui.js breaks (2).
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
  'stream.phase.retryRateLimited': '⏳ 模型 {model} 限流中，正在排队重试（第 {attempt} 次）…',
  'stream.retryReason.keyBalanceExhausted': '密钥余额已耗尽',
  'stream.phase.retrying': '重试中…',
  'stream.phase.reasoning': '思考中',
  'stream.phase.chars': '{n} 字',
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

/* activeStreams: needed by _phaseTurnStillRunning (this tab holds a live
 * stream for the conv — the carrier attach). */
win.activeStreams = global.activeStreams = new Map([['C1', { taskId: 'carrier-1' }]]);

/* Streaming substrate (mirror health_stream_timer.js, synchronous). */
const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.twStart = global.twStart = (cid) => { streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null }); };
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); };
/* twUpdate mimic of health_stream_timer._streamFrameArg: project the message
 * document + the session phase into updateStreamingUI. */
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
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'stream_lifecycle.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _handleAutopilotVuEvent !== 'function') { console.log('FAIL fn_exposed _handleAutopilotVuEvent missing'); process.exit(0); }
check('fn_exposed', true);

// Seed the worker substrate + stand up the VU bubble via the REAL start event.
twStart('C1');
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_start', vuMsgId: 'vu-1' });
const vuTail = conv.messages[conv.messages.length - 1];
check('vu_bubble_born', !!(vuTail && vuTail._isVirtualUser && vuTail._streamingVu));

// Recreate the LIVE streaming bubble for the VU tail (pinned behaviour from
// test_frontend_autopilot_vu_rerender_keeps_stream).
showStreamingUIForConv('C1');
check('live_streaming_msg', !!document.getElementById('streaming-msg'));

// THE ACT: a 429 retry phase envelope, exactly what the carrier transform
// wraps (inner = manager/_stream.py::_on_retry's PHASE event).
function retryEnv(attempt) {
  return { type: 'autopilot_vu_event', vuMsgId: 'vu-1',
           inner: { type: 'phase', phase: 'retrying',
                    detail: '⏳ 模型 kimi-k3 限流中，正在排队重试 (第 ' + attempt + ' 次)…',
                    detailKey: 'stream.phase.retryRateLimited',
                    detailArgs: { model: 'kimi-k3', attempt: attempt },
                    attempt: attempt, statusCode: 429, model: 'kimi-k3' } };
}
_handleAutopilotVuEvent('C1', retryEnv(1));

// (1) The envelope landed in the stream session through the whitelist.
const s1 = getStreamSession('C1').phase;
check('session_phase_landed', !!(s1 && s1.phase === 'retrying'
      && s1.detailKey === 'stream.phase.retryRateLimited'
      && s1.attempt === 1 && s1.statusCode === 429));

// (2) Paint → the localized「第 1 次」chip appears.
twUpdate('C1');
const body1 = document.getElementById('streaming-body');
const zone1 = body1 ? body1.querySelector('[data-zone="status"]') : null;
const html1 = zone1 ? zone1.innerHTML : '';
check('chip_attempt_1', /第 1 次/.test(html1) && /kimi-k3/.test(html1));

// (3) Next beat repaints to「第 2 次」(attempt-keyed refresh).
_handleAutopilotVuEvent('C1', retryEnv(2));
twUpdate('C1');
const zone2 = body1 ? body1.querySelector('[data-zone="status"]') : null;
const html2 = zone2 ? zone2.innerHTML : '';
check('chip_attempt_2_refresh', /第 2 次/.test(html2) && !/第 1 次/.test(html2));

// (4) Sidebar rate-limit mirror derives {model, attempt} from the VU phase.
const rl = (typeof convRateLimitPhase === 'function') ? convRateLimitPhase('C1') : null;
check('sidebar_mirror', !!(rl && rl.model === 'kimi-k3' && rl.attempt === 2));

// (5) Honest-label negative: a QUOTA wait must NOT read as 限流.
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-1',
  inner: { type: 'phase', phase: 'retrying',
           detail: 'Waiting for model (key balance exhausted)',
           detailKey: 'stream.phase.retryReason',
           detailArgs: { reason: 'Key balance exhausted', model: 'kimi-k3',
                         reasonKey: 'stream.retryReason.keyBalanceExhausted' },
           attempt: 3, model: 'kimi-k3' } });
const rl2 = (typeof convRateLimitPhase === 'function') ? convRateLimitPhase('C1') : null;
check('honest_label_quota_not_rl', rl2 === null);

// (6) Raw-detail fallback: no detailKey → the raw detail text paints.
_handleAutopilotVuEvent('C1', retryEnv(7));
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-1',
  inner: { type: 'phase', phase: 'retrying',
           detail: 'Retrying… custom-cause (kimi-k3, attempt 8)',
           attempt: 8, model: 'kimi-k3' } });
twUpdate('C1');
const zone3 = body1 ? body1.querySelector('[data-zone="status"]') : null;
const html3 = zone3 ? zone3.innerHTML : '';
check('raw_detail_fallback', /custom-cause/.test(html3));

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_vu_retry_phase_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT],
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
def test_vu_retry_phase_projects_into_bubble():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'vu-retry-phase failures:\n' + output
    # fn_exposed + vu_bubble_born + live_streaming_msg + session_phase_landed
    # + chip_attempt_1 + chip_attempt_2_refresh + sidebar_mirror
    # + honest_label_quota_not_rl + raw_detail_fallback = 9
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


def test_source_guard_phase_whitelist_in_vu_render():
    """Source-level guard: the VU phase branch must keep forwarding the
    retry-identifying fields (detailKey/detailArgs/attempt/statusCode) — the
    exact fields the retry chip + sidebar mirror key on."""
    sl = os.path.join(JS_DIR, 'ui', 'streaming_render.js')
    with open(sl, encoding='utf-8') as f:
        src = f.read()
    for token in ('detailKey: inner.detailKey',
                  'detailArgs: inner.detailArgs',
                  'attempt: inner.attempt',
                  'statusCode: inner.statusCode'):
        assert token in src, (
            f'regression: VU phase branch no longer forwards {token!r} — '
            'the retry chip loses its identity / refresh key and the bubble '
            'goes back to a frozen generic spinner during 429 cycling.')
