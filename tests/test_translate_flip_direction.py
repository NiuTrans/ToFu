"""tests/test_translate_flip_direction.py — direction-based flip verdict.

Production evidence (2026-07-27): the wrong-language flip guard rejected
252 translations in one day, concentrated on a handful of code/term-dense
MIXED chunks (one 488-char chunk alone failed 36×). Five different model
vendors all "failed" the same way: source cjk≈0.10-0.12, output cjk≈0.12-0.20
with latin_ratio ≥ 0.55. Those outputs were FAITHFUL translations — the model
translated the prose into Chinese (output CJK share went UP) but the bulk of
the chunk is identifiers (code / paths / URLs / log lines) that correctly
stay Latin. The old guard only checked "is the output latin-dominant", which
such content can never pass, so every page load burned 5 model calls per
chunk and ended in a 502.

Owner adjudication: a TRUE flip always moves the CJK share DOWN toward zero.
So the verdict becomes directional — the guard only fires when
``cjk_ratio(output) < cjk_ratio(source)``. Output that keeps or raises the
CJK share while staying latin-dominant is accepted as faithful.

failing-first: the faithful-latin test is RED on the old guard (it refuses
after 5 attempts). NEUTER: the true-flip test bites if the direction rule is
inverted or the guard deleted.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_translate_flip_direction.py -v
"""

from __future__ import annotations

import pytest

import lib.translate.engine as engine
from lib.text_lang import cjk_ratio, latin_ratio
from lib.translate import TranslationContentRefused

pytestmark = pytest.mark.unit


# ── Faithful-but-latin-dominant fixtures (the production failure shape) ───
# A code/log-dense mixed chunk: identifiers dominate (latin≈0.71), a Chinese
# tail carries cjk≈0.126. The faithful translation translates the prose to
# Chinese (cjk rises to ≈0.162) while every identifier stays Latin — so the
# output is STILL latin-dominant (≈0.662) yet moved TOWARD Chinese.

_FAITHFUL_SOURCE = (
    'The guard lives in lib/translate/engine/_engine.py and the call chain is:\n'
    "def _translate_one_chunk(chunk, system_prompt, chunk_label='', source='', "
    "target='', status_cb=None, progress_cb=None, overall_deadline=None, use_cache=True):\n"
    '    cached = translate_cache.get(chunk, source, target) if use_cache else None\n'
    '直接原因很明确：检查只看输出是不是拉丁主导，不看方向。\n'
    '回退也只会指向主目录，两条路都到不了。'
)

_FAITHFUL_OUTPUT = (
    '检查位于 lib/translate/engine/_engine.py，调用链是：\n'
    "def _translate_one_chunk(chunk, system_prompt, chunk_label='', source='', "
    "target='', status_cb=None, progress_cb=None, overall_deadline=None, use_cache=True):\n"
    '    cached = translate_cache.get(chunk, source, target) if use_cache else None\n'
    '直接原因很明确：检查只看输出是不是拉丁主导，不看方向。\n'
    '回退也只会指向主目录，两条路都到不了。'
)

# ── True-flip fixtures (the shape the guard exists for) ───────────────────

_MIXED_SOURCE = (
    'Good question. The relevant code is in tool_display.py.\n\n'
    '## 为什么没有前缀\n'
    '直接原因很明确：模型这次用的是绝对路径，不是带前缀的命名空间路径。'
    '整套逻辑只能从两个来源推断出名字，而这条调用两个都命中不了。'
    '所以结论是：一个落在非主目录下的绝对路径，既不能从前缀解析出来，'
    '回退也只会指向主目录，两条路都到不了，于是前端就没有标签显示出来。'
)

_FLIPPED_EN = (
    'Good question. The relevant code is in tool_display.py.\n\n'
    '## Why there is no prefix\n'
    'The direct reason is clear: the model used an absolute path this time, '
    'not a namespace path with a prefix. The entire logic can only infer the '
    'name from two sources, and this call misses both. So the conclusion is: '
    'an absolute path under a non-primary directory cannot be parsed from the '
    'prefix, and the fallback only points to the primary directory, so neither '
    'path works and the frontend shows no label.'
)


def _patch_sequence(monkeypatch, replies):
    """Disable MT + cache; make smart_chat return successive `replies`.
    state['i'] counts real LLM dispatches."""
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


