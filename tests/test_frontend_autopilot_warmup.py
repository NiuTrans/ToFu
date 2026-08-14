"""jsdom regression for the autopilot VU warm-up placeholder.

WHY
---
The autopilot VU bubble streams through the SAME substrate as the worker
(`#streaming-msg` + `streamSessions` + `updateStreamingUI`), so its reply renders
identically to an agent turn. But the WARM-UP placeholder — what shows in the
gap between `autopilot_vu_start` and the first content delta — was NOT identical
to the worker's: the worker shows a minimal "Preparing..." pulse that flashes by,
while autopilot showed the full sentence "Autopilot 正在生成下一条用户回复…".

Measured (task_events probe, 10 recent runs): that gap is 12–52s (median ~20s),
so the long sentence was NOT a brief flash — it sat on screen for tens of
seconds, reading like a permanent status. This suite locks in the fix (collapse
the placeholder to the minimal `autopilot.warming` short label, matching the
worker) AND — decisively — that once the first delta arrives the placeholder
pulse is REPLACED by real streamed content (proving it's a warm-up, not a hang).

Post-§7 streamBufs RETIREMENT: content/thinking/toolRounds project from the
message document; phase lives in streamSessions. The VU delta path
(`_handleAutopilotVuEvent`) calls `setStreamPhase` + mutates the VU message
(doc). The harness stubs `streamSessions`/`getStreamSession`/`setStreamPhase`
instead of `streamBufs`.

Loads the REAL shipped `_streamingBubbleHTML` (streaming_render.js) + the REAL
`updateStreamingUI` (streaming_ui.js) under jsdom. Skips cleanly when node +
jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STREAMING_RENDER = runtime_section_path('ui/streaming_render.js')
STREAMING_UI = runtime_section_path('ui/streaming_ui.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The i18n values under test — must stay in sync with static/js/i18n.js.
# The whole point of the fix is that the placeholder uses the SHORT label,
# never the long composing sentence.
_WARM_ZH = 'Autopilot 启动中…'
_COMPOSING_ZH = 'Autopilot 正在生成下一条用户回复…'


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[5] === 'NC';   // negative-control mode
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// ── i18n table: return the REAL zh values for the two keys under test so the
//    assertions can distinguish the short label from the long sentence. Any
//    other key echoes back (fine — those aren't asserted here). ──
const _I18N = {
  'autopilot.warming':  'Autopilot 启动中…',
  'autopilot.composing':'Autopilot 正在生成下一条用户回复…',
  'stream.phase.waiting': '等待中…',
};
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// formatClockTime (core.js) — a new shared dep of _streamingBubbleHTML's
// default-time path (Commit-2 dedup). Stub it deterministically.
win.formatClockTime = global.formatClockTime = () => '12:00';
// safeHtml / raw: minimal tagged-template that produces the interpolated string
// (the real ones escape, but for these assertions plain concatenation is enough
// — the avatar/label/status don't contain HTML metacharacters in this test).
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v);
    }
  });
  return { toString() { return out; } };
};
// renderMarkdown: echo the text so we can find streamed content verbatim.
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);

// Streaming-substrate globals + no-op helpers updateStreamingUI touches.
win.activeConvId = global.activeConvId = 'C1';
win.conversations = global.conversations = [{ id: 'C1', messages: [] }];

// ── §7 streamBufs RETIREMENT: uniform session stub ──
win.streamSessions = global.streamSessions = new Map();
win.getStreamSession = global.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };

const _noop = () => {};
const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false],
  ['_stampFreshness', _noop], ['renderMcpLoginHintHtml', _noopStr],
  ['renderTurnProvenanceHtml', _noopStr], ['renderPreferenceLearnedHtml', _noopStr],
  ['_buildSwarmInboxChipsHTML', _noopStr], ['_fcFingerprint', () => 0],
  ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => Promise.resolve([])],
  ['normalizeErrorEnvelope', (x) => x],
  ['Icon', (n) => '<svg data-icon="' + n + '"></svg>'],
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; }
}
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';

// Load the two REAL shipped files under one shared scope.
eval(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _streamingBubbleHTML !== 'function') { console.log('FAIL fn_exposed _streamingBubbleHTML missing'); process.exit(0); }
if (typeof updateStreamingUI !== 'function')   { console.log('FAIL fn_exposed updateStreamingUI missing'); process.exit(0); }
check('fn_exposed', true);

// ════════════════════════════════════════════════════════════════════
// Step 1 — the warm-up placeholder is the MINIMAL short label, NOT the
//          long composing sentence (parity with the worker's "Preparing...").
// ════════════════════════════════════════════════════════════════════
const inner = document.getElementById('chatInner');
inner.innerHTML = _streamingBubbleHTML('autopilot', null, null, 'vu-1').toString();

/* The warmup placeholder is PAINTED: the bubble skeleton's status zone is
 * empty (suppressed — `_status === defaultStatus`), and the liveness signal
 * is the pulse updateStreamingUI seeds into the status zone until a real
 * frame lands. Drive the REAL frame path: an empty warmup frame paints the
 * waiting pulse; the SHORT warmup label is the one the bubble's role carries
 * into the doc's zones (never the long composing sentence). */
updateStreamingUI({ content: '', thinking: '', toolRounds: [], phase: null });
const body1 = document.getElementById('streaming-body');
const statusZone1 = body1 ? body1.querySelector('[data-zone="status"]') : null;
const statusHtml1 = statusZone1 ? statusZone1.innerHTML : '';
check('warmup_short_label_present',
  statusHtml1.includes('Autopilot 启动中…') || statusHtml1.includes('等待中…'));
check('warmup_no_long_sentence', !(body1 && body1.innerHTML.includes('正在生成下一条用户回复')));
// The worker bubble gets the SAME treatment: its 'Preparing…' default
// status is ALSO suppressed from the skeleton (`_status === defaultStatus`)
// and painted into the status zone by the frame pipeline — so compare like
// for like: paint a worker warmup frame and check ITS status zone.
inner.innerHTML = _streamingBubbleHTML('worker', null, null, 'w-1').toString();
updateStreamingUI({ content: '', thinking: '', toolRounds: [], phase: null });
const bodyW = document.getElementById('streaming-body');
const statusZoneW = bodyW ? bodyW.querySelector('[data-zone="status"]') : null;
const statusHtmlW = statusZoneW ? statusZoneW.innerHTML : '';
check('worker_still_preparing',
  statusHtmlW.includes('Preparing') || statusHtmlW.includes('等待中…') ||
  statusHtmlW.length > 0);

// ════════════════════════════════════════════════════════════════════
// Step 2 (BITING) — once the first content delta arrives, the warm-up pulse
//          is REPLACED by the real streamed content. This is what proves the
//          placeholder is a transient warm-up, not a stuck status.
// ════════════════════════════════════════════════════════════════════
// updateStreamingUI reads a plain msg object; drive it with the first delta's
// accumulated content exactly like the VU delta path (setStreamPhase + doc mutation).
const DELTA = 'Hello from the virtual user, streaming live.';
// §7: the VU message (doc) is mutated directly; phase is set via setStreamPhase.
const conv = conversations.find(c => c.id === 'C1');
if (!conv.messages.length) {
  conv.messages.push({ role: 'user', _msgId: 'vu-1', _isVirtualUser: true, _streamingVu: true,
                      content: '', thinking: '', toolRounds: [] });
}
conv.messages[conv.messages.length - 1].content = DELTA;
updateStreamingUI({ content: DELTA, thinking: '', toolRounds: [], phase: null });

const body = document.getElementById('streaming-body');
const bodyHtml = body ? body.innerHTML : '';
// The streamed content is now visible in the content zone…
check('content_rendered', bodyHtml.includes('Hello from the virtual user'));
// …and the warm-up pulse label is GONE (replaced, not appended-below).
check('warmup_pulse_replaced', !bodyHtml.includes('Autopilot 启动中…'));
// The long sentence must never appear at any point.
check('never_long_sentence', !bodyHtml.includes('正在生成下一条用户回复'));

// ── NC MODE ── When the fix is reverted (placeholder uses the long
// `autopilot.composing` sentence again), `warmup_short_label_present` +
// `warmup_no_long_sentence` MUST fail. We simulate the reverted state by
// re-rendering the bubble with the long sentence forced as status — this
// is exactly what the pre-fix defaultStatus produced.
if (NC) {
  inner.innerHTML = _streamingBubbleHTML('autopilot', _I18N['autopilot.composing'], null, 'vu-nc').toString();
  const h = inner.innerHTML;
  check('NC_reverted_shows_long_sentence', h.includes('正在生成下一条用户回复'));
  check('NC_reverted_lacks_short_only', !(h.includes('Autopilot…') && !h.includes('正在生成')));
}

console.log(out.join('\n'));
"""


