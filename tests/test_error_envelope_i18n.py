"""Typed error envelopes stay in parity with Vite-owned locale chunks."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'frontend/src/runtime/app-runtime.js'
LOCALES = {
    language: json.loads(
        (ROOT / f'frontend/src/i18n/locales/{language}.json').read_text(encoding='utf-8')
    )
    for language in ('zh', 'en')
}


def _expected_legacy_message(kind: str, model: str = '') -> str:
    from lib.error_envelope._constants import _TITLES
    cn_title, en_title, _cn_hint, _en_hint = _TITLES[kind]
    if model:
        return f'{cn_title}（模型：{model}）\n{en_title} (model: {model})'
    return f'{cn_title}\n{en_title}'


def _expected_legacy_hint(kind: str) -> str:
    from lib.error_envelope._constants import _TITLES
    _cn_title, _en_title, cn_hint, en_hint = _TITLES[kind]
    if cn_hint and en_hint:
        return f'解决办法 / How to fix:\n{cn_hint}\n\n{en_hint}'
    return cn_hint or en_hint


def test_every_kind_ships_title_and_hint_keys():
    from lib.error_envelope import KINDS, make_envelope
    assert len(KINDS) > 15
    for kind in sorted(KINDS):
        envelope = make_envelope(kind)
        assert envelope.get('titleKey') == f'err.k.{kind}.title'
        assert envelope.get('hintKey') == f'err.k.{kind}.hint'


def test_legacy_bilingual_fields_remain_byte_identical():
    from lib.error_envelope import KINDS, make_envelope
    for kind in sorted(KINDS):
        envelope = make_envelope(kind)
        assert envelope['message'] == _expected_legacy_message(kind), kind
        assert envelope['hint'] == _expected_legacy_hint(kind), kind


def test_model_suffix_stays_in_legacy_message_only():
    from lib.error_envelope import make_envelope
    envelope = make_envelope('endpoint_unreachable', model='kimi-k3')
    assert envelope['message'] == _expected_legacy_message(
        'endpoint_unreachable', model='kimi-k3')
    assert envelope['titleKey'] == 'err.k.endpoint_unreachable.title'


def test_custom_message_and_hint_do_not_claim_default_translation_keys():
    from lib.error_envelope import make_envelope
    message = make_envelope('generic', message='Totally custom text')
    hint = make_envelope('generic', hint='Custom bilingual hint')
    assert 'titleKey' not in message
    assert message['message'] == 'Totally custom text'
    assert 'hintKey' not in hint
    assert hint['hint'] == 'Custom bilingual hint'


def test_explicit_custom_hint_key_is_preserved():
    from lib.error_envelope import make_envelope
    envelope = make_envelope(
        'invalid_image', hint='解决办法 / How to fix:\n• x\n\n• y',
        hint_key='err.k.invalid_image.hintSize',
    )
    assert envelope['hintKey'] == 'err.k.invalid_image.hintSize'


def test_unknown_kind_downgrades_to_generic_keys():
    from lib.error_envelope import make_envelope
    envelope = make_envelope('rateLimit')
    assert envelope['kind'] == 'generic'
    assert envelope['titleKey'] == 'err.k.generic.title'
    assert envelope['hintKey'] == 'err.k.generic.hint'


def test_endpoint_exception_carries_localizable_keys():
    from lib.error_envelope import from_exception
    from lib.llm_errors import EndpointUnreachableError
    envelope = from_exception(
        EndpointUnreachableError("All endpoints for model 'kimi-k3' are unreachable"),
        model='kimi-k3', context='no-fallback', source='llm-stream',
    )
    assert envelope['kind'] == 'endpoint_unreachable'
    assert envelope['titleKey'] == 'err.k.endpoint_unreachable.title'
    assert envelope['hintKey'] == 'err.k.endpoint_unreachable.hint'


def test_invalid_image_call_sites_pair_custom_hints_with_keys():
    source = (ROOT / 'lib/tasks_pkg/llm_fallback/_call.py').read_text(encoding='utf-8')
    assert "_hint_key = 'err.k.invalid_image.hintMany'" in source
    assert "_hint_key = 'err.k.invalid_image.hintSize'" in source
    assert 'hint_key=_hint_key,' in source


def test_every_error_kind_exists_in_both_locale_chunks():
    from lib.error_envelope import KINDS
    for kind in sorted(KINDS):
        for suffix in ('chip', 'title', 'hint'):
            key = f'err.k.{kind}.{suffix}'
            assert key in LOCALES['zh'], key
            assert key in LOCALES['en'], key


def test_locale_titles_and_hints_match_the_python_source_of_truth():
    from lib.error_envelope._constants import _TITLES
    for kind, (zh_title, en_title, zh_hint, en_hint) in _TITLES.items():
        assert LOCALES['zh'][f'err.k.{kind}.title'] == zh_title, kind
        assert LOCALES['en'][f'err.k.{kind}.title'] == en_title, kind
        assert LOCALES['zh'][f'err.k.{kind}.hint'] == zh_hint, kind
        assert LOCALES['en'][f'err.k.{kind}.hint'] == en_hint, kind


def test_english_chips_match_the_runtime_fallback_labels():
    source = RUNTIME.read_text(encoding='utf-8')
    match = re.search(r'const ERROR_KIND_LABELS = \{(.*?)\n\};', source, re.S)
    assert match
    labels = dict(re.findall(r"^\s*([a-z_]+):\s*'([^']*)'", match.group(1), re.M))
    from lib.error_envelope import KINDS
    assert set(KINDS).issubset(labels)
    for kind in KINDS:
        assert LOCALES['en'][f'err.k.{kind}.chip'] == labels[kind], kind


def test_shared_fragments_and_invalid_image_variants_are_complete():
    assert LOCALES['zh']['err.k._howToFix'] == '解决办法：'
    assert LOCALES['en']['err.k._howToFix'] == 'How to fix:'
    for language in ('zh', 'en'):
        assert '{model}' in LOCALES[language]['err.k._modelSuffix']
        assert LOCALES[language]['err.k.invalid_image.hintMany'].startswith('• ')
        assert LOCALES[language]['err.k.invalid_image.hintSize'].startswith('• ')


def test_error_renderer_reads_the_vite_translator_without_classic_globals():
    source = RUNTIME.read_text(encoding='utf-8')
    assert source.count('migrated source: core/error_envelope.js') == 1
    owner = source[source.index('function _envResolveI18n'):]
    owner = owner[:owner.index('/* ===== migrated source:', 1)]
    assert 'const text = t(key, params);' in owner
    assert 'return text === key ? null : text;' in owner
    assert '_i18n[' not in owner
    assert not (ROOT / 'static/js').exists()
