"""tests/test_frontend_translation_freshness.py — regression for the
"reasoning reappears but the per-round narration translation is lost on reopen"
report.

WHY
---
Server-side auto-translate commits AFTER a turn settles: it stamps
``translatedContent`` on the deliverable AND ``segments[].translatedText`` on
each per-round narration segment (lib/translate/commit.py
_stamp_segment_translations), keyed by llmRound. The interleaved timeline
renders each round's Chinese from ``segment.translatedText`` when present
(static/js/ui/tool_rounds.js).

But the IndexedDB conversation cache can hold a copy captured BEFORE the
translate commit ran (or a live-stream copy that never stamped it). On reopen,
``loadConversationMessages`` decided freshness with:

    cacheIsStale = !cacheHit
        || serverMsgs.length !== conv.messages.length   // same count
        || serverUpdatedAt > _cachedUpdatedAt           // a later English PUT
                                                        //   can re-stamp cachedAt
        || _serverHasSegmentsLocalLacks(...)            // local HAS segments

— all three miss the "server segment has translatedText, local lacks it" case,
so the stale ENGLISH cache is judged FRESH and kept. Even the cache-FRESH
else-branch's ``_mergeServerTranslations`` only merged the deliverable
(``translatedContent``) and never the per-round ``segments[].translatedText`` —
so the narration above every tool batch renders English while the deliverable
is Chinese. That is the reported loss.

THE FIX (static/js/core/conversations.js)
------------------------------------------
1. ``_serverHasTranslationLocalLacks(serverMsgs, localMsgs)`` — symmetric to
   the segments backstop; returns true when the server carries a translation
   (deliverable OR per-round ``segments[].translatedText``) the aligned local
   copy lacks. Added as a disjunct to ``cacheIsStale``.
2. ``_mergeServerTranslations`` extended to ALSO merge
   ``segments[].translatedText`` (and the ``_translatePartialByRound`` sidecar)
   positionally within an identity-matched message.

This harness eval's the REAL shipped functions sliced verbatim from
conversations.js — it bites the actual logic, not a copy.

NEUTER (proven here by monkeypatching the source before eval):
  • Strip the segment-gap branch from ``_serverHasTranslationLocalLacks`` (only
    keep the translatedContent check) → the narration-only staleness case is
    missed and ``narration_gap_is_stale`` FAILS.
  • Strip the segment-merge branch from ``_mergeServerTranslations`` → the
    per-round Chinese is not merged and ``merge_stamps_segment_translatedText``
    FAILS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests._conv_bundle_sources import sources_defining

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _read_src(symbol: str) -> str:
    """Source of the bundled file defining *symbol*.

    Located by SYMBOL, not by path: the predicate moved out of
    core/conversations.js in pt_3879f00e slice 3 while the merge closure stayed,
    so a single hard-coded path cannot serve both (that is what broke this
    suite). Resolving each independently means a further slice re-points itself.
    """
    return open(sources_defining(symbol)[0], encoding='utf-8').read()


def _slice_predicate(src: str) -> str:
    """Slice the top-level _serverHasTranslationLocalLacks function verbatim."""
    m = re.search(
        r'function _serverHasTranslationLocalLacks\(serverMsgs, localMsgs\) \{.*?\n\}\n',
        src, re.DOTALL,
    )
    if not m:
        raise AssertionError(
            '_serverHasTranslationLocalLacks not found in the bundled file that '
            'declares it — the implementation may have been removed (a real '
            'regression) or its shape changed; re-point this slice.')
    return m.group(0)


def _slice_merge(src: str) -> str:
    """Slice the _mergeServerTranslations arrow-const closure verbatim.

    It is a local `const _mergeServerTranslations = (sourceMsgs, destMsgs) => {
    ... };` — extract it and eval as a standalone const so the test bites the
    real shipped merge logic.
    """
    m = re.search(
        r'const _mergeServerTranslations = \(sourceMsgs, destMsgs\) => \{.*?\n    \};',
        src, re.DOTALL,
    )
    if not m:
        raise AssertionError(
            '_mergeServerTranslations not found in the bundled file that '
            'declares loadConversationMessages — removed or reshaped; '
            're-point this slice.')
    return m.group(0)


def _fn_span(src: str, name: str) -> str:
    """Full text of a top-level ``function <name>(...) {...}``, brace-balanced."""
    m = re.search(r'^function\s+' + re.escape(name) + r'\s*\(', src, re.M)
    assert m, f'{name}() not found — implementation removed or renamed'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces while slicing {name}()')


def _merge_chain(neuter_segment_branch: bool = False) -> str:
    """The FULL shipped merge chain: the closure PLUS the reducer it delegates to.

    The per-segment narration merge used to live INLINE in the closure; it now
    lives in ``core/conv_reducers.js::_mergeTranslationFields`` (documented
    there as "THE single source of truth"), which the closure calls per aligned
    index. Slicing only the closure therefore left this guard asserting a branch
    that no longer exists in it — charter's THIRD failure mode (a guard testing
    code that moved out from under it). Delivering both halves makes the guard
    drive the REAL end-to-end merge.

    *neuter_segment_branch* excises the segment loop from the REDUCER (its real
    home today), which is what the NEUTER must target now.
    """
    reducer = _fn_span(_read_src('_mergeTranslationFields'), '_mergeTranslationFields')
    if neuter_segment_branch:
        neutered = re.sub(
            r'  if \(Array\.isArray\(sm\.segments\) && Array\.isArray\(lm\.segments\)\) \{.*?\n  \}\n',
            '', reducer, count=1, flags=re.DOTALL)
        assert neutered != reducer, (
            'NEUTER did not modify _mergeTranslationFields — the segment loop '
            'moved again; re-point the marker')
        reducer = neutered
    return reducer + '\n' + _slice_merge(_read_src('_mergeServerTranslations'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// argv[2] = predicate slice, argv[3] = merge slice
eval(fs.readFileSync(process.argv[2], 'utf8'));  // _serverHasTranslationLocalLacks (real/neutered)
eval(fs.readFileSync(process.argv[3], 'utf8'));  // _mergeServerTranslations (real/neutered)

check('predicate_exposed', typeof _serverHasTranslationLocalLacks === 'function');
check('merge_exposed', typeof _mergeServerTranslations === 'function');

// ── Fixtures mirroring the real conv (mroorksis7zmik) shape ──
// An assistant turn with two tool rounds' narration segments. Server has the
// Chinese narration (translatedText); the cached local copy has only English.
function mkAssistant(withTranslatedText, withTranslatedContent) {
  return {
    role: 'assistant',
    content: 'DELIVERABLE',
    segments: [
      { type: 'thinking', llmRound: 0, text: 'reasoning A' },
      { type: 'text', llmRound: 0, text: "I'll look into it.",
        translatedText: withTranslatedText ? '我会调查一下。' : undefined },
      { type: 'tool_use', llmRound: 0 },
      { type: 'text', llmRound: 1, text: 'Let me read the template.',
        translatedText: withTranslatedText ? '让我读一下模板。' : undefined },
      { type: 'text', deliverable: true, terminal: true, text: 'DELIVERABLE',
        translatedText: withTranslatedContent ? '交付物' : undefined },
    ],
    translatedContent: withTranslatedContent ? '交付物' : undefined,
  };
}
const userMsg = { role: 'user', content: 'hi' };

// ══ 1. Staleness predicate: narration-only gap must be STALE ══
{
  const server = [userMsg, mkAssistant(true, false)];   // server has zh narration
  const local  = [userMsg, mkAssistant(false, false)];  // cache English-only
  check('narration_gap_is_stale', _serverHasTranslationLocalLacks(server, local) === true);
}

// ══ 2. Staleness predicate: deliverable-only gap must be STALE ══
{
  const server = [userMsg, mkAssistant(false, true)];
  const local  = [userMsg, mkAssistant(false, false)];
  check('deliverable_gap_is_stale', _serverHasTranslationLocalLacks(server, local) === true);
}

// ══ 3. Staleness predicate: fully-translated local → NOT stale (no needless overwrite) ══
{
  const server = [userMsg, mkAssistant(true, true)];
  const local  = [userMsg, mkAssistant(true, true)];
  check('fully_translated_not_stale', _serverHasTranslationLocalLacks(server, local) === false);
}

// ══ 4. Staleness predicate: content mismatch (edited turn) → NOT stale (identity guard) ══
{
  const server = [userMsg, mkAssistant(true, false)];
  const local  = [userMsg, mkAssistant(false, false)];
  local[1].content = 'EDITED DIFFERENT';   // aligned turn no longer identical
  check('content_mismatch_not_stale', _serverHasTranslationLocalLacks(server, local) === false);
}

// ══ 5. Merge: per-round segments[].translatedText is copied in ══
{
  const server = [userMsg, mkAssistant(true, true)];
  const local  = [userMsg, mkAssistant(false, false)];
  const merged = _mergeServerTranslations(server, local);
  const lm = local[1];
  const seg0 = lm.segments[1], seg1 = lm.segments[3];
  check('merge_returns_positive', merged > 0);
  check('merge_stamps_segment_translatedText',
        seg0.translatedText === '我会调查一下。' && seg1.translatedText === '让我读一下模板。');
  check('merge_stamps_deliverable', lm.translatedContent === '交付物');
}

// ══ 6. Merge: does NOT overwrite an existing local translation ══
{
  const server = [userMsg, mkAssistant(true, true)];
  const local  = [userMsg, mkAssistant(false, false)];
  local[1].segments[1].translatedText = 'ALREADY-LOCAL';
  _mergeServerTranslations(server, local);
  check('merge_preserves_existing_local', local[1].segments[1].translatedText === 'ALREADY-LOCAL');
}

console.log(out.join('\n'));
"""


