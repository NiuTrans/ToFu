"""F1 — ``finishStream`` decides auto-translate with the EFFECTIVE resolver.

WHY (the reported bug)
----------------------
``finishStream`` used to gate translation ONLY on the FROZEN
``convAutoTranslate(conv)`` (the send-time value the backend safety net also
reads). So a conversation sent with ``autoTranslate`` frozen-OFF that the user
then toggled ON globally got NOTHING at finalize — the translation only appeared
when the user switched AWAY and the effective-gated on-open retro path fired.
That is exactly "nothing happens while I stay focused; a big bar appears the
moment I switch conversations".

Fix: converge the finalize decision on ``convAutoTranslateEffective`` too, with
two branches:
  • frozen-ON  → backend safety net owns it; only ARM the dropped-frame watchdog.
  • effective-ON but frozen-OFF → the backend WON'T translate (it reads the
    frozen-OFF settings), so schedule the client pipeline NOW, in place.
  • effective-OFF → nothing.

Drives the REAL shipped ``finishStream`` (ui/stream_lifecycle.js) under jsdom,
stubbing its many collaborators, and observes WHICH path fired. NEUTER: force
the resolver used to the frozen one and prove the frozen-OFF+global-ON case then
schedules NOTHING (i.e. the effective gate is load-bearing).
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let _pipelineKicks = [];
let _watchdogArms = [];

const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  // core/conversations.js, then core/conv_reducers.js (the resolvers —
  // convAutoTranslate / convAutoTranslateEffective moved there in the
  // pt_3879f00e decomposition), then ui/stream_lifecycle.js (finishStream).
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    activeConvId: 'c1',
    activeStreams: new Map([['c1', { taskId: 't' }]]),   // finishStream deletes it
    saveConversations: () => {},
    renderConversationList: () => {},
    updateSendButton: () => {},
    buildTurnNav: () => {},
    _convRenderFingerprint: () => '',
    isNearBottom: () => false,
    scrollToBottom: () => {},
    ConvCache: { put: () => {} },
    _maybeAutoGenerateTitle: () => {},
    _findAutopilotPendingCarrier: () => null,
    _dispatchableQueueCount: () => 0,
    _checkForQueuedTask: () => {},
    _streamingBubbleHTML: () => '',
    formatClockTime: () => '',
    // _armAutoTranslateWatchdog is defined elsewhere; safe to stub pre-eval.
    _armAutoTranslateWatchdog: (convId, idx, msg) => {
      _watchdogArms.push({ convId, idx });
    },
  },
});

// _startAutoTranslateForMsg is defined IN stream_lifecycle.js (it calls
// _runTranslationPipeline, unstubbed here → throws), so the eval clobbers any
// pre-eval stub. Reassign it AFTER the eval so finishStream calls our observer.
_startAutoTranslateForMsg = (conv, convId, idx, msg) => {
  _pipelineKicks.push({ convId, idx });
};

// conversations.js declares saveConversations / renderConversationList as
// top-level functions, which CLOBBER the pre-eval global stubs (same trap as
// _runTranslationPipeline — the real saveConversations references _convSorter
// and throws). Reassign the stubs AFTER the evals so finishStream uses ours.
saveConversations = () => {};
if (typeof renderConversationList !== 'undefined') renderConversationList = () => {};

if (typeof finishStream !== 'function') { check('finishStream_exposed', false); report(); return; }
check('finishStream_exposed', true);
if (typeof convAutoTranslateEffective !== 'function') { check('effective_exposed', false); report(); return; }
check('effective_exposed', true);

// Build a conv whose stream just finished: a finished assistant tail, no
// translation yet. `frozen` sets the send-time conv.autoTranslate.
function mkConv(frozen) {
  const conv = {
    id: 'c1',
    messages: [
      { role: 'user', content: 'q', _msgId: 'mU' },
      { role: 'assistant', content: 'A finished reply.', _msgId: 'mA', done: true },
    ],
    activeTaskId: 't',
  };
  if (frozen !== undefined) conv.autoTranslate = frozen;
  return conv;
}
function reset(conv) {
  _pipelineKicks = []; _watchdogArms = [];
  global.conversations = [conv];
  global.activeStreams = new Map([['c1', { taskId: 't' }]]);
}

// ── CASE A (the bug): frozen-OFF, global ON → schedule client pipeline NOW. ──
{
  const conv = mkConv(false); reset(conv);
  global.autoTranslate = true;
  finishStream('c1');
  check('A_frozenOFF_globalON_schedules_pipeline',
    _pipelineKicks.some(k => k.convId === 'c1' && k.idx === 1));
  check('A_frozenOFF_globalON_no_watchdog', _watchdogArms.length === 0);
}

// ── CASE B: frozen-ON → backend owns it; arm watchdog, DON'T client-schedule. ──
{
  const conv = mkConv(true); reset(conv);
  global.autoTranslate = true;
  finishStream('c1');
  check('B_frozenON_arms_watchdog',
    _watchdogArms.some(k => k.convId === 'c1' && k.idx === 1));
  check('B_frozenON_no_client_pipeline', _pipelineKicks.length === 0);
}

// ── CASE C: frozen-ON, global OFF → still backend-owned (explicit per-conv). ──
{
  const conv = mkConv(true); reset(conv);
  global.autoTranslate = false;
  finishStream('c1');
  check('C_frozenON_globalOFF_arms_watchdog',
    _watchdogArms.some(k => k.idx === 1));
  check('C_frozenON_globalOFF_no_pipeline', _pipelineKicks.length === 0);
}

// ── CASE D: effective OFF (frozen-OFF + global OFF) → do nothing. ──
{
  const conv = mkConv(false); reset(conv);
  global.autoTranslate = false;
  finishStream('c1');
  check('D_effectiveOFF_nothing',
    _pipelineKicks.length === 0 && _watchdogArms.length === 0);
}

// ── NEUTER: monkeypatch convAutoTranslateEffective to the FROZEN resolver, so
//    the frozen-OFF + global-ON case reads OFF and schedules NOTHING — the
//    original bug. Proves finishStream's use of the effective resolver is the
//    load-bearing fix. ──
{
  const _savedEff = convAutoTranslateEffective;
  convAutoTranslateEffective = (conv) => convAutoTranslate(conv);  // neuter → frozen
  const conv = mkConv(false); reset(conv);
  global.autoTranslate = true;
  finishStream('c1');
  check('NC_neuter_frozen_resolver_schedules_nothing',
    _pipelineKicks.length === 0 && _watchdogArms.length === 0);
  convAutoTranslateEffective = _savedEff;   // restore
}

report();
"""


def test_finalize_effective_translate():
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'conversations.js'),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
                       os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js')],
        min_pass=10,
        label='finalize-effective-translate',
    )
