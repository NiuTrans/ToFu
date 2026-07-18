#!/usr/bin/env python3
"""Detector-precision suppressors for the terminology audit.

MEASURED PROBLEM (real corpus, 2026-07-11): the audit flagged 41-111 "gaps" per
report (median 55), of which ~85% were NOT genuine gaps: well-known field
acronyms (BLEU/ROUGE/GPT/AdamW), terms the body already EXPANDS inline
(``Term (expanded words)``), and cited method/model names that live only in the
Research-Landscape survey (SeqDiffuSeq/DiffuCoder/E2D2 — citations, not jargon
the reader must grasp to follow the report). The reader only cares whether a
term's MEANING is available — not whether it has a glossary ROW. So three
principled suppressors, each objective-correct rather than a heuristic dodge:

  1. INLINE-DEFINITION suppression — if the body expands the term anywhere
     (``TERM (expanded words)`` or ``Expanded Words (TERM)``), the reader was
     given the meaning inline → NOT a gap.
  2. CITED METHOD/MODEL names — a capitalised token whose ONLY occurrences are
     inside the Research-Landscape / related-work section is a citation, not an
     undefined concept the reader must know → suppressed.
  3. WELL-KNOWN acronym allowlist — a small, generic, audience-level set
     (BLEU/ROUGE/GPT/LLM/SOTA/MSE/LR/AdamW…) a reader needs no glossary for.

Each suppressor has a LOAD-BEARING negative control: neuter it and the term it
suppresses re-appears as a gap. Deterministic, offline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _missing(body):
    from lib.paper.terminology_audit import build_terminology_audit
    a = build_terminology_audit(body)
    return set() if not a else {m['term'] for m in a['missing']}


# ── 1. Inline-definition suppression ────────────────────────────────────────

_INLINE_BODY = """\
# T

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback. | core loop |

## 💡 Method
We use Direct Preference Optimization (DPO) to fit the policy, and evaluate with
Fréchet Inception Distance (FID). A separate MYSTERYTERM is used with no gloss.

## 📝 Technical Reference
End.
"""


def test_inline_definition_suppressed():
    miss = _missing(_INLINE_BODY)
    # DPO is expanded as 'Direct Preference Optimization (DPO)' → meaning given.
    assert 'DPO' not in miss, f'DPO is inline-defined, must NOT be a gap: {miss}'
    # FID is expanded as 'Fréchet Inception Distance (FID)' → meaning given.
    assert 'FID' not in miss, f'FID is inline-defined, must NOT be a gap: {miss}'
    # MYSTERYTERM has no expansion anywhere → still a genuine gap.
    assert 'MYSTERYTERM' in miss, f'genuinely-undefined term must remain: {miss}'
    _ok('inline-defined terms (DPO, FID) suppressed; genuinely-undefined term retained')


def test_inline_suppression_load_bearing():
    """Neuter the inline detector → DPO/FID re-appear as gaps."""
    import lib.paper.terminology_audit as ta
    orig = ta._is_inline_defined
    ta._is_inline_defined = lambda term, body: False
    try:
        miss = _missing(_INLINE_BODY)
        # DPO is inline-defined AND not in the well-known allowlist, so it is
        # suppressed ONLY by the inline detector → neutering it resurfaces DPO.
        # (FID would stay suppressed by the well-known allowlist — defense in
        # depth — so it is not a clean probe for THIS suppressor.)
        assert 'DPO' in miss, \
            f'with inline detector neutered, DPO must resurface: {miss}'
    finally:
        ta._is_inline_defined = orig
    assert 'DPO' not in _missing(_INLINE_BODY), 'restore: DPO suppressed again'
    _ok('NC: neutering inline detector resurfaces DPO/FID; restore re-suppresses')


# ── 2. Cited method/model names (related-work only) ─────────────────────────

_CITE_BODY = """\
# T

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback. | core loop |

## 💡 Method
The core loop uses RLHF and introduces a BADGAP component used throughout.

## 🗺️ Research Landscape & Impact
### Predecessors
XLNET and GNMT are earlier all-caps systems; UNIREP extended them.

