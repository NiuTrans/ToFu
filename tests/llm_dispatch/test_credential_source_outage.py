"""Credential-source outages route around one credential, never the request.

2026-10-23: a missing ``CODEX_HOME`` made every Codex-subscription bearer read
fail before provider ingress. Nine pooled slots shared that ONE credential, so
a cheap call that picked one of them failed outright, and every later request
rediscovered the same outage. 2026-10-24: an expired Codex login whose refresh
was rejected raised ``SubscriptionLoginRequiredError`` instead; it was never
learned, so it stayed the top-ranked summary route for every compaction.

Pins (sync stream, async stream and non-stream chat loops):
  * one proven-unsent ``SubscriptionCredentialUnavailableError`` or
    ``SubscriptionLoginRequiredError`` excludes every slot sharing the
    credential; the request succeeds on another eligible slot without
    spending its retry budget or charging slot health;
  * strict_model never switches logical model: a lone outaged credential
    re-raises the ORIGINAL typed error object;
  * every other local preparation failure still surfaces immediately;
  * the outage is an owner-scoped learned fact: the next request starts on
    another slot, the cooled credential is still tried when nothing else
    qualifies, and a verified success clears it.
"""
from __future__ import annotations

import asyncio

import pytest

from lib.llm.llm_errors import (
    LocalRequestPreparationError,
    SubscriptionCredentialUnavailableError,
    SubscriptionLoginRequiredError,
)
from lib.model_routing.health import (
    HealthTarget,
    existing_owner_health_registry,
    owner_health_learned,
)

pytestmark = pytest.mark.unit

OWNER = 7
OUTAGED = 'cred_codex'
MODES = ('stream', 'astream', 'chat')


def _slot(key, model, *, credential, owner=OWNER, latency=1.0):
    from lib.llm_dispatch.slot import Slot
    slot = Slot(key_name=key, api_key=f'sk-{key}', model=model,
                capabilities={'text'}, routing_owner_user_id=owner,
                route_credential_id=credential)
    slot.latency_ema = latency
    return slot


def _codex_pool(count=9, model='gpt-5.4-mini'):
    return [_slot(f'{OUTAGED}:dep{index}', model, credential=OUTAGED)
            for index in range(count)]


def _healthy():
    return _slot('cred_other:dep0', 'other-model', credential='cred_other',
                 latency=500.0)


def _outage():
    cause = SubscriptionCredentialUnavailableError(
        reason='codex_home_unavailable', credential_source='codex_shared_login',
        transient=True)
    error = LocalRequestPreparationError(
        f'local request preparation failed: {cause}', stage='wire_projection')
    error.__cause__ = cause
    return error


def _sign_in():
    cause = SubscriptionLoginRequiredError('Codex subscription not logged in')
    error = LocalRequestPreparationError(
        f'local request preparation failed: {cause}', stage='wire_projection')
    error.__cause__ = cause
    return error


UNSENT_CREDENTIAL_FAILURES = pytest.mark.parametrize(
    'failure', [_outage, _sign_in], ids=['credential-source', 'sign-in'])


class _Transport:
    """Provider attempt fake keyed by the selected slot's api key."""

    def __init__(self, slots, failures):
        self.by_key = {slot.api_key: slot for slot in slots}
        self.failures = failures
        self.calls = []

    def _attempt(self, api_key):
        slot = self.by_key[api_key]
        self.calls.append(slot.key_name)
        failure = self.failures.get(slot.route_credential_id)
        if failure is not None:
            raise failure()

    def stream(self, body, **kwargs):
        self._attempt(kwargs['api_key'])
        return 'ok', 'stop', {'completion_tokens': 1}

    async def astream(self, body, **kwargs):
        self._attempt(kwargs['api_key'])
        return 'ok', 'stop', {'completion_tokens': 1}

    def chat(self, *args, **kwargs):
        self._attempt(kwargs['api_key'])
        return 'ok', {'completion_tokens': 1}


