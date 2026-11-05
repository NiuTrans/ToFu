"""Qwen3.8-27B: complete the draft an installation already holds.

Facts checked 2026-11-02 (docs/MODEL_REGISTRATION.md, "Qwen3.8-27B"): the Model
Studio model page fixes the ``qwen3.8-27b`` id, a 1,000,000-token window,
131,072 max output, text/image/video input and the Beijing list price of
CNY 3/12 per million input/output tokens (cache hit CNY 0.6); the
newly-released-models notes date API availability to 2026-08-17. The exact
``enable_thinking`` defaults already existed, so the only missing admission
fact was the release date: relay discovery had saved the identity as a
non-callable ``pending_metadata`` draft that never left the Models page queue.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'alibaba', 'model_id': 'qwen3.8-27b'}


def _official_model():
    from lib.model_routing import empty_document, public_projection
    from lib.provider.provider_template_recipes import compile_provider_template_bundle

    bundle = compile_provider_template_bundle('qwen', selected_model_ids=[MODEL['model_id']])
    document = empty_document()
    document['creators'], document['models'] = bundle['creators'], bundle['models']
    return next(row for row in public_projection(document)['models']
                if row['model_id'] == MODEL['model_id'])


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    model = _official_model()
    assert model['display_name'] == 'Qwen3.8-27B'
    assert model['release_date'] == '2026-08-17'
    assert model['context_window'] == 1_000_000
    assert {'text', 'vision', 'video', 'thinking'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (0.41, 1.66)
    assert price['cache_read'] == pytest.approx(0.082)
    assert model['inference_profile']['reasoning']['wire_format'] == 'enable_thinking'
    assert registration_missing_fields(model) == []


def test_stored_draft_becomes_ready_on_its_next_admission():
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document

    draft = {
        **MODEL, 'display_name': MODEL['model_id'],
        'capabilities': ['text', 'vision', 'thinking'], 'context_window': 1_000_000,
        'quality_rank': 50,
        'list_pricing': {'input': 0.42, 'output': 3, 'currency': 'USD',
                         'unit': 'per_million_tokens'},
        'registration_state': 'pending_metadata',
    }
    document = empty_document()
    document['creators'] = [{'creator_id': 'alibaba', 'name': 'Alibaba'}]
    document['models'] = [draft]
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(498)

    committed = repo.compare_and_swap(owner, document, expected_revision=0)

    model = committed.document['models'][0]
    assert model['registration_state'] == 'ready'
    assert model['registration_issues'] == []
