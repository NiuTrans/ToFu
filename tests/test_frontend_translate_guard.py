"""Phase-3 regression: the FRONTEND translate path is id-anchored + dedup-safe,
mirroring the backend contract.

Two fragilities Phase 1/2 did NOT touch on the frontend:

  1. POSITIONAL DRIFT — the push 'translate' subscriber fell back to using the
     raw ``frame.msgIdx`` as an array index even when a stable ``msgId`` was
     present. After a truncation/insert the stale index paints the WRONG
     bubble. Fix: resolve ONLY by ``_msgId`` when the frame carries one; use
     the positional index only for legacy (no-id) frames.

  2. NO DEDUP — a manual Translate click racing the server safety-net (or two
     clicks) could both run + render the same message. Fix: a per-(conv,msgId)
     in-flight guard (core/translate_guard.js) claimed before scheduling; the
     later caller stands down.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
The guard module is loaded directly; the subscriber's id-resolution logic is
exercised via a faithful port driven by the same inputs (the production
resolver is a few lines inside an IIFE that wires pushSubscribe, so we test the
exact resolution rule it now implements).
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


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
// core/translate_guard.js is all top-level declarations — eval defines the
// guard fns with no side effects.
eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/translate_guard.js
eval(fs.readFileSync(process.argv[4], 'utf8'));  // core/translation_model.js (needsTranslation/readTranslation dep)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Stub the globals the REAL push subscriber touches, and CAPTURE the
//    handler the translation.js IIFE registers via pushSubscribe('translate').
let _pushHandler = null;
global.pushSubscribe = (channel, taskId, fn) => { if (channel === 'translate') _pushHandler = fn; };
global.conversations = [];
global.saveConversations = () => {};
global._renderMsgInPlace = () => {};
global.emitMessageChanged = () => true;   // render seam (ui/translation_render.js) — stubbed
global._markStreamXlateFinal = () => {};
global._armAutoTranslateWatchdog = () => {};
global._renderStreamingTranslatePreview = () => false;
global._applyPartialByRoundToSettled = () => false;
global._applyTranslationStatus = () => {};
global.errorEnvelopeMessage = () => '';
// translation.js declares many top-level helpers; eval it so the IIFE at the
// bottom registers the real subscriber against our pushSubscribe stub.
eval(fs.readFileSync(process.argv[3], 'utf8'));  // translation.js
if (typeof _pushHandler !== 'function') {
  console.log('FAIL push_handler_captured the translate subscriber did not register');
  process.exit(0);
}
check('push_handler_captured', true);

// ─────────────────────────────────────────────────────────────
// PART A — the in-flight guard (core/translate_guard.js)
// ─────────────────────────────────────────────────────────────
if (typeof translateClaim !== 'function' ||
    typeof translateRelease !== 'function' ||
    typeof translateInflight !== 'function') {
  console.log('FAIL guard_exposed translate{Claim,Release,Inflight} missing');
  process.exit(0);
}
check('guard_exposed', true);

// First claim wins; a second claim for the SAME (conv,msgId) stands down.
check('first_claim_wins', translateClaim('c1', 'mid-1', 3) === true);
check('second_claim_stands_down', translateClaim('c1', 'mid-1', 3) === false);
// Even if the index drifted, the id still dedups.
check('id_dedup_ignores_index', translateClaim('c1', 'mid-1', 99) === false);
// Release frees the slot for a legitimate re-translate.
translateRelease('c1', 'mid-1', 3);
check('release_allows_reclaim', translateClaim('c1', 'mid-1', 3) === true);
// Different message / conv are independent.
check('different_msg_independent', translateClaim('c1', 'mid-2', 4) === true);
check('different_conv_independent', translateClaim('c2', 'mid-1', 3) === true);
// No-id falls back to an index key (still dedups on the same index).
check('idx_fallback_claims', translateClaim('c9', '', 7) === true);
check('idx_fallback_dedups', translateClaim('c9', '', 7) === false);
// is-inflight probe reflects state.
check('inflight_probe_true', translateInflight('c1', 'mid-1', 3) === true);
translateRelease('c1', 'mid-1', 3);
check('inflight_probe_false_after_release', translateInflight('c1', 'mid-1', 3) === false);

// ─────────────────────────────────────────────────────────────
// PART B — REAL push subscriber target resolution (id-anchored)
//   Drive the captured handler with 'done' frames and observe which message
//   actually received translatedContent. A 'done' frame writes
//   msg.translatedContent on the resolved message (and only that one).
// ─────────────────────────────────────────────────────────────
function freshConv() {
  return {
    id: 'c1',
    messages: [
      { role: 'user', content: 'q', _msgId: 'mU' },
      { role: 'assistant', content: 'OTHER reply', _msgId: 'mA' },    // idx 1
      { role: 'assistant', content: 'the right reply', _msgId: 'mB' }, // idx 2
    ],
  };
}
function loadConv() { global.conversations = [freshConv()]; return global.conversations[0]; }

// id present + STALE msgIdx (=1, points at mA) → must land on mB (idx 2).
let conv = loadConv();
_pushHandler({ convId: 'c1', msgId: 'mB', msgIdx: 1, status: 'done', translated: 'ZH-B' });
check('id_anchored_writes_correct_msg', conv.messages[2].translatedContent === 'ZH-B');
check('id_anchored_leaves_stale_msg_untouched', conv.messages[1].translatedContent === undefined);

// id present but NOT in memory + msgIdx pointing at a real (wrong) row →
// must DROP (no positional fallback), so NO message is mutated.
conv = loadConv();
_pushHandler({ convId: 'c1', msgId: 'mGONE', msgIdx: 1, status: 'done', translated: 'ZH-X' });
check('id_miss_no_positional_fallback',
  conv.messages.every(m => m.translatedContent === undefined));

// Legacy frame (NO msgId) → positional index allowed → lands on idx 2.
conv = loadConv();
_pushHandler({ convId: 'c1', msgIdx: 2, status: 'done', translated: 'ZH-LEGACY' });
check('legacy_no_id_uses_index', conv.messages[2].translatedContent === 'ZH-LEGACY');

// Legacy frame, out-of-range index → drop safely, nothing mutated.
conv = loadConv();
_pushHandler({ convId: 'c1', msgIdx: 99, status: 'done', translated: 'ZH-OOB' });
check('legacy_oob_index_drops',
  conv.messages.every(m => m.translatedContent === undefined));

// ─────────────────────────────────────────────────────────────
// PART C — REAL _runTranslationPipeline stands down when the guard is
//   already claimed (the manual-click-vs-safety-net double-fire). We stub
//   _startTranslateTask to record whether the pipeline tried to start a task;
//   a stood-down call must NOT call it.
// ─────────────────────────────────────────────────────────────
if (typeof _runTranslationPipeline === 'function') {
  let _startCalls = 0;
  // Stub the REAL network seam _startTranslateTask uses (Api.translate.start);
  // the inner _startTranslateTask is a translation.js const we can't reassign.
  // start() counts attempts; poll() returns terminal 'done' so the REAL poll
  // loop settles on the first tick (we're testing claim/release, not polling).
  global.Api = { translate: {
    start: async () => { _startCalls++; return { taskId: 'task-xyz' }; },
    poll: async () => ({ _ok: true, status: 'done', translated: 'ZH', model: 'fake' }),
  } };
  global._isAlreadyChinese = async () => false;
  global._isStalePartialTranslation = () => false;

  const pconv = { id: 'cP', messages: [{ role: 'assistant', content: 'english', _msgId: 'mP' }] };
  global.conversations = [pconv];
  const pmsg = pconv.messages[0];

  // Pre-claim the slot as if the server safety-net / a first click owns it.
  translateClaim('cP', 'mP', 0);

  // A SECOND path tries to translate the same message → must stand down.
  // (Async — chain the print so it runs after Part C completes. No top-level
  //  `return` in eval'd code, which node rejects as an illegal return.)
  (async () => {
    await _runTranslationPipeline(pconv, 0, pmsg,
      { sourceLang: 'English', targetLang: 'Chinese', field: 'translatedContent', mode: 'manual' });
    check('pipeline_stands_down_when_claimed', _startCalls === 0);

    // After the owner releases, a fresh translate may proceed (claims + starts).
    translateRelease('cP', 'mP', 0);
    await _runTranslationPipeline(pconv, 0, pmsg,
      { sourceLang: 'English', targetLang: 'Chinese', field: 'translatedContent', mode: 'manual' });
    check('pipeline_proceeds_after_release', _startCalls === 1);
    // The pipeline released its own claim on completion (finally), so the slot
    // is free again.
    check('pipeline_releases_on_completion', translateInflight('cP', 'mP', 0) === false);
  })().then(() => console.log(out.join('\n')));
} else {
  console.log(out.join('\n'));
}
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_translate_guard_and_id_anchor():
    harness = os.path.join(HERE, '_translate_guard_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'translate_guard.js'),  # argv[2]
             os.path.join(JS_DIR, 'translation.js'),              # argv[3]
             os.path.join(JS_DIR, 'core', 'translation_model.js'),  # argv[4]
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
    assert not fails, 'FE translate guard / id-anchor failures:\n' + output
    assert output.count('PASS') >= 20, f'expected >=20 PASS lines, got:\n{output}'
