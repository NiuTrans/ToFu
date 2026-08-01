#!/usr/bin/env python3
"""ConvView live-task twin guard — one message must never render as both a
static `msg-N` node AND the live `#streaming-msg`.

Production evidence (2026-07-31, conv ms8nqhwgur07jm, task bddcb6f9):
a zero-token retry-storm turn (137 phase events, first delta 6 minutes in,
zero premature terminal frames — the terminal_gate family observably worked)
rendered TWO assistant bubbles for ONE message: a static full-date bubble
carrying only the turn-provenance strip + a lone model pill (the exact
fingerprint of `renderMessage(liveStub)`, reproduced byte-shape in the
harness) AND the live streaming bubble with the elapsed timer + retry phase.

Mechanics, reproduced in JSDOM: `ConvView.apply`'s live-bubble refusal
resolves the target by `data-msg-id`. When the message's `_msgId` has DRIFTED
from the id stamped on the bubble (re-mint / server-adopted copy / bubble
stamped before the id existed), resolution misses, the live-bubble guard
never fires, and the append branch inserts a static twin. `_evictByMsgId`
cannot connect the two nodes (different ids) and explicitly exempts
`#streaming-msg`.

Fix (static/js/conv_view.js, append branch only): when a `#streaming-msg`
exists and the conv has a live stream, applying THE SAME OBJECT the stream
accumulates into (`msg === stream.assistantMsg`) is refused loudly with a
stack trace (the refusal IS the forensic line — the next recurrence names
its writer); appending a DIFFERENT object stamped with the live task's
`_taskId` is allowed (endpoint/VU lanes legitimately re-apply settled
messages of the live task) but warn-logged with a stack.

Guards:
  test_baseline_single_bubble_through_event_sequence — the 16:17 event
        sequence (memprefetch + preferences + retry phases, zero deltas)
        renders exactly one live bubble.
  test_twin_repro_refused_after_msgid_drift — the T5 reproduction: re-mint
        the stub's _msgId, apply → REFUSED, no msg-4 node, error logged.
  test_NEUTER_guard_removed_twin_returns — same scenario against a
        guard-stripped conv_view.js → the static twin APPEARS (proves the
        guard is the load-bearing barrier, not the harness).
  test_replace_in_place_unaffected — a settled message with an existing
        node is still replaced in place (the guard only gates appends).
  test_same_task_nonbound_object_warns_not_refuses — endpoint/VU-safe lane:
        a different object of the live task appends, with the drift warn.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_convview_live_twin_guard.py
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR, ROOT

pytestmark = pytest.mark.unit

CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')

_BODY = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const JS = (...p) => path.join(ROOT, 'static', 'js', ...p);
const { setup } = require(process.env.JSDOM_HARNESS);

const conversations = [];
const activeStreams = new Map();
const beaconCalls = [];

const globals = {
  conversations, activeStreams, AbortController,
  debugLog: () => {}, saveConversations: () => {}, renderConversationList: () => {},
  updateSendButton: () => {}, updateContextBar: () => {}, buildTurnNav: () => {},
  scrollToBottom: () => {}, isNearBottom: () => true, _forceScrollToBottom: () => {},
  _withInstantScroll: (ct, fn) => fn(), _captureScrollAnchor: () => null,
  _restoreScrollAnchor: () => {}, _destroyLazyObserver: () => {}, _ensureLazyObserver: () => {},
  _surgicalTruncateDOM: () => false, _convRenderFingerprint: () => 'fp',
  ConvCache: { put: () => {} }, serverModel: 'kimi-k3',
  getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
  _ensureMsgId: (m) => { if (!m._msgId) m._msgId = 'tmp_' + Math.random().toString(36).slice(2); return m._msgId; },
  twUpdate: () => {}, twStop: () => {}, twStart: () => {},
  Api: { chat: { poll: async () => null }, conversations: { get: async () => null }, post: async () => ({ ok: true }),
         clientError: { report: (p) => { beaconCalls.push(p); return true; } } },
  EventSource: class { constructor() {} close() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  _reportClientError: () => {}, _checkServerHealth: async () => true,
  _startOfflineRecoveryPolling: () => {}, showToast: () => {},
  _applySettingsToConv: () => {}, _restoreConvToolState: () => {},
  _reconnectServerTaskIfIdle: () => false, loadConversationsFromServer: async () => {},
  _bootLoadHeld: () => false, _editingMsgIdx: null, loadConversationMessages: async () => {},
  showMessagesInDebug: () => {}, stripNoTranslateTags: (s) => s,
  renderMcpLoginHintHtml: () => '', _buildSwarmInboxChipsHTML: () => '',
  errorEnvelopeKind: () => null, formatClockTime: () => '16:17',
  _convMainTurnInFlight: () => true,
  getConvById: (id) => conversations.find((c) => c.id === id) || null,
  raw: (s) => s,
  safeHtml: (strings, ...vals) => strings.reduce((acc, s, i) => acc + s + (i < vals.length ? vals[i] : ''), ''),
  renderTurnProvenanceHtml: () => '<div class="tp-strip"></div>',
  _activeBranch: null,
  renderFileChangesBar: () => '', renderContextBar: () => '', renderUsageBar: () => '',
  renderCostBar: () => '', renderToolRounds: () => '', renderMessageActions: () => '',
  renderFinishBar: () => '', renderMemoryChips: () => '', renderModifiedFilesBar: () => '',
  renderCompactionCard: () => '', renderSearchResults: () => '', renderAttachments: () => '',
  hljs: { highlight: (s) => ({ value: s }), highlightAuto: (s) => ({ value: s }) },
  katex: { render: () => {} }, calcCostCny: () => 0, fmtCost: () => '', fmtTokens: () => '',
};

const targets = [
  JS('core', 'conv_reducers.js'),
  JS('core', 'translation_model.js'),
  JS('core', 'chatinner_dom.js'),
  process.argv[2],  // conv_view.js (or the NEUTER stripped copy)
  JS('ui', 'stream_session.js'),
  JS('ui', 'streaming_render.js'),
  JS('ui', 'streaming_ui.js'),
  JS('ui', 'chat_render.js'),
  JS('ui', 'stream_lifecycle.js'),
  JS('ui', 'translation_render.js'),
  JS('ui', 'sse_handlers_misc.js'),
  JS('ui', 'finish_info.js'),
];

/* Capture console.error / console.warn for the forensic-line assertions. */
const errLog = [], warnLog = [];
const _ce = console.error, _cw = console.warn;
console.error = (...a) => { errLog.push(a.join(' ')); _ce(...a); };
console.warn = (...a) => { warnLog.push(a.join(' ')); _cw(...a); };

const { window, document, check, report } = setup({
  root: ROOT,
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div><div id="convList"></div><textarea id="userInput"></textarea></body>',
  targets: [],
  globals,
});
/* Bundle-faithful: ONE concatenated eval so top-level let/const module state
 * is shared across files exactly as in the shipped bundle. */
(0, eval)(targets.map((f) => fs.readFileSync(f, 'utf8')).join('\n;\n'));

const convId = 'conv1';
const stub = { _msgId: 'tmp_AAA', role: 'assistant', content: '', thinking: '',
               toolRounds: [], model: 'kimi-k3', timestamp: Date.now() };
const settled = { _msgId: 'm_a1', role: 'assistant', content: 'a1-answer',
                  finishReason: 'stop', model: 'kimi-k3', timestamp: Date.now() - 60000 };
const conv = { id: convId, messages: [
  { _msgId: 'm_u0', role: 'user', content: 'q1' },
  settled,
  { _msgId: 'm_s2', role: 'assistant', content: '', thinking: '', toolRounds: [], model: 'kimi-k3', _trimmed: true, timestamp: Date.now() - 59000 },
  { _msgId: 'm_u3', role: 'user', content: 'q2', timestamp: Date.now() - 1000 },
  stub,
] };
conversations.push(conv);
global.activeConvId = convId; window.activeConvId = convId;
activeStreams.set(convId, { controller: new AbortController(), taskId: 'TASK1', assistantMsg: stub });

const nodes = () => document.querySelectorAll('#chatInner .message').length;

// ── the 16:17 sequence ──
window.ConvView.startStreaming(convId, { role: 'worker', msgId: stub._msgId, timeStr: '16:17' });
const ctx = { convId, taskId: 'TASK1', assistantMsg: stub, epCriticPhase: false, epCriticMsg: null };
global._handleMemoryPrefetch({ phase: 'done', selected: 0, candidates: 3, bm25_ms: 183, rerank_ms: 0, total_ms: 2000 }, ctx);
global._handlePreferencesApplied({ chars: 900, items: new Array(9).fill('x') }, ctx);
global.setStreamPhase(convId, { phase: 'retrying', detailKey: 'stream.phase.retryReason',
  detailArgs: { reasonKey: 'stream.retryReason.waitingSharedProject', model: 'kimi-k3', attempt: 120 }, attempt: 120 });
global.updateStreamingUI({ thinking: '', content: '', toolRounds: [],
  phase: (global.streamSessions.get(convId) || {}).phase, _memoryPrefetch: stub._memoryPrefetch });
check('baseline: single live bubble through the retry-storm event sequence',
      nodes() === 1 && !!document.getElementById('streaming-msg'));

// ── mid-stream full render: still one live bubble, orphan stub suppressed ──
window.ConvView.replaceAll(convId, { forceScroll: false });
check('mid-stream renderChat: one live bubble, no static tail, orphan suppressed',
      nodes() === 4 && !!document.getElementById('streaming-msg')
      && !document.getElementById('msg-2') && !document.getElementById('msg-4'));

// ── T5: msgId drift → apply must now be REFUSED (the twin guard) ──
stub._msgId = 'tmp_BBB';
const refused = window.ConvView.apply(convId, 4, stub);
check('twin repro: apply after msgId drift is refused', refused === false);
check('twin repro: no static msg-4 node appeared', !document.getElementById('msg-4'));
check('twin repro: live bubble intact', !!document.getElementById('streaming-msg'));
check('twin repro: forensic error logged with live-task-twin marker',
      errLog.some((l) => l.includes('live-task twin')));
check('twin repro: forensic line BEACONS to the server (client-error report)',
      beaconCalls.length === 1
      && beaconCalls[0].extra && beaconCalls[0].extra.kind === 'live-task-twin-refused'
      && String(beaconCalls[0].message).includes('live-task twin trace')
      && beaconCalls[0].extra.site === 'ConvView.apply');

// ── positive control: replace-in-place of a settled message still works ──
settled.content = 'a1-answer-v2';
const replaced = window.ConvView.apply(convId, 1, settled);
check('replace-in-place unaffected (settled msg, existing node)',
      replaced === true && document.getElementById('msg-1')
      && document.getElementById('msg-1').innerHTML.includes('a1-answer-v2'));

// ── same-task non-bound object: allowed, but the drift warn fires ──
const serverCopy = { _msgId: 'srv_999', role: 'assistant', content: '', thinking: '',
                     toolRounds: [], model: 'kimi-k3', _taskId: 'TASK1', timestamp: Date.now() };
conv.messages.push(serverCopy);
const appended = window.ConvView.apply(convId, 5, serverCopy);
check('same-task non-bound object: append allowed (endpoint/VU-safe lane)', appended === true);
check('same-task non-bound object: drift warn logged',
      warnLog.some((l) => l.includes('same-task drift trace') || l.includes('twin risk')));
check('same-task non-bound object: drift warn BEACONS too',
      beaconCalls.length === 2
      && beaconCalls[1].extra && beaconCalls[1].extra.kind === 'same-task-drift-warn'
      && String(beaconCalls[1].message).includes('same-task drift trace'));

report();
"""

