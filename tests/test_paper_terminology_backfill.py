#!/usr/bin/env python3
"""Definition-backfill second pass — the acceptance gate is the RE-AUDIT.

Step 1 (terminology_audit) makes the gap VISIBLE to the reader; it labels the
failure. This pass CURES it: when the audit flags gaps, a bounded pure-body-
context LLM call generates definitions for the missing/dangling terms, and an
addendum glossary table is appended — but ONLY the subset that PROVABLY closes a
gap. "Provably" = re-run ``build_terminology_audit`` on ``body + addendum`` and
keep a new row iff it (a) removes a real gap and (b) does not itself introduce a
NEW undefined term. The north star is the report being self-contained, not
merely annotated, so the re-audit gate is the definition of done.

Design commitments proven here (owner-locked):
  1. RE-AUDIT GATE IS REAL: golden gappy body → backfill defines the missing
     (SFT) + dangling (DPO) terms → re-audit of body+addendum returns empty.
  2. GATE IS LOAD-BEARING (negative control): force the LLM to return JUNK that
     does NOT close the gaps (defines an unrelated term, or defines SFT USING a
     fresh undefined term) → the addendum is DROPPED (''), so the warning card
     is retained. A backfill that ran but didn't close gaps is NOT success.
  3. OFF THE PERSISTENCE-CRITICAL PATH: the primary ``done`` event + persisted
     body are byte-identical whether or not backfill runs (separate ``termfill:``
     key, append-only, never rewrites ``enriched``). Source-level neuter proves
     the engine hook is purely additive.
  4. FLAG-GATED OFF + REVIEW-SKIPPED.

Offline: the LLM is a stub ``dispatch`` injected into ``build_backfill_addendum``
(returns strict JSON), so no network. The audit itself is deterministic.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402

TEST_OWNER_USER_ID = 1
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# A real gappy body: glossary defines RLHF/PPO/RM (RM's def leans on undefined
# DPO); the body uses SFT with no row. So: missing={SFT}, dangling={DPO via RM}.
_GAPPY_BODY = """\
# Efficient RLHF Training

## ⚡ TL;DR
We align a language model with human feedback and report a +4.2 win-rate gain.

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


def _audit(body):
    from lib.paper.terminology_audit.audit import build_terminology_audit
    return build_terminology_audit(body)


def _gaps(body):
    a = _audit(body)
    if not a:
        return set(), set()
    return ({m['term'] for m in a['missing']},
            {d['referencedTerm'] for d in a['dangling']})


# ── Stub dispatchers (strict-JSON {term: definition}) ──────────────────────

def _good_dispatch(messages, **kw):
    """A well-behaved backfill: defines SFT (missing) + DPO (dangling), each in
    self-contained prose that introduces NO fresh undefined term."""
    out = {
        'SFT': 'Supervised fine-tuning: training the policy on curated demonstration examples.',
        'DPO': 'Direct Preference Optimization: a reference-free objective that fits a policy directly to preference pairs.',
    }
    return json.dumps(out), {'prompt_tokens': 5, 'completion_tokens': 10}


def _junk_dispatch(messages, **kw):
    """Junk: defines an UNRELATED term and leaves the real gaps open."""
    return json.dumps({'FOO': 'A completely unrelated made-up term.'}), {}


def _poisoned_dispatch(messages, **kw):
    """Poisoned: 'defines' SFT but the definition itself leans on a FRESH
    undefined acronym (GRPO), so accepting it would trade one gap for another —
    the re-audit gate must reject this row."""
    out = {
        'SFT': 'Supervised fine-tuning implemented on top of the GRPO trainer.',
        'DPO': 'Direct Preference Optimization: fits a policy to preference pairs.',
    }
    return json.dumps(out), {}


# ── Tests ───────────────────────────────────────────────────────────────────

