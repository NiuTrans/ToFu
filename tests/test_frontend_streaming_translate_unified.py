"""Streaming auto-translate render UNIFICATION (2026-07-07).

WHY
---
During an agent task with auto-translate ON, the English reply streamed into the
``[data-zone="content"]`` zone (interleaved with the tool timeline) while the
incremental translator's Chinese-so-far was painted into a SEPARATE
``[data-zone="translatePreview"]`` zone pinned at the BOTTOM of the bubble. So
mid-task the user saw the full English body and then a growing Chinese block
dumped underneath ("one big block at the very bottom"), plus a visible layout
jump at finalize when the bubble flipped to the settled bilingual structure
(Chinese primary, English in the collapsed 原文/译文 toggle).

The fix unifies the streaming render with the SETTLED render:
  • A new ``[data-zone="translatedPrimary"]`` slot sits ABOVE the content zone.
    ``_renderStreamingTranslatePreview`` paints the Chinese-so-far there as the
    PRIMARY body (a bare ``.md-content``, no spinner chrome) — its natural
    interleaved reading position, mirroring the settled view.
  • The still-untranslated English tail keeps streaming into the content zone,
    which is DEMOTED (``stream-content-demoted``) to a quiet live tail that
    swaps to Chinese in place as each round's segment lands.
  • At finalize the worker's ``started`` frame stamps ``data-xlate-final`` so the
    closing round's English tail is HIDDEN (``stream-content-tail-hidden``) and
    never doubles with its just-arriving Chinese.

Because the streaming bubble already has the settled structure, finalize is a
visual no-op (zero layout jump).

This drives the REAL shipped ``_ensureStreamZones`` / ``_renderStreamingTranslate
Preview`` / ``updateStreamingUI`` (+ the push subscriber registered by
translation.js) under jsdom, stubbing only the render/network seams. Each
assertion is paired with a NEUTER that disables the mechanism and proves the
guard is load-bearing.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

// Capture the translate push subscriber that translation.js registers at eval.
let _pushHandler = null;

const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  // streaming_ui.js (argv[2]) then translation.js (argv[4]).
  targets: [process.argv[2], process.argv[4]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [
      { role: 'assistant', content: 'English body.', _msgId: 'mLive' },
    ] }],
    pushSubscribe: (channel, taskId, fn) => { if (channel === 'translate') _pushHandler = fn; },
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
    // updateStreamingUI helper seams (defined in other bundle files) — no-ops.
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
  },
});

const body = document.getElementById('streaming-body');
function zoneIdx(name) {
  const z = body.querySelector('[data-zone="' + name + '"]');
  if (!z) return -1;
  return Array.prototype.indexOf.call(body.children, z);
}

// ── Zone layout: translatedPrimary sits ABOVE content (both above the legacy
//    translatePreview). This is the structural core of the fix. ──
_ensureStreamZones(body);
const iPrimary = zoneIdx('translatedPrimary');
const iContent = zoneIdx('content');
const iLegacy  = zoneIdx('translatePreview');
check('translatedPrimary_zone_exists', iPrimary >= 0);
check('translatedPrimary_above_content', iPrimary >= 0 && iContent >= 0 && iPrimary < iContent);
check('legacy_translatePreview_is_last', iLegacy > iContent);

// ── Live render: Chinese lands in translatedPrimary (ABOVE content), NOT a
//    bottom block; content zone is demoted; body flags data-xlate. ──
const painted = _renderStreamingTranslatePreview('c1', 'mLive', '第一段。第二段。');
const primaryZone = body.querySelector('[data-zone="translatedPrimary"]');
const primaryMd = primaryZone && primaryZone.querySelector('.md-content');
check('render_returned_true', painted === true);
check('chinese_in_primary_md', !!primaryMd && primaryMd.innerHTML.indexOf('第一段') >= 0);
// The rendered Chinese node is ABOVE the content zone (not appended at bottom).
check('chinese_node_above_content',
  Array.prototype.indexOf.call(body.children, primaryZone)
    < Array.prototype.indexOf.call(body.children, body.querySelector('[data-zone="content"]')));
check('body_flagged_xlate', body.getAttribute('data-xlate') === '1');
check('content_zone_demoted',
  body.querySelector('[data-zone="content"]').classList.contains('stream-content-demoted'));
// Settled-structure match: primary uses a bare .md-content (same tag the
// settled assistant body uses), NOT the old spinner-chrome preview shell.
check('primary_has_no_spinner_chrome',
  !!primaryMd && !primaryZone.querySelector('.translate-spinner'));

// ── NEUTER 1 (in-place swap disabled → regresses to bottom block) ──
// Remove the translatedPrimary zone so _renderStreamingTranslatePreview falls
// back to the legacy bottom translatePreview zone. The Chinese then sits BELOW
// content — exactly the "one big block at the very bottom" bug. This proves the
// above-content placement is load-bearing, not incidental.
{
  const fresh = document.createElement('div');
  fresh.id = 'streaming-body';
  body.parentNode.replaceChild(fresh, body);
  // rebuild zones, then delete translatedPrimary to force the fallback
  _ensureStreamZones(fresh);
  const tp = fresh.querySelector('[data-zone="translatedPrimary"]');
  if (tp) tp.remove();
  // bust the module zone cache by clearing #streaming-msg id lookup? cache keys
  // on body identity — a NEW body element invalidates it automatically.
  _renderStreamingTranslatePreview('c1', 'mLive', '掉到底部的中文。');
  const legacyZone = fresh.querySelector('[data-zone="translatePreview"]');
  const legacyMd = legacyZone && legacyZone.querySelector('.md-content');
  const contentIdx = Array.prototype.indexOf.call(fresh.children, fresh.querySelector('[data-zone="content"]'));
  const legacyIdx  = Array.prototype.indexOf.call(fresh.children, legacyZone);
  check('NC1_neuter_regresses_to_bottom_block',
    !!legacyMd && legacyMd.innerHTML.indexOf('掉到底部') >= 0 && legacyIdx > contentIdx);
  // restore original body for the remaining tests
  fresh.parentNode.replaceChild(body, fresh);
}

// ── Finalize is a no-op via updateStreamingUI reading data-xlate flags ──
// With data-xlate set, updateStreamingUI keeps the content zone demoted; with
// data-xlate-final set it HIDES the tail (the closing-round English dedup).
body.setAttribute('data-xlate', '1');
body.removeAttribute('data-xlate-final');
updateStreamingUI({ content: 'English tail sentence.', toolRounds: [] });
check('demotion_survives_restream',
  body.querySelector('[data-zone="content"]').classList.contains('stream-content-demoted'));
check('tail_not_hidden_before_finalize',
  !body.querySelector('[data-zone="content"]').classList.contains('stream-content-tail-hidden'));

// finalize 'started' frame via the REAL push subscriber → stamps data-xlate-final
check('push_subscriber_registered', typeof _pushHandler === 'function');
if (typeof _pushHandler === 'function') {
  _pushHandler({ status: 'running', statusKind: 'started', convId: 'c1',
                 msgId: 'mLive', field: 'translatedContent' });
}
check('finalize_frame_stamps_xlate_final', body.getAttribute('data-xlate-final') === '1');
updateStreamingUI({ content: 'English tail sentence.', toolRounds: [] });
check('tail_hidden_after_finalize',
  body.querySelector('[data-zone="content"]').classList.contains('stream-content-tail-hidden'));

// ── NEUTER 2 (finalize-structure match disabled → layout delta) ──
// Clear the finalize flag: the tail is NO LONGER hidden, so the closing round's
// English stays visible (doubling with the Chinese) — the layout delta the
// unification removes. Proves the data-xlate-final hide is load-bearing.
body.removeAttribute('data-xlate-final');
updateStreamingUI({ content: 'English tail sentence.', toolRounds: [] });
check('NC2_neuter_leaves_tail_visible',
  !body.querySelector('[data-zone="content"]').classList.contains('stream-content-tail-hidden'));

report();
"""


