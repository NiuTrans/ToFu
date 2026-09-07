"""tests/test_translate_content_refused.py — pt_75d8f8c7.

POST /api/v1/translate returned a bare 500 ('INTERNAL SERVER ERROR') 73×/day
when the engine's content-quality guards (wrong-language flip / no-op echo /
over-generated contamination) exhausted their retry budget. The engine KNEW
exactly why it refused — that information died inside a generic ValueError.

The fix: the guards raise ``TranslationContentRefused`` (a ValueError
subclass carrying the machine-readable verdict); the v1 sync route catches it
BEFORE the generic handler and answers 502 + a typed ``content_refused``
envelope; the frontend renders the localized title/hint via the existing
error-envelope vertical (constants → i18n keys → ERROR_KIND_LABELS).

Failing-first: the typed-refusal tests fail on the old shape (plain
ValueError from the trailing emptiness check). Each guard also carries a
byte-reverting NEUTER.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_translate_content_refused.py -v
"""

from __future__ import annotations

import os

import pytest

import lib.translate.engine as engine
from lib.error_envelope import from_exception, make_envelope
from lib.error_envelope._constants import _RETRYABLE_KINDS, _TITLES, KINDS
from lib.translate import TranslationContentRefused

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

# ── Shared fixtures (same shapes as test_translate_wrong_language_flip) ──

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

    # The engine resolves smart_chat via ``from lib.llm_dispatch import
    # smart_chat`` INSIDE _translate_one_chunk, so patching the facade package
    # attr is sufficient — here. Bind the fake into EVERY namespace it could
    # resolve from (facade, api submodule, the engine module itself in case
    # the import was hoisted to module scope by the export transform chain),
    # or a tree where the lazy-import assumption fails silently makes REAL
    # provider calls (measured 2026-08-05 on public CI: the chunk burned 32s
    # of 401 retries against the conftest placeholder key instead of using
    # this fake).
    monkeypatch.setattr('lib.llm_dispatch.smart_chat', _fake_smart_chat)
    monkeypatch.setattr('lib.llm_dispatch.api.smart_chat', _fake_smart_chat,
                        raising=False)
    monkeypatch.setattr('lib.translate.engine._engine.smart_chat',
                        _fake_smart_chat, raising=False)
    return state


# ── 1. Engine: typed refusal on every guard's give-up ─────────────────────

