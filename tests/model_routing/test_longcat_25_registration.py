"""LongCat-2.5 knowledge and the Meituan gateway's LongCat-2.5 / Hy4 recipes.

Facts checked 2026-11-06 (docs/MODEL_REGISTRATION.md, "LongCat-2.5"): the
LongCat API change log makes 2.5 available 2026-11-02 (2.5-Preview
2026-09-25); the model-parameter page fixes a 1,048,576 context and 262,144
max_tokens and the ``thinking`` enabled/disabled switch. The list price
(¥2/¥8 per million, ¥0.04 cache hit) is the LongCat API platform's pricing
card, supplied by the owner the same day.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

LONGCAT = {'creator_id': 'meituan', 'model_id': 'LongCat-2.5'}
HY4 = {'creator_id': 'tencent', 'model_id': 'hy4'}


def _meituan_bundle():
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates,
    )
    templates = load_provider_templates()
    return compile_provider_template_bundle(
        'meituan', official_directory=official_model_directory(templates),
        _templates=templates)


def _wire_ids(bundle, model):
    offerings = {o['offering_id'] for o in bundle['offerings']
                 if o.get('model') == model}
    return [d['wire_model_id'] for d in bundle['deployments']
            if d['offering_id'] in offerings]


def test_meituan_gateway_offers_both_public_wire_ids():
    bundle = _meituan_bundle()
    assert _wire_ids(bundle, LONGCAT) == ['LongCat-2.5']
    assert _wire_ids(bundle, HY4) == ['hy4']


def test_longcat_25_model_carries_the_documented_facts():
    from lib.model_info._context import context_profile
    from lib.model_info._release import release_date

    row = next(m for m in _meituan_bundle()['models']
               if m['model_id'] == 'LongCat-2.5')
    assert row['creator_id'] == 'meituan'
    assert row['context_window'] == 1_048_576
    assert {'text', 'vision', 'thinking'} <= set(row['capabilities'])
    assert release_date('LongCat-2.5') == '2026-11-02'
    assert release_date('LongCat-2.5-Preview') == '2026-09-25'
    assert release_date('LongCat-2.0') == '2026-06'
    assert context_profile('LongCat-2.5')['window'] == 1_048_576
    assert context_profile('LongCat-2.0')['window'] == 1_000_000


@pytest.mark.parametrize(('depth', 'thinking'), [
    ('off', 'disabled'), ('high', 'enabled'),
])
def test_shipped_profile_compiles_the_thinking_switch(depth, thinking):
    from lib.llm.body import build_body
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(
        ModelRef.from_value(LONGCAT))
    body = build_body('LongCat-2.5', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, inference_profile=profile)
    assert body['thinking'] == {'type': thinking}


def test_world_catalogue_sync_publishes_the_model_ready_with_its_list_price():
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog

    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)
    row = next(m for m in repository.get(boundary).document['models']
               if (m['creator_id'], m['model_id']) == ('meituan', 'LongCat-2.5'))
    assert row['registration_state'] == 'ready'
    assert row['list_pricing'] == {'input': 0.276, 'output': 1.105,
                                   'currency': 'USD', 'unit': 'per_million_tokens'}


def test_maintained_price_keeps_the_documented_cache_rate():
    from lib.pricing import lookup_pricing

    row = lookup_pricing('LongCat-2.5', prompt_tokens=1_000)
    assert (row['input'], row['output'], row['cacheReadMul']) == (0.276, 1.105, 0.02)