def _run(predicate_js: str, merge_js: str) -> str:
    pred_f = os.path.join(HERE, '_tf_pred.js')
    merge_f = os.path.join(HERE, '_tf_merge.js')
    harness = os.path.join(HERE, '_tf_harness.js')
    with open(pred_f, 'w', encoding='utf-8') as f:
        f.write(predicate_js + "\nif (typeof window!=='undefined') window._serverHasTranslationLocalLacks=_serverHasTranslationLocalLacks;\n")
    with open(merge_f, 'w', encoding='utf-8') as f:
        f.write(merge_js + "\nif (typeof window!=='undefined') window._mergeServerTranslations=_mergeServerTranslations;\n")
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, pred_f, merge_f],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (pred_f, merge_f, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_translation_freshness_and_merge():
    output = _run(_slice_predicate(_read_src('_serverHasTranslationLocalLacks')),
                  _merge_chain())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'translation-freshness failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NEUTER_predicate_without_segment_branch_misses_narration_gap():
    """Strip the per-round segment gap check from the predicate → the
    narration-only staleness case is missed (proves that branch is load-bearing)."""
    pred = _slice_predicate(_read_src('_serverHasTranslationLocalLacks'))
    # Excise the `if (Array.isArray(sm.segments) ...` narration block.
    neutered = re.sub(
        r'    // Per-round narration gap.*?\n    \}\n(?=  \}\n  return false;)',
        '', pred, flags=re.DOTALL,
    )
    assert neutered != pred, 'NEUTER regex did not modify the predicate — update the marker'
    output = _run(neutered, _merge_chain())
    # With the branch gone, the narration-only gap is NOT flagged stale.
    assert 'FAIL narration_gap_is_stale' in output, (
        'expected narration_gap_is_stale to FAIL under NEUTER, got:\n' + output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NEUTER_merge_without_segment_branch_skips_narration():
    """Strip the segment-merge branch from the REDUCER
    (core/conv_reducers.js::_mergeTranslationFields — its single source of
    truth) → per-round translatedText is not merged, proving that branch is
    load-bearing."""
    output = _run(_slice_predicate(_read_src('_serverHasTranslationLocalLacks')),
                  _merge_chain(neuter_segment_branch=True))
    assert 'FAIL merge_stamps_segment_translatedText' in output, (
        'expected merge_stamps_segment_translatedText to FAIL under NEUTER, got:\n' + output)