def test_persistent_flip_raises_typed_refusal(monkeypatch):
    """Every model flips to English → after the budget the engine raises
    TranslationContentRefused(verdict='wrong_language'), NOT a plain
    ValueError. Failing-first: the old shape raises the trailing ValueError,
    which is NOT a TranslationContentRefused."""
    _patch_sequence(monkeypatch, [_FLIPPED_EN])
    with pytest.raises(TranslationContentRefused) as exc_info:
        engine._translate_one_chunk(
            _MIXED_SOURCE, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    assert exc_info.value.verdict == 'wrong_language'
    assert exc_info.value.content_fails >= 2


def test_noop_echo_raises_typed_refusal(monkeypatch):
    """Model echoes the input verbatim every time → typed refusal with
    verdict='noop'."""
    english = ('This is a sufficiently long English paragraph that must be '
               'echoed verbatim by the stubborn fake model every single time.')
    _patch_sequence(monkeypatch, [english])  # output == input
    with pytest.raises(TranslationContentRefused) as exc_info:
        engine._translate_one_chunk(
            english, system_prompt='translate', source='English',
            target='Chinese', overall_deadline=30)
    assert exc_info.value.verdict == 'noop'


def test_overgen_raises_typed_refusal(monkeypatch):
    """Model translates then appends a huge hallucinated block every time →
    typed refusal with verdict='over_generated'."""
    short = 'Translate this short sentence please.'
    bloated = short + '\n\n' + ('Unrelated training-corpus prose. ' * 60)
    assert len(bloated) > len(short) * 8
    _patch_sequence(monkeypatch, [bloated])
    with pytest.raises(TranslationContentRefused) as exc_info:
        engine._translate_one_chunk(
            short, system_prompt='translate', source='English',
            target='French', overall_deadline=30)
    assert exc_info.value.verdict == 'over_generated'


# ── 2. Envelope: content_refused kind is fully registered ─────────────────

def test_content_refused_kind_registered():
    """The kind must exist in the closed enum, carry bilingual titles, and be
    retryable (a fresh call can land a healthy model — the exclusion list was
    per-call). NEUTER: deleting any of these pins fails this test."""
    assert 'content_refused' in KINDS
    cn_title, en_title, cn_hint, en_hint = _TITLES['content_refused']
    assert cn_title and en_title and cn_hint and en_hint
    assert 'content_refused' in _RETRYABLE_KINDS
    env = make_envelope('content_refused')
    assert env['kind'] == 'content_refused'
    assert env['retryable'] is True
    assert env['titleKey'] == 'err.k.content_refused.title'
    assert env['hintKey'] == 'err.k.content_refused.hint'


def test_from_exception_uses_verdict_detail():
    """The envelope built from the typed exception carries the real reason in
    detail (the frontend shows it in the expandable detail block)."""
    exc = TranslationContentRefused('wrong_language', 'out latin-dominant',
                                    attempts=3, content_fails=3)
    env = from_exception(exc, kind='content_refused',
                         source='api_v1.translate.sync')
    assert env['kind'] == 'content_refused'
    assert 'wrong_language' in env['detail']
    assert env['source'] == 'api_v1.translate.sync'


# ── 3. Route: typed refusal is a user-visible 502 envelope ───────────────

def test_route_returns_502_for_content_refusal(monkeypatch):
    """Exercise the registered route; catch order is observable as 502 vs 500."""
    import asyncio

    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from routes.api_v1 import translate as translate_route

    def refuse(*_args, **_kwargs):
        raise TranslationContentRefused(
            'wrong_language', 'latin-dominant output',
            attempts=3, content_fails=3,
        )

    monkeypatch.setattr(translate_route, '_translate_freetext', refuse)
    app = Quart(__name__)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(translate_route.api_v1_translate_bp)

    async def _request():
        async with app.test_client() as client:
            response = await client.post(
                '/api/v1/translate',
                json={'text': 'Translate this paragraph.',
                      'targetLang': 'Chinese'},
            )
            return response.status_code, await response.get_json()

    status, body = asyncio.run(_request())
    assert status == 502
    assert body['ok'] is False
    assert body['error']['kind'] == 'content_refused'
    assert body['error']['retryable'] is True
    assert 'wrong_language' in body['error']['detail']


# ── 4. Frontend vertical: i18n keys + kind label (source-scan guard) ──────

def _frontend_missing(i18n_src: str, labels_src: str) -> list[str]:
    out = []
    for key in ('err.k.content_refused.chip',
                'err.k.content_refused.title',
                'err.k.content_refused.hint'):
        if key not in i18n_src:
            out.append(f'i18n.js missing {key}')
    if 'content_refused' not in labels_src:
        out.append('error-presentation.ts ERROR_KIND_LABELS missing content_refused')
    return out


def test_frontend_vertical_keys_present():
    with open(os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales', 'zh.json'),
              encoding='utf-8') as f:
        i18n_src = f.read()
    with open(os.path.join(ROOT, 'frontend', 'src', 'error-presentation.ts'),
              encoding='utf-8') as f:
        labels_src = f.read()
    missing = _frontend_missing(i18n_src, labels_src)
    assert not missing, 'frontend vertical incomplete:\n  ' + '\n  '.join(missing)
    # NEUTER (byte-reverting): the same scanner must fire when the keys are
    # absent — proves it is not a vacuous pass.
    neutered_i18n = i18n_src.replace('err.k.content_refused.', 'err.k.REMOVED.', 3)
    assert _frontend_missing(neutered_i18n, labels_src), (
        'NEUTER FAILED: scanner did not fire on stripped i18n keys')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
