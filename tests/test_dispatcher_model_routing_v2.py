"""The shared personal Slot pool materializes only model-routing v2 state."""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    OwnerBoundary,
)


pytestmark = pytest.mark.unit


def _document() -> dict:
    alpha = {'creator_id': 'creator', 'model_id': 'alpha'}
    whisper = {'creator_id': 'creator', 'model_id': 'whisper'}
    return {
        'contract_version': 'tofu.model-routing/v2',
        'revision': 0,
        'creators': [{'creator_id': 'creator', 'name': 'Creator'}],
        'models': [{
            **alpha,
            'display_name': 'Alpha',
            'capabilities': ['text', 'thinking'],
            'context_window': 100_000,
            'quality_rank': 10,
        }, {
            **whisper,
            'display_name': 'Whisper',
            'capabilities': ['transcription'],
            'context_window': 10_000,
            'quality_rank': 1,
        }],
        'providers': [{
            'provider_id': 'provider-a',
            'name': 'Provider A',
            'scope': 'owner',
        }, {
            'provider_id': 'provider-b',
            'name': 'Provider B',
            'scope': 'owner',
        }],
        'provider_accesses': [{
            'provider_access_id': 'access-a',
            'provider_id': 'provider-a',
            'enabled': True,
            'quota_policy': {'rpm': 31},
        }, {
            'provider_access_id': 'access-b',
            'provider_id': 'provider-b',
            'enabled': True,
            'quota_policy': {},
        }],
        'connections': [{
            'connection_id': 'connection-a',
            'provider_access_id': 'access-a',
            'base_url': 'https://a.example/v1',
            'protocol': 'openai',
            'enabled': True,
            'priority': 0,
            'extra_headers': {'X-Route': 'a'},
        }, {
            'connection_id': 'connection-b',
            'provider_access_id': 'access-b',
            'base_url': 'https://b.example/v1',
            'protocol': 'anthropic',
            'enabled': True,
            'priority': 0,
            'extra_headers': {},
        }],
        'credentials': [{
            'credential_id': 'credential-a',
            'provider_access_id': 'access-a',
            'kind': 'local_identity',
            'secret_reference': '',
            'key_hint': '',
            'enabled': True,
            'authorization': {
                'connection_ids': ['connection-a'],
                'models': [alpha, whisper],
            },
            'quota_policy': {},
        }, {
            'credential_id': 'credential-b',
            'provider_access_id': 'access-b',
            'kind': 'local_identity',
            'secret_reference': '',
            'key_hint': '',
            'enabled': True,
            'authorization': {
                'connection_ids': ['connection-b'],
                'models': [alpha],
            },
            'quota_policy': {},
        }],
        'offerings': [{
            'offering_id': 'alpha-a',
            'provider_access_id': 'access-a',
            'identity_state': 'confirmed',
            'model': alpha,
            'enabled': True,
            'stale': False,
            'capabilities': ['text', 'thinking'],
            'context_window': 100_000,
            'priority': 0,
        }, {
            'offering_id': 'alpha-b',
            'provider_access_id': 'access-b',
            'identity_state': 'confirmed',
            'model': alpha,
            'enabled': True,
            'stale': False,
            'capabilities': ['text'],
            'context_window': 80_000,
            'priority': 1,
        }, {
            'offering_id': 'whisper-a',
            'provider_access_id': 'access-a',
            'identity_state': 'confirmed',
            'model': whisper,
            'enabled': True,
            'stale': False,
            'capabilities': ['transcription'],
            'context_window': 10_000,
            'priority': 0,
        }],
        'deployments': [{
            'deployment_id': 'alpha-deployment-a',
            'offering_id': 'alpha-a',
            'connection_id': 'connection-a',
            'wire_model_id': 'alpha-wire-a',
            'max_output_tokens': 65_535,
            'enabled': True,
            'identity_confidence': 'high',
            'probe_status': 'passed',
            'priority': 0,
        }, {
            'deployment_id': 'alpha-deployment-b',
            'offering_id': 'alpha-b',
            'connection_id': 'connection-b',
            'wire_model_id': 'alpha-wire-b',
            'enabled': True,
            'identity_confidence': 'high',
            'probe_status': 'passed',
            'priority': 0,
        }, {
            'deployment_id': 'whisper-deployment-a',
            'offering_id': 'whisper-a',
            'connection_id': 'connection-a',
            'wire_model_id': 'whisper-wire',
            'enabled': True,
            'identity_confidence': 'high',
            'probe_status': 'passed',
            'priority': 0,
        }],
    }


