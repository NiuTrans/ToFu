"""Encrypted Responses state binds dispatch failover to its minting Deployment.

2026-10-31 (task d9a651dc round 28): every round
of ``gpt-6.1-sol`` had been served by its Kunlun Deployment, whose credentials
rotated 2,039 times without harm. Two gateway 400s excluded two Kunlun pairs,
the third was cooling on a 429, and failover sent 27 rounds of replayed
reasoning to the model's public Deployment, which answered
``invalid_encrypted_content``. The pick also rebound conversation affinity, so
the follow-up turn failed the same way on its first request.

Pins (sync stream, async stream and non-stream chat loops, real dispatcher):
  * a request replaying state minted on Deployment K never reaches a Responses
    slot on another Deployment while a K slot is in scope: it rotates K's
    credentials, or fails with K's own error once K cannot serve;
  * unmarked state and an origin outside the requested model keep ordinary
    cross-Deployment failover;
  * a settled stream names its slot's Deployment on the encrypted state it
    returns, so the next request of the conversation is bound.
"""
from __future__ import annotations

import asyncio

import pytest

from lib.llm.llm_errors import BadRequestError
from lib.llm.responses_state_origin import STATE_ORIGIN_TYPE

pytestmark = pytest.mark.unit

MODEL = 'gpt-6.1-sol'
KUNLUN = 'deployment-kunlun'
PUBLIC = 'deployment-public'
MODES = ('stream', 'astream', 'chat')


def _slot(key, wire_model, deployment, *, latency, logical=MODEL):
    from lib.llm_dispatch.slot import Slot
    slot = Slot(key_name=key, api_key=f'sk-{key}', model=wire_model,
                logical_model=logical, capabilities={'text'},
                protocol='responses', route_deployment_id=deployment,
                route_credential_id=key.split(':')[0])
    slot.latency_ema = latency
    return slot


def _gpt_route_group():
    """The incident's candidates; without a binding the public slot is next."""
    return [
        _slot('cred-a:kunlun', 'kunlunbedrock.gpt-6.1-sol-m17', KUNLUN, latency=1.0),
        _slot('cred-p:public', MODEL, PUBLIC, latency=100.0),
        _slot('cred-b:kunlun', 'kunlunbedrock.gpt-6.1-sol-m17', KUNLUN, latency=500.0),
    ]


def _reasoning(cipher):
    return {'type': 'reasoning', 'id': 'rs-' + cipher, 'summary': [],
            'encrypted_content': cipher}


def _history(origin=KUNLUN):
    items = [_reasoning('minted-on-kunlun')]
    if origin:
        items.append({'type': STATE_ORIGIN_TYPE, 'version': 1,
                      'deployment_id': origin})
    return [
        {'role': 'user', 'content': 'Optimize the project'},
        {'role': 'assistant', 'content': 'Inspected the tree',
         '_responses_items': items},
        {'role': 'user', 'content': 'Continue'},
    ]


class _Transport:
    """Provider fake keyed by the selected slot; failures are planned per key."""

    def __init__(self, slots, failures):
        self.by_key = {slot.api_key: slot for slot in slots}
        self.failures = failures
        self.calls = []

    def _attempt(self, api_key):
        slot = self.by_key[api_key]
        self.calls.append(slot.key_name)
        failure = self.failures.get(slot.key_name)
        if failure is not None:
            raise failure
        return {'role': 'assistant', 'content': 'ok', '_responses_items': [
            _reasoning('minted-on-' + slot.route_deployment_id)]}

    def stream(self, body, **kwargs):
        return self._attempt(kwargs['api_key']), 'stop', {'completion_tokens': 1}

    async def astream(self, body, **kwargs):
        return self._attempt(kwargs['api_key']), 'stop', {'completion_tokens': 1}

    def chat(self, *args, **kwargs):
        self._attempt(kwargs['api_key'])
        return 'ok', {'completion_tokens': 1}


@pytest.fixture
def install(monkeypatch):
    from lib.llm_dispatch import api
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    import lib.llm as llm_mod
    import lib.llm.astream as astream_mod

    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)

    def _install(slots, failures=None):
        transport = _Transport(slots, failures or {})
        dispatcher = LLMDispatcher()
        dispatcher._initialized = True
        dispatcher.slots = list(slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
        monkeypatch.setattr(llm_mod, 'stream_chat', transport.stream)
        monkeypatch.setattr(astream_mod, 'async_stream_chat', transport.astream)
        monkeypatch.setattr(llm_mod, 'chat', transport.chat)
        return transport
    return _install


def _dispatch(mode, messages, **kwargs):
    from lib.llm_dispatch import api
    kwargs.setdefault('log_prefix', '[binding]')
    kwargs.setdefault('prefer_model', MODEL)
    kwargs.setdefault('strict_model', True)
    kwargs.setdefault('max_retries', 3)
    if mode == 'stream':
        return api.dispatch_stream(messages, **kwargs)
    if mode == 'astream':
        return asyncio.run(api.async_dispatch_stream(messages, **kwargs))
    return api.dispatch_chat(messages, **kwargs)


@pytest.mark.parametrize('mode', MODES)
def test_failover_rotates_origin_credentials_instead_of_crossing_deployments(
        install, mode):
    kunlun_a, public, kunlun_b = _gpt_route_group()
    transport = install([kunlun_a, public, kunlun_b], {
        kunlun_a.key_name: BadRequestError('gateway rejected this pair')})

    _dispatch(mode, _history())

    assert transport.calls == [kunlun_a.key_name, kunlun_b.key_name]
    assert public.total_requests == 0 and public.inflight == 0


@pytest.mark.parametrize('mode', MODES)
def test_origin_that_cannot_serve_fails_with_its_own_error(install, mode):
    kunlun_a, public, kunlun_b = _gpt_route_group()
    rejection = BadRequestError('gateway rejected this pair')
    transport = install([kunlun_a, public, kunlun_b], {
        kunlun_a.key_name: rejection, kunlun_b.key_name: rejection})

    with pytest.raises(BadRequestError):
        _dispatch(mode, _history())

    assert public.key_name not in transport.calls
    assert all(slot.inflight == 0 for slot in (kunlun_a, public, kunlun_b))


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('history', [
    pytest.param(_history(origin=''), id='unmarked-state'),
    pytest.param(_history(origin='deployment-other-model'), id='origin-out-of-scope'),
])
def test_unbound_requests_keep_cross_deployment_failover(install, mode, history):
    kunlun_a, public, kunlun_b = _gpt_route_group()
    other = _slot('cred-o:other', 'other-model', 'deployment-other-model',
                  latency=1.0, logical='other-model')
    transport = install([kunlun_a, public, kunlun_b, other], {
        kunlun_a.key_name: BadRequestError('gateway rejected this pair')})

    _dispatch(mode, history)

    assert transport.calls == [kunlun_a.key_name, public.key_name]


@pytest.mark.parametrize('mode', ['stream', 'astream'])
def test_settled_stream_names_the_serving_deployment(install, mode):
    kunlun_a, public, kunlun_b = _gpt_route_group()
    install([kunlun_a, public, kunlun_b])

    result = _dispatch(mode, [{'role': 'user', 'content': 'Start'}])

    items = result.message['_responses_items']
    assert items[-1] == {'type': STATE_ORIGIN_TYPE, 'version': 1,
                         'deployment_id': KUNLUN}
    assert [item['type'] for item in items].count(STATE_ORIGIN_TYPE) == 1
