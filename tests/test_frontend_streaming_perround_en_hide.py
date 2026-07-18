"""Per-round English narration hide is GATED on the round's Chinese twin.

WHY (the reported bug — intermediate-round content lost under auto-translate)
----------------------------------------------------------------------------
During a live agent task with auto-translate ON, each closed round's English
narration is rendered as a `.stream-seg-en-narration[data-seg-round="L<n>"]`
sibling of its tool card by `_renderStreamRoundProse` (streaming_ui.js). The
incremental translator then paints the Chinese equivalent into a
`.stream-seg-narration[data-seg-round="L<n>"]` sibling via
`_renderStreamingTranslatePreview` (translation_render.js), and the English is
hidden to avoid a bilingual double.

The OLD hide was a GLOBAL body rule: `[data-xlate="1"] .stream-seg-en-narration
{ display:none }`. `data-xlate="1"` is set once, the instant the FIRST round's
Chinese lands — so it hid EVERY round's English, including intermediate rounds
whose Chinese was still pending / had failed / whose accumulator was reclaimed.
Those rounds then showed NOTHING: English hidden, Chinese absent. That is the
"Content of intermediate rounds is lost in the inline tool timeline" report.

The FIX makes the hide PER ROUND: a `.xlate-hidden` class is toggled onto a
round's English ONLY when its matching Chinese sibling exists. A round whose
Chinese hasn't landed keeps its English as the fallback.

This drives the REAL shipped `_renderStreamRoundProse` (via `_syncToolRoundsDOM`,
streaming_ui.js) + `_renderStreamingTranslatePreview` (translation_render.js)
under jsdom. Each assertion is paired with a neuter proving the gate is
load-bearing. Skips cleanly when node/jsdom absent.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  // streaming_ui.js (argv[2]) + translation_render.js (argv[4]).
  targets: [process.argv[2], process.argv[4]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [
      { role: 'assistant', content: 'English body.', _msgId: 'mLive' },
    ] }],
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
    renderMarkdown: (s) => '<md>' + String(s) + '</md>',
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
    _isRoundSwarm: () => false,
    _buildSwarmPanelHTML: () => '',
    _renderUnifiedToolLine: (r) => '<div class="ptool-line">' + (r.toolName || '') + '</div>',
    _renderTurnHead: () => '<div class="ptool-turn-head"></div>',
    _renderSoloRoundTag: (rno) => '<div class="ptool-turn-rno-solo">' + rno + '</div>',
    _turnLabelText: () => 'parallel',
  },
});

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');

// Two rounds, each carrying per-round English narration (as delta_reset stamps
// onto the first tool round of each llmRound batch).
const rounds = [
  { roundNum: 1, toolCallId: 'tc0', toolName: 'read_files', status: 'done',
    llmRound: 0, assistantContent: 'Round zero English narration.' },
  { roundNum: 2, toolCallId: 'tc1', toolName: 'grep_search', status: 'done',
    llmRound: 1, assistantContent: 'Round one English narration.' },
];
_syncToolRoundsDOM(toolZone, rounds);

const pbody = toolZone.querySelector('.ptool-panel-body');
function enOf(gkey) {
  return pbody.querySelector(':scope > .stream-seg-en-narration[data-seg-round="' + gkey + '"]');
}
function zhOf(gkey) {
  return pbody.querySelector(':scope > .stream-seg-narration[data-seg-round="' + gkey + '"]');
}

// ── Before any translation: BOTH rounds' English present + VISIBLE (no hide). ──
const en0 = enOf('L0'), en1 = enOf('L1');
check('en0_present', !!en0 && en0.innerHTML.indexOf('Round zero') >= 0);
check('en1_present', !!en1 && en1.innerHTML.indexOf('Round one') >= 0);
check('en0_visible_pre_xlate', !!en0 && !en0.classList.contains('xlate-hidden'));
check('en1_visible_pre_xlate', !!en1 && !en1.classList.contains('xlate-hidden'));

// ── ONLY round 0's Chinese lands (round 1 still pending / failed). ──
const painted = _renderStreamingTranslatePreview('c1', 'mLive', '第零轮。',
                    { '0': '第零轮的中文。' });
check('translate_painted', painted === true);

const zh0 = zhOf('L0'), zh1 = zhOf('L1');
check('round0_chinese_painted', !!zh0 && zh0.innerHTML.indexOf('第零轮') >= 0);
check('round1_chinese_absent', !zh1);

// ★ THE FIX: round 0's English is hidden (its Chinese twin exists), but
//   round 1's English STAYS VISIBLE (its Chinese never landed) — instead of
//   the old global rule hiding BOTH and leaving round 1 showing NOTHING.
check('round0_english_hidden', !!en0 && en0.classList.contains('xlate-hidden'));
check('round1_english_still_visible',
  !!enOf('L1') && !enOf('L1').classList.contains('xlate-hidden'));

// ── A re-sync (twUpdate coalesced re-render) must PRESERVE the per-round gate:
//    round 0 stays hidden (twin still there), round 1 stays visible. ──
_syncToolRoundsDOM(toolZone, rounds);
check('round0_hidden_survives_resync',
  !!enOf('L0') && enOf('L0').classList.contains('xlate-hidden'));
check('round1_visible_survives_resync',
  !!enOf('L1') && !enOf('L1').classList.contains('xlate-hidden'));

// ── Now round 1's Chinese ALSO lands → its English hides too. ──
_renderStreamingTranslatePreview('c1', 'mLive', '第零轮。\n\n第一轮。',
    { '0': '第零轮的中文。', '1': '第一轮的中文。' });
check('round1_english_hidden_after_its_chinese',
  !!enOf('L1') && enOf('L1').classList.contains('xlate-hidden'));

report();
"""