def test_baseline_body_is_gappy():
    miss, dang = _gaps(_GAPPY_BODY)
    assert 'SFT' in miss and 'DPO' in dang, (miss, dang)
    _ok('precondition: the golden body has a real missing (SFT) + dangling (DPO) gap')


def test_good_backfill_closes_gaps_reaudit_empty():
    from lib.paper.terminology_backfill import build_backfill_addendum
    audit = _audit(_GAPPY_BODY)
    addendum = build_backfill_addendum(_GAPPY_BODY, audit, 'en', dispatch=_good_dispatch)
    assert addendum, 'a good backfill must produce a non-empty addendum'
    # The addendum must define both gap terms.
    assert 'SFT' in addendum and 'DPO' in addendum, addendum
    # THE GATE: re-audit of body + addendum returns EMPTY.
    miss, dang = _gaps(_GAPPY_BODY.rstrip() + '\n\n' + addendum + '\n')
    assert not miss and not dang, f're-audit still gappy: missing={miss} dangling={dang}\n{addendum}'
    _ok('re-audit gate: good backfill closes SFT + DPO → re-audit empty (report now self-contained)')


def test_junk_backfill_dropped_card_retained():
    from lib.paper.terminology_backfill import build_backfill_addendum
    audit = _audit(_GAPPY_BODY)
    addendum = build_backfill_addendum(_GAPPY_BODY, audit, 'en', dispatch=_junk_dispatch)
    assert addendum == '', f'junk that closes no gap must be DROPPED, got: {addendum!r}'
    # And the original gaps are of course still there → the warning card stays.
    miss, dang = _gaps(_GAPPY_BODY)
    assert 'SFT' in miss and 'DPO' in dang
    _ok('load-bearing NC: junk backfill (closes nothing) → addendum dropped, warning card retained')


def test_poisoned_definition_rejected():
    """A definition that introduces a FRESH undefined term (GRPO) must not be
    accepted — it would trade one gap for another. The DPO row (clean) may still
    be kept; the SFT-via-GRPO row must be dropped, and the re-audit must NOT
    contain GRPO as a newly-created gap."""
    from lib.paper.terminology_backfill import build_backfill_addendum
    audit = _audit(_GAPPY_BODY)
    addendum = build_backfill_addendum(_GAPPY_BODY, audit, 'en', dispatch=_poisoned_dispatch)
    combined = _GAPPY_BODY.rstrip() + '\n\n' + (addendum or '') + '\n'
    miss, dang = _gaps(combined)
    # The poisoned SFT row must NOT have been appended (it would add GRPO).
    assert 'GRPO' not in (addendum or ''), f'poisoned row leaked GRPO: {addendum}'
    assert 'GRPO' not in miss, f'backfill created a NEW gap GRPO: {miss}'
    # SFT stays open (its only offered definition was poisoned & rejected).
    assert 'SFT' in miss, 'SFT should remain open after rejecting its poisoned def'
    _ok('re-audit gate: a definition introducing a fresh undefined term is rejected (no gap-for-gap trade)')


def test_flag_and_key_helpers():
    from lib.paper.terminology_backfill import (
        termfill_globally_disabled, termfill_lang_key,
    )
    # Env master switch is a KILL SWITCH now (default: NOT disabled). Per-request
    # enablement lives in personal_scope.resolve_paper_termfill_enabled.
    os.environ.pop('TOFU_PAPER_TERMFILL', None)
    assert termfill_globally_disabled() is False
    os.environ['TOFU_PAPER_TERMFILL'] = '0'
    assert termfill_globally_disabled() is True
    os.environ.pop('TOFU_PAPER_TERMFILL', None)
    assert termfill_lang_key('en') == 'termfill:en'
    assert termfill_lang_key('zh') == 'termfill:zh'
    _ok('env is a kill switch (default not-disabled); termfill:<ui> key helper')