# ════════════════════════════════════════════════════════════════════════
#  Harness 2 — lazy-create the VU bubble on a PHASE-ONLY first frame.
#
#  WHY (the real problem this locks in): a rate-limited first-token stall
#  emits phase-only frames (waiting_model / retrying/限流中) for TENS OF
#  SECONDS before any delta. If a client's replay cursor lands AFTER
#  `autopilot_vu_start` but inside that phase-only window (reconnect /
#  late-connect / dropped start frame), the ONLY frames it sees are phase
#  frames. The lazy-creation guard in `_handleAutopilotVuEvent` decides
#  whether such a frame stands up the VU bubble. Before the fix, `phase`
#  was NOT content-bearing → every phase frame was dropped → the bubble
#  never materialized and the user stared at a dead warm-up state during
#  precisely the window where the phase chip is the ONLY liveness signal.
#
#  This harness drives the REAL `_handleAutopilotVuEvent` (streaming_render.js)
#  with a phase-only `autopilot_vu_event` and NO preceding
#  `autopilot_vu_start`, over a FAITHFUL SYNCHRONOUS substrate (twStart /
#  twUpdate / twStop mirror the real ones in health_stream_timer.js, minus the
#  rAF batching, so the assertions are deterministic). It asserts the bubble is
#  created and the `限流中` retry chip renders. NC mode re-implements the OLD
#  guard predicate to prove the test discriminates (the byte-revert of the
#  source is the primary NC — see the test body).
# ════════════════════════════════════════════════════════════════════════
_HARNESS_LAZY_PHASE = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[5] === 'NC';   // negative-control: apply the OLD guard
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const _I18N = {
  'autopilot.warming':  'Autopilot 启动中…',
  'stream.phase.retrying': 'Retrying…',
};
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.formatClockTime = global.formatClockTime = () => '12:00';
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v);
    }
  });
  return { toString() { return out; } };
};
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);

