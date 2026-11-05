"""An authority refusal at dispatch is terminal: no key, slot or model rotation.

Incident 2026-10-29 (coordinator-1a390889, task 8de735b0): the Coordinator
request account refused the first dispatch before any provider was contacted.
Dispatch treated the refusal as a stream failure, charged it to three healthy
keys of the selected model and moved on, and the fallback layer then tried
kimi-k3, so the turn reported six failed provider requests. A typed
``ModelAdmissionRefused`` is an ``AbortedError``: dispatch releases its slot
and stops at the first attempt.
"""
import pytest

import lib.llm_dispatch.api as api
from lib.llm.llm_errors import ModelAdmissionRefused
from lib.llm_dispatch.provider_pin import provider_pin

pytestmark = pytest.mark.unit

_PIN = 'model-route:7:test'
_MESSAGES = [{'role': 'user', 'content': 'hi'}]


def _slot(key, model):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-' + key, model=model,
                logical_model=model, provider_id=_PIN,
                routing_provider_id='gateway', route_rank=0,
                capabilities={'text'})


@pytest.fixture
def slots(monkeypatch):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.model_routing.health import reset_owner_health_registries
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    reset_owner_health_registries()
    dispatcher = LLMDispatcher()
    dispatcher.slots = [_slot('k1', 'opus'), _slot('k2', 'opus'), _slot('k3', 'kimi')]
    dispatcher._initialized = True
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    yield dispatcher.slots
    reset_owner_health_registries()


def test_refused_dispatch_tries_no_other_key_or_model(slots, monkeypatch):
    calls, retries = [], []

    def refuse(body, **kwargs):
        calls.append((kwargs.get('api_key'), body.get('model')))
        raise ModelAdmissionRefused('Coordinator request authority ended: source changed')

    monkeypatch.setattr('lib.llm.stream_chat', refuse)
    with provider_pin(_PIN), pytest.raises(ModelAdmissionRefused):
        api.dispatch_stream(_MESSAGES, prefer_model='opus', log_prefix='[T]',
                            on_retry=lambda **kw: retries.append(kw))

    assert len(calls) == 1 and retries == []
    assert all((slot.consecutive_errors, slot.total_errors, slot.inflight) == (0, 0, 0)
               for slot in slots)
