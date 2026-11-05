"""Mistral Large 4: the 2026-10-29 contract and its exact AA identity.

Facts checked 2026-11-02 (docs/MODEL_REGISTRATION.md, "Mistral Large 4"):
Mistral serves the improved 2026-10-29 version under the same API ids
(``mistral-large-4`` / ``mistral-large-4-0``) at $0.70/$0.07/$2.10 per million
input/cache-read/output tokens with the ``low``/``medium``/``high`` effort
ladder (``high`` the recommended default). It no longer accepts ``none``: the
preview's ``none``/``high`` ladder and $1.36/$4.18 list price are retired, so
the old profile turned every "reasoning off" request into a rejected call.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'mistral', 'model_id': 'mistral-large-4-0'}


def _public_model():
    from lib.model_routing import empty_document, public_projection
    from lib.provider.provider_template_recipes import compile_provider_template_bundle

    bundle = compile_provider_template_bundle('mistral', selected_model_ids=[MODEL['model_id']])
    document = empty_document()
    document['creators'], document['models'] = bundle['creators'], bundle['models']
    return next(row for row in public_projection(document)['models']
                if row['model_id'] == MODEL['model_id'])


def test_official_template_registers_the_current_contract():
    from lib.model_routing.registration import registration_missing_fields

    model = _public_model()
    assert model['display_name'] == 'Mistral Large 4'
    assert model['release_date'] == '2026-10-06'
    assert model['context_window'] == 1_000_000
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (0.7, 2.1)
    assert price['cache_read'] == pytest.approx(0.07)
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['low', 'medium', 'high']
    assert reasoning['default_effort'] == 'high'
    assert registration_missing_fields(model) == []


@pytest.mark.parametrize(('intent', 'wire'), [
    ({'version': 1, 'mode': 'enabled', 'strict': True}, 'high'),
    ({'version': 1, 'mode': 'off', 'strict': False}, 'low'),
    ({'version': 1, 'mode': 'exact', 'value': 'medium', 'strict': True}, 'medium'),
])
def test_reasoning_never_sends_the_retired_none_rung(intent, wire):
    from lib.llm.reasoning import compile_reasoning

    body = compile_reasoning({'_reasoning_intent': intent}, MODEL['model_id'],
                             inference_profile=_public_model()['inference_profile'])
    assert body['reasoning_effort'] == wire


def test_aa_score_attaches_by_the_official_name_not_by_suffix_stripping():
    """AA lists the bare model as ``mistral-large-4``; the retired preview
    and the medium-effort run are separate rows that must not attach."""
    from lib.model_catalog import aa

    def row(name, slug, score):
        return {'name': name, 'slug': slug, 'model_creator': {'name': 'Mistral'},
                'evaluations': {'artificial_analysis_intelligence_index': score}}

    parsed = aa._parse_dataset({'data': [
        row('Mistral Large 4 (High)', 'mistral-large-4', 26.0),
        row('Mistral Large 4 Preview', 'mistral-large-4-preview', 25.3),
        row('Mistral Large 4 (Medium)', 'mistral-large-4-medium', 23.1),
    ]})
    dataset = {'status': 'ok', 'fetched_at': 1.0, 'models': parsed, 'index_version': 5}
    model = _public_model()

    named = aa._block([model], dataset, key_source='env', key_hint='')
    id_echo = aa._block([{**model, 'display_name': MODEL['model_id']}], dataset,
                        key_source='env', key_hint='')

    score = named['scores']['mistral::mistral-large-4-0']
    assert (score['intelligence'], score['aa_slug']) == (26.0, 'mistral-large-4')
    assert id_echo['scores'] == {}


def test_installed_preview_registration_moves_to_the_current_contract_on_sync():
    """An authority saved before 2026-10-29 holds the shipped preview profile
    and an id-echo name; catalogue sync must replace both, because the preview
    profile is a superseded shipped default, not an owner edit."""
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document
    from lib.model_routing.world_catalog import sync_world_model_catalog

    installed = {
        **MODEL, 'display_name': MODEL['model_id'],
        'capabilities': ['text', 'thinking', 'vision'], 'context_window': 1_000_000,
        'quality_rank': 50, 'lifecycle': 'stable',
        'inference_profile': {'source': 'official', 'reasoning': {
            'support': 'supported', 'wire_format': 'reasoning_effort',
            'can_disable': True, 'supported_efforts': ['none', 'high'],
            'default_effort': 'high'}},
    }
    document = empty_document()
    document['creators'] = [{'creator_id': 'mistral', 'name': 'Mistral AI'}]
    document['models'] = [installed]
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(499)
    repo.compare_and_swap(owner, document, expected_revision=0)

    sync_world_model_catalog(boundary=owner, repository=repo)

    model = next(row for row in repo.get(owner).document['models']
                 if row['model_id'] == MODEL['model_id'])
    assert model['display_name'] == 'Mistral Large 4'
    reasoning = model['inference_profile']['reasoning']
    assert (reasoning['can_disable'], reasoning['supported_efforts']) == (
        False, ['low', 'medium', 'high'])
