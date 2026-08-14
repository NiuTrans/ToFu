"""Auto-translate finalize must be ORDER-INDEPENDENT vs the chat `done`.

WHY
---
The auto-translate `done` push (channel 'translate') and the chat `done` SSE
event are two INDEPENDENT async completions with NO ordering guarantee. The
chat `done` handler is what projects the backend-committed
``toolRounds``/``segments``/``finishReason``/``usage``/``cost`` onto the message
(``committedMessage`` verbatim projection in ui/sse_pipeline.js). The translate
`done` handler only stamps ``translatedContent``/``_showingTranslation``.

Before this fix, the translate `done` handler unconditionally called
``emitMessageChanged(convId, idx, msg, {kind:'full'})`` — a WHOLE-bubble
``outerHTML = renderMessage(msg, idx)`` repaint. When the translate frame WON
the race (arrived while the assistant turn was still streaming / before the
chat `done` projection), that repaint re-ran renderMessage against a message
whose settled fields had NOT landed yet, so:
  • the tool panel vanished (no committed segments/toolRounds to render), and
  • renderFinishInfo — gated on ``finishReason || usage`` — degraded to a bare
    model tag (no rounds / cost / finish reason).
It self-healed a moment later when the chat `done` projection + finishStream
re-rendered the complete bubble. That transient "giant purple translation box,
tools gone, finish bar stripped, then normal again" is the reported bug.

ROOT FIX (order-independent, NOT a timing tweak): the translate finalize only
does the whole-bubble repaint when the turn is ALREADY SETTLED (finishReason or
usage present — the committed fields are on the message and renderFinishInfo has
its terminal signal). When it is NOT settled AND a live stream/task is still
finalizing this conv, it DEFERS: ``translatedContent`` is already stamped, and
the imminent chat-`done` committedMessage projection + finishStream's
finalizeStreaming render the COMPLETE bilingual bubble in one pass (that render
reads translatedContent, so the translation is never lost). Historical / manual
retranslation of an already-finished message is settled → repaints immediately
as before, so the translation still surfaces for the normal case.

These harnesses drive the REAL shipped translate push subscriber (translation.js)
and the REAL renderFinishInfo (ui/finish_info.js) under jsdom, stubbing only the
render/network seams. Each guard is paired with a NEUTER that disables the
mechanism and proves it is load-bearing.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════════
#  Harness 1 — the finalize SEAM (drives translation.js's real push handler).
#
#  emitMessageChanged is stubbed as a global SPY. translation.js calls it by
#  bare name, so the global stub intercepts every whole-bubble repaint request.
#  The gate function _translateFinalizeShouldDefer(convId, msg) is a top-level
#  declaration in translation.js → a global the harness can rebind to NEUTER it.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_SEAM = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let _pushHandler = null;
const _emitCalls = [];   // records every emitMessageChanged({kind:'full'|'status'|...})

const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],  // translation.js
  globals: {
    // cA: live stream active (translate wins the race — MUST defer)
    // cB: no stream, settled msg (manual/historical — MUST repaint now)
    // cC: live stream active (NEUTER reuse)
    activeConvId: 'cA',
    activeStreams: new Map([['cA', { taskId: 't1' }], ['cC', { taskId: 't3' }]]),
    conversations: [
      { id: 'cA', activeTaskId: 't1', messages: [
        // Unsettled live assistant: has streamed toolRounds but NO
        // finishReason / usage / cost yet (chat `done` hasn't projected).
        { role: 'assistant', content: 'English body.', _msgId: 'mA',
          toolRounds: [{ roundNum: 1, toolName: 'read_files', status: 'done' }] },
      ] },
      { id: 'cB', activeTaskId: null, messages: [
        // Settled historical assistant: full committed fields present.
        { role: 'assistant', content: 'English body B.', _msgId: 'mB',
          toolRounds: [{ roundNum: 1, toolName: 'grep_search', status: 'done' }],
          finishReason: 'stop', usage: { prompt_tokens: 100, completion_tokens: 50 },
          cost: { costCny: 0.012 } },
      ] },
      { id: 'cC', activeTaskId: 't3', messages: [
        { role: 'assistant', content: 'English body C.', _msgId: 'mC',
          toolRounds: [{ roundNum: 1, toolName: 'run_command', status: 'done' }] },
      ] },
    ],
    pushSubscribe: (channel, taskId, fn) => { if (channel === 'translate') _pushHandler = fn; },
    saveConversations: () => {},
    // THE SEAM SPY — every whole-bubble repaint request lands here.
    emitMessageChanged: (convId, idx, msg, detail) => {
      _emitCalls.push({ convId, idx, kind: (detail && detail.kind) || 'full',
                        msgId: msg && msg._msgId });
    },
    _patchMessageOnServer: () => {},
    _armAutoTranslateWatchdog: () => {},
    stripNoTranslateTags: (s) => s,
  },
});

check('push_subscriber_registered', typeof _pushHandler === 'function');
const convA = conversations[0], msgA = convA.messages[0];
const convB = conversations[1], msgB = convB.messages[0];
const convC = conversations[2], msgC = convC.messages[0];

function emitFullFor(msgId) {
  return _emitCalls.filter(c => c.msgId === msgId && c.kind === 'full').length;
}

// ── Guard 1 — TRANSLATE WINS THE RACE (unsettled + live stream) → DEFER ──
// The translate `done` arrives before the chat `done` projection. The handler
// must stamp the translation onto state but must NOT fire the whole-bubble
// repaint (which would read the incomplete msg and drop the tool panel /
// degrade the finish bar).
_pushHandler({ status: 'done', translated: '中文正文。', convId: 'cA',
               msgId: 'mA', field: 'translatedContent', model: 'm1',
               segmentsByRound: { '0': '第零轮中文。' } });

check('unsettled_translation_state_stamped',
      msgA.translatedContent === '中文正文。' && msgA._showingTranslation === true);
// THE CORE GUARD: no whole-bubble repaint while unsettled.
check('unsettled_defers_no_full_repaint', emitFullFor('mA') === 0);
// The settled fields the chat `done` owns are NOT touched/cleared by translate.
check('unsettled_toolRounds_preserved',
      Array.isArray(msgA.toolRounds) && msgA.toolRounds.length === 1
      && msgA.toolRounds[0].toolName === 'read_files');
check('unsettled_no_finishReason_invented', !msgA.finishReason);
check('unsettled_no_usage_invented', !msgA.usage);
check('unsettled_no_cost_invented', !msgA.cost);

// ── Guard 2 — SETTLED (manual / historical) → REPAINT NOW ──
// A translation landing on an already-finished message MUST still paint (the
// defer only applies while the turn is finalizing). Order-independence: the
// translation surfaces for the common / manual case, not only the race case.
_pushHandler({ status: 'done', translated: '中文正文B。', convId: 'cB',
               msgId: 'mB', field: 'translatedContent', model: 'm1' });
check('settled_translation_state_stamped',
      msgB.translatedContent === '中文正文B。' && msgB._showingTranslation === true);
check('settled_repaints_now', emitFullFor('mB') === 1);
// Settled fields remain intact (repaint reads a COMPLETE message).
check('settled_toolRounds_preserved',
      Array.isArray(msgB.toolRounds) && msgB.toolRounds.length === 1);
check('settled_finishReason_preserved', msgB.finishReason === 'stop');
check('settled_usage_preserved', !!msgB.usage && msgB.usage.prompt_tokens === 100);
check('settled_cost_preserved', !!msgB.cost && msgB.cost.costCny === 0.012);

// ── Order-independence tail: DEFERRED translation is NOT lost. After the
//    chat `done` projection settles mA (committedMessage stamps
//    finishReason/usage/cost), a subsequent finalize-style repaint request
//    (the finishStream / committedMessage render) paints the bubble WITH the
//    already-stamped translatedContent. We simulate that settle here and prove
//    a repaint for mA can now happen (the gate no longer defers once settled).
msgA.finishReason = 'stop';
msgA.usage = { prompt_tokens: 80, completion_tokens: 40 };
check('deferred_translation_retained_on_msg', msgA.translatedContent === '中文正文。');
// The gate itself, now that mA is settled, would allow a repaint (the function
// is the single source of the decision — assert it directly).
check('gate_allows_repaint_once_settled',
      typeof _translateFinalizeShouldDefer === 'function'
      && _translateFinalizeShouldDefer('cA', msgA) === false);

// ── NEUTER — disable the gate (force it to never defer) and prove the
//    premature repaint returns: the unsettled translate frame now fires the
//    whole-bubble repaint that drops the tool panel / degrades the finish bar.
//    Proves the defer gate is load-bearing, not incidental. ──
const _origGate = _translateFinalizeShouldDefer;
globalThis._translateFinalizeShouldDefer = () => false;   // rebind global
_pushHandler({ status: 'done', translated: '中文正文C。', convId: 'cC',
               msgId: 'mC', field: 'translatedContent', model: 'm1' });
check('NEUTER_unsettled_repaints_when_gate_disabled', emitFullFor('mC') === 1);
globalThis._translateFinalizeShouldDefer = _origGate;     // restore

report();
"""


