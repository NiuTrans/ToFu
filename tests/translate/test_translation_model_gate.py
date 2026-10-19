"""Demand-driven provider-call ownership across translation carriers."""

from __future__ import annotations

import threading
import time

import pytest

from lib.translate.model_gate import TranslationModelGate


pytestmark = pytest.mark.unit


def _wait_for(predicate, *, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail('condition did not become true before timeout')


def _gate(capacity: int) -> TranslationModelGate:
    return TranslationModelGate(metric_pool='test-translation-provider')
















def test_sync_route_projects_no_admissible_provider_as_retryable_503(monkeypatch):
    import asyncio

    from lib.api_keys import local_admin_context
    from lib.translate.errors import TranslationNoAdmissibleProvider
    from quart import Quart, g
    from routes.api_v1 import translate as translate_route

    def unavailable(*_args, **_kwargs):
        raise TranslationNoAdmissibleProvider()

    monkeypatch.setattr(translate_route, '_translate_freetext', unavailable)
    app = Quart(__name__)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(translate_route.api_v1_translate_bp)

    async def request_translation():
        async with app.test_client() as client:
            response = await client.post(
                '/api/v1/translate',
                json={
                    'text': 'Translate this paragraph.',
                    'targetLang': 'Chinese',
                },
            )
            return response.status_code, await response.get_json()

    status, body = asyncio.run(request_translation())
    assert status == 503
    assert body['error']['kind'] == 'no_slot'
    assert body['error']['retryable'] is True
    assert body['error']['context'] == 'translation:no_admissible_slot'


def test_background_no_admissible_provider_settles_as_no_slot(monkeypatch):
    from lib.translate.errors import TranslationNoAdmissibleProvider
    from lib.translate.runtime import _worker

    observed = {}

    def finish(task_id, **kwargs):
        observed['task_id'] = task_id
        observed.update(kwargs)

    monkeypatch.setattr(_worker._translate_runtime, 'finish', finish)
    _worker._settle_error(
        {'model': None},
        'translate-task',
        TranslationNoAdmissibleProvider(),
        '',
        '',
        '',
        'translatedContent',
    )

    assert observed['task_id'] == 'translate-task'
    assert observed['error']['kind'] == 'no_slot'
    assert observed['error']['retryable'] is True
    assert observed['error_context'] == 'translate'


def test_llm_translation_dispatch_enters_shared_gate(monkeypatch):
    import lib.translate.engine as engine
    from lib.translate import model_gate

    gate = _gate(1)
    monkeypatch.setattr(model_gate, '_translation_model_gate', gate)
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *_a, **_k: None)
    monkeypatch.setattr(engine.translate_cache, 'put', lambda *_a, **_k: None)
    monkeypatch.setattr(engine.translate_refusal, 'get', lambda *_a, **_k: None)

    def fake_smart_chat(*_args, **_kwargs):
        assert gate.snapshot()['active'] == 1
        return (
            '这是一个清晰且完整的句子，需要被准确地翻译成中文。',
            {'finish_reason': 'stop',
             '_dispatch': {'model': 'translator', 'key': 'key'}},
        )

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)
    translated, _usage = engine._translate_one_chunk(
        'This is one clear sentence that needs an accurate translation.',
        system_prompt='translate',
        source='English',
        target='Chinese',
        overall_deadline=10,
    )

    assert translated.startswith('这是一个')
    assert gate.snapshot()['acquired'] == 1
    assert gate.snapshot()['active'] == 0


def test_cache_hit_uses_no_provider_slot(monkeypatch):
    import lib.translate.engine as engine
    from lib.translate import model_gate

    gate = _gate(1)
    monkeypatch.setattr(model_gate, '_translation_model_gate', gate)
    monkeypatch.setattr(
        engine.translate_cache,
        'get',
        lambda *_a, **_k: {'translated': '缓存译文', 'model': 'cache-model'},
    )

    translated, usage = engine._translate_one_chunk(
        'A source sentence long enough to translate.',
        system_prompt='translate', source='English', target='Chinese')

    assert translated == '缓存译文'
    assert usage['_cache_hit'] is True
    assert gate.snapshot()['acquired'] == 0


