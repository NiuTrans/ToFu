"""F2 — per-round narration translation is folded into ``_msgFingerprint``.

WHY
---
The interleaved segment timeline renders each round's Chinese from
``seg.translatedText``. That field was NOT covered by any fingerprint token
(``translatedContent`` tracks only the DELIVERABLE), so when a narration-only
translation landed (segmentsByRound / partialByRound) the surgical renderChat
diff saw the row as "unchanged" and never repainted it — leaving the blunt
whole-bubble ``_renderMsgInPlace`` outerHTML swap (a separate, flicker-prone
tick) as the ONLY way tool narration ever turned Chinese.

Folding a compact per-round signature (round + translatedText length) into the
fingerprint makes a narration translation re-render the row through the same
surgical path as the deliverable, coalesced on one tick — no separate flicker.

Drives the REAL shipped ``_msgFingerprint`` from ui/chat_render.js. Paired with
a NEUTER that strips ``translatedText`` and proves the fingerprint then does NOT
move (i.e. the fold is load-bearing).
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],   // ui/chat_render.js
  globals: {},
});

if (typeof _msgFingerprint !== 'function') {
  check('fingerprint_exposed', false);
  report();
  return;
}
check('fingerprint_exposed', true);

// A finished assistant message with two narration segments, no translation yet.
function baseMsg() {
  return {
    role: 'assistant',
    content: 'The final answer.',
    thinking: '',
    segments: [
      { type: 'text', text: 'First narration.', deliverable: false, llmRound: 0 },
      { type: 'tool_use', id: 't0', llmRound: 0 },
      { type: 'text', text: 'Second narration.', deliverable: false, llmRound: 1 },
      { type: 'tool_use', id: 't1', llmRound: 1 },
      { type: 'text', text: 'The final answer.', deliverable: true, terminal: true },
    ],
    toolRounds: [{ roundNum: 0 }, { roundNum: 1 }],
  };
}

// ── Baseline fingerprint (no narration translation). ──
const before = _msgFingerprint(baseMsg());

// ── Stamp round-0's Chinese onto seg.translatedText → fingerprint MUST move. ──
const m1 = baseMsg();
m1.segments[0].translatedText = '第零轮的中文。';
const after1 = _msgFingerprint(m1);
check('fp_changes_when_round0_translated', before !== after1);

// ── Stamp round-1 too → fingerprint moves AGAIN (each round is tracked). ──
const m2 = baseMsg();
m2.segments[0].translatedText = '第零轮的中文。';
m2.segments[2].translatedText = '第一轮的中文。';
const after2 = _msgFingerprint(m2);
check('fp_changes_again_when_round1_translated', after1 !== after2);

// ── A DIFFERENT translation length for the same round moves it again (length
//    is part of the signature, so a growing partial repaints). ──
const m3 = baseMsg();
m3.segments[0].translatedText = '第零轮的中文，更长一些的译文。';
const after3 = _msgFingerprint(m3);
check('fp_sensitive_to_length', after1 !== after3);

// ── The DELIVERABLE/terminal segment's translatedText must NOT affect this
//    token (it is rendered via translatedContent, tracked separately) — proves
//    the fold targets NARRATION only, not double-counting the deliverable. ──
const m4 = baseMsg();
m4.segments[4].translatedText = '成品答案的中文。';  // deliverable segment
const after4 = _msgFingerprint(m4);
check('fp_ignores_deliverable_segment_translatedText', before === after4);

// ── NEUTER: a message WITHOUT segments (pre-v36) — stamping is impossible, so
//    two messages differing only in a (non-existent) narration translation
//    fingerprint identically. Proves the fold reads segments, and degrades
//    cleanly when they're absent. ──
const noSegA = baseMsg(); delete noSegA.segments;
const noSegB = baseMsg(); delete noSegB.segments;
check('NC_no_segments_stable_fingerprint',
  _msgFingerprint(noSegA) === _msgFingerprint(noSegB));

report();
"""


def test_seg_translation_fingerprint():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'chat_render.js'),
        body_js=_BODY,
        min_pass=6,
        label='seg-translation-fingerprint',
    )
