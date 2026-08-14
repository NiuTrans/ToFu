"""Regression harness for the streaming-UI hot path (Cluster A/C).

WHY
---
``ui/streaming_ui.js`` holds two clusters with no prior jsdom coverage:

  • Cluster A (stays in streaming_ui.js): ``_getStreamZones`` /
    ``updateStreamingUI`` build the zone DOM and render content + thinking +
    a phase indicator incrementally.
  • Cluster C (extracted to ui/stream_lifecycle.js on 2026-06-27):
    ``showStreamingUIForConv`` / ``finishStream`` / the HG-translate helpers.
    ``finishStream`` clears orphaned ``awaiting_human`` / ``submitted`` tool
    rounds to ``done`` (the ``_hgCleaned`` path) so the sidebar amber dot
    clears when a task ends without an answer.

This harness loads the REAL shipped JS under jsdom and locks both contracts
so the Cluster-C extraction is a verified pure move. It must pass against the
monolith FIRST, then still pass after the split (the recipe).

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed. The harness eval's BOTH ui/streaming_ui.js (argv[2]) AND
ui/stream_lifecycle.js (argv[4]) in one shared scope — argv[4] is optional so
the harness works against the monolith (before the split) too.
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
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;   // neuter tickers
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

// ── Globals the streaming-UI code touches at load / render time ──
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};
// t() i18n — the streaming phase renderer emits user-visible labels via
// t('stream.phase.*') (migrated from hardcoded English). Return the REAL
// English strings (mirrors static/js/i18n.js) so phase labels render the
// same text production ships; fall back to key+n for any unmapped key so the
// deterministic '{key}:{n}' counter behavior other checks rely on is kept.
const _STREAM_PHASE_EN = {
  'stream.phase.reasoning': 'Reasoning',
  'stream.phase.deepThinking': 'Deep thinking',
  'stream.phase.chars': '{n} chars',
  'stream.phase.waitingModel': 'Sent to the model, waiting for it to start replying…',
  'stream.phase.retrying': 'Retrying…',
  'stream.phase.waiting': 'Waiting…',
  // New keys for the tool-exec phase contract (mirror static/js/i18n.js EN):
  'stream.phase.analyzingRound': 'Analyzing results and planning next step… (round {round})',
  'stream.phase.toolExec': '{tool}…',
  'stream.phase.toolExecMulti': 'Running {n} tools: {tools}…',
  'stream.phase.toolContext': 'prev: {tools}',
  'tool.label.read_files': 'Reading files',
  'tool.label.apply_diff': 'Patching files',
  'tool.label.join': ', ',
};
win.t = global.t = (k, o) => {
  let v = _STREAM_PHASE_EN[k];
  if (v === undefined) return k + (o && o.n != null ? (':' + o.n) : '');
  if (o) for (const kk in o) v = v.split('{' + kk + '}').join(o[kk]);
  return v;
};
// Hot-path no-ops / stubs (function-body refs, resolved at call time).
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
win._stampFreshness = global._stampFreshness = () => {};
win._fcFingerprint = global._fcFingerprint = () => 0;
win._extractFileChangesFromRoundsAsync = global._extractFileChangesFromRoundsAsync = () => ({ then: () => {} });
win._renderFileChangesHtml = global._renderFileChangesHtml = () => '';
win.renderMcpLoginHintHtml = global.renderMcpLoginHintHtml = () => '';
win.renderPreferencesAppliedHtml = global.renderPreferencesAppliedHtml = () => '';
win.renderPreferenceLearnedHtml = global.renderPreferenceLearnedHtml = () => '';
win.renderMemoryPrefetchHtml = global.renderMemoryPrefetchHtml = () => '';
// The unified turn-provenance strip (memory-prefetch + preferences +
// related-conversations + resolved-login) — updateStreamingUI renders it.
win.renderTurnProvenanceHtml = global.renderTurnProvenanceHtml = () => '';
win._isRoundSwarm = global._isRoundSwarm = (r) => !!(r && r._swarm);
// Program-call cards are implemented by core ui/tool_rounds.js, which this
// focused harness intentionally replaces with render stubs. Mirror the small
// shared predicate so streaming_ui.js is exercised against its real dependency
// contract without loading the entire tool-round renderer.
win._isProgramRound = global._isProgramRound = (r) => !!(r && r._programSynthetic);
win.convAutoTranslate = global.convAutoTranslate = (c) =>
  (c && c.autoTranslate !== undefined) ? !!c.autoTranslate
    : (typeof autoTranslate !== 'undefined' && autoTranslate !== undefined ? !!autoTranslate : false);
// finishStream now also consults the EFFECTIVE resolver (unification fix —
// live global toggle wins when ON). Stub it mirroring core/conversations.js.
win.convAutoTranslateEffective = global.convAutoTranslateEffective = (c) =>
  (typeof autoTranslate !== 'undefined' && autoTranslate) ? true
    : global.convAutoTranslate(c);
// _startAutoTranslateForMsg is defined in stream_lifecycle.js; the effective-ON
// branch calls it. This test keeps autoTranslate OFF so it never fires, but
// stub it defensively so a future ON case can't hit an undefined ref.
win._startAutoTranslateForMsg = global._startAutoTranslateForMsg = () => {};
win._renderUnifiedToolLine = global._renderUnifiedToolLine = () => '<div class="ptool-line"></div>';
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '<div class="sw-panel"></div>';
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win._buildSwarmInboxChipsHTML = global._buildSwarmInboxChipsHTML = () => '';
// Cluster C externals — all stubbed; finishStream calls them but we assert
// only the orphaned-HG cleanup, which is pure local mutation.
win.activeStreams = global.activeStreams = new Map();
win.streamBufs = global.streamBufs = new Map();
win.pendingMessageQueue = global.pendingMessageQueue = new Map();
win.saveConversations = global.saveConversations = () => {};
win.syncConversationToServer = global.syncConversationToServer = () => {};
win.ConvCache = global.ConvCache = { put: () => {} };
win.ConvView = global.ConvView = { finalizeStreaming: () => {} };
win.renderConversationList = global.renderConversationList = () => {};
win.updateSendButton = global.updateSendButton = () => {};
win.buildTurnNav = global.buildTurnNav = () => {};
win._convRenderFingerprint = global._convRenderFingerprint = () => 0;
win._findAutopilotPendingCarrier = global._findAutopilotPendingCarrier = () => null;
win._attachAutopilotFollowup = global._attachAutopilotFollowup = () => {};
win._checkForQueuedTask = global._checkForQueuedTask = () => {};
win._armAutoTranslateWatchdog = global._armAutoTranslateWatchdog = () => {};
win._maybeAutoGenerateTitle = global._maybeAutoGenerateTitle = () => {};
win.autoTranslate = global.autoTranslate = false;
win.CSS = global.CSS = undefined;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js
if (process.argv[4]) {
  eval(fs.readFileSync(process.argv[4], 'utf8'));  // ui/stream_lifecycle.js (after split)
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

for (const fn of ['updateStreamingUI', '_getStreamZones', 'finishStream']) {
  if (typeof eval(fn) !== 'function') { console.log('FAIL functions_exposed ' + fn + ' missing'); process.exit(0); }
}
check('functions_exposed', true);

// ── 1. updateStreamingUI builds zones + renders content + thinking + phase ──
const body = document.createElement('div');
body.id = 'streaming-body';
document.getElementById('chatInner').appendChild(body);
// minimal conv state for _stampFreshness lookup path
global.activeConvId = win.activeConvId = 'c1';
global.conversations = win.conversations = [{ id: 'c1', messages: [] }];

updateStreamingUI({
  content: 'Hello world body text',
  thinking: 'some reasoning here',
  toolRounds: [],
  phase: { phase: 'tool_exec', detail: 'Patching files', tools: ['apply_diff'] },
});
check('zones_built', !!body.querySelector('[data-zone="content"]') && !!body.querySelector('[data-zone="thinking"]') && !!body.querySelector('[data-zone="status"]'));
check('content_rendered', body.querySelector('[data-zone="content"]').innerHTML.includes('Hello world body text'));
check('thinking_rendered', body.querySelector('[data-zone="thinking"]').textContent.includes('some reasoning here'));
/* ★ STALE-PHASE GUARD (owner report 2026-08-03): a tool_exec phase with NO
 * in-flight tool round is a LIE — every tool already settled (or the tool
 * cards were never announced on this lane), yet the row claims a tool is
 * running RIGHT NOW ("✏️ Applying changes…" while only settled cards sit
 * above). The phase row must NOT render in that window; the tool cards are
 * the live record. FAILING-FIRST: pre-fix this rendered the raw detail. */
check('phase_tool_exec_stale_suppressed',
  !body.querySelector('[data-zone="status"]').innerHTML.includes('Patching files'));

// Waiting phase when no content/thinking yet
const body2host = document.getElementById('chatInner');
body.remove();
const body2 = document.createElement('div'); body2.id = 'streaming-body';
body2host.appendChild(body2);
updateStreamingUI({ content: '', thinking: '', toolRounds: [], phase: null });
check('waiting_phase', body2.querySelector('[data-zone="status"]').innerHTML.includes('Waiting'));

// ── 1b. Blank-bubble regression (tablet): a selection-active FIRST frame with
//    an empty buffer must NOT leave a fully-blank body. _ensureStreamZones
//    wipes the seeded pulse; updateStreamingUI then early-returns on the
//    _hasSelectionInStreaming() guard BEFORE writing the status zone. The fix
//    seeds the waiting pulse inside _ensureStreamZones so the wipe can never
//    produce empty zones with no pulse (the "blank Agent bubble" bug).
body2.remove();
const body3 = document.createElement('div'); body3.id = 'streaming-body';
body2host.appendChild(body3);
// Report an active selection anchored on #streaming-body ITSELF. A child text
// node would be detached by _ensureStreamZones' innerHTML wipe (so the guard
// would not fire under jsdom); a select-all / triple-click in a real browser
// anchors on the container element, which survives the wipe — that is the
// scenario that reproduces the blank bubble.
global.getSelection = win.getSelection = () => ({
  isCollapsed: false, rangeCount: 1,
  getRangeAt: () => ({ startContainer: body3, endContainer: body3 }),
});
updateStreamingUI({ content: '', thinking: '', toolRounds: [], phase: null });
// Restore the collapsed-selection stub for later checks.
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });
check('blank_bubble_guard', body3.querySelector('.stream-status') !== null
  && body3.querySelector('.pulse') !== null);

/* ── 1c. tool_exec phase-row contract (gate + localization) ──
 * The row renders ONLY while a round is genuinely in-flight (here:
 * pending_approval — in-flight but NOT 'searching', the case the old
 * hasActiveSearch gate under-covered), and composes its label from the
 * structured phase.tools list through i18n — never the raw English detail. */
function _freshBody() {
  const old = document.getElementById('streaming-body');
  if (old) old.remove();
  const b = document.createElement('div');
  b.id = 'streaming-body';
  document.getElementById('chatInner').appendChild(b);
  return b;
}
const bodyExec = _freshBody();
updateStreamingUI({
  content: '', thinking: '',
  toolRounds: [{ roundNum: 1, status: 'pending_approval', toolName: 'apply_diff', query: 'apply_diff a.py' }],
  phase: { phase: 'tool_exec', detail: 'LEGACY-ENGLISH-DETAIL', tools: ['apply_diff'] },
});
const _execHtml = bodyExec.querySelector('[data-zone="status"]').innerHTML;
check('phase_tool_exec_inflight_localized',
  _execHtml.includes('Patching files') && !_execHtml.includes('LEGACY-ENGLISH-DETAIL'));

/* Multi-tool dispatch: N counted over the calls, labels deduped + localized. */
const bodyMulti = _freshBody();
updateStreamingUI({
  content: '', thinking: '',
  toolRounds: [{ roundNum: 1, status: 'executing', toolName: 'read_files', query: 'a.py' }],
  phase: { phase: 'tool_exec', detail: 'Executing 3 tools: Reading files, Patching files', tools: ['read_files', 'apply_diff', 'apply_diff'] },
});
const _multiHtml = bodyMulti.querySelector('[data-zone="status"]').innerHTML;
check('phase_tool_exec_multi_localized',
  _multiHtml.includes('Running 3 tools') && _multiHtml.includes('Reading files')
  && _multiHtml.includes('Patching files') && !_multiHtml.includes('Executing'));

/* Round-open llm_thinking suffix: compose from toolContextTools (structured)
 * — the raw English toolContext string is the headless fallback only. */
const bodyCtx = _freshBody();
updateStreamingUI({
  content: '', thinking: '',
  toolRounds: [],
  phase: { phase: 'llm_thinking', detailKey: 'stream.phase.analyzingRound', detailArgs: { round: 2 }, toolContext: 'RAW-ENGLISH-CTX', toolContextTools: ['read_files', 'apply_diff'] },
});
const _ctxHtml = bodyCtx.querySelector('[data-zone="status"]').innerHTML;
check('phase_tool_context_localized',
  _ctxHtml.includes('Reading files') && _ctxHtml.includes('Patching files')
  && !_ctxHtml.includes('RAW-ENGLISH-CTX'));

/* Legacy phase (no toolContextTools — older backend / headless replay):
 * the raw toolContext string still renders (fallback preserved). */
const bodyLegacy = _freshBody();
updateStreamingUI({
  content: '', thinking: '',
  toolRounds: [],
  phase: { phase: 'llm_thinking', detail: 'Analyzing…', toolContext: 'legacy ctx string' },
});
check('phase_tool_context_legacy_fallback',
  bodyLegacy.querySelector('[data-zone="status"]').innerHTML.includes('legacy ctx string'));
bodyLegacy.remove();

/* VU-forwarded tool_exec (detail-only, NO tools list) + an in-flight round:
 * renders the raw English detail — and must NOT crash. This pins the
 * self-contained fallback: the i18n-resolving _phaseDetailText is nested
 * inside updateStreamingUI and is NOT reachable from the module-level
 * helper (a blind call there is a ReferenceError on exactly this frame —
 * caught by the N4 neuter pass, 2026-08-03). */
const bodyVu = _freshBody();
updateStreamingUI({
  content: '', thinking: '',
  toolRounds: [{ roundNum: 1, status: 'submitted', toolName: 'apply_diff', query: 'apply_diff a.py' }],
  phase: { phase: 'tool_exec', detail: 'apply_diff a.py' },
});
const _vuHtml = bodyVu.querySelector('[data-zone="status"]').innerHTML;
check('phase_tool_exec_no_tools_fallback',
  _vuHtml.includes('apply_diff a.py'));
bodyVu.remove();

// ── 1d. execute_tools is a protocol adapter, never a live tool row/count ──
const bodyGateway = _freshBody();
updateStreamingUI({
  content: '', thinking: '', phase: null,
  toolRounds: [{ roundNum: 1, llmRound: 0, status: 'done',
                 toolName: 'execute_tools', query: 'execute_tools' }],
});
check('execute_gateway_only_hidden',
  !bodyGateway.querySelector('.ptool-panel')
  && !bodyGateway.querySelector('[data-prn="1"]'));

updateStreamingUI({
  content: '', thinking: '', phase: null,
  toolRounds: [
    { roundNum: 1, llmRound: 0, status: 'done',
      toolName: 'execute_tools', query: 'execute_tools' },
    { roundNum: 8700000, llmRound: 0, status: 'error',
      toolName: 'read_files', query: 'read_files' },
  ],
});
const _gatewayPanel = bodyGateway.querySelector('.ptool-panel');
check('execute_gateway_child_only_visible',
  !!_gatewayPanel
  && !_gatewayPanel.querySelector('[data-prn="1"]')
  && !!_gatewayPanel.querySelector('[data-prn="8700000"]')
  && _gatewayPanel.querySelectorAll('[data-prn]').length === 1
  && _gatewayPanel.querySelector('.ptool-panel-label').textContent.includes(':1'));
bodyGateway.remove();

// ── 2. finishStream clears orphaned awaiting_human / submitted rounds to done ──
const conv = {
  id: 'c1', title: 'T', activeTaskId: 't1',
  autoTranslate: false,
  messages: [
    { role: 'user', content: 'q' },
    { role: 'assistant', content: 'partial answer text', toolRounds: [
      { roundNum: 1, status: 'awaiting_human', guidanceId: 'g1' },
      { roundNum: 2, status: 'submitted' },
      { roundNum: 3, status: 'done' },
    ] },
  ],
};
global.conversations = win.conversations = [conv];
global.activeConvId = win.activeConvId = 'c1';
global.activeStreams = win.activeStreams = new Map([['c1', {}]]);
document.getElementById('chatInner').innerHTML = '';

finishStream('c1');

const r = conv.messages[1].toolRounds;
check('hg_awaiting_cleared', r[0].status === 'done' && r[0].guidanceId === null && r[0]._hgSkipped === true);
check('hg_submitted_cleared', r[1].status === 'done' && r[1]._hgSkipped === true);
check('hg_done_untouched', r[2].status === 'done' && !r[2]._hgSkipped);
check('active_stream_removed', !global.activeStreams.has('c1'));
check('active_task_cleared', conv.activeTaskId === null);

console.log(out.join('\n'));
"""


def _run(extra_argv):
    harness = os.path.join(HERE, '_streaming_ui_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),   # argv[2]
             ROOT,                                            # argv[3]
             *extra_argv,                                     # argv[4] (optional)
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
    assert not fails, 'streaming-UI failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_streaming_ui_hot_path():
    # argv[4] = stream_lifecycle.js once it exists; harmless to pass when the
    # file is present, omitted (monolith) otherwise.
    lifecycle = os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js')
    extra = [lifecycle] if os.path.exists(lifecycle) else []
    _run(extra)