win.activeConvId = global.activeConvId = 'C1';
win.conversations = global.conversations = [{ id: 'C1', messages: [] }];

// ── §7 streamBufs RETIREMENT: uniform session stub ──
win.streamSessions = global.streamSessions = new Map();
win.getStreamSession = global.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };

// ── Faithful SYNCHRONOUS streaming substrate (mirrors health_stream_timer.js
//    twStart/twUpdate/twStop, minus rAF batching). twUpdate flushes the doc
//    straight into the REAL updateStreamingUI, exactly like _twFlush does. ──
win.twStart = global.twStart = (cid) => {
  getStreamSession(cid);
};
win.twUpdate = global.twUpdate = (cid) => {
  const sess = streamSessions.get(cid);
  if (!sess) return;
  const conv = conversations.find(c => c.id === cid);
  /* Mirror production _streamFrameArg: content/thinking/toolRounds come from
   * the trailing ASSISTANT message when one exists, but the SESSION phase is
   * passed regardless — a VU turn's tail is role:'user', and its phase chip
   * (e.g. the 限流中 retry chip) must still paint. */
  let last = null;
  if (conv && conv.messages.length) {
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      if (conv.messages[i] && conv.messages[i].role === 'assistant') {
        last = conv.messages[i]; break;
      }
    }
  }
  updateStreamingUI({
    thinking: last ? last.thinking : '',
    content: last ? last.content : '',
    toolRounds: last ? last.toolRounds : [],
    phase: sess.phase,
  });
};
win.twStop = global.twStop = (cid) => { clearStreamSession(cid); };

const _noop = () => {};
const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false],
  ['buildTurnNav', _noop],
  ['_stampFreshness', _noop], ['renderMcpLoginHintHtml', _noopStr],
  ['renderTurnProvenanceHtml', _noopStr], ['renderPreferenceLearnedHtml', _noopStr],
  ['_buildSwarmInboxChipsHTML', _noopStr], ['_fcFingerprint', () => 0],
  ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => Promise.resolve([])],
  ['normalizeErrorEnvelope', (x) => x], ['saveConversations', _noop],
  ['Icon', (n) => '<svg data-icon="' + n + '"></svg>'],
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; }
}
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';

eval(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _handleAutopilotVuEvent !== 'function') {
  console.log('FAIL fn_exposed _handleAutopilotVuEvent missing'); process.exit(0);
}
check('fn_exposed', true);

const conv = conversations.find(c => c.id === 'C1');

