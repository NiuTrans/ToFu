"""Step 5 Preview: exact first-party registration published by catalogue sync.

Facts checked 2026-11-02 (docs/MODEL_REGISTRATION.md, "Step 5 Preview"): the
official model page fixes the ``step-5-preview`` API id, a 1M-token window,
64K max output, text/image/video input, and the ``low``/``medium``/``high``
``reasoning_effort`` ladder; the reasoning guide names ``medium`` the default
and documents no off rung; the platform.stepfun.ai price card lists
$1.00/$0.10/$2.70 per million input/cache-hit/output tokens; StepFun's news
post opened the API to everyone on 2026-09-20. StepFun had no complete
registration before, so the Models page listed no StepFun Model at all.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'stepfun', 'model_id': 'step-5-preview'}


def _public_model():
    from lib.model_routing import empty_document, public_projection
    from lib.provider.provider_template_recipes import compile_provider_template_bundle

    bundle = compile_provider_template_bundle('stepfun', selected_model_ids=[MODEL['model_id']])
    document = empty_document()
    document['creators'], document['models'] = bundle['creators'], bundle['models']
    return next(row for row in public_projection(document)['models']
                if row['model_id'] == MODEL['model_id'])


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    model = _public_model()
    assert model['display_name'] == 'Step 5 Preview'
    assert model['release_date'] == '2026-09-20'
    assert model['context_window'] == 1_000_000
    assert {'text', 'vision', 'video', 'thinking', 'cheap'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (1.0, 2.7)
    assert price['cache_read'] == pytest.approx(0.10)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['low', 'medium', 'high']
    assert reasoning['default_effort'] == 'medium'
    assert registration_missing_fields(model) == []


def test_world_catalogue_sync_publishes_the_model_ready():
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document
    from lib.model_routing.world_catalog import sync_world_model_catalog
    from lib.provider.provider_template_recipes import load_provider_templates
    from lib.model_routing.world_catalog import withheld_catalog_models

    assert all((row['creator_id'], row['model_id']) != tuple(MODEL.values())
               for row in withheld_catalog_models(load_provider_templates()))
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(497)
    repo.compare_and_swap(owner, empty_document(), expected_revision=0)

    sync_world_model_catalog(boundary=owner, repository=repo)

    model = next(row for row in repo.get(owner).document['models']
                 if row['creator_id'] == 'stepfun' and row['model_id'] == MODEL['model_id'])
    assert model['registration_state'] == 'ready'