# ── 0. Fixture self-check (pins the production signature) ─────────────────

def test_fixture_shape_matches_production_signature():
    """If anyone edits the fixture strings, these invariants keep the test
    aimed at the real production shape: src cjk 0.10-0.15, output latin ≥ 0.55
    AND output cjk ≥ source cjk, length ≥ 200 (the guard's floor)."""
    assert 0.10 <= cjk_ratio(_FAITHFUL_SOURCE.strip()) <= 0.15
    assert latin_ratio(_FAITHFUL_OUTPUT.strip()) >= 0.55
    assert len(_FAITHFUL_OUTPUT.strip()) >= 200
    assert cjk_ratio(_FAITHFUL_OUTPUT.strip()) >= cjk_ratio(_FAITHFUL_SOURCE.strip())
    # And the true-flip fixture keeps the opposite direction:
    assert cjk_ratio(_FLIPPED_EN.strip()) < cjk_ratio(_MIXED_SOURCE.strip())


# ── 1. failing-first: faithful latin-dominant output is accepted ──────────

def test_faithful_latin_dominant_output_accepted(monkeypatch):
    """RED on the old guard: output is latin-dominant → flip fires → 5
    attempts → TranslationContentRefused. GREEN on the directional guard:
    out_cjk ≥ src_cjk → accepted on the FIRST attempt, zero retries."""
    state = _patch_sequence(monkeypatch, [_FAITHFUL_OUTPUT])
    out, usage = engine._translate_one_chunk(
        _FAITHFUL_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _FAITHFUL_OUTPUT.strip()
    assert state['i'] == 1, (
        f'faithful output burned {state["i"]} dispatches — the directional '
        'rule did not accept it on first try')
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


# ── 2. NEUTER: a TRUE flip (CJK moves DOWN) must still be refused ──────────

def test_true_flip_still_refused(monkeypatch):
    """The direction rule must not open the gate for real flips: source
    mostly Chinese → output all-English has out_cjk < src_cjk → refused with
    verdict='wrong_language'. Bites if the direction comparison is inverted
    or the guard removed."""
    _patch_sequence(monkeypatch, [_FLIPPED_EN])  # every model flips
    with pytest.raises(TranslationContentRefused) as exc_info:
        engine._translate_one_chunk(
            _MIXED_SOURCE, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    assert exc_info.value.verdict == 'wrong_language'


def test_true_flip_then_good_output_is_retried(monkeypatch):
    """1st model flips down → rejected + retried; 2nd returns proper Chinese
    → accepted. The retry machinery around the guard keeps working."""
    good_zh = (
        '好问题。相关代码在 tool_display.py 里。\n\n'
        '## 为什么没有前缀\n'
        '直接原因很明确：模型这次用的是绝对路径，不是带前缀的命名空间路径。'
        '整套逻辑只能从两个来源推断出名字，而这条调用两个都命中不了。'
        '所以结论是：一个落在非主目录下的绝对路径，既不能从前缀解析出来，'
        '回退也只会指向主目录，两条路都到不了，于是前端就没有标签显示出来。'
    )
    state = _patch_sequence(monkeypatch, [_FLIPPED_EN, good_zh])
    out, _usage = engine._translate_one_chunk(
        _MIXED_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == good_zh.strip()
    assert state['i'] >= 2


# ── 3. Boundary: out_cjk == src_cjk is NOT a flip (owner's rule: ≥ passes) ─

def test_equal_cjk_share_is_not_a_flip(monkeypatch):
    """Output that preserves the exact CJK share did not move away from
    Chinese — accepted even when still latin-dominant."""
    src = _FAITHFUL_SOURCE
    # Equal-length word swap ('guard'→'check'): out differs from src (a
    # verbatim echo would trip the no-op guard first) while the CJK share
    # stays EXACTLY the source's — the model kept the Chinese, didn't flip.
    out_same_share = src.replace('The guard lives in', 'The check lives in')
    assert out_same_share != src
    assert abs(cjk_ratio(out_same_share.strip())
               - cjk_ratio(src.strip())) < 0.01
    state = _patch_sequence(monkeypatch, [out_same_share])
    out, _usage = engine._translate_one_chunk(
        src, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == out_same_share.strip()
    assert state['i'] == 1


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
