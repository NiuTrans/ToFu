"""Regression: clicking Continue must react IMMEDIATELY, must not mint a twin
bubble, must not land mid-history, must gate re-entry, and must fail VISIBLY.

THE REPORTED BUG (owner, 2026-07-29)
------------------------------------
"点了 continue 前端没有立刻反馈，等一会儿才动；动起来之后还会跳到历史某个位置。"

FOUR ROOT CAUSES, all in ``continueAssistant()``
(static/js/main/main_regen_continue.js):

1. DEAD ZONE. The click ran ``_buildConvConfig`` → a FULL-conversation
   ``syncConversationToServer`` → ``Api.chat.continue`` — three serial awaits —
   before ANY pixel changed. The sibling ``regenerateFromUser`` already fixed
   exactly this ("★ Fix ①: … deterministic '连接中…' placeholder") but Continue
   was left out.

   ★ AND THE OBVIOUS PORT IS WRONG. ``_renderTranslatingBubble`` APPENDS a new
   ``#translating-msg`` at the TAIL. That is only safe for regenerate, which
   TRUNCATES the assistant message first. Continue does NOT truncate — the
   interrupted assistant bubble is still the tail — so appending would show TWO
   assistant bubbles (the interrupted one + a fake twin) for the whole POST
   window. That is the recurring twin-bubble defect the ms43foj3 comment in
   ``_applyContinueCheckpoint`` documents.

   THE FIX: convert the tail bubble to the streaming shell IN PLACE, BEFORE the
   first await. One data entry ⇒ one DOM node, and the pulse appears on the
   click frame.

2. SCROLL JUMP. ``ConvView.replaceAll(convId, {forceScroll:false})`` takes
   renderChat's SURGICAL path, whose anchor capture/restore is gated on
   ``conv._bgRepaint`` (set only by ``_bgRefreshChat``) — so this repaint had NO
   scroll preservation at all. The checkpoint rollback then shrinks the tail
   bubble hard (discarded rounds dropped, thinking cleared, tail prose demoted
   to collapsed priorContent/priorThinking), scrollTop is clamped, and the
   viewport lands back in history. The rescue was a BARE ``scrollToBottom()``,
   which ``core.js`` early-returns from when ``!force && !isNearBottom(200)``.
   ``.message`` also carries ``content-visibility:auto``, so even when it did
   run, a single-rAF ``scrollTop = scrollHeight`` reads an UNDER-estimated
   height and "lands mid-history" (the verbatim warning in
   main_translating_bubble.js). Every other bubble-insert site already uses
   ``_forceScrollToBottom(null, true)`` (cv-off → real heights → re-assert
   across double-rAF + 150ms). Continue must too.

3. SILENT FAILURE. The POST catch was ``debugLog(...)`` + ``return``. debugLog
   only reaches the debug panel, so a failed Continue was a total dead end —
   the worst possible form of "no response". Worse, once the shell is raised
   EARLY (fix 1) a silent failure strands the user staring at a frozen
   ``Continuing…`` pulse forever, so the failure path MUST roll the bubble back
   to its continuable static form AND surface a user-visible error.
   Also ``data = await res.json()`` ran BEFORE the ``res.ok`` check, so a proxy
   502 (HTML body) threw inside ``json()`` straight into the same silent catch.

4. NO IN-FLIGHT GATE. The entry guard reads ``conv.activeTaskId``, but that is
   only assigned AFTER all three awaits. So the whole dead zone accepted a
   second click, which replayed the full rollback + task start.

WHAT THIS FILE PINS
-------------------
Behavioural jsdom guards driving the REAL shipped ``continueAssistant``, one per
root cause, each with a NEUTER that re-introduces the original defect and must
turn the corresponding check red.
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import ROOT, run_harness
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

SRC_JS = runtime_section_path('main/main_regen_continue.js')
STREAMING_JS = runtime_section_path('ui/streaming_render.js')
CONV_VIEW_JS = runtime_section_path('conv_view.js')


# ── Shared harness prologue: DOM + stubs + a scene reset ────────────────────
_PROLOGUE = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let conv = null;
let calls = null;
let api = null;          // per-scenario Api.chat.continue behaviour

const HTML = '<!DOCTYPE html><body><div id="chatContainer">'
           + '<div id="chatInner"></div></div></body>';

function staticAssistantHtml(idx, msgId) {
  return '<div class="message ep-worker-msg" id="msg-' + idx + '"'
    + ' data-msg-id="' + msgId + '" data-mfp="fp-static">'
    + '<div class="message-avatar"></div><div class="message-content">'
    + '<div class="message-header"><span class="message-role">Agent</span></div>'
    + '<div class="message-body"><div class="md-content">interrupted tail prose</div>'
    + '<div class="tool-panel">3 tool rounds worth of height</div></div>'
    + '<div class="message-actions"><button class="msg-continue-btn">Continue</button></div>'
    + '</div></div>';
}
function staticUserHtml(idx) {
  return '<div class="message user-msg" id="msg-' + idx + '" data-msg-id="u1">'
    + '<div class="message-content"><div class="message-body">ask</div></div></div>';
}

/* Snapshot of what the user can SEE, taken at an arbitrary moment. */
function domSnap() {
  const inner = document.getElementById('chatInner');
  return {
    messages: inner.querySelectorAll('.message').length,
    streaming: !!document.getElementById('streaming-msg'),
    translating: !!document.getElementById('translating-msg'),
    pulses: inner.querySelectorAll('.stream-status .pulse').length,
  };
}

const { check, report } = setup({
  root: process.argv[3],
  html: HTML,
  targets: [process.argv[2]],
  globals: {
    conversations: [],
    activeConvId: 'c1',
    activeStreams: new Map(),
    getActiveConv: () => conv,
    getConvById: () => conv,
    getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
    _ensureMsgId: (m) => { if (m && !m._msgId) m._msgId = 'tmp_x'; return m; },
    _buildConvConfig: async () => {
      calls.atBuildConfig = domSnap();
      return {};
    },
    syncConversationToServer: async () => {
      calls.atSync = domSnap();
      calls.sync++;
      return true;
    },
    saveConversations: () => {},
    buildTurnNav: () => {},
    renderConversationList: () => {},
    connectToTask: (cid, tid) => { calls.connect.push(tid); },
    startAssistantResponse: async () => { calls.startAssistant++; },
    updateStreamingUI: (m) => {
      calls.updateStreamingUI.push({
        content: (m && m.content) || '',
        rounds: ((m && m.toolRounds) || []).length,
      });
    },
    scrollToBottom: (force) => { calls.scrollToBottom.push(force); },
    _forceScrollToBottom: (c, real) => { calls.forceScroll.push(!!real); },
    showToast: (msg, kind) => { calls.toasts.push({ msg: String(msg), kind: kind }); },
    debugLog: (msg, kind) => { calls.debugLogs.push({ msg: String(msg), kind: kind }); },
    updateContextBar: () => {},
    ConvCache: { put: () => {} },
    Api: {
      chat: {
        continue: async (body) => {
          calls.continuePosts++;
          calls.atPost = domSnap();
          return api();
        },
        abortConv: async () => {},
      },
    },
  },
});

/* ConvView stub. `finalizeStreaming` faithfully mirrors the REAL seam's
 * observable effect (conv_view.js): the #streaming-msg node becomes the static
 * msg-N node again. That makes the rollback assertion a DOM fact, not a
 * stub-call count. */
window.ConvView = global.ConvView = {
  /* Mirrors the REAL conv_view.js seam VERBATIM (identity-first, positional
   * fallback only for id-less rows). Faithful because the drift guard's whole
   * point is which node gets resolved — a stub that just returned
   * getElementById would test nothing. */
  findMessageEl: (msg, idx) => {
    const inner = document.getElementById('chatInner');
    if (!inner) return null;
    if (msg && msg._msgId) {
      const byId = inner.querySelector('[data-msg-id="' + msg._msgId + '"]');
      if (byId) return byId;
    }
    if (typeof idx === 'number') return document.getElementById('msg-' + idx);
    return null;
  },
  replaceAll: (cid, opts) => {
    calls.replaceAll.push(opts || {});
    return true;
  },
  startStreaming: () => { calls.startStreaming++; return true; },
  finalizeStreaming: (cid, msg) => {
    calls.finalize++;
    const sm = document.getElementById('streaming-msg');
    if (sm) {
      sm.id = 'msg-' + (conv.messages.length - 1);
      const b = sm.querySelector('.message-body');
      if (b) {
        b.removeAttribute('id');
        b.innerHTML = '<div class="md-content">interrupted tail prose</div>';
      }
    }
    return true;
  },
};

function resetScene() {
  const inner = document.getElementById('chatInner');
  inner.innerHTML = staticUserHtml(0) + staticAssistantHtml(1, 'a1');
  conv = {
    id: 'c1',
    activeTaskId: null,
    messages: [
      { role: 'user', content: 'ask', _msgId: 'u1' },
      {
        role: 'assistant', _msgId: 'a1',
        content: 'interrupted tail prose',
        thinking: 'live thinking tail',
        finishReason: 'interrupted',
        interruptedReason: 'killed',
        toolRounds: [
          { toolCallId: 'a', status: 'done', roundNum: 1, llmRound: 0 },
          { toolCallId: 'b', status: 'done', roundNum: 2, llmRound: 1 },
          { toolCallId: 'c', status: 'running', roundNum: 3, llmRound: 2 },
        ],
      },
    ],
  };
  global.conversations = window.conversations = [conv];
  global.activeStreams = window.activeStreams = new Map();
  calls = {
    sync: 0, continuePosts: 0, startAssistant: 0, finalize: 0, startStreaming: 0,
    connect: [], replaceAll: [], updateStreamingUI: [], scrollToBottom: [],
    forceScroll: [], toasts: [], debugLogs: [],
    atBuildConfig: null, atSync: null, atPost: null,
  };
}

/* A successful checkpoint reply: server keeps 1 of 3 rounds. */
function okCheckpoint() {
  return {
    ok: true,
    json: async () => ({
      taskId: 'T-new',
      checkpoint: {
        resumeMode: 'checkpoint', keptRounds: 1, discardedRounds: 2,
        contentPrefix: 'kept', priorContent: ' rolled-back tail',
        priorThinking: 'live thinking tail',
        preservedContentLen: 4, discardedContentLen: 17,
        preservedThinkingChars: 0, discardedThinking: 18,
      },
    }),
  };
}
"""

