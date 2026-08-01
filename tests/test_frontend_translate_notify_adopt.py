"""Regression: a server-committed translation reaches the OPEN conversation
through the reliable rev-driven `conv_changed` path — no refresh, no switch.

WHY (the reported bug)
----------------------
Auto-translate is an AFTER-THE-FACT writer. ``lib/translate/commit.py``
commits ``translatedContent`` (+ per-round ``segments[].translatedText``) long
after the turn settled, then announces it TWO ways:

  1. a fire-and-forget ``translate`` push frame, and
  2. ``notify_conv_changed(conv_id, rev=<post-commit rev>)``.

Path 1 is lossy BY DESIGN — ``lib/agent_core/push.py::_deliver_frame`` drops
the frame when no client is subscribed at emit time and offers NO replay — so
path 2 (the rev bump) is the reliable half.

But the notify path's adopter, ``_verifyActiveConvFromServer``, only merged
content / thinking / toolRounds behind a "did the turn GROW?" gate (plus the
terminal-metadata fields via ``_mergeTerminalTurnFields``). A translation
commit grows NOTHING: same message count, same content, same toolRounds — only
``translatedContent`` / ``segments[].translatedText`` appear. So Case 1 was
skipped (count equal), Case 2's growth gate never fired, the verify returned
``changed === false``, and the 译文 was dropped on the floor. It surfaced only
once the user refreshed or switched conversations, where
``loadConversationMessages``' own translation merge (the working twin) ran —
exactly the forced-refresh dependency this project's sync contract forbids.

FIX
---
ONE shared pure reducer ``core/conv_reducers.js::_mergeTranslationFields(lm,
sm)`` owns the field list + the same-turn identity guard, and BOTH lanes call
it:

  * ``core/cross_tab_sync.js::_verifyActiveConvFromServer`` — across the whole
    aligned window, OUTSIDE every growth gate (this is the fix);
  * ``core/conversations.js::_mergeServerTranslations`` — the on-open lane,
    re-pointed at the shared reducer so the two can never drift apart the way
    the terminal-metadata list did before ``_mergeTerminalTurnFields``.

Whole-window rather than tail-only because switching auto-translate ON sweeps
every still-untranslated message, so several historical turns can gain a 译文
under ONE rev bump.

HARNESSES
---------
  A. bare-node: the REAL pure reducer — additive/identity-guard semantics.
  B. bare-node: the REAL ``_verifyActiveConvFromServer`` — equal count, equal
     content, ZERO growth (the exact translate-commit shape) must still adopt
     the 译文, report ``changed === true`` and repaint.
     NEUTER: strip the reducer CALL from cross_tab_sync.js on a COPY → the
     translation never lands and no repaint fires → red (proves the call, not
     some incidental path, is what carries the fix).
  C. bare-node: the on-open lane still routes through the SHARED reducer.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _run(name: str, body: str, *js_paths: str) -> str:
    harness = os.path.join(HERE, f'_xlate_notify_{name}.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(body)
    try:
        proc = subprocess.run(
            ['node', harness, *js_paths],
            capture_output=True, text=True, timeout=90,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


# ═══════════════════════════════════════════════════════════════════════
# Harness A — the REAL pure reducer
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_A = r"""
const fs = require('fs');
global.window = global;
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conv_reducers.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('A_reducer_exposed', typeof _mergeTranslationFields === 'function');

// ── deliverable adopt + display flags ──
{
  const lm = { role: 'assistant', content: 'hello world' };
  const sm = { role: 'assistant', content: 'hello world',
               translatedContent: '你好世界', _translateModel: 'kimi-k3' };
  const n = _mergeTranslationFields(lm, sm);
  check('A_deliverable_adopted', lm.translatedContent === '你好世界');
  check('A_showing_flag_set', lm._showingTranslation === true);
  check('A_done_flag_set', lm._translateDone === true);
  check('A_model_carried', lm._translateModel === 'kimi-k3');
  check('A_count_nonzero', n >= 1);
}

