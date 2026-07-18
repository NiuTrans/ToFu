"""Regression test for the identity-invariant short-circuit (board epic
pt_6080a12a53aa4081).

Evidence (task inc-translate-89dd9bf7, 2026-07-14 18:01): auto-translating an
assistant reply that was essentially a single absolute path (98 chars), the
engine's no-op detector treated the model's correct verbatim echo as a
*translation failure* and burned 9 real model attempts across two fallback
rounds before surfacing a ValueError. Wasted spend + a surfaced error for
content that never needed translating.

The fix short-circuits legitimately identity-invariant content BEFORE the
retry loop — accepting it verbatim with ZERO model calls:
  (a) no translatable letters (pure symbols / digits / punctuation),
  (b) a lone path / URL token,
  (c) text already predominantly in the target language.

Genuinely-translatable content (including a genuine echo of foreign text)
must still enter the retry loop and be flagged.

These tests patch the LLM dispatch + MT provider + cache so they run offline
and deterministically. Run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

import pytest

import lib.translate.engine as engine
import lib.translate.engine._engine as _engine


pytestmark = pytest.mark.unit


def _patch_common(monkeypatch, reply):
    """Disable MT + cache and count how many times smart_chat is called."""
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


# ── (b) path / URL token — the reported bug ──────────────────────────────

# The 98-char shape from task inc-translate-89dd9bf7.
_ABS_PATH = ('/path/to/your/data'
             'your-username/chatui/lib/translate/engine/_engine.py')


def test_absolute_path_accepted_verbatim_zero_model_calls(monkeypatch):
    # The reported bug: a lone absolute path. Accepted verbatim, NO model call,
    # NO ValueError — the whole retry-storm is skipped.
    calls = _patch_common(monkeypatch, _ABS_PATH)
    out, usage = engine._translate_one_chunk(
        _ABS_PATH, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out == _ABS_PATH
    assert calls['n'] == 0                       # ZERO model attempts
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'identity' and tr['path'] == 'identity'
    assert usage.get('_identity_invariant') is True


@pytest.mark.parametrize('token', [
    'https://km.internal.example.com/collabpage/123456',
    'file:///tmp/report.pdf',
    '~/projects/tofu/server.py',
    './static/js/main.js',
    '../lib/text_lang/_ratios.py',
    r'C:\Users\me\notes.txt',
])
def test_url_and_path_shapes_short_circuit(monkeypatch, token):
    calls = _patch_common(monkeypatch, token)
    out, _u = engine._translate_one_chunk(
        token, system_prompt='translate', source='English', target='Chinese',
        overall_deadline=30)
    assert out == token
    assert calls['n'] == 0


# ── (a) no translatable letters ──────────────────────────────────────────

@pytest.mark.parametrize('symbols', [
    '=== 12345 === 67.89 ===',
    '>>> --- +++ *** ///',
])
def test_pure_symbols_accepted_verbatim(monkeypatch, symbols):
    assert not _engine._HAS_LETTER_RE.search(symbols)
    calls = _patch_common(monkeypatch, symbols)
    out, _u = engine._translate_one_chunk(
        symbols, system_prompt='translate', source='English', target='Chinese',
        overall_deadline=30)
    assert out == symbols
    assert calls['n'] == 0


def test_numbers_and_punctuation_only(monkeypatch):
    content = '2026-07-14 18:01:58 (99.5%) [#42]'
    assert not _engine._HAS_LETTER_RE.search(content)
    calls = _patch_common(monkeypatch, content)
    out, _u = engine._translate_one_chunk(
        content, system_prompt='translate', source='English', target='Chinese',
        overall_deadline=30)
    assert out == content
    assert calls['n'] == 0


# ── (c) already in the target language ───────────────────────────────────

_CHINESE = '我是由月之暗面开发的人工智能助手，有什么可以帮你的吗？这里没有任何拉丁字母。'


def test_already_chinese_target_chinese_short_circuits(monkeypatch):
    calls = _patch_common(monkeypatch, _CHINESE)
    out, _u = engine._translate_one_chunk(
        _CHINESE, system_prompt='translate', source='Chinese', target='Chinese',
        overall_deadline=30)
    assert out == _CHINESE
    assert calls['n'] == 0


def test_already_english_target_english_short_circuits(monkeypatch):
    english = ('This is a perfectly ordinary English paragraph that is already '
               'in the target language and needs no translation at all.')
    calls = _patch_common(monkeypatch, english)
    out, _u = engine._translate_one_chunk(
        english, system_prompt='translate', source='English', target='English',
        overall_deadline=30)
    assert out == english
    assert calls['n'] == 0


# ── Negative controls: genuinely-translatable content still runs the loop ─

def test_genuine_english_to_chinese_echo_still_flagged(monkeypatch):
    # Source English, target Chinese, model echoes → a GENUINE no-op. Must NOT
    # be short-circuited (it carries translatable letters and is not a path):
    # it enters the retry loop, exhausts it, and raises. This is the behaviour
    # the fix must PRESERVE — proving the short-circuit didn't swallow real
    # failures.
    english = ('This is a sufficiently long English sentence that should be '
               'translated into Chinese but the cheap model just echoed it.')
    calls = _patch_common(monkeypatch, english)
    with pytest.raises(ValueError):
        engine._translate_one_chunk(
            english, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    assert calls['n'] >= 2                        # entered the retry loop


def test_mixed_bilingual_is_not_identity_invariant(monkeypatch):
    # A mixed EN+ZH message translating to Chinese still needs the model for
    # its English half — it must NOT be short-circuited. The model returns a
    # proper Chinese translation which is accepted normally (>=1 call).
    mixed = ('Please 帮我预约 an appointment 明天上午 at the hospital 好吗？'
             ' The doctor 王芳 recommended it.')
    inv, _reason = _engine._is_identity_invariant(mixed.strip(), 'Chinese')
    assert inv is False
    good_zh = '请帮我预约明天上午医院的门诊好吗？王芳医生推荐的。'
    calls = _patch_common(monkeypatch, good_zh)
    out, _u = engine._translate_one_chunk(
        mixed, system_prompt='translate', source='English', target='Chinese',
        overall_deadline=30)
    assert out == good_zh.strip()
    assert calls['n'] == 1                        # translated, not short-circuited


def test_path_with_prose_is_not_a_bare_token(monkeypatch):
    # A sentence that MENTIONS a path is real prose — must be translated, not
    # short-circuited (it has internal whitespace, so it's not a lone token).
    prose = 'The config lives at /etc/tofu/config.json and must be readable.'
    inv, _reason = _engine._is_identity_invariant(prose.strip(), 'Chinese')
    assert inv is False


# ── Load-bearing NEUTER control ──────────────────────────────────────────

def test_NEUTER_without_short_circuit_path_burns_retries_and_raises(monkeypatch):
    # Proves the short-circuit is load-bearing: force _is_identity_invariant to
    # never fire, then feed the reported absolute path. Without the guard the
    # model echoes the path → the no-op detector treats it as failure → the
    # engine burns the retry budget and RAISES (the reported bug behaviour).
    monkeypatch.setattr(_engine, '_is_identity_invariant',
                        lambda *_a, **_k: (False, ''))
    calls = _patch_common(monkeypatch, _ABS_PATH)
    with pytest.raises(ValueError):
        engine._translate_one_chunk(
            _ABS_PATH, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    assert calls['n'] >= 2                        # the retry storm we fixed