# The guard bodies are async IIFEs. ``report()`` MUST run only after they
# settle — calling it synchronously would print an EMPTY result set (0 PASS,
# 0 FAIL), which run_harness reports as "expected >= N PASS lines, got 0" and
# is indistinguishable from a genuine behavioural red. A throw is surfaced as
# an explicit FAIL line for the same reason.
_EPILOGUE = (
    "\n})().then(report).catch((e) => {\n"
    "  console.log('FAIL harness_threw ' + ((e && e.stack) || e));\n"
    "  report();\n"
    "});\n"
)


# ══════════════════════════════════════════════════════════════════════════
#  Guard 1 — immediate feedback, and NO twin bubble
# ══════════════════════════════════════════════════════════════════════════
_BODY_FEEDBACK = _PROLOGUE + r"""
(async () => {
  resetScene();
  const before = domSnap();
  check('pre_no_streaming', before.streaming === false);
  check('pre_two_messages', before.messages === 2);

  api = okCheckpoint;
  await continueAssistant();

  /* ── The click frame must already show a live shell. Every snapshot below is
   * taken INSIDE a stubbed await, i.e. strictly BEFORE the network reply. ── */
  const b = calls.atBuildConfig, s = calls.atSync, p = calls.atPost;
  check('snapshots_taken', !!(b && s && p));

  check('shell_up_before_buildConfig', !!b && b.streaming === true);
  check('shell_up_before_sync', !!s && s.streaming === true);
  check('shell_up_before_post', !!p && p.streaming === true);
  check('pulse_visible_before_post', !!p && p.pulses >= 1);

  /* ★ THE TWIN-BUBBLE PIN: the shell must be the SAME node, converted in
   * place — the visible bubble count may never grow, and the regenerate-style
   * appended placeholder must never appear. */
  check('no_twin_before_buildConfig', !!b && b.messages === 2);
  check('no_twin_before_sync', !!s && s.messages === 2);
  check('no_twin_before_post', !!p && p.messages === 2);
  check('no_translating_bubble_before_post', !!p && p.translating === false);

  const after = domSnap();
  check('post_no_twin', after.messages === 2);
  check('post_still_streaming', after.streaming === true);

  /* The checkpoint fact was applied and painted into the live zones. */
  check('one_post', calls.continuePosts === 1);
  check('rolled_back_to_server_count', conv.messages[1].toolRounds.length === 1);
  check('painted_after_fact',
        calls.updateStreamingUI.some((u) => u.rounds === 1 && u.content === 'kept'));
  check('task_bound', conv.activeTaskId === 'T-new');
  check('connected', calls.connect.length === 1 && calls.connect[0] === 'T-new');
""" + _EPILOGUE