_NEUTER_BODY = _BODY.replace(
    "check('twin repro: apply after msgId drift is refused', refused === false);",
    "check('NEUTER: without the guard the apply goes through', refused === true);",
).replace(
    "check('twin repro: no static msg-4 node appeared', !document.getElementById('msg-4'));",
    "check('NEUTER: without the guard the static twin msg-4 APPEARS', !!document.getElementById('msg-4'));",
).replace(
    "check('twin repro: live bubble intact', !!document.getElementById('streaming-msg'));\n",
    "",
).replace(
    "check('twin repro: forensic error logged with live-task-twin marker',\n"
    "      errLog.some((l) => l.includes('live-task twin')));",
    "",
).replace(
    "check('twin repro: forensic line BEACONS to the server (client-error report)',\n"
    "      beaconCalls.length === 1\n"
    "      && beaconCalls[0].extra && beaconCalls[0].extra.kind === 'live-task-twin-refused'\n"
    "      && String(beaconCalls[0].message).includes('live-task twin trace')\n"
    "      && beaconCalls[0].extra.site === 'ConvView.apply');",
    "check('NEUTER: no beacon without the guard after the twin apply',\n"
    "      beaconCalls.length === 0);",
).replace(
    "check('same-task non-bound object: drift warn logged',\n"
    "      warnLog.some((l) => l.includes('same-task drift trace') || l.includes('twin risk')));",
    "check('NEUTER: drift warn is part of the guard and is gone too',\n"
    "      !warnLog.some((l) => l.includes('same-task drift trace') || l.includes('twin risk')));",
).replace(
    "check('same-task non-bound object: drift warn BEACONS too',\n"
    "      beaconCalls.length === 2\n"
    "      && beaconCalls[1].extra && beaconCalls[1].extra.kind === 'same-task-drift-warn'\n"
    "      && String(beaconCalls[1].message).includes('same-task drift trace'));",
    "check('NEUTER: still no beacon after the drift append', beaconCalls.length === 0);",
)


def _guard_stripped_copy(tmp_path):
    """conv_view.js with the live-task twin guard removed (NEUTER)."""
    with open(CONV_VIEW, encoding='utf-8') as f:
        src = f.read()
    start = src.index("        /* ★ LIVE-TASK TWIN GUARD")
    end = src.index("        /* ★ ORDER-INVARIANT LOUD WARN")
    stripped = src[:start] + src[end:]
    assert 'LIVE-TASK TWIN GUARD' not in stripped
    p = tmp_path / 'conv_view.js'
    p.write_text(stripped, encoding='utf-8')
    return str(p)


def test_baseline_and_guard_and_controls(tmp_path):
    run_harness(CONV_VIEW, _BODY, min_pass=9, label='live-twin-guard')


def test_NEUTER_guard_removed_twin_returns(tmp_path):
    run_harness(_guard_stripped_copy(tmp_path), _NEUTER_BODY, min_pass=7,
                label='live-twin-guard-NEUTER')