def test_translate_finalize_order_independent_seam():
    run_harness(
        target_js=os.path.join(JS_DIR, 'translation.js'),
        body_js=_BODY_SEAM,
        min_pass=16,
        label='translate-finalize-order-independent',
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Harness 2 — renderFinishInfo contract (why deferring is SAFE for the bar).
#
#  Locks the two states the defer relies on:
#   • a model-only, UNSETTLED live-tail message → NO finish bar (so a stray
#     repaint while unsettled could never render a lying "finished" bar);
#   • a SETTLED message (finishReason + usage) → a bar with the finish reason,
#     token counts and cost — NOT just the model tag.
#  This is the "finish 栏定稿后必须能画出轮次/花费/结束原因" guard. NEUTER: mark
#  the settled message as the live tail → the premature-bar guard suppresses it,
#  proving the bar's terminal-signal gate is real.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_FINISHBAR = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],  // ui/finish_info.js
  globals: {
    _i18nLang: 'en',
    _featureFlags: { debug_mode: false },
    formatCny: (v) => '¥' + Number(v).toFixed(3),
    calcCostCny: () => null,   // force reliance on msg.cost
    _detectBrand: () => 'generic',
    _brandSvg: () => '',
    _providerDisplayName: (p) => p || '',
    _isThinkingCapable: () => false,
    _i18n: {},
  },
});