def test_continue_click_paints_shell_in_place_before_any_roundtrip():
    run_harness(target_js=SRC_JS, body_js=_BODY_FEEDBACK, min_pass=18,
                label='continue-immediate-feedback')


# ══════════════════════════════════════════════════════════════════════════
#  Guard 2 — real-height bottom pin at click time
# ══════════════════════════════════════════════════════════════════════════
_BODY_SCROLL = _PROLOGUE + r"""
(async () => {
  resetScene();
  api = okCheckpoint;

  /* Record scroll calls that happened BEFORE the POST by snapshotting the
   * counter inside the stubbed POST. */
  const origContinue = Api.chat.continue;
  Api.chat.continue = async (body) => {
    calls.forceScrollAtPost = calls.forceScroll.slice();
    calls.scrollToBottomAtPost = calls.scrollToBottom.slice();
    return origContinue(body);
  };

  await continueAssistant();

  check('force_scroll_used_at_click',
        Array.isArray(calls.forceScrollAtPost) && calls.forceScrollAtPost.length >= 1);
  check('force_scroll_asks_real_heights',
        Array.isArray(calls.forceScrollAtPost) && calls.forceScrollAtPost.includes(true));

  /* The bare, near-bottom-gated scrollToBottom() must NOT be the mechanism
   * relied on: core.js early-returns from it when the reader is >200px from the
   * bottom, which is exactly how the viewport got stranded in history. */
  const bareUngated = calls.scrollToBottom.filter((f) => !f);
  check('no_bare_ungated_scrollToBottom', bareUngated.length === 0);
""" + _EPILOGUE


def test_continue_pins_bottom_via_real_height_primitive():
    run_harness(target_js=SRC_JS, body_js=_BODY_SCROLL, min_pass=3,
                label='continue-real-height-scroll')


def test_force_scroll_primitive_actually_exists():
    """Guard-the-guard: Guard 2 stubs ``_forceScrollToBottom``, so a typo'd
    symbol would still 'pass'. Pin that the real shipped primitive exists under
    that exact name (and still takes the real-heights flag)."""
    with open(STREAMING_JS, encoding='utf-8') as f:
        src = f.read()
    assert 'function _forceScrollToBottom(container, forceActualHeights)' in src, (
        '_forceScrollToBottom(container, forceActualHeights) is gone from '
        'ui/streaming_render.js — Guard 2 would be stubbing a symbol that no '
        'longer exists in production.')
    assert "inner.classList.add('cv-off')" in src, (
        '_forceScrollToBottom no longer flips cv-off — it would read '
        'content-visibility:auto ESTIMATES again and land mid-history.')


