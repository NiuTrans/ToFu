#!/usr/bin/env python3
"""tests/test_frontend_trimmed_tail_continue.py — a WINDOWED/TRIMMED tail turn
must still offer the honest checkpoint Continue, and must never be destroyed
by the client-side empty-guard.

THE REPORTED BUG (owner screenshot, 2026-08-03)
-----------------------------------------------
"Why does the action bar show only 重新生成 (no 继续生成) before I click
加载工具活动 (Load tool activity)?"

ROOT CAUSE — one class, two sites. A windowed first-open serves the tail
messages with the heavy fields STRIPPED for transport
(routes/conversations.py::_trim_heavy_for_window): the assistant message
carries ``_trimmed: true`` + the SERVER-stamped ``_trimmedToolRoundCount: N``
instead of its real ``toolRounds``. Any LOCAL judgment that scans
``msg.toolRounds`` then sees 0 rounds:

  SITE 1 (button lie — static/js/ui/chat_render.js):
    ``continueButtonForSettlement(computeTurnSettlement(msg, model))``
    fail-closes to ``resume.mode='regenerate'`` (no local checkpoint, an
    'error' finish is not prefill-resumable) → the action bar renders ONLY
    "Regenerate" even though the checkpoint provably exists in the DB. The
    click re-verifies via /api/chat/continue's authoritative rescan
    (scan_continue_checkpoint), so the label — not the resume — was wrong.
    FIX: at the button gate ONLY (computeTurnSettlement stays byte-locked by
    tests/test_frontend_turn_settlement_equivalence.py), a verdict that would
    offer a resume at all (show:true, kind:'regenerate') is upgraded to the
    checkpoint Continue when ``_trimmed && _trimmedToolRoundCount > 0``. The
    count is a server FACT, not uncertainty — fail-closed philosophy intact.
    A clean finish (show:false) is never touched; hydrateFullConversation
    clears ``_trimmed`` on refill, after which the verdict computes the real
    keptRounds itself.

  SITE 2 (data loss — static/js/main/main_regen_continue.js):
    ``continueAssistant``'s empty-guard shortcut pops a turn that has no
    content/thinking/toolRounds LOCALLY and syncs with ``allowTruncate:true``
    — which SKIPS the PUT heavy-field preservation guard
    (routes/conversations.py, "windowed/trimmed-read guard"). For a trimmed
    error tail (rounds only exist server-side) that one click permanently
    destroyed the recoverable checkpoint in the DB and regenerated from
    scratch — the server was never asked. FIX: ``_hasRounds`` also honours
    the server-stamped ``_trimmedToolRoundCount``; a trimmed turn falls
    through to the POST where scan_continue_checkpoint rescan owns the
    verdict (a truly-empty turn still comes back fallback:'regenerate').

WHAT THIS FILE PINS
  Part A (render gate, real renderMessage under jsdom):
    A1 trimmed empty-looking error tail → Continue (checkpoint kind), not Regenerate
    A2 trimmed error tail with partial content → Continue (checkpoint kind)
    A3 trimmed CLEAN turn → NO continue button at all (no over-upgrade)
    A4 untrimmed error tail, no rounds → honest Regenerate (unchanged)
    A5 _trimmed marker but count 0/absent → honest Regenerate (unchanged)
    NC-A: removing the gate upgrade turns A1/A2 red.
  Part B (empty-guard, real continueAssistant under jsdom):
    B1 trimmed empty-looking tail → NOT popped, sync carries NO allowTruncate,
       /api/chat/continue IS posted (server rescan owns the verdict)
    B2 trimmed tail + server fallback:'regenerate' → popped THEN (server-owned)
    B0 untrimmed truly-empty tail → the local shortcut still fires (no drift)
    NC-B: reverting _hasRounds to ignore the trimmed count turns B1 red.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
TRANSLATION_MODEL = os.path.join(JS_DIR, 'core', 'translation_model.js')
TRANSLATION_INDICATOR = os.path.join(JS_DIR, 'ui', 'translation_indicator.js')
TURN_SETTLEMENT = os.path.join(JS_DIR, 'core', 'turn_settlement.js')
REGEN_CONTINUE = os.path.join(JS_DIR, 'main', 'main_regen_continue.js')

# The shipped gate-upgrade block (Site 1). The NEUTER copies remove exactly
# this region — keep the anchors byte-identical to chat_render.js.
_GATE_ANCHOR_START = "    if (_tsBtn.show && _tsBtn.kind === 'regenerate'\n"
_GATE_ANCHOR_END = "      };\n    }\n"


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ══════════════════════════════════════════════════════════════════════════
#  Part A — the action-bar gate on a trimmed tail (REAL renderMessage)
# ══════════════════════════════════════════════════════════════════════════
_HARNESS_A = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[7];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const _conv = { id: 'c-trim', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-trim';
win.getActiveConv = global.getActiveConv = () => _conv;

win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '<div class="ptool-panel">TOOLS</div>';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[5], 'utf8'));  // translation_model.js
(0, eval)(fs.readFileSync(process.argv[6], 'utf8'));  // translation_indicator.js
(0, eval)(fs.readFileSync(process.argv[8], 'utf8'));  // turn_settlement.js (REAL verdict)
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // chat_render.js (real / neutered)

if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
check('fn_exposed', true);

/* The continue/regenerate discrimination: BOTH kinds render .msg-continue-btn,
 * so the TITLE is the honest face ('Continue generating from where it left
 * off' vs 'Regenerate this response' — the harness `t` echo makes _mt fall
 * back to the English strings). */
function renderTail(msg) {
  _conv.messages = [
    { role: 'user', _msgId: 'u1', content: 'ask' },
    msg,
  ];
  const html = renderMessage(msg, 1);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  const btn = frag.querySelector('.msg-continue-btn');
  return {
    html: html,
    btn: btn,
    title: btn ? (btn.getAttribute('title') || '') : '',
    chip: !!frag.querySelector('.trimmed-tool-activity'),
  };
}

// ── A1: trimmed, empty-LOOKING error tail (the owner screenshot shape) ──
{
  const r = renderTail({
    role: 'assistant', _msgId: 'a1', content: '', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3', _trimmed: true, _trimmedToolRoundCount: 9,
  });
  check('a1_scenario_is_trimmed_shape', r.chip === true);
  check('a1_button_present', !!r.btn);
  check('a1_button_is_checkpoint_continue',
        r.title.indexOf('Continue generating from where it left off') !== -1);
  check('a1_button_not_regenerate', r.title.indexOf('Regenerate') === -1);
}

// ── A2: trimmed error tail WITH partial content → same checkpoint Continue ──
{
  const r = renderTail({
    role: 'assistant', _msgId: 'a2', content: 'partial answer', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3', _trimmed: true, _trimmedToolRoundCount: 9,
  });
  check('a2_button_present', !!r.btn);
  check('a2_button_is_checkpoint_continue',
        r.title.indexOf('Continue generating from where it left off') !== -1);
}

// ── A3 control: trimmed CLEAN turn → NO continue affordance at all ──
{
  const r = renderTail({
    role: 'assistant', _msgId: 'a3', content: 'done answer', thinking: '',
    toolRounds: [], finishReason: 'stop',
    model: 'kimi-k3', _trimmed: true, _trimmedToolRoundCount: 9,
  });
  check('a3_clean_turn_no_button', !r.btn);
}

// ── A4 control: UNTRIMMED error tail with no rounds → honest Regenerate ──
{
  const r = renderTail({
    role: 'assistant', _msgId: 'a4', content: 'partial answer', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3',
  });
  check('a4_button_present', !!r.btn);
  check('a4_honest_regenerate_unchanged',
        r.title.indexOf('Regenerate this response') !== -1);
}

// ── A5 control: _trimmed marker but count 0/absent → no upgrade ──
{
  const r = renderTail({
    role: 'assistant', _msgId: 'a5', content: 'partial answer', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3', _trimmed: true,
  });
  check('a5_count_absent_stays_regenerate',
        r.title.indexOf('Regenerate this response') !== -1);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run_a(chat_render_path: str) -> str:
    harness = os.path.join(HERE, '_trimmed_gate_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS_A)
    try:
        proc = subprocess.run(
            ['node', harness,
             chat_render_path,      # argv[2]
             ESCAPE_HTML,           # argv[3]
             SAFE_HTML,             # argv[4]
             TRANSLATION_MODEL,     # argv[5]
             TRANSLATION_INDICATOR, # argv[6]
             ROOT,                  # argv[7]
             TURN_SETTLEMENT,       # argv[8]
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
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_trimmed_tail_offers_checkpoint_continue():
    output = _run_a(CHAT_RENDER)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'trimmed-tail continue-gate failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_gate_upgrade_removed_turns_red(tmp_path):
    """NEUTER-A: delete the gate-upgrade block from a COPY of chat_render.js —
    the trimmed error tail must fall back to the 'Regenerate' lie, failing A1."""
    src = open(CHAT_RENDER, encoding='utf-8').read()
    start = src.index(_GATE_ANCHOR_START)
    end = src.index(_GATE_ANCHOR_END, start) + len(_GATE_ANCHOR_END)
    neutered = src[:start] + src[end:]
    assert neutered != src
    nfile = tmp_path / 'chat_render_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    output = _run_a(str(nfile))
    assert 'FAIL a1_button_is_checkpoint_continue' in output, (
        'NEUTER did not bite: without the gate upgrade the trimmed tail still '
        f'showed Continue — the A1 assertion is not load-bearing:\n{output}')
    # Controls must stay green under the neuter (the mutation is surgical).
    assert 'PASS a3_clean_turn_no_button' in output
    assert 'PASS a4_honest_regenerate_unchanged' in output


# ══════════════════════════════════════════════════════════════════════════
#  Part B — the empty-guard must not destroy a trimmed tail (REAL
#  continueAssistant under jsdom)
# ══════════════════════════════════════════════════════════════════════════
_PROLOGUE_B = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let conv = null;
let calls = null;
let api = null;

const HTML = '<!DOCTYPE html><body><div id="chatContainer">'
           + '<div id="chatInner"></div></div></body>';

function staticAssistantHtml(idx, msgId) {
  return '<div class="message ep-worker-msg" id="msg-' + idx + '"'
    + ' data-msg-id="' + msgId + '">'
    + '<div class="message-avatar"></div><div class="message-content">'
    + '<div class="message-header"><span class="message-role">Agent</span></div>'
    + '<div class="message-body"><div class="md-content"></div></div>'
    + '<div class="message-actions"><button class="msg-continue-btn">Continue</button></div>'
    + '</div></div>';
}
function staticUserHtml(idx) {
  return '<div class="message user-msg" id="msg-' + idx + '" data-msg-id="u1">'
    + '<div class="message-content"><div class="message-body">ask</div></div></div>';
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
    _buildConvConfig: async () => ({}),
    syncConversationToServer: async (c, opts) => {
      calls.sync++;
      calls.syncOpts.push(opts || {});
      return true;
    },
    saveConversations: () => {},
    buildTurnNav: () => {},
    renderConversationList: () => {},
    connectToTask: (cid, tid) => { calls.connect.push(tid); },
    startAssistantResponse: async () => { calls.startAssistant++; },
    updateStreamingUI: () => {},
    scrollToBottom: () => {},
    _forceScrollToBottom: () => {},
    showToast: (msg, kind) => { calls.toasts.push(String(msg)); },
    debugLog: () => {},
    updateContextBar: () => {},
    ConvCache: { put: () => {} },
    Api: {
      chat: {
        continue: async () => { calls.continuePosts++; return api(); },
        abortConv: async () => {},
      },
    },
  },
});

window.ConvView = global.ConvView = {
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
  replaceAll: () => true,
  startStreaming: () => true,
  finalizeStreaming: (cid, msg) => {
    const sm = document.getElementById('streaming-msg');
    if (sm) sm.id = 'msg-' + (conv.messages.length - 1);
    return true;
  },
};

function resetScene(tailMsg) {
  const inner = document.getElementById('chatInner');
  inner.innerHTML = staticUserHtml(0) + staticAssistantHtml(1, tailMsg._msgId);
  conv = {
    id: 'c1',
    activeTaskId: null,
    messages: [{ role: 'user', content: 'ask', _msgId: 'u1' }, tailMsg],
  };
  global.conversations = window.conversations = [conv];
  global.activeStreams = window.activeStreams = new Map();
  calls = { sync: 0, syncOpts: [], continuePosts: 0, startAssistant: 0,
            connect: [], toasts: [] };
}

/* The trimmed, empty-LOOKING error tail — exactly what a windowed first-open
 * serves for a tools turn cut by a 401/429 (rounds exist only server-side). */
function trimmedErrorTail() {
  return {
    role: 'assistant', _msgId: 'a1', content: '', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3', _trimmed: true, _trimmedToolRoundCount: 9,
  };
}
function okCheckpoint() {
  return {
    ok: true,
    json: async () => ({
      taskId: 'T-new',
      checkpoint: {
        resumeMode: 'checkpoint', keptRounds: 9, discardedRounds: 0,
        contentPrefix: '', priorContent: '', priorThinking: '',
        preservedContentLen: 0, discardedContentLen: 0,
        preservedThinkingChars: 0, discardedThinking: 0,
      },
    }),
  };
}
function fallbackRegenerate() {
  return { ok: true, json: async () => ({ fallback: 'regenerate',
                                          reason: 'empty_assistant' }) };
}
"""

