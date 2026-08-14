"""tests/test_frontend_conv_notify_push.py — regression for the event-driven
cross-device conversation sync handler (_onConvNotifyPush) + the NON-DESTRUCTIVE
active-conv verify (_verifyActiveConvFromServer).

WHY
---
Cross-device sync used to be PULL-only (refocus + a 25s poll) — the "needs a
manual refresh" pain. The server now emits a tiny `notify` push frame on every
conversation mutation (lib/conversations/meta_cache.py::notify_conv_changed):

    { type:'conv_changed'|'conv_deleted', convId, rev?, userId }

`_onConvNotifyPush` (static/js/core/cross_tab_sync.js) consumes it:
  • rev-GATE: rev <= conv._serverRev → NO-OP.
  • metadata-only (no rev) / unknown conv → debounced sidebar refresh.
  • ACTIVE conv, genuinely-newer rev → DEBOUNCED, NON-DESTRUCTIVE verify via
    `_verifyActiveConvFromServer` (NOT loadConversationMessages — whose Phase-1
    replaces conv.messages from the IDB cache + full renderChat, the root cause
    of the "empty/partial agent bubble" + "positions keep shifting" reports).
    The verify adopts in place and covers BOTH:
      (1) server has MORE messages → adopt the full set;
      (2) SAME message count but the trailing assistant turn GREW in place
          (filled-in / regenerated / continued / persisted-short-then-extended)
          → adopt the longer content/thinking/toolRounds. `forceRecoverFromServer`
          MISSES (2) (guard is count-only) — that gap left a viewing device on a
          stale/empty bubble until manual refresh.
    Re-renders ONLY when something actually changed (no scroll reset on a no-op).
  • SELF-ECHO (two-writer race): a completed turn is written to the server TWICE
    — backend task-save (emits the frame) + this device's finishStream PUT. The
    backend frame can beat our PUT's _serverRev advance, so the rev-gate misses
    it. A `_localWriteAt` stamp makes a frame within _CONV_SELF_ECHO_MS a no-op;
    the debounce also lets our PUT land and collapse the frame to a rev-gate
    no-op inside the timer.
  • BACKGROUND conv → mark _needsLoad + debounced sidebar refresh; never repaints.
  • conv_deleted → _applyRemoteConvDeleted. multi-user → drop foreign userId.

This harness loads the REAL shipped cross_tab_sync.js under node, stubs the
window globals + Api.conversations.get, and drives the handler directly.
setTimeout is captured (not auto-fired) so the debounce runs on fireTimers().

DOUBLE-NEUTERS (on a MUTATED copy; the shipped file is never modified):
  • strip both _localWriteAt guards → a just-locally-written conv WRONGLY
    verifies.
  • strip the equal-count content-grew adopt branch → a same-count/longer-content
    server copy is NOT adopted (bubble stays stale), proving that branch — the
    fix for the reported symptom — is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

CROSS_TAB_SYNC = runtime_section_path('core/cross_tab_sync.js')
CONV_REDUCERS = runtime_section_path('core/conv_reducers.js')
CONV_STATE_REDUCER = runtime_section_path('core/conv_state_reducer.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Observable side-effects ──
let getCalls = [];           // Api.conversations.get(convId)
let renderChatCalls = [];    // renderChat(conv) — a repaint (adopt happened)
let saveCalls = [];          // saveConversations(convId)
let listRefreshCalls = 0;    // loadConversationsFromServer()
let bodyRefetchCalls = [];   // loadConversationMessages — must NEVER run for active
let serverResponse = null;   // what Api.conversations.get resolves to
let getImpl = null;          // optional deferred-response seam for single-flight
let reconnectCalls = [];     // _reconnectServerTaskIfIdle(convId)
let sendBtnCalls = 0;        // updateSendButton()
let reconnectReturns = false;// what _reconnectServerTaskIfIdle returns

// ── Window-scope globals cross_tab_sync.js touches at load + runtime ──
global._syncChannel = null;
global.TAB_ID = 'tab-test';
global.debugLog = () => {};
global.conversations = [];
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.activeConvId = null;
global.addEventListener = () => {};
global.document = { visibilityState: 'visible', addEventListener: () => {} };
let _timers = [];
global.setTimeout = (fn, ms) => { _timers.push(fn); return _timers.length; };
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
function fireTimers() { const t = _timers; _timers = []; t.forEach((fn) => { try { fn(); } catch (e) {} }); }

global.ConvCache = { remove: () => {}, put: () => {}, get: async () => null };
global.loadConversation = () => {};
global.newChat = () => {};
global.renderConversationList = () => {};
global.renderChat = (conv) => { renderChatCalls.push(conv && conv.id); };
/* The shipped repaint seam moved from renderChat(conv) to
 * window.ConvView.replaceAll(convId) — stub it into the SAME counter so the
 * '*_rendered' assertions keep their original meaning. */
global.ConvView = { replaceAll: (id) => renderChatCalls.push(id) };
global.saveConversations = (id) => { saveCalls.push(id); };
global._applySettingsToConv = (conv, settings) => { if (settings && settings.activeTaskId && conv && !conv.activeTaskId && !conv._activeTaskClearedAt) conv.activeTaskId = settings.activeTaskId; };
global._restoreConvToolState = () => {};
global._reconnectServerTaskIfIdle = (id) => { reconnectCalls.push(id); return reconnectReturns; };
global.updateSendButton = () => { sendBtnCalls++; };
global.loadConversationsFromServer = async () => { listRefreshCalls++; };
global.loadConversationMessages = async (id) => { bodyRefetchCalls.push(id); };
global.pushIsConnected = () => true;
global.pushSubscribe = () => {};
global.Api = { conversations: { get: async (id) => {
  getCalls.push(id);
  return getImpl ? getImpl(id) : serverResponse;
} } };

const SRC = fs.readFileSync(process.argv[2], 'utf8');
// Epic-E slices: _verifyActiveConvFromServer now calls _mergeTerminalTurnFields
// (conv_reducers.js) — load the REAL reducer first (same pattern as the
// conv_persist_helpers harness), so the verify path executes for real.
const REDUCERS = fs.readFileSync(process.argv[3], 'utf8');
// _onConvNotifyPush's multi-user gate delegates to window._frameIsOurs, whose
// real implementation lives in conv_state_reducer.js — load it too, else the
// gate fails OPEN in the harness and other_user_dropped can't be exercised.
const STATE_REDUCER = fs.readFileSync(process.argv[4], 'utf8');
function loadModule(src) { (0, eval)(src); }
loadModule(REDUCERS);
loadModule(STATE_REDUCER);

function reset() {
  getCalls = []; renderChatCalls = []; saveCalls = [];
  listRefreshCalls = 0; bodyRefetchCalls = []; serverResponse = null; getImpl = null;
  reconnectCalls = []; sendBtnCalls = 0; reconnectReturns = false;
  _timers = [];
  global.conversations = [];
  global.activeStreams = new Map();
  global._editingMsgIdx = null;
  global.activeConvId = null;
  global._currentUserId = undefined;
  window._bootLoadInFlight = false;
}

const flush = () => new Promise((r) => setImmediate(r));
// The verify is async (awaits Api.get); give microtasks a few turns to settle.
const settle = async () => { for (let i = 0; i < 5; i++) await flush(); };

(async () => {
  loadModule(SRC);
  check('fn_exposed', typeof _onConvNotifyPush === 'function');
  check('verify_fn_exposed', typeof _verifyActiveConvFromServer === 'function');
  check('wire_fn_exposed', typeof _wireConvSyncPush === 'function');

  // ══ 1. Active newer rev → DEBOUNCED verify; server has MORE msgs → adopt full ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [{ role: 'user', content: 'q' }] }];
    activeConvId = 'c1';
    serverResponse = { rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'answer' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush();
    check('active_deferred_no_immediate_get', getCalls.length === 0);
    check('active_never_destructive_body_load', bodyRefetchCalls.length === 0);
    fireTimers(); await settle();
    check('active_get_after_debounce', getCalls.length === 1 && getCalls[0] === 'c1');
    check('active_adopted_new_msg', conversations[0].messages.length === 2);
    check('active_rendered', renderChatCalls.length === 1);
    check('active_rev_advanced', conversations[0]._serverRev === 6);
  }

  // ══ 2. THE GAP FIX: SAME count, trailing assistant GREW in place → adopt ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: '' },   // empty/partial bubble locally
    ] }];
    activeConvId = 'c1';
    serverResponse = { rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'the full answer', thinking: 'reasoning' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('equalcount_get_issued', getCalls.length === 1);
    check('equalcount_content_adopted', conversations[0].messages[1].content === 'the full answer');
    check('equalcount_thinking_adopted', conversations[0].messages[1].thinking === 'reasoning');
    check('equalcount_rendered', renderChatCalls.length === 1);
    check('equalcount_count_unchanged', conversations[0].messages.length === 2);
  }

  // ══ 2b. NO-OP: same count, server NOT longer → GET but no adopt/render ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'already have it' },
    ] }];
    activeConvId = 'c1';
    serverResponse = { rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'already have it' },   // identical
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('noop_get_issued', getCalls.length === 1);
    check('noop_not_rendered', renderChatCalls.length === 0);  // no scroll reset on no-op
    check('noop_rev_still_advanced', conversations[0]._serverRev === 6);  // won't re-verify
  }

  // ══ 2c. KEEP-LONGER: local content is LONGER than server → do NOT shrink ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'a much longer local answer already streamed' },
    ] }];
    activeConvId = 'c1';
    serverResponse = { rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'short' },   // server shorter (stale)
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('keeplonger_not_shrunk', conversations[0].messages[1].content === 'a much longer local answer already streamed');
    check('keeplonger_not_rendered', renderChatCalls.length === 0);
  }

  // ══ 2d. CROSS-DEVICE LIVE TURN: adopted turn is still generating server-side
  //         → reconnect the live task + refresh the composer button (Stop). ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
    ] }];
    activeConvId = 'c1';
    reconnectReturns = true;   // server task still running → connectToTask attaches
    serverResponse = { rev: 6, settings: { activeTaskId: 'task-live' }, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'partial so far' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('liveturn_activeTaskId_restored', conversations[0].activeTaskId === 'task-live');
    check('liveturn_reconnect_attempted', reconnectCalls.length === 1 && reconnectCalls[0] === 'c1');
    check('liveturn_sendbtn_refreshed', sendBtnCalls === 1);
    // Reconnect repaints via showStreamingUIForConv → skip the static renderChat.
    check('liveturn_no_double_paint', renderChatCalls.length === 0);
  }

  // ══ 2e. NOT live server-side (reconnect returns false) → static render + btn ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
    ] }];
    activeConvId = 'c1';
    reconnectReturns = false;  // task already finished server-side
    serverResponse = { rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'the settled answer' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('settled_static_rendered', renderChatCalls.length === 1);
    check('settled_sendbtn_refreshed', sendBtnCalls === 1);
  }

  // ══ 3. rev-GATE: equal / older rev → NO-OP, no GET ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 6, messages: [{}] }];
    activeConvId = 'c1';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 4, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('revgate_no_get', getCalls.length === 0);
  }

  // ══ 3b. A newer rev DURING a slow verify: one in-flight GET, then one
  //          bounded catch-up GET. Never concurrent; never lose rev 7. ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
    ] }];
    activeConvId = 'c1';
    const resolvers = [];
    getImpl = () => new Promise((resolve) => { resolvers.push(resolve); });

    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    fireTimers(); await settle();
    check('singleflight_first_get_started', getCalls.length === 1 && resolvers.length === 1);

    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 7, userId: 1 });
    fireTimers(); await settle();
    check('singleflight_no_concurrent_duplicate', getCalls.length === 1);

    resolvers[0]({ rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'rev six' },
    ] });
    await settle();
    check('singleflight_catchup_started', getCalls.length === 2 && resolvers.length === 2);

    resolvers[1]({ rev: 7, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'rev seven is longer' },
    ] });
    await settle();
    check('singleflight_newest_rev_adopted', conversations[0]._serverRev === 7);
    check('singleflight_newest_body_adopted',
      conversations[0].messages[1].content === 'rev seven is longer');
  }

  // ══ 4. SELF-ECHO fast-path: fresh local PUT (_localWriteAt) → no DESTRUCTIVE verify ══
  //    2026-07-27: the frame is no longer discarded outright — it diverts to the
  //    TRANSLATION-ONLY lane (_translationOnlyVerify), because server-side
  //    auto-translate commits inside this very window and the rev channel has no
  //    replay. What this case protects is unchanged and still asserted: our own
  //    PUT is authoritative, so NO content/thinking/toolRounds may be adopted.
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'local copy' },
    ], _localWriteAt: Date.now() }];
    activeConvId = 'c1';
    serverResponse = { rev: 9, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'STALE SERVER CONTENT' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('self_echo_no_destructive_adopt',
      conversations[0].messages[1].content === 'local copy');
    check('self_echo_not_rendered', renderChatCalls.length === 0);
    check('self_echo_rev_not_advanced', conversations[0]._serverRev === 5);
  }

  // ══ 4b. SELF-ECHO caught in timer: our PUT advances _serverRev during debounce ══
  {
    reset();
    const conv = { id: 'c1', _serverRev: 5, messages: [{}] };
    conversations = [conv];
    activeConvId = 'c1';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush();
    conv._serverRev = 6;   // our finishStream PUT lands during the debounce
    fireTimers(); await settle();
    check('self_echo_revcaught_in_timer', getCalls.length === 0);
  }

  // ══ 5. Metadata-only (no rev) → debounced sidebar refresh, no GET ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 6, messages: [{}] }];
    activeConvId = 'c1';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', userId: 1 });
    await flush();
    check('meta_no_get', getCalls.length === 0);
    fireTimers(); await settle();
    check('meta_list_refresh', listRefreshCalls === 1);
  }

  // ══ 6. Unknown conv → debounced list discovery ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 6, messages: [{}] }];
    activeConvId = 'c1';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'cNEW', rev: 1, userId: 1 });
    fireTimers(); await settle();
    check('unknown_list_refresh', listRefreshCalls === 1);
    check('unknown_no_get', getCalls.length === 0);
  }

  // ══ 7. Background conv → mark stale + list, never verify/repaint ══
  {
    reset();
    conversations = [
      { id: 'c1', _serverRev: 6, messages: [{}] },
      { id: 'c2', _serverRev: 2, messages: [{}] },
    ];
    activeConvId = 'c1';
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c2', rev: 3, userId: 1 });
    await flush();
    check('bg_marked_stale', conversations[1]._needsLoad === true);
    fireTimers(); await settle();
    check('bg_no_get', getCalls.length === 0);
    check('bg_list_refresh', listRefreshCalls === 1);
  }

  // ══ 8. Editing / live-stream on the active conv → DESTRUCTIVE adopt suppressed ══
  //    2026-07-27: these frames divert to the translation-only lane rather than
  //    being dropped (auto-translate must not need a manual refresh). The
  //    property these cases exist to protect — never overwrite the content this
  //    device holds more recently — is asserted directly below.
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'being edited locally' },
    ] }];
    activeConvId = 'c1'; _editingMsgIdx = 2;
    serverResponse = { rev: 9, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'STALE SERVER CONTENT' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('editing_no_destructive_adopt',
      conversations[0].messages[1].content === 'being edited locally');
    check('editing_not_rendered', renderChatCalls.length === 0);
  }
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'streaming so far' },
    ] }];
    activeConvId = 'c1'; activeStreams.set('c1', { controller: {} });
    serverResponse = { rev: 9, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'STALE SERVER CONTENT' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('live_stream_no_destructive_adopt',
      conversations[0].messages[1].content === 'streaming so far');
    check('live_stream_not_rendered', renderChatCalls.length === 0);
  }

  // ══ 9. conv_deleted → removed ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [{}] },
                     { id: 'c2', _serverRev: 1, messages: [{}] }];
    activeConvId = 'c2';
    _onConvNotifyPush({ type: 'conv_deleted', convId: 'c1', userId: 1 });
    await flush();
    check('deleted_removed', !conversations.some((c) => c.id === 'c1'));
  }

  // ══ 10. Multi-user gate ══
  {
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [{}] }];
    activeConvId = 'c1'; window._currentUserId = 1;
    serverResponse = { rev: 9, messages: [{}, { role: 'assistant', content: 'x' }] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 2 }); // not us
    await flush(); fireTimers(); await settle();
    check('other_user_dropped', getCalls.length === 0 && listRefreshCalls === 0);
  }

  // ══ 11. DOUBLE-NEUTER A: strip both _localWriteAt guards → wrongly adopts ══
  //    The guards now DIVERT to the translation-only lane instead of returning,
  //    so the neuter targets the diverting block and replaces it with a
  //    fall-through into the destructive verify.
  {
    const G1 = 'if (conv._localWriteAt && (Date.now() - conv._localWriteAt) < _CONV_SELF_ECHO_MS) {\n      _scheduleTranslationOnlyVerify(convId);\n      return;\n    }';
    const G2 = 'if (c._localWriteAt && (Date.now() - c._localWriteAt) < _CONV_SELF_ECHO_MS) {\n          _scheduleTranslationOnlyVerify(convId);\n          return;\n        }';
    const neutered = SRC.split(G1).join('/* NEUTERED self-echo (conv) */')
                        .split(G2).join('/* NEUTERED self-echo (c) */');
    check('neuterA_patch_applied', neutered !== SRC && !neutered.includes(G1) && !neutered.includes(G2));
    loadModule(neutered);
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [{ role: 'user' }, { role: 'assistant', content: '' }], _localWriteAt: Date.now() }];
    activeConvId = 'c1';
    serverResponse = { rev: 9, messages: [{ role: 'user' }, { role: 'assistant', content: 'x' }] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await flush(); fireTimers(); await settle();
    check('neuterA_selfecho_wrongly_adopts',
      conversations[0].messages[1].content === 'x');
    loadModule(SRC);
  }

  // ══ 12. DOUBLE-NEUTER B: strip the equal-count content-grew adopt branch →
  //          a same-count/longer-content server copy is NOT adopted ══
  {
    const BRANCH = 'if ((sc > lc || st > lt || sr > lr) && sc >= lc) {';
    const neutered = SRC.split(BRANCH).join('if (false) {');
    check('neuterB_patch_applied', neutered !== SRC && !neutered.includes(BRANCH));
    loadModule(neutered);
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: '' },
    ] }];
    activeConvId = 'c1';
    serverResponse = { rev: 6, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'the full answer' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    // Branch neutered → equal-count content growth is NOT adopted → stays empty.
    check('neuterB_equalcount_not_adopted', conversations[0].messages[1].content === '');
    check('neuterB_not_rendered', renderChatCalls.length === 0);
    loadModule(SRC);
  }

  // ══ 13. NEUTER C: strip the cross-device live-turn reconnect → an adopted
  //          still-generating turn leaves this tab with NO live stream (the
  //          reported bug: composer reverts to Send, no phase text). ══
  {
    const RECON = 'const _reconnected = (typeof _reconnectServerTaskIfIdle === "function")\n        && _reconnectServerTaskIfIdle(convId);';
    const neutered = SRC.split(RECON).join('const _reconnected = false;');
    check('neuterC_patch_applied', neutered !== SRC && !neutered.includes(RECON));
    loadModule(neutered);
    reset();
    conversations = [{ id: 'c1', _serverRev: 5, messages: [{ role: 'user', content: 'q' }] }];
    activeConvId = 'c1';
    reconnectReturns = true;   // task IS live server-side, but the call is neutered out
    serverResponse = { rev: 6, settings: { activeTaskId: 'task-live' }, messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'partial' },
    ] };
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
    await flush(); fireTimers(); await settle();
    // Reconnect never attempted → the live turn is NOT re-attached in this tab.
    check('neuterC_no_reconnect', reconnectCalls.length === 0);
    loadModule(SRC);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_conv_notify_push_handler():
    harness = os.path.join(HERE, '_conv_notify_push_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CROSS_TAB_SYNC, CONV_REDUCERS,
             CONV_STATE_REDUCER],
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
    assert not fails, 'conv-notify-push handler failures:\n' + output
    assert output.count('PASS') >= 39, f'expected >=39 PASS lines, got:\n{output}'