# ══════════════════════════════════════════════════════════════════════════
#  Guard 3 — in-flight gate closes the dead-zone re-entry window
# ══════════════════════════════════════════════════════════════════════════
_BODY_REENTRY = _PROLOGUE + r"""
(async () => {
  resetScene();
  api = okCheckpoint;

  /* Fire twice WITHOUT awaiting the first — this is a real double-click inside
   * the dead zone, where conv.activeTaskId is still null. */
  const p1 = continueAssistant();
  const p2 = continueAssistant();
  await Promise.all([p1, p2]);

  check('single_post_despite_double_click', calls.continuePosts === 1);
  check('single_sync_despite_double_click', calls.sync === 1);
  check('single_connect', calls.connect.length === 1);
  check('flag_cleared_after', !conv._continueInFlight);

  /* And after the turn is bound, a later click is still refused (activeTaskId
   * guard) — the pre-existing behaviour must survive. */
  const postsBefore = calls.continuePosts;
  await continueAssistant();
  check('refused_once_task_bound', calls.continuePosts === postsBefore);
""" + _EPILOGUE


def test_continue_refuses_reentry_during_dead_zone():
    run_harness(target_js=SRC_JS, body_js=_BODY_REENTRY, min_pass=5,
                label='continue-inflight-gate')


# ══════════════════════════════════════════════════════════════════════════
#  Guard 4 — failure is VISIBLE and the shell is rolled back
# ══════════════════════════════════════════════════════════════════════════
_BODY_FAILURE = _PROLOGUE + r"""
(async () => {
  /* ── 4a: network throw ── */
  resetScene();
  api = () => { throw new Error('network down'); };
  await continueAssistant();

  check('a_no_task_bound', !conv.activeTaskId);
  check('a_flag_cleared', !conv._continueInFlight);
  check('a_visible_error', calls.toasts.length >= 1);
  check('a_shell_rolled_back', document.getElementById('streaming-msg') === null);
  check('a_no_twin_left', domSnap().messages === 2);
  check('a_message_doc_intact',
        conv.messages[1].toolRounds.length === 3
        && conv.messages[1].finishReason === 'interrupted');

  /* Retryable: a second click after the failure must be able to POST again. */
  api = okCheckpoint;
  await continueAssistant();
  check('a_retryable_after_failure', calls.continuePosts === 2);

  /* ── 4b: proxy 502 whose body is HTML (res.json() throws) ── */
  resetScene();
  api = () => ({
    ok: false, status: 502,
    json: async () => { throw new SyntaxError('Unexpected token < in JSON'); },
  });
  await continueAssistant();

  check('b_no_task_bound', !conv.activeTaskId);
  check('b_visible_error', calls.toasts.length >= 1);
  check('b_shell_rolled_back', document.getElementById('streaming-msg') === null);
  check('b_flag_cleared', !conv._continueInFlight);

  /* ── 4c: well-formed error envelope from the server ── */
  resetScene();
  api = () => ({ ok: false, status: 500, json: async () => ({ error: 'boom' }) });
  await continueAssistant();
  check('c_visible_error', calls.toasts.length >= 1);
  check('c_shell_rolled_back', document.getElementById('streaming-msg') === null);
""" + _EPILOGUE


def test_continue_failure_is_visible_and_rolls_back():
    run_harness(target_js=SRC_JS, body_js=_BODY_FAILURE, min_pass=13,
                label='continue-failure-visible')


# ══════════════════════════════════════════════════════════════════════════
#  Source-level pin: the banned twin-bubble port
# ══════════════════════════════════════════════════════════════════════════
def test_continue_never_appends_a_translating_placeholder():
    """``_renderTranslatingBubble`` APPENDS ``#translating-msg`` at the tail.
    regenerate may use it (it truncates the assistant message first); Continue
    may NOT (it keeps the interrupted bubble as the tail), or the user sees two
    assistant bubbles for the whole POST window. Pin the ban inside
    ``continueAssistant`` so the "obvious" port can't be re-applied later."""
    with open(SRC_JS, encoding='utf-8') as f:
        src = f.read()
    fn_start = src.index('async function continueAssistant()')
    fn_body = src[fn_start:]
    # Strip comments so the rationale can name the banned helper.
    code = re.sub(r'/\*.*?\*/', '', fn_body, flags=re.S)
    code = re.sub(r'^\s*//.*$', '', code, flags=re.M)
    assert '_renderTranslatingBubble' not in code, (
        'continueAssistant calls _renderTranslatingBubble — that APPENDS a tail '
        'placeholder, so the interrupted assistant bubble plus the placeholder '
        'render as TWIN assistant bubbles for the whole POST window. Convert '
        'the existing tail bubble to the streaming shell IN PLACE instead.')


def test_continue_checks_response_ok_before_parsing_json():
    """``data = await res.json()`` before the ``res.ok`` check meant a proxy 502
    (HTML body) threw inside ``json()`` and vanished into the silent catch. The
    ok-check must come FIRST."""
    with open(SRC_JS, encoding='utf-8') as f:
        src = f.read()
    fn_start = src.index('async function continueAssistant()')
    fn_body = src[fn_start:]
    m_post = re.search(r'Api\.chat\.continue\(', fn_body)
    assert m_post, 'Api.chat.continue call not found in continueAssistant'
    region = fn_body[m_post.start():m_post.start() + 900]
    i_ok = region.find('res.ok')
    i_json = region.find('res.json()')
    assert i_ok != -1, 'no res.ok check after the Continue POST'
    assert i_json != -1, 'no res.json() after the Continue POST'
    assert i_ok < i_json, (
        'res.json() is parsed BEFORE res.ok is checked — a non-JSON error body '
        '(proxy 502 HTML) throws inside json() and is swallowed by the catch.'
    )


