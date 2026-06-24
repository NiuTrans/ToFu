"""Regression test for the wrong-language flip on MIXED English+Chinese input.

The reported bug: an assistant message that is English intro + a large
Chinese body is auto-translated with target=Chinese. A weak cheap model
"normalises" the whole document into English (rewriting the already-Chinese
majority) to satisfy the prompt's anti-verbatim rule. The result is a
译文 that is predominantly English while the 原文 was mostly Chinese.

The old no-op CJK guard only fired when ``cjk_ratio(source) < 0.1`` (source
almost pure English), so it missed this case entirely (source cjk_ratio was
well above 0.1). The new flip detector verifies the OUTCOME: when
target=Chinese and the source carried meaningful Chinese, the output must
NOT be predominantly English — otherwise reject, exclude the model, retry.

These tests patch the LLM dispatch + MT provider + cache so they run
offline and deterministically.
"""

import lib.translate.engine as engine


# A mixed message: short English lead-in + a large Chinese body (the shape of
# the real assistant reply that triggered the bug). cjk_ratio >> 0.1.
_MIXED_SOURCE = (
    'Good question. The relevant code is in tool_display.py.\n\n'
    '## 为什么没有前缀\n'
    '直接原因很明确：模型这次用的是绝对路径，不是带前缀的命名空间路径。'
    '整套逻辑只能从两个来源推断出名字，而这条调用两个都命中不了。'
    '所以结论是：一个落在非主目录下的绝对路径，既不能从前缀解析出来，'
    '回退也只会指向主目录，两条路都到不了，于是前端就没有标签显示出来。'
)

# The BAD output: the whole thing rewritten into English (the flip).
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

# The GOOD output: English lead-in translated to Chinese, Chinese body kept —
# uniformly Chinese (what the translation SHOULD produce).
_GOOD_ZH = (
    '好问题。相关代码在 tool_display.py 里。\n\n'
    '## 为什么没有前缀\n'
    '直接原因很明确：模型这次用的是绝对路径，不是带前缀的命名空间路径。'
    '整套逻辑只能从两个来源推断出名字，而这条调用两个都命中不了。'
    '所以结论是：一个落在非主目录下的绝对路径，既不能从前缀解析出来，'
    '回退也只会指向主目录，两条路都到不了，于是前端就没有标签显示出来。'
)


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


def test_mixed_source_flipped_to_english_is_retried(monkeypatch):
    # 1st model flips the mixed source to all-English → must be rejected and
    # retried; 2nd model returns the correct uniformly-Chinese output.
    state = _patch_sequence(monkeypatch, [_FLIPPED_EN, _GOOD_ZH])
    out, usage = engine._translate_one_chunk(
        _MIXED_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _GOOD_ZH.strip()
    assert state['i'] >= 2  # the flip was rejected, a 2nd attempt was made
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_persistent_flip_refuses_to_commit(monkeypatch):
    # Every model flips to English → after the retry budget the engine must
    # RAISE rather than commit an English "translation" over a Chinese source.
    state = _patch_sequence(monkeypatch, [_FLIPPED_EN])  # always flips
    raised = False
    try:
        engine._translate_one_chunk(
            _MIXED_SOURCE, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    except ValueError:
        raised = True
    assert raised is True
    assert state['i'] >= 2  # multiple attempts before giving up


def test_correct_chinese_output_not_flagged(monkeypatch):
    # The good (uniformly Chinese) output must be accepted on the FIRST try —
    # the flip detector must not fire on a legitimate translation.
    state = _patch_sequence(monkeypatch, [_GOOD_ZH])
    out, _usage = engine._translate_one_chunk(
        _MIXED_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _GOOD_ZH.strip()
    assert state['i'] == 1  # accepted immediately, no retry


def test_pure_english_to_chinese_unaffected(monkeypatch):
    # A pure-English source correctly translated to Chinese is accepted — the
    # flip guard requires the SOURCE to carry meaningful Chinese, so it never
    # fires here (the existing no-op/truncation guards still apply).
    english = ('This is a sufficiently long English paragraph that should be '
               'translated into Chinese by the model without any issue at all.')
    zh = '这是一段足够长的英文段落，模型应当把它顺利地翻译成中文，不应出现任何问题。'
    state = _patch_sequence(monkeypatch, [zh])
    out, _usage = engine._translate_one_chunk(
        english, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == zh.strip()
    assert state['i'] == 1
