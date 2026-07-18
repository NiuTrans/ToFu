"""F3 — a SETTLED message streams its per-round narration translation in place.

WHY
---
Before unification, the retro / on-open / frozen-OFF whole-message path
delivered nothing per-round: the settled bubble showed a bare spinner that hung,
then a whole-bubble ``_renderMsgInPlace`` outerHTML swap replaced everything at
once (the reported "hangs, then suddenly replaces" + flicker).

Now the backend streams ``partialByRound`` running frames (runtime._seg_progress)
even for the whole-message path, and the settled render stamps each round's
narration block with ``data-seg-round="L<n>"``. ``_applyPartialByRoundToSettled``
locates ``#msg-N .seg-narration[data-seg-round]`` and rewrites just that block's
markdown as each round's Chinese arrives — the settled-node analogue of the live
``_renderStreamingTranslatePreview``. So a settled bubble fills its narration in
Chinese progressively, in place, matching the live path.

Drives the REAL shipped ``_applyPartialByRoundToSettled`` + the translate push
subscriber (both from translation.js) under jsdom. NEUTER: remove the
``data-seg-round`` attribute → the painter can't target the block, proving the
key is load-bearing (without it we'd be back to the whole-bubble swap).
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let _pushHandler = null;
let _fullRerenders = 0;

// A settled assistant bubble #msg-1 whose tool panel already rendered two
// rounds' narration blocks keyed by data-seg-round (as the shipped
// _renderSegNarrationHTML / _renderTimelineBatch now emit).
const _html = '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">'
  + '<div id="msg-1" class="message">'
  + '<div class="ptool-panel"><div class="ptool-panel-body">'
  + '<div class="md-content seg-narration" data-seg-round="L0">First narration.</div>'
  + '<div class="ptool-turn" data-llm-round="L0"><div data-prn="0">tool0</div></div>'
  + '<div class="md-content seg-narration" data-seg-round="L1">Second narration.</div>'
  + '<div class="ptool-turn" data-llm-round="L1"><div data-prn="1">tool1</div></div>'
  + '</div></div></div>'
  + '</div></div></body>';

const { document, check, report } = setup({
  root: process.argv[3],
  html: _html,
  // translation.js (engine) + ui/translation_render.js (the relocated
  // _applyPartialByRoundToSettled painter, decoupling step 4).
  targets: [process.argv[2], process.argv[4]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [
      { role: 'user', content: 'q', _msgId: 'mU' },
      { role: 'assistant', content: 'The final answer.', _msgId: 'mA' },
    ] }],
    pushSubscribe: (channel, taskId, fn) => { if (channel === 'translate') _pushHandler = fn; },
    stripNoTranslateTags: (s) => s,
    renderMarkdown: (s) => s,
    saveConversations: () => {},
    _patchMessageOnServer: () => {},
    _armAutoTranslateWatchdog: () => {},
    _applyTranslationStatus: () => {},
    _renderStreamingTranslatePreview: () => false,  // no live #streaming-msg here
    isNearBottom: () => false,
    scrollToBottom: () => {},
  },
});

// The engine now requests whole-bubble repaints via the emitMessageChanged
// seam (not _renderMsgInPlace directly). translation_render.js defined the real
// one at load; override it with a COUNTING SPY so we can prove the surgical
// per-round painter avoided a whole-bubble re-render. (A 'full'/'status' kind
// would be a whole-bubble repaint; the surgical path returns before emitting.)
emitMessageChanged = window.emitMessageChanged = (convId, idx, msg, detail) => {
  if (!detail || detail.kind === 'full' || detail.kind === 'status') _fullRerenders++;
  return true;
};

if (typeof _applyPartialByRoundToSettled !== 'function') {
  check('painter_exposed', false); report(); return;
}
check('painter_exposed', true);

// ── Direct call: paint round-0 into its settled narration slot in place. ──
const ok = _applyPartialByRoundToSettled('c1', 1, { '0': '第零轮的中文。' });
check('painter_returned_true', ok === true);
const n0 = document.querySelector('#msg-1 .seg-narration[data-seg-round="L0"]');
const n1 = document.querySelector('#msg-1 .seg-narration[data-seg-round="L1"]');
check('round0_painted_zh', !!n0 && n0.innerHTML.indexOf('第零轮') >= 0);
check('round1_untouched_before_its_frame', !!n1 && n1.innerHTML.indexOf('Second narration') >= 0);

// Round-1 arrives on a later frame → painted in place, round-0 preserved.
_applyPartialByRoundToSettled('c1', 1, { '0': '第零轮的中文。', '1': '第一轮的中文。' });
check('round1_painted_zh', n1.innerHTML.indexOf('第一轮') >= 0);
check('round0_preserved', n0.innerHTML.indexOf('第零轮') >= 0);

// ── Through the REAL push subscriber: a running frame with partialByRound must
//    route to the surgical painter and NOT trigger the whole-bubble re-render. ──
check('push_subscriber_registered', typeof _pushHandler === 'function');
_fullRerenders = 0;
_pushHandler({ status: 'running', statusKind: 'in_progress', convId: 'c1',
               msgId: 'mA', field: 'translatedContent',
               partial: '第零轮的中文。\n\n第一轮的中文。',
               partialByRound: { '0': '第零轮的中文。（更新）', '1': '第一轮的中文。' } });
const n0b = document.querySelector('#msg-1 .seg-narration[data-seg-round="L0"]');
check('push_routed_to_surgical_painter', n0b.innerHTML.indexOf('更新') >= 0);
check('push_avoided_whole_bubble_rerender', _fullRerenders === 0);

// ── NEUTER: strip data-seg-round from the round-0 block → the painter can no
//    longer target it, so that round is NOT painted (would fall back to the
//    whole-bubble swap in the real flow). Proves the key is load-bearing. ──
{
  const victim = document.querySelector('#msg-1 .seg-narration[data-seg-round="L0"]');
  victim.removeAttribute('data-seg-round');
  victim.innerHTML = 'Reset EN narration.';
  const painted = _applyPartialByRoundToSettled('c1', 1, { '0': '不该出现的中文。' });
  const still = document.querySelector('#msg-1 .ptool-panel-body').innerHTML;
  check('NC_neuter_unkeyed_block_not_painted',
    still.indexOf('不该出现') < 0 && still.indexOf('Reset EN narration') >= 0);
}

report();
"""


def test_settled_perround_translate():
    run_harness(
        target_js=os.path.join(JS_DIR, 'translation.js'),
        body_js=_BODY,
        min_pass=10,
        label='settled-perround-translate',
        extra_targets=[os.path.join(JS_DIR, 'ui', 'translation_render.js')],
    )