def test_continue_failure_path_is_not_debuglog_only():
    """debugLog only reaches the debug panel. The POST failure path must raise a
    USER-visible signal (toast) — a silent catch is the worst form of the
    reported "no response"."""
    with open(SRC_JS, encoding='utf-8') as f:
        src = f.read()
    fn_start = src.index('async function continueAssistant()')
    fn_body = src[fn_start:]
    # Anchor on the failure log line itself, NOT on an indentation-sensitive
    # brace pattern: the catch is nested inside the in-flight try/finally, and a
    # future re-indent must not silently make this guard vacuous.
    i = fn_body.find('debugLog("Continue failed: "')
    assert i != -1, (
        'the Continue POST failure log line is gone — update this guard to the '
        'new failure-path anchor')
    catch_body = fn_body[i:i + 700]
    assert 'showToast' in catch_body, (
        'the Continue POST catch block still surfaces nothing but debugLog — '
        'the user gets no feedback at all on a failed Continue:\n' + catch_body)
    assert '_rollbackContinueShell' in catch_body, (
        'the Continue POST catch block does not roll the streaming shell back — '
        'the user is left staring at a frozen "Continuing…" pulse forever:\n'
        + catch_body)


def test_no_await_precedes_the_inflight_gate():
    """STRUCTURAL pin for the gate's precondition.

    The in-flight gate only closes the double-click window if it is reached
    SYNCHRONOUSLY from the click. During this very fix an ``await
    _buildConvConfig(conv)`` was left sitting above ``conv._continueInFlight =
    true`` — both clicks then sailed past the guard (which still saw a null
    ``activeTaskId``) and the behavioural guard caught it. Pin the shape too, so
    a future edit that reintroduces an await above the gate fails loudly at the
    source level instead of only under jsdom.
    """
    with open(SRC_JS, encoding='utf-8') as f:
        src = f.read()
    fn_start = src.index('async function continueAssistant()')
    fn_body = src[fn_start:]
    i_gate = fn_body.index('conv._continueInFlight = true;')
    head = fn_body[:i_gate]
    # Strip comments so prose mentioning "await" can't trip the scan.
    head = re.sub(r'/\*.*?\*/', '', head, flags=re.S)
    head = re.sub(r'^\s*//.*$', '', head, flags=re.M)
    # The empty-turn shortcut legitimately awaits, but it RETURNS before ever
    # reaching the gate — excise that whole block before scanning.
    head = re.sub(r'if \(!assistantMsg\.content.*?\n  \}\n', '', head, flags=re.S)
    assert 'await ' not in head, (
        'an await now precedes the in-flight gate — the dead-zone re-entry '
        'window it exists to close is reopened:\n' + head[-600:])