@pytest.mark.parametrize('cause', [
    SubscriptionCredentialUnavailableError(
        reason='codex_home_unavailable', credential_source='codex_shared_login',
        transient=True),
    SubscriptionLoginRequiredError('Codex subscription not logged in'),
], ids=['credential-source', 'login-required'])
def test_nonstream_chat_types_oauth_preparation_failures(monkeypatch, cause):
    """The real non-stream boundary types OAuth failures like the stream path.

    Untyped, they reached dispatch_chat's generic handler, which charged slot
    health for a credential that never reached the provider and bypassed the
    credential-outage routing above.
    """
    import importlib
    chat_module = importlib.import_module('lib.llm.chat')
    posted = []

    def _resolve(*_args, **_kwargs):
        raise cause

    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request', _resolve)
    monkeypatch.setattr(chat_module, 'http_post',
                        lambda *args, **kwargs: posted.append(kwargs))
    with pytest.raises(LocalRequestPreparationError) as raised:
        chat_module.chat([{'role': 'user', 'content': 'hi'}], 'gpt-5.4-mini',
                         api_key='managed',
                         base_url='https://example.invalid/codex',
                         api_protocol='responses', oauth='codex',
                         max_retries=0, owner_user_id=OWNER)
    assert raised.value.__cause__ is cause
    assert raised.value.provider_reached is False
    assert posted == []


@pytest.fixture
def install(monkeypatch):
    from lib.llm_dispatch import api
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    import lib.llm as llm_mod
    import lib.llm.astream as astream_mod

    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)

    def _install(slots, failures):
        transport = _Transport(slots, failures)
        dispatcher = LLMDispatcher()
        dispatcher._initialized = True
        dispatcher.slots = list(slots)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
        monkeypatch.setattr(llm_mod, 'stream_chat', transport.stream)
        monkeypatch.setattr(astream_mod, 'async_stream_chat', transport.astream)
        monkeypatch.setattr(llm_mod, 'chat', transport.chat)
        return transport
    return _install


def _dispatch(mode, **kwargs):
    from lib.llm_dispatch import api
    kwargs.setdefault('log_prefix', '[outage]')
    kwargs.setdefault('owner_user_id', OWNER)
    messages = [{'role': 'user', 'content': 'extract the answer'}]
    if mode == 'stream':
        return api.dispatch_stream(messages, **kwargs)
    if mode == 'astream':
        return asyncio.run(api.async_dispatch_stream(messages, **kwargs))
    return api.dispatch_chat(messages, **kwargs)


def _cooled(credential_id, owner=OWNER):
    registry = existing_owner_health_registry(owner, '')
    return registry is not None and registry.target_unavailable(
        HealthTarget('credential', credential_id))


@UNSENT_CREDENTIAL_FAILURES
@pytest.mark.parametrize('mode', MODES)
def test_outage_excludes_every_slot_sharing_the_credential(install, mode, failure):
    codex, healthy = _codex_pool(), _healthy()
    transport = install([*codex, healthy], {OUTAGED: failure})

    _dispatch(mode, max_retries=1)

    assert len(transport.calls) == 2, transport.calls
    assert transport.calls[0].startswith(f'{OUTAGED}:')
    assert transport.calls[1] == healthy.key_name
    assert healthy.last_success_time > 0
    for slot in codex:
        assert (slot.inflight, slot.consecutive_errors, slot.total_errors) == (0, 0, 0)
    assert _cooled(OUTAGED) and not _cooled('cred_other')


@UNSENT_CREDENTIAL_FAILURES
@pytest.mark.parametrize('mode', MODES)
def test_strict_lone_outage_reraises_the_original_typed_error(install, mode, failure):
    codex = _codex_pool(count=3)
    other = _slot('cred_other:dep0', 'other-model', credential='cred_other')
    original = failure()
    transport = install([*codex, other], {OUTAGED: lambda: original})

    with pytest.raises(LocalRequestPreparationError) as raised:
        _dispatch(mode, prefer_model='gpt-5.4-mini', strict_model=True,
                  max_retries=3)

    assert raised.value is original
    assert raised.value.__cause__ is original.__cause__
    assert len(transport.calls) == 1, 'sibling slots of the credential are never tried'
    assert other.total_requests == 0, 'strict_model never switches logical model'