_EPILOGUE_B = (
    "\n})().then(report).catch((e) => {\n"
    "  console.log('FAIL harness_threw ' + ((e && e.stack) || e));\n"
    "  report();\n"
    "});\n"
)

_BODY_B = _PROLOGUE_B + r"""
(async () => {
  // ── B1: trimmed error tail must reach the SERVER rescan, never the local
  //    pop-and-regenerate shortcut (which syncs allowTruncate:true and would
  //    destroy the DB checkpoint the heavy-field guard exists to protect). ──
  resetScene(trimmedErrorTail());
  api = okCheckpoint;
  await continueAssistant();
  check('b1_tail_not_popped', conv.messages.length === 2);
  check('b1_no_truncate_sync',
        !calls.syncOpts.some((o) => o.allowTruncate === true));
  check('b1_server_consulted', calls.continuePosts === 1);
  check('b1_no_local_regenerate', calls.startAssistant === 0);
  check('b1_task_bound', conv.activeTaskId === 'T-new');

  // ── B2: a trimmed tail the SERVER finds unrecoverable is still popped —
  //    the regenerate decision is server-owned, not client-removed. ──
  resetScene(trimmedErrorTail());
  api = fallbackRegenerate;
  await continueAssistant();
  check('b2_server_fallback_pops', conv.messages.length === 1);
  check('b2_server_fallback_regenerates', calls.startAssistant === 1);

  // ── B0 control: an UNTRIMMED truly-empty tail still takes the LOCAL
  //    shortcut (the pre-existing cheap path is untouched). ──
  resetScene({
    role: 'assistant', _msgId: 'a1', content: '', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3',
  });
  api = okCheckpoint;
  await continueAssistant();
  check('b0_untrimmed_empty_popped_locally', conv.messages.length === 1);
  check('b0_untrimmed_empty_syncs_truncate',
        calls.syncOpts.some((o) => o.allowTruncate === true));
  check('b0_untrimmed_empty_no_post', calls.continuePosts === 0);
  check('b0_untrimmed_empty_regenerates', calls.startAssistant === 1);
""" + _EPILOGUE_B