# ═══════════════════════════════════════════════════════════════════════
#  Guard 5 — INDEX DRIFT: the shell must land on the message's OWN node
# ═══════════════════════════════════════════════════════════════════════
#
# THE REGRESSION THIS PINS (owner-reproduced, introduced by the shell fix)
# ------------------------------------------------------------------------
# `_raiseContinueShell` looked the node up with
# `document.getElementById('msg-' + lastIdx)` — a PURE POSITIONAL id.
#
# Before the shell fix that same positional lookup was SAFE, because it ran
# AFTER `ConvView.replaceAll(...)`: that repaint had just re-stamped every
# `msg-N`, so the positional id was guaranteed fresh. The shell fix deleted the
# replaceAll AND moved the lookup ahead of ANY render — the precondition
# vanished while the lookup did not change.
#
# So when `conv.messages` has changed length but no repaint has happened yet (a
# poll/merge/peer reconcile dropping a mid-history row is the common case), the
# positional slot `msg-{length-1}` belongs to a DIFFERENT, older message. The
# result is strictly worse than the bug being fixed: a HISTORICAL answer is
# rewritten in place into a `Continuing…` pulse (its prose destroyed by the
# zone innerHTML), the genuinely-interrupted turn is never converted, and
# `_shellUp` still returns true — so the success path skips its
# "repaint-then-raise" fallback and streams into the WRONG node's zones.
# Functionality broken while the UI presents it as working.
#
# This is not a new rule: `chat_render.js::_reconcileFindEl` already states that
# a message WITH a `_msgId` must be matched by it ONLY — "after an index shift
# that slot belongs to a DIFFERENT message — grabbing it would reuse the wrong
# node" — and `conv_view.js::_findMsgEl` documents the same
# `data-msg-id → msg-${idx}` priority.
#
# WHY THE OTHER GUARDS ARE BLIND TO IT: their fixture paints
# `staticAssistantHtml(1, 'a1')` against a 2-message conv, so the positional id
# is ALWAYS `length - 1`. Both lookup strategies behave identically there. Only
# a drifted scene discriminates them.
_BODY_DRIFT = _PROLOGUE + r"""
(async () => {
  /* Scene: the DOM was painted when the conv had THREE messages
   *   msg-0 = user
   *   msg-1 = an older assistant answer      (data-msg-id="a-old")
   *   msg-2 = the INTERRUPTED tail           (data-msg-id="a-tail")
   * then a reconcile dropped the middle row, so conv.messages is now
   *   [user, a-tail]  — length 2, lastIdx 1 — and NO repaint has run.
   * The positional slot `msg-1` therefore points at `a-old`. */
  const inner = document.getElementById('chatInner');
  inner.innerHTML =
      staticUserHtml(0)
    + staticAssistantHtml(1, 'a-old')
    + staticAssistantHtml(2, 'a-tail');
  /* Make the historical bubble's prose identifiable so we can prove it was not
   * overwritten. */
  const oldEl = inner.querySelector('[data-msg-id="a-old"]');
  oldEl.querySelector('.md-content').textContent = 'HISTORICAL ANSWER';

  conv = {
    id: 'c1',
    activeTaskId: null,
    messages: [
      { role: 'user', content: 'ask', _msgId: 'u1' },
      {
        role: 'assistant', _msgId: 'a-tail',
        content: 'interrupted tail prose',
        thinking: 'live thinking tail',
        finishReason: 'interrupted',
        toolRounds: [
          { toolCallId: 'a', status: 'done', roundNum: 1, llmRound: 0 },
          { toolCallId: 'b', status: 'done', roundNum: 2, llmRound: 1 },
          { toolCallId: 'c', status: 'running', roundNum: 3, llmRound: 2 },
        ],
      },
    ],
  };
  global.conversations = window.conversations = [conv];
  global.activeStreams = window.activeStreams = new Map();
  calls = {
    sync: 0, continuePosts: 0, startAssistant: 0, finalize: 0, startStreaming: 0,
    connect: [], replaceAll: [], updateStreamingUI: [], scrollToBottom: [],
    forceScroll: [], toasts: [], debugLogs: [],
    atBuildConfig: null, atSync: null, atPost: null,
  };

  api = okCheckpoint;
  await continueAssistant();

  const shell = document.getElementById('streaming-msg');
  check('drift_shell_exists', !!shell);

  /* ★ THE PIN: the shell must be the node bound to the message Continue is
   * actually resuming — identity, never array position. */
  check('drift_shell_is_the_target_msg',
        !!shell && shell.getAttribute('data-msg-id') === 'a-tail');

  /* ★ COMPLEMENT: the historical bubble must be untouched. Without this a fix
   * that raises the shell on the RIGHT node but also clobbers the wrong one
   * would still pass. */
  const stillOld = inner.querySelector('[data-msg-id="a-old"]');
  check('drift_historical_bubble_survives', !!stillOld);
  check('drift_historical_prose_intact',
        !!stillOld && /HISTORICAL ANSWER/.test(stillOld.textContent));
  check('drift_historical_not_the_shell',
        !!stillOld && stillOld.id !== 'streaming-msg');
  check('drift_no_pulse_in_historical',
        !!stillOld && stillOld.querySelectorAll('.stream-status .pulse').length === 0);

  /* The turn still resumes normally end-to-end. */
  check('drift_one_post', calls.continuePosts === 1);
  check('drift_task_bound', conv.activeTaskId === 'T-new');
""" + _EPILOGUE


def test_continue_shell_lands_on_the_target_message_under_index_drift():
    run_harness(target_js=SRC_JS, body_js=_BODY_DRIFT, min_pass=8,
                label='continue-index-drift')


def test_raise_shell_resolves_by_stable_id_not_position():
    """Source-level complement to Guard 5.

    The behavioural guard proves the OUTCOME; this pins the MECHANISM, so a
    future edit cannot quietly reintroduce a positional-first lookup and rely on
    some incidental repaint to keep the scene passing. Mirrors the rule already
    stated in ``chat_render.js::_reconcileFindEl`` and
    ``conv_view.js::_findMsgEl``: match on ``_msgId`` first; the positional
    handle is a LEGACY fallback for id-less messages only.
    """
    with open(SRC_JS, encoding='utf-8') as f:
        src = f.read()
    start = src.index('function _raiseContinueShell(')
    end = src.index('runtimeScope._raiseContinueShell = _raiseContinueShell;')
    body = src[start:end]
    code = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    code = re.sub(r'^\s*//.*$', '', code, flags=re.M)

    assert ('findMessageEl' in code or '_msgId' in code
            or 'data-msg-id' in code), (
        '_raiseContinueShell resolves the bubble by array position alone — it '
        'consults neither the stable _msgId nor the shared identity-first seam '
        'ConvView.findMessageEl, so after an index shift it converts a '
        'DIFFERENT (historical) message into the streaming shell.')

    # If a positional lookup is present at all, the stable-id lookup must come
    # FIRST (fallback-only semantics).
    i_pos = code.find("getElementById('msg-")
    if i_pos == -1:
        i_pos = code.find('getElementById(`msg-')
    if i_pos != -1:
        i_id = min(
            [i for i in (code.find('data-msg-id'), code.find('_findMsgEl'),
                         code.find('findMessageEl')) if i != -1] or [-1])
        assert i_id != -1 and i_id < i_pos, (
            '_raiseContinueShell reaches for the positional `msg-N` handle '
            'BEFORE (or instead of) the stable `_msgId` lookup — that is the '
            'exact inversion chat_render.js::_reconcileFindEl forbids.')