check('renderFinishInfo_defined', typeof renderFinishInfo === 'function');

// ── UNSETTLED, model-only, LIVE TAIL → suppressed (no premature bar). ──
const liveTail = { role: 'assistant', content: 'x', model: 'aws.claude-opus' };
const outLive = renderFinishInfo(liveTail, /*isLiveTail=*/true);
check('model_only_live_tail_suppressed', outLive === '');

// ── SETTLED → full bar with reason + tokens + cost, not just the model. ──
const settled = {
  role: 'assistant', content: 'x', model: 'aws.claude-opus',
  finishReason: 'stop',
  usage: { prompt_tokens: 1200, completion_tokens: 300 },
  apiRounds: [{ usage: {} }, { usage: {} }],
  cost: { costCny: 0.0123, cacheSavingsCny: 0 },
  _taskId: 'task-abc',
};
const outSettled = renderFinishInfo(settled, /*isLiveTail=*/false);
check('settled_renders_a_bar', outSettled.indexOf('message-finish') >= 0);
check('settled_has_finish_reason_ok', outSettled.indexOf('finish-tag ok') >= 0);
check('settled_has_token_tag', outSettled.indexOf('token-tag') >= 0);
check('settled_omits_duplicate_activity_summary',
      outSettled.indexOf('finish-activity-tag') < 0);
check('settled_has_cost_tag', outSettled.indexOf('cost-tag') >= 0);
// "不得只剩模型标签": the bar carries MORE than the lone model/preset tag.
check('settled_not_only_model_tag',
      outSettled.indexOf('token-tag') >= 0 && outSettled.indexOf('cost-tag') >= 0);

// ── NEUTER — treat the settled message as the live tail: the premature-bar
//    guard only suppresses when NOT terminal, so a message WITH finishReason
//    still renders even as live tail. Prove the guard keys on the terminal
//    signal, not merely on isLiveTail: a model-ONLY live tail is suppressed
//    (above), a terminal one is NOT. ──
const terminalLiveTail = {
  role: 'assistant', content: 'x', model: 'aws.claude-opus',
  finishReason: 'stop', usage: { prompt_tokens: 10, completion_tokens: 5 },
};
const outTLT = renderFinishInfo(terminalLiveTail, /*isLiveTail=*/true);
check('NEUTER_terminal_survives_live_tail', outTLT.indexOf('message-finish') >= 0);

report();
"""


def test_translate_finish_bar_contract():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'finish_info.js'),
        body_js=_BODY_FINISHBAR,
        min_pass=9,
        label='translate-finish-bar-contract',
    )