def test_termfill_persistence_uses_semantic_sidecar(monkeypatch):
    import lib.paper.terminology_backfill as tb

    calls = []

    class Client:
        def command(self, operation, payload, command_id):
            calls.append((operation, payload, command_id))
            return {'saved': True}

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: Client())
    monkeypatch.setattr(
        tb, 'build_backfill_addendum',
        lambda *_args, **_kwargs: '## Added definitions')

    result = tb.run_report_termfill(
        'body', 'en', phash='paper-hash', model='model-x',
        audit={'missing': [{'term': 'SFT'}]}, user_id=TEST_OWNER_USER_ID)

    assert result['closed'] is True
    assert calls[0][0] == 'paper.report.upsert'
    assert calls[0][1]['user_id'] == TEST_OWNER_USER_ID
    assert calls[0][1]['lang'] == 'termfill:en'
    assert calls[0][1]['meta'] == {'kind': 'termfill'}
    assert calls[0][2].startswith('paper-termfill:')


def test_negctl_reaudit_gate_load_bearing():
    """SOURCE-LEVEL: neuter the re-audit filter so EVERY offered row is accepted
    blindly → the junk addendum would no longer be empty. Proves the gate (not
    just 'the LLM returned something') is what makes the pass honest."""
    import lib.paper.terminology_backfill as tb
    orig = tb._keep_gap_closing_rows
    # Blindly accept every offered definition (no re-audit filtering).
    tb._keep_gap_closing_rows = lambda body, rows, audit: dict(rows)
    try:
        audit = _audit(_GAPPY_BODY)
        addendum = tb.build_backfill_addendum(_GAPPY_BODY, audit, 'en', dispatch=_junk_dispatch)
        assert addendum and 'FOO' in addendum, \
            'with the gate neutered, the junk row should slip through (proving the gate is real)'
    finally:
        tb._keep_gap_closing_rows = orig
    # Restore sanity: the real gate drops it again.
    addendum2 = tb.build_backfill_addendum(_GAPPY_BODY, _audit(_GAPPY_BODY), 'en', dispatch=_junk_dispatch)
    assert addendum2 == '', 'real gate must drop the junk row once restored'
    _ok('negative control: neutering the re-audit gate lets junk through; restore drops it')


# ── Engine off-path byte-identity ───────────────────────────────────────────

def _patch_dispatch_stream(body):
    import lib.paper.report_engine.worker as re_mod

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        if body and on_content:
            on_content(body)
        return {'role': 'assistant', 'content': body, 'tool_calls': []}, 'stop', \
               {'prompt_tokens': 10, 'completion_tokens': 20, '_dispatch': {}}
    re_mod.dispatch_stream = _fake


def _run_engine(tid, body, lang='en'):
    import lib.paper.report_engine.worker as re_mod
    from lib.paper.report_runtime import _new_report_task
    _patch_dispatch_stream(body)
    task = _new_report_task(tid, 'phashbackfill000000000000000000', lang, None,
                            client_title='Efficient RLHF Training',
                            ui_lang='en' if lang == 'en' else 'en',
                            config={
                                'paperInsightEnabled': False,
                                'paperCheckpointsEnabled': False,
                            }, user_id=TEST_OWNER_USER_ID)
    re_mod.run_report_task(task, [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'paper'},
    ], [])
    return task


def test_engine_done_body_byte_identical_kill_switch():
    import lib.paper.report_engine.worker as re_mod
    orig = re_mod.dispatch_stream
    try:
        # Engage the fleet-wide kill switch → backfill must not run at all.
        os.environ['TOFU_PAPER_TERMFILL'] = '0'
        task = _run_engine('rpt_bf_off', _GAPPY_BODY)
        done = [e for e in task['events'] if e.get('type') == 'done'][-1]
        body_off = done['report']
        # The primary body still carries the ORIGINAL gappy glossary (backfill is
        # separate-key, append-only, and OFF here) — and the warning card is
        # still attached (detection is independent of backfill).
        assert task['report_meta'].get('terminologyAudit') is not None
        assert '## 📖' not in body_off and 'termfill' not in body_off.lower(), \
            'kill switch: no addendum may touch the primary persisted body'
    finally:
        re_mod.dispatch_stream = orig
        os.environ.pop('TOFU_PAPER_TERMFILL', None)
    _ok('off-path: kill switch → primary done body carries no addendum, warning card intact')