def test_convview_find_seam_is_identity_first():
    """Guard-the-guard for Guard 5.

    Guard 5 STUBS ``ConvView.findMessageEl``, and the shell now DELEGATES to it
    — so if the shipped seam were positional-first, production would still be
    broken while the harness stayed green. Pin the real one: it must be exposed,
    and inside ``_findMsgEl`` the ``data-msg-id`` match must precede the
    positional ``msg-`` fallback.
    """
    conv_view = CONV_VIEW_JS
    with open(conv_view, encoding='utf-8') as f:
        src = f.read()
    assert 'findMessageEl:' in src, (
        'ConvView.findMessageEl is gone — _raiseContinueShell delegates to it, '
        'so removing it silently drops the Continue shell to a null lookup '
        '(shell never rises) or back to a positional one.')
    start = src.index('function _findMsgEl(')
    body = src[start:src.index('function _idxOf(')]
    i_id = body.find('data-msg-id')
    i_pos = body.find("getElementById('msg-")
    assert i_id != -1, '_findMsgEl no longer matches on data-msg-id at all'
    assert i_pos == -1 or i_id < i_pos, (
        '_findMsgEl now reaches the positional msg-N handle before the stable '
        'data-msg-id match — every caller (incl. the Continue shell) would '
        'resolve the WRONG node under index drift.')


# ═══════════════════════════════════════════════════════════════════════
#  Guard 6 — DRIFT × FAILURE: the rolled-back bubble must SURVIVE
# ═══════════════════════════════════════════════════════════════════════
#
# THE SECOND HALF OF THE SAME ROOT CAUSE (owner-reproduced)
# ---------------------------------------------------------
# Guard 5 fixed the ENTRY (`_raiseContinueShell` resolves by `_msgId`). The EXIT
# — rolling the shell back on a failed POST — still resolved by ARRAY POSITION,
# so under the SAME drift the two ends disagreed and the bubble was destroyed:
#
#   `finalizeStreaming` takes `idx = _idxOf(conv, msg)` (drift ⇒ 1), renders
#   `renderMessage(msg, 1)` — which stamps `id="msg-1"` — and swaps it in. But a
#   REAL `msg-1` already exists in the DOM (the historical bubble). Two nodes now
#   carry that id, and the identity sweep's keep-node was
#   `getElementById('msg-' + idx)`, which returns the FIRST match — the
#   HISTORICAL one. So `_evictByMsgId(inner, msg._msgId, keep=<wrong node>)`
#   removed the bubble it had just restored.
#
# Net effect for the user: they click Continue, the network fails, and the turn
# they wanted to resume DISAPPEARS from the screen — while a toast says "you can
# retry". There is nothing left to retry. That is strictly worse than the
# original defect.
#
# THE FIX: the sweep must keep the node it JUST created, held by reference
# (`replaceWith` returns control of the node; `outerHTML` orphans `sm` and is
# precisely why the old code had to re-find it by id). Then the keep-node is
# correct regardless of whether the positional index is fresh.
_BODY_DRIFT_FAILURE = _PROLOGUE.replace(
    "targets: [process.argv[2]],",
    "targets: [process.argv[4], process.argv[2]],",
).replace(
    """window.ConvView = global.ConvView = {""",
    """/* ★ Guard 6 drives the REAL ConvView (conv_view.js is loaded as argv[4],
 * BEFORE the target, so its IIFE installs window.ConvView). The stub below is
 * therefore DELIBERATELY NOT installed — the whole point of this guard is the
 * identity sweep inside the shipped finalizeStreaming. Overwriting it with a
 * stub is what made an earlier revision of this guard pass vacuously. */
const _REAL_CONVVIEW = window.ConvView;
const _UNUSED_STUB = {""",
) + r"""
/* Restore the real seam (the stub object above is never wired in) + the
 * renderMessage the sweep needs. Fail LOUDLY if conv_view.js did not load. */
window.ConvView = global.ConvView = _REAL_CONVVIEW;
if (!window.ConvView || typeof window.ConvView.finalizeStreaming !== 'function') {
  console.log('FAIL real_convview_loaded conv_view.js did not install ConvView');
}
global.renderMessage = window.renderMessage = (m, i) =>
  '<div class="message" id="msg-' + i + '" data-msg-id="' + m._msgId + '"'
  + ' data-mfp="fp-restored"><div class="message-avatar"></div>'
  + '<div class="message-content"><div class="message-header">'
  + '<span class="message-role">Agent</span></div>'
  + '<div class="message-body"><div class="md-content">interrupted tail prose</div>'
  + '</div></div></div>';
global.isNearBottom = window.isNearBottom = () => true;
global._withInstantScroll = window._withInstantScroll = (el, fn) => fn();
global._convRenderFingerprint = window._convRenderFingerprint = () => 'fp';
global._lastRenderedFingerprint = window._lastRenderedFingerprint = '';
""" + r"""
(async () => {
  check('real_convview_in_use',
        !!window.ConvView && typeof window.ConvView.finalizeStreaming === 'function');

  /* Same drifted scene as Guard 5 — but the POST FAILS. */
  const inner = document.getElementById('chatInner');
  inner.innerHTML =
      staticUserHtml(0)
    + staticAssistantHtml(1, 'a-old')
    + staticAssistantHtml(2, 'a-tail');
  inner.querySelector('[data-msg-id="a-old"]').querySelector('.md-content')
      .textContent = 'HISTORICAL ANSWER';

  conv = {
    id: 'c1',
    activeTaskId: null,
    messages: [
      { role: 'user', content: 'ask', _msgId: 'u1' },
      {
        role: 'assistant', _msgId: 'a-tail',
        content: 'interrupted tail prose',
        thinking: 'live thinking tail',
        finishReason: 'interrupted',
        toolRounds: [
          { toolCallId: 'a', status: 'done', roundNum: 1, llmRound: 0 },
          { toolCallId: 'b', status: 'done', roundNum: 2, llmRound: 1 },
          { toolCallId: 'c', status: 'running', roundNum: 3, llmRound: 2 },
        ],
      },
    ],
  };
  global.conversations = window.conversations = [conv];
  global.activeStreams = window.activeStreams = new Map();
  calls = {
    sync: 0, continuePosts: 0, startAssistant: 0, finalize: 0, startStreaming: 0,
    connect: [], replaceAll: [], updateStreamingUI: [], scrollToBottom: [],
    forceScroll: [], toasts: [], debugLogs: [],
    atBuildConfig: null, atSync: null, atPost: null,
  };

  api = () => { throw new Error('network down'); };
  await continueAssistant();

  /* ★ THE PIN: the turn the user asked to resume must STILL BE ON SCREEN. */
  const tailEl = inner.querySelector('[data-msg-id="a-tail"]');
  check('dxf_tail_bubble_survives', !!tailEl);
  check('dxf_tail_not_a_live_shell',
        !!tailEl && tailEl.id !== 'streaming-msg');
  check('dxf_tail_has_no_pulse',
        !!tailEl && tailEl.querySelectorAll('.stream-status .pulse').length === 0);
  check('dxf_no_streaming_left', document.getElementById('streaming-msg') === null);

  /* The historical bubble is collateral in the same sweep — pin it too. */
  const oldEl = inner.querySelector('[data-msg-id="a-old"]');
  check('dxf_historical_survives', !!oldEl);
  check('dxf_historical_prose_intact',
        !!oldEl && /HISTORICAL ANSWER/.test(oldEl.textContent));

  /* Nothing may be lost OR duplicated by the rollback. */
  check('dxf_msg_count_unchanged', inner.querySelectorAll('.message').length === 3);
  check('dxf_no_tail_duplicate',
        inner.querySelectorAll('[data-msg-id="a-tail"]').length === 1);

  /* And the failure is still honest + retryable. */
  check('dxf_visible_error', calls.toasts.length >= 1);
  check('dxf_flag_cleared', !conv._continueInFlight);
  check('dxf_no_task_bound', !conv.activeTaskId);
  check('dxf_message_doc_intact',
        conv.messages[1].toolRounds.length === 3
        && conv.messages[1].finishReason === 'interrupted');
""" + _EPILOGUE