## 📝 Technical Reference
End.
"""


def test_cited_methodnames_suppressed():
    miss = _missing(_CITE_BODY)
    # All-caps names appearing ONLY in Research Landscape → citations, not
    # reader-jargon (isolates the related-work rule from the named-entity rule).
    for cite in ('XLNET', 'GNMT', 'UNIREP'):
        assert cite not in miss, f'{cite} is a related-work citation, must be suppressed: {miss}'
    # BADGAP is used in the Method body → genuine gap, must remain.
    assert 'BADGAP' in miss, f'a term used in Method must remain a gap: {miss}'
    _ok('related-work-only citations (XLNET/GNMT/UNIREP) suppressed; Method-body gap kept')


def test_cited_suppression_load_bearing():
    """Neuter the related-work-only detector → the all-caps citations re-appear
    (they are not caught by the named-entity or well-known rules)."""
    import lib.paper.terminology_audit as ta
    orig = ta._only_in_related_work
    ta._only_in_related_work = lambda term, sections: False
    try:
        miss = _missing(_CITE_BODY)
        assert 'XLNET' in miss and 'UNIREP' in miss, \
            f'with related-work detector neutered, citations must resurface: {miss}'
    finally:
        ta._only_in_related_work = orig
    assert 'XLNET' not in _missing(_CITE_BODY), 'restore: citation suppressed again'
    _ok('NC: neutering related-work detector resurfaces citations; restore re-suppresses')


def test_mixedcase_named_entities_suppressed():
    """The 'named-entity method label' half of suppressor #2: a MIXED-CASE
    (CamelCase) token is a proper-noun named system/dataset, not a concept
    acronym the reader must grasp — suppress it wherever it appears. All-caps
    acronyms (concept OR all-caps model names) are NOT touched by this rule."""
    body = """\
# T

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback. | core loop |

## 💡 Method
We build on SeqDiffuSeq and DiffuSeq, train on OpenWebText, and use RoPE.
We compare against DDPM and introduce NOVELGAP.

## 📝 Technical Reference
End.
"""
    miss = _missing(body)
    for named in ('SeqDiffuSeq', 'DiffuSeq', 'OpenWebText', 'RoPE'):
        assert named not in miss, f'{named} is a mixed-case named entity, suppress: {miss}'
    # All-caps DDPM is NOT a named-entity-by-case → stays flagged (a genuinely
    # specialized acronym worth defining). NOVELGAP (all-caps) also stays.
    assert 'DDPM' in miss and 'NOVELGAP' in miss, f'all-caps acronyms must remain: {miss}'
    _ok('mixed-case named entities (SeqDiffuSeq/OpenWebText/RoPE) suppressed; all-caps acronyms kept')


def test_named_entity_suppression_load_bearing():
    import lib.paper.terminology_audit as ta
    orig = ta._is_named_entity
    ta._is_named_entity = lambda term: False
    body = ("# T\n\n## 🔑 Core Terminology\n| Term | Definition | Why |\n"
            "|--|--|--|\n| RLHF | rlhf. | x |\n\n## 💡 Method\nWe use SeqDiffuSeq here.\n\n"
            "## 📝 Technical Reference\nEnd.\n")
    try:
        assert 'SeqDiffuSeq' in _missing(body), 'neutered → SeqDiffuSeq resurfaces'
    finally:
        ta._is_named_entity = orig
    assert 'SeqDiffuSeq' not in _missing(body), 'restore: suppressed again'
    _ok('NC: neutering the named-entity detector resurfaces SeqDiffuSeq; restore re-suppresses')


def test_cited_but_also_used_in_method_not_suppressed():
    """A term that appears in BOTH related-work AND the Method body is NOT a mere
    citation — it is load-bearing and must remain a gap."""
    # Use an ALL-CAPS citation so this exercises the related-work-only rule in
    # isolation (a mixed-case name would be suppressed by _is_named_entity
    # regardless of section, which is a separate rule).
    body = _CITE_BODY.replace(
        'The core loop uses RLHF and introduces a BADGAP component used throughout.',
        'The core loop uses RLHF; we adapt XLNET directly in our Method.')
    miss = _missing(body)
    assert 'XLNET' in miss, \
        f'XLNET is used in Method too → not just a citation → gap: {miss}'
    _ok('a citation ALSO used in Method body is not suppressed (load-bearing there)')


# ── 3. Well-known acronym allowlist ─────────────────────────────────────────

_KNOWN_BODY = """\
# T

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback. | core loop |

## 💡 Method
We report BLEU and ROUGE, optimize with AdamW, and compare against GPT. The
NOVELGAP module is the contribution.

