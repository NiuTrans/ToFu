#!/usr/bin/env python3
"""Terminology self-containment audit for generated paper reports.

The report is a SINGLE forward pass (one ``run_agent_loop`` over one shared
``messages`` list — the prompt says "write the full report in one pass"), and
the glossary ("Core Terminology") sits EARLY in the section order. So the
glossary is written before the Method/Experiments prose exists — it is a
FORECAST of terms, not an index of terms actually used. The self-containment
rules in the prompt ("every used term must have a row"; "no glossary definition
may lean on an undefined sibling term") are soft prompt text with ZERO
enforcement. This audit is the acceptance gate over the COMPLETE body that
enforces them mechanically, modelled on ``citation_audit``: deterministic,
zero-LLM, best-effort (failure → ``None``, never an exception), additive (a
card attached to the report meta — the body is NEVER mutated).

Two failure modes, isolated on ONE real body:
  (A) MISSING — an acronym used in the body with no glossary row (here ``SFT``,
      used in Method + Experimental, no row).
  (B) DANGLING — a glossary definition that leans on a term with no row of its
      own (here the ``RM`` row's definition references ``DPO``, which is not a
      row and appears nowhere else, so it is dangling-only, never missing).

Plus a clean-body negative control (empty gate) and a SOURCE-LEVEL load-bearing
negative control (force the gap-finders empty → no card; restore → card
returns), proving the card is driven by DETECTED gaps, not mere presence.

Offline: no network. The engine-wiring test stubs ``dispatch_stream`` exactly
like tests/test_paper_report_dedup.py / tests/test_paper_citation_audit.py, and
uses a body with NO arXiv/DOI identifiers so the sibling citation audit no-ops
without any HTTP.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── Real report bodies ──────────────────────────────────────────────────────
# FAILING body: glossary rows = RLHF, PPO, RM (NO SFT). Body uses SFT (→ missing
# only). The RM row's definition references DPO, which has no row AND appears
# nowhere in the body (→ dangling only). "reinforcement-learning" is spelled out
# in prose (never as the bare acronym "RL") so it is not a stray flag.
_FAILING_BODY = """\
# Efficient RLHF Training

## ⚡ TL;DR
We align a language model with human feedback and report a +4.2 win-rate gain.

## 📋 Paper Card
| Field | Detail |
|-------|--------|
| **Title** | Efficient RLHF Training |

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback; the policy is optimized against a reward using PPO and an RM. | It is the core training loop. |
| PPO | Proximal Policy Optimization, a clipped policy-gradient method. | It is the optimizer of the alignment stage. |
| RM | A reward model trained via the DPO objective to score responses. | It supplies the reward signal. |

## 💡 Method
The pipeline begins with an SFT stage before the reinforcement-learning loop,
then applies PPO against the RM to update the policy.

## 📊 Experimental Analysis
We compare SFT-only against the full RLHF pipeline.

## 📝 Technical Reference
The end.
"""

# CLEAN body: adds the SFT row and rewrites the RM definition so it references
# only glossaried terms (drops DPO). Every used acronym has a row; every
# definition references only rows → the gate must be EMPTY.
_CLEAN_BODY = """\
# Efficient RLHF Training

## ⚡ TL;DR
We align a language model with human feedback and report a +4.2 win-rate gain.

## 📋 Paper Card
| Field | Detail |
|-------|--------|
| **Title** | Efficient RLHF Training |

## 🔑 Core Terminology (read this first)
| Term | Definition | Why it matters |
|------|-----------|---------------|
| RLHF | Reinforcement learning from human feedback; the policy is optimized against a reward using PPO and an RM. | It is the core training loop. |
| PPO | Proximal Policy Optimization, a clipped policy-gradient method. | It is the optimizer of the alignment stage. |
| RM | A reward model trained on preference pairs to score responses. | It supplies the reward signal. |
| SFT | Supervised fine-tuning on demonstration data. | It warm-starts the policy. |

## 💡 Method
The pipeline begins with an SFT stage before the reinforcement-learning loop,
then applies PPO against the RM to update the policy.

## 📊 Experimental Analysis
We compare SFT-only against the full RLHF pipeline.