@pytest.mark.parametrize('mode', MODES)
def test_other_preparation_failures_surface_immediately(install, mode):
    def body_failure():
        error = LocalRequestPreparationError(
            'local request preparation failed: opaque reasoning',
            stage='wire_projection')
        error.__cause__ = ValueError('opaque reasoning item cannot be replayed')
        return error

    transport = install([*_codex_pool(count=2), _healthy()], {OUTAGED: body_failure})

    with pytest.raises(LocalRequestPreparationError):
        _dispatch(mode)

    assert len(transport.calls) == 1, transport.calls
    assert not owner_health_learned()


@UNSENT_CREDENTIAL_FAILURES
def test_learned_outage_demotes_but_never_strands_the_credential(install, failure):
    codex, healthy = _codex_pool(count=3), _healthy()
    failures = {OUTAGED: failure}
    transport = install([*codex, healthy], failures)
    _dispatch('stream')
    assert transport.calls[-1] == healthy.key_name

    transport.calls.clear()
    _dispatch('stream')
    assert transport.calls == [healthy.key_name], (
        'the next request starts on another eligible slot')

    # Only the cooled credential serves this model: it is still tried (never
    # a no-slot error), and a verified success clears the learned fact.
    failures.clear()
    transport.calls.clear()
    _dispatch('stream', prefer_model='gpt-5.4-mini', strict_model=True)
    assert len(transport.calls) == 1 and transport.calls[0].startswith(f'{OUTAGED}:')
    assert not _cooled(OUTAGED)


def test_foreign_owner_request_never_teaches_credential_owner_health(install):
    transport = install([*_codex_pool(count=2), _healthy()], {OUTAGED: _outage})

    _dispatch('stream', owner_user_id=OWNER + 1)

    assert transport.calls[-1] == 'cred_other:dep0'
    assert existing_owner_health_registry(OWNER, '') is None


@pytest.mark.parametrize('owner', [None, 0, True, 'owner'])
@pytest.mark.parametrize('mode', MODES)
def test_ownerless_request_never_learns_any_owners_outage(install, mode, owner):
    """An ownerless failure speaks for no owner.

    Every Codex slot shares the outbound resolver, so the request-local
    exclusion spans owners. Learning under each slot's own owner let one
    ownerless failure cool every owner's Codex credential at once.
    """
    codex = _codex_pool(count=2)
    foreign = _slot('cred_foreign:dep0', 'gpt-5.4-mini',
                    credential='cred_foreign', owner=OWNER + 1)
    for slot in (*codex, foreign):
        slot.oauth = 'codex'
    healthy = _healthy()
    transport = install([*codex, foreign, healthy],
                        {OUTAGED: _outage, 'cred_foreign': _outage})

    _dispatch(mode, owner_user_id=owner)

    assert transport.calls[-1] == healthy.key_name
    assert len(transport.calls) == 2, 'the shared resolver is still excluded for the call'
    assert not owner_health_learned(), 'no owner table is taught or created'


def test_ownerless_request_never_consults_or_clears_owner_cooldowns(install):
    codex, healthy = _codex_pool(count=2), _healthy()
    failures = {OUTAGED: _outage}
    transport = install([*codex, healthy], failures)
    _dispatch('stream')
    assert _cooled(OUTAGED)

    failures.clear()
    transport.calls.clear()
    _dispatch('stream', owner_user_id=None)

    assert len(transport.calls) == 1 and transport.calls[0].startswith(f'{OUTAGED}:'), (
        'an owner table never steers an ownerless request')
    assert _cooled(OUTAGED), 'nor does an ownerless success clear the owner fact'


def test_shared_oauth_resolver_is_the_same_credential_source():
    from lib.llm_dispatch._credential_outage import _shares_credential_source
    failed = _slot('cred_a:dep0', 'm', credential='cred_a')
    alias = _slot('cred_b:dep0', 'm', credential='cred_b')
    failed.oauth = alias.oauth = 'codex'
    same_credential = _slot('cred_a:dep1', 'n', credential='cred_a')
    unrelated = _slot('cred_c:dep0', 'm', credential='cred_c')
    foreign_owner = _slot('cred_a:dep2', 'm', credential='cred_a', owner=OWNER + 1)

    assert _shares_credential_source(alias, failed)
    assert _shares_credential_source(same_credential, failed)
    assert not _shares_credential_source(unrelated, failed)
    assert not _shares_credential_source(foreign_owner, failed)