def test_streaming_translate_render_unification():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'translation.js')],
        min_pass=15,
        label='streaming-translate-unified',
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Per-round INTERLEAVE (streaming half) — the reported bug fix.
#
#  Before: every round's Chinese piled up as ONE blob in translatedPrimary,
#  BELOW the entire tool panel ("content stuck together at the bottom"). Now
#  the push frame carries `partialByRound` = {String(round_num): 中文}, and
#  _renderStreamingTranslatePreview routes each round's Chinese to a SIBLING
#  block in the panel body, immediately BEFORE its matching
#  .ptool-turn[data-llm-round="L<n>"] card (NOT nested inside it) — mirroring
#  the settled renderSegmentTimelineHTML, which keeps prose as flat siblings of
#  the tool card. round_num ≡ llmRound. Rounds whose .ptool-turn isn't in the
#  DOM yet fall back to the blob (graceful degrade). NC: drop byRound →
#  everything falls to the blob.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_INTERLEAVE = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let _pushHandler = null;
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2], process.argv[4]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [
      { role: 'assistant', content: 'English body.', _msgId: 'mLive' },
    ] }],
    pushSubscribe: (channel, taskId, fn) => { if (channel === 'translate') _pushHandler = fn; },
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
  },
});

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
// Simulate the live tool panel: two rounds, each its own .ptool-turn group
// with one tool slot (as _syncToolRoundsDOM builds them).
const toolZone = body.querySelector('[data-zone="tool"]');
toolZone.innerHTML =
  '<div class="ptool-panel"><div class="ptool-panel-body">'
  + '<div class="ptool-turn" data-llm-round="L0"><div data-prn="0" class="tool-slot-0">tool0</div></div>'
  + '<div class="ptool-turn" data-llm-round="L1"><div data-prn="1" class="tool-slot-1">tool1</div></div>'
  + '</div></div>';