## 📝 Technical Reference
The end.
"""


# ── Direct unit tests on build_terminology_audit ────────────────────────────

def test_missing_term_caught():
    from lib.paper.terminology_audit import build_terminology_audit
    audit = build_terminology_audit(_FAILING_BODY)
    assert audit is not None, 'a gap exists — the card MUST be attached'
    missing = {m['term'] for m in audit.get('missing', [])}
    dangling_ref = {d['referencedTerm'] for d in audit.get('dangling', [])}
    assert 'SFT' in missing, f'SFT is used in the body with no glossary row: {missing}'
    # SFT is a MISSING gap, not a DANGLING one.
    assert 'SFT' not in dangling_ref, dangling_ref
    # Every glossaried acronym actually used (RLHF/PPO/RM) is covered.
    assert 'RLHF' not in missing and 'PPO' not in missing and 'RM' not in missing, missing
    # The missing entry carries a section + evidence for the reader.
    sft = [m for m in audit['missing'] if m['term'] == 'SFT'][0]
    assert sft.get('section'), 'missing entry must name the section it appears in'
    assert 'SFT' in sft.get('evidence', ''), 'evidence must quote the usage'
    _ok('mode A: acronym used in body but absent from glossary is caught (missing)')


def test_dangling_definition_caught():
    from lib.paper.terminology_audit import build_terminology_audit
    audit = build_terminology_audit(_FAILING_BODY)
    assert audit is not None
    dangling = audit.get('dangling', [])
    by_ref = {d['referencedTerm']: d for d in dangling}
    assert 'DPO' in by_ref, f'RM definition leans on undefined DPO: {dangling}'
    assert by_ref['DPO']['term'] == 'RM', by_ref['DPO']
    # DPO appears ONLY inside a glossary definition, never in the body, so it is
    # a dangling reference — NOT a missing-in-body term.
    missing = {m['term'] for m in audit.get('missing', [])}
    assert 'DPO' not in missing, f'DPO is dangling-only, not missing: {missing}'
    _ok('mode B: glossary definition leaning on an undefined sibling term is caught (dangling)')


def test_clean_body_empty_gate():
    from lib.paper.terminology_audit import build_terminology_audit
    audit = build_terminology_audit(_CLEAN_BODY)
    assert audit is None, f'a self-contained body must produce NO card, got {audit}'
    _ok('negative control: a clean, self-contained body produces an empty gate (None)')


def test_no_glossary_returns_none():
    """Best-effort: a body without a Core Terminology section is not audited
    (avoids false positives on non-report text) — mirrors citation_audit's
    'nothing to check → None'."""
    from lib.paper.terminology_audit import build_terminology_audit
    body = '## ⚡ TL;DR\nAn SFT model trained with PPO.\n## 📝 Technical Reference\nEnd.\n'
    assert build_terminology_audit(body) is None
    assert build_terminology_audit('') is None
    _ok('no glossary section / empty text → None (no false-positive card)')


# ── Engine wiring (drives the REAL _run_report_task) ────────────────────────

def _patch_dispatch(body):
    import lib.paper.report_engine as re_mod

    def _fake_dispatch(messages, on_content=None, on_thinking=None, **kw):
        if body and on_content:
            on_content(body)
        msg = {'role': 'assistant', 'content': body, 'tool_calls': []}
        usage = {'prompt_tokens': 10, 'completion_tokens': 20, '_dispatch': {}}
        return msg, 'stop', usage

    re_mod.dispatch_stream = _fake_dispatch


def _make_task(tid, lang='en'):
    from lib.paper import _new_report_task
    return _new_report_task(tid, 'phashterm00000000000000000000000', lang, None,
                            client_title='Efficient RLHF Training')


def _run(tid, body, lang='en'):
    import lib.paper.report_engine as re_mod
    _patch_dispatch(body)
    task = _make_task(tid, lang)
    re_mod._run_report_task(task, [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'paper'},
    ], [])
    return task


def test_engine_attaches_terminology_audit():
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    try:
        task = _run('rpt_term_1', _FAILING_BODY)
        assert task['status'] == 'done', task.get('status')
        meta = task.get('report_meta') or {}
        audit = meta.get('terminologyAudit')
        assert audit is not None, 'terminologyAudit MUST ride the report meta'
        assert 'SFT' in {m['term'] for m in audit['missing']}
        assert 'DPO' in {d['referencedTerm'] for d in audit['dangling']}
        # The done event carries the same meta (frontend gating source).
        done = [e for e in task['events'] if e.get('type') == 'done'][-1]
        assert done['meta'].get('terminologyAudit') is not None
    finally:
        re_mod.dispatch_stream = orig
    _ok('engine attaches terminologyAudit to report meta + done event')


def test_engine_clean_body_no_card():
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    try:
        task = _run('rpt_term_2', _CLEAN_BODY)
        meta = task.get('report_meta') or {}
        assert meta.get('terminologyAudit') is None, 'clean body → no card'
    finally:
        re_mod.dispatch_stream = orig
    _ok('engine attaches NO card for a self-contained body')


def test_engine_skips_review_mode():
    """Review Mode is a decision document, not an illustrated explainer — the
    terminology audit is skipped there (call-site guard, mirrors the text-only
    image handling)."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    try:
        # A review composite lang key; the body still has the gap, but the audit
        # must not run for reviews.
        task = _run('rpt_term_rev', _FAILING_BODY, lang='review:iclr:en')
        meta = task.get('report_meta') or {}
        assert meta.get('terminologyAudit') is None, 'review mode must not be audited'
    finally:
        re_mod.dispatch_stream = orig
    _ok('engine skips the terminology audit in Review Mode')


def test_negctl_gap_finders_load_bearing():
    """SOURCE-LEVEL negative control: force BOTH gap-finders to return empty →
    build returns None (no card) even on the FAILING body; restoring them brings
    the card back. Proves the card is driven by DETECTED gaps, not by the mere
    presence of a glossary."""
    import lib.paper.terminology_audit as ta
    orig_missing = ta._find_missing_terms
    orig_dangling = ta._find_dangling_refs
    ta._find_missing_terms = lambda *a, **k: []
    ta._find_dangling_refs = lambda *a, **k: []
    try:
        assert ta.build_terminology_audit(_FAILING_BODY) is None, \
            'with both gap-finders neutered, no card must be produced'
    finally:
        ta._find_missing_terms = orig_missing
        ta._find_dangling_refs = orig_dangling
    # Restore sanity: the real finders bring the card back on the same body.
    assert ta.build_terminology_audit(_FAILING_BODY) is not None, \
        'card must return once the real gap-finders are restored'
    _ok('negative control: neutering the gap-finders removes the card; restore brings it back')


def main():
    print()
    print(_color('═══ Paper Report Terminology-Audit Tests ═══', '36'))
    print()
    tests = [
        test_missing_term_caught,
        test_dangling_definition_caught,
        test_clean_body_empty_gate,
        test_no_glossary_returns_none,
        test_engine_attaches_terminology_audit,
        test_engine_clean_body_no_card,
        test_engine_skips_review_mode,
        test_negctl_gap_finders_load_bearing,
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
