"""Regression test for the "Model echoed input after retries" false positive.

When an agent already replies in Chinese and the auto-translate target is
Chinese, the engine's no-op/echo detector used to flag the (correct)
verbatim output as "model echoed input", burn the full retry budget, and
then FAIL the whole translation. The fix exempts the exact-match no-op
branch when the source is already in the target language.

These tests patch the LLM dispatch + MT provider + cache so they run
offline and deterministically.
"""

import pytest

import lib.translate.engine as engine


pytestmark = pytest.mark.unit


_CHINESE = '我是 Kimi，由月之暗面（Moonshot AI）开发的人工智能助手。有什么我可以帮你的吗？'


def _patch_common(monkeypatch, reply):
    """Disable MT + cache and make smart_chat echo a fixed reply."""
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *a, **k: None)
    monkeypatch.setattr(engine.translate_cache, 'put', lambda *a, **k: None)

    calls = {'n': 0}

    def _fake_smart_chat(messages=None, **kw):
        calls['n'] += 1
        return reply, {'finish_reason': 'stop',
                       '_dispatch': {'model': 'fake-cheap', 'key': 'k1'}}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', _fake_smart_chat)
    return calls


def test_chinese_to_chinese_verbatim_is_passthrough(monkeypatch):
    # Model returns the already-Chinese input unchanged — legitimate no-op.
    calls = _patch_common(monkeypatch, _CHINESE)
    out, _usage = engine._translate_one_chunk(
        _CHINESE, system_prompt='translate', source='English', target='Chinese')
    # Accepted on the FIRST attempt, returned verbatim (stripped) — NOT flagged
    # as an echo, NOT retried, NOT raised.
    assert out == _CHINESE.strip()
    assert calls['n'] == 1


def test_english_to_chinese_verbatim_still_flagged_as_noop(monkeypatch):
    # Source is English, target Chinese, model echoes the English input →
    # this IS a genuine no-op and must still be detected (retried, then fail).
    english = ('This is a sufficiently long English sentence that should be '
               'translated into Chinese but the cheap model just echoed it.')
    calls = _patch_common(monkeypatch, english)
    raised = False
    try:
        engine._translate_one_chunk(
            english, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    except ValueError:
        raised = True
    # Genuine echo is still caught: multiple attempts, then ValueError.
    assert raised is True
    assert calls['n'] >= 2


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


def test_en_to_zh_half_dropped_is_retried(monkeypatch):
    # A 2000-char English doc where the model returns a Chinese translation
    # of only the first ~25% (500 chars). That clears the OLD flat 20% floor
    # but must trip the new Chinese-aware 30% floor and be retried; the 2nd
    # attempt returns a full-length (~0.5x) translation which is accepted.
    english = 'Sentence number %d explaining the design in detail. ' % 0
    english = (english * 40)[:2000]            # ~2000 chars EN
    short_zh = '第一段的中文翻译。' * 28              # ~252 chars, ratio ~0.13... bump up
    short_zh = '第一段说明设计细节的中文译文。' * 33     # ~ < 30% but > 20%
    full_zh = '这是完整文档的中文译文，覆盖了从头到尾的全部内容。' * 45  # ~0.5x
    state = _patch_sequence(monkeypatch, [short_zh, full_zh])
    out, usage = engine._translate_one_chunk(
        english, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    # The short (half-dropped) output was rejected; the full one accepted.
    assert out == full_zh.strip()
    assert state['i'] >= 2
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok' and tr['path'] == 'llm'


def test_trace_marks_truncated_when_all_attempts_short(monkeypatch):
    # Every attempt returns a half-dropped translation → after the retry
    # budget the engine accepts best-effort but stamps verdict='truncated'
    # so the provenance trail flags the incomplete commit.
    english = ('Detailed sentence about the system. ' * 60)[:2000]
    short_zh = '只翻译了开头的一小段中文。' * 18   # well under 30% of 2000
    state = _patch_sequence(monkeypatch, [short_zh])  # always short
    out, usage = engine._translate_one_chunk(
        english, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == short_zh.strip()         # best-effort accepted
    assert state['i'] >= 5                  # exhausted content retries
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'truncated'