// ── Route per-round Chinese into the matching groups. ──
const painted = _renderStreamingTranslatePreview('c1', 'mLive', '第零轮。\n\n第一轮。',
                    { '0': '第零轮的中文narration。', '1': '第一轮的中文narration。' });
check('interleave_render_returned_true', painted === true);

const pbody = body.querySelector('.ptool-panel-body');
const g0 = body.querySelector('.ptool-turn[data-llm-round="L0"]');
const g1 = body.querySelector('.ptool-turn[data-llm-round="L1"]');
// ★ OWNER DIRECTIVE (2026-07-08): the Chinese narration is a SIBLING of the
//   tool card in the panel body (located by data-seg-round), NOT nested inside
//   .ptool-turn — the same "don't box the three together" fix as the English
//   path. Mirrors the settled render (flat siblings of the card).
const n0 = pbody && pbody.querySelector(':scope > .stream-seg-narration[data-seg-round="L0"]');
const n1 = pbody && pbody.querySelector(':scope > .stream-seg-narration[data-seg-round="L1"]');

// ROUND-0 Chinese lands next to round-0's card (adjacency — the whole point).
check('round0_chinese_for_group0', !!n0 && n0.innerHTML.indexOf('第零轮') >= 0);
check('round1_chinese_for_group1', !!n1 && n1.innerHTML.indexOf('第一轮') >= 0);
// NOT NESTED: the narration must not be a descendant of any .ptool-turn.
check('narration_not_inside_ptool_turn', !!n0 && !n0.closest('.ptool-turn'));
check('narration_parent_is_panel_body', !!n0 && n0.parentElement === pbody);
// NO cross-contamination: round-0's text is not in round-1's block.
check('no_crosstalk_g1_lacks_r0', !!n1 && n1.innerHTML.indexOf('第零轮') < 0);
// Narration sits immediately ABOVE the round's tool card (settled-render parity).
check('narration_above_its_card_g0',
  !!n0 && Array.prototype.indexOf.call(pbody.children, n0)
        < Array.prototype.indexOf.call(pbody.children, g0));

// The translatedPrimary BLOB must NOT re-show the routed rounds (else the
// Chinese doubles: once interleaved, once at the bottom = the original bug).
const primaryMd = body.querySelector('[data-zone="translatedPrimary"] .md-content');
check('blob_excludes_routed_round0', !primaryMd || primaryMd.innerHTML.indexOf('第零轮') < 0);
check('blob_excludes_routed_round1', !primaryMd || primaryMd.innerHTML.indexOf('第一轮') < 0);

// ── Graceful degrade: a round with NO .ptool-turn in the DOM yet falls to
//    the blob rather than being dropped. ──
const painted2 = _renderStreamingTranslatePreview('c1', 'mLive',
                    '第零轮的中文narration。\n\n第一轮的中文narration。\n\n第二轮还没渲染工具。',
                    { '0': '第零轮的中文narration。', '1': '第一轮的中文narration。',
                      '2': '第二轮还没渲染工具。' });
const primaryMd2 = body.querySelector('[data-zone="translatedPrimary"] .md-content');
check('unrendered_round_falls_to_blob',
  !!primaryMd2 && primaryMd2.innerHTML.indexOf('第二轮') >= 0);
check('blob_still_excludes_routed',
  !!primaryMd2 && primaryMd2.innerHTML.indexOf('第零轮') < 0);

// ── NEUTER: drop byRound entirely → the routing can't happen, so ALL the
//    Chinese collapses into the bottom blob (the original "wall at the bottom"
//    bug) and NOTHING lands in the per-round groups. Proves the byRound
//    routing is load-bearing, not incidental. ──
{
  const fresh = document.createElement('div');
  fresh.id = 'streaming-body';
  body.parentNode.replaceChild(fresh, body);
  _ensureStreamZones(fresh);
  const tz = fresh.querySelector('[data-zone="tool"]');
  tz.innerHTML =
    '<div class="ptool-panel"><div class="ptool-panel-body">'
    + '<div class="ptool-turn" data-llm-round="L0"><div data-prn="0">tool0</div></div>'
    + '</div></div>';
  // Call WITHOUT byRound (old backend / neutered routing).
  _renderStreamingTranslatePreview('c1', 'mLive', '第零轮的中文narration。');
  const g0n = fresh.querySelector('.ptool-turn[data-llm-round="L0"]');
  const nNeuter = g0n && g0n.querySelector(':scope > .stream-seg-narration');
  const blobNeuter = fresh.querySelector('[data-zone="translatedPrimary"] .md-content');
  check('NC_neuter_no_narration_in_group', !nNeuter);
  check('NC_neuter_chinese_falls_to_blob',
    !!blobNeuter && blobNeuter.innerHTML.indexOf('第零轮') >= 0);
}

report();
"""


def test_streaming_translate_per_round_interleave():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY_INTERLEAVE,
        extra_targets=[os.path.join(JS_DIR, 'translation.js')],
        min_pass=11,
        label='streaming-translate-interleave',
    )