// ── a LOCAL translation is never clobbered ──
{
  const lm = { role: 'assistant', content: 'x', translatedContent: '本地译文' };
  const n = _mergeTranslationFields(lm, {
    role: 'assistant', content: 'x', translatedContent: '服务器译文' });
  check('A_local_translation_kept', lm.translatedContent === '本地译文');
  check('A_no_change_reported', n === 0);
}

// ── identity guard: content differs (edited/regenerated turn) → refuse ──
{
  const lm = { role: 'assistant', content: 'edited text' };
  const n = _mergeTranslationFields(lm, {
    role: 'assistant', content: 'the ORIGINAL text', translatedContent: '旧译文' });
  check('A_identity_guard_content', lm.translatedContent === undefined && n === 0);
}

// ── identity guard: endpoint lane mismatch → refuse ──
{
  const lm = { role: 'user', content: 'q', _epIteration: 1 };
  const n = _mergeTranslationFields(lm, {
    role: 'user', content: 'q', _epIteration: 2, translatedContent: 'zh' });
  check('A_identity_guard_lane', lm.translatedContent === undefined && n === 0);
}

// ── per-round narration segments ──
{
  const lm = { role: 'assistant', content: 'c', segments: [
    { type: 'text', llmRound: 0 },
    { type: 'text', llmRound: 1, translatedText: '已有译文' },
    { type: 'text', llmRound: 2, deliverable: true },
  ] };
  const sm = { role: 'assistant', content: 'c', segments: [
    { type: 'text', llmRound: 0, translatedText: '第一轮中文' },
    { type: 'text', llmRound: 1, translatedText: '不该覆盖' },
    { type: 'text', llmRound: 2, deliverable: true, translatedText: '交付物' },
  ] };
  _mergeTranslationFields(lm, sm);
  check('A_narration_adopted', lm.segments[0].translatedText === '第一轮中文');
  check('A_narration_not_clobbered', lm.segments[1].translatedText === '已有译文');
  check('A_deliverable_seg_skipped', lm.segments[2].translatedText === undefined);
}