def test_perround_english_hide_gated_on_chinese_twin():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'translation_render.js')],
        min_pass=12,
        label='perround-en-hide',
    )


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER — prove the per-round gate is load-bearing.
#
#  Restore the OLD behaviour by force-hiding EVERY English node the instant ANY
#  Chinese lands (simulate the global `[data-xlate="1"]` rule at the JS level):
#  then round 1 — whose Chinese never landed — is ALSO hidden, so it shows
#  NOTHING. This is the exact regression the fix removes.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_NC = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2], process.argv[4]],
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [
      { role: 'assistant', content: 'x', _msgId: 'mLive' },
    ] }],
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
    renderMarkdown: (s) => '<md>' + String(s) + '</md>',
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
    _isRoundSwarm: () => false,
    _buildSwarmPanelHTML: () => '',
    _renderUnifiedToolLine: (r) => '<div class="ptool-line">' + (r.toolName || '') + '</div>',
    _renderTurnHead: () => '<div class="ptool-turn-head"></div>',
    _renderSoloRoundTag: (rno) => '<div class="ptool-turn-rno-solo">' + rno + '</div>',
    _turnLabelText: () => 'parallel',
  },
});

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');
const rounds = [
  { roundNum: 1, toolCallId: 'tc0', toolName: 'read_files', status: 'done',
    llmRound: 0, assistantContent: 'Round zero English.' },
  { roundNum: 2, toolCallId: 'tc1', toolName: 'grep_search', status: 'done',
    llmRound: 1, assistantContent: 'Round one English.' },
];
_syncToolRoundsDOM(toolZone, rounds);
_renderStreamingTranslatePreview('c1', 'mLive', '第零轮。', { '0': '第零轮的中文。' });

const pbody = toolZone.querySelector('.ptool-panel-body');
const en1 = pbody.querySelector(':scope > .stream-seg-en-narration[data-seg-round="L1"]');

// NEUTER: emulate the OLD global rule — hide EVERY English node once any
// Chinese landed (data-xlate body flag), regardless of per-round twin.
for (const el of pbody.querySelectorAll('.stream-seg-en-narration')) {
  el.classList.add('xlate-hidden');
}
// With the neuter, round 1 (no Chinese twin) is now ALSO hidden → invisible.
check('NC_round1_english_hidden_by_global_rule',
  !!en1 && en1.classList.contains('xlate-hidden'));
// And there is no Chinese twin for round 1 either → the round shows NOTHING.
const zh1 = pbody.querySelector(':scope > .stream-seg-narration[data-seg-round="L1"]');
check('NC_round1_has_no_chinese_twin', !zh1);
check('NC_round1_would_be_invisible',
  !!en1 && en1.classList.contains('xlate-hidden') && !zh1);

report();
"""


def test_neuter_global_hide_regresses_round_to_invisible():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY_NC,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'translation_render.js')],
        min_pass=3,
        label='perround-en-hide-nc',
    )


def test_css_gate_is_perround_not_global():
    """Source guard: the CSS hide keys on the per-round `.xlate-hidden` class,
    NOT the global `[data-xlate="1"]` body flag. If a refactor reinstates the
    global rule, an intermediate round's English hides before its Chinese lands
    (the bug) — so pin the per-round selector and forbid the global one."""
    css_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'static', 'styles.css')
    css = open(css_path, encoding='utf-8').read()
    assert '.stream-seg-en-narration.xlate-hidden' in css, \
        'per-round English-hide selector missing/renamed — coordinate before landing'
    assert '[data-xlate="1"] .ptool-panel-body > .stream-seg-en-narration' not in css, \
        ('the GLOBAL data-xlate English-hide rule is back — it hides intermediate '
         'rounds whose Chinese has not landed (the reported bug). Use the '
         'per-round .xlate-hidden class instead.')
