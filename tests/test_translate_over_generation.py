"""Regression test for the OVER-GENERATION guard (the "poetry bleed" bug).

The reported bug (conv mrk5at9binpr2p, task 75370c11): a 45-char Chinese
user message was auto-translated on the send path (CN→EN) and the cheap model
(gemini-3.5-flash) produced a 2373-char output — it translated the message
correctly, then kept going and appended a large block of unrelated web-page
prose (a Ted Hughes poetry-archive page). The engine committed it as
``verdict=ok`` because EVERY existing content guard (truncation, no-op,
wrong-language-flip) defends only against output that is too SHORT — nothing
caught the opposite failure of runaway over-generation. The contaminated
"translation" (correct answer + hallucinated poetry) then surfaced in the
译文 view.

The fix adds an over-generation guard symmetric to the truncation floors: an
outsized output (both ratio AND absolute increase beyond a floor) on a
non-trivial input is rejected, the model is excluded, and the call retries; a
persistently over-generating model makes the engine RAISE rather than commit
the contamination.

These tests patch the LLM dispatch + MT provider + cache so they run offline
and deterministically. Run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

import pytest

import lib.translate.engine as engine
# The guard thresholds are module globals of the _engine submodule (the facade
# re-exports the callable but the guard reads the constants from HERE), so the
# NEUTER control must patch them on this module, not the facade.
import lib.translate.engine._engine as _engine


pytestmark = pytest.mark.unit


# The real bug's input: a 45-char Chinese message (send-path CN→EN).
_CN_INPUT = '帮我搜一下北京重点血液科吧，王芳医生建议我换医院，手术在7天后能解决吗'

# The correct translation (the legitimate leading part of the poisoned output).
_GOOD_EN = ('Please help me search for top-tier hematology departments in '
            'Beijing. Dr. Wang Fang suggested I change hospitals. My surgery '
            'is in 7 days — can it be resolved in time?')

# The POISON: the correct translation FUSED with a large block of unrelated
# hallucinated web-page prose, reproducing the real ~2373-char over-generation.
_POISON_TAIL = (
    ' of 1968, in addition to this collection, we also hold his papers, '
    'containing manuscripts, photographs, correspondence and ephemera. '
    'The University acquired the collection in 2007, with assistance from '
    'the National Heritage Memorial Fund, the MLA/V&A Purchase Grant Fund, '
    'and many individual donors. This acquisition followed a highly '
    'successful campaign to save the collection from being split up and '
    'sold abroad. The acquisition was highly significant for the University, '
    'as it represents a unique resource for the study of post-war British '
    'poetry, and is a major contribution to the cultural life of the region. '
)
# Repeat the tail until the total comfortably reproduces the ~2373-char blow-up.
_POISON_EN = _GOOD_EN + (_POISON_TAIL * 3)


def _patch_sequence(monkeypatch, replies):
    """Disable MT + cache; make smart_chat return successive `replies`."""
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *a, **k: None)
    monkeypatch.setattr(engine.translate_cache, 'put', lambda *a, **k: None)
    state = {'i': 0}

    def _fake_smart_chat(messages=None, **kw):
        i = min(state['i'], len(replies) - 1)
        state['i'] += 1
        return replies[i], {'finish_reason': 'stop',
                            '_dispatch': {'model': f'm{i}', 'key': 'k1'}}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', _fake_smart_chat)
    return state


def test_over_generation_is_rejected_and_retried(monkeypatch):
    # Sanity: the fixture reproduces the real blow-up scale.
    assert len(_POISON_EN) > len(_CN_INPUT) * _engine._OVERGEN_RATIO
    assert len(_POISON_EN) - len(_CN_INPUT) > _engine._OVERGEN_ABS

    # 1st model over-generates (correct translation + poetry) → must be
    # rejected and retried; 2nd model returns the clean translation.
    state = _patch_sequence(monkeypatch, [_POISON_EN, _GOOD_EN])
    out, usage = engine._translate_one_chunk(
        _CN_INPUT, system_prompt='translate', source='Chinese',
        target='English', overall_deadline=30)
    assert out == _GOOD_EN.strip()
    assert 'we also hold his papers' not in out   # contamination gone
    assert state['i'] >= 2                          # the poison was rejected
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_persistent_over_generation_refuses_to_commit(monkeypatch):
    # Every model over-generates → after the retry budget the engine must
    # RAISE rather than commit the "correct + poetry" contamination.
    state = _patch_sequence(monkeypatch, [_POISON_EN])  # always over-generates
    with pytest.raises(ValueError):
        engine._translate_one_chunk(
            _CN_INPUT, system_prompt='translate', source='Chinese',
            target='English', overall_deadline=30)
    assert state['i'] >= 2  # multiple attempts before giving up


def test_NEUTER_without_guard_over_generation_passes_as_ok(monkeypatch):
    # NEUTER control: raise the thresholds so the guard CANNOT fire. This
    # proves the guard is load-bearing — without it the exact poisoned output
    # is committed verbatim as verdict=ok (the reported bug).
    monkeypatch.setattr(_engine, '_OVERGEN_RATIO', 10_000.0)
    monkeypatch.setattr(_engine, '_OVERGEN_ABS', 10_000_000)
    state = _patch_sequence(monkeypatch, [_POISON_EN])
    out, usage = engine._translate_one_chunk(
        _CN_INPUT, system_prompt='translate', source='Chinese',
        target='English', overall_deadline=30)
    assert out == _POISON_EN.strip()               # contamination committed
    assert 'we also hold his papers' in out        # the poetry is present
    assert state['i'] == 1                          # accepted on the FIRST try
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_normal_short_translation_not_flagged(monkeypatch):
    # No-false-positive control: a legitimately terse translation with the
    # natural CN→EN expansion (~22→113 chars, ratio ≈ 5) must be accepted on
    # the first try. The guard requires BOTH ratio > 8x AND abs increase > 800,
    # so a normal short translation never trips it.
    cn = '你好，请帮我预约明天上午的门诊。'                    # ~16 chars
    en = ('Hello, please help me book an outpatient appointment for tomorrow '
          'morning at your earliest convenience.')            # ~101 chars
    assert len(en) < len(cn) * _engine._OVERGEN_RATIO
    state = _patch_sequence(monkeypatch, [en])
    out, usage = engine._translate_one_chunk(
        cn, system_prompt='translate', source='Chinese',
        target='English', overall_deadline=30)
    assert out == en.strip()
    assert state['i'] == 1                           # accepted immediately
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'
