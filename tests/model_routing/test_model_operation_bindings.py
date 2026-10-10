"""Installed operation adapters are necessary, never sufficient authorization."""
from __future__ import annotations

import pytest

from lib.model_routing import (
    ModelRoutingError, list_capability_routes, mint_capability_slot_group,
)
from lib.model_routing.operations import (
    CHAT_PROTOCOLS, OPENAI_OPERATION_PROTOCOLS, has_operation_binding,
    protocols_for_capability,
)
from tests.model_routing.test_model_routing_capability_adapter import _owner_with_image_route

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('capability', ['image_gen', 'embedding', 'transcription', 'tts'])
def test_nonchat_binding_never_inherits_a_chat_protocol(capability):
    assert protocols_for_capability(capability) == OPENAI_OPERATION_PROTOCOLS
    for protocol in ('anthropic', 'openai_responses', 'custom'):
        assert not has_operation_binding(capability, protocol)


def test_unknown_operation_is_not_guessed_from_a_capability_name():
    assert protocols_for_capability('future_vendor_feature') == frozenset()
    assert protocols_for_capability('text') == CHAT_PROTOCOLS


@pytest.mark.parametrize('protocol', ['anthropic', 'openai_responses', 'custom'])
def test_advertised_image_capability_does_not_offer_a_nonexecuting_route(protocol):
    repository, boundary = _owner_with_image_route(41)
    authority = repository.get(boundary)
    document = authority.document
    document['connections'][0]['protocol'] = protocol
    repository.compare_and_swap(boundary, document, expected_revision=authority.revision)
    assert list_capability_routes(repository, boundary, 'image_gen') == []
    assert list_capability_routes(
        repository, boundary, 'image_gen', required_protocols=frozenset({protocol})) == []
    with pytest.raises(ModelRoutingError) as failure:
        mint_capability_slot_group(repository, boundary, 'image_gen')
    assert failure.value.kind == 'model_route_unavailable'


@pytest.mark.parametrize('prefer_model', ['', 'local-image-v1'])
def test_caller_protocols_cannot_widen_installed_bindings(monkeypatch, prefer_model):
    repository, boundary = _owner_with_image_route(41)
    monkeypatch.setattr(
        'lib.model_routing.capability_adapter.mint_routed_slot_group',
        lambda *_a, **_kw: pytest.fail('incompatible operation must fail before minting'),
    )
    with pytest.raises(ModelRoutingError) as failure:
        mint_capability_slot_group(
            repository, boundary, 'image_gen', prefer_model=prefer_model,
            required_protocols=frozenset({'anthropic', 'openai_responses'}))
    assert failure.value.kind == 'model_operation_unavailable'


def test_explicit_model_selection_still_carries_the_operation_binding(monkeypatch):
    repository, boundary = _owner_with_image_route(41)
    observed = {}
    sentinel = object()

    def mint(*args, **kwargs):
        observed.update(kwargs)
        return sentinel

    monkeypatch.setattr('lib.model_routing.capability_adapter.mint_routed_slot_group', mint)
    model, group = mint_capability_slot_group(
        repository, boundary, 'image_gen', prefer_model='local-image-v1',
        preferred_provider_id='owner_41_images',
        required_protocols=frozenset({'local', 'anthropic'}))
    assert model == 'local-image-v1'
    assert group is sentinel
    assert observed['policy'].required_protocols == frozenset({'local'})
    assert observed['policy'].required_capabilities == frozenset({'image_gen'})