def test_failed_continue_keeps_the_bubble_under_index_drift():
    run_harness(target_js=SRC_JS, body_js=_BODY_DRIFT_FAILURE, min_pass=13,
                extra_targets=[CONV_VIEW_JS],
                label='continue-drift-x-failure')


def test_finalize_streaming_keeps_the_node_it_just_created():
    """Source-level complement to Guard 6 — pins the MECHANISM.

    ``finalizeStreaming``'s identity sweep must keep the node it JUST wrote,
    held by REFERENCE. Re-finding it via ``getElementById('msg-' + idx)`` is
    unsafe: right after the swap TWO nodes can carry that positional id (the
    freshly-finalized one and a stale bubble sitting at the same slot after an
    index shift), and ``getElementById`` returns the FIRST — so the sweep keeps
    the wrong node and evicts the one it just restored.
    """
    conv_view = CONV_VIEW_JS
    with open(conv_view, encoding='utf-8') as f:
        src = f.read()
    start = src.index('finalizeStreaming: function')
    body = src[start:]
    code = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    code = re.sub(r'^\s*//.*$', '', code, flags=re.M)
    # Bound to the sweep call itself.
    i_sweep = code.index('_evictByMsgId(')
    window = code[max(0, i_sweep - 400):i_sweep + 120]
    assert "getElementById('msg-' + idx)" not in window, (
        "finalizeStreaming's identity sweep still resolves its keep-node via "
        "getElementById('msg-' + idx). Directly after the swap that id can match "
        'TWO nodes (the just-finalized one and a stale bubble at the same slot '
        'after an index shift), and getElementById returns the FIRST — so the '
        'sweep evicts the bubble it just restored. Hold the new node by '
        'reference instead (replaceWith).')


def test_epilogue_marker_present_for_all_guards():
    """Cheap self-check that the harness prologue still exposes the three
    in-await DOM snapshots the twin-bubble pin depends on. Without them the
    'no_twin_before_*' checks would silently degrade to `!!null && ...` → FAIL,
    but a future refactor could delete a snapshot and quietly weaken the pin."""
    for name in ('atBuildConfig', 'atSync', 'atPost'):
        assert _PROLOGUE.count(name) >= 2, (
            f'harness snapshot {name} is no longer both written and read — the '
            'twin-bubble pin would be vacuous')
    assert 'domSnap()' in _PROLOGUE