def test_engine_flag_on_fires_and_appends_addendum_event():
    """Flag ON, plain report, real gap → the engine hook runs the backfill and
    emits a ``termfill`` event carrying a gap-closing addendum, WITHOUT altering
    the primary persisted ``done`` body (separate-key, append-only)."""
    import lib.paper.report_engine.worker as re_mod
    import lib.paper.terminology_backfill as tb
    orig = re_mod.dispatch_stream
    orig_run = tb.run_report_termfill

    # Route the pass through the good stub dispatcher (offline) + skip DB persist.
    def _stub_run(report_md, ui_lang='en', **k):
        k.pop('dispatch', None)
        k.pop('persist', None)
        return orig_run(report_md, ui_lang, dispatch=_good_dispatch, persist=False, **k)
    tb.run_report_termfill = _stub_run
    try:
        os.environ.pop('TOFU_PAPER_TERMFILL', None)  # default-ON for interactive (config=None)
        task = _run_engine('rpt_bf_on', _GAPPY_BODY)
        done = [e for e in task['events'] if e.get('type') == 'done'][-1]
        # Primary body byte-identical to the OFF case: no addendum baked in.
        assert '## 📖' not in done['report'], 'addendum must not touch the primary body'
        tf = [e for e in task['events'] if e.get('type') == 'termfill']
        assert tf, 'a termfill event must be emitted when the flag is on and a gap exists'
        addendum = tf[-1]['addendum']
        assert 'SFT' in addendum and 'DPO' in addendum
        # And it genuinely closes the gaps (re-audit of body + event addendum empty).
        miss, dang = _gaps(done['report'].rstrip() + '\n\n' + addendum + '\n')
        assert not miss and not dang, f'engine addendum did not close gaps: {miss} {dang}'
    finally:
        re_mod.dispatch_stream = orig
        tb.run_report_termfill = orig_run
        tb.__dict__.pop('_orig_run', None)
        os.environ.pop('TOFU_PAPER_TERMFILL', None)
    _ok('engine (flag ON): fires backfill, emits gap-closing termfill event, primary body untouched')


def test_engine_skips_backfill_in_review_mode():
    """A review lang key must short-circuit the backfill hook before any LLM
    call — run_report_termfill is never invoked."""
    import lib.paper.report_engine.worker as re_mod
    import lib.paper.terminology_backfill as tb
    orig = re_mod.dispatch_stream
    orig_run = tb.run_report_termfill
    called = {'n': 0}
    tb.run_report_termfill = lambda *a, **k: called.__setitem__('n', called['n'] + 1)
    try:
        os.environ.pop('TOFU_PAPER_TERMFILL', None)
        _run_engine('rpt_bf_rev', _GAPPY_BODY, lang='review:iclr:en')
        assert called['n'] == 0, 'review mode must not invoke run_report_termfill'
    finally:
        re_mod.dispatch_stream = orig
        tb.run_report_termfill = orig_run
        os.environ.pop('TOFU_PAPER_TERMFILL', None)
    _ok('review mode: backfill hook short-circuits (no run_report_termfill call)')


def main():
    print()
    print(_color('═══ Paper Report Terminology-Backfill Tests ═══', '36'))
    print()
    tests = [
        test_baseline_body_is_gappy,
        test_good_backfill_closes_gaps_reaudit_empty,
        test_junk_backfill_dropped_card_retained,
        test_poisoned_definition_rejected,
        test_flag_and_key_helpers,
        test_negctl_reaudit_gate_load_bearing,
        test_engine_done_body_byte_identical_kill_switch,
        test_engine_flag_on_fires_and_appends_addendum_event,
        test_engine_skips_backfill_in_review_mode,
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