// ── llmRound misalignment → refuse that segment ──
{
  const lm = { role: 'assistant', content: 'c',
               segments: [{ type: 'text', llmRound: 5 }] };
  _mergeTranslationFields(lm, { role: 'assistant', content: 'c',
    segments: [{ type: 'text', llmRound: 9, translatedText: 'zh' }] });
  check('A_round_misalign_skipped', lm.segments[0].translatedText === undefined);
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_translation_reducer_semantics():
    out = _run('a', _HARNESS_A, os.path.join(JS_DIR, 'core', 'conv_reducers.js'))
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'translation reducer failures:\n' + out
    assert out.count('PASS') >= 14, f'expected >=14 PASS, got:\n{out}'
    print(out)


# ═══════════════════════════════════════════════════════════════════════
# Harness B — REAL _verifyActiveConvFromServer, zero-growth translate commit
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_B = r"""
const fs = require('fs');
global.window = global;
global.addEventListener = () => {};
global.document = { addEventListener: () => {}, visibilityState: 'visible',
                    getElementById: () => null };
global.activeConvId = 'c1';
global.activeStreams = new Map();
global.debugLog = () => {};
global.config = {};
global._applySettingsToConv = () => {};
global.saveConversations = () => {};
global.ConvCache = { put: () => {} };
global.AbortSignal = { timeout: () => undefined };
let repaintCount = 0;
global.ConvView = { replaceAll: () => { repaintCount++; } };

let SERVER_BODY = null;
global.Api = { conversations: { get: async () => SERVER_BODY } };

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conv_reducers.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL core/cross_tab_sync.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The settled English turn as this tab holds it: content/thinking/toolRounds
// all FINAL, terminal metadata already present. Nothing can "grow".
function seed() {
  conversations.length = 0;
  repaintCount = 0;
  conversations.push({
    id: 'c1', title: 't', createdAt: 1000, updatedAt: 1001,
    _serverMsgCount: 3, _serverRev: 8,
    messages: [
      { role: 'user', content: 'q', timestamp: 1000 },
      { role: 'assistant', content: 'An older settled answer.', thinking: '',
        toolRounds: [], timestamp: 1001, finishReason: 'stop',
        usage: { prompt_tokens: 5 } },
      { role: 'assistant', content: 'The settled English answer.', thinking: 'th',
        toolRounds: [{ roundNum: 0 }], timestamp: 1002, finishReason: 'stop',
        usage: { prompt_tokens: 10 },
        segments: [
          { type: 'text', llmRound: 0 },
          { type: 'text', llmRound: 1, deliverable: true },
        ] },
    ],
  });
}

(async () => {
  // ══ B1. The translate commit: SAME count, SAME content, SAME toolRounds —
  //    only translatedContent + segments[].translatedText appear, rev bumped.
  //    This is byte-for-byte the shape lib/translate/commit.py produces. ══
  {
    seed();
    SERVER_BODY = {
      id: 'c1', title: 't', updatedAt: 2000, rev: 9, settings: {},
      messages: [
        { role: 'user', content: 'q', timestamp: 1000 },
        { role: 'assistant', content: 'An older settled answer.', thinking: '',
          toolRounds: [], timestamp: 1001, finishReason: 'stop',
          usage: { prompt_tokens: 5 },
          translatedContent: '一个较早的已完成回答。' },
        { role: 'assistant', content: 'The settled English answer.', thinking: 'th',
          toolRounds: [{ roundNum: 0 }], timestamp: 1002, finishReason: 'stop',
          usage: { prompt_tokens: 10 },
          translatedContent: '已完成的英文回答。',
          _translateModel: 'kimi-k3',
          segments: [
            { type: 'text', llmRound: 0, translatedText: '第一轮旁白中文' },
            { type: 'text', llmRound: 1, deliverable: true },
          ] },
      ],
    };
    const changed = await _verifyActiveConvFromServer('c1');
    const tail = conversations[0].messages[2];
    const older = conversations[0].messages[1];
    check('B1_changed_true', changed === true);
    check('B1_tail_translation_adopted', tail.translatedContent === '已完成的英文回答。');
    check('B1_tail_showing_flag', tail._showingTranslation === true);
    check('B1_tail_done_flag', tail._translateDone === true);
    check('B1_tail_model_carried', tail._translateModel === 'kimi-k3');
    check('B1_narration_adopted', tail.segments[0].translatedText === '第一轮旁白中文');
    // Whole-window: a NON-tail historical turn translated under the same rev.
    check('B1_older_turn_translation_adopted',
      older.translatedContent === '一个较早的已完成回答。');
    check('B1_repaint_fired', repaintCount >= 1);
    // Non-destructive: the English 原文 is untouched (the toggle needs it).
    check('B1_content_untouched', tail.content === 'The settled English answer.');
    check('B1_thinking_untouched', tail.thinking === 'th');
  }

  // ══ B2. No translation on the server → still a clean no-op (no phantom
  //    repaint on every rev bump). ══
  {
    seed();
    SERVER_BODY = {
      id: 'c1', title: 't', updatedAt: 2001, rev: 10, settings: {},
      messages: JSON.parse(JSON.stringify(conversations[0].messages)),
    };
    const changed = await _verifyActiveConvFromServer('c1');
    check('B2_noop_changed_false', changed === false);
    check('B2_noop_no_repaint', repaintCount === 0);
  }

  // ══ B3. Growth control: the pre-existing content adopt still works and is
  //    not disturbed by the translation pass running ahead of it. ══
  {
    seed();
    const msgs = JSON.parse(JSON.stringify(conversations[0].messages));
    msgs[2].content = 'The settled English answer, now genuinely longer.';
    SERVER_BODY = {
      id: 'c1', title: 't', updatedAt: 2002, rev: 11, settings: {}, messages: msgs,
    };
    const changed = await _verifyActiveConvFromServer('c1');
    check('B3_growth_still_adopted',
      conversations[0].messages[2].content
        === 'The settled English answer, now genuinely longer.');
    check('B3_growth_changed_true', changed === true);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_notify_path_adopts_translation_without_growth():
    out = _run(
        'b', _HARNESS_B,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'))
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'notify-path translation adopt failures:\n' + out
    assert out.count('PASS') >= 14, f'expected >=14 PASS, got:\n{out}'
    print(out)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NEUTER_reducer_call_is_load_bearing_in_notify_path(tmp_path):
    """NEUTER: strip the ``_mergeTranslationFields(...)`` CALL from
    cross_tab_sync.js on a COPY → the zero-growth translate commit is dropped
    again (the original bug), no repaint fires. Proves the fix is carried by
    that call and not by some incidental side effect. Real file untouched."""
    js = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
    with open(js, encoding='utf-8') as f:
        src = f.read()

    needle = '      _trMerged += _mergeTranslationFields(localMsgs[i], serverMsgs[i]);'
    assert src.count(needle) == 1, (
        'reducer-call fragment drifted — update the neuter target')
    neutered = src.replace(needle, '', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'cross_tab_sync_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    out = _run(
        'b_nc', _HARNESS_B,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        str(copy))
    assert 'FAIL B1_tail_translation_adopted' in out, (
        'NEUTER did not bite: the 译文 still landed without the reducer call.\n' + out)
    assert 'FAIL B1_narration_adopted' in out, (
        'NEUTER did not bite: narration still landed without the reducer call.\n' + out)
    assert 'FAIL B1_older_turn_translation_adopted' in out, out
    assert 'FAIL B1_changed_true' in out, out
    # The growth path does NOT route through this reducer — stays green.
    assert 'PASS B3_growth_still_adopted' in out, out

    with open(js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped cross_tab_sync.js'


# ═══════════════════════════════════════════════════════════════════════
# Harness C — the on-open lane routes through the SHARED reducer
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_on_open_lane_delegates_to_shared_reducer():
    """The on-open translation merge must DELEGATE, not re-implement: a second
    hand-written field list is exactly how the terminal-metadata list drifted
    across three call sites before ``_mergeTerminalTurnFields``.

    NOTE: ``_mergeServerTranslations`` moved OUT of conversations.js in
    slice 12 (pt_3879f00e sub-part 2) into core/conv_reducers.js — the
    delegation assertion anchors on the LEAF (its new home), while the
    no-inline assertion stays on conversations.js, and a third pin proves
    the on-open lane still routes through the wrapper."""
    # (1) The array wrapper lives in conv_reducers.js and delegates to the
    #     shared per-message primitive.
    with open(os.path.join(JS_DIR, 'core', 'conv_reducers.js'), encoding='utf-8') as f:
        leaf = f.read()
    assert '_mergeTranslationFields(destMsgs[i], sourceMsgs[i])' in leaf, (
        'conv_reducers.js::_mergeServerTranslations no longer delegates to '
        'the shared per-message reducer')
    # (2) conversations.js keeps NO inline implementation of the field list.
    with open(os.path.join(JS_DIR, 'core', 'conversations.js'), encoding='utf-8') as f:
        src = f.read()
    assert "lm._showingTranslation = sm._showingTranslation !== false;" not in src, (
        'inline translation field list still present in conversations.js — '
        'the shared reducer is now the single source of truth')
    # (3) The on-open lane still routes through the (now-resident-in-leaf)
    #     wrapper, not a re-implementation.
    assert '_mergeServerTranslations(' in src, (
        'conversations.js no longer calls _mergeServerTranslations — the '
        'on-open lane lost its route to the shared wrapper')
