"""Short display strings reuse settled-turn segment enrichment in one bounded pass."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_TRANSLATIONS = {
    'Fix folder creation in the sidebar': '修复侧边栏的文件夹创建',
    'Merged and verified.': '已合并并验证。',
    'Research New Year homestays': '调研元旦民宿',
}


@pytest.fixture
def provider(monkeypatch):
    """Record provider requests at the engine seam; a frozen clock makes the pass budget exact."""
    from lib.translate.runtime import _segments as segments_mod
    requests = []

    def translate(text, prompt, **kwargs):
        requests.append({'text': text, 'prompt': prompt, **kwargs})
        for source, translated in _TRANSLATIONS.items():
            text = text.replace(source, translated)
        return text, {}

    monkeypatch.setattr(segments_mod, '_translate_freetext', translate)
    monkeypatch.setattr(segments_mod.translate_cache, 'get', lambda *args, **kwargs: None)
    monkeypatch.setattr(segments_mod, 'time', SimpleNamespace(monotonic=lambda: 1000.0))
    return requests


def test_display_texts_share_one_enrichment_request_and_its_policy(provider, monkeypatch):
    from lib.translate import policy
    from lib.translate.display_texts import translate_display_texts
    from lib.translate.prompt import _build_translate_prompt
    from lib.translate.runtime._segments import _segment_enrichment_deadline_seconds

    monkeypatch.setattr(policy, 'translation_max_429_attempts', lambda: 5)
    texts = [*_TRANSLATIONS, 'Merged and verified.']
    assert translate_display_texts(texts, 'Chinese', log_tag='board') == _TRANSLATIONS
    assert len(provider) == 1
    request = provider[0]
    assert all(source in request['text'] for source in _TRANSLATIONS)
    assert request['prompt'] == _build_translate_prompt('Chinese', 'English')
    assert (request['source'], request['target']) == ('English', 'Chinese')
    assert request['max_429_attempts'] == 5 and request['defer_on_shared_contention'] is True
    assert request['overall_deadline'] == pytest.approx(
        _segment_enrichment_deadline_seconds(sum(len(text) for text in _TRANSLATIONS)))


def test_text_already_in_the_target_language_costs_no_provider_call(provider, monkeypatch):
    import lib.text_lang
    from lib.translate.display_texts import translate_display_texts

    assert translate_display_texts(['文件夹修复已验证', 'Merged and verified.'], 'Chinese', log_tag='board') == {
        '文件夹修复已验证': '文件夹修复已验证', 'Merged and verified.': '已合并并验证。'}
    assert [request['text'] for request in provider] == ['Merged and verified.']
    # The general skip gate covers other targets too (statistical detector absent).
    monkeypatch.setattr(lib.text_lang, '_get_ft_detector', lambda force=False: None)
    assert translate_display_texts(['Merged and verified.'], 'English', log_tag='board') == {
        'Merged and verified.': 'Merged and verified.'}
    assert len(provider) == 1


def test_cached_text_needs_no_provider_call(provider, monkeypatch):
    from lib.translate.display_texts import translate_display_texts
    from lib.translate.runtime import _segments as segments_mod

    monkeypatch.setattr(segments_mod.translate_cache, 'get', lambda text, source, target: (
        {'translated': '已缓存的译文'} if (text, source, target) == ('Merged and verified.', 'English', 'Chinese')
        else None))
    assert translate_display_texts(['Merged and verified.'], 'Chinese', log_tag='board') == {
        'Merged and verified.': '已缓存的译文'}
    assert provider == []


def test_a_failed_request_leaves_its_texts_out(provider, monkeypatch):
    from lib.translate.display_texts import translate_display_texts
    from lib.translate.runtime import _segments as segments_mod

    def unavailable(text, prompt, **kwargs):
        raise RuntimeError('translation provider unavailable')

    monkeypatch.setattr(segments_mod, '_translate_freetext', unavailable)
    assert translate_display_texts(['Merged and verified.', '文件夹修复已验证'], 'Chinese', log_tag='board') == {
        '文件夹修复已验证': '文件夹修复已验证'}


def test_abort_stops_the_pass_before_any_request(provider):
    from lib.llm import AbortedError
    from lib.translate.display_texts import translate_display_texts

    with pytest.raises(AbortedError):
        translate_display_texts(['Merged and verified.'], 'Chinese', log_tag='board', abort_check=lambda: True)
    assert provider == []
