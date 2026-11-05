"""GPT-6.1 Terra: exact first-party registration and its Codex supply.

Facts checked 2026-10-28 (docs/MODEL_REGISTRATION.md, "GPT-6.1 Terra"): the
official model page fixes the 1,050,000-token window, 128,000 max output,
text and image input, the ``none``…``max`` effort ladder with ``medium`` as
default, and $1/$0.05/$5 per million input/cache-read/output tokens, with 2x
input/cache and 1.5x output above 272K prompt tokens. The ChatGPT & Codex
changelog and the deployment-safety index both date the release 2026-10-26.

Before this registration the Codex subscription catalog listed the slug as a
pending identity, so selecting it failed every turn start with
``model_registration_incomplete``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'openai', 'model_id': 'gpt-6.1-terra'}
# The oauth_codex catalog row observed on 2026-10-28: a 500K subscription
# window and the subscription's own low…ultra ladder (no ``none`` rung).
CODEX_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']


def _public_model():
    from lib.model_routing import empty_document, public_projection
    from lib.provider.provider_template_recipes import compile_provider_template_bundle

    bundle = compile_provider_template_bundle('openai', selected_model_ids=[MODEL['model_id']])
    document = empty_document()
    document['creators'], document['models'] = bundle['creators'], bundle['models']
    return next(row for row in public_projection(document)['models']
                if row['model_id'] == MODEL['model_id'])


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    model = _public_model()
    assert model['release_date'] == '2026-10-26'
    assert model['context_window'] == 1_050_000
    assert {'text', 'vision', 'thinking'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (1.0, 5.0)
    assert price['cache_read'] == pytest.approx(0.05)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is True
    assert reasoning['supported_efforts'] == ['none', 'low', 'medium', 'high', 'xhigh', 'max']
    assert reasoning['default_effort'] == 'medium'
    assert 'temperature' in model['inference_profile']['sampling']['omit']
    assert registration_missing_fields(model) == []


def test_contract_prices_the_long_context_tier_and_caps_output():
    from lib.llm import build_body
    from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS
    from lib.model_info._context import context_profile
    from lib.pricing import lookup_pricing

    short = lookup_pricing(MODEL['model_id'], prompt_tokens=272_000)
    long = lookup_pricing(MODEL['model_id'], prompt_tokens=272_001)
    assert (short['input'], short['output']) == (1.0, 5.0)
    assert (long['input'], long['output']) == (2.0, 7.5)
    assert short['cacheReadMul'] == long['cacheReadMul'] == pytest.approx(0.05)
    assert short['cacheWriteMul'] == long['cacheWriteMul'] == 1.25
    assert long['selectedTier']['id'] == 'ctx_gt_272000'

    window = context_profile(MODEL['model_id'])
    assert (window['window'], window['exact']) == (1_050_000, True)
    built = build_body(MODEL['model_id'], [{'role': 'user', 'content': 'work'}],
                       max_tokens=200_000, thinking_enabled=True, stream=False)
    assert built['max_tokens'] == 128_000
    assert 'cheap' in DEFAULT_SLOT_CONFIGS[MODEL['model_id']]['caps']


def test_codex_catalog_supply_binds_to_the_registered_model(monkeypatch):
    from lib.model_routing import (
        InMemoryModelRoutingRepository, ModelRef, NativeModelSelection,
        OwnerBoundary, compile_candidates,
    )
    from lib.oauth import codex  # Install its refresh hooks before patching token I/O.
    from lib.oauth import codex_catalog, outbound

    assert codex is not None
    rows = codex_catalog._normalise_models({'models': [{
        'slug': MODEL['model_id'], 'visibility': 'list', 'context_window': 500_000,
        'input_modalities': ['text', 'image'], 'default_reasoning_level': 'low',
        'supported_reasoning_levels': [{'effort': effort} for effort in CODEX_EFFORTS],
    }]})
    monkeypatch.setattr(codex_catalog, 'cached_codex_provider_models',
                        lambda: codex_catalog._provider_models(rows))
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(495)
    entry = outbound._managed_oauth_entry('codex', plan_type='test')
    assert outbound._upsert_oauth_provider(entry, owner_user_id=owner.owner_user_id,
                                         tenant_id=None, repository=repo)

    document = repo.get(owner).document
    model = next(row for row in document['models'] if row['model_id'] == MODEL['model_id'])
    assert model['registration_state'] == 'ready'
    offering = next(row for row in document['offerings']
                    if row.get('model') == MODEL)
    assert offering['identity_state'] == 'confirmed'
    assert offering['context_window'] == 500_000
    candidates = compile_candidates(
        document, NativeModelSelection(ModelRef(**MODEL), None, ''))
    assert candidates
    assert not candidates[0].deployment.get('registration_issues')


def test_stored_pending_identity_binds_on_catalog_sync():
    """An installation that imported the slug before the rule binds on sync."""
    from lib.model_routing import (
        InMemoryModelRoutingRepository, ModelRef, NativeModelSelection,
        OwnerBoundary, compile_candidates,
    )
    from lib.model_routing.world_catalog import sync_world_model_catalog
    from tests.model_routing.test_model_routing_contract import _full_document

    document = _full_document()
    document['models'] = []
    for collection in ('providers', 'provider_accesses', 'connections',
                       'credentials', 'offerings', 'deployments'):
        document[collection] = document[collection][:1]
    offering = document['offerings'][0]
    offering.pop('model')
    offering.update(identity_state='pending_identity', pending_model_id=MODEL['model_id'],
                    capabilities=['text', 'thinking', 'vision'], context_window=500_000,
                    inherited_model_fields=[])
    document['credentials'][0]['authorization']['models'] = []
    document['deployments'][0].update(wire_model_id=MODEL['model_id'],
                                      identity_confidence='pending')
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(496)
    repo.compare_and_swap(owner, document, expected_revision=0)

    summary = sync_world_model_catalog(boundary=owner, repository=repo)

    assert summary['identities_bound'] == 1
    after = repo.get(owner).document
    assert after['offerings'][0]['identity_state'] == 'confirmed'
    assert after['offerings'][0]['model'] == MODEL
    assert after['deployments'][0]['wire_model_id'] == MODEL['model_id']
    assert MODEL in after['credentials'][0]['authorization']['models']
    assert compile_candidates(after, NativeModelSelection(ModelRef(**MODEL), None, ''))
