"""Hard provider-concurrency contracts for every translation carrier."""

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
    return TranslationModelGate(
        capacity,
        waiting_capacity=8,
        cancellation_poll_seconds=0.01,
        metric_pool='test-translation-provider',
    )


def test_gate_enforces_hard_active_bound_under_pressure():
    gate = _gate(2)
    release = threading.Event()
    entered = []
    entered_lock = threading.Lock()

    def worker(index):
        with gate.slot():
            with entered_lock:
                entered.append(index)
            assert release.wait(2)

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(6)]
    for thread in threads:
        thread.start()

    _wait_for(lambda: gate.snapshot()['active'] == 2
              and gate.snapshot()['waiting'] == 4)
    assert len(entered) == 2
    assert gate.snapshot()['peakActive'] == 2

    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    snapshot = gate.snapshot()
    assert snapshot['active'] == 0
    assert snapshot['waiting'] == 0
    assert snapshot['acquired'] == 6
    assert snapshot['peakActive'] == 2


def test_gate_admits_waiters_fifo():
    gate = _gate(1)
    order = []
    threads = []

    def worker(label):
        with gate.slot():
            order.append(label)

    with gate.slot():
        for expected_waiters, label in enumerate(('a', 'b', 'c'), start=1):
            thread = threading.Thread(target=worker, args=(label,))
            threads.append(thread)
            thread.start()
            _wait_for(
                lambda count=expected_waiters:
                gate.snapshot()['waiting'] == count)

    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert order == ['a', 'b', 'c']


def test_waiting_admission_is_cooperatively_cancellable():
    from lib.llm_errors import AbortedError

    gate = _gate(1)
    abort = threading.Event()
    observed = []

    def waiter():
        try:
            with gate.slot(abort_check=abort.is_set):
                observed.append('entered')
        except BaseException as exc:
            observed.append(exc)

    with gate.slot():
        thread = threading.Thread(target=waiter)
        thread.start()
        _wait_for(lambda: gate.snapshot()['waiting'] == 1)
        abort.set()
        thread.join(2)
        assert not thread.is_alive()
        assert len(observed) == 1
        assert isinstance(observed[0], AbortedError)
        snapshot = gate.snapshot()
        assert snapshot['active'] == 1
        assert snapshot['waiting'] == 0
        assert snapshot['cancelledWaits'] == 1

    assert gate.snapshot()['active'] == 0


def test_waiting_capacity_rejects_without_growing_or_dispatching():
    from lib.translate.errors import TranslationProviderQueueFull

    gate = TranslationModelGate(
        1,
        waiting_capacity=2,
        cancellation_poll_seconds=0.01,
        metric_pool='test-translation-provider',
    )
    release = threading.Event()
    entered = []
    threads = []

    def waiter(label):
        with gate.slot():
            entered.append(label)
            release.wait(2)

    with gate.slot():
        for label in ('a', 'b'):
            thread = threading.Thread(target=waiter, args=(label,))
            threads.append(thread)
            thread.start()
        _wait_for(lambda: gate.snapshot()['waiting'] == 2)

        started = time.monotonic()
        with pytest.raises(TranslationProviderQueueFull) as raised:
            with gate.slot():
                pytest.fail('saturated provider gate admitted extra work')
        assert time.monotonic() - started < 0.1
        assert raised.value.retryable is True
        assert gate.snapshot()['waiting'] == 2
        assert gate.snapshot()['waitingCapacity'] == 2
        assert gate.snapshot()['rejectedWaits'] == 1

    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert entered == ['a', 'b']


def test_provider_queue_saturation_is_not_retried_by_engine(monkeypatch):
    from lib.translate.engine import _engine as engine
    from lib.translate.errors import TranslationProviderQueueFull

    attempts = []
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *_a, **_k: None)

    def saturated(*_args, **_kwargs):
        attempts.append('dispatch')
        raise TranslationProviderQueueFull(capacity=1)

    monkeypatch.setattr(engine, '_dispatch_translation_candidate', saturated)

    with pytest.raises(TranslationProviderQueueFull):
        engine._translate_one_chunk(
            'A source sentence long enough to require real translation.',
            system_prompt='translate',
            source='English',
            target='Chinese',
            overall_deadline=5,
        )
    assert attempts == ['dispatch']


def test_sync_route_projects_provider_saturation_as_retryable_503(monkeypatch):
    import asyncio

    from lib.api_keys import local_admin_context
    from lib.translate.errors import TranslationProviderQueueFull
    from quart import Quart, g
    from routes.api_v1 import translate as translate_route

    def saturated(*_args, **_kwargs):
        raise TranslationProviderQueueFull(capacity=4)

    monkeypatch.setattr(translate_route, '_translate_freetext', saturated)
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
    assert body['error']['kind'] == 'server_busy'
    assert body['error']['retryable'] is True
    assert body['error']['context'] == 'translation:provider_queue_saturated'


def test_background_saturation_settles_as_retryable_server_busy(monkeypatch):
    from lib.translate.errors import TranslationProviderQueueFull
    from lib.translate.runtime import _worker

    observed = {}

    def finish(task_id, **kwargs):
        observed['task_id'] = task_id
        observed.update(kwargs)

    monkeypatch.setattr(_worker._translate_runtime, 'finish', finish)
    _worker._settle_error(
        {'model': None},
        'translate-task',
        TranslationProviderQueueFull(capacity=4),
        '',
        '',
        '',
        'translatedContent',
    )

    assert observed['task_id'] == 'translate-task'
    assert observed['error']['kind'] == 'server_busy'
    assert observed['error']['retryable'] is True
    assert observed['error_context'] == 'translate'


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