def _build(monkeypatch, document=None, *, mode='personal'):
    import lib.model_routing as model_routing
    from lib.llm_dispatch.dispatcher import LLMDispatcher

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    repository.compare_and_swap(
        boundary, document or _document(), expected_revision=0)
    monkeypatch.setattr(
        model_routing, 'ModelRoutingRepository', lambda: repository)
    monkeypatch.setattr(
        'runtime_guards.load_deployment_configuration',
        lambda: SimpleNamespace(mode=mode))
    dispatcher = LLMDispatcher()
    dispatcher._build_slots_from_model_routing()
    dispatcher._initialized = True
    return dispatcher


def test_v2_materialization_preserves_wire_contract_and_nonchat_slots(
        monkeypatch):
    dispatcher = _build(monkeypatch)
    by_wire = {slot.model: slot for slot in dispatcher.slots}

    assert set(by_wire) == {'alpha-wire-a', 'alpha-wire-b', 'whisper-wire'}
    assert by_wire['alpha-wire-a'].logical_model == 'alpha'
    assert by_wire['alpha-wire-a'].protocol == 'openai'
    assert by_wire['alpha-wire-a'].extra_headers == {'X-Route': 'a'}
    assert by_wire['alpha-wire-a'].rpm_limit == 31
    assert by_wire['alpha-wire-a'].max_output_tokens == 65_535
    assert by_wire['alpha-wire-b'].protocol == 'anthropic'
    assert by_wire['whisper-wire'].capabilities == {'transcription'}
    assert dispatcher._is_chat_compatible(by_wire['whisper-wire']) is False


def test_strict_picker_fails_over_only_within_official_model(monkeypatch):
    dispatcher = _build(monkeypatch)
    first = dispatcher.pick_slot(
        prefer_model='alpha', strict_model=True)
    assert first is not None
    first_provider = first.provider_id
    first.release()
    for slot in dispatcher.slots:
        if slot.provider_id == first_provider:
            slot.cooldown_until = time.time() + 1000

    second = dispatcher.pick_slot(
        prefer_model='alpha', strict_model=True)

    assert second is not None
    assert second.provider_id != first_provider
    assert second.logical_model == 'alpha'
    assert second.model in {'alpha-wire-a', 'alpha-wire-b'}


def test_disabled_access_is_not_materialized(monkeypatch):
    document = copy.deepcopy(_document())
    document['provider_accesses'][1]['enabled'] = False

    dispatcher = _build(monkeypatch, document)

    assert {slot.model for slot in dispatcher.slots} == {
        'alpha-wire-a', 'whisper-wire'}


def test_distributed_mode_has_no_shared_owner_pool(monkeypatch):
    dispatcher = _build(monkeypatch, mode='distributed')

    assert dispatcher.slots == []


def test_key_health_identity_is_owner_and_credential_scoped(monkeypatch):
    from lib.key_stats import _state as key_state
    from lib.llm_dispatch.slot import Slot

    first = Slot(
        key_name='request-random-a',
        api_key='key-a',
        model='wire-a',
        capabilities={'text'},
        provider_id='request-pin-a',
        routing_provider_id='provider-shared-name',
        routing_owner_user_id=11,
        route_credential_id='credential-a',
    )
    second = Slot(
        key_name='request-random-b',
        api_key='key-b',
        model='wire-b',
        capabilities={'text'},
        provider_id='request-pin-b',
        routing_provider_id='provider-shared-name',
        routing_owner_user_id=12,
        route_credential_id='credential-b',
    )
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher',
        lambda: SimpleNamespace(slots=[first, second]))
    key_state._siblings_cache.update({'ts': 0.0, 'by_provider': {}})

    assert first.key_stats_provider_id() == (
        'owner:11:provider-shared-name')
    assert first.key_stats_key_name() == 'credential-a'
    assert key_state._list_siblings(first.key_stats_provider_id()) == [
        'owner:11:provider-shared-name::credential-a']
    assert key_state._list_siblings(second.key_stats_provider_id()) == [
        'owner:12:provider-shared-name::credential-b']