def test_machine_translation_enters_shared_gate(monkeypatch):
    import lib.translate.engine as engine
    from lib.translate import model_gate

    gate = _gate(1)
    monkeypatch.setattr(model_gate, '_translation_model_gate', gate)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *_a, **_k: None)
    monkeypatch.setattr(engine.translate_cache, 'put', lambda *_a, **_k: None)
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: True)

    def fake_mt(*_args, **_kwargs):
        assert gate.snapshot()['active'] == 1
        return '机器翻译结果'

    monkeypatch.setattr('lib.mt_provider.mt_translate_chunked', fake_mt)
    translated, usage = engine._translate_one_chunk(
        'A source sentence long enough to translate.',
        system_prompt='translate', source='English', target='Chinese')

    assert translated == '机器翻译结果'
    assert usage['model'] == 'mt:niutrans'
    assert gate.snapshot()['acquired'] == 1


def test_streaming_translation_forwards_strict_model_policy(monkeypatch):
    """Paper translation can reuse the shared guards without losing its pin."""
    from lib import key_stats
    import lib.translate.engine as engine

    observed = {}
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *_a, **_k: None)
    monkeypatch.setattr(engine.translate_cache, 'put', lambda *_a, **_k: None)
    monkeypatch.setattr(engine.translate_refusal, 'get', lambda *_a, **_k: None)

    def fake_dispatch_stream(messages, **kwargs):
        assert key_stats.is_strict_billing_stop_admission() is True
        observed['messages'] = messages
        observed.update(kwargs)
        return (
            {
                'role': 'assistant',
                'content': '这是一个清晰、完整且严格限定模型的中文译文。',
            },
            'stop',
            {'_dispatch': {'model': 'model-a', 'key': 'key-a'}},
        )

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', fake_dispatch_stream)

    translated, usage = engine._translate_one_chunk(
        'This is one clear and complete sentence for a strictly pinned model.',
        system_prompt='translate into Chinese',
        source='English',
        target='Chinese',
        overall_deadline=10,
        prefer_model='model-a',
        strict_model=True,
        allow_mt=False,
        stream=True,
        capability='text',
        temperature=0,
        accept_truncated=False,
    )

    assert translated.endswith('。')
    assert usage['_dispatch']['model'] == 'model-a'
    assert observed['prefer_model'] == 'model-a'
    assert observed['strict_model'] is True
    assert observed['capability'] == 'text'
    assert observed['temperature'] == 0
    assert callable(observed['abort_check'])
    assert key_stats.is_strict_billing_stop_admission() is False


def test_independent_provider_burst_enters_without_count_permits():
    gate = _gate(1)
    release = threading.Event()
    errors = []
    def run():
        try:
            with gate.slot():
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
    threads = [threading.Thread(target=run) for _ in range(64)]
    try:
        for thread in threads:
            thread.start()
        _wait_for(lambda: gate.snapshot()['active'] == 64, timeout=3)
        assert gate.snapshot()['waiting'] == gate.snapshot()['rejectedWaits'] == 0
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)
                assert not thread.is_alive()
    assert not errors
    assert gate.snapshot()['acquired'] == gate.snapshot()['peakActive'] == 64
    assert gate.snapshot()['active'] == 0


def test_abort_and_body_failure_leave_no_live_provider_accounting():
    from lib.llm.llm_errors import AbortedError
    gate = _gate(1)
    with pytest.raises(AbortedError):
        with gate.slot(abort_check=lambda: True):
            pytest.fail('aborted call entered provider')
    assert gate.snapshot()['active'] == gate.snapshot()['acquired'] == 0
    assert gate.snapshot()['cancelledWaits'] == 1
    with pytest.raises(RuntimeError, match='actual provider failure'):
        with gate.slot():
            raise RuntimeError('actual provider failure')
    assert gate.snapshot()['active'] == 0
    assert gate.snapshot()['acquired'] == 1


def test_observability_failure_cannot_deny_or_leak_provider_call(monkeypatch):
    from lib.translate import model_gate
    def fail(*args, **kwargs):
        raise RuntimeError('metric unavailable')
    monkeypatch.setattr(model_gate, 'publish_executor_state', fail)
    gate = _gate(1)
    with gate.slot():
        assert gate.snapshot()['active'] == 1
    assert gate.snapshot()['active'] == 0