def test_trimmed_tail_empty_guard_never_destroys_checkpoint():
    run_harness(target_js=REGEN_CONTINUE, body_js=_BODY_B, min_pass=11,
                label='trimmed-empty-guard')


def test_nc_hasrounds_ignores_trimmed_count_turns_red(tmp_path):
    """NEUTER-B: revert _hasRounds to ignore _trimmedToolRoundCount — B1 must go
    red (the trimmed tail is popped locally, the server is never consulted)."""
    src = open(REGEN_CONTINUE, encoding='utf-8').read()
    anchor = ("    || !!(assistantMsg._trimmed && "
              "(assistantMsg._trimmedToolRoundCount || 0) > 0);")
    assert anchor in src, (
        '_hasRounds trimmed-count anchor missing — the Site-2 fix is gone; '
        'update the neuter target')
    neutered = src.replace(anchor, '    || false;  // NEUTER: ignore trimmed count')
    assert neutered != src
    nfile = tmp_path / 'main_regen_continue_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    proc_out = None
    try:
        proc_out = run_harness(target_js=str(nfile), body_js=_BODY_B,
                               min_pass=0, label='trimmed-empty-guard-neuter')
        raise AssertionError(
            'NEUTER did not bite: ignoring the trimmed count still passed B1 — '
            'the guard is not load-bearing.')
    except AssertionError as e:
        msg = str(e)
        assert ('b1_tail_not_popped' in msg
                or 'b1_server_consulted' in msg
                or 'b1_no_truncate_sync' in msg), (
            'NEUTER turned red but on the WRONG check — expected a b1_* '
            f'failure:\n{msg}')
        _ = proc_out


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
