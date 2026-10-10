"""Regression: documented ``<think>`` tags must not truncate translations.

Cheap MiniMax-style translators wrap *their own* reasoning in
``<think>...</think>``. The engine used to slice from the first unclosed
``<think>`` to EOF, so a source document that *mentioned* the tag (API
tables, inline code such as `` `<think>` 全文 ``) lost everything after
that mention — including the rest of a markdown table.

These tests patch LLM dispatch + MT + cache so they run offline.
Run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

import pytest

import lib.translate.engine as engine
import lib.translate.engine._engine as _engine
from lib.translate.notranslate import (
    _extract_notranslate_blocks,
    _reattach_notranslate_blocks,
)


pytestmark = pytest.mark.unit


# The user-visible failure: a comparison table that documents the tag.
_TRANSLATED_WITH_MENTION = (
    '## 实操对照\n'
    '\n'
    '| 你想拿的东西 | Grok 4.6 有没有 |\n'
    '| --- | --- |\n'
    '| 完整原始 thinking / `<think>` 全文 | 没有 |\n'
    '| 可读思考摘要 `reasoning_content` | 有（Responses / xAI SDK；4.5 没有） |\n'
    '| Chat Completions 的思考字段 | 官方说没有 |\n'
    '| 加密 thinking 续会话 | 有 |\n'
    '\n'
    '所以：说「完全没有任何思考输出」——不成立。\n'
)

_EN_SOURCE = (
    '## Hands-on comparison\n'
    '\n'
    '| What you want | Does Grok 4.6 have it |\n'
    '| --- | --- |\n'
    '| Full original thinking / `<think>` text | No |\n'
    '| Readable summary `reasoning_content` | Yes (Responses / xAI SDK; 4.5 no) |\n'
    '| Chat Completions thinking field | Officially no |\n'
    '| Encrypted thinking continuation | Yes |\n'
    '\n'
    'So: "there is no thinking output at all" is false.\n'
)


def _patch_common(monkeypatch, reply):
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


def test_strip_keeps_mid_document_think_mention():
    out = _engine._strip_model_thinking(_TRANSLATED_WITH_MENTION)
    assert '`<think>` 全文' in out
    assert '可读思考摘要' in out
    assert '加密 thinking 续会话' in out
    assert '不成立' in out


def test_strip_peels_leading_minimax_wrapper():
    wrapped = '<think>I will translate the table.</think>\n' + _TRANSLATED_WITH_MENTION
    out = _engine._strip_model_thinking(wrapped)
    assert out == _TRANSLATED_WITH_MENTION
    assert '<think>I will translate' not in out
    assert '`<think>` 全文' in out


def test_strip_peels_multiple_leading_wrappers():
    wrapped = (
        '<think>first</think>\n'
        '<think>second</think>\n'
        '完整译文。'
    )
    assert _engine._strip_model_thinking(wrapped) == '完整译文。'


def test_strip_unclosed_leading_think_is_empty():
    assert _engine._strip_model_thinking('<think>still reasoning about the table') == ''


def test_extract_protects_literal_think_tags():
    source = '完整原始 thinking / `<think>` 全文 | 没有'
    body, blocks = _extract_notranslate_blocks(source)
    assert '<think>' not in body
    assert [b['content'] for b in blocks] == ['<think>']
    assert [b['kind'] for b in blocks] == ['notranslate']
    restored = _reattach_notranslate_blocks(body, blocks)
    assert restored == source


def test_engine_keeps_table_after_documented_think_tag(monkeypatch):
    calls = _patch_common(monkeypatch, _TRANSLATED_WITH_MENTION)
    out, usage = engine._translate_one_chunk(
        _EN_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert calls['n'] == 1
    assert '`<think>` 全文' in out
    assert '可读思考摘要 `reasoning_content`' in out
    assert '官方说没有' in out
    assert '不成立' in out
    tr = usage.get('_translate_trace')
    assert tr and tr['verdict'] == 'ok'


def test_engine_strips_leading_wrapper_then_keeps_mention(monkeypatch):
    wrapped = '<think>plan the translation</think>\n' + _TRANSLATED_WITH_MENTION
    _patch_common(monkeypatch, wrapped)
    out, _u = engine._translate_one_chunk(
        _EN_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30)
    assert out.startswith('## 实操对照')
    assert '`<think>` 全文' in out
    assert 'plan the translation' not in out
