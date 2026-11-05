"""Tencent Hy4: exact first-party registration on Tencent Cloud TokenHub.

Facts checked 2026-11-05 (docs/MODEL_REGISTRATION.md, "Tencent Hy4"): Tencent's
2026-11-04 announcement adds image understanding and keeps the preview's list
price (¥6/¥18 per million, ¥0.3 cache hit); the TokenHub model list fixes the
``hy4`` API id and the 1M window; the deep-thinking guide documents thinking
on by default with ``reasoning_effort`` ``high`` (default) and ``no_think``.
``hy4`` is a substring of ``hy4-preview``, so the release and context needles
must keep the preview's own facts.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'tencent', 'model_id': 'hy4'}


def _bundle(key):
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates,
    )
    templates = load_provider_templates()
    return compile_provider_template_bundle(
        key, official_directory=official_model_directory(templates),
        _templates=templates)


def _wire_ids(bundle):
    offerings = {o['offering_id'] for o in bundle['offerings']
                 if o.get('model') == MODEL}
    return [d['wire_model_id'] for d in bundle['deployments']
            if d['offering_id'] in offerings]


def _public_model(bundle):
    from lib.model_routing.domain import normalize_document, public_projection
    row = next(m for m in bundle['models'] if m['model_id'] == MODEL['model_id'])
    document = normalize_document({
        'contract_version': 'tofu.model-routing/v2', 'revision': 0,
        'creators': bundle['creators'], 'models': [row], 'providers': [],
        'provider_accesses': [], 'connections': [], 'credentials': [],
        'offerings': [], 'deployments': [],
    })
    return public_projection(document)['models'][0]


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    bundle = _bundle('tencent')
    assert _wire_ids(bundle) == ['hy4']
    model = _public_model(bundle)
    assert model['release_date'] == '2026-11-04'
    assert model['context_window'] == 1_000_000
    assert {'text', 'vision', 'thinking'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (0.829, 2.486)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is True
    assert reasoning['supported_efforts'] == ['no_think', 'high']
    assert reasoning['default_effort'] == 'high'
    assert reasoning['disabled_effort'] == 'no_think'
    assert reasoning['preserve_history'] == 'replay'
    assert registration_missing_fields(model) == []


def test_world_catalogue_sync_publishes_the_model():
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog

    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)

    published = {(row['creator_id'], row['model_id'])
                 for row in repository.get(boundary).document['models']}
    assert (MODEL['creator_id'], MODEL['model_id']) in published


def test_maintained_price_keeps_the_documented_cache_rate():
    from lib.pricing import lookup_pricing

    row = lookup_pricing('hy4', prompt_tokens=1_000)
    assert (row['input'], row['output'], row['cacheReadMul']) == (0.829, 2.486, 0.05)


@pytest.mark.parametrize(('depth', 'effort'), [
    ('off', 'no_think'), ('low', 'high'), ('medium', 'high'), ('high', 'high'),
    ('max', 'high'), ('default', 'high'),
])
def test_shipped_profile_compiles_the_documented_effort_controls(depth, effort):
    from lib.llm.body import build_body
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(ModelRef.from_value(MODEL))
    body = build_body('hy4', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, inference_profile=profile)
    assert body['reasoning_effort'] == effort
    assert 'thinking' not in body
