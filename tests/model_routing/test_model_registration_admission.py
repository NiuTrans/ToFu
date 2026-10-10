"""Registration rejects missing facts before saving; exact rules repair routing."""
from copy import deepcopy

import pytest

from lib.model_routing import empty_document, normalize_document, public_projection
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.registration import prepare_registration_document
from lib.llm.reasoning import compile_reasoning

pytestmark = pytest.mark.unit


def document(model_id='synthetic-model', creator='synthetic'):
    doc = empty_document()
    doc['creators'] = [{'creator_id': creator, 'name': creator}]
    doc['models'] = [{
        'creator_id': creator, 'model_id': model_id, 'display_name': model_id,
        'registration_state': 'ready', 'capabilities': ['text'], 'context_window': 1000000, 'quality_rank': 0,
        'registration_metadata': {'release_date': '2026-09-10', 'checked_at': '2026-09-18',
                                  'source_urls': ['https://example.invalid/model-card']},
        'list_pricing': {'input': 0, 'output': 0, 'currency': 'USD', 'unit': 'per_million_tokens'},
        'inference_profile': {'reasoning': {'support': 'unsupported'}},
    }]
    return doc


@pytest.mark.parametrize('field', ['registration_metadata', 'list_pricing', 'inference_profile'])
def test_incomplete_new_model_rejected_with_research_instructions_without_mutation(field):
    doc = document()
    del doc['models'][0][field]
    before = deepcopy(doc)
    with pytest.raises(ModelRoutingError) as error:
        prepare_registration_document(doc, previous=empty_document())
    assert error.value.kind == 'model_registration_incomplete'
    assert error.value.field == 'models[0]'
    assert field in str(error.value)
    assert 'Search official documentation' in str(error.value)
    assert doc == before


def test_researched_model_roundtrip_preserves_date_evidence_and_explicit_free_price():
    registered = prepare_registration_document(document(), previous=empty_document())
    public = public_projection(registered)
    assert public['models'][0]['release_date'] == '2026-09-10'
    assert public['models'][0]['list_pricing']['input'] == 0
    assert prepare_registration_document(public, previous=registered) == registered


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True, '0'])
def test_invalid_price_cannot_register(value):
    doc = document()
    doc['models'][0]['list_pricing']['input'] = value
    with pytest.raises(ModelRoutingError, match='list_pricing'):
        prepare_registration_document(doc, previous=empty_document())


@pytest.mark.parametrize('changes', [
    {'release_date': '2026-02-30'}, {'checked_at': 'yesterday'},
    {'source_urls': []}, {'source_urls': ['https://']},
    {'source_urls': ['https://user:password@example.invalid']},
    {'source_urls': ['https://example.invalid'] * 9},
])
def test_malformed_research_evidence_rejected(changes):
    doc = document()
    doc['models'][0]['registration_metadata'].update(changes)
    with pytest.raises(ModelRoutingError, match='registration_metadata'):
        normalize_document(doc)


def test_unchanged_legacy_model_does_not_block_repairs_but_edit_requires_completion():
    previous = document()
    row = previous['models'][0]
    row.pop('registration_state')
    for field in ('registration_metadata', 'list_pricing', 'inference_profile'):
        row.pop(field)
    assert prepare_registration_document(previous, previous=previous) == normalize_document(previous)
    edited = deepcopy(previous)
    edited['models'][0]['display_name'] = 'Edited model'
    draft = prepare_registration_document(edited, previous=previous)
    assert draft['models'][0]['registration_state'] == 'pending_metadata'


@pytest.mark.parametrize('model_id', ['deepseek-v4.1-flash', 'deepseek-v4-1-flash', 'deepseek-flash'])
@pytest.mark.parametrize('effort,expected', [('low', 'low'), ('high', 'high'), ('max', 'max'), ('xhigh', 'high')])
def test_deepseek_registration_to_wire_without_network(model_id, effort, expected):
    doc = document(model_id, 'deepseek')
    for field in ('registration_metadata', 'list_pricing', 'inference_profile'):
        doc['models'][0].pop(field)
    registered = prepare_registration_document(doc, previous=empty_document())
    model = public_projection(registered)['models'][0]
    assert model['release_date'] == '2026-09-10'
    assert model['official_pricing']['input'] == .15
    body = {'_reasoning_intent': {'version': 1, 'mode': 'preset', 'value': effort, 'strict': True}}
    compile_reasoning(body, model_id, inference_profile=model['inference_profile'])
    assert body['thinking'] == {'type': 'enabled'}
    assert body['reasoning_effort'] == expected


def test_exact_defaults_preserve_explicit_owner_and_provider_disablement():
    from lib.model_routing.inference import resolve_inference_profile
    doc = document('deepseek-v4.1-flash', 'deepseek')
    model = normalize_document(doc)['models'][0]
    assert model['inference_profile']['reasoning']['support'] == 'unsupported'
    del doc['models'][0]['inference_profile']
    model = normalize_document(doc)['models'][0]
    resolved = resolve_inference_profile(model=model, offering={
        'inference_profile': {'reasoning': {'support': 'unsupported'}}})
    assert resolved['reasoning']['support'] == 'unsupported'


def test_unknown_lookalike_never_inherits_reasoning_from_name():
    doc = document('deepseek-v4.1-flash-custom', 'deepseek')
    del doc['models'][0]['inference_profile']
    with pytest.raises(ModelRoutingError, match='reasoning.support'):
        prepare_registration_document(doc, previous=empty_document())


def test_effort_ladder_and_default_are_required_when_supported():
    doc = document()
    doc['models'][0]['inference_profile'] = {'reasoning': {
        'support': 'supported', 'wire_format': 'thinking_type', 'can_disable': True}}
    with pytest.raises(ModelRoutingError, match='supported_efforts'):
        prepare_registration_document(doc, previous=empty_document())
    doc['models'][0]['inference_profile']['reasoning']['supported_efforts'] = ['low', 'high']
    with pytest.raises(ModelRoutingError, match='default_effort'):
        prepare_registration_document(doc, previous=empty_document())


def test_uninstalled_reasoning_dialect_rejected_before_request_preparation():
    doc = document()
    doc['models'][0]['inference_profile'] = {'reasoning': {
        'support': 'supported', 'wire_format': 'nonexistent_dialect',
        'can_disable': True, 'supported_efforts': ['high'], 'default_effort': 'high'}}
    with pytest.raises(ModelRoutingError, match='controls cannot compile'):
        prepare_registration_document(doc, previous=empty_document())


def test_null_metadata_fails_with_a_domain_error():
    doc = document()
    doc['models'][0]['registration_metadata'] = None
    with pytest.raises(ModelRoutingError, match='registration_metadata'):
        normalize_document(doc)
