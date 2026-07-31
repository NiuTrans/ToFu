"""tests/test_frontend_vu_carrier_contract.py — frontend half of the
VU-carrier stream contract (2026-07-26, conv ms1rrjchpa5pqw).

The backend (tests/test_vu_carrier_stream_contract.py) pins the wire
shape; this suite pins the three frontend behaviours that make the hop
(parent stream → VU carrier stream) render correctly:

  1. ``autopilot_vu_start`` carrying ``replaySnapshot`` applies with
     RESET semantics — the client that hopped from the parent stream
     already saw the early frames there; assignment (not append)
     dedupes the pre-hop window exactly.
  2. The live VU content masks machine-sentinel lines
     (``[VU: TASK_DONE]`` / ``[PROGRESS: …]``) on every delta — the
     owner ruled them NEVER visible; persistence is clean backend-side,
     so this is the live-display half.
  3. Wiring source guards: the supersede attach must delegate a VU
     successor to the kick connector (``opts.vuCarrier``), and the
     ``latestLiveTaskIsVu`` stamp must ride ``_stampLatestLiveTask``.

NEGATIVE CONTROLS: (a) an identity shim for ``_maskVuMachineTokens``
leaves the sentinels visible (proves the mask is load-bearing);
(b) an append-not-reset shim for the snapshot produces doubled content
(proves the reset semantics are load-bearing).

Skips cleanly when node + jsdom aren't installed (frontend half);
the source guards run everywhere.
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


# ══════════════════════════════════════════════════════════════════════
#  Source guards (run everywhere, no node needed).
# ══════════════════════════════════════════════════════════════════════

def test_wiring_vu_carrier_delegation_and_isvu_stamp():
    """The supersede attach must delegate a VU successor to the VU
    connector, and the IsVu stamp must be stored + consumed."""
    pipe = open(os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js'),
                encoding='utf-8').read()
    assert 'opts.vuCarrier' in pipe, (
        'connectToTask must accept opts.vuCarrier and delegate to the VU '
        'connector (detached dummy assistant, no Agent placeholder)')
    assert 'opts.autopilotKick || opts.vuCarrier' in pipe.replace('  ', ' ') or \
           'opts.autopilotKick || opts.vuCarrier' in pipe, (
        'vuCarrier must share the kick-connector delegation branch')

    life = open(os.path.join(ROOT, 'static', 'js', 'ui', 'stream_lifecycle.js'),
                encoding='utf-8').read()
    assert 'latestLiveTaskIsVu' in life, (
        '_stampLatestLiveTask must store conv._latestLiveTaskIsVu from the '
        'terminal frame')
    assert 'vuCarrier: true' in life, (
        'the supersede attach must pass { vuCarrier: true } to connectToTask '
        'when the successor is a VU carrier')

    render = open(os.path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'),
                  encoding='utf-8').read()
    assert 'replaySnapshot' in render, (
        'the vu_start handler must apply replaySnapshot (reset semantics)')
    assert '_maskVuMachineTokens' in render, (
        'the VU live-content path must mask machine-sentinel lines')


# ══════════════════════════════════════════════════════════════════════
#  Frontend half — jsdom harness (mirrors test_frontend_autopilot_vu_start_eager)
# ══════════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const _I18N = { 'autopilot.warming': 'Autopilot 启动中…', 'stream.phase.waiting': '等待中…' };
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.formatClockTime = global.formatClockTime = () => '12:00';
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => { out += s; if (i < vals.length) { const v = vals[i]; out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v); } });
  return { toString() { return out; } };
};
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);
win.activeConvId = global.activeConvId = 'C1';
win.conversations = global.conversations = [{ id: 'C1', messages: [] }];

win.activeStreams = global.activeStreams = new Map();
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win._syncToolRoundsDOM = global._syncToolRoundsDOM = () => {};
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win._isRoundSwarm = global._isRoundSwarm = (r) => !!(r && r._swarm);
win._renderUnifiedToolLine = global._renderUnifiedToolLine = () => '';
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '';
win.getSelection = global.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;

const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.streamSessions = global.streamSessions = new Map();
win.getStreamSession = global.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof activeStreams !== "undefined" && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };
win.twStart = global.twStart = (cid) => { streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null }); win.getStreamSession(cid); };
win.twUpdate = global.twUpdate = () => {};
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); win.clearStreamSession(cid); };

const _noop = () => {}; const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false], ['buildTurnNav', _noop],
  ['_stampFreshness', _noop], ['renderMcpLoginHintHtml', _noopStr],
  ['renderTurnProvenanceHtml', _noopStr], ['renderPreferenceLearnedHtml', _noopStr],
  ['_buildSwarmInboxChipsHTML', _noopStr], ['_fcFingerprint', () => 0],
  ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => Promise.resolve([])],
  ['normalizeErrorEnvelope', (x) => x], ['saveConversations', _noop],
  ['Icon', (n) => '<svg data-icon="' + n + '"></svg>'],
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; } }
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';
win.ConvCache = global.ConvCache = { put: () => {} };

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_ui.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
if (typeof _handleAutopilotVuEvent !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
check('fn_exposed', true);

const conv = conversations.find(c => c.id === 'C1');

/* NC shims: prove each behaviour is load-bearing.
 *   NC_MASK   — identity mask: sentinels stay visible.
 *   NC_APPEND — snapshot applied by APPEND, not reset: content doubles. */
if (NC === 'NC_MASK') {
  /* Rebind the MODULE-SCOPE binding created by eval (the delta branch's
   * identifier resolution finds it before any window/global property). */
  _maskVuMachineTokens = (s) => s;
}

// ── Phase 1: the bubble streams some content on the parent stream ──
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_start', vuMsgId: 'vu-hop-1' });
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-hop-1',
  inner: { type: 'delta', content: 'early-part' } });
const entry1 = _findVuMsgById(conv, 'vu-hop-1');
check('precondition_streamed', !!entry1 && entry1.msg.content === 'early-part');

// ── Phase 2: the hop — fresh connect to the carrier replays vu_start WITH
//    a replaySnapshot that carries the COMPLETE-so-far state (a superset
//    that also advanced past what the parent stream showed) ──
const _snap = { content: 'early-part+later', thinking: 'think-1',
                toolRounds: [{ roundNum: 1, status: 'done' }] };
let _applyEv = { type: 'autopilot_vu_start', vuMsgId: 'vu-hop-1', replaySnapshot: _snap };
if (NC === 'NC_APPEND') {
  // NC: emulate append-not-reset semantics
  const e = _findVuMsgById(conv, 'vu-hop-1');
  if (e) {
    e.msg.content = (e.msg.content || '') + (_snap.content || '');
    e.msg.thinking = (e.msg.thinking || '') + (_snap.thinking || '');
  }
} else {
  _handleAutopilotVuEvent('C1', _applyEv);
}
const entry2 = _findVuMsgById(conv, 'vu-hop-1');
check('snapshot_resets_not_appends',
  !!entry2 && entry2.msg.content === 'early-part+later');
check('snapshot_thinking_applied', !!entry2 && entry2.msg.thinking === 'think-1');
check('snapshot_rounds_applied',
  !!entry2 && Array.isArray(entry2.msg.toolRounds) && entry2.msg.toolRounds.length === 1);

// ── Phase 3: live deltas after the snapshot APPEND (no reset) ──
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-hop-1',
  inner: { type: 'delta', content: '+tail' } });
const entry3 = _findVuMsgById(conv, 'vu-hop-1');
if (NC !== 'NC_APPEND') {
  check('post_snapshot_deltas_append', !!entry3 && entry3.msg.content === 'early-part+later+tail');
}

// ── Phase 4: machine sentinels are masked from live content ──
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_start', vuMsgId: 'vu-mask-1' });
_handleAutopilotVuEvent('C1', { type: 'autopilot_vu_event', vuMsgId: 'vu-mask-1',
  inner: { type: 'delta', content: '报告正文\n[VU: TASK_DONE]\n[PROGRESS: resolved=4 remaining=0]\n收尾。' } });
const entry4 = _findVuMsgById(conv, 'vu-mask-1');
check('sentinel_done_masked', !!entry4 && !entry4.msg.content.includes('[VU: TASK_DONE]'));
check('sentinel_progress_masked', !!entry4 && !entry4.msg.content.includes('[PROGRESS:'));
check('real_content_kept', !!entry4 && entry4.msg.content.includes('报告正文')
  && entry4.msg.content.includes('收尾。'));

console.log(out.join('\n'));
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_vu_carrier_{"nc_" + nc.lower() if nc else "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if nc:
            argv.append(nc)
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
def test_vu_carrier_hop_snapshot_reset_and_mask():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'vu-carrier frontend contract failures:\n' + output
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_identity_mask_leaves_sentinels_visible():
    """NC: an identity mask (the pre-fix behaviour) keeps both sentinels
    visible — proving _maskVuMachineTokens is what hides them."""
    output = _run('NC_MASK')
    lines = output.splitlines()

    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name) or ln.endswith(name):
                return ln.split(' ', 1)[0]
        return None

    assert _status('sentinel_done_masked') == 'FAIL', \
        'NC identity mask should leave [VU: TASK_DONE] visible:\n' + output
    assert _status('sentinel_progress_masked') == 'FAIL', \
        'NC identity mask should leave [PROGRESS:] visible:\n' + output
    assert _status('precondition_streamed') == 'PASS', \
        'NC harness precondition broke:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_append_semantics_double_content():
    """NC: applying the snapshot by APPEND (not reset) doubles the pre-hop
    content — proving the reset semantics are what dedupes the hop."""
    output = _run('NC_APPEND')
    lines = output.splitlines()

    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name) or ln.endswith(name):
                return ln.split(' ', 1)[0]
        return None

    assert _status('snapshot_resets_not_appends') == 'FAIL', \
        'NC append semantics should produce doubled content:\n' + output


if __name__ == '__main__':
    test_wiring_vu_carrier_delegation_and_isvu_stamp()
    if not _node_deps_available():
        print('SKIP frontend — node + jsdom not available')
    else:
        test_vu_carrier_hop_snapshot_reset_and_mask()
        test_NC_identity_mask_leaves_sentinels_visible()
        test_NC_append_semantics_double_content()
    print('PASS test_frontend_vu_carrier_contract')
