"""Golden test for the ``needsTranslation`` pure predicate (coverage decision,
decoupling Symptom-1 fix) in core/translation_model.js.

WHY
---
"Should this message be auto-translated?" used to be smeared across three
places with divergent answers — the backend net (send-time-FROZEN per-conv
flag), the frontend resume Phase-0b (last-assistant-only + break), and the
effective-toggle resolver. That scatter is exactly the coverage gap: turning
auto-translate ON left every message except the last untranslated forever.

``needsTranslation(msg, conv, {autoTranslateOn, policy})`` is now the ONE pure
authority every trigger path consults (same discipline as displayContent /
readTranslation). This test pins its truth table AND the property that most
directly fixes the bug: a ≥3-message untranslated history sweeps ALL of them,
not just the last.

Runs the real module under node; skips cleanly without node+jsdom.
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
setup({ root: process.argv[3], html: '<!DOCTYPE html><body></body>',
        targets: [process.argv[2]], globals: {} });

function check(name, cond) { console.log((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof needsTranslation !== 'function' || typeof isStalePartialTranslation !== 'function') {
  console.log('FAIL predicates_exposed'); return;
}
check('predicates_exposed', true);

const ON = { autoTranslateOn: true };
const OFF = { autoTranslateOn: false };
const conv = { id: 'c' };

// ── toggle gating ──
// frozen-off-but-global-on: the CALLER resolves the effective flag; needsTranslation
// only sees the resolved boolean. With it ON, an untranslated assistant qualifies.
check('assistant_untranslated_on', needsTranslation({ role:'assistant', content:'hi' }, conv, ON) === true);
check('toggle_off_never', needsTranslation({ role:'assistant', content:'hi' }, conv, OFF) === false);

// ── already translated / owned states ──
check('already_translated_skip',
  needsTranslation({ role:'assistant', content:'hi', translatedContent:'你好', _translateDone:true }, conv, ON) === false);
check('done_true_skip',
  needsTranslation({ role:'assistant', content:'hi', _translateDone:true }, conv, ON) === false);
check('live_task_skip',
  needsTranslation({ role:'assistant', content:'hi', _translateTaskId:'t1' }, conv, ON) === false);
check('server_running_frame_skip',
  needsTranslation({ role:'assistant', content:'hi', _translateDone:false }, conv, ON) === false);

// ── stale-<15% partial IS re-translated even though translatedContent exists ──
const stale = { role:'assistant', content:'x'.repeat(1000), translatedContent:'短', _translateDone:true };
check('stale_partial_needs', needsTranslation(stale, conv, ON, ) === true);
check('stale_partial_detector', isStalePartialTranslation(stale) === true);
// a full (non-stale) translation is NOT stale
check('full_translation_not_stale',
  isStalePartialTranslation({ role:'assistant', content:'x'.repeat(1000), translatedContent:'完整译文'.repeat(120) }) === false);
// short source below min_source_chars is never "stale" (avoids re-translate churn)
check('short_source_not_stale',
  isStalePartialTranslation({ role:'assistant', content:'short', translatedContent:'x' }) === false);

// ── role scope: VU + critic users ARE display-translated; a plain user is NOT ──
check('vu_user_needs',
  needsTranslation({ role:'user', _isVirtualUser:true, content:'VU body' }, conv, ON) === true);
check('critic_user_needs',
  needsTranslation({ role:'user', _isEndpointReview:true, content:'verdict' }, conv, ON) === true);
check('plain_user_never',
  needsTranslation({ role:'user', content:'typed', originalContent:'中文' }, conv, ON) === false);

// ── image-gen bubbles are never translated ──
check('imagegen_single_skip',
  needsTranslation({ role:'assistant', content:'desc', _igResult:{ image_url:'x' } }, conv, ON) === false);
check('imagegen_batch_skip',
  needsTranslation({ role:'assistant', content:'desc', _igResults:[{}] }, conv, ON) === false);
check('imagegen_user_skip',
  needsTranslation({ role:'user', content:'prompt', _isImageGen:true }, conv, ON) === false);

// ── empty / missing content ──
check('empty_content_skip', needsTranslation({ role:'assistant', content:'' }, conv, ON) === false);
check('whitespace_content_skip', needsTranslation({ role:'assistant', content:'   ' }, conv, ON) === false);
check('null_msg_skip', needsTranslation(null, conv, ON) === false);

// ══ THE COVERAGE FIX: a ≥3-message untranslated history sweeps ALL, not last ══
// (Simulate the resume-sweep driver: filter the window by needsTranslation.)
const history = [
  { role:'user', content:'q1', originalContent:'问题1' },        // plain user — skip
  { role:'assistant', content:'answer 1' },                       // untranslated — NEEDS
  { role:'user', content:'q2', originalContent:'问题2' },        // plain user — skip
  { role:'assistant', content:'answer 2' },                       // untranslated — NEEDS
  { role:'user', content:'q3', originalContent:'问题3' },        // plain user — skip
  { role:'assistant', content:'answer 3' },                       // untranslated — NEEDS (the last)
];
const toTranslate = history.map((m, i) => ({ m, i }))
  .filter(({ m }) => needsTranslation(m, conv, ON))
  .map(({ i }) => i);
check('sweep_selects_all_three_assistants', JSON.stringify(toTranslate) === JSON.stringify([1, 3, 5]));
// The bug was "only the last": prove indices 1 and 3 (EARLIER assistants) are included.
check('sweep_includes_earlier_not_just_last', toTranslate.indexOf(1) !== -1 && toTranslate.indexOf(3) !== -1);

// A history where the middle assistant is already translated: it's skipped, the
// others still qualify (idempotent — re-running the sweep won't re-dispatch it).
const history2 = [
  { role:'assistant', content:'a1' },
  { role:'assistant', content:'a2', translatedContent:'译2', _translateDone:true },
  { role:'assistant', content:'a3' },
];
const sel2 = history2.map((m, i) => ({ m, i })).filter(({ m }) => needsTranslation(m, conv, ON)).map(({ i }) => i);
check('sweep_idempotent_skips_translated', JSON.stringify(sel2) === JSON.stringify([0, 2]));
"""


def test_needs_translation_predicate():
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'translation_model.js'),
        body_js=_BODY,
        min_pass=22,
        label='needs-translation-golden',
    )