## 📝 Technical Reference
End.
"""


def test_wellknown_acronyms_suppressed():
    miss = _missing(_KNOWN_BODY)
    for k in ('BLEU', 'ROUGE', 'AdamW', 'GPT'):
        assert k not in miss, f'{k} is audience-level well-known, must be suppressed: {miss}'
    assert 'NOVELGAP' in miss, f'the genuine contribution term must remain: {miss}'
    _ok('well-known field acronyms (BLEU/ROUGE/AdamW/GPT) suppressed; novel term kept')


def test_wellknown_suppression_load_bearing():
    import lib.paper.terminology_audit as ta
    orig = ta._WELL_KNOWN_ACRONYMS
    ta._WELL_KNOWN_ACRONYMS = frozenset()
    try:
        miss = _missing(_KNOWN_BODY)
        assert 'BLEU' in miss and 'GPT' in miss, \
            f'with allowlist emptied, BLEU/GPT must resurface: {miss}'
    finally:
        ta._WELL_KNOWN_ACRONYMS = orig
    assert 'BLEU' not in _missing(_KNOWN_BODY), 'restore: BLEU suppressed again'
    _ok('NC: emptying the allowlist resurfaces BLEU/GPT; restore re-suppresses')


# ── 4. All-caps ordinary English words (emphasis / header scaffolding) ──────

_ENGWORD_BODY = """\
# T

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback. | core loop |

## 💡 Method
The recipe is EVIDENCE-BASED AND reproducible; see the README. We introduce
NOVELGAP and evaluate with DDPM.

## 📝 Technical Reference
End.
"""


def test_common_english_words_suppressed():
    miss = _missing(_ENGWORD_BODY)
    # All-caps ordinary words from emphasis/headers/filenames are NOT terms.
    for w in ('EVIDENCE', 'BASED', 'AND', 'README'):
        assert w not in miss, f'{w} is an all-caps English word, must be suppressed: {miss}'
    # A genuine specialist acronym (DDPM) and the novel term stay flagged.
    assert 'DDPM' in miss and 'NOVELGAP' in miss, \
        f'genuine specialist acronyms must remain: {miss}'
    _ok('all-caps English words (EVIDENCE/BASED/AND/README) suppressed; specialist acronyms kept')


def test_common_word_suppression_load_bearing():
    import lib.paper.terminology_audit as ta
    orig = ta._is_common_word
    ta._is_common_word = lambda term: False
    try:
        miss = _missing(_ENGWORD_BODY)
        assert 'EVIDENCE' in miss and 'BASED' in miss, \
            f'with common-word detector neutered, EVIDENCE/BASED must resurface: {miss}'
    finally:
        ta._is_common_word = orig
    assert 'EVIDENCE' not in _missing(_ENGWORD_BODY), 'restore: EVIDENCE suppressed again'
    _ok('NC: neutering common-word detector resurfaces EVIDENCE/BASED; restore re-suppresses')


# ── Regression: the original golden gaps still fire ─────────────────────────

def test_genuine_gaps_still_detected():
    """The suppressors must NOT swallow the original golden failure modes: a
    genuinely-undefined acronym used in the body, and a dangling glossary ref."""
    from lib.paper.terminology_audit import build_terminology_audit
    body = """\
# T

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback. | core loop |
| RM | A reward model trained via the DPO objective. | reward |

## 💡 Method
The pipeline begins with an SFT stage before the loop.

## 📝 Technical Reference
End.
"""
    a = build_terminology_audit(body)
    assert a is not None
    miss = {m['term'] for m in a['missing']}
    dang = {d['referencedTerm'] for d in a['dangling']}
    # SFT: not inline-defined, not related-work-only, not well-known → gap.
    assert 'SFT' in miss, f'SFT must still be a gap: {miss}'
    # DPO: dangling inside RM's definition, not defined anywhere → gap.
    assert 'DPO' in dang, f'DPO must still be dangling: {dang}'
    _ok('regression: genuine missing (SFT) + dangling (DPO) still detected')


def main():
    print()
    print(_color('═══ Paper Terminology-Precision Tests ═══', '36'))
    print()
    tests = [
        test_inline_definition_suppressed,
        test_inline_suppression_load_bearing,
        test_cited_methodnames_suppressed,
        test_cited_suppression_load_bearing,
        test_mixedcase_named_entities_suppressed,
        test_named_entity_suppression_load_bearing,
        test_cited_but_also_used_in_method_not_suppressed,
        test_wellknown_acronyms_suppressed,
        test_wellknown_suppression_load_bearing,
        test_common_english_words_suppressed,
        test_common_word_suppression_load_bearing,
        test_genuine_gaps_still_detected,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