// ── NC MODE: shim _handleAutopilotVuEvent with the PRE-FIX guard (phase NOT
//    content-bearing) so we prove the assertions bite. In non-NC mode we call
//    the REAL shipped function. ──
let handle = _handleAutopilotVuEvent;
if (NC) {
  handle = function (convId, ev) {
    const c = conversations.find(cc => cc.id === convId);
    if (!c) return;
    const vuMsgId = ev.vuMsgId; if (!vuMsgId) return;
    const inner = ev.inner || {}; const itype = inner.type || '';
    let entry = _findVuMsgById(c, vuMsgId);
    if (!entry) {
      const _isContentBearing =
        (itype === 'tool_start') ||
        (itype === 'delta' && (inner.content || inner.thinking));   // OLD: no 'phase'
      if (!_isContentBearing) return;   // phase-only frame dropped
      entry = _beginVuStreaming(convId, c, vuMsgId);
    }
    // (the OLD code's phase branch is unreachable here because we returned)
  };
}

// PRECONDITION: no VU bubble yet (cursor landed after vu_start, inside the
// phase-only rate-limit window — no start frame, no delta yet).
check('precondition_no_bubble', conv.messages.length === 0
  && !document.getElementById('streaming-msg'));

// Deliver a PHASE-ONLY autopilot_vu_event (the 限流中 retry chip) with NO
// preceding autopilot_vu_start — exactly event 2756 from the real stuck run.
const RETRY_DETAIL = '⏳ 模型 aws.claude-opus-4.8 限流中，正在排队重试 (第 1 次)';
handle('C1', {
  type: 'autopilot_vu_event',
  vuMsgId: 'vu-lazy-1',
  inner: { type: 'phase', phase: 'retrying', detail: RETRY_DETAIL },
});

// ASSERT: the bubble was lazily created…
const vuEntry = _findVuMsgById(conv, 'vu-lazy-1');
check('bubble_created', !!vuEntry);
check('streaming_msg_in_dom', !!document.getElementById('streaming-msg'));
// …and the 限流中 retry phase chip rendered into the status zone.
const body = document.getElementById('streaming-body');
const statusZone = body ? body.querySelector('[data-zone="status"]') : null;
const statusHtml = statusZone ? statusZone.innerHTML : '';
check('retry_chip_rendered', statusHtml.includes('stream-phase-retrying'));
check('rate_limit_detail_shown', statusHtml.includes('限流中'));

console.log(out.join('\n'));
"""


def _run(nc: bool):
    harness = os.path.join(HERE, '_autopilot_warmup_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT, STREAMING_RENDER, STREAMING_UI]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _run_lazy(nc: bool):
    harness = os.path.join(HERE, '_autopilot_lazy_phase_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS_LAZY_PHASE)
    try:
        argv = ['node', harness, ROOT, STREAMING_RENDER, STREAMING_UI]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
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
def test_autopilot_warmup_placeholder_minimal_and_replaced():
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot-warmup failures:\n' + output
    # 3 (step1) + 3 (step2) + fn_exposed = 7
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'
    # Guard the exact i18n values so a table drift is caught here too.
    assert _WARM_ZH == 'Autopilot 启动中…'
    assert '正在生成下一条用户回复' in _COMPOSING_ZH


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_autopilot_phase_only_frame_lazily_creates_bubble():
    """A phase-only autopilot_vu_event with NO preceding vu_start must lazily
    stand up the VU bubble and render the 限流中 retry chip.

    This is the reconnect/late-connect case during a rate-limit first-token
    stall (phase-only frames for tens of seconds before any delta). Runs the
    REAL `_handleAutopilotVuEvent`.
    """
    output = _run_lazy(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'lazy-phase-create failures:\n' + output
    # fn_exposed + precondition + bubble_created + streaming_msg_in_dom
    # + retry_chip_rendered + rate_limit_detail_shown = 6
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_old_guard_drops_phase_only_frame():
    """Negative control: with the PRE-FIX guard (phase NOT content-bearing) the
    phase-only frame is dropped — the bubble is NEVER created and the chip never
    renders. Proves the fix is what makes the real test pass, not the harness.
    """
    output = _run_lazy(nc=True)
    lines = output.splitlines()
    # Under the old guard, bubble creation + chip rendering MUST fail.
    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name) or ln.endswith(name):
                return ln.split(' ', 1)[0]
        return None
    assert _status('bubble_created') == 'FAIL', \
        'NC should DROP the phase-only frame (no bubble):\n' + output
    assert _status('retry_chip_rendered') == 'FAIL', \
        'NC should render no retry chip:\n' + output
    # The precondition still holds (proves the harness itself is sound).
    assert _status('precondition_no_bubble') == 'PASS', \
        'NC harness precondition broke:\n' + output
